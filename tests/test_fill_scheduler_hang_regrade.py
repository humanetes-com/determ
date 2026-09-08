"""The executor writes a run's row when the run ends and re-grades a run's
aim on the fill's own keeps before it opens.

Run `3500465-67c40e78` (2026-09-08): eight route runs completed in one
fill and the ledger held no row for any of them — rows were appended once,
at the end of the fill, and the process was killed inside the ninth run.
The same fill's per-fn run was graded `never_served` at plan time and
never re-graded after a session keep crashed a binary at 15:32.

Sentences pinned here:
- a wave's rows reach the ledger before the next wave opens;
- a run whose every aimed test is in a binary a keep in THIS fill made
  incomplete is re-graded `rebuy_flat` at open and skipped, budget 0
  included; a run with one aimed test in a completing binary still opens
  (the negative control).
"""
from __future__ import annotations

from collections.abc import Sequence

from pipeline_core.application.reconstruct import fill_reading
from pipeline_core.application.reconstruct.fill_routes import RouteRunReport
from pipeline_core.application.reconstruct.fill_scheduler import (
    FillExecutionDeps,
    execute_fill,
)
from pipeline_core.domain.fill_schedule import (
    RouteKind,
    RouteRun,
    RunClock,
    RunOutcome,
)
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.test_results import (
    RegressionComparison,
    TestStateReading,
    parse_binary_results,
)

_A = FnLocator("src/a.rs", "f")
_B = FnLocator("src/b.rs", "g")

_SUITE = """
     Running tests/bin_x.rs (target/debug/deps/bin_x-0123456789abcdef)

running 2 tests
test t_x1 ... FAILED
test t_x2 ... FAILED

test result: FAILED. 0 passed; 2 failed; 0 ignored

     Running tests/bin_ok.rs (target/debug/deps/bin_ok-0123456789abcdef)

running 1 test
test t_ok ... FAILED

test result: FAILED. 0 passed; 1 failed; 0 ignored
"""


class _Ledger:
    def __init__(self) -> None:
        self.batches: list[int] = []

    def append(self, project: str, rows: Sequence[object]) -> int:
        self.batches.append(len(rows))
        return len(rows)

    def load(self, project: str, cluster: str) -> list[object]:
        return []


class _Route:
    """A fake route; `on_run` runs inside `run` (a keep, an assertion)."""

    def __init__(self, kind: RouteKind, runs: Sequence[RouteRun], *,
                 calls: list[str], on_run=None) -> None:
        self.kind = kind
        self.parallel = False
        self._runs = list(runs)
        self._calls = calls
        self._on_run = on_run

    def plan(self) -> Sequence[RouteRun]:
        return list(self._runs)

    def run(self, run: RouteRun, clock: RunClock) -> RouteRunReport:
        self._calls.append(run.label)
        if self._on_run is not None:
            self._on_run()
        return RouteRunReport(model_calls=1, kept=run.targets)


def _deps(ledger: _Ledger, log: list[str]) -> FillExecutionDeps:
    return FillExecutionDeps(
        log=log.append, emit=lambda *a, **k: None, check_stop=lambda _c: None,
        ledger=ledger, project="p", cluster="c", run_id="r", iteration=1,
        oracle_fp="o", budget_minutes=0.0, scheduled=True, parallelism=1)


def _keep_crash_of_bin_x() -> None:
    reading = TestStateReading(parse_binary_results(_SUITE))
    fill_reading.note_last_reading(reading)
    label = next(b.label for b in reading.binaries if "bin_x" in b.label)
    fill_reading.note_kept_comparison(RegressionComparison(
        newly_red=frozenset(), newly_crashed=(), excluded_crashed=(),
        not_run=(), measured=(), base_pass=0, cur_pass=0,
        base_executed=3, cur_executed=1, crashed_all_red=(label,)))


def _session(on_run=None, calls=None) -> _Route:
    return _Route(RouteKind.SESSION, [RouteRun(
        kind=RouteKind.SESSION, label="session[1 test(s)]",
        targets=frozenset({_A}), target_tests=frozenset({"t_a"}))],
        calls=calls if calls is not None else [], on_run=on_run)


def test_rows_reach_the_ledger_wave_by_wave() -> None:
    fill_reading.reset_last_comparison()
    ledger = _Ledger()
    calls: list[str] = []
    seen_at_open: list[list[int]] = []
    per_fn = _Route(RouteKind.PER_FN, [RouteRun(
        kind=RouteKind.PER_FN, label="per-fn", targets=frozenset({_B}),
        target_tests=frozenset({"t_ok"}))],
        calls=calls, on_run=lambda: seen_at_open.append(list(ledger.batches)))
    out = execute_fill([_session(calls=calls), per_fn], hollow={},
                       bodies_sha=lambda _t: "b", deps=_deps(ledger, []))
    assert calls == ["session[1 test(s)]", "per-fn"]
    # The session's row was on disk when the per-fn run opened.
    assert seen_at_open == [[1]]
    assert ledger.batches == [1, 1]
    assert [r.outcome for r in out.rows] == [RunOutcome.FLAT, RunOutcome.FLAT]


def test_a_run_aimed_only_at_a_binary_a_keep_crashed_is_skipped_at_open() -> None:
    fill_reading.reset_last_comparison()
    ledger = _Ledger()
    calls: list[str] = []
    log: list[str] = []
    per_fn = _Route(RouteKind.PER_FN, [RouteRun(
        kind=RouteKind.PER_FN, label="per-fn", targets=frozenset({_B}),
        target_tests=frozenset({"t_x1", "t_x2"}))], calls=calls)
    out = execute_fill([_session(on_run=_keep_crash_of_bin_x, calls=calls), per_fn],
                       hollow={}, bodies_sha=lambda _t: "b",
                       deps=_deps(ledger, log))
    assert calls == ["session[1 test(s)]"]
    row = next(r for r in out.rows if r.label == "per-fn")
    assert row.outcome is RunOutcome.DEFERRED and row.reason == "rebuy_flat"
    assert any("per-fn: re-graded rebuy_flat at open" in ln for ln in log)
    assert ledger.batches == [1, 1]


def test_a_run_with_one_measurable_aimed_test_still_opens() -> None:
    fill_reading.reset_last_comparison()
    calls: list[str] = []
    per_fn = _Route(RouteKind.PER_FN, [RouteRun(
        kind=RouteKind.PER_FN, label="per-fn", targets=frozenset({_B}),
        target_tests=frozenset({"t_x1", "t_ok"}))], calls=calls)
    out = execute_fill([_session(on_run=_keep_crash_of_bin_x, calls=calls), per_fn],
                       hollow={}, bodies_sha=lambda _t: "b",
                       deps=_deps(_Ledger(), []))
    assert calls == ["session[1 test(s)]", "per-fn"]
    assert all(r.outcome is RunOutcome.FLAT for r in out.rows)
