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
code of that day — is the negative control and must break both invariants;
the admission stage of today's code, fed the same ranked candidates, must
break neither and must open a run that writes the stub.
"""
from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

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
            hollow[t] = _pick(rng, tuple(stubs), 1, 2)
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
    stubs = hollow_stubs_from_event(ev) or frozenset()
    assert any(d.opened and d.targets & stubs for d in decided)


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
