"""The fill schedule's three rules, each with its negative control.

The sentences under test are the plan's, not the implementation's:
"a run that fills a stub which makes a leftover test hollow goes ahead of
runs that fill no such stub"; "a run with no ledger row on its identity
ranks ahead of a re-buy"; "a re-buy is deferred unless every candidate is
one or it was deferred `readmit_after` times"; "a run that cashed on its
last claim may extend past its share; one that produced nothing stops".
The recorded-fixture tests (`tests/test_fill_schedule_recorded.py`) pin
the same sentences on the live 2026-09-06 fills.
"""
from __future__ import annotations

from dataclasses import replace

from pipeline_core.domain.fill_schedule import (
    DEFAULT_UNPAID_GRACE,
    TODAY_ORDER,
    FillBudget,
    RouteKind,
    RouteLedgerRow,
    RouteRun,
    RunClock,
    RunOutcome,
    ScheduledRun,
    bodies_sha_of,
    consecutive_unpaid,
    hollow_by_test,
    rebuy_identity,
    row_paid_test,
    run_blocks,
    schedule,
    unpaid_attributed_writes,
    unpaid_tries_for_test,
)
from pipeline_core.domain.models import FnLocator, FnSite

_STUB = FnLocator("src/x.rs", "drop_rest")
_FMT = FnLocator("src/x.rs", "fmt")
_NEW = FnLocator("src/y.rs", "new")
_PEEK = FnLocator("src/x.rs", "peek")


def _session(label: str, tests: set[str], units: set[FnLocator], seq: int = 0) -> RouteRun:
    return RouteRun(kind=RouteKind.SESSION, label=label, targets=frozenset(units),
                    target_tests=frozenset(tests), files=("src/x.rs",), seq=seq,
                    bodies_sha="s")


def _file(label: str, units: set[FnLocator], stubs: set[FnLocator],
          tests: set[str] = frozenset(), sha: str = "f") -> RouteRun:
    return RouteRun(kind=RouteKind.FILE, label=label, targets=frozenset(units),
                    target_tests=frozenset(tests), stubs_in_targets=frozenset(stubs),
                    files=("src/x.rs",), bodies_sha=sha)


def _per_fn(stubs: set[FnLocator]) -> RouteRun:
    return RouteRun(kind=RouteKind.PER_FN, label="per-fn", targets=frozenset(stubs),
                    stubs_in_targets=frozenset(stubs), bodies_sha="p")


def _hollow() -> dict[str, frozenset[FnLocator]]:
    slices = {
        "t_hollow": {FnSite("src/x.rs", "drop_rest", ""), FnSite("src/x.rs", "fmt", "impl Debug for X")},
        "t_fmt": {FnSite("src/x.rs", "fmt", "impl Debug for X")},
    }
    return hollow_by_test(slices, {_STUB})


def test_hollow_by_test_names_the_blocking_stubs() -> None:
    h = _hollow()
    assert h == {"t_hollow": frozenset({_STUB})}


def test_a_stub_run_that_hollows_a_leftover_test_goes_before_a_stub_free_session() -> None:
    """Plan sentence: blockers first. Control: today's fixed order puts the
    session first."""
    session = _session("session[1 test(s)]", {"t_fmt"}, {_FMT})
    file_run = _file("file src/x.rs", {_STUB, _FMT}, {_STUB}, {"t_hollow", "t_fmt"})
    per_fn = _per_fn({_STUB})
    runs = [session, file_run, per_fn]
    sched = schedule(runs, [], oracle_fp="o", hollow=_hollow())
    assert [s.run.label for s in sched.ordered] == [
        "file src/x.rs", "per-fn", "session[1 test(s)]"]
    assert sched.ordered[0].level == 1 and sched.ordered[2].level == 2
    # Negative control: the pre-schedule walk.
    fixed = sorted(runs, key=lambda r: TODAY_ORDER.index(r.kind))
    assert fixed[0].kind is RouteKind.SESSION


def test_with_no_hollow_test_the_session_keeps_todays_precedence() -> None:
    """The 91-plateau shape: no stub blocks any leftover test, so nothing
    is demoted and the session opens first."""
    session = _session("session[1 test(s)]", {"t_fmt"}, {_FMT})
    file_run = _file("file src/x.rs", {_FMT}, set(), {"t_fmt"})
    sched = schedule([file_run, session], [], oracle_fp="o", hollow={})
    assert [s.run.label for s in sched.ordered] == [
        "session[1 test(s)]", "file src/x.rs"]


def _pair(label: str, units: set[FnLocator], tests: set[str]) -> RouteRun:
    return RouteRun(kind=RouteKind.PAIR, label=label, targets=frozenset(units),
                    target_tests=frozenset(tests),
                    files=tuple(sorted({u.file for u in units})), bodies_sha="pair")


def _row(run: RouteRun, outcome: RunOutcome, delta: int = 0,
         oracle: str = "o") -> RouteLedgerRow:
    return RouteLedgerRow(
        kind=run.kind, label=run.label,
        targets=tuple(sorted((f, fn) for f, fn in run.targets)),
        bodies_sha=run.bodies_sha, oracle_fp=oracle, outcome=outcome,
        net_pass_delta=delta,
        target_tests=tuple(sorted(run.target_tests)))


def _pair_blocked_inputs() -> tuple[RouteRun, RouteRun, RouteRun, list[RouteLedgerRow]]:
    pair = _pair("pair src/x.rs::fmt + src/y.rs::new", {_FMT, _NEW}, {"t_fmt"})
    prior = RouteRun(
        kind=pair.kind, label=pair.label, targets=pair.targets,
        target_tests=pair.target_tests, files=pair.files, bodies_sha="old")
    per_fn = RouteRun(
        kind=RouteKind.PER_FN, label="per-fn 0 stub(s), 6 re-fill(s)",
        targets=frozenset({_FMT, _STUB}), target_tests=frozenset({"t_fmt"}),
        bodies_sha="p")
    file_run = _file("file src/x.rs", {_FMT}, set(), {"t_fmt"})
    return pair, per_fn, file_run, [_row(prior, RunOutcome.FLAT, 0)]


def test_a_served_pair_precedes_never_served_runs_of_the_same_leftovers() -> None:
    """252 control of pair_wait: the same inputs opened pair, then file,
    then per-fn, and the budgeted case kept {pair, file} in ordered.
    Live schedule() after pair_wait is
    test_a_pair_blocked_run_is_deferred_this_fill."""
    pair, per_fn, file_run, rows = _pair_blocked_inputs()
    _252_ordered = [pair.label, file_run.label, per_fn.label]
    sched = schedule([per_fn, file_run, pair], rows, oracle_fp="o", hollow={})
    assert [s.run.label for s in sched.ordered] != _252_ordered
    budgeted = schedule(
        [per_fn, file_run, pair], rows, oracle_fp="o", hollow={},
        budget_minutes=60)
    assert not {pair.label, file_run.label} <= {s.run.label for s in budgeted.ordered}
    no_pair = schedule([per_fn, file_run], [], oracle_fp="o", hollow={})
    assert [s.run.label for s in no_pair.ordered] == [
        "file src/x.rs", "per-fn 0 stub(s), 6 re-fill(s)"]
    other = _pair("pair src/y.rs::new + src/y.rs::next", {_NEW}, {"t_other"})
    other_sched = schedule(
        [per_fn, file_run, other], [_row(other, RunOutcome.FLAT, 0)],
        oracle_fp="o", hollow={})
    assert [s.run.label for s in other_sched.ordered][:2] == [
        "file src/x.rs", "per-fn 0 stub(s), 6 re-fill(s)"]
    assert not run_blocks(other, per_fn, {})
    assert run_blocks(pair, per_fn, {})
    assert run_blocks(pair, file_run, {})
    assert not run_blocks(per_fn, pair, {})


def test_a_pair_blocked_run_is_deferred_this_fill() -> None:
    """A hop pair is the fill; a run it blocks is deferred this fill
    (pair_wait), not queued with a share. Also at budget 0. Control: the
    252 order on the same inputs kept the file and per-fn in ordered
    after the pair. A stub file that hollows the pair's leftovers still
    opens first. A pair that shares no leftover does not defer them."""
    pair, per_fn, file_run, rows = _pair_blocked_inputs()
    _252_ordered = [pair.label, file_run.label, per_fn.label]
    sched = schedule([per_fn, file_run, pair], rows, oracle_fp="o", hollow={})
    assert [s.run.label for s in sched.ordered] == [pair.label]
    assert [s.run.label for s in sched.ordered] != _252_ordered
    assert {s.run.label: s.reason for s in sched.deferred} == {
        file_run.label: "pair_wait", per_fn.label: "pair_wait"}
    assert all(s.deferred and s.share_minutes == 0.0 for s in sched.deferred)
    zero = schedule([per_fn, file_run, pair], rows, oracle_fp="o", hollow={},
                    budget_minutes=0)
    assert [s.run.label for s in zero.ordered] == [pair.label]
    assert {s.reason for s in zero.deferred} == {"pair_wait"}
    budgeted = schedule(
        [per_fn, file_run, pair], rows, oracle_fp="o", hollow={},
        budget_minutes=60)
    assert [s.run.label for s in budgeted.ordered] == [pair.label]
    assert budgeted.ordered[0].share_minutes == 60.0
    assert {s.reason for s in budgeted.deferred} == {"pair_wait"}
    holder = _file("file src/x.rs", {_STUB, _FMT}, {_STUB}, {"t_hollow"})
    hollow_pair = _pair("pair src/x.rs::fmt + src/y.rs::new", {_FMT, _NEW},
                        {"t_hollow"})
    stub_first = schedule([hollow_pair, holder], [], oracle_fp="o",
                          hollow=_hollow())
    assert [s.run.label for s in stub_first.ordered] == [
        holder.label, hollow_pair.label]
    assert not stub_first.deferred
    other = _pair("pair src/y.rs::new + src/y.rs::next", {_NEW}, {"t_other"})
    other_sched = schedule(
        [per_fn, file_run, other], [_row(other, RunOutcome.FLAT, 0)],
        oracle_fp="o", hollow={})
    assert [s.run.label for s in other_sched.ordered][:2] == [
        file_run.label, per_fn.label]
    assert not other_sched.deferred


