"""A cash pays for the tests it turned green, not for the ones it left red.

Run `2355041-eae39441` (2026-10-27), fill 8: the Display pair ranked
`paying` (double share) after its fill-7 try cashed one test; the four it
then aimed at were the four that try had run and left red. It re-emitted its
bodies for 28.9 minutes; a session cashed the one of them it could move by
rewriting a unit outside the pair. Over the route ledger 11 `paying` runs
had that shape: 1 cashed, 10 bought nothing (257 of 270 minutes).

Claims: a paying try whose every aimed test the last try ran and left red
(the row's `still_red`) ranks as the flat re-buy, on the route-row path and
on the unit-evidence path. Controls: a row without the reading (written
before the field) still pays; an aimed test the last try did not measure
keeps it paying. Invented shape.
"""
from __future__ import annotations

from pipeline_core.domain.fill_schedule import (
    RouteKind,
    RouteLedgerRow,
    RouteRun,
    RunOutcome,
    UnitEvidence,
    aim_left_red,
    schedule,
)
from pipeline_core.domain.models import FnLocator

_D = FnLocator("src/a.rs", "dispatch")
_I = FnLocator("src/b.rs", "implement")
_OTHER = RouteRun(kind=RouteKind.FILE, label="file src/q.rs",
                  targets=frozenset({FnLocator("src/q.rs", "e")}),
                  target_tests=frozenset({"t_q"}), bodies_sha="new")


def _pair(tests: frozenset[str]) -> RouteRun:
    return RouteRun(kind=RouteKind.PAIR, label="pair src/a.rs::dispatch + src/b.rs::implement",
                    targets=frozenset({_D, _I}), target_tests=tests,
                    files=("src/a.rs", "src/b.rs"), bodies_sha="after")


def _cashed(still: tuple[str, ...] | None) -> RouteLedgerRow:
    return RouteLedgerRow(
        kind=RouteKind.PAIR, label="pair src/a.rs::dispatch + src/b.rs::implement",
        targets=(("src/a.rs", "dispatch"), ("src/b.rs", "implement")),
        bodies_sha="before", oracle_fp="o", outcome=RunOutcome.CASHED,
        net_pass_delta=1, kept=(("src/a.rs", "dispatch"), ("src/b.rs", "implement")),
        run_id="r", iteration=7, started="2026-10-27T13:00",
        target_tests=("t_paid", "t_left_1", "t_left_2"),
        newly_green=("t_paid",), still_red=still, model_calls=1, minutes=11.4)


def _reason(run: RouteRun, row: RouteLedgerRow, ev: UnitEvidence | None) -> str:
    sched = schedule([run, _OTHER], [row], oracle_fp="o", hollow={}, unit_evidence=ev)
    return next(s.reason for s in (*sched.ordered, *sched.deferred) if s.run.kind is RouteKind.PAIR)


def _ev(compared: bool = True) -> UnitEvidence:
    keys = {("r", 7, u): ("s", "proven") for u in (_D, _I)}
    return UnitEvidence(grades=keys, compared=frozenset(keys) if compared else frozenset())


def test_a_rerun_on_the_tests_its_cash_left_red_is_a_re_buy() -> None:
    left = frozenset({"t_left_1", "t_left_2"})
    row = _cashed(("t_left_1", "t_left_2"))
    assert aim_left_red(_pair(left), row)
    assert _reason(_pair(left), row, _ev()).startswith("rebuy")
    assert _reason(_pair(left), row, None).startswith("rebuy")
    # Control: the row written before the reading existed still pays.
    assert _reason(_pair(left), _cashed(None), _ev()) == "paying"
    # Control: a test the try did not measure is a new question.
    wider = left | {"t_unmeasured"}
    assert not aim_left_red(_pair(wider), row)
    assert _reason(_pair(wider), row, _ev()) == "paying"


def test_the_comparison_and_the_keep_record_name_what_stayed_red() -> None:
    """`still_failing` is the tests failing in both readings of a binary that
    completed in both; the keep record carries it to the row, less a test a
    later keep of the run turned green."""
    from pipeline_core.application.reconstruct import fill_reading
    from pipeline_core.domain.test_results import (
        STDERR_MARK,
        TestStateReading,
        compare_readings,
        parse_binary_results,
    )

    def reading(stdout: str) -> TestStateReading:
        return TestStateReading(parse_binary_results(
            stdout + f"\n{STDERR_MARK}\n     Running tests/h.rs (target/debug/deps/h-aaaa)\n"))

    before = reading("running 3 tests\ntest a ... FAILED\ntest b ... FAILED\ntest c ... ok\n"
                     "test result: FAILED. 1 passed; 2 failed; 0 ignored\n")
    after = reading("running 3 tests\ntest a ... ok\ntest b ... FAILED\ntest c ... ok\n"
                    "test result: FAILED. 2 passed; 1 failed; 0 ignored\n")
    cmp = compare_readings(before, after)
    assert {n.rsplit("::", 1)[-1] for n in cmp.still_failing} == {"b"}
    assert {n.rsplit("::", 1)[-1] for n in cmp.newly_green} == {"a"}
    fill_reading.reset_last_comparison()
    mark = fill_reading.kept_mark()
    fill_reading.note_kept_comparison(cmp)
    fill_reading.note_kept(0, newly_green=("b",), still_failing=("b",))
    *_rest, still = fill_reading.kept_since(mark)
    assert still == ()
    omitted = fill_reading.kept_mark()
    fill_reading.note_kept(4, newly_green=("paid",))
    *_rest, blank = fill_reading.kept_since(omitted)
    assert blank is None
    named = fill_reading.kept_mark()
    fill_reading.note_kept(4, newly_green=("paid",), still_failing=("left",))
    *_rest, left = fill_reading.kept_since(named)
    assert left == ("left",)
    sess = RouteRun(kind=RouteKind.SESSION, label="session",
                    targets=frozenset({_D}), target_tests=frozenset({"left"}))
    row = RouteLedgerRow(
        kind=RouteKind.SESSION, label="session",
        targets=(("src/a.rs", "dispatch"),), bodies_sha="before", oracle_fp="o",
        outcome=RunOutcome.CASHED, net_pass_delta=1,
        kept=(("src/a.rs", "dispatch"),), run_id="r", iteration=8,
        started="2026-10-27T13:00", target_tests=("paid", "left"),
        newly_green=("paid",), still_red=("left",), model_calls=1, minutes=7.4)
    assert aim_left_red(sess, row)
