"""The fill schedule orders a cycle of mutually-blocking runs by its best
member, defers a run that can move no verdict, and bounds one unit's time.

Run `484518-91681c1a` (2026-09-23, cluster `stable`) fill 5: the sweep named
`src/ptr.rs::new` as the first failure of all 141 failing tests and the per-fn
run holding it ranked level 0, never served. The 11 level-0/1 runs blocked each
other (every one holds a stub some other's tests are hollow on), so the old
walk — "the first run whose blockers are all placed" — could place none of
them, and opened the only runs nothing can block: two formatter pairs with no
stub and no aimed test (level 2). They kept four bodies, flat, in 20 minutes.
The same run's iter 4 spent 199 minutes of a 15-minute share on one unit that
never compiled.

Each claim below is the plan's sentence, with the pre-fix behaviour as the
negative control.
"""
from __future__ import annotations

import json
import random
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path

import pytest

from pipeline_core.application.fix_loop import (
    StepStatus,
    drive_fix_loop,
    run_fix_loop,
)
from pipeline_core.domain import fill_schedule as fs
from pipeline_core.domain import unit_clock
from pipeline_core.domain.attribution import FailureFrontier, FormatterPair
from pipeline_core.domain.fill_schedule import (
    TODAY_ORDER,
    RouteKind,
    RouteRun,
    RunClock,
    condensed_order,
    schedule,
)
from pipeline_core.domain.models import FnLocator, FnSite
from pipeline_core.domain.refine import FixProgress

FIXTURE = (Path(__file__).resolve().parent / "fixtures" / "fills"
           / "484518-91681c1a_fill5.json")


def _loc(s: str) -> FnLocator:
    f, fn = s.split("::", 1)
    return FnLocator(f, fn)


def _plain_walk(keys: Sequence[str],
                blockers: Mapping[str, Collection[str]]) -> list[str]:
    """The pre-fix rule, verbatim: the first key in sort order whose every
    blocker is placed; with none placeable, the first pending key."""
    pending = list(keys)
    placed: set[str] = set()
    out: list[str] = []
    while pending:
        pick = next((k for k in pending
                     if not (set(blockers.get(k, ())) - placed)), None)
        if pick is None:
            pick = pending[0]
        out.append(pick)
        placed.add(pick)
        pending.remove(pick)
    return out


def _recorded() -> dict:
    return json.loads(FIXTURE.read_text())


def _recorded_graph(fix: dict) -> tuple[list[str], dict[str, set[str]]]:
    """The live sort order (level, rank, route kind, seq) and the live
    blocks relation, both off the recorded event."""
    order_index = {k.value: i for i, k in enumerate(TODAY_ORDER)}
    runs = fix["runs"]
    labels = {r["label"] for r in runs}
    keys = [r["label"] for r in sorted(
        runs, key=lambda r: (r["level"], r["rank"],
                             order_index[r["kind"]], r["seq"]))]
    blockers = {r["label"]: set(r["blocked_by"]) & labels for r in runs}
    return keys, blockers


def _recorded_runs(fix: dict) -> list[RouteRun]:
    return [RouteRun(
        kind=RouteKind(r["kind"]), label=r["label"],
        targets=frozenset(_loc(t) for t in r["targets"]),
        target_tests=frozenset(r["target_tests"]),
        stubs_in_targets=frozenset(_loc(t) for t in r["stubs"]),
        files=tuple(r["files"]), seq=r["seq"], bodies_sha=r["bodies_sha"])
        for r in fix["runs"]]


# ---- ordering over strongly connected components ----


def test_recorded_484518_fill_5_the_first_run_writes_the_crash_blamed_stub() -> None:
    fix = _recorded()
    blamed = {_loc(t) for t in fix["inputs"]["crash_blamed"]}
    targets = {r["label"]: {_loc(t) for t in r["targets"]} for r in fix["runs"]}
    keys, blockers = _recorded_graph(fix)
    live = [r["label"] for r in fix["runs"]]
    # The replay is the live rule: the pre-fix walk reproduces the recorded
    # order exactly, so the control below is a statement about that fill.
    assert _plain_walk(keys, blockers) == live
    assert not targets[live[0]] & blamed
    new = condensed_order(keys, blockers)
    assert targets[new[0]] & blamed
    assert sorted(new) == sorted(live)