def test_a_crash_blamed_run_of_the_pairs_callee_does_not_wait() -> None:
    """A compiling pair crash-revert recovered a table fn the pair does
    not write (a third file). pair_wait must not defer a run that holds
    that locator (same exception as a blocking stub). Control: the same
    file without the blamed locator still waits."""
    pair = _pair("pair src/x.rs::fmt + src/y.rs::new", {_FMT, _NEW}, {"t_fmt"})
    holder = _file("file src/x.rs", {_PEEK, _FMT}, set(), {"t_fmt"})
    assert not run_blocks(pair, holder, {}, crash_blamed={_PEEK})
    sched = schedule([pair, holder], [], oracle_fp="o", hollow={},
                     crash_blamed={_PEEK})
    assert [s.run.label for s in sched.ordered] == [
        holder.label, pair.label]
    assert not sched.deferred
    assert sched.ordered[0].level == 0
    empty = _file("file src/x.rs", {_FMT}, set(), {"t_fmt"})
    assert run_blocks(pair, empty, {}, crash_blamed={_PEEK})
    waiting = schedule([pair, empty], [], oracle_fp="o", hollow={},
                       crash_blamed={_PEEK})
    assert [s.run.label for s in waiting.ordered] == [pair.label]
    assert [s.run.label for s in waiting.deferred] == [empty.label]
    assert waiting.deferred[0].reason == "pair_wait"


def test_a_crash_callee_the_pair_holds_still_waits() -> None:
    """Same-file table slots join the pair write set, so a file of those
    locators is a split of the joint write and pair_waits. Control: today's
    blamed exemption on a pair that does not hold the locator still opens
    the file (test_a_crash_blamed_run_of_the_pairs_callee_does_not_wait)."""
    pair = _pair("pair src/x.rs::fmt + src/y.rs::new",
                 {_FMT, _NEW, _PEEK}, {"t_fmt"})
    holder = _file("file src/x.rs", {_PEEK, _FMT}, set(), {"t_fmt"})
    assert run_blocks(pair, holder, {}, crash_blamed={_PEEK})
    sched = schedule([pair, holder], [], oracle_fp="o", hollow={},
                     crash_blamed={_PEEK})
    assert [s.run.label for s in sched.ordered] == [pair.label]
    assert [s.run.label for s in sched.deferred] == [holder.label]
    assert sched.deferred[0].reason == "pair_wait"
    other = _file("file src/z.rs", {FnLocator("src/z.rs", "slot")}, set(),
                  {"t_fmt"})
    third = schedule(
        [pair, other], [], oracle_fp="o", hollow={},
        crash_blamed={FnLocator("src/z.rs", "slot")})
    assert other.label in [s.run.label for s in third.ordered]
    assert pair.label in [s.run.label for s in third.ordered]


def test_a_stub_run_that_hollows_the_pairs_leftovers_still_goes_first() -> None:
    """Blockers-first outranks the pair wait: a file that fills the stub
    making the leftover hollow does not wait on the pair. Control: the
    same file with no blocking stub does wait."""
    pair = _pair("pair src/x.rs::fmt + src/y.rs::new", {_FMT, _NEW}, {"t_hollow"})
    holder = _file("file src/x.rs", {_STUB, _FMT}, {_STUB}, {"t_hollow"})
    assert not run_blocks(pair, holder, _hollow())
    sched = schedule([pair, holder], [], oracle_fp="o", hollow=_hollow())
    assert [s.run.label for s in sched.ordered] == [
        "file src/x.rs", "pair src/x.rs::fmt + src/y.rs::new"]
    empty = _file("file src/x.rs", {_FMT}, set(), {"t_hollow"})
    assert run_blocks(pair, empty, {})
    filled = schedule([pair, empty], [], oracle_fp="o", hollow={})
    assert [s.run.label for s in filled.ordered] == [
        "pair src/x.rs::fmt + src/y.rs::new"]
    assert [s.run.label for s in filled.deferred] == ["file src/x.rs"]
    assert filled.deferred[0].reason == "pair_wait"


def test_a_pair_sharing_a_sessions_units_goes_before_that_session() -> None:
    """Plan: the pair is the two-file transaction; a session that can write
    either member waits. Control: TODAY_ORDER puts the session first, and
    a pair that shares no unit and no leftover test does not move."""
    session = _session("session[1 test(s)]", {"t_fmt"}, {_FMT, _NEW})
    pair = _pair("pair src/x.rs::fmt + src/y.rs::new", {_FMT, _NEW}, {"t_fmt"})
    assert run_blocks(pair, session, {})
    assert not run_blocks(session, pair, {})
    sched = schedule([session, pair], [], oracle_fp="o", hollow={})
    assert [s.run.label for s in sched.ordered] == [
        "pair src/x.rs::fmt + src/y.rs::new"]
    assert [s.run.label for s in sched.deferred] == ["session[1 test(s)]"]
    assert sched.deferred[0].reason == "pair_wait"
    assert sched.deferred[0].blocked_by == (
        "pair src/x.rs::fmt + src/y.rs::new",)
    fixed = sorted([session, pair], key=lambda r: TODAY_ORDER.index(r.kind))
    assert fixed[0].kind is RouteKind.SESSION
    other = _pair("pair src/y.rs::new + src/y.rs::next", {_NEW}, {"t_other"})
    session_fmt = _session("session[1 test(s)]", {"t_fmt"}, {_FMT})
    assert not run_blocks(other, session_fmt, {})
    sched2 = schedule([session_fmt, other], [], oracle_fp="o", hollow={})
    assert [s.run.label for s in sched2.ordered] == [
        "session[1 test(s)]", "pair src/y.rs::new + src/y.rs::next"]


def test_a_flat_pair_is_readmitted_when_pair_wait_empties_the_rest() -> None:
    """A FLAT pair is still a rebuy. Today a FLAT pair plus never-served
    sessions of the same leftovers opened the sessions. After pair_wait
    the sessions leave scheduled; the pair is the only remaining
    candidate; rebuy_readmitted opens the pair. Control: a FLAT pair
    next to a session of different leftovers still defers as rebuy_flat.
    Do not also flip pair FLAT to never_served."""
    pair = _pair("pair src/x.rs::fmt + src/y.rs::new", {_FMT, _NEW}, {"t_fmt"})
    session = _session("session[1 test(s)]", {"t_fmt"}, {_FMT, _NEW})
    rows = [_row(pair, RunOutcome.FLAT, 0)]
    _pre_pair_wait_ordered = [session.label]
    sched = schedule([pair, session], rows, oracle_fp="o", hollow={},
                     budget_minutes=60)
    assert [s.run.label for s in sched.ordered] == [pair.label]
    assert [s.run.label for s in sched.ordered] != _pre_pair_wait_ordered
    assert sched.ordered[0].reason == "rebuy_readmitted"
    assert [s.run.label for s in sched.deferred] == [session.label]
    assert sched.deferred[0].reason == "pair_wait"
    other = _session("session[1 test(s)]", {"t_other"}, {_STUB})
    mixed = schedule([pair, other], rows, oracle_fp="o", hollow={},
                     budget_minutes=60)
    assert [s.run.label for s in mixed.ordered] == [other.label]
    assert mixed.deferred[0].run.label == pair.label
    assert mixed.deferred[0].reason == "rebuy_flat"


