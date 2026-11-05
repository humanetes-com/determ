"""A body a crash settlement took off a unit reaches every next writer of that
unit, filled or stubbed, in its own section.

Run `630350-93c5a6ee` (2026-11-05, cluster `stable`, fill 2): the owed
`src/error.rs::context_drop_rest` got three file-route attempts, each one line
from right; the second compiled, crashed (`heap-use-after-free`) and was put
back to its previous text by a NARROWED settlement that wrote a refuted ledger
row. The only reader of that row was the held-stub branch, so fill 3's per-fn
re-fill opened with no word of it and failed on the same `E0609` the first
attempt had hit. The fixture is that recorded row
(`tests/fixtures/refuted_body/630350-93c5a6ee_refuted_row.json`).
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from pipeline_core.domain.body_ledger import LedgerEntry
from pipeline_core.domain.crash_settlement import held_stub_evidence
from pipeline_core.domain.crash_site import CrashSites
from pipeline_core.domain.evidence_premise import evidence_fingerprints
from pipeline_core.domain.models import FnLocator, FnSite
from pipeline_core.domain.refuted_body import (
    refuted_notes,
    refuted_text_for,
    with_refuted_notes,
)
from pipeline_core.domain.slice_session import (
    CONTEXT_DROPS,
    SessionSlice,
    omitted_names,
)
from pipeline_core.prompts.context import PerFnFixSections, PerImplSections
from pipeline_core.prompts.fix_prompts import BlockFillPlan, build_file_fill_prompt
from pipeline_core.prompts.per_fn import build_per_fn_fix_prompt
from pipeline_core.prompts.retry_turn import (
    AttemptFeedback,
    refuted_feedback,
    render_retry_turn,
)
from pipeline_core.prompts.sections import REFUTED_BODY_LEAD, REFUTED_BODY_TAG
from pipeline_core.prompts.slice_session import build_slice_session_prompt

_FIXTURE = (Path(__file__).parent / "fixtures" / "refuted_body"
            / "630350-93c5a6ee_refuted_row.json")
_ORACLE = "2676edb08686"
_UNIT = ("src/error.rs", "context_drop_rest")


def _row() -> LedgerEntry:
    e = LedgerEntry.from_dict(json.loads(_FIXTURE.read_text()))
    assert e is not None
    return e


def _note() -> str:
    return refuted_text_for([_row()], _ORACLE, [_UNIT])


def test_the_recorded_refutation_is_told_to_the_filled_unit() -> None:
    """The plan's claim: the next writer of the unit the settlement put back
    to its previous text is told the body that crashed and the crash."""
    note = _note()
    assert "as *mut E as *mut crate::Error" in note      # the body that crashed
    assert "heap-use-after-free" in note                  # its crash
    assert "iteration 2" in note and "Do not write that body again." in note
    # It does not claim the unit is a stub: the tree holds its earlier text.
    assert "stub" not in note.split("That body was:")[0]
    # Scoped to the unit and the oracle.
    assert refuted_text_for([_row()], _ORACLE, [("src/error.rs", "drop")]) == ""
    assert refuted_text_for([_row()], "other-oracle", [_UNIT]) == ""
    # The leading `./` or bare path a writer may hold is the same unit.
    assert refuted_text_for([_row()], _ORACLE, [("./src/error.rs", _UNIT[1])])


def test_a_resolved_frame_keeps_the_note_and_the_dict_merge_loses_it() -> None:
    """Negative control: the note as a crash-evidence dict value is read
    through `CrashSites.evidence_for`, which returns only the resolved
    frame's text once a frame of the name resolved."""
    loc = FnLocator(*_UNIT)
    site = FnSite(_UNIT[0], _UNIT[1], "")
    sites = CrashSites(evidence={site: "SIGSEGV at the new body"})
    merged = {loc: "SIGSEGV at the new body\n\n" + _note()}
    shown = sites.evidence_for(merged, _UNIT[0], _UNIT[1], "")
    assert "as *mut E as *mut crate::Error" not in shown        # lost
    # The section a writer renders after its own crash text keeps it.
    p = build_per_fn_fix_prompt(
        file_text="unsafe fn context_drop_rest() {}", fn_name=_UNIT[1],
        sig_text="unsafe fn context_drop_rest()", test_blocks=[],
        module_name="m", cluster_name="stable", file_rel=_UNIT[0],
        prev_body="", error_excerpt="",
        sections=PerFnFixSections(
            macros="", notes="", container="", types="", invariants="",
            siblings="", crate_idioms="", bound_methods="",
            crash_evidence=shown, refuted_body=_note()),
        fr_path=None)
    assert f"<{REFUTED_BODY_TAG}>" in p and "as *mut E as *mut crate::Error" in p
    assert REFUTED_BODY_LEAD in p


def test_the_note_never_rides_under_the_crash_lead() -> None:
    """A unit with no crash of its own this sweep gets no CRASH_EVIDENCE
    block, whose lead says the body on the tree crashed."""
    p = build_per_fn_fix_prompt(
        file_text="fn f() {}", fn_name="f", sig_text="fn f()", test_blocks=[],
        module_name="m", cluster_name="stable", file_rel="src/lib.rs",
        prev_body="", error_excerpt="",
        sections=PerFnFixSections(
            macros="", notes="", container="", types="", invariants="",
            siblings="", crate_idioms="", bound_methods="",
            refuted_body=_note()),
        fr_path=None)
    assert "<CRASH_EVIDENCE>" not in p and f"<{REFUTED_BODY_TAG}>" in p


