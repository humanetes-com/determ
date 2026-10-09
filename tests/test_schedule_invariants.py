"""The fill schedule's invariants, over generated inputs and over the record.

A schedule correction tested only on the fill that exposed it cannot see the
sibling hold it leaves open. Run `82925-203ed7b8` (2026-09-28, cluster
`stable`) fills 5 and 6 are the instance: the per-fn run holding the stub
47-48 of 49 failing tests first stopped at waited `coverage_split` on the
file and impl runs of the same stub, which the same function then deferred as
re-buys; nothing wrote the stub for 2 h 10 min. The pair_wait instance of the
same shape had been closed on 2026-09-18.

Here the properties of `domain/schedule_invariants.py` are asserted on
`schedule()` for every seed of a generator of small candidate sets — kinds,
targets over three files, stubs, hollow tests, crash blame, attribution,
ledger histories that make runs re-buys, pairs with and without grace,
budgets, kind caps — so a hold that can name a run that does not open, or a
fill that leaves a blocker with no opened writer, fails here on shapes no run
has produced. The generator names no crate symbol and no live test.

The recorded half: `tests/fixtures/fills/82925-203ed7b8_iter5_decided.json`
is that fill's schedule event verbatim (candidates, recorded decisions,
inputs) plus its route ledger rows. The recorded decision — produced by the
code of that day — is the negative control and must break both invariants.
Today's admission, fed the same ranked candidates, must break neither, open
the per-fn run first, and defer the two re-buys of that stub with nobody
held.

The generated seeds assert today's schedule breaks neither invariant. The
pre-change admission (holds, then a weight-zero deferral) is the negative
control twice: on a constructed case of that same shape (a flat re-buy
opens, holds the run that also writes the stub, and is then deferred), and
over the same generator, where it breaks the invariants on 841 of the 3000
seeds (968 unwritten blockers, 582 holds on runs that did not open). Those
counts are the same under every PYTHONHASHSEED only because the generator
samples from sorted sequences; sampling from a frozenset made each process
draw different cases (measured 2026-09-28: 977/583 in one process, 1086-1097
unwritten blockers in others).
"""
from __future__ import annotations

import json
import random
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from pipeline_core.domain import fill_schedule as fs
from pipeline_core.domain.fill_schedule import (
    RouteKind,
    RouteLedgerRow,
    RouteRun,
    RunOutcome,
    admit_ranked,
    rebuy_identity,
    schedule,
)
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.schedule_invariants import (
    all_blocking,
    blamed_from_event,
    decided_from_event,
    decided_from_schedule,
    hollow_stubs_from_event,
    violations,
)

FILES = ("src/a.rs", "src/b.rs", "src/c.rs")
FNS = tuple(FnLocator(f, f"f{i}") for f in FILES for i in range(4))
TESTS = tuple(f"t{i}" for i in range(8))
ORACLE = "o"


def _pick(rng: random.Random, pool: tuple[Any, ...], lo: int, hi: int
          ) -> frozenset[Any]:
    return frozenset(rng.sample(pool, rng.randint(lo, min(hi, len(pool)))))


def _run(rng: random.Random, seq: int, stubs: frozenset[FnLocator],
         hollow: dict[str, frozenset[FnLocator]]) -> RouteRun:
    kind = rng.choice(list(RouteKind))
    if kind in (RouteKind.FILE, RouteKind.IMPL):
        f = rng.choice(FILES)
        targets = _pick(rng, tuple(u for u in FNS if u.file == f), 1, 4)
    elif kind is RouteKind.PAIR:
        a, b = rng.sample(FILES, 2)
        targets = frozenset({
            rng.choice([u for u in FNS if u.file == a]),
            rng.choice([u for u in FNS if u.file == b])})
    else:
        targets = _pick(rng, FNS, 1, 5)
    # Aimed tests: mostly the tests hollow on (or otherwise tied to) the
    # targets, sometimes others, sometimes none.
    tied = [t for t, locs in hollow.items() if locs & targets]
    tests = frozenset(tied) | _pick(rng, TESTS, 0, 2)
    if rng.random() < 0.15:
        tests = frozenset()
    unit_tests: tuple[tuple[FnLocator, frozenset[str]], ...] = ()
    if kind is RouteKind.PER_FN:
        unit_tests = tuple(
            (u, frozenset(t for t in tests if u in hollow.get(t, frozenset()))
             or tests)
            for u in sorted(targets, key=lambda x: (x.file, x.fn_name)))
    return RouteRun(
        kind=kind, label=f"{kind.value} #{seq}", targets=targets,
        target_tests=tests, stubs_in_targets=targets & stubs,
        files=tuple(sorted({u.file for u in targets})), seq=seq,
        bodies_sha="b", unit_tests=unit_tests)