def test_an_attributed_write_defers_a_coverage_session_when_there_is_no_pair() -> None:
    """Served file of an attributed unit; never-served session of that
    unit plus a coverage locator in another file. With attributed={the
    unit}, the file opens and the session is deferred coverage_split.
    Control: today's schedule (no attributed) opens the session first.
    A coverage-only run with no attributed write in the candidate set
    still opens."""
    file_run = _file("file src/x.rs", {_FMT}, set(), {"t_fmt"})
    session = _session("session[1 test(s)]", {"t_fmt"}, {_FMT, _NEW})
    rows = [_row(file_run, RunOutcome.FLAT, 0)]
    control = schedule([file_run, session], rows, oracle_fp="o", hollow={})
    assert [s.run.label for s in control.ordered] == [
        session.label, file_run.label]
    assert not control.deferred
    sched = schedule([file_run, session], rows, oracle_fp="o", hollow={},
                     attributed={_FMT}, budget_minutes=60)
    assert [s.run.label for s in sched.ordered] == [file_run.label]
    assert [s.run.label for s in sched.deferred] == [session.label]
    assert sched.deferred[0].reason == "coverage_split"
    zero = schedule([file_run, session], rows, oracle_fp="o", hollow={},
                    attributed={_FMT})
    assert [s.run.label for s in zero.ordered] == [file_run.label]
    assert zero.deferred[0].reason == "coverage_split"
    only_session = schedule([session], [], oracle_fp="o", hollow={},
                            attributed={_FMT})
    assert [s.run.label for s in only_session.ordered] == [session.label]
    assert not only_session.deferred


def test_same_file_extras_are_the_installable_complement_not_a_split() -> None:
    """A file of {attributed unit, same-file other} is not a coverage-split;
    a session of {attributed unit, other-file spine} is. Control: the
    written targets-minus-attributed-nonempty predicate would mark the
    file a split too and, with no non-split attributed write, open the
    session first."""
    file_run = _file("file src/x.rs", {_FMT, _STUB}, set(), {"t_fmt"})
    session = _session("session[1 test(s)]", {"t_fmt"}, {_FMT, _NEW})
    naive_file_is_split = bool(file_run.targets - {_FMT}) and bool(file_run.target_tests)
    assert naive_file_is_split
    control = schedule([file_run, session], [], oracle_fp="o", hollow={})
    assert [s.run.label for s in control.ordered][0] == session.label
    sched = schedule([file_run, session], [], oracle_fp="o", hollow={},
                     attributed={_FMT}, budget_minutes=60)
    assert [s.run.label for s in sched.ordered] == [file_run.label]
    assert [s.run.label for s in sched.deferred] == [session.label]
    assert sched.deferred[0].reason == "coverage_split"


def test_a_run_whose_tests_are_hollow_goes_after_the_run_holding_its_stub() -> None:
    holder = _file("file src/x.rs", {_STUB}, {_STUB}, {"t_hollow"})
    consumer = RouteRun(kind=RouteKind.IMPL, label="impl src/y.rs",
                        targets=frozenset({_NEW}), target_tests=frozenset({"t_hollow"}),
                        stubs_in_targets=frozenset({_NEW}), bodies_sha="i")
    assert run_blocks(holder, consumer, _hollow())
    assert not run_blocks(consumer, holder, _hollow())
    sched = schedule([consumer, holder], [], oracle_fp="o", hollow=_hollow())
    assert [s.run.label for s in sched.ordered] == ["file src/x.rs", "impl src/y.rs"]
    assert sched.ordered[1].blocked_by == ("file src/x.rs",)


def test_a_reverted_pair_retries_instead_of_deferring_under_a_budget() -> None:
    """A pair revert stores a diagnosis the next prompt carries. Ranking it
    as a zero-weight rebuy defers that retry, so the next fill opens the
    overlapping session instead. Control: a reverted FILE run on the same
    ledger still defers."""
    pair = _pair("pair src/x.rs::fmt + src/y.rs::new", {_FMT, _NEW}, {"t_fmt"})
    file_run = _file("file src/x.rs", {_FMT}, set(), {"t_fmt"})
    rows = [_row(pair, RunOutcome.REVERTED, 0),
            _row(file_run, RunOutcome.REVERTED, 0)]
    sched = schedule([pair, file_run], rows, oracle_fp="o", hollow={},
                     budget_minutes=60)
    assert [s.run.label for s in sched.ordered] == [
        "pair src/x.rs::fmt + src/y.rs::new"]
    assert sched.ordered[0].reason == "never_served"
    assert [s.run.label for s in sched.deferred] == ["file src/x.rs"]
    control = schedule([pair], [], oracle_fp="o", hollow={}, budget_minutes=60)
    assert control.ordered[0].reason == "never_served"


def test_a_flat_rebuy_sinks_below_a_never_served_run_and_is_deferred_under_a_budget() -> None:
    """Plan sentence: never-served before re-buys; a zero-weight re-buy is
    deferred. Control: with no ledger the same run keeps its rank."""
    file_run = _file("file src/x.rs", {_STUB}, {_STUB})
    per_fn = _per_fn({_STUB})
    rows = [_row(file_run, RunOutcome.FLAT, 0)]
    sched = schedule([file_run, per_fn], rows, oracle_fp="o", hollow=_hollow())
    assert [s.run.label for s in sched.ordered] == ["per-fn", "file src/x.rs"]
    assert sched.ordered[1].reason == "rebuy_flat"
    budgeted = schedule([file_run, per_fn], rows, oracle_fp="o",
                        hollow=_hollow(), budget_minutes=60)
    assert [s.run.label for s in budgeted.ordered] == ["per-fn"]
    assert [s.run.label for s in budgeted.deferred] == ["file src/x.rs"]
    assert budgeted.ordered[0].share_minutes == 60.0
    control = schedule([file_run, per_fn], [], oracle_fp="o", hollow=_hollow(),
                       budget_minutes=60)
    assert [s.run.label for s in control.ordered] == ["file src/x.rs", "per-fn"]
    assert control.ordered[0].reason == "never_served"
    assert control.ordered[0].share_minutes == 30.0


def test_a_unit_paid_on_another_route_is_served_not_never_served() -> None:
    """Service is per unit: the per-fn's only target was opened (and paid)
    by the file route under this oracle, so the per-fn is `served` (weight
    1), and the paying file run goes first."""
    file_run = _file("file src/x.rs", {_STUB}, {_STUB})
    per_fn = _per_fn({_STUB})
    rows = [_row(file_run, RunOutcome.CASHED, 3)]
    sched = schedule([per_fn, file_run], rows, oracle_fp="o", hollow=_hollow())
    assert sched.ordered[0].reason == "paying" and sched.ordered[0].weight == 2
    assert sched.ordered[1].run.label == "per-fn"
    assert sched.ordered[1].reason == "served" and sched.ordered[1].weight == 1


def test_a_body_this_route_moved_is_still_a_rebuy_another_routes_move_is_not() -> None:
    """The identity carries the bodies, and the route's own flat keep moves
    them — so under a whole-batch key every re-buy came back as a new try
    (run `450977-5f48b68a`). Per unit: the latest row on this route was
    flat and nothing paid since, so it is a re-buy — a flat keep elsewhere
    moves the body, not the evidence. Once a try on ANOTHER route PAID
    after it, the verdicts it saw moved: `served`."""
    before = _file("file src/x.rs", {_STUB}, {_STUB}, sha="old")
    after = _file("file src/x.rs", {_STUB}, {_STUB}, sha="new")
    rows = [_row(before, RunOutcome.FLAT, 0)]
    assert rebuy_identity(after.kind, after.targets, after.bodies_sha, "o") != rows[0].identity
    sched = schedule([after], rows, oracle_fp="o", hollow=_hollow())
    assert sched.ordered[0].reason == "rebuy_flat" and sched.ordered[0].weight == 0
    flat_elsewhere = [*rows, _row(_per_fn({_STUB}), RunOutcome.FLAT, 0)]
    sched = schedule([after], flat_elsewhere, oracle_fp="o", hollow=_hollow())
    assert sched.ordered[0].reason == "rebuy_flat"
    paid_elsewhere = [*rows, _row(_per_fn({_STUB}), RunOutcome.CASHED, 1)]
    sched = schedule([after], paid_elsewhere, oracle_fp="o", hollow=_hollow())
    assert sched.ordered[0].reason == "served" and sched.ordered[0].weight == 1


def test_a_rebuy_deferred_readmit_after_times_is_readmitted_with_weight_one() -> None:
    file_run = _file("file src/x.rs", {_STUB}, {_STUB})
    per_fn = _per_fn({_STUB})
    rows = [_row(file_run, RunOutcome.FLAT, 0),
            _row(file_run, RunOutcome.DEFERRED), _row(file_run, RunOutcome.DEFERRED)]
    sched = schedule([file_run, per_fn], rows, oracle_fp="o", hollow=_hollow(),
                     budget_minutes=60, readmit_after=2)
    labels = [s.run.label for s in sched.ordered]
    assert "file src/x.rs" in labels and not sched.deferred
    readmitted = next(s for s in sched.ordered if s.run.label == "file src/x.rs")
    assert readmitted.reason == "rebuy_readmitted" and readmitted.weight == 1
    once = rows[:2]
    sched_once = schedule([file_run, per_fn], once, oracle_fp="o", hollow=_hollow(),
                          budget_minutes=60, readmit_after=2)
    assert [s.run.label for s in sched_once.deferred] == ["file src/x.rs"]