def test_recorded_484518_fill_5_schedule_opens_the_blamed_run_first(
        monkeypatch: pytest.MonkeyPatch) -> None:
    fix = _recorded()
    runs = _recorded_runs(fix)
    blamed = frozenset(_loc(t) for t in fix["inputs"]["crash_blamed"])
    hollow = {t: frozenset(_loc(x) for x in v)
              for t, v in fix["inputs"]["hollow"].items()}
    kw = {"oracle_fp": "o", "hollow": hollow, "crash_blamed": blamed,
          "budget_minutes": fix["budget_minutes"]}
    sched = schedule(runs, [], **kw)
    assert sched.ordered[0].run.targets & blamed
    assert sorted((s.run.label, s.reason) for s in sched.deferred
                  if s.run.kind is RouteKind.PAIR) == [
        ("pair src/error.rs::fmt + src/fmt.rs::debug", "nothing_aimed"),
        ("pair src/error.rs::fmt + src/fmt.rs::display", "nothing_aimed")]
    # Control: the pre-fix walk and no nothing-aimed deferral open a pair.
    monkeypatch.setattr(fs, "condensed_order", _plain_walk)
    monkeypatch.setattr(fs, "_aims_at_nothing", lambda _r, _b: False)
    control = schedule(runs, [], **kw)
    assert control.ordered[0].run.kind is RouteKind.PAIR
    assert not control.ordered[0].run.targets & blamed


def _stub_run(name: str, test: str, seq: int) -> RouteRun:
    loc = FnLocator("src/c.rs", name)
    return RouteRun(kind=RouteKind.PER_FN, label=f"per-fn {name}",
                    targets=frozenset({loc}), target_tests=frozenset({test}),
                    stubs_in_targets=frozenset({loc}), seq=seq)