def test_every_builder_has_the_slot() -> None:
    file_p = build_file_fill_prompt(
        file_rel=_UNIT[0], file_text="fn a() {}",
        fill_plan=[BlockFillPlan("free fns", (), (_UNIT[1],))],
        tests_block="", blueprint="", notes="", refuted_body=_note())
    assert f"<{REFUTED_BODY_TAG}>" in file_p
    assert "<CRASH_EVIDENCE>" not in file_p
    assert PerImplSections(
        types="", invariants="", notes="", crate_idioms="", dispatch_family="",
        macros="", siblings="", retrospective_guidance="", bound_methods="",
        refuted_body="x").refuted_body == "x"
    # Negative control: no refutation, no section, prompt unchanged.
    plain = build_file_fill_prompt(
        file_rel=_UNIT[0], file_text="fn a() {}",
        fill_plan=[BlockFillPlan("free fns", (), (_UNIT[1],))],
        tests_block="", blueprint="", notes="")
    assert f"<{REFUTED_BODY_TAG}>" not in plain


def test_a_fitted_session_still_sends_the_note() -> None:
    """The fit ladder drops CRASH_EVIDENCE before it refuses a session; the
    refuted body is never dropped."""
    kw: dict[str, object] = {
        "module_name": "m", "cluster_name": "c",
        "slice_": SessionSlice(tests=(), units=()), "records": {},
        "all_records": [], "file_texts": {}, "target_evidence": {},
        "test_blocks": [], "types_section": "",
        "crash_evidence": "SIGSEGV", "refuted_body": _note(),
        "omit": omitted_names(len(CONTEXT_DROPS))}
    p = build_slice_session_prompt(**kw)  # type: ignore[arg-type]
    assert "<CRASH_EVIDENCE>" not in p                 # the control: dropped
    assert f"<{REFUTED_BODY_TAG}>" in p and "heap-use-after-free" in p


def test_a_stub_is_told_through_the_same_reader() -> None:
    """The 2026-10-16 case (`757853-e28f1a7d`): a stub the settlement made is
    told the body it held. The stub sentence itself says only that it is a
    stub; the body comes through `refuted_text_for` like every unit's."""
    plain = held_stub_evidence(3)
    assert plain.endswith("Give it a real body.") and "taken off" not in plain
    assert "heap-use-after-free" in _note()


def test_the_fingerprint_moves_with_the_note_and_both_stamps_agree() -> None:
    """A unit asked before the refutation is asked under new evidence; two
    stamps over the same crash map and notes are equal. The writers' crash
    dict is not mutated."""
    loc = FnLocator(*_UNIT)
    recs = [{"file": _UNIT[0], "fn_name": _UNIT[1], "body_text": "{}"}]
    crash: dict[FnLocator, str] = {}
    notes = refuted_notes([_row()], _ORACLE)
    before = evidence_fingerprints([loc], recs, crash=crash)
    after = evidence_fingerprints([loc], recs, crash=with_refuted_notes(crash, notes))
    again = evidence_fingerprints([loc], recs, crash=with_refuted_notes(crash, notes))
    assert before[loc] != after[loc]
    assert after == again
    assert crash == {}


def test_the_note_is_told_whatever_body_is_on_the_tree() -> None:
    """The note states only history, so no reader compares it with the tree.
    An earlier version omitted it when the current body equalled the refuted
    one, against a body the file, per-impl and pair routes read when the
    fill planned: a body STUBbed by a settlement later in the same fill
    still compared equal, and the note was hidden while the tree held a
    stub. The reader has no on-tree argument at all now."""
    import inspect

    from pipeline_core.domain import refuted_body as rb
    row = _row()
    for reader in (rb.refuted_notes, rb.refuted_text_for):
        assert "on_tree" not in inspect.signature(reader).parameters
    assert not hasattr(rb, "on_tree_bodies")
    note = _note()
    # The crashing bytes back on the tree: the note says so, as history.
    assert "If the body you are shown for this fn is that text, it is the " \
           "one that crashed." in note
    assert row.bodies[0].strip()[:40] in note
    # The lead claims nothing about the tree either.
    assert "NOT" not in REFUTED_BODY_LEAD
    assert "is the one that crashed" in REFUTED_BODY_LEAD
    # Two quoted occurrences are both told.
    two = replace(row, bodies=(row.bodies[0], "fn other() {}"))
    assert "fn other() {}" in refuted_text_for([two], _ORACLE, [_UNIT])


def test_a_retry_sends_the_note_until_the_conversation_holds_it() -> None:
    """An open conversation does not rebuild the opening prompt. The retry
    carries the note once; a later turn drops it. The body the model already
    wrote is not the note, so a transcript that holds only that body still
    receives the section."""
    note = _note()
    fb = AttemptFeedback(
        kind=None, excerpt="error[E0609]: no field",
        targets=("f",), output="<BODY>\nbody\n</BODY>",
        sections=refuted_feedback(note))
    first = render_retry_turn(fb, conversation="")
    assert f"<{REFUTED_BODY_TAG}>" in first
    assert REFUTED_BODY_LEAD.strip() in first
    assert "heap-use-after-free" in first
    assert "was taken off the tree" in first
    again = render_retry_turn(fb, conversation=first)
    assert f"<{REFUTED_BODY_TAG}>" not in again
    body_only = render_retry_turn(
        fb, conversation="as *mut E as *mut crate::Error")
    assert f"<{REFUTED_BODY_TAG}>" in body_only
    assert refuted_feedback("") == ()
    assert refuted_feedback("   ") == ()