def test_when_every_candidate_is_a_rebuy_the_budget_is_shared_not_spun() -> None:
    file_run = _file("file src/x.rs", {_STUB}, {_STUB})
    per_fn = _per_fn({_STUB})
    rows = [_row(file_run, RunOutcome.FLAT, 0), _row(per_fn, RunOutcome.REVERTED, -2)]
    sched = schedule([file_run, per_fn], rows, oracle_fp="o", hollow=_hollow(),
                     budget_minutes=60)
    assert not sched.deferred
    assert {s.reason for s in sched.ordered} == {"rebuy_readmitted"}
    assert sum(s.share_minutes or 0 for s in sched.ordered) == 60.0


def test_an_oracle_move_makes_every_row_a_different_identity() -> None:
    file_run = _file("file src/x.rs", {_STUB}, {_STUB})
    rows = [_row(file_run, RunOutcome.FLAT, 0, oracle="old")]
    sched = schedule([file_run], rows, oracle_fp="new", hollow=_hollow())
    # Service is counted under THIS oracle: a row graded against another
    # set of tests served nothing here.
    assert sched.ordered[0].reason == "never_served"


def test_order_only_under_a_zero_budget_defers_nothing() -> None:
    file_run = _file("file src/x.rs", {_STUB}, {_STUB})
    per_fn = _per_fn({_STUB})
    rows = [_row(file_run, RunOutcome.FLAT, 0)]
    sched = schedule([file_run, per_fn], rows, oracle_fp="o", hollow=_hollow(),
                     budget_minutes=0)
    assert len(sched.ordered) == 2 and not sched.deferred
    assert all(s.share_minutes is None for s in sched.ordered)


def test_an_unmeasurable_run_ranks_rebuy_flat_and_defers_under_a_budget() -> None:
    """A run whose every aimed test is unmeasurable, and whose targets miss
    the crash-blamed set, ranks rebuy_flat (weight 0) even with no ledger
    row. Under a budget it defers behind a never-served neighbour.
    Control: without the new arguments it ranks never_served and runs."""
    fmt = _FMT
    source = FnLocator("src/x.rs", "source")
    drop = FnLocator("src/y.rs", "drop")
    aimed = {"t_crash_a", "t_crash_b"}
    file_run = RouteRun(
        kind=RouteKind.FILE, label="file src/fmt.rs",
        targets=frozenset({fmt, source}), target_tests=frozenset(aimed),
        files=("src/x.rs",), bodies_sha="f")
    neighbour = RouteRun(
        kind=RouteKind.PER_FN, label="per-fn",
        targets=frozenset({_NEW}), target_tests=frozenset({"t_ok"}),
        bodies_sha="p")
    unmeas = aimed
    blamed = {drop}
    control = schedule([file_run, neighbour], [], oracle_fp="o", hollow={})
    assert {s.run.label: s.reason for s in control.ordered} == {
        "file src/fmt.rs": "never_served", "per-fn": "never_served"}
    assert not control.deferred
    ranked = schedule(
        [file_run, neighbour], [], oracle_fp="o", hollow={},
        unmeasurable_tests=unmeas, crash_blamed=blamed)
    by = {s.run.label: s for s in ranked.ordered}
    assert by["file src/fmt.rs"].reason == "rebuy_flat"
    assert by["file src/fmt.rs"].weight == 0
    assert by["per-fn"].reason == "never_served"
    budgeted = schedule(
        [file_run, neighbour], [], oracle_fp="o", hollow={},
        budget_minutes=60, unmeasurable_tests=unmeas, crash_blamed=blamed)
    assert [s.run.label for s in budgeted.ordered] == ["per-fn"]
    assert [s.run.label for s in budgeted.deferred] == ["file src/fmt.rs"]


def test_a_crash_blamed_target_keeps_the_run_measurable() -> None:
    """Same run as the unmeasurable case, but crash_blamed includes a
    target: not unmeasurable, keeps never_served."""
    fmt = _FMT
    source = FnLocator("src/x.rs", "source")
    file_run = RouteRun(
        kind=RouteKind.FILE, label="file src/fmt.rs",
        targets=frozenset({fmt, source}),
        target_tests=frozenset({"t_crash_a", "t_crash_b"}),
        files=("src/x.rs",), bodies_sha="f")
    sched = schedule(
        [file_run], [], oracle_fp="o", hollow={},
        unmeasurable_tests={"t_crash_a", "t_crash_b"},
        crash_blamed={fmt})
    assert sched.ordered[0].reason == "never_served"
    assert not sched.deferred


def test_one_measurable_aimed_test_keeps_the_run_measurable() -> None:
    """Same run, one aimed test not in unmeasurable_tests: not unmeasurable."""
    fmt = _FMT
    file_run = RouteRun(
        kind=RouteKind.FILE, label="file src/fmt.rs",
        targets=frozenset({fmt}),
        target_tests=frozenset({"t_crash_a", "t_ok"}),
        files=("src/x.rs",), bodies_sha="f")
    sched = schedule(
        [file_run], [], oracle_fp="o", hollow={},
        unmeasurable_tests={"t_crash_a"},
        crash_blamed={FnLocator("src/y.rs", "drop")})
    assert sched.ordered[0].reason == "never_served"


def test_bodies_sha_folds_whitespace_and_ignores_order() -> None:
    a = bodies_sha_of([(_STUB, "let x = 1;\n  y"), (_FMT, "z")])
    b = bodies_sha_of([(_FMT, "z"), (_STUB, "let x = 1; y")])
    assert a == b
    assert a != bodies_sha_of([(_STUB, "let x = 2; y"), (_FMT, "z")])


def test_row_round_trips_through_json() -> None:
    run = _file("file src/x.rs", {_STUB}, {_STUB})
    row = _row(run, RunOutcome.FLAT, 0)
    back = RouteLedgerRow.from_dict(__import__("json").loads(row.to_json()))
    assert back == row


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_run_clock_claims_within_share_extends_only_while_paying() -> None:
    """Plan sentence: budget follows payoff. An unpaid run past its share
    stops claiming; a paid one extends into the fill's unspent budget."""
    now = _Clock()
    budget = FillBudget(100, now=now)
    clock = RunClock(share_minutes=10, fill_remaining=budget.remaining, now=now)
    assert clock.may_claim()
    now.t = 11 * 60
    assert not clock.may_claim()  # share spent, nothing paid
    clock.note_delta(3)
    assert clock.may_claim()      # paying: extends into the fill budget
    now.t = 101 * 60
    assert not clock.may_claim()  # the fill budget is gone too
    assert clock.refused == 2


def test_an_unbounded_clock_always_claims_and_shares_flow_forward() -> None:
    now = _Clock()
    clock = RunClock(share_minutes=None, fill_remaining=lambda: None, now=now)
    now.t = 10_000
    assert clock.may_claim()
    now.t = 0
    budget = FillBudget(60, now=now)

    def _ahead(weight: int, label: str) -> ScheduledRun:
        return ScheduledRun(
            run=RouteRun(kind=RouteKind.PER_FN, label=label,
                         targets=frozenset({_FMT})),
            rank=0, weight=weight, reason="never_served", level=1)

    assert budget.opening_shares([_ahead(2, "a"), _ahead(2, "b")]) == [30.0, 30.0]
    now.t = 30 * 60  # the first run finished early: 30 min unspent of 60
    assert budget.opening_shares([_ahead(2, "b")]) == [30.0]  # flows forward
    assert budget.opening_shares([_ahead(0, "z")]) == [0.0]
    unbounded = FillBudget(0, now=now)
    assert unbounded.opening_shares([_ahead(2, "b")]) == [None]


def test_two_sessions_that_share_a_count_label_both_appear_in_the_schedule() -> None:
    """Plan sentence: two SESSION RouteRuns with the same label, different
    seq and target_tests, both appear in schedule().ordered, and the set
    of keys has two entries."""
    a = _session("session[2 test(s), 2 unit(s)]", {"t_a", "t_b"}, {_FMT, _NEW}, seq=0)
    b = _session("session[2 test(s), 2 unit(s)]", {"t_c", "t_d"}, {_FMT, _STUB}, seq=1)
    # Negative control: today's kind:label key collides.
    assert f"{a.kind.value}:{a.label}" == f"{b.kind.value}:{b.label}"
    assert a.key != b.key
    sched = schedule([a, b], [], oracle_fp="o", hollow={})
    assert len(sched.ordered) == 2
    assert {s.run.key for s in sched.ordered} == {a.key, b.key}


