"""The walk for run `58819-ee78a1d1`, cluster `stable`, 2026-08-13.

A from-scratch resume at 67/106 climbed to 79 by iteration 3, where a
deterministic use-after-free SIGABRT in `test_downcast` (ASAN: no in-crate
frame in 27, blaming NOBODY) fired the crash retrigger. `Quoted` — first OWNS
candidate in registry order — landed attempt 1 and reverted 3 bodies; the
re-fills re-climbed to 79 by iteration 5; the identical crash reproduced and
the walk picked `Quoted` AGAIN (nothing remembered the crash had survived its
re-author), reverting the same 3 bodies. Pass oscillated 79/74/79/74 at ~2.1h
a fire-to-fire cycle until the operator killed the run mid-iteration 6.

Inside that cycle sat a second defect, and it is the one THIS walk replays:
the iteration-boundary epoch re-base ran BEFORE the sweep, so at iterations 4
and 6 it adopted `best_pass` while that number was still the pre-destruction
79 (the fire comes after a sweep; nothing had re-based the watermark yet).
The log holds both adoptions verbatim — `re-admission baseline 68 predates a
deliberate destruction — re-based to 79` and the same again from 74 — each
followed by `high-water re-based 79 -> 74`. The release that iteration 5's
genuine 74 -> 79 re-climb had earned was consumed by that bookkeeping:
79 > 79 never fires, and the convicted tail (`IteratorforChain` family,
`error.rs::Error`) stayed locked behind a baseline describing a deleted tree.

The retrigger half of the cycle is bounded elsewhere (signature refutation in
the retrigger action, pinned in `test_retrigger_layout.py` — the action is
scripted here, so a walk cannot exercise it). What this walk asserts is the
DRIVER half: the boundary decision now runs after the sweep and the
high-water's own destruction re-base, so the baseline always describes the
tree on disk and the re-climb's release is reachable. Assertions are on
decisions, never scores.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.walks.harness import load_walk, refusals_against, replay

pytestmark = pytest.mark.walk

WALK = "58819-ee78a1d1"
RELEASED = ["src/a.rs::f"]


@pytest.fixture
def presweep_ordering(monkeypatch) -> None:
    """The pre-fix consumer ordering, injected: the boundary decision reads
    the best_pass of the PREVIOUS iteration — exactly what deciding before
    this iteration's sweep and high-water re-base amounted to. This is the
    negative control's only difference from the shipped loop."""
    import pipeline_core.application.reconstruct.phase_c_tdd as tdd
    real = tdd.readmission_boundary
    stale: dict[str, int | None] = {"best": None}

    def _old(*, best_pass: int, **kw):
        prev = stale["best"]
        stale["best"] = best_pass
        return real(best_pass=prev if prev is not None else best_pass, **kw)

    monkeypatch.setattr(tdd, "readmission_boundary", _old)


def test_the_walk_is_the_run_that_cycled() -> None:
    """The fixture is evidence, so its shape is pinned: the two landed fires
    with their recovered 79 readings, and the two boundary adoptions of the
    pre-destruction 79 — the recorded defect in two fields."""
    walk = load_walk(WALK)
    fires = [(it["iter"], it["retrigger"]["reverted"], it.get("crash_passed"))
             for it in walk["iterations"] if "retrigger" in it]
    assert fires == [(3, 3, 79), (5, 3, 79)]
    rebases = [(it["iter"], it["readmission_rebased"])
               for it in walk["iterations"] if "readmission_rebased" in it]
    assert rebases == [(4, {"from": 68, "to": 79}),
                       (6, {"from": 74, "to": 79})]
    # The release the ordering ate, visible as its refusal one iteration
    # earlier: 16 re-fills judged against a baseline of 74 BEFORE the sweep
    # that measured them at 79.
    assert [it["iter"] for it in walk["iterations"]
            if "readmission_refused_against" in it] == [5]


def test_the_baseline_never_adopts_the_deleted_trees_79(
        tmp_path: Path, monkeypatch) -> None:
    """No boundary may re-base the re-admission baseline to 79 — that number
    describes the tree the retrigger deleted, and after the high-water's own
    re-base (79 -> 74) the epoch re-base finds the values already agreeing."""
    logs = replay(load_walk(WALK), monkeypatch, tmp_path, events=[],
                  reset_returns=RELEASED)
    assert not [ln for ln in logs if "re-based to 79" in ln], \
        "the boundary re-adopted the pre-destruction high-water"
    assert not refusals_against(logs, 79), \
        "a re-admission was refused against the deleted tree's 79"


def test_the_reclimbs_release_is_reachable(tmp_path: Path, monkeypatch) -> None:
    """The decision the fix exists to make reachable: the 74 -> 79 re-climb
    between the two fires buys a release when it is measured, instead of
    being consumed by the boundary's own bookkeeping."""
    logs = replay(load_walk(WALK), monkeypatch, tmp_path, events=[],
                  reset_returns=RELEASED)
    assert any("iter 6: re-admitted" in ln for ln in logs), \
        "the re-climb past the re-based 74 released nothing"


def test_the_eaten_release_reproduces_under_the_presweep_ordering(
        tmp_path: Path, monkeypatch, presweep_ordering: None) -> None:
    """The negative control, and the reason to believe the two above: with
    the decision fed pre-sweep values, the recorded adoptions of 79 come back
    (68 -> 79, then 74 -> 79) and no release ever fires after the first
    destruction — the recorded lock, reproduced from the recording."""
    logs = replay(load_walk(WALK), monkeypatch, tmp_path, events=[],
                  reset_returns=RELEASED)
    assert [ln for ln in logs if "re-based to 79" in ln], \
        "the pre-sweep ordering no longer adopts the deleted tree's 79 — " \
        "the walk has drifted off the run it was distilled from"
    late_releases = [ln for ln in logs if "re-admitted" in ln
                     and any(f"iter {n}:" in ln for n in range(5, 12))]
    assert not late_releases, \
        "a release fired despite the pre-sweep ordering — the control " \
        "proves nothing"