def _rows(rng: random.Random, runs: list[RouteRun]) -> list[RouteLedgerRow]:
    """Histories on the runs' own identities: some reverted, some flat, some
    paid, some deferred — enough to rank re-buys, spend a pair's grace and
    trip the bounded re-admission."""
    rows: list[RouteLedgerRow] = []
    it = 0
    for run in runs:
        for _ in range(rng.choice((0, 0, 1, 2, 3, 4))):
            it += 1
            outcome = rng.choice((RunOutcome.REVERTED, RunOutcome.FLAT,
                                  RunOutcome.FLAT, RunOutcome.CASHED,
                                  RunOutcome.DEFERRED))
            tgt = tuple(sorted((u.file, u.fn_name) for u in run.targets))
            rows.append(RouteLedgerRow(
                kind=run.kind, label=run.label, targets=tgt,
                bodies_sha=run.bodies_sha, oracle_fp=ORACLE, outcome=outcome,
                run_id="r", iteration=it,
                net_pass_delta=1 if outcome is RunOutcome.CASHED else 0,
                target_tests=tuple(sorted(run.target_tests)),
                kept=tgt if outcome is RunOutcome.FLAT else (),
                reverted=tgt if outcome is RunOutcome.REVERTED else (),
                newly_green=(tuple(sorted(run.target_tests))[:1]
                             if outcome is RunOutcome.CASHED else ()),
                opened=tgt))
    rng.shuffle(rows)
    return rows


def _case(seed: int) -> dict[str, Any]:
    rng = random.Random(seed)
    stubs = _pick(rng, FNS, 0, 4)
    hollow: dict[str, frozenset[FnLocator]] = {}
    for t in TESTS:
        if stubs and rng.random() < 0.5:
            # Sorted: a frozenset's order follows the per-process string
            # hash, so sampling from it gave each process different cases.
            hollow[t] = _pick(rng, tuple(sorted(
                stubs, key=lambda u: (u.file, u.fn_name))), 1, 2)
    runs = [_run(rng, i, stubs, hollow) for i in range(rng.randint(2, 9))]
    blamed = _pick(rng, FNS, 0, 2) if rng.random() < 0.4 else frozenset()
    attributed = _pick(rng, FNS, 0, 6) if rng.random() < 0.6 else frozenset()
    return {
        "runs": runs, "rows": _rows(rng, runs), "hollow": hollow,
        "blamed": blamed, "attributed": attributed,
        "budget": rng.choice((0.0, 200.0, 200.0)),
        "max_per_kind": ({RouteKind.SESSION: 1} if rng.random() < 0.3
                         else None),
        "unpaid_grace": rng.choice((0, 3, 3)),
    }


def _schedule(case: dict[str, Any]) -> Any:
    return schedule(
        case["runs"], case["rows"], oracle_fp=ORACLE, hollow=case["hollow"],
        budget_minutes=case["budget"], crash_blamed=case["blamed"],
        attributed=case["attributed"], max_per_kind=case["max_per_kind"],
        unpaid_grace=case["unpaid_grace"], bodies_sha=lambda _t: "b")


SEEDS = range(3000)


