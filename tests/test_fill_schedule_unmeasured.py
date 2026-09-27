"""The schedule's unmeasurable demotion reads an isolated capture.

A run aimed only at tests whose own verdict was never read, holding no
crash-blamed unit, ranks as a flat re-buy and is deferred under a budget.
"Never read" used to mean "no binary verdict behind the failure", which an
isolated capture never produces: every aborted binary is re-run one test per
process and rebuilt complete. From the day isolation shipped (2026-09-19) to
run `118933-877c43bd` (2026-09-27) the set was non-empty in 1 of ~160 fills.
That run's fill 1 aimed ten runs holding no member of the three-function
recursion at 13 stack-overflow aborts, and they shared the budget with the
seven runs that held it. Replayed on the recorded schedule inputs with the
set computed from a real isolated capture of that tree: 17 opened / 1
deferred before, 7 opened / 11 deferred after, every opened run holding a
member.
"""
from __future__ import annotations

from pipeline_core.domain.fill_schedule import RouteKind, RouteRun, schedule
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.oracle_ledger import bare_test_name
from pipeline_core.domain.test_isolation import (
    read_single_test_run,
    render_isolated_section,
)
from pipeline_core.domain.test_results import (
    parse_binary_results,
    parse_failed_tests,
    unmeasured_failures,
)

MEMBER = FnLocator("src/ring.rs", "ping")
BYSTANDER = FnLocator("src/view.rs", "render")


def _capture() -> str:
    """One binary, one test asserting normally, one aborting on a stack
    overflow — isolated, as `run_cargo` hands it to every reader."""
    aborted = read_single_test_run(
        "t_overflow", -6, "running 1 test\ntest t_overflow ... ",
        stderr=("thread 't_overflow' has overflowed its stack\n"
                "fatal runtime error: stack overflow, aborting\n"),
        timed_out=False, timeout=120)
    asserted = read_single_test_run(
        "t_assert", 101,
        "running 1 test\ntest t_assert ... FAILED\n\nfailures:\n\n"
        "---- t_assert stdout ----\n\nthread 't_assert' panicked at "
        "tests/ring.rs:9:5:\nassertion `left == right` failed\n\n"
        "failures:\n    t_assert\n\ntest result: FAILED. 0 passed; 1 failed; "
        "0 ignored; 0 measured; 0 filtered out\n",
        timed_out=False, timeout=120)
    return ("    Running tests/ring.rs (target/debug/deps/ring-abc)\n"
            + render_isolated_section([aborted, asserted]))


def _pre_fix_unmeasurable(out: str) -> frozenset[str]:
    # The rule the loop applied before: failed names with no binary verdict.
    verdicted = {bare_test_name(n) for b in parse_binary_results(out)
                 for n in b.failed}
    return frozenset(bare_test_name(t) for t in parse_failed_tests(out)
                     if bare_test_name(t) not in verdicted)


def test_an_isolated_abort_is_unmeasured_and_an_assertion_is_not() -> None:
    out = _capture()
    got = unmeasured_failures(parse_binary_results(out), parse_failed_tests(out))
    assert got == {"t_overflow"}


def test_the_pre_fix_rule_reads_the_isolated_abort_as_measured() -> None:
    # Negative control: the same capture under the old rule is empty.
    assert _pre_fix_unmeasurable(_capture()) == frozenset()


def _runs() -> list[RouteRun]:
    return [
        RouteRun(RouteKind.FILE, "file src/view.rs", frozenset({BYSTANDER}),
                 target_tests=frozenset({"t_overflow"}), seq=0),
        RouteRun(RouteKind.FILE, "file src/ring.rs", frozenset({MEMBER}),
                 target_tests=frozenset({"t_overflow"}), seq=1),
    ]


def test_a_budget_opens_only_the_run_holding_the_crash_culprit() -> None:
    out = _capture()
    unmeas = unmeasured_failures(parse_binary_results(out),
                                 parse_failed_tests(out))
    s = schedule(_runs(), [], oracle_fp="o", hollow={}, budget_minutes=60,
                 unmeasurable_tests=unmeas, crash_blamed=frozenset({MEMBER}))
    assert [r.run.label for r in s.ordered] == ["file src/ring.rs"]
    assert [(r.run.label, r.reason) for r in s.deferred] == [
        ("file src/view.rs", "rebuy_flat")]


def test_under_the_pre_fix_set_both_runs_share_the_budget() -> None:
    s = schedule(_runs(), [], oracle_fp="o", hollow={}, budget_minutes=60,
                 unmeasurable_tests=_pre_fix_unmeasurable(_capture()),
                 crash_blamed=frozenset({MEMBER}))
    assert sorted(r.run.label for r in s.ordered) == [
        "file src/ring.rs", "file src/view.rs"]
