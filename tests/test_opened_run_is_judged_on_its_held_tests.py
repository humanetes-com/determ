"""A run opened on a blocker the fill measured is judged, prompted and
recorded on the tests that now stop in that body.

Run `2129106-ead996ab` (2026-10-28, cluster `stable`, fill 1): three runs
opened at the cursor on 9, 4 and 15 still-stubbed bodies, holding 125–128
failing tests per wave. The sweep's owner sets name none of them (at the
sweep every failing test stopped in a shallower stub and never entered these
bodies), the per-fn route handed the passes the sweep's frontier, so
`u2_test_gate` received an empty set for each and returned a plain accept:
28 bodies kept with no scoped run, 28 verdict rows reading "none were
failing", and the first-fill TEST section was whatever files mentioned the
name.

The plan's claims, each with its negative control:

- the run's frontier puts the held tests in the CONVICTING map of the body
  the pass fills, container included, and in dispatch (the one writer is
  `FailureFrontier.build`); a union into dispatch alone leaves the gate's
  set empty;
- a homonym of that body does not inherit them;
- the per-fn route hands the passes that frontier for a held run, and the
  sweep's for a planned one;
- the per-fn gate is handed the held tests, and a body whose tests stop in a
  deeper stub is kept deferred naming that holder;
- the first-fill TEST section quotes the files holding those tests;
- the opener carries the container from the held view into the run;
- a held run of any kind but per-fn cannot be built.

Invented shape, no crate names.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from pipeline_core.adapters.route_ledger_store import RouteLedgerStore
from pipeline_core.application.reconstruct import fill_one_fn as fof
from pipeline_core.application.reconstruct import fill_scheduler as fs
from pipeline_core.application.reconstruct import phase_c_engine as pce
from pipeline_core.domain.attribution import FailureFrontier, frontier_for_run
from pipeline_core.domain.fill_asks import FillAsks
from pipeline_core.domain.fill_schedule import RouteKind, RouteRun, RunClock
from pipeline_core.domain.models import (
    ClusterFillResult,
    FnLocator,
    FnSite,
    Stub,
)
from pipeline_core.domain.test_report import (
    FailureKind,
    TestFailure,
    U2Verdict,
)
from tests.test_fill_engine_usecases import (
    FakeFill,
    _ctx,
    _fill_one,
    _mk_ws,
    _verdicts,
)

_FILE = "src/a.rs"
_TOP = FnLocator(_FILE, "top")
_MID_A = FnSite(_FILE, "mid", "impl Shape for Alpha")   # the held stub
_MID_B = FnSite(_FILE, "mid", "impl Shape for Beta")    # its homonym
_LOW = FnSite(_FILE, "low", "")                         # the next layer


def _sweep() -> FailureFrontier:
    """The sweep: every failing test stopped in `top`, the only owner."""
    return FailureFrontier.build(
        {_TOP: {"t1", "t2"}}, {FnSite(_FILE, "top", ""): {"t1", "t2"}})


_HELD = ((_MID_A, frozenset({"t1", "t2"})),)


# ---- the frontier a run is judged on ----------------------------------------

def test_the_run_frontier_convicts_the_held_body_with_its_tests() -> None:
    run_f = frontier_for_run(_sweep(), _HELD)
    assert run_f is not None
    assert run_f.convicting_for_body(_FILE, "mid", _MID_A.container) == {
        "t1", "t2"}
    assert run_f.tests_for(FnLocator(_FILE, "mid")) == {"t1", "t2"}
    # The sweep's own facts are carried unchanged.
    assert run_f.convicting_for_body(_FILE, "top", "") == {"t1", "t2"}


def test_negative_control_a_union_into_dispatch_leaves_the_gate_nothing() -> None:
    """The writer the first plan named: the held tests as attribution only.
    `build` keeps no convicting entry it was not given, so the gate's set
    stays empty and the gate accepts with no run (the live outcome)."""
    sweep = _sweep()
    dispatch_only = FailureFrontier.build(
        {**sweep.dispatch, FnLocator(_FILE, "mid"): {"t1", "t2"}},
        sweep.convicting)
    assert dispatch_only.convicting_for_body(
        _FILE, "mid", _MID_A.container) == set()


def test_a_homonym_of_the_held_body_does_not_inherit_its_tests() -> None:
    run_f = frontier_for_run(_sweep(), _HELD)
    assert run_f is not None
    assert run_f.convicting_for_body(_FILE, "mid", _MID_B.container) == set()
    # Control: the locator union (the gate's read before this change)
    # hands the homonym the held body's tests.
    assert run_f.convicting_for(FnLocator(_FILE, "mid")) == {"t1", "t2"}


def test_a_site_the_sweep_measured_keeps_the_sweeps_set() -> None:
    sweep = FailureFrontier.build(
        {FnLocator(_FILE, "mid"): {"t9"}}, {_MID_A: {"t9"}})
    run_f = frontier_for_run(sweep, _HELD)
    assert run_f is not None
    assert run_f.convicting_for_body(_FILE, "mid", _MID_A.container) == {"t9"}


def test_nothing_held_is_the_sweeps_frontier_itself() -> None:
    sweep = _sweep()
    assert frontier_for_run(sweep, ()) is sweep
    assert frontier_for_run(sweep, ((_MID_A, frozenset()),)) is sweep


# ---- the per-fn route hands the passes the run's frontier --------------------

class _Engine:
    def __init__(self) -> None:
        self.frontiers: list[FailureFrontier | None] = []

    def _per_fn_passes(self, **kw: Any) -> ClusterFillResult:
        self.frontiers.append(kw["tdd_frontier_tests"])
        return ClusterFillResult()


def _route(engine: _Engine, sweep: FailureFrontier) -> pce._PerFnRoute:
    base: dict[str, Any] = {"crash_evidence": {}, "dispatched": set(),
                            "reverted": set(), "landed_fn": set(),
                            "tdd_frontier_tests": sweep}
    for name in ("project_dir", "target_src_dir", "tests_dir_files",
                 "module_name", "cluster_name", "cluster", "check_cmd",
                 "cluster_env", "max_passes", "leaf_name_counts", "fr_path",
                 "tdd_frontier_keys", "skip_end_pass_test", "tdd_test_cmd",
                 "crash_blamed", "crash_blocked", "dispatch_ranks",
                 "behaviour_specs", "cfg_dead", "is_cfg_dead", "answer_lost",
                 "landed_before", "heal_file"):
        base[name] = None
    return pce._PerFnRoute(engine, **base)  # type: ignore[arg-type]


def _opened() -> RouteRun:
    return RouteRun(kind=RouteKind.PER_FN, label="per-fn 1 blocker(s)",
                    targets=frozenset({FnLocator(_FILE, "mid")}),
                    parent="per-fn:x", held_tests=_HELD)


def _planned() -> RouteRun:
    return RouteRun(kind=RouteKind.PER_FN, label="per-fn 1 stub(s)",
                    targets=frozenset({_TOP}),
                    unit_tests=((_TOP, frozenset({"t1", "t2"})),))


def test_the_route_judges_a_held_run_on_its_own_frontier() -> None:
    engine = _Engine()
    sweep = _sweep()
    route = _route(engine, sweep)
    clock = RunClock(share_minutes=None, fill_remaining=lambda: None)
    route.run(_opened(), clock)
    route.run(_planned(), clock)
    held_f, planned_f = engine.frontiers
    assert held_f is not None
    assert held_f.convicting_for_body(_FILE, "mid", _MID_A.container) == {
        "t1", "t2"}
    assert planned_f is sweep


def test_negative_control_the_sweep_frontier_hands_the_gate_nothing(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pce, "frontier_for_run", lambda sweep, _held: sweep)
    engine = _Engine()
    route = _route(engine, _sweep())
    route.run(_opened(), RunClock(share_minutes=None,
                                  fill_remaining=lambda: None))
    (got,) = engine.frontiers
    assert got is not None
    assert got.convicting_for_body(_FILE, "mid", _MID_A.container) == set()


# ---- the per-fn gate and the prompt of the opened body -----------------------

class _Recorder(FakeFill):
    """The fill roles, recording the gate's test set; a test file per test."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.handed: list[set[str]] = []

    def u2_test_gate(self, *a: Any) -> Any:
        self.handed.append(set(a[0]))
        return super().u2_test_gate(*a)

    def find_tests_for_fn(self, _files: Any, name: str, **_k: Any
                          ) -> list[tuple[str, str]]:
        if name in ("t1", "t2"):
            return [(f"tests/{name}.rs", f"#[test]\nfn {name}() {{}}\n")]
        return [("tests/names_it.rs", f"// mentions {name}\n")]