def test_session_rebuy_is_the_slice_file_rebuy_is_the_bodies() -> None:
    """Plan sentence: two SESSION runs with equal targets and bodies_sha
    and different target_tests have different rebuy_identity. Two FILE
    runs with equal targets and bodies_sha and different target_tests
    have the same rebuy_identity."""
    units = {_FMT, _NEW}
    s1 = _session("session[2 test(s), 2 unit(s)]", {"t_a", "t_b"}, units, seq=0)
    s2 = _session("session[2 test(s), 2 unit(s)]", {"t_c", "t_d"}, units, seq=1)
    old = (
        f"{s1.kind.value}|"
        + ",".join(sorted(f"{f}::{fn}" for f, fn in s1.targets))
        + f"|{s1.bodies_sha}|o")
    assert old == (
        f"{s2.kind.value}|"
        + ",".join(sorted(f"{f}::{fn}" for f, fn in s2.targets))
        + f"|{s2.bodies_sha}|o")
    assert rebuy_identity(
        s1.kind, s1.targets, s1.bodies_sha, "o",
        target_tests=s1.target_tests) != rebuy_identity(
        s2.kind, s2.targets, s2.bodies_sha, "o",
        target_tests=s2.target_tests)
    f1 = _file("file src/x.rs", {_STUB}, {_STUB},
               tests={"t_hollow", "t_fmt"}, sha="f")
    f2 = _file("file src/x.rs", {_STUB}, {_STUB}, tests={"t_other"}, sha="f")
    assert rebuy_identity(
        f1.kind, f1.targets, f1.bodies_sha, "o",
        target_tests=f1.target_tests) == rebuy_identity(
        f2.kind, f2.targets, f2.bodies_sha, "o",
        target_tests=f2.target_tests)
    sched = schedule([s2], [_row(s1, RunOutcome.FLAT, 0)],
                     oracle_fp="o", hollow={})
    # Its units were opened by a session aimed at other tests: served (a
    # new question on known units), not a re-buy of s1 and not never-served.
    assert sched.ordered[0].reason == "served"


def test_an_empty_row_neither_hides_the_last_verdict_nor_serves_the_targets() -> None:
    """Plan sentences on EMPTY (the route returned before its first
    dispatch): on the same identity an EMPTY row after a FLAT row leaves
    the identity a flat re-buy — the EMPTY row is not evidence and does
    not hide the verdict before it; on a moved identity (the bodies
    changed, targets the same) an EMPTY or DEFERRED row alone leaves the
    run never-served — a try that never opened served nothing. Control:
    a FLAT row on the moved identity is the unit's re-buy on this route."""
    f1 = _file("file src/x.rs", {_FMT}, set(), {"t_fmt"}, sha="f1")
    flat_then_empty = [_row(f1, RunOutcome.FLAT, 0), _row(f1, RunOutcome.EMPTY, 0)]
    sched = schedule([f1], flat_then_empty, oracle_fp="o", hollow={})
    assert sched.ordered[0].reason == "rebuy_flat"
    f2 = _file("file src/x.rs", {_FMT}, set(), {"t_fmt"}, sha="f2")
    for outcome in (RunOutcome.EMPTY, RunOutcome.DEFERRED):
        sched = schedule([f2], [_row(f1, outcome, 0)], oracle_fp="o", hollow={})
        assert sched.ordered[0].reason == "never_served", outcome
    sched = schedule([f2], [_row(f1, RunOutcome.FLAT, 0)], oracle_fp="o", hollow={})
    assert sched.ordered[0].reason == "rebuy_flat"


def test_a_per_kind_cap_defers_the_sessions_beyond_it_in_schedule_order() -> None:
    """Run `3014175-d98ed09e` iter 3 (2026-09-08): eight packed sessions of
    14–50 minutes ran back to back for 4 h 18 min before the file and
    per-fn routes opened. Under `max_per_kind={SESSION: 4}` the first four
    in schedule order open, the rest are deferred `kind_cap` (share 0) and
    re-enter the next fill as never-served candidates; other kinds are
    untouched. Control: no cap opens all eight."""
    sessions = [_session(f"session #{i}", {f"t{i}"}, {FnLocator("src/x.rs", f"f{i}")}, seq=i)
                for i in range(8)]
    per_fn = _per_fn({FnLocator("src/x.rs", "s")})
    capped = schedule([*sessions, per_fn], [], oracle_fp="o", hollow={},
                      max_per_kind={RouteKind.SESSION: 4})
    opened = [s.run.label for s in capped.ordered]
    assert opened == [f"session #{i}" for i in range(4)] + [per_fn.label]
    assert [s.run.label for s in capped.deferred] == [f"session #{i}" for i in range(4, 8)]
    assert all(s.reason == "kind_cap" and s.deferred and s.share_minutes == 0.0
               for s in capped.deferred)
    # Under a budget the capped ones stay deferred and the rest share it.
    budgeted = schedule([*sessions, per_fn], [], oracle_fp="o", hollow={},
                        budget_minutes=100.0, max_per_kind={RouteKind.SESSION: 4})
    assert len(budgeted.ordered) == 5 and len(budgeted.deferred) == 4
    assert all(s.share_minutes for s in budgeted.ordered)
    # Control: no cap, or a cap of 0, opens every session.
    for cap in (None, {RouteKind.SESSION: 0}):
        free = schedule([*sessions, per_fn], [], oracle_fp="o", hollow={},
                        max_per_kind=cap)
        assert len(free.ordered) == 9 and not free.deferred


def test_the_grace_counts_a_route_across_a_moved_bodies_sha() -> None:
    """The sentence: a route that keeps writing SOMETHING still
    accumulates a history. Keyed on the re-buy identity it would not —
    the identity carries the bodies sha, and a hop pair moves it every
    fill by keeping the table slots no test executes. That is why the
    live schedule kept calling a route with dozens of graded, never-paying
    rows `never_served`.

    Negative control: the same rows counted the identity's way."""
    pair = _pair("pair src/x.rs::fmt + src/y.rs::new", {_FMT, _NEW}, {"t_fmt"})
    rows = []
    for i in range(5):
        moved = replace(pair, bodies_sha=f"body{i}")
        rows.append(_row(moved, RunOutcome.REVERTED, 0))
    assert consecutive_unpaid(rows, pair, "o") == 5
    ident = rebuy_identity(pair.kind, pair.targets, pair.bodies_sha, "o",
                           target_tests=pair.target_tests)
    assert len([r for r in rows if r.identity == ident]) == 0


def test_the_grace_stops_at_a_row_that_paid() -> None:
    pair = _pair("pair src/x.rs::fmt + src/y.rs::new", {_FMT, _NEW}, {"t_fmt"})
    rows = [_row(pair, RunOutcome.REVERTED) for _ in range(4)]
    rows.insert(2, _row(pair, RunOutcome.CASHED, 3))
    assert consecutive_unpaid(rows, pair, "o") == 2
    rows.append(_row(pair, RunOutcome.FLAT, 2))          # a net gain also pays
    assert consecutive_unpaid(rows, pair, "o") == 0


def test_the_grace_ignores_rows_that_spent_no_fill() -> None:
    """A deferred row opened nothing, an empty row returned before its
    first dispatch, a stopped row was cut by the operator's graceful stop:
    none of the three consumed a fill, so none counts toward the grace or
    interrupts it."""
    pair = _pair("pair src/x.rs::fmt + src/y.rs::new", {_FMT, _NEW}, {"t_fmt"})
    rows = [_row(pair, RunOutcome.REVERTED),
            _row(pair, RunOutcome.DEFERRED),
            _row(pair, RunOutcome.EMPTY),
            _row(pair, RunOutcome.STOPPED),
            _row(pair, RunOutcome.REVERTED)]
    assert consecutive_unpaid(rows, pair, "o") == 2


def test_a_truncated_run_spends_the_grace_it_does_not_earn_a_rank_from() -> None:
    """An UNMEASURED row is not evidence that the route cannot pay — the
    rank reads it as never served — but it IS a fill the route spent while
    zeroing every competing run, which is the only question this clock
    asks. Three truncated tries therefore lapse a grace of three.

    The negative control is the rule this replaced: skipping them, the same
    three rows read as 0 and the competitors stay deferred forever, since a
    route that never finishes a deliberation never grades anything else
    either."""
    pair = _pair("pair src/x.rs::fmt + src/y.rs::new", {_FMT, _NEW}, {"t_fmt"})
    rows = [_row(pair, RunOutcome.UNMEASURED) for _ in range(3)]
    assert consecutive_unpaid(rows, pair, "o") == 3
    skipped = [r for r in rows if r.outcome is not RunOutcome.UNMEASURED]
    assert consecutive_unpaid(skipped, pair, "o") == 0


def test_a_cash_still_ends_the_streak_before_a_truncated_try() -> None:
    """Counting truncated tries does not make the streak unstoppable: the
    walk still stops at the newest row that paid."""
    pair = _pair("pair src/x.rs::fmt + src/y.rs::new", {_FMT, _NEW}, {"t_fmt"})
    rows = [_row(pair, RunOutcome.UNMEASURED),
            _row(pair, RunOutcome.CASHED),
            _row(pair, RunOutcome.UNMEASURED),
            _row(pair, RunOutcome.FLAT)]
    assert consecutive_unpaid(rows, pair, "o") == 2


def test_the_grace_does_not_read_rows_graded_under_another_oracle() -> None:
    pair = _pair("pair src/x.rs::fmt + src/y.rs::new", {_FMT, _NEW}, {"t_fmt"})
    rows = [_row(pair, RunOutcome.REVERTED, oracle="other") for _ in range(9)]
    assert consecutive_unpaid(rows, pair, "o") == 0
    rows += [_row(pair, RunOutcome.REVERTED) for _ in range(3)]
    assert consecutive_unpaid(rows, pair, "o") == 3