def _prechange_schedule(case: dict[str, Any]) -> Any:
    """Admission as it was before a run's own deferral was decided inside
    the hold pass: a re-buy could join the opened set, hold someone, and
    then be deferred by the weight-zero step. No cover."""
    rows_list = list(case["rows"])
    oracle_fp = ORACLE
    hollow = case["hollow"]
    budget_minutes = case["budget"]
    unpaid_grace = case["unpaid_grace"]
    max_per_kind = case["max_per_kind"]
    readmit_after = 2
    blamed = frozenset(case["blamed"])
    attr = frozenset(case["attributed"])
    history = fs._latest_by_identity(rows_list)
    units = fs.UnitService(rows_list, oracle_fp)
    spent = frozenset(
        fs._served_key(r) for r in case["runs"]
        if unpaid_grace > 0
        and fs.consecutive_unpaid(rows_list, r, oracle_fp) >= unpaid_grace)
    carved: list[RouteRun] = []
    for planned in case["runs"]:
        for piece in fs.carve_rebuys(planned, units, readmit_after=readmit_after):
            if piece.parent:
                piece = replace(piece, bodies_sha="b")
            carved.append(piece)
    order_index = {k: i for i, k in enumerate(fs.TODAY_ORDER)}
    split_of: dict[str, bool] = {}
    ranked: list[tuple[RouteRun, int, str, int]] = []
    for run in carved:
        rank, reason, _deferrals = fs._rank_of(
            run, oracle_fp, history, units, readmit_after=readmit_after,
            grace_left=fs._served_key(run) not in spent)
        level = fs._level(run, hollow, blamed)
        split_of[run.key] = fs._is_coverage_split(run, attr)
        ranked.append((run, rank, reason, level))

    def _sort_key(item: tuple[RouteRun, int, str, int]
                  ) -> tuple[int, int, int, int, int]:
        run, rank, _reason, level = item
        return (level, int(split_of.get(run.key, False)), rank,
                order_index.get(run.kind, len(order_index)), run.seq)

    ranked.sort(key=_sort_key)
    per_test: dict[tuple[str, str], int] = {}

    def _test_spent(kind: str, test: str) -> bool:
        if unpaid_grace <= 0:
            return False
        key = (kind, test)
        if key not in per_test:
            per_test[key] = (
                fs.unpaid_tries_for_test(
                    rows_list, test, oracle_fp,
                    match=lambda r: r.kind is RouteKind.PAIR)
                if kind == "pair" else
                fs.unpaid_attributed_writes(rows_list, test, attr, oracle_fp))
        return per_test[key] >= unpaid_grace

    def _grace_left(p: RouteRun, run: RouteRun) -> bool:
        return (fs._served_key(p) not in spent
                and not ((shared := p.target_tests & run.target_tests)
                         and all(_test_spent("pair", t) for t in shared)))

    def _orders(a: RouteRun, b: RouteRun) -> bool:
        if not fs.run_blocks(a, b, hollow, blamed):
            return False
        return a.kind is not RouteKind.PAIR or _grace_left(a, b)

    blockers_of = {
        it[0].key: tuple(
            other[0].label for other in ranked
            if other[0] is not it[0] and _orders(other[0], it[0]))
        for it in ranked}
    blocker_keys = {
        it[0].key: {
            other[0].key for other in ranked
            if other[0] is not it[0] and _orders(other[0], it[0])}
        for it in ranked}
    by_key = {it[0].key: it for it in ranked}
    ordered_items = [
        (*by_key[k], blockers_of[k])
        for k in fs.condensed_order([it[0].key for it in ranked], blocker_keys)]
    scheduled: list[fs.ScheduledRun] = []
    for pos, (run, rank, reason, level, by) in enumerate(ordered_items):
        weight = 1 if reason == "rebuy_readmitted" else fs._WEIGHT[rank]
        scheduled.append(fs.ScheduledRun(
            run=run, rank=rank, weight=weight, reason=reason, level=level,
            blocked_by=by, position=pos))
    waiting: list[fs.ScheduledRun] = []
    non_split_runs = [s.run for s in scheduled if not split_of.get(s.run.key, False)]
    opened_runs: list[RouteRun] = []
    opened_per_kind: dict[RouteKind, int] = {}
    kept: list[fs.ScheduledRun] = []
    for s in scheduled:
        run = s.run
        if fs._aims_at_nothing(run, blamed):
            waiting.append(replace(s, deferred=True, share_minutes=0.0,
                                   reason="nothing_aimed"))
            continue
        blocking_pairs = [p for p in opened_runs
                          if p.kind is RouteKind.PAIR
                          and fs.run_blocks(p, run, hollow, blamed)]
        mutual = [p for p in blocking_pairs if fs.run_blocks(run, p, hollow, blamed)]
        with_grace = [p for p in blocking_pairs if _grace_left(p, run)]
        holders = [p for p in with_grace if p not in mutual]
        if holders:
            waiting.append(replace(
                s, deferred=True, share_minutes=0.0, reason="pair_wait",
                held_by=tuple(p.key for p in holders), prior_reason=s.reason))
            continue
        if with_grace:
            s = replace(s, mutual=tuple(p.label for p in with_grace))
        elif blocking_pairs:
            s = replace(s, lapsed=("pair_wait",))
        if attr and split_of.get(run.key, False):
            shared = frozenset().union(*(
                run.target_tests & o.target_tests for o in non_split_runs))
            if shared:
                split_holders = [
                    o for o in non_split_runs
                    if o in opened_runs and (run.target_tests & o.target_tests)]
                if split_holders and not all(
                        _test_spent("attributed", t) for t in shared):
                    waiting.append(replace(
                        s, deferred=True, share_minutes=0.0,
                        reason="coverage_split",
                        held_by=tuple(o.key for o in split_holders),
                        prior_reason=s.reason))
                    continue
                s = replace(s, lapsed=tuple(dict.fromkeys(
                    (*s.lapsed, "coverage_split"))))
        cap = int((max_per_kind or {}).get(run.kind, 0) or 0)
        if cap > 0 and opened_per_kind.get(run.kind, 0) >= cap:
            waiting.append(replace(s, deferred=True, share_minutes=0.0,
                                   reason="kind_cap"))
            continue
        opened_per_kind[run.kind] = opened_per_kind.get(run.kind, 0) + 1
        opened_runs.append(run)
        kept.append(s)
    scheduled = kept
    if budget_minutes <= 0:
        return fs.FillSchedule(tuple(scheduled), tuple(waiting), 0.0)
    runnable = [s for s in scheduled if s.weight > 0]
    if not runnable:
        scheduled = [replace(s, weight=1, reason="rebuy_readmitted")
                     for s in scheduled]
    ordered: list[fs.ScheduledRun] = []
    deferred: list[fs.ScheduledRun] = list(waiting)
    for s in scheduled:
        if s.weight <= 0:
            deferred.append(replace(s, deferred=True, share_minutes=0.0))
            continue
        ordered.append(s)
    ordered = fs._allot(ordered, float(budget_minutes))
    return fs.FillSchedule(tuple(ordered), tuple(deferred), float(budget_minutes))


