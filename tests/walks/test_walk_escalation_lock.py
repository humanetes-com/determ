"""Walk `2773540-e47c3754` (2026-08-17, 105m20s, exit 0) — the escalation
lock, and the futility evidence that unlocks it.

The recorded run resumed on the restored 83/106 tree, read 83/106 at both of
its sweeps, and the flat-score guard stopped it at the second — which is also
the FIRST sweep the futility escalation could fire in, because the
escalation's trigger ("this test's attributed unit-set is unchanged across
two judged sweeps") lives in a process-local dict. The escalated additions —
on the live tree `construct`, `erase`, `chain::new`, and the traversal steps
including `context.rs::source`, whose body is the severed `None` diagnosed on
2026-08-14 — were computed and never dispatched, and died with the process.
Every relaunch reproduced the shape: the guard's one-batch-per-relaunch
design walks the consumer frontier only.

What a walk may assert: reachability, never the score. The replay asserts the
DECISION became reachable — a resumed process escalates at its first sweep
and its one batch carries the escalated unit — and the negative control
replays the pre-fix floor shape to reproduce the recorded lock.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline_core.domain.models import FnLocator
from tests.walks.harness import load_walk, replay

pytestmark = pytest.mark.walk

_WALK = "2773540-e47c3754"
# The harness frontier unit (the consumer) and the escalation's addition.
_ACC = FnLocator("src/a.rs", "f")
_PROD = FnLocator("src/b.rs", "producer")


def _process(monkeypatch, tmp_path: Path, floor: dict,
             events: list[tuple[str, dict]] | None = None) -> tuple[
        list[str], list[tuple[FnLocator, ...]], list[str]]:
    """One process over the recorded readings: guard ON at the live repeat
    rule, the escalation returning one producer, batches captured."""
    from pipeline_core.domain.models import ClusterFillResult
    events = [] if events is None else events
    batches: list[tuple[FnLocator, ...]] = []
    escalations: list[str] = []

    def _fill(**kw):
        batches.append(tuple(kw.get("tdd_frontier_keys") or ()))
        return ClusterFillResult(attempts=1, fns_filled=2)

    logs = replay(load_walk(_WALK), monkeypatch, tmp_path, events=events,
                  gpu_guard="2", max_stalls="9", floor=floor,
                  escalate=lambda src, t, units, producers_out=None: (
                      escalations.append(t) or {_PROD: {t}}),
                  fill=_fill)
    return logs, batches, escalations


def test_the_recorded_lock_and_the_evidence_the_stop_leaves_behind(
        monkeypatch, tmp_path: Path) -> None:
    """Process 1 = the recorded run: the guard stops the second 83-reading
    BEFORE the escalated batch dispatches (the lock, on the recorded
    readings) — and the floor it leaves now carries the futility evidence."""
    floor: dict = {}
    logs, batches, escalations = _process(monkeypatch, tmp_path, floor)
    assert any("GPU_GUARD_STOP" in ln for ln in logs)
    assert escalations, "the escalation fired at the stopped sweep"
    assert all(_PROD not in b for b in batches), (
        "the escalated unit was computed and never dispatched — the lock")
    assert (floor.get("row") or {}).get("futile_units"), (
        "the stop must leave the evidence on disk")


def test_a_relaunch_dispatches_the_escalated_slice_in_its_one_batch(
        monkeypatch, tmp_path: Path) -> None:
    """THE FIX, on the recorded readings: the resumed process seeds the
    evidence, escalates at its FIRST sweep, and the one batch the guard's
    design grants it carries the escalated unit — the guard's own relaunch
    arithmetic ("a slice the previous process never reached") becomes true
    for escalation targets."""
    floor: dict = {}
    _process(monkeypatch, tmp_path, floor)
    logs, batches, escalations = _process(monkeypatch, tmp_path, floor)
    assert escalations, "the resumed process escalated"
    assert batches and _PROD in batches[0], (
        f"the resumed process's first batch must carry the escalated unit, "
        f"got {batches[0]}")


def test_the_product_survives_to_the_sweep_the_guard_stops(
        monkeypatch, tmp_path: Path) -> None:
    """The product's lifetime is the evidence's lifetime. The resumed process
    fires at sweep 1 and the guard stops sweep 2 — and at THAT sweep the
    escalated unit must still be in the frontier the stop line counts, so the
    pass denominator never shrinks and `take_batch` cannot close the pass
    over units that only 'left' because a fire-once guard withheld them
    (run `2953362-63d4b47e`: 43 at iter 1, 16 at iter 2, one dispatch from a
    wiped cursor and a false traversal credit)."""
    floor: dict = {}
    _process(monkeypatch, tmp_path, floor)
    events: list[tuple[str, dict]] = []
    logs, _, _ = _process(monkeypatch, tmp_path, floor, events)
    stop = [ln for ln in logs if "GPU_GUARD_STOP" in ln]
    assert stop and " of 2 " in stop[0], (
        f"the stopped sweep's frontier must still carry the escalated unit "
        f"(2 units, not the accessor alone): {stop}")
    sustained = [d for e, d in events
                 if e == "phase_c.attribution.escalation_sustained"]
    assert sustained and "src/b.rs::producer" in sustained[0]["added"], (
        "the sweep after the fire re-merges the cached product")


def test_negative_control_blinding_the_sustain_restores_the_shrink(
        monkeypatch, tmp_path: Path) -> None:
    """The same replay with the sustain blinded (fire-once semantics) re-runs
    the recorded shape: the stopped sweep's frontier is the accessor alone —
    the denominator shrink that had the cursor one dispatch from a wipe."""
    import pipeline_core.application.reconstruct.phase_c_tdd as _mod
    from pipeline_core.domain.escalation_lifetime import SustainResult
    floor: dict = {}
    _process(monkeypatch, tmp_path, floor)
    monkeypatch.setattr(
        _mod, "sustain_escalation_products",
        lambda products, units_by_test, stub_keys: SustainResult(
            merges={}, sustained=(), dropped=()))
    events: list[tuple[str, dict]] = []
    logs, _, _ = _process(monkeypatch, tmp_path, floor, events)
    stop = [ln for ln in logs if "GPU_GUARD_STOP" in ln]
    assert stop and " of 1 " in stop[0], (
        f"fire-once shape: the stopped sweep sees the accessor alone: {stop}")
    assert not any(e == "phase_c.attribution.escalation_sustained"
                   for e, _ in events)


def test_negative_control_the_pre_fix_floor_reproduces_the_lock(
        monkeypatch, tmp_path: Path) -> None:
    """The same relaunch over a floor a pre-fix build wrote (no
    `futile_units`) re-runs the recorded shape: escalation waits for its own
    second sweep, the guard stops it before dispatch, the escalated unit
    reaches no batch."""
    floor: dict = {}
    _process(monkeypatch, tmp_path, floor)
    row = dict(floor["row"])
    row.pop("futile_units", None)
    floor["row"] = row
    logs, batches, _ = _process(monkeypatch, tmp_path, floor)
    assert any("GPU_GUARD_STOP" in ln for ln in logs)
    assert all(_PROD not in b for b in batches), (
        "pre-fix floor shape: the lock must reproduce")