def test_a_pair_past_its_grace_loses_its_precedence_and_the_rest_get_a_share() -> None:
    """The sentence. A pair past its grace neither zeroes the runs its
    leftovers share nor sorts ahead of them: the never-served runs go
    first and the pair queues with its own ledger rank (`grace_spent`,
    weight 1). Inside the grace the pair still leads and the rest are
    `pair_wait` at share 0 (negative control: one fewer graded row).

    Measured on run `3893442-cb547388` (2026-09-19): with the order kept
    past the grace the pairs opened ahead of the session that paid, every
    fill, once the fill re-planned at its first premise move."""
    pair, per_fn, file_run, _ = _pair_blocked_inputs()
    runs = [pair, per_fn, file_run]
    spent = [_row(replace(pair, bodies_sha=f"b{i}"), RunOutcome.REVERTED)
             for i in range(DEFAULT_UNPAID_GRACE)]

    inside = spent[1:]
    assert consecutive_unpaid(inside, pair, "o") == DEFAULT_UNPAID_GRACE - 1
    control = schedule(runs, inside, oracle_fp="o", hollow={},
                       budget_minutes=120)
    assert [s.run.label for s in control.ordered] == [pair.label]
    assert all(s.reason == "pair_wait" and s.share_minutes == 0.0
               for s in control.deferred)

    sched = schedule(runs, spent, oracle_fp="o", hollow={},
                     budget_minutes=120)
    order = [s.run.label for s in sched.ordered]
    # Past the grace the pair's units carry `unpaid_grace` reverted pair
    # rows and nothing since: the pair is the re-buy its units' record says
    # (weight 0, deferred, re-admitted after `readmit_after` deferrals) and
    # the runs it held open with a share.
    assert set(order) == {per_fn.label, file_run.label}
    assert [(s.run.label, s.reason) for s in sched.deferred] == [
        (pair.label, "rebuy_reverted")]
    assert not [s for s in sched.deferred if s.reason == "pair_wait"]
    assert all(s.share_minutes and s.share_minutes > 0 for s in sched.ordered)


def test_a_pair_past_its_grace_that_returned_the_same_bodies_flat_is_a_rebuy() -> None:
    """Past the grace the ledger speaks for the pair as for any run: its
    last graded row on the identical bodies came back FLAT, so it is a
    `rebuy_flat` and a positive budget defers it. Inside the grace the
    same history keeps it first (control)."""
    pair, per_fn, file_run, _ = _pair_blocked_inputs()
    moved = [_row(replace(pair, bodies_sha=f"b{i}"), RunOutcome.FLAT)
             for i in range(DEFAULT_UNPAID_GRACE - 1)]
    rows = [*moved, _row(pair, RunOutcome.FLAT)]
    sched = schedule([pair, per_fn, file_run], rows, oracle_fp="o",
                     hollow={}, budget_minutes=120)
    assert [s.run.label for s in sched.deferred] == [pair.label]
    assert sched.deferred[0].reason == "rebuy_flat"
    assert {s.run.label for s in sched.ordered} == {per_fn.label, file_run.label}
    control = schedule([pair, per_fn, file_run], rows[1:], oracle_fp="o",
                       hollow={}, budget_minutes=120)
    assert [s.run.label for s in control.ordered] == [pair.label]


def test_the_grace_bound_is_switchable_off() -> None:
    """`unpaid_grace=0` restores the unbounded exemptions, so a reading
    can be taken against the old behaviour without reverting code."""
    pair, per_fn, file_run, _ = _pair_blocked_inputs()
    rows = [_row(replace(pair, bodies_sha=f"b{i}"), RunOutcome.REVERTED)
            for i in range(20)]
    sched = schedule([pair, per_fn, file_run], rows, oracle_fp="o", hollow={},
                     budget_minutes=120, unpaid_grace=0)
    assert [s.run.label for s in sched.ordered] == [pair.label]
    assert sched.ordered[0].reason != "grace_spent"
    assert all(s.reason == "pair_wait" and s.share_minutes == 0.0
               for s in sched.deferred)
    assert {s.run.label for s in sched.deferred} == {per_fn.label, file_run.label}


# --- per-test pay: an exemption over one leftover is spent by that
# leftover's own record, not by what the route did for a sibling.

_OTHER = FnLocator("src/z.rs", "fmt")


def test_a_row_that_paid_a_sibling_is_an_unpaid_try_of_the_other_test() -> None:
    """Plan sentence: a row paid `t` only if it turned `t` green. A pair
    aimed at two tests that cashed on one is a try of the other that did
    not pay it, so the other's streak runs through it.

    Negative control: the same row with no recorded green set (every row
    written before the set existed) stops the walk for both tests — the
    row-level answer every walk gave before, which is what restarted the
    streak of a leftover the cash never moved."""
    pair = _pair("pair src/x.rs::fmt + src/y.rs::new", {_FMT, _NEW},
                 {"t_a", "t_b"})
    cash = replace(_row(pair, RunOutcome.CASHED, 2), newly_green=("t_a",))
    rows = [_row(pair, RunOutcome.REVERTED), _row(pair, RunOutcome.REVERTED),
            cash, _row(pair, RunOutcome.FLAT)]
    assert row_paid_test(cash, "t_a") and not row_paid_test(cash, "t_b")
    assert unpaid_tries_for_test(rows, "t_a", "o") == 1
    assert unpaid_tries_for_test(rows, "t_b", "o") == 4
    legacy = [*rows[:2], replace(cash, newly_green=None), rows[3]]
    assert unpaid_tries_for_test(legacy, "t_b", "o") == 1
    assert unpaid_tries_for_test(legacy, "t_a", "o") == 1
    # A green name printed with its module path is the same test.
    pathed = replace(cash, newly_green=("tests::t_b",))
    assert row_paid_test(pathed, "t_b")


def test_an_unrecorded_cash_the_same_process_aimed_at_again_did_not_pay_that_test() -> None:
    """Plan sentence: a row with no green set did not pay a test that the
    same process aimed a row at in a later iteration — that later row's
    aimed tests are the later sweep's leftovers.

    Negative controls: the same rows with the later row in ANOTHER process
    (a relaunch: nothing ties its sweep to this row's keeps), and with the
    later row aimed only at a sibling, both stop the walk at the cash."""
    pair = _pair("pair src/x.rs::fmt + src/y.rs::new", {_FMT, _NEW},
                 {"t_a", "t_b"})
    tries = [replace(_row(pair, RunOutcome.FLAT), run_id="p1", iteration=i)
             for i in (1, 2)]
    cash = replace(_row(pair, RunOutcome.CASHED, 2), run_id="p1", iteration=3)
    later = replace(_row(replace(pair, target_tests=frozenset({"t_b"})),
                         RunOutcome.FLAT), run_id="p1", iteration=4)
    rows = [*tries, cash, later]
    assert unpaid_tries_for_test(rows, "t_b", "o") == 4
    assert unpaid_tries_for_test(rows, "t_a", "o") == 0
    relaunched = [*tries, cash, replace(later, run_id="p2", iteration=1)]
    assert unpaid_tries_for_test(relaunched, "t_b", "o") == 1
    sibling = [*tries, cash, replace(
        later, target_tests=("t_c",))]
    assert unpaid_tries_for_test(sibling, "t_b", "o") == 0


def test_a_recorded_green_set_that_is_empty_paid_nobody() -> None:
    pair = _pair("pair src/x.rs::fmt + src/y.rs::new", {_FMT, _NEW}, {"t_a"})
    net_gain = replace(_row(pair, RunOutcome.CASHED, 1), newly_green=())
    assert not row_paid_test(net_gain, "t_a")
    assert row_paid_test(replace(net_gain, newly_green=None), "t_a")


def test_the_green_set_survives_the_ledger_round_trip() -> None:
    pair = _pair("pair src/x.rs::fmt + src/y.rs::new", {_FMT, _NEW}, {"t_a"})
    row = replace(_row(pair, RunOutcome.CASHED, 1), newly_green=("t_a",))
    back = RouteLedgerRow.from_dict(row.to_dict())
    assert back is not None and back.newly_green == ("t_a",)
    empty = RouteLedgerRow.from_dict(replace(row, newly_green=()).to_dict())
    assert empty is not None and empty.newly_green == ()
    raw = row.to_dict()
    del raw["newly_green"]                       # a row written before the field
    legacy = RouteLedgerRow.from_dict(raw)
    assert legacy is not None and legacy.newly_green is None


def _split_inputs() -> tuple[RouteRun, RouteRun]:
    """An attributed write of `t_a` in src/x.rs, and a never-served per-fn
    of `t_a` holding a unit in a file no attributed member occupies."""
    file_run = _file("file src/x.rs", {_FMT}, set(), {"t_a"})
    split = RouteRun(kind=RouteKind.PER_FN, label="per-fn 0 stub(s), 1 re-fill(s)",
                     targets=frozenset({_OTHER}), target_tests=frozenset({"t_a"}),
                     bodies_sha="p")
    return file_run, split