def test_every_generated_schedule_satisfies_the_invariants() -> None:
    broken: list[str] = []
    for seed in SEEDS:
        case = _case(seed)
        sched = _schedule(case)
        found = violations(
            decided_from_schedule(sched),
            hollow_stubs=all_blocking(case["hollow"]),
            crash_blamed=case["blamed"], budget_minutes=case["budget"])
        broken.extend(f"seed {seed}: {v.render()}" for v in found)
    assert not broken, f"{len(broken)} violation(s):\n" + "\n".join(broken[:20])


def test_the_generator_reaches_the_pre_change_defect() -> None:
    """The property is only a test if the same generator breaks it under the
    code it was written against: the pre-change admission must violate the
    invariants on some seeds, both kinds."""
    kinds: set[str] = set()
    for seed in range(0, 3000, 5):
        case = _case(seed)
        kinds.update(v.kind for v in violations(
            decided_from_schedule(_prechange_schedule(case)),
            hollow_stubs=all_blocking(case["hollow"]),
            crash_blamed=case["blamed"], budget_minutes=case["budget"]))
    assert {"holder_not_opened", "blocker_unwritten"} <= kinds


def _rebuy_then_hold_case() -> dict[str, Any]:
    """A flat re-buy of the stub, and a coverage-split that writes it too.
    The pre-change admission lets the re-buy hold the split and then defers
    the re-buy. Names no live unit: two files, one stub, one test."""
    stub = FnLocator("src/a.rs", "f0")
    extra = FnLocator("src/b.rs", "f1")
    other = FnLocator("src/c.rs", "f2")
    test = "t0"
    file_run = RouteRun(
        kind=RouteKind.FILE, label="file src/a.rs",
        targets=frozenset({stub}), target_tests=frozenset({test}),
        stubs_in_targets=frozenset({stub}), files=("src/a.rs",),
        seq=0, bodies_sha="b")
    per_fn = RouteRun(
        kind=RouteKind.PER_FN, label="per-fn",
        targets=frozenset({stub, extra}), target_tests=frozenset({test}),
        stubs_in_targets=frozenset({stub}),
        files=("src/a.rs", "src/b.rs"), seq=1, bodies_sha="p")
    # A weight-positive run so the "every opened run is a re-buy" readmission
    # does not put the holder back. It writes neither the stub nor the test.
    bystander = RouteRun(
        kind=RouteKind.FILE, label="file src/c.rs",
        targets=frozenset({other}), target_tests=frozenset({"t1"}),
        files=("src/c.rs",), seq=2, bodies_sha="c")
    tgt = tuple(sorted((u.file, u.fn_name) for u in file_run.targets))
    row = RouteLedgerRow(
        kind=file_run.kind, label=file_run.label, targets=tgt,
        bodies_sha=file_run.bodies_sha, oracle_fp=ORACLE,
        outcome=RunOutcome.FLAT, net_pass_delta=0,
        target_tests=(test,), kept=tgt, opened=tgt)
    return {
        "runs": [file_run, per_fn, bystander], "rows": [row],
        "hollow": {test: frozenset({stub})}, "blamed": frozenset(),
        "attributed": frozenset({stub}), "budget": 200.0,
        "max_per_kind": None, "unpaid_grace": 3,
    }


