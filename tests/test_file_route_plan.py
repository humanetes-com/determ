"""The file-first route plans only files it can OPEN under the fn bound.

The bound (`PHASE_C_FILE_MAX_FNS`) used to be read only when a run opened,
inside the fill: the schedule had already ranked the run on the ledger and
the route returned EMPTY at 0.0 min. `_rank_of` skips EMPTY rows by contract,
so the same identity ranked never-served again on the NEXT fill — on the
nightly tree of run `2131920-86818224` (2026-09-07) every schedule line
opened with `1. file src/error.rs (never_served; 14 stub(s))` for a file of
21 fns with no test-welded group under the bound. The plan's sentence: a file
the route cannot open is not a run. The negative control is the pre-fix
plan — every file unit becomes a run, and the declined one's run opens and
lands nothing.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct.fill_routes import RouteRunReport
from pipeline_core.application.reconstruct.impl_fill import (
    _file_fill_plan,
    _file_route_admits,
    _FileRoute,
)
from pipeline_core.domain.attribution import FailureFrontier
from pipeline_core.domain.fill_schedule import RouteKind, RunClock
from pipeline_core.domain.models import FnLocator, FnRecord, ImplBlock

# Recorded shape of nightly `src/error.rs` on run `2131920-86818224`: 21
# fns against `PHASE_C_FILE_MAX_FNS=20`, no convicting overlap so nothing
# welds under the bound. Names are the count, not the crate's leftovers.
_OVER_BOUND = "src/error.rs"
_FITS = "src/small.rs"
_OVER_NAMES = tuple(f"f{i:02d}" for i in range(21))
_NO_CONVICTING = FailureFrontier.of_dispatch({})
_PER_IMPL_MAX = 12


def _block(file: str, *names: str) -> ImplBlock:
    recs = tuple(FnRecord(file=file, fn_name=n, container="impl T",
                          sig_text=f"fn {n}()") for n in names)
    return ImplBlock(file=file, container="impl T", members=recs, stub_members=recs)


def _units() -> list[tuple[str, list[ImplBlock]]]:
    return [(_OVER_BOUND, [_block(_OVER_BOUND, *_OVER_NAMES)]),
            (_FITS, [_block(_FITS, "d")])]


def _tree(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "error.rs").write_text("fn x() {}", encoding="utf-8")
    (src / "small.rs").write_text("fn d() {}", encoding="utf-8")
    return tmp_path


def _admits(tmp_path: Path, logs: list[str]):
    def _fn(file_rel: str, fblocks: list[ImplBlock]) -> bool:
        return _file_route_admits(
            file_rel, fblocks, target_src_dir=tmp_path,
            frontier_tests=_NO_CONVICTING, per_impl_max_stubs=_PER_IMPL_MAX,
            log=logs.append)
    return _fn


def _route(admits, *, units, fill, logs: list[str]) -> _FileRoute:
    return _FileRoute(
        units_now=lambda: list(units), fill_file=fill, admits=admits,
        snapshot=lambda: None,  # type: ignore[arg-type,return-value]
        report_since=lambda _s: RouteRunReport(),
        tests_for=lambda _locs: frozenset(), stubs=frozenset(),
        on_plan=lambda _u: None, log=logs.append)


def test_a_file_over_the_bound_with_no_welded_group_is_not_a_run(
        tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PHASE_C_FILE_MAX_FNS", "20")
    root = _tree(tmp_path)
    logs: list[str] = []
    opened: list[str] = []
    plan = _file_fill_plan(
        _OVER_BOUND, [_block(_OVER_BOUND, *_OVER_NAMES)],
        frontier_tests=_NO_CONVICTING, per_impl_max_stubs=_PER_IMPL_MAX)
    assert plan.wanted == 21 and plan.max_fns == 20 and plan.groups == []
    route = _route(_admits(root, logs), units=_units(),
                   fill=lambda f, _bs, **_k: opened.append(f), logs=logs)
    runs = route.plan()
    assert [r.label for r in runs] == [f"file {_FITS}"]
    assert all(r.kind is RouteKind.FILE for r in runs)
    assert runs[0].targets == frozenset({FnLocator(_FITS, "d")})
    assert any("not scheduled" in ln and _OVER_BOUND in ln for ln in logs)
    assert opened == []


def test_negative_control_the_prefix_plan_schedules_a_run_that_opens_and_lands_nothing(
        tmp_path: Path, monkeypatch) -> None:
    """Every unit a run, the bound read only when it opens: the declined
    file's run is planned first and the fill is entered then returns
    without a landing — the row the schedule then ranked never-served
    again."""
    monkeypatch.setenv("PHASE_C_FILE_MAX_FNS", "20")
    _tree(tmp_path)
    logs: list[str] = []
    opened: list[str] = []

    def _fill(file_rel: str, _blocks, **_kw) -> None:
        # The pre-fix run-time decline: the bound refuses inside the fill.
        opened.append(file_rel)

    route = _route(lambda _f, _bs: True, units=_units(), fill=_fill, logs=logs)
    runs = route.plan()
    assert [r.label for r in runs] == [f"file {_OVER_BOUND}", f"file {_FITS}"]
    clock = RunClock(share_minutes=None, fill_remaining=lambda: None)
    route.run(runs[0], clock)
    assert opened == [_OVER_BOUND], (
        "the run OPENED (snapshot taken, fill entered) and the fill declined "
        "inside — the shape the plan-time bound removes")