def test_a_coverage_split_stops_waiting_once_the_attributed_write_spent_its_grace() -> None:
    """Plan sentence: the coverage-split deferral is bounded like
    pair_wait. After `unpaid_grace` graded attributed writes of the
    leftover in a row that did not turn it green, the split run opens —
    behind the attributed write, with a share — and the schedule line
    says the deferral lapsed.

    Negative controls: one fewer graded write still defers it
    `coverage_split`; `unpaid_grace=0` defers it however long the
    attributed write has failed."""
    file_run, split = _split_inputs()
    runs = [file_run, split]
    spent = [_row(replace(file_run, bodies_sha=f"b{i}"), RunOutcome.FLAT)
             for i in range(DEFAULT_UNPAID_GRACE)]
    # A try on another route that aimed at no test paid after the file
    # writes: the attributed write is `served` (the verdicts it saw moved)
    # rather than the unit's zero-weight re-buy, and the grace counts are
    # untouched — the clock under test here.
    spent.append(RouteLedgerRow(
        kind=RouteKind.IMPL, label="impl src/x.rs", targets=((_FMT.file, _FMT.fn_name),),
        bodies_sha="i", oracle_fp="o", outcome=RunOutcome.CASHED, net_pass_delta=1))
    assert unpaid_attributed_writes(spent, "t_a", {_FMT}, "o") == DEFAULT_UNPAID_GRACE

    inside = schedule(runs, spent[1:], oracle_fp="o", hollow={},
                      attributed={_FMT}, budget_minutes=120)
    assert [s.run.label for s in inside.ordered] == [file_run.label]
    assert [(s.run.label, s.reason) for s in inside.deferred] == [
        (split.label, "coverage_split")]

    sched = schedule(runs, spent, oracle_fp="o", hollow={},
                     attributed={_FMT}, budget_minutes=120)
    assert [s.run.label for s in sched.ordered] == [file_run.label, split.label]
    opened = sched.ordered[1]
    assert opened.lapsed == ("coverage_split",)
    assert opened.share_minutes and opened.share_minutes > 0
    assert "grace spent: coverage_split" in sched.render()
    assert not sched.deferred

    off = schedule(runs, spent * 5, oracle_fp="o", hollow={},
                   attributed={_FMT}, budget_minutes=120, unpaid_grace=0)
    assert [(s.run.label, s.reason) for s in off.deferred] == [
        (split.label, "coverage_split")]


def test_split_rows_are_not_the_attributed_writes_the_grace_counts() -> None:
    """The count is over writes that are NOT splits against today's
    attributed set: a split that ran (it lapsed earlier, or ran before the
    rule) is not the attributed write failing, so it neither spends nor
    restarts the attributed write's grace."""
    file_run, split = _split_inputs()
    rows = [_row(file_run, RunOutcome.FLAT),
            _row(split, RunOutcome.FLAT), _row(split, RunOutcome.FLAT),
            _row(file_run, RunOutcome.FLAT)]
    assert unpaid_attributed_writes(rows, "t_a", {_FMT}, "o") == 2
    rows.insert(2, replace(_row(split, RunOutcome.CASHED, 1),
                           newly_green=("t_a",)))
    assert unpaid_attributed_writes(rows, "t_a", {_FMT}, "o") == 2


def test_a_split_sharing_two_leftovers_waits_while_either_is_unspent() -> None:
    file_run = _file("file src/x.rs", {_FMT}, set(), {"t_a", "t_b"})
    split = RouteRun(kind=RouteKind.PER_FN, label="per-fn", bodies_sha="p",
                     targets=frozenset({_OTHER}),
                     target_tests=frozenset({"t_a", "t_b"}))
    rows = [replace(_row(file_run, RunOutcome.CASHED, 1), newly_green=("t_b",))]
    rows += [_row(replace(file_run, bodies_sha=f"b{i}"), RunOutcome.FLAT)
             for i in range(DEFAULT_UNPAID_GRACE - 1)]
    assert unpaid_attributed_writes(rows, "t_a", {_FMT}, "o") == DEFAULT_UNPAID_GRACE
    assert unpaid_attributed_writes(rows, "t_b", {_FMT}, "o") == DEFAULT_UNPAID_GRACE - 1
    sched = schedule([file_run, split], rows, oracle_fp="o", hollow={},
                     attributed={_FMT}, budget_minutes=60)
    assert [s.reason for s in sched.deferred] == ["coverage_split"]


def test_a_pair_that_paid_a_sibling_does_not_keep_zeroing_the_other_tests_runs() -> None:
    """Plan sentence: a pair's pair_wait over a run that shares its
    leftover tests lapses when every shared test has spent the grace on
    the pair's own tries, even if the pair cashed on another test since —
    the route-level count restarted at that cash, the leftover's did not.

    Negative controls: the same record with the cashing row carrying no
    green set (the pre-field ledger) keeps the run at pair_wait; and a run
    of the test the pair DID pay still waits."""
    pair = _pair("pair src/x.rs::fmt + src/y.rs::new", {_FMT, _NEW},
                 {"t_a", "t_b"})
    b_only = _session("session[1 test(s)] b", {"t_b"}, {_OTHER}, seq=1)
    a_only = _session("session[1 test(s)] a", {"t_a"}, {_OTHER}, seq=2)
    # Each graded try kept or moved a body, so each row sits on its own
    # identity and the pair ranks served, as the live pair does.
    cash = replace(_row(replace(pair, bodies_sha="cash"), RunOutcome.CASHED, 1),
                   newly_green=("t_a",))
    # The last try reverted (not flat): inside the grace a reverted pair is
    # a retry with a diagnosis, `served`; a flat one is its units' re-buy.
    rows = [_row(replace(pair, bodies_sha=f"b{i}"), RunOutcome.REVERTED)
            for i in range(DEFAULT_UNPAID_GRACE)] + [
        cash, _row(replace(pair, bodies_sha="flat"), RunOutcome.REVERTED)]
    assert consecutive_unpaid(rows, pair, "o") == 1 < DEFAULT_UNPAID_GRACE
    sched = schedule([pair, b_only, a_only], rows, oracle_fp="o", hollow={},
                     budget_minutes=120)
    # The spent grace over `t_b` also releases the order: `b_only` is ranked
    # against the pair by the ledger (never served vs served) and opens
    # first; the pair still holds `a_only`, whose test it has not spent on.
    assert [s.run.label for s in sched.ordered] == [b_only.label, pair.label]
    assert [(s.run.label, s.reason) for s in sched.deferred] == [
        (a_only.label, "pair_wait")]

    legacy = [*rows[:-2], replace(cash, newly_green=None), rows[-1]]
    control = schedule([pair, b_only, a_only], legacy, oracle_fp="o",
                       hollow={}, budget_minutes=120)
    assert [s.run.label for s in control.ordered] == [pair.label]
    assert {s.reason for s in control.deferred} == {"pair_wait"}


# --- a deferral is only ever a wait for a run that opens -------------------
#
# Invented shape: one dispatcher `draw` reached by every test, two
# cross-file pairs through it (`draw + outline`, `draw + shade`), and a
# per-fn holding a stub (`area`) both tests also execute. Every unit is a
# stub, so every test is hollow on stubs held by more than one run — the
# fresh-tree regime. The pre-fix rule deferred every run a pair blocks,
# whether or not that pair itself opened.

_DRAW = FnLocator("src/shape.rs", "draw")
_OUTLINE = FnLocator("src/paint.rs", "outline")
_SHADE = FnLocator("src/paint.rs", "shade")
_AREA = FnLocator("src/shape.rs", "area")


def _fresh_tree() -> tuple[list[RouteRun], dict[str, frozenset[FnLocator]]]:
    slices = {
        "t_outline": {FnSite("src/shape.rs", "draw", ""),
                      FnSite("src/paint.rs", "outline", ""),
                      FnSite("src/shape.rs", "area", "")},
        "t_shade": {FnSite("src/shape.rs", "draw", ""),
                    FnSite("src/paint.rs", "shade", ""),
                    FnSite("src/shape.rs", "area", "")},
    }
    hollow = hollow_by_test(slices, {_DRAW, _OUTLINE, _SHADE, _AREA})
    tests = frozenset(slices)
    pair_o = RouteRun(kind=RouteKind.PAIR, label="pair draw + outline",
                      targets=frozenset({_DRAW, _OUTLINE}),
                      target_tests=tests,
                      stubs_in_targets=frozenset({_DRAW, _OUTLINE}),
                      files=("src/shape.rs", "src/paint.rs"), seq=1,
                      bodies_sha="po")
    pair_s = RouteRun(kind=RouteKind.PAIR, label="pair draw + shade",
                      targets=frozenset({_DRAW, _SHADE}),
                      target_tests=tests,
                      stubs_in_targets=frozenset({_DRAW, _SHADE}),
                      files=("src/shape.rs", "src/paint.rs"), seq=2,
                      bodies_sha="ps")
    per_fn = RouteRun(kind=RouteKind.PER_FN, label="per-fn area",
                      targets=frozenset({_AREA}), target_tests=tests,
                      stubs_in_targets=frozenset({_AREA}), seq=3,
                      bodies_sha="pf")
    return [pair_o, pair_s, per_fn], hollow