def test_a_rebuy_that_holds_and_is_then_deferred_breaks_both() -> None:
    """The pre-change admission on the shape of the recorded fill: the re-buy
    holds the other writer, then the weight-zero step defers the re-buy, and
    the stub has no opened writer. Today's schedule defers the re-buy first
    and opens the other writer."""
    case = _rebuy_then_hold_case()
    old = _prechange_schedule(case)
    found = violations(
        decided_from_schedule(old),
        hollow_stubs=all_blocking(case["hollow"]),
        crash_blamed=case["blamed"], budget_minutes=case["budget"])
    kinds = {v.kind for v in found}
    assert kinds == {"holder_not_opened", "blocker_unwritten"}
    deferred = {s.run.label: s for s in old.deferred}
    assert deferred["file src/a.rs"].reason == "rebuy_flat"
    assert deferred["per-fn"].reason == "coverage_split"
    assert deferred["per-fn"].held_by == (deferred["file src/a.rs"].run.key,)
    assert [s.run.label for s in old.ordered] == ["file src/c.rs"]

    now = _schedule(case)
    assert not violations(
        decided_from_schedule(now),
        hollow_stubs=all_blocking(case["hollow"]),
        crash_blamed=case["blamed"], budget_minutes=case["budget"])
    assert [s.run.label for s in now.ordered][0] == "per-fn"
    assert ("file src/a.rs", "rebuy_flat", ()) in [
        (s.run.label, s.reason, s.held_by) for s in now.deferred]


def test_the_schedule_is_deterministic() -> None:
    for seed in range(0, 3000, 7):
        case = _case(seed)
        a, b = _schedule(case), _schedule(case)
        assert [s.run.key for s in a.ordered] == [s.run.key for s in b.ordered]
        assert ([(s.run.key, s.reason) for s in a.deferred]
                == [(s.run.key, s.reason) for s in b.deferred])


def test_the_generator_reaches_every_hold_and_deferral() -> None:
    """A property is only as good as the shapes it is run on: the generator
    must produce every deferral reason the admission pass can give, or the
    invariants were never exercised on it."""
    reasons: set[str] = set()
    for seed in SEEDS:
        sched = _schedule(_case(seed))
        reasons.update(s.reason for s in sched.deferred)
        reasons.update(s.reason for s in sched.ordered)
    for r in ("pair_wait", "coverage_split", "kind_cap", "nothing_aimed",
              "rebuy_flat", "rebuy_reverted", "rebuy_readmitted"):
        assert r in reasons, f"generator never produced {r}: {sorted(reasons)}"


def test_the_checker_names_a_hold_on_a_run_that_did_not_open() -> None:
    """The checker's own negative control, independent of the schedule."""
    from pipeline_core.domain.schedule_invariants import Decided
    u = FnLocator("src/a.rs", "f0")
    decided = [
        Decided(key="h", label="holder", targets=frozenset({u}), opened=False,
                reason="rebuy_reverted"),
        Decided(key="w", label="waiter", targets=frozenset({u}), opened=False,
                reason="coverage_split", held_by=("h",)),
        Decided(key="o", label="other", targets=frozenset(), opened=True),
    ]
    kinds = {v.kind for v in violations(
        decided, hollow_stubs=frozenset({u}), budget_minutes=0.0)}
    assert kinds == {"holder_not_opened", "blocker_unwritten"}


# ------------------------------------------------------------ the record

FIXTURE = (Path(__file__).parent / "fixtures" / "fills"
           / "82925-203ed7b8_iter5_decided.json")


def _recorded() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def test_the_recorded_decision_breaks_both_invariants() -> None:
    """Negative control: the decision the schedule of 2026-09-28 took on
    this fill held the per-fn run behind two runs it then deferred, and left
    the stub every failing test's first failure sat on with no writer."""
    ev = _recorded()["event"]
    found = violations(
        decided_from_event(ev), hollow_stubs=hollow_stubs_from_event(ev),
        crash_blamed=blamed_from_event(ev),
        budget_minutes=float(ev["budget_minutes"]))
    kinds = {v.kind for v in found}
    assert "holder_not_opened" in kinds
    assert "blocker_unwritten" in kinds


