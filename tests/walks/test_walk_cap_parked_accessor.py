"""Run `263474-b0e608d3` iter 2 (2026-08-25): under `PHASE_C_TDD_MAX_DISPATCH=12`
a 28-unit frontier dispatches 12 a sweep, and `test_altdebug`'s only unit
(`src/fmt.rs::debug`, one attributed test, so ranked last) sat parked in batch
3. Its unit-set was identical at iters 1 and 2 because nothing happened to it
— and the futility escalation, whose trigger was set identity alone, declared
it futile and escalated past it to the producers of `ErrorImpl`. The accessor
then meets its own batch carrying a "measured futile" grade against the body
the verdict pins.

What a walk may assert: reachability, never the score. Replayed over the
`2773540` flat readings with a cap of 12 and a 13-unit frontier: the fixed
rule DEFERS the parked test at iter 2 (`escalation_deferred`, naming the
unserved unit), dispatches the unit at iter 2's batch, and fires at iter 3 —
after service. The negative control injects the old rule (identity only) and
reproduces the iter-2 fire before any dispatch.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline_core.domain.models import FnLocator
from tests.walks.harness import load_walk, replay

pytestmark = pytest.mark.walk

_WALK = "2773540-e47c3754"
_PARKED = FnLocator("src/fmt.rs", "debug")
_PROD = FnLocator("src/error.rs", "construct")


def _frontier() -> dict[FnLocator, set[str]]:
    # Twelve units carrying three failing tests each rank ahead of the one
    # unit carrying a single test — the recorded shape (27 escalated units
    # on 3 tests, `debug` on `test_altdebug` alone).
    fr = {FnLocator("src/error.rs", f"u{i}"): {"t1", "t2", "t3"}
          for i in range(12)}
    fr[_PARKED] = {"t4"}
    return fr


def _process(monkeypatch, tmp_path: Path) -> tuple[
        list[str], list[tuple[FnLocator, ...]], list[tuple[str, dict]]]:
    from pipeline_core.domain.models import ClusterFillResult
    events: list[tuple[str, dict]] = []
    batches: list[tuple[FnLocator, ...]] = []

    def _fill(**kw):
        batches.append(tuple(kw.get("tdd_frontier_keys") or ()))
        return ClusterFillResult(attempts=1, fns_filled=2)

    logs = replay(load_walk(_WALK), monkeypatch, tmp_path, events=events,
                  max_stalls="2", dispatch_cap="12", frontier=_frontier(),
                  escalate=lambda src, t, units, producers_out=None: (
                      {_PROD: {t}}),
                  fill=_fill)
    return logs, batches, events


def _iter_of(events: list[tuple[str, dict]], name: str, test: str) -> int | None:
    """Sweep number of the FIRST `name` event for `test`: escalation events
    precede their sweep's `phase_c.tdd.iter` event."""
    iters = 0
    for ev, data in events:
        if ev == "phase_c.tdd.iter":
            iters += 1
        elif ev == name and data.get("test") == test:
            return iters + 1
    return None


def test_a_parked_accessor_is_not_declared_futile_until_it_was_served(
        monkeypatch, tmp_path: Path) -> None:
    logs, batches, events = _process(monkeypatch, tmp_path)
    # iter 1 dispatched the twelve; the parked unit waited.
    assert batches and _PARKED not in batches[0] and len(batches[0]) == 12
    # iter 2: unchanged set, NOT served -> deferred, naming the unit.
    assert _iter_of(events, "phase_c.attribution.escalation_deferred", "t4") == 2
    deferred = [d for ev, d in events
                if ev == "phase_c.attribution.escalation_deferred"
                and d["test"] == "t4"]
    assert deferred[0]["unserved"] == ["src/fmt.rs::debug"]
    assert any("never dispatched since it was observed" in ln for ln in logs)
    # iter 2's batch carried it; the fire arrives at iter 3, after service.
    assert _PARKED in batches[1]
    assert _iter_of(events, "phase_c.attribution.escalated", "t4") == 3
    # The three-test units were served in batch 1 and fire at iter 2 exactly
    # as before — the rule is unchanged where service is real.
    assert _iter_of(events, "phase_c.attribution.escalated", "t1") == 2


def test_negative_control_identity_alone_fires_before_any_dispatch(
        monkeypatch, tmp_path: Path) -> None:
    """The old rule, injected: the recorded `263474` fire — the parked unit
    is declared futile at iter 2 with no dispatch behind it."""
    from pipeline_core.application.reconstruct import phase_c_tdd
    from pipeline_core.domain.escalation_lifetime import FireDecision
    monkeypatch.setattr(
        phase_c_tdd, "escalation_fire_due",
        lambda units, prev, served_at, since: FireDecision(
            due=(prev is not None and prev == units), unserved=()))
    logs, batches, events = _process(monkeypatch, tmp_path)
    assert _PARKED not in batches[0]
    assert _iter_of(events, "phase_c.attribution.escalated", "t4") == 2
    assert not any("never dispatched since it was observed" in ln for ln in logs)
