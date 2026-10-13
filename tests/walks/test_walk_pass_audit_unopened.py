"""Walk: a unit no route opened had no turn — the pass offers it once more,
and the audit never files it as a try.

Run `11358-e5de094e` (2026-09-15, cluster `stable`) closed traversal 1 at
iter 3 with `27 dispatched, 1 landed a semantic change; 26 dispatched with
NO landed change`, while the landed-change lines of iters 1–3 named most of
those units `no route attempted` — every run holding them had been deferred
by the schedule. The cursor had marked them attempted at dispatch, so the
pass closed over them and a flat sweep after it read as refuting 26 units
the fill never wrote.

Replayed on the flat readings of `358833-aa15d93c` with a three-unit
frontier in one batch and an engine that opens only one of them. Asserted:
the fill that opened one unit REOPENS the pass for the other two; the next
batch is exactly those two; when they come back unopened again the pass
closes (once per pass, so it cannot be held open forever) and the audit
names them unopened, not unchanged. Negative controls: the pre-change cursor
(no re-offer) closes at iter 1 and the next batch re-sends all three as a
new pass; the pre-change audit (no unopened set) files both as unchanged.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline_core.domain.models import ClusterFillResult, FnLocator
from tests.walks.harness import load_walk, replay

pytestmark = pytest.mark.walk

_WALK = "358833-aa15d93c"
_OPENED = FnLocator("src/a.rs", "f")
_DEFERRED = FnLocator("src/b.rs", "g")
_REFUSED = FnLocator("src/c.rs", "h")
_FRONTIER = {_OPENED: {"t136"}, _DEFERRED: {"t136"}, _REFUSED: {"t137"}}


def _replay(monkeypatch, tmp_path: Path):
    events: list[tuple[str, dict]] = []
    batches: list[tuple[FnLocator, ...]] = []

    def _fill(**kw):
        keys = tuple(kw.get("tdd_frontier_keys") or ())
        batches.append(keys)
        opened = tuple((k.file, k.fn_name) for k in keys if k == _OPENED)
        return ClusterFillResult(attempts=1, fns_filled=0,
                                 dispatched_fns=opened)

    def _enumerate(_src, **_kw):
        return [{"file": k.file, "fn_name": k.fn_name,
                 "body_text": "filled()"} for k in _FRONTIER]

    replay(load_walk(_WALK), monkeypatch, tmp_path, events=events,
           gpu_guard="4", flat_window="5", max_stalls="9", floor={},
           dispatch_cap="12", frontier=dict(_FRONTIER),
           enumerate_fns=_enumerate, fill=_fill)
    return events, batches


def _names(units) -> set[str]:
    return {f"{k.file}::{k.fn_name}" for k in units}


def test_unopened_units_reopen_the_pass_once_and_are_audited_as_unopened(
        monkeypatch, tmp_path: Path) -> None:
    events, batches = _replay(monkeypatch, tmp_path)
    reoffers = [d for e, d in events if e == "phase_c.tdd.pass_reoffer"]
    assert reoffers and reoffers[0]["reopened"] is True
    assert set(reoffers[0]["units"]) == {"src/b.rs::g", "src/c.rs::h"}
    assert len(batches) >= 2
    assert _names(batches[0]) == _names(_FRONTIER)
    # The cap covers the whole frontier, so the rank offers all three again,
    # in class order (an owed unit leads one that is not); the two no route
    # opened are in it.
    assert {"src/b.rs::g", "src/c.rs::h"} <= _names(batches[1]), (
        "the units no route opened must be offered again at the next batch")
    # Once per pass: the second refusal (iter 2) does not re-offer again,
    # and the pass closes there. A later re-offer belongs to the NEXT pass.
    assert not [d for d in reoffers if d["iter"] == 2]
    audits = [d for e, d in events if e == "phase_c.tdd.pass_audit"]
    assert audits, "the pass must close after the one re-offer"
    assert audits[0]["iter"] == 2
    assert audits[0]["unopened"] == ["src/b.rs::g", "src/c.rs::h"]
    assert audits[0]["unchanged"] == ["src/a.rs::f"]


def test_negative_control_the_pre_change_cursor_closes_over_them(
        monkeypatch, tmp_path: Path) -> None:
    import pipeline_core.application.reconstruct.phase_c_tdd as _mod
    monkeypatch.setattr(_mod, "reoffer_unopened", lambda *_a, **_k: frozenset())
    # The opened unit came back unchanged under the same red-test words; the
    # own-ask exclusion would keep it out of the new pass on its own, which
    # is not the cursor this control isolates.
    monkeypatch.setenv("PHASE_C_ASKED_UNCHANGED", "0")
    events, batches = _replay(monkeypatch, tmp_path)
    assert not [e for e, _ in events if e == "phase_c.tdd.pass_reoffer"]
    audits = [d for e, d in events if e == "phase_c.tdd.pass_audit"]
    assert audits and audits[0]["iter"] == 1
    assert len(batches) >= 2 and _names(batches[1]) == _names(_FRONTIER)


def test_negative_control_the_pre_change_audit_files_them_as_unchanged(
        monkeypatch, tmp_path: Path) -> None:
    import pipeline_core.application.reconstruct.phase_c_tdd as _mod
    monkeypatch.setattr(_mod, "reoffer_unopened", lambda *_a, **_k: frozenset())
    monkeypatch.setattr(_mod, "pass_unopened", lambda *_a, **_k: frozenset())
    events, _ = _replay(monkeypatch, tmp_path)
    audits = [d for e, d in events if e == "phase_c.tdd.pass_audit"]
    assert audits
    assert audits[0]["unopened"] == []
    assert audits[0]["unchanged"] == ["src/a.rs::f", "src/b.rs::g",
                                      "src/c.rs::h"]