def test_todays_admission_on_the_recorded_ranking_keeps_both() -> None:
    """The same fill's ranked candidates — rank, weight, reason and level
    as that day's ranking produced them, which is not what this change is
    about — through today's order-and-admit stage."""
    rec = _recorded()
    ev = rec["event"]
    sched = admit_ranked(**_ranked_inputs(rec))
    decided = decided_from_schedule(sched)
    found = violations(
        decided, hollow_stubs=hollow_stubs_from_event(ev),
        crash_blamed=blamed_from_event(ev),
        budget_minutes=float(ev["budget_minutes"]))
    assert not found, [v.render() for v in found]
    opened = list(sched.ordered)
    assert opened[0].run.label == "per-fn 1 stub(s), 18 re-fill(s)"
    stubs = hollow_stubs_from_event(ev) or frozenset()
    assert opened[0].run.targets & stubs
    rebuys = [s for s in sched.deferred if s.reason == "rebuy_reverted"]
    assert [s.run.label for s in rebuys] == [
        "file src/error.rs [1 re-buy unit(s)]",
        "impl src/error.rs [1 re-buy unit(s)]",
    ]
    assert all(not s.held_by for s in rebuys)
    opened_keys = {s.run.key for s in opened}
    held = [s for s in sched.deferred if s.held_by]
    assert held and all(set(s.held_by) <= opened_keys for s in held)


def _ranked_inputs(rec: dict[str, Any]) -> dict[str, Any]:
    ev = rec["event"]
    inp = ev["inputs"]

    def loc(s: str) -> FnLocator:
        f, _, fn = s.partition("::")
        return FnLocator(f, fn)

    ranked = []
    for raw in list(ev["runs"]) + list(ev["deferred"]):
        run = RouteRun(
            kind=RouteKind(raw["kind"]), label=raw["label"],
            targets=frozenset(loc(t) for t in raw["targets"]),
            target_tests=frozenset(raw["target_tests"]),
            stubs_in_targets=frozenset(loc(t) for t in raw["stubs"]),
            files=tuple(raw["files"]), seq=int(raw["seq"]),
            bodies_sha=raw["bodies_sha"], parent=raw.get("parent", ""))
        # The ranking's reason as ranked, before any hold replaced it.
        reason = raw.get("prior_reason") or raw["reason"]
        if reason in ("pair_wait", "coverage_split", "kind_cap",
                      "nothing_aimed"):
            reason = raw.get("prior_reason") or "served"
        ranked.append((run, int(raw["rank"]), reason, int(raw["level"])))
    rows = [r for r in (RouteLedgerRow.from_dict(x)
                        for x in rec["ledger_rows"]) if r is not None]
    return {
        "ranked": ranked, "rows": rows, "oracle_fp": rec["oracle_fp"],
        "hollow": {t: frozenset(loc(x) for x in v)
                   for t, v in inp["hollow"].items()},
        "budget_minutes": float(ev["budget_minutes"]),
        "crash_blamed": [loc(x) for x in inp["crash_blamed"]],
        "attributed": [loc(x) for x in inp["attributed"]],
        "max_per_kind": {RouteKind(k): v
                         for k, v in inp["max_per_kind"].items()},
        "session_min_turns": int(inp["session_min_turns"]),
    }


@pytest.mark.parametrize("seed", [0])
def test_identity_helper_is_the_one_the_rows_use(seed: int) -> None:
    """The generator's rows are keyed on the runs' own identities, so its
    re-buy ranks are real re-buys, not a second guess at the key."""
    case = _case(seed)
    for row in case["rows"]:
        run = next(r for r in case["runs"] if r.label == row.label)
        assert row.identity == rebuy_identity(
            run.kind, run.targets, run.bodies_sha, ORACLE,
            target_tests=run.target_tests)


