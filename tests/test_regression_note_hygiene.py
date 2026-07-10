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