def _pre_fix_pair_wait(runs: list[RouteRun],
                       hollow: dict[str, frozenset[FnLocator]]) -> list[str]:
    """The deferral before 2026-09-18, with no ledger (every pair inside its
    grace): a run is deferred when ANY candidate pair blocks it, opened or
    not."""
    pairs = [r for r in runs if r.kind is RouteKind.PAIR]
    return [r.label for r in runs
            if any(run_blocks(p, r, hollow) for p in pairs)]


def test_two_pairs_through_one_dispatcher_on_a_stub_tree_all_open() -> None:
    """Plan sentence: a run is held only by a run that opens, and a pair
    does not hold a run its own tests are hollow on. On the fresh-tree
    shape every candidate opens. Control: the pre-fix rule defers all
    three, so the fill opens nothing and no grace clock can ever run
    (a deferred run writes no graded row)."""
    runs, hollow = _fresh_tree()
    pair_o, pair_s, per_fn = runs
    # The shape IS the cycle: each pair blocks the other and the per-fn,
    # and the per-fn blocks both pairs.
    assert run_blocks(pair_o, pair_s, hollow) and run_blocks(pair_s, pair_o, hollow)
    assert run_blocks(pair_o, per_fn, hollow) and run_blocks(per_fn, pair_o, hollow)
    assert sorted(_pre_fix_pair_wait(runs, hollow)) == sorted(
        r.label for r in runs)
    for budget in (0, 200):
        sched = schedule(runs, [], oracle_fp="o", hollow=hollow,
                         budget_minutes=budget)
        assert not sched.deferred
        assert {s.run.label for s in sched.ordered} == {
            r.label for r in runs}
        released = [s for s in sched.ordered if s.mutual]
        assert released and all(not s.lapsed for s in released)
        assert "not held, the pair waits on it" in sched.render()


def test_the_endgame_pair_still_holds_a_run_its_tests_do_not_need() -> None:
    """The same pair on a tree whose leftovers are hollow on nothing (the
    endgame the pair_wait instances were measured on) still defers a
    stub-free file of its members. Control for the test above: the mutual
    release is keyed on the hollow relation, not on the pair's shape."""
    runs, _hollow_map = _fresh_tree()
    pair_o = replace(runs[0], stubs_in_targets=frozenset())
    file_run = RouteRun(kind=RouteKind.FILE, label="file src/paint.rs",
                        targets=frozenset({_OUTLINE, _SHADE}),
                        target_tests=pair_o.target_tests,
                        files=("src/paint.rs",), seq=4, bodies_sha="fp")
    sched = schedule([pair_o, file_run], [], oracle_fp="o", hollow={})
    assert [s.run.label for s in sched.ordered] == [pair_o.label]
    assert [(s.run.label, s.reason) for s in sched.deferred] == [
        (file_run.label, "pair_wait")]


def test_a_pair_the_kind_cap_defers_holds_nothing() -> None:
    """A holder that does not open holds nothing: with one pair per fill,
    the second pair is capped and the file only it blocks opens. Control:
    the pre-fix order (pair_wait decided before the cap) deferred the file
    behind a pair that never ran."""
    runs, _h = _fresh_tree()
    pair_o = replace(runs[0], stubs_in_targets=frozenset(),
                     target_tests=frozenset({"t_outline"}))
    pair_s = replace(runs[1], stubs_in_targets=frozenset(),
                     target_tests=frozenset({"t_shade"}),
                     targets=frozenset({_SHADE, FnLocator("src/ink.rs", "tone")}))
    file_run = RouteRun(kind=RouteKind.FILE, label="file src/ink.rs",
                        targets=frozenset({FnLocator("src/ink.rs", "tone")}),
                        target_tests=frozenset({"t_shade"}),
                        files=("src/ink.rs",), seq=5, bodies_sha="fi")
    assert run_blocks(pair_s, file_run, {})
    assert not run_blocks(pair_o, file_run, {})
    assert _pre_fix_pair_wait([pair_o, pair_s, file_run], {}) == [
        file_run.label]
    sched = schedule([pair_o, pair_s, file_run], [], oracle_fp="o",
                     hollow={}, max_per_kind={RouteKind.PAIR: 1})
    assert {s.run.label for s in sched.ordered} == {
        pair_o.label, file_run.label}
    assert [(s.run.label, s.reason) for s in sched.deferred] == [
        (pair_s.label, "kind_cap")]


def test_a_fill_with_candidates_always_opens_one() -> None:
    """The progress guarantee, over generated shapes: whatever the blocking
    relation among pairs, stub holders and coverage runs, a schedule with at
    least one candidate opens at least one run (the first in order is held
    by nothing, since nothing opened before it). Control: the pre-fix rule
    opened nothing on some of the same shapes."""
    import random
    rng = random.Random(20260918)
    files = ["src/a.rs", "src/b.rs", "src/c.rs"]
    units = [FnLocator(f, n) for f in files for n in ("p", "q", "r")]
    kinds = [RouteKind.PAIR, RouteKind.FILE, RouteKind.IMPL,
             RouteKind.PER_FN, RouteKind.SESSION]
    pre_fix_empty = 0
    for i in range(400):
        tests = [f"t{j}" for j in range(rng.randint(1, 4))]
        stubs = set(rng.sample(units, rng.randint(0, len(units))))
        slices = {t: {FnSite(u.file, u.fn_name, "")
                      for u in rng.sample(units, rng.randint(1, 5))}
                  for t in tests}
        hollow = hollow_by_test(slices, stubs)
        runs = []
        for seq in range(rng.randint(1, 7)):
            tg = frozenset(rng.sample(units, rng.randint(1, 3)))
            runs.append(RouteRun(
                kind=rng.choice(kinds), label=f"run{i}.{seq}", targets=tg,
                target_tests=frozenset(rng.sample(tests, rng.randint(1, len(tests)))),
                stubs_in_targets=tg & frozenset(stubs), seq=seq,
                files=tuple(sorted({u.file for u in tg})), bodies_sha=f"b{seq}"))
        sched = schedule(runs, [], oracle_fp="o", hollow=hollow)
        assert sched.ordered, (runs, hollow)
        if len(_pre_fix_pair_wait(runs, hollow)) == len(runs):
            pre_fix_empty += 1
    assert pre_fix_empty > 0


def test_recorded_220812_fill_1_opens_the_run_holding_the_crash_evidenced_stub_first() -> None:
    """Plan sentence: among runs that all fill a blocking stub, the one
    writing the locator the sweep's crash evidence names opens first. Run
    `220812-8d346f9c` fill 1 (2026-09-23): `src/error.rs::construct` was the
    first panic of 128 of 141 failing tests and the per-fn run holding it
    opened at slot 8 of 9. Control: the same fill with the two tiers merged
    (no crash evidence to split on) reproduces the live order."""
    import json
    from pathlib import Path
    from types import SimpleNamespace

    from pipeline_core.domain.fill_schedule import (
        RouteLedgerRow,
        unit_evidence_from,
    )

    fix = json.loads((Path(__file__).resolve().parent / "fixtures" / "fills"
                      / "220812-8d346f9c_fill1.json").read_text())

    def loc(s: str) -> FnLocator:
        f, fn = s.split("::", 1)
        return FnLocator(f, fn)

    runs = [RouteRun(
        kind=RouteKind(r["kind"]), label=r["label"],
        targets=frozenset(loc(x) for x in r["targets"]),
        target_tests=frozenset(r["target_tests"]),
        stubs_in_targets=frozenset(loc(x) for x in r["stubs"]),
        files=tuple(r["files"]), seq=r["seq"], bodies_sha=r["bodies_sha"])
        for r in fix["runs"]]
    rows = [r for r in (RouteLedgerRow.from_dict(x) for x in fix["ledger"])
            if r is not None]
    # Live, every run sat at level 0: each held a stub a hollow test runs
    # through. One hollow test per stub restores that.
    hollow = {f"t_{s}": frozenset({s}) for r in runs
              for s in r.stubs_in_targets}
    body = [SimpleNamespace(key=SimpleNamespace(file=b["file"], fn_name=b["fn"]),
                            run_id=b["run_id"], iteration=b["iteration"],
                            body_sha=b["body_sha"], state=b["state"],
                            oracle_fp=b["oracle_fp"])
            for b in fix["body_ledger"]]
    kw = {"oracle_fp": fix["oracle_fp"], "hollow": hollow,
          "budget_minutes": fix["budget_minutes"],
          "unit_evidence": unit_evidence_from(body, {},
                                              oracle_fp=fix["oracle_fp"])}
    sched = schedule(runs, rows, crash_blamed=frozenset(
        loc(x) for x in fix["crash_blamed"]), **kw)
    first = sched.ordered[0]
    assert first.run.kind is RouteKind.PER_FN and first.level == 0
    assert FnLocator("src/error.rs", "construct") in first.run.targets
    control = schedule(runs, rows, **kw)
    assert [s.run.label for s in control.ordered] == fix["live_order"]