def test_the_executor_logs_a_broken_schedule_loudly() -> None:
    """`fill_scheduler._check_schedule` on the recorded (broken) decision
    logs `HEALTH ‼ SCHEDULE_INVARIANT` and emits the event; on today's
    decision of the same ranking it says nothing."""
    from types import SimpleNamespace

    from pipeline_core.application.reconstruct.fill_scheduler import (
        _check_schedule,
    )
    from pipeline_core.domain.fill_schedule import FillSchedule, ScheduledRun

    rec = _recorded()
    ev = rec["event"]
    inputs = _ranked_inputs(rec)
    by_label = {run.label: run for run, *_ in inputs["ranked"]}

    def as_scheduled(raw: dict[str, Any], deferred: bool) -> ScheduledRun:
        run = by_label[raw["label"]]
        return ScheduledRun(
            run=run, rank=int(raw["rank"]), weight=int(raw["weight"]),
            reason=raw["reason"], level=int(raw["level"]), deferred=deferred,
            share_minutes=raw.get("share_minutes"),
            held_by=tuple(raw.get("held_by") or ()))

    recorded = FillSchedule(
        tuple(as_scheduled(r, False) for r in ev["runs"]),
        tuple(as_scheduled(r, True) for r in ev["deferred"]),
        float(ev["budget_minutes"]))
    log: list[str] = []
    events: list[tuple[str, dict[str, Any]]] = []
    deps = SimpleNamespace(
        scheduled=True, crash_blamed=frozenset(inputs["crash_blamed"]),
        measured_owners=frozenset(), due=frozenset(),
        iteration=5, cluster="stable", log=log.append,
        emit=lambda name, data: events.append((name, data)))
    _check_schedule(recorded, inputs["hollow"], deps)  # type: ignore[arg-type]
    assert len(log) == 1 and "HEALTH ‼ SCHEDULE_INVARIANT fill 5" in log[0]
    assert [n for n, _ in events] == ["phase_c.fill.schedule_invariant"]
    assert {v["kind"] for v in events[0][1]["violations"]} == {
        "holder_not_opened", "blocker_unwritten"}
    log.clear()
    events.clear()
    _check_schedule(admit_ranked(**inputs), inputs["hollow"], deps)  # type: ignore[arg-type]
    assert log == [] and events == []


