"""Walk: a pass whose units no route opened does not file them as tries.

Run `11358-e5de094e` (2026-09-15, cluster `stable`) closed traversal 1 at
iter 3 with `27 dispatched, 1 landed a semantic change; 26 dispatched with
NO landed change`, while the landed-change lines of iters 1–3 named most of
those units `no route attempted` — every run holding them had been deferred
by the schedule. A flat sweep after that pass reads as refuting 26 units the
fill never wrote.

Replayed on the recorded readings of `2773540-e47c3754` with the whole
frontier in one batch and an engine that reports opening one unit. Asserted:
the pass audit names the two others as dispatched-but-unopened and keeps
them out of the unchanged list. Negative control: the pre-fix rule (no unit
is ever unopened) injected into the same replay files both as unchanged.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline_core.domain.models import ClusterFillResult, FnLocator
from tests.walks.harness import load_walk, replay

pytestmark = pytest.mark.walk

_WALK = "2773540-e47c3754"
_OPENED = FnLocator("src/a.rs", "f")
_DEFERRED = FnLocator("src/b.rs", "g")
_REFUSED = FnLocator("src/c.rs", "h")
_FRONTIER = {_OPENED: {"t2"}, _DEFERRED: {"t3"}, _REFUSED: {"t4"}}


def _replay(monkeypatch, tmp_path: Path) -> list[dict]:
    events: list[tuple[str, dict]] = []

    def _fill(**kw):
        keys = tuple(kw.get("tdd_frontier_keys") or ())
        opened = tuple((k.file, k.fn_name) for k in keys if k == _OPENED)
        return ClusterFillResult(attempts=1, fns_filled=0,
                                 dispatched_fns=opened)

    def _enumerate(_src, **_kw):
        return [{"file": k.file, "fn_name": k.fn_name,
                 "body_text": "filled()"} for k in _FRONTIER]

    replay(load_walk(_WALK), monkeypatch, tmp_path, events=events,
           gpu_guard="2", max_stalls="9", floor={}, dispatch_cap="12",
           frontier=dict(_FRONTIER), enumerate_fns=_enumerate, fill=_fill)
    return [d for e, d in events if e == "phase_c.tdd.pass_audit"]


def test_units_no_route_opened_are_not_filed_as_unchanged_tries(
        monkeypatch, tmp_path: Path) -> None:
    audits = _replay(monkeypatch, tmp_path)
    assert audits, "the one-batch pass must close and be audited"
    first = audits[0]
    assert first["unopened"] == ["src/b.rs::g", "src/c.rs::h"]
    assert first["unchanged"] == ["src/a.rs::f"]


def test_negative_control_the_pre_fix_audit_files_them_as_unchanged(
        monkeypatch, tmp_path: Path) -> None:
    import pipeline_core.application.reconstruct.phase_c_tdd as _mod
    monkeypatch.setattr(_mod, "pass_unopened", lambda *_a, **_k: frozenset())
    audits = _replay(monkeypatch, tmp_path)
    assert audits
    assert audits[0]["unopened"] == []
    assert audits[0]["unchanged"] == ["src/a.rs::f", "src/b.rs::g",
                                      "src/c.rs::h"]
