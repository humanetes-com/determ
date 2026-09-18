"""Pin the model-notes relevance-filter + error-rule-promotion conduit.

This is the SOLE learning path into phase-c: per-fn prompts carry the relevance-
filtered MODEL_NOTES block (`_filter_model_notes_for_prompt`) plus the promoted
RECURRING_ERRORS block (`_promoted_error_rules`). Pin what gets selected/dropped
for a known fn, the cap under bloat, and the promotion threshold + past-note
surfacing so the conduit can't silently regress (it's load-bearing and untested).
"""
from __future__ import annotations

import contextlib
from collections import Counter

import pipeline

HEADER = "## Model Notes — Confusions to Avoid"


def _write_fr(tmp_path, notes: list[str]):
    fr = tmp_path / "functional_requirements_x.md"
    body = "# FR\n\nsome spec.\n\n" + HEADER + "\n\n"
    body += "\n".join(f"- {n}" for n in notes) + "\n"
    fr.write_text(body)
    return fr


@contextlib.contextmanager
def _error_counts(counts: dict[str, int], excerpts: dict[str, list[str]] | None = None):
    """Temporarily set the store's run-scoped recurring-error accumulators."""
    store = pipeline._MODEL_NOTES_STORE
    old_c, old_e = store.error_code_counts, store.error_code_excerpts
    store.error_code_counts = Counter(counts)
    store.error_code_excerpts = dict(excerpts or {})
    try:
        yield
    finally:
        store.error_code_counts = old_c
        store.error_code_excerpts = old_e


def test_notes_filter_tier_ordering(tmp_path) -> None:
    """An error-code match (tier 1) outranks an fn-name mention (tier 3), which
    outranks a generic recency-only note (tier 5)."""
    generic = "generic recency-only note about wrappers and delegation"
    fmt_note = "remember to call fmt on the inner Display value before returning"
    ecode = "[E0277] T may be unsized; add where T: Sized or return &T instead"
    fr = _write_fr(tmp_path, [generic, ecode, fmt_note])

    out = pipeline._MODEL_NOTES_STORE.filter_for_prompt(
        fr, "src/error.rs", "fmt",
        file_text="struct Wrapper { }",
        current_error="error[E0277]: the trait bound `T: Sized` is not satisfied",
    )
    lines = [ln[2:] for ln in out.splitlines()]  # strip the "- " bullet
    assert lines[0].startswith("[E0277]"), "error-code note must rank first (tier 1)"
    assert lines.index(fmt_note) < lines.index(generic), "fn-name note (t3) before generic (t5)"


def test_notes_filter_symbol_match_outranks_generic(tmp_path) -> None:
    """A note naming a symbol defined in `file_text` (tier 4) outranks a generic
    recency-only note (tier 5) — pins the `item_names`-driven symbol relevance."""
    generic = "generic recency only note about delegation and ownership"
    sym_note = "the MyWidget value must be cloned before the closure captures it"
    fr = _write_fr(tmp_path, [generic, sym_note])
    out = pipeline._MODEL_NOTES_STORE.filter_for_prompt(
        fr, "src/core.rs", "render",
        file_text="pub struct MyWidget { }\npub fn helper() {}",
    )
    lines = [ln[2:] for ln in out.splitlines()]
    assert lines.index(sym_note) < lines.index(generic), "symbol note (t4) before generic (t5)"


def test_notes_filter_fn_specific_selection_and_drop(tmp_path) -> None:
    """A note naming fn `alpha` is tier-3 (selected first) when filtering for alpha,
    but drops to generic for beta — so the beta-specific note wins for beta."""
    alpha_note = "in alpha remember to clear the state before returning"
    beta_note = "in beta the index starts at one not zero"
    fr = _write_fr(tmp_path, [alpha_note, beta_note])

    first_for = lambda fn: [  # noqa: E731
        ln[2:] for ln in pipeline._MODEL_NOTES_STORE.filter_for_prompt(fr, "src/widget.rs", fn).splitlines()
    ][0]
    assert first_for("alpha") == alpha_note  # selected for alpha
    assert first_for("beta") == beta_note    # alpha-note dropped to generic → beta-note wins


def test_notes_filter_caps_at_bullet_boundary(tmp_path) -> None:
    """Under bloat the block is capped at max_chars and never emits a partial bullet."""
    notes = [f"note {i} about lifetimes generics sized bounds padding padding" for i in range(50)]
    fr = _write_fr(tmp_path, notes)
    out = pipeline._MODEL_NOTES_STORE.filter_for_prompt(fr, "a.rs", "f", max_chars=200)
    assert 0 < len(out) <= 200
    assert all(ln.startswith("- ") for ln in out.splitlines()), "no truncated partial bullet"


def test_notes_filter_empty_when_no_section(tmp_path) -> None:
    fr = tmp_path / "functional_requirements_x.md"
    fr.write_text("# FR\nno notes section here.\n")
    assert pipeline._MODEL_NOTES_STORE.filter_for_prompt(fr, "a.rs", "f") == ""


def test_promotion_threshold(tmp_path) -> None:
    """A code recurs < 3× → not promoted; at 3× → promoted with its canonical hint."""
    with _error_counts({"E0277": 2}):
        assert pipeline._MODEL_NOTES_STORE.promoted_error_rules() == "", "below threshold → nothing promoted"
    with _error_counts({"E0277": 3}):
        out = pipeline._MODEL_NOTES_STORE.promoted_error_rules()
        assert "E0277" in out and "seen 3" in out
        assert "trait bound" in out, "canonical E0277 hint must be surfaced"


def test_promotion_surfaces_past_note(tmp_path) -> None:
    """With an FR, a `[E0277]`-tagged stored note is surfaced under the promoted code
    (the rule the model wrote for itself last time the code fired)."""
    fr = _write_fr(tmp_path, ["[E0277] build via construct_from_display, do not call new"])
    with _error_counts({"E0277": 3}):
        out = pipeline._MODEL_NOTES_STORE.promoted_error_rules(fr)
        assert "past note: build via construct_from_display" in out
    # below threshold → the code isn't promoted, so no past note either.
    with _error_counts({"E0277": 2}):
        assert pipeline._MODEL_NOTES_STORE.promoted_error_rules(fr) == ""
