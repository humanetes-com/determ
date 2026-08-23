"""Adapter tests for FileModelNotesStore — the I/O + run-state + LLM-extraction
side of the model-notes conduit (pure rules pinned in test_model_notes.py)."""
from __future__ import annotations

from pipeline_core.adapters.model_notes_store import FileModelNotesStore

HEADER = "## Model Notes — Confusions to Avoid"


class _FakeAsk:
    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)
        self.calls = 0

    def ask(self, prompt, system=None, model=None, max_tokens=None,
            think=None, temperature=None):
        self.calls += 1
        return self._responses[min(self.calls - 1, len(self._responses) - 1)]


def _store(ask=None, log=None):
    return FileModelNotesStore(
        ask or _FakeAsk(""), "m", log or (lambda *a, **k: None),
        max_chars=4000, max_entries=80, failed_body_cap=2000, fix_err_cap=2000)


def _fr(tmp_path, notes):
    fr = tmp_path / "functional_requirements_x.md"
    body = "# FR\n\nspec.\n\n" + HEADER + "\n\n" + "\n".join(f"- {n}" for n in notes) + "\n"
    fr.write_text(body)
    return fr


def test_load_missing_file(tmp_path) -> None:
    assert _store().load(tmp_path / "nope.md") == []


def test_save_and_load_round_trip(tmp_path) -> None:
    s = _store()
    fr = tmp_path / "functional_requirements_x.md"
    s.save(fr, ["first note here", "second note here"])
    assert s.load(fr) == ["first note here", "second note here"]


def test_append_dedup(tmp_path) -> None:
    s = _store()
    fr = _fr(tmp_path, [])
    assert s.append(fr, "a sufficiently long note about ptr casts and deref") is True
    # literal dupe rejected
    assert s.append(fr, "a sufficiently long note about ptr casts and deref") is False
    assert len(s.load(fr)) == 1
    # too short rejected
    assert s.append(fr, "tiny") is False


def test_append_tags_error_codes(tmp_path) -> None:
    s = _store()
    fr = _fr(tmp_path, [])
    s.append(fr, "use the proper construct path not the raw new call here", {"E0599"})
    assert s.load(fr)[0].startswith("[E0599]")


def test_filter_for_prompt_selects_relevant(tmp_path) -> None:
    fr = _fr(tmp_path, ["generic recency note about delegation and ownership",
                        "the MyWidget value must be cloned before capture"])
    out = _store().filter_for_prompt(fr, "src/core.rs", "render",
                                     file_text="pub struct MyWidget { }")
    lines = [ln[2:] for ln in out.splitlines()]
    assert lines.index("the MyWidget value must be cloned before capture") < lines.index(
        "generic recency note about delegation and ownership")


def test_record_and_promote(tmp_path) -> None:
    s = _store()
    # one tally per cargo run (unique codes per call), so 3 runs → count 3
    for _ in range(3):
        s.record_error_codes_seen("error[E0277]: trait bound not satisfied")
    assert s.error_code_counts["E0277"] == 3
    promoted = s.promoted_error_rules()
    assert "E0277" in promoted and "seen 3" in promoted
    # below threshold → empty
    s2 = _store()
    s2.record_error_codes_seen("error[E0308]: mismatch")
    assert s2.promoted_error_rules() == ""


def test_extract_self_note_ladder(tmp_path) -> None:
    note = "this is a sufficiently long self-correction note about lifetimes"
    s = _store(ask=_FakeAsk(note))
    assert s.extract_self_note("failbody", "err excerpt", "fixbody") == note
    # decline → None
    assert _store(ask=_FakeAsk("NONE")).extract_self_note("fb", "err", "fix") is None
    # insufficient inputs short-circuit (no ask)
    fake = _FakeAsk(note)
    assert _store(ask=fake).extract_self_note("", "err", "fix") is None
    assert fake.calls == 0


def test_extract_self_note_retries_on_empty(tmp_path) -> None:
    note = "second attempt yields a long enough note about generic bounds here"
    fake = _FakeAsk("", note)  # first empty → think=False retry
    assert _store(ask=fake).extract_self_note("fb", "err", "fix") == note
    assert fake.calls == 2


def test_escalated_grade_drops_the_regression_note_from_the_prompt(tmp_path) -> None:
    """Slice C, at the seam the prompt is actually built from.

    The `[regression]` note and the ESCALATED grade land in the same prompt and
    say opposite things about the same fn. The measurement is newer, it is
    scoped to one unit where the note's blame is spread over a whole batch, and
    the prompt's instruction is built from it — so the note goes."""
    note = ("[regression] In error.rs, after `construct_from_adhoc` was written, "
            "previously-passing test(s) FAILED: t (cluster pass count dropped "
            "83→25). Body applied:\n"
            "    Self::construct_from_std(MessageError(message), backtrace)")
    other = "I tried X because Y; use Z instead of X in this crate"
    fr = _fr(tmp_path, [note, other])
    tree = "fn construct_from_adhoc() { Self::construct_from_std(MessageError(message), backtrace) }"
    logged: list[str] = []
    store = _store(log=lambda m, *a, **k: logged.append(m))

    ungraded = store.filter_for_prompt(fr, "src/error.rs", "construct_from_adhoc",
                                       file_text=tree)
    assert "construct_from_adhoc` was written" in ungraded      # live without the grade
    graded = store.filter_for_prompt(fr, "src/error.rs", "construct_from_adhoc",
                                     file_text=tree,
                                     escalated_fns=frozenset({"construct_from_adhoc"}))
    assert "construct_from_adhoc` was written" not in graded
    assert other in graded                                      # unrelated notes stay
    assert any("ESCALATED" in m for m in logged)                # the drop is reported


def test_bodyless_regression_note_never_reaches_a_prompt(tmp_path) -> None:
    """Slice A end to end: no grade needed, no FR rewrite needed."""
    bodyless = ("[regression] In error.rs, after `construct_from_display` was "
                "written, previously-passing test(s) FAILED: t (cluster pass "
                "count dropped 83→25). Body applied:")
    fr = _fr(tmp_path, [bodyless, "I tried X because Y; use Z instead here"])
    out = _store().filter_for_prompt(fr, "src/error.rs", "construct_from_display",
                                     file_text="fn construct_from_display() {}")
    assert "[regression]" not in out
    assert "I tried X" in out
