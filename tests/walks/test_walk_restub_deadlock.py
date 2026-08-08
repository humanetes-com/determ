"""The walk for run `259637-36db9aad`, cluster `stable`, 2026-08-07.

The run resumed a tree at 81 passing of a 108-test oracle. Its crash retrigger
fired in the first iteration and reverted **56 of 178 filled bodies** to
`unimplemented!()` on purpose, and the sweeps that followed read 21, 23, 26, 26.
Three consumers of the 81 were told the drop was deliberate — the batch ratchet
withheld its breach, the stall budget stood down, the stub-progress check
exempted the sweep. The fourth was not: `readmission_warranted` releases the
disk-persisted `fully_exhausted` convictions only on a score move past the
high-water, and the high-water was still the deleted tree's 81. From iteration 4
every sweep logged `109 fill(s) since the last check moved pass no further than
81 — NOT re-admitting exhausted fn(s)`, while the only two units that can reach
the ~60 missing tests sat convicted. Nine hours, 148 landed fills, +5 tests.

What this walk asserts is that the re-admission became REACHABLE, not that the
run would have finished — a recorded observation sequence cannot predict the
score past the first iteration where the fix changes what the loop fills. Each
assertion is paired with the same walk under the pre-fix grading, because a
fix-verification test that does not fail on the old code has proved nothing.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.walks.harness import load_walk, refusals_against, replay

pytestmark = pytest.mark.walk

WALK = "259637-36db9aad"


@pytest.fixture
def old_grading(monkeypatch) -> None:
    """The pre-fix high-water: a plain monotone maximum that no deliberate
    re-stub can lower. This is the negative control's only difference from the
    shipped loop — `rebase_due` is what decides whether the sweep after a
    re-stub restates the lineage's baseline."""
    from pipeline_core.domain.pass_highwater import PassHighWater
    monkeypatch.setattr(PassHighWater, "rebase_due",
                        lambda self, iteration: False)


def test_the_walk_is_the_run_that_deadlocked() -> None:
    """The fixture is evidence, so its shape is pinned: if a future distiller
    change quietly drops the retrigger or the refusals, the walk below would
    still pass while asserting nothing."""
    walk = load_walk(WALK)
    assert walk["entry_best"] == 81
    assert walk["iterations"][0]["retrigger"] == {
        "cause": "RUNTIME_CRASH", "reverted": 56}
    assert [it.get("passed") for it in walk["iterations"]] == [None, 21, 23, 26, 26]
    # The deadlock in one field: two sweeps refused a re-admission for failing
    # to beat a tree the pipeline itself had deleted.
    assert [it["iter"] for it in walk["iterations"]
            if "readmission_refused_against" in it] == [4, 5]


def test_the_restub_rebases_the_high_water(tmp_path: Path, monkeypatch) -> None:
    """The re-base happens once, on the first sweep after the re-stub, and it
    moves the baseline DOWN to what that sweep actually measured."""
    events: list[tuple[str, dict]] = []
    replay(load_walk(WALK), monkeypatch, tmp_path, events=events)
    rebased = [d for ev, d in events if ev == "phase_c.tdd.highwater_rebased"]
    assert len(rebased) == 1, "one re-base, on the first sweep after the re-stub"
    assert rebased[0]["stale_best"] == 81
    assert rebased[0]["restubbed"] == 56
    assert rebased[0]["new_best"] == 21


def test_the_deadlock_does_not_reproduce(tmp_path: Path, monkeypatch) -> None:
    """The decision the fix exists to make reachable. No sweep of the re-climb
    may be judged against the 81, which is the tree the retrigger deleted."""
    events: list[tuple[str, dict]] = []
    logs = replay(load_walk(WALK), monkeypatch, tmp_path, events=events)
    assert not refusals_against(logs, 81), \
        "still judging the re-climb against the tree the retrigger deleted"


def test_the_deadlock_reproduces_under_the_old_grading(
        tmp_path: Path, monkeypatch, old_grading: None) -> None:
    """The negative control, and the reason to believe the two above.

    Under the monotone high-water the recorded refusal comes back verbatim —
    the same `no further than 81` the live run printed on every sweep from
    iteration 4 to its death. A walk whose control passes is measuring nothing.
    """
    events: list[tuple[str, dict]] = []
    logs = replay(load_walk(WALK), monkeypatch, tmp_path, events=events)
    assert not [d for ev, d in events if ev == "phase_c.tdd.highwater_rebased"]
    assert refusals_against(logs, 81), \
        "the old grading no longer reproduces the deadlock — the walk has " \
        "drifted off the run it was distilled from and proves nothing"