def test_an_invented_cycle_goes_before_a_run_nothing_blocks(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Three stub runs whose tests are each hollow on the next one's stub
    (a cycle at level 1) and one filled-body run with an aimed test that no
    stub holds (level 2, unblockable)."""
    a, b, c = (_stub_run(n, f"t_{n}", i) for i, n in enumerate("abc"))
    hollow = {"t_a": frozenset({FnLocator("src/c.rs", "b")}),
              "t_b": frozenset({FnLocator("src/c.rs", "c")}),
              "t_c": frozenset({FnLocator("src/c.rs", "a")})}
    loose = RouteRun(kind=RouteKind.FILE, label="file src/d.rs",
                     targets=frozenset({FnLocator("src/d.rs", "d")}),
                     target_tests=frozenset({"t_d"}), seq=9)
    sched = schedule([loose, a, b, c], [], oracle_fp="o", hollow=hollow)
    assert [s.run.label for s in sched.ordered] == [
        "per-fn a", "per-fn b", "per-fn c", "file src/d.rs"]
    monkeypatch.setattr(fs, "condensed_order", _plain_walk)
    control = schedule([loose, a, b, c], [], oracle_fp="o", hollow=hollow)
    assert control.ordered[0].run.label == "file src/d.rs"


@pytest.mark.parametrize("seed", range(20))
def test_on_an_acyclic_relation_the_order_is_the_plain_walk(seed: int) -> None:
    rng = random.Random(seed)
    keys = [f"k{i}" for i in range(12)]
    rank = list(range(12))
    rng.shuffle(rank)  # a topological rank independent of the sort order
    blockers = {k: {o for o in keys if rank[keys.index(o)] < rank[keys.index(k)]
                    and rng.random() < 0.25} for k in keys}
    assert condensed_order(keys, blockers) == _plain_walk(keys, blockers)


# ---- a run that can move no verdict is not a candidate ----


def test_a_pair_that_aims_at_a_test_still_opens() -> None:
    pair = RouteRun(kind=RouteKind.PAIR, label="pair",
                    targets=frozenset({FnLocator("a.rs", "fmt"),
                                       FnLocator("b.rs", "debug")}),
                    target_tests=frozenset({"t_debug"}))
    sched = schedule([pair], [], oracle_fp="o", hollow={})
    assert [s.run.label for s in sched.ordered] == ["pair"]
    empty = RouteRun(kind=RouteKind.PAIR, label="pair",
                     targets=pair.targets)
    sched = schedule([empty], [], oracle_fp="o", hollow={})
    assert not sched.ordered
    assert [s.reason for s in sched.deferred] == ["nothing_aimed"]
    # A crash-blamed target is aimed even with no test named.
    blamed = schedule([empty], [], oracle_fp="o", hollow={},
                      crash_blamed=[FnLocator("a.rs", "fmt")])
    assert [s.run.label for s in blamed.ordered] == ["pair"]


def test_the_frontier_carries_only_pairs_a_failing_test_is_dispatched_against() -> None:
    live = FormatterPair(FnSite("a.rs", "fmt", "impl Display for E"),
                         FnSite("b.rs", "show", "impl Printer"))
    idle = FormatterPair(FnSite("a.rs", "fmt", "impl Debug for E"),
                         FnSite("c.rs", "dump", "impl Printer"))
    dispatch = {FnLocator("b.rs", "show"): {"t_show"},
                FnLocator("c.rs", "dump"): set()}
    ff = FailureFrontier.build(dispatch, {}, pairs=[live, idle])
    assert ff.pairs == frozenset({live})


# ---- the claim gate says why it refused ----


def test_a_refusal_names_its_reason() -> None:
    t = [0.0]
    moved = {"t_moved"}
    clock = RunClock(share_minutes=10.0, fill_remaining=lambda: 100.0,
                     now=lambda: t[0],
                     ground_moved=lambda scope, _units: bool(scope and scope & moved))
    assert clock.refusal(["t_moved"]) == "premise"
    assert clock.refusal(["t_other"]) == ""
    t[0] = 11 * 60.0
    assert clock.refusal(["t_other"]) == "share"
    assert clock.may_claim(["t_other"]) is False


# ---- one unit's wall-clock bound ----


def _failing(n: int) -> StepStatus:
    return StepStatus.CONTINUE


class _Gauge:
    stalls = 0
    repeated = False


def test_a_unit_past_its_bound_ends_at_the_next_attempt_boundary() -> None:
    t = [0.0]
    clock = RunClock(share_minutes=15.0, fill_remaining=lambda: None,
                     now=lambda: t[0], unit_multiple=4.0)
    try:
        assert clock.may_claim()
        assert unit_clock.bound_minutes() == 60.0

        def attempt(n: int) -> StepStatus:
            t[0] += 25 * 60.0  # each attempt takes 25 minutes
            return StepStatus.CONTINUE

        out = run_fix_loop(attempt, _Gauge(), hard_cap=25, max_stalls=99,
                           should_end=unit_clock.expired)
        assert (out.reason, out.attempts) == ("unit_bound", 3)
        # Control: no multiple, no bound — the loop runs to its hard cap.
        t[0] = 0.0
        unbounded = RunClock(share_minutes=15.0, fill_remaining=lambda: None,
                             now=lambda: t[0], unit_multiple=0.0)
        assert unbounded.may_claim()
        out = run_fix_loop(attempt, _Gauge(), hard_cap=25, max_stalls=99,
                           should_end=unit_clock.expired)
        assert out.reason == "hard_cap"
    finally:
        unit_clock.clear()


def test_the_shared_driver_reads_the_bound_and_always_runs_one_attempt() -> None:
    from pipeline_core.application.fix_loop import FixOutcome
    t = [0.0]
    clock = RunClock(share_minutes=1.0, fill_remaining=lambda: None,
                     now=lambda: t[0], unit_multiple=1.0)
    calls: list[int] = []
    try:
        assert clock.may_claim()
        t[0] = 10 * 60.0  # already past the bound when the loop opens

        def attempt(n: int) -> FixOutcome:
            calls.append(n)
            return FixOutcome("failed", err_sig=frozenset({str(n)}),
                              metric=FixProgress(10 - n, 0))

        accepted, n = drive_fix_loop(attempt, hard_cap=10, max_stalls=99,
                                     stop_context="unit")
        assert (accepted, n, calls) == (False, 1, [1])
    finally:
        unit_clock.clear()
    # A thread that never claimed has no bound.
    assert not unit_clock.expired()