def test_the_ledger_slice_keeps_the_session_turn_price() -> None:
    """A session floor is priced from minutes and model calls. The slice
    the admission replay reads keeps both on a session row."""
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "scripts" / "distill_schedule_decision.py"
    spec = importlib.util.spec_from_file_location(
        "distill_schedule_decision", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    event = {"runs": [{"target_tests": ["t_floor"], "kind": "session"}],
             "deferred": []}
    loaded = [{
        "kind": "session", "label": "session #0", "targets": [],
        "bodies_sha": "b", "oracle_fp": "o", "outcome": "flat",
        "run_id": "r", "iteration": 1, "cluster": "c",
        "net_pass_delta": 0, "target_tests": ["t_floor", "other"],
        "newly_green": [], "reason": "served",
        "minutes": 11.3, "model_calls": 2,
    }]
    oracle, kept = mod.slice_ledger(loaded, event, run_id="r")
    assert oracle == "o"
    assert kept == [{
        "kind": "session", "label": "session #0", "targets": [],
        "bodies_sha": "b", "oracle_fp": "o", "outcome": "flat",
        "run_id": "r", "iteration": 1, "cluster": "c",
        "net_pass_delta": 0, "target_tests": ["t_floor"],
        "newly_green": [], "reason": "served",
        "minutes": 11.3, "model_calls": 2,
    }]


def test_an_admitted_owner_no_candidate_writes_is_named() -> None:
    """Run `486447-5207101c` fill 6: `src/ensure.rs::render` was admitted as
    a measured owner and dispatched, and the only `ensure.rs` runs wrote the
    `Buf` stubs. Controls: the same owner as a deferred run's target is a
    named deferral, not a violation; an owner in an opened run is written."""
    from pipeline_core.domain.models import FnLocator
    from pipeline_core.domain.schedule_invariants import Decided, violations

    render = FnLocator("src/ensure.rs", "render")
    buf = FnLocator("src/ensure.rs", "new")
    file_run = Decided(key="f", label="file src/ensure.rs",
                       targets=frozenset({buf}), opened=True, share_minutes=5.0)
    got = violations([file_run], hollow_stubs=None, measured_owners={render})
    assert [v.kind for v in got] == ["owner_unrouted"]
    assert "src/ensure.rs::render" in got[0].detail

    parked = Decided(key="s", label="session[1 test(s), 3 unit(s)] #8",
                     targets=frozenset({render}), opened=False,
                     reason="pair_wait")
    assert violations([file_run, parked], hollow_stubs=None,
                      measured_owners={render}) == ()
    opened = Decided(key="f2", label="file src/ensure.rs",
                     targets=frozenset({render, buf}), opened=True,
                     share_minutes=5.0)
    assert violations([opened], hollow_stubs=None,
                      measured_owners={render}) == ()
    assert violations([file_run], hollow_stubs=None) == ()


def test_an_owner_admitted_on_an_earlier_sweep_is_still_checked() -> None:
    """The checker's input was this sweep's admits and
    attaches inside the 12-cap batch, so `render`, admitted the sweep
    before and still in `ordered`, was invisible to it. The input is every
    owner-map unit still in `ordered` after the flat exclusion and the crash
    scope. Controls: the same owner inside a deferral with a reason does not
    fire; a unit `drop_standing_flat` removed has left `ordered`."""
    from pipeline_core.domain.escalation_lifetime import measured_owners_on_frontier
    from pipeline_core.domain.models import FnLocator
    from pipeline_core.domain.schedule_invariants import Decided, violations

    render = FnLocator("src/ensure.rs", "render")
    flat = FnLocator("src/ensure.rs", "flat_one")
    buf = FnLocator("src/ensure.rs", "new")
    admitted_before = {render, flat}
    this_sweep: set[FnLocator] = set()
    batch = {buf}
    # The old input: this sweep's admits inside the batch — empty.
    assert (this_sweep & batch) == set()
    ordered = [buf, render]          # `flat` left with the flat exclusion
    owners = measured_owners_on_frontier(admitted_before | this_sweep, ordered)
    assert owners == {render}
    file_run = Decided(key="f", label="file src/ensure.rs",
                       targets=frozenset({buf}), opened=True, share_minutes=5.0)
    got = violations([file_run], hollow_stubs=None, measured_owners=owners)
    assert [v.kind for v in got] == ["owner_unrouted"]
    assert "src/ensure.rs::render" in got[0].detail
    hollow = Decided(key="h", label="pair hollow", targets=frozenset({render}),
                     opened=False, reason="hollow")
    assert violations([file_run, hollow], hollow_stubs=None,
                      measured_owners=owners) == ()


def test_a_recorded_event_carries_its_admitted_owners() -> None:
    """The census reads `measured_owners` off the schedule event; an event
    that predates the field (or has no inputs) carries none."""
    from pipeline_core.domain.models import FnLocator
    from pipeline_core.domain.schedule_invariants import measured_owners_from_event

    assert measured_owners_from_event(
        {"inputs": {"measured_owners": ["src/ensure.rs::render"]}}) == frozenset(
            {FnLocator("src/ensure.rs", "render")})
    assert measured_owners_from_event({"inputs": {}}) == frozenset()
    assert measured_owners_from_event({}) == frozenset()


def _due_of(seed: int, runs: list[Any]) -> frozenset[FnLocator]:
    """Owed functions for one seed: a sample of what some candidate writes
    (a function no candidate writes is the driver's to place in the batch,
    and the checker names it whatever the schedule does)."""
    written = sorted({u for r in runs for u in r.targets},
                     key=lambda u: (u.file, u.fn_name))
    rng = random.Random(seed * 7919 + 1)
    return frozenset(rng.sample(written, min(len(written), rng.randint(0, 3))))


def test_every_owed_function_some_candidate_writes_gets_an_opened_writer() -> None:
    """Every generated seed: with `due`, no owed function a candidate writes
    is left without an opened writer, whatever held, re-bought or capped its
    writers. Control: the same seeds scheduled without `due` (the pre-cover
    admission) starve owed functions on some seeds, so the property can
    fail."""
    broken: list[str] = []
    starved_before = 0
    for seed in SEEDS:
        case = _case(seed)
        due = _due_of(seed, case["runs"])
        if not due:
            continue
        with_due = schedule(
            case["runs"], case["rows"], oracle_fp=ORACLE, hollow=case["hollow"],
            budget_minutes=case["budget"], crash_blamed=case["blamed"],
            attributed=case["attributed"], max_per_kind=case["max_per_kind"],
            unpaid_grace=case["unpaid_grace"], bodies_sha=lambda _t: "b",
            due=due)
        found = [v for v in violations(
            decided_from_schedule(with_due), hollow_stubs=None, due=due)
            if v.kind == "slice_unit_starved"]
        broken.extend(f"seed {seed}: {v.render()}" for v in found)
        starved_before += any(
            v.kind == "slice_unit_starved" for v in violations(
                decided_from_schedule(_schedule(case)), hollow_stubs=None,
                due=due))
    assert not broken, f"{len(broken)} violation(s):\n" + "\n".join(broken[:20])
    assert starved_before > 0, "the generator never starves an owed function"
