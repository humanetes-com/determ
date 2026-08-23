"""Pins for regression-note hygiene (deep-analysis finding 2026-07-10):
notes claiming a drop that didn't happen (22→22, 15→16) poisoned the model's
feedback channel — the writer must not produce them and readers must drop the
legacy ones already on disk."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct.failure_diagnosis import (
    record_tdd_regression,
)
from pipeline_core.domain.model_notes import is_bogus_regression_note


def _recs(fn: str = "source"):
    return [{"fn_name": fn, "file": "src/context.rs", "body_text": "BODY"}]


def _run(prev: int, now: int, notes: list[str], filled=None) -> str:
    return record_tdd_regression(
        Path("fr.md"), Path("src"),
        filled if filled is not None else [("src/context.rs", "source")],
        {"test_as_ref"}, prev, now,
        enumerate_fns=lambda _p: _recs(),
        append_note=lambda _p, n: notes.append(n),
        body_cap=100,
    )


def test_no_note_when_aggregate_did_not_drop() -> None:
    notes: list[str] = []
    assert _run(22, 22, notes) == ""
    assert _run(15, 16, notes) == ""
    assert notes == []


def test_note_on_true_drop_is_honest_and_batch_aware() -> None:
    notes: list[str] = []
    out = _run(22, 19, notes, filled=[("f", "source"), ("f", "fmt")])
    assert out == "regressed test_as_ref"
    assert notes and "dropped 22→19" in notes[0]
    assert "ONE of them" in notes[0]          # shotgun blame is flagged
    single: list[str] = []
    _run(22, 19, single)
    assert "ONE of them" not in single[0]     # single-fn fill: direct blame ok


def test_reader_drops_legacy_bogus_notes() -> None:
    assert is_bogus_regression_note(
        "[regression] In context.rs, ... (cluster pass count dropped 22→22). WRONG")
    assert is_bogus_regression_note(
        "[regression] ... (cluster pass count dropped 15→16). WRONG")
    assert not is_bogus_regression_note(
        "[regression] ... (cluster pass count dropped 22→19). WRONG")
    assert not is_bogus_regression_note("I tried X because Y; correction Z")


def test_stale_note_retired_when_body_superseded() -> None:
    from pipeline_core.domain.model_notes import is_stale_regression_note

    note = ("[regression] In error.rs, after `root_cause` was written, "
            "previously-passing test(s) FAILED: t (cluster pass count dropped "
            "22→19). Body applied:\nloop { match source(current) { } }\n"
            "Re-implement `root_cause` so t keep passing.")
    current_has_it = "fn root_cause() { loop { match source(current) { } } }"
    current_changed = "fn root_cause() { self.chain().last().unwrap() }"
    assert not is_stale_regression_note(note, current_has_it)   # still live
    assert is_stale_regression_note(note, current_changed)      # superseded
    assert not is_stale_regression_note(note, "")               # no evidence
    assert not is_stale_regression_note("I tried X", current_changed)


def test_bodyless_regression_note_is_retired() -> None:
    """A `[regression]` note with no body is an accusation nothing can check.

    It is also the note the reader could never retire — the retirement test
    reads the body — so it rode into every prompt naming its file forever. Run
    `263257-9e46def4` iter 3 (2026-08-22) dispatched
    `src/error.rs::construct_from_adhoc` under the ESCALATED grade, i.e. told
    to rewrite it, while this exact shape sat in the same prompt saying a
    rewrite of it had cost 83→25. This pin fails on the pre-fix predicate,
    which returned False for every one of them."""
    from pipeline_core.domain.model_notes import is_stale_regression_note

    bodyless = ("[regression] In error.rs, after `construct_from_adhoc` was "
                "written, previously-passing test(s) FAILED: t1, t2 (cluster "
                "pass count dropped 83→25) (12 bodies were filled this pass — "
                "the culprit is ONE of them). Body applied:")
    live_tree = "pub(crate) fn construct_from_adhoc<M>() { Self::x() }"
    assert is_stale_regression_note(bodyless, live_tree)
    assert is_stale_regression_note(bodyless, "")
    # …and one whose body survived only as the trailing instruction line.
    assert is_stale_regression_note(
        bodyless + "\nRe-implement `construct_from_adhoc` so t1 keep passing.",
        live_tree)
    # A non-regression note is untouched by any of this.
    assert not is_stale_regression_note("I tried X because Y; use Z", "")


def test_escalated_grade_supersedes_a_regression_note_on_the_same_fn() -> None:
    """Two instructions in one prompt is not a steer.

    The ESCALATED grade is this sweep's measurement that the unit must be
    rewritten; the note is an older, batch-spread claim that rewriting it cost
    tests. 199's slice A suspended the retrospectives channel for exactly this
    collision and left this one live."""
    from pipeline_core.domain.model_notes import (
        escalation_superseded_notes,
        regression_note_subject,
    )

    notes = [
        "[regression] In error.rs, after `construct_from_adhoc` was written, "
        "previously-passing test(s) FAILED: t (cluster pass count dropped "
        "83→25). Body applied:\nSelf::construct_from_std(m)",
        "[regression] In error.rs, after `object_ref` was written, "
        "previously-passing test(s) FAILED: t (dropped 83→25). Body applied:\nx",
        "I tried X because Y; use Z",
    ]
    assert regression_note_subject(notes[0]) == "construct_from_adhoc"
    assert regression_note_subject(notes[2]) == ""
    out = escalation_superseded_notes(notes, frozenset({"construct_from_adhoc"}))
    assert out == [notes[0]]
    assert escalation_superseded_notes(notes, frozenset()) == []
