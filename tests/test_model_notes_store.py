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


def _prov(**kw):
    from pipeline_core.domain.model_notes import NoteProvenance
    base = {"unit": "src/a.rs::foo", "error_codes": ("E0599",),
            "rejected_symbols": ("uninit_array",),
            "failed_body_hash": "ab" * 8}
    base.update(kw)
    return NoteProvenance(**base)


def test_append_tags_error_codes(tmp_path) -> None:
    # A lesson is durable only WITH its premise (204 N1) — provenance rides
    # beside the prose here so the tag pin stays about the tag.
    s = _store()
    fr = _fr(tmp_path, [])
    s.append(fr, "use the proper construct path not the raw new call here",
             {"E0599"}, provenance=_prov())
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


# ---------------------------------------------------------------------------
# 204 — the notes-channel contract: N1 (premise kept at write time, else
# run-scoped), N2 (a measured producer grade outranks every remembered
# [E0xxx] lesson), N3 (premise-checked retirement on disk).
# The live instance: the `[E0425]` lesson below sat at line 1213 of prompt
# `f4d593f8…` (run `40938-f4e8e94b`) while the write list ordered the very
# vtable it forbids; the three literal constructors came back
# `ESCALATED, unchanged`.
# ---------------------------------------------------------------------------

E0425_LESSON = (
    "[E0425] I tried building the error with an unsafe constructor and a "
    "manually referenced ad-hoc vtable because the vtable value isn't in "
    "scope, use the std-compatible construction helper that omits the "
    "vtable instead of referencing a manual one.")


def test_n1_a_lesson_without_provenance_is_run_scoped(tmp_path) -> None:
    """No premise recorded → the lesson steers THIS run's prompts and never
    reaches disk, so it cannot become immortal standing guidance."""
    logs: list[str] = []
    s = _store(log=logs.append)
    fr = _fr(tmp_path, [])
    assert s.append(fr, "I tried a manual vtable reference, use the helper "
                        "construction path instead", {"E0425"}) is True
    assert s.load(fr) == []                       # nothing durable
    assert any("run-scoped" in ln for ln in logs)
    out = s.filter_for_prompt(fr, "src/error.rs", "context")
    assert "manual vtable reference" in out       # but it does steer this run
    # a fresh store (a new process) never sees it
    assert _store().filter_for_prompt(fr, "src/error.rs", "context") == ""


def test_n1_a_lesson_with_provenance_is_durable_and_recorded(tmp_path) -> None:
    s = _store()
    fr = _fr(tmp_path, [])
    assert s.append(fr, "I tried a manual vtable reference, use the helper "
                        "construction path instead", {"E0425"},
                    provenance=_prov(rejected_symbols=("vtable",))) is True
    assert len(s.load(fr)) == 1
    sidecar = fr.with_name(fr.name + ".note_provenance.json")
    assert sidecar.exists() and "vtable" in sidecar.read_text()


def test_n2_the_producer_grade_keeps_every_lesson_out_of_the_prompt(tmp_path) -> None:
    """The blanket over the class, not a filter on one note: the live prompt
    carried nine numbered lessons besides the vtable one, and legacy prose
    carries no premise a narrower rule could check (204.3)."""
    logs: list[str] = []
    s = _store(log=logs.append)
    fr = _fr(tmp_path, [
        E0425_LESSON,
        "[E0308] I tried returning a reference, return the owned value.",
        "plain prose advice with no code tag that must survive the drop",
    ])
    out = s.filter_for_prompt(fr, "src/error.rs", "construct_from_context",
                              escalated_grade="REWRITE WITH THE PRODUCER")
    assert "[E0425]" not in out and "[E0308]" not in out
    assert "plain prose advice" in out
    assert any("kept out of the prompt" in ln
               and "REWRITE WITH THE PRODUCER" in ln
               for ln in logs), "the exclusion must be logged with its grade"
    # nothing was retired: the lessons keep steering ordinary fills
    assert any(n.startswith("[E0425]") for n in s.load(fr))


def test_n2_negative_control_no_grade_keeps_the_lessons(tmp_path) -> None:
    """Byte-identical `<MODEL_NOTES>` on the pre-fix shape: a prompt whose
    write list carries no producer grade still renders the `[E0425]` lesson —
    proof the exclusion bites only where the measured grade stands."""
    s = _store()
    fr = _fr(tmp_path, [E0425_LESSON])
    with_flag = s.filter_for_prompt(fr, "src/error.rs", "construct_from_std",
                                    escalated_grade="")
    assert "[E0425]" in with_flag
    assert with_flag == _store().filter_for_prompt(
        fr, "src/error.rs", "construct_from_std")


