"""The file-first route plans only files it can OPEN under the fn bound.

The bound (`PHASE_C_FILE_MAX_FNS`) used to be read only when a run opened,
inside the fill: the schedule had already ranked the run on the ledger and
the route returned EMPTY at 0.0 min. `_rank_of` skips EMPTY rows by contract,
so the same identity ranked never-served again on the NEXT fill — on the
nightly tree of run `2131920-86818224` (2026-09-07) every schedule line
opened with `1. file src/error.rs (never_served; 14 stub(s))` for a file of
21 fns with no test-welded group under the bound. The plan's sentence: a file
the route cannot open is not a run. The negative control is the pre-fix
plan — every file unit becomes a run, and the declined one's run is EMPTY.
"""
from __future__ import annotations

from pipeline_core.application.reconstruct.fill_routes import RouteRunReport
from pipeline_core.application.reconstruct.impl_fill import _FileRoute
from pipeline_core.domain.fill_schedule import RouteKind, RunClock
from pipeline_core.domain.models import FnLocator, FnRecord, ImplBlock


def _block(file: str, *names: str) -> ImplBlock:
    recs = tuple(FnRecord(file=file, fn_name=n, container="impl T",
                          sig_text=f"fn {n}()") for n in names)
    return ImplBlock(file=file, container="impl T", members=recs, stub_members=recs)


_UNITS = [("src/big.rs", [_block("src/big.rs", "a", "b", "c")]),
          ("src/small.rs", [_block("src/small.rs", "d")])]


def _route(admits, *, filled: list[str], logs: list[str]) -> _FileRoute:
    def _fill(file_rel: str, _blocks, **_kw) -> None:
        # The pre-fix run-time decline: the bound refuses inside the fill.
        if file_rel == "src/big.rs":
            return
        filled.append(file_rel)

    return _FileRoute(
        units_now=lambda: list(_UNITS), fill_file=_fill, admits=admits,
        snapshot=lambda: None,  # type: ignore[arg-type,return-value]
        report_since=lambda _s: RouteRunReport(),
        tests_for=lambda _locs: frozenset(), stubs=frozenset(),
        on_plan=lambda _u: None, log=logs.append)


def test_a_file_the_bound_refuses_is_not_planned_as_a_run() -> None:
    logs: list[str] = []
    filled: list[str] = []
    over = {"src/big.rs"}
    route = _route(lambda f, _bs: f not in over, filled=filled, logs=logs)
    runs = route.plan()
    assert [r.label for r in runs] == ["file src/small.rs"]
    assert all(r.kind is RouteKind.FILE for r in runs)
    assert runs[0].targets == frozenset({FnLocator("src/small.rs", "d")})


def test_negative_control_the_prefix_plan_schedules_a_run_that_can_only_be_empty() -> None:
    """Every unit a run, the bound read only when it opens: the declined
    file's run is planned first and returns EMPTY without a call — the row
    the schedule then skips, and the identity it ranks never-served again."""
    logs: list[str] = []
    filled: list[str] = []
    route = _route(lambda _f, _bs: True, filled=filled, logs=logs)
    runs = route.plan()
    assert [r.label for r in runs] == ["file src/big.rs", "file src/small.rs"]
    clock = RunClock(share_minutes=None, fill_remaining=lambda: None)
    report = route.run(runs[0], clock)
    assert filled == [] and not report.empty, (
        "the run OPENED (snapshot taken, fill entered) and the fill declined "
        "inside — the shape the plan-time bound removes")