_UNIT = FnSite("lib.rs", "answer", "")


def _run_unit(tmp_path: Path, frontier: FailureFrontier | None,
              fake: _Recorder, monkeypatch: pytest.MonkeyPatch
              ) -> tuple[Any, list[list[tuple[str, str]]]]:
    blocks: list[list[tuple[str, str]]] = []
    real = fof.build_per_fn_prompt

    def _spy(**kw: Any) -> Any:
        blocks.append(list(kw["test_blocks"]))
        return real(**kw)

    monkeypatch.setattr(fof, "build_per_fn_prompt", _spy)
    ws = _mk_ws(tmp_path)
    ctx = replace(_ctx(ws), tdd_frontier_tests=frontier)
    res = _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"),
                              ctx=ctx)
    return res, blocks


def _held_unit_frontier() -> FailureFrontier | None:
    return frontier_for_run(
        FailureFrontier.build({}, {}), ((_UNIT, frozenset({"t1", "t2"})),))


def test_the_gate_is_handed_the_held_tests_and_the_prompt_quotes_them(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Recorder(run=(0, ""), regr=None)
    res, blocks = _run_unit(tmp_path, _held_unit_frontier(), fake, monkeypatch)
    assert res.filled
    assert fake.handed == [{"t1", "t2"}]
    paths = [p for p, _ in blocks[0]]
    assert paths[:2] == ["tests/t1.rs", "tests/t2.rs"]
    assert "tests/names_it.rs" in paths


def test_negative_control_on_the_sweeps_frontier_the_gate_gets_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Recorder(run=(0, ""), regr=None, u2=U2Verdict.no_tests())
    _res, blocks = _run_unit(tmp_path, FailureFrontier.build({}, {}), fake,
                             monkeypatch)
    assert fake.handed == [set()]
    assert [p for p, _ in blocks[0]] == ["tests/names_it.rs"]
    rows = _verdicts(fake)
    assert "no test is attributed to this body" in rows[0]["reason"]
    assert "none were failing" not in rows[0]["reason"]


def test_a_held_body_whose_tests_stop_deeper_is_kept_naming_the_holder(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ext = ((TestFailure("t1", FailureKind.STUB_PANIC, "src/a.rs", 1, 1,
                        "not implemented", ""), _LOW),)
    fake = _Recorder(run=(0, ""), regr=None,
                     u2=U2Verdict.accept_external_only(ext))
    res, _blocks = _run_unit(tmp_path, _held_unit_frontier(), fake,
                             monkeypatch)
    assert res.filled and _LOW in res.blocked_by
    rows = _verdicts(fake)
    assert rows[0]["disposition"] == "keep_deferred"
    assert "src/a.rs::low" in rows[0]["evidence_keys"]


# ---- the opener carries the body -------------------------------------------

def test_the_opener_carries_the_held_body_with_its_container(
        tmp_path: Path) -> None:
    events: list[str] = []
    deps = fs.FillExecutionDeps(
        log=lambda _m: None, emit=lambda name, **_k: events.append(name),
        check_stop=lambda _c: None, ledger=RouteLedgerStore(tmp_path),
        project="p", cluster="c", run_id="r", iteration=1, oracle_fp="o")
    held = {"t1": frozenset({_MID_A}), "t2": frozenset({_MID_A})}
    ordered = fs._open_for_blockers(
        [], 0, held, parent="per-fn:p", planned_held=(), landed=(),
        opened=set(), asked=FillAsks(), bodies_sha=lambda _t: "",
        deps=deps)
    (opened,) = ordered
    assert opened.run.targets == frozenset({FnLocator(_FILE, "mid")})
    assert dict(opened.run.held_tests) == {_MID_A: frozenset({"t1", "t2"})}
    assert "phase_c.fill.blocker_opened" in events


def test_a_held_run_of_another_kind_cannot_be_built() -> None:
    with pytest.raises(ValueError, match="per-fn"):
        RouteRun(kind=RouteKind.FILE, label="file src/a.rs",
                 targets=frozenset({FnLocator(_FILE, "mid")}),
                 held_tests=_HELD)


def test_the_gate_of_a_homonym_body_is_not_handed_the_held_tests(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fill reads the gate's set per body (`convicting_for_body`).
    Control: the locator union, the read before this change, hands the
    other body the held body's tests."""
    held_on = FnSite("lib.rs", "answer", "impl Shape for Alpha")
    frontier = frontier_for_run(FailureFrontier.build({}, {}),
                                ((held_on, frozenset({"t1"})),))
    assert frontier is not None

    def _run(container: str) -> set[str]:
        fake = _Recorder(run=(0, ""), regr=None)
        monkeypatch.setattr(fof, "build_per_fn_prompt",
                            fof.build_per_fn_prompt)
        ws = _mk_ws(tmp_path / container.replace(" ", "_"))
        ctx = replace(_ctx(ws), tdd_frontier_tests=frontier)
        _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer",
                                    container=container), ctx=ctx)
        return fake.handed[0]

    assert _run("impl Shape for Beta") == set()
    assert _run("impl Shape for Alpha") == {"t1"}
    assert frontier.convicting_for(FnLocator("lib.rs", "answer")) == {"t1"}


# ---- the review of the first implementation (2026-10-28) --------------------

def test_a_free_function_homonym_is_not_handed_a_held_methods_tests() -> None:
    """Review: the empty-container caller read the union of every site."""
    run_f = frontier_for_run(_sweep(), _HELD)
    assert run_f is not None
    assert run_f.convicting_for_body(_FILE, "mid", "") == set()


def test_a_held_free_function_does_not_convict_the_methods_of_its_name() -> None:
    """Review: a held free function was written on the empty-container site,
    which every body of the name inherits while fewer than two containers
    are told apart."""
    free = FnSite(_FILE, "mid", "")
    run_f = frontier_for_run(_sweep(), ((free, frozenset({"t3"})),))
    assert run_f is not None
    assert run_f.convicting_for_body(_FILE, "mid", "") == {"t3"}
    assert run_f.convicting_for_body(_FILE, "mid", _MID_A.container) == set()


def test_negative_control_a_lifted_locator_still_convicts_every_body() -> None:
    """The sweep's empty-container sites are locator-level facts and keep
    applying to every body; only the measured, body-exact ones narrow."""
    sweep = FailureFrontier.build({FnLocator(_FILE, "mid"): {"t4"}},
                                  {FnLocator(_FILE, "mid"): {"t4"}})
    assert sweep.convicting_for_body(_FILE, "mid", _MID_A.container) == {"t4"}
    assert sweep.convicting_for_body(_FILE, "mid", "") == {"t4"}


class _SiteEngine(_Engine):
    def __init__(self) -> None:
        super().__init__()
        self.sites: list[frozenset[FnSite] | None] = []

    def _per_fn_passes(self, **kw: Any) -> ClusterFillResult:
        self.sites.append(kw.get("write_sites"))
        return super()._per_fn_passes(**kw)


def test_the_route_writes_only_the_held_bodies_of_an_opened_run() -> None:
    engine = _SiteEngine()
    route = _route(engine, _sweep())
    clock = RunClock(share_minutes=None, fill_remaining=lambda: None)
    route.run(_opened(), clock)
    route.run(_planned(), clock)
    assert engine.sites[0] == frozenset({fs_canon(_MID_A)})
    assert engine.sites[1] is None


def fs_canon(site: FnSite) -> FnSite:
    from pipeline_core.domain.attribution import canon_site
    return canon_site(site)


def test_the_pass_leaves_a_homonym_stub_of_an_opened_name_alone(
        tmp_path: Path) -> None:
    """The per-fn pass keeps every stub record whose locator is a target;
    with `write_sites` it keeps only the bodies the fill measured.
    Control: without it both bodies of the name are filled."""
    from pipeline_core.domain.cfg_dead_sites import CfgDeadSites
    from pipeline_core.domain.enums import FillUnit
    from tests.test_fill_engine_usecases import _engine

    def _pass(write_sites: frozenset[FnSite] | None) -> tuple[list[str], int]:
        ws = _mk_ws(tmp_path / f"w{len(list(tmp_path.iterdir()))}")
        recs = [{"file": "lib.rs", "fn_name": "answer", "container": "",
                 "sig_text": "pub fn answer()"},
                {"file": "lib.rs", "fn_name": "answer",
                 "container": "impl Shape for Alpha",
                 "sig_text": "fn answer(&self)"}]
        fake = FakeFill(stub_records=recs, run=(0, ""), regr=None)
        eng = _engine(fake, FillUnit.FN)
        eng._per_fn_passes(
            project_dir=ws, target_src_dir=ws / "src", tests_dir_files=[],
            module_name="m", cluster_name="t", cluster=None,
            check_cmd="cargo check", cluster_env={}, max_passes=1,
            leaf_name_counts=None, fr_path=None,
            tdd_frontier_keys={FnLocator("lib.rs", "answer")},
            skip_end_pass_test=True, tdd_frontier_tests=None,
            tdd_test_cmd=None, crash_evidence=None, crash_blamed=None,
            crash_blocked=None, culprits_of=None, dispatch_ranks=None,
            behaviour_specs=(), _cfg_dead=CfgDeadSites(),
            _is_cfg_dead=lambda _k: False, _dispatched=set(),
            _reverted=set(), _answer_lost=set(), landed_before=lambda: 0,
            claim=lambda *_a, **_k: True, write_sites=write_sites)
        lines = [m for m in fake.logs if "not the bodies the fill measured" in m]
        return lines, fake.events.count("phase_c.fn.fill.start")

    held = frozenset({fs_canon(FnSite("lib.rs", "answer",
                                      "impl Shape for Alpha"))})
    lines, started = _pass(held)
    assert len(lines) == 1 and "lib.rs::answer []" in lines[0]
    assert started == 1
    lines, started = _pass(None)
    assert lines == [] and started == 2