def test_n3_a_void_premise_retires_the_lesson_on_disk(tmp_path) -> None:
    """The rejected symbol is now writable → the premise the lesson was
    learned under is gone, and the retirement is durable and logged."""
    logs: list[str] = []
    s = _store(log=logs.append)
    fr = _fr(tmp_path, [])
    s.append(fr, "I tried referencing the vtable value directly, use the "
                 "helper construction path instead", {"E0425"},
             provenance=_prov(rejected_symbols=("context_downcast",)))
    assert len(s.load(fr)) == 1
    out = s.filter_for_prompt(
        fr, "src/error.rs", "construct_from_context",
        writable_fns=frozenset({"context_downcast", "construct_from_context"}))
    assert "vtable value" not in out
    assert s.load(fr) == []                       # retired ON DISK
    assert any("VOID premise" in ln and "context_downcast" in ln
               for ln in logs)
    sidecar = fr.with_name(fr.name + ".note_provenance.json")
    assert "context_downcast" not in sidecar.read_text()


def test_n3_negative_control_an_absent_symbol_keeps_the_lesson(tmp_path) -> None:
    """Symbols still missing from tree and write list → the premise stands,
    the lesson survives — and a legacy note with no provenance is never
    retired here (N2 is the only sound rule for it)."""
    s = _store()
    fr = _fr(tmp_path, [])
    s.append(fr, "I tried referencing the vtable value directly, use the "
                 "helper construction path instead", {"E0425"},
             provenance=_prov(rejected_symbols=("nonexistent_symbol",)))
    s.filter_for_prompt(fr, "src/error.rs", "construct_from_context",
                        writable_fns=frozenset({"construct_from_context"}))
    assert any("vtable value" in n for n in s.load(fr))
    # legacy prose (no provenance): untouchable by N3, whatever is writable
    fr2 = tmp_path / "functional_requirements_legacy.md"
    fr2.write_text("# FR\n\nspec.\n\n" + HEADER + "\n\n- " + E0425_LESSON + "\n")
    s.filter_for_prompt(fr2, "src/error.rs", "construct_from_context",
                        writable_fns=frozenset({"construct_from_std"}))
    assert s.load(fr2) == [E0425_LESSON]


def test_n2_fires_on_the_per_block_fallback_grade_and_logs_it(tmp_path) -> None:
    """CHANNEL-level, not route-level: the previous run's file-first
    compile-failed twice and fell back to per-block prompts that re-injected
    the `[E0425]` lesson beside the same ESCALATED rewrite instruction (run
    `40938-f4e8e94b`, iter 2). The per-block caller passes the plain grade;
    the log must name the grade that was actually passed — a bypass-only
    context.rs prompt must not be logged as an installable offer."""
    logs: list[str] = []
    s = _store(log=logs.append)
    fr = _fr(tmp_path, [E0425_LESSON])
    out = s.filter_for_prompt(fr, "src/error.rs", "construct_from_context",
                              escalated_grade="ESCALATED REWRITE")
    assert "[E0425]" not in out
    assert any("ESCALATED REWRITE" in ln and "kept out" in ln for ln in logs)
    assert not any("REWRITE WITH THE PRODUCER" in ln for ln in logs)


def test_hidden_symbols_drop_notes_that_name_an_unreachable_method(
        tmp_path) -> None:
    """A lesson earned on context.rs (`use e.ext_context`) is served to every
    file unless the filter knows the symbol is behind a private-mod wall in
    another file. Same-file-only scoping is wrong: the E0603 note can be
    earned ON error.rs with provenance.unit in error.rs."""
    note = ("[E0277] I tried Error::new(e).context(context) because the "
            "method seemed right; use e.ext_context(context) not "
            "Error::new(e).context(context)")
    other = "generic recency note about ownership and cloning a Widget"
    fr = _fr(tmp_path, [note, other])
    out = _store().filter_for_prompt(
        fr, "src/error.rs", "construct",
        hidden_symbols=frozenset({"ext_context"}))
    assert "ext_context" not in out
    assert "Widget" in out
    kept = _store().filter_for_prompt(
        fr, "src/context.rs", "ext_context",
        hidden_symbols=frozenset())
    assert "ext_context" in kept
