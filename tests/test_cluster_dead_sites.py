"""A site a cluster never compiles is dead whatever its body holds, and no
insert — the frontier map, a formatter pair, a measured-owner admit, a
session slice, the per-fn re-fill collector, the splice — can write it.

Run `473471-33301154` (2026-09-29, cluster `stable`, fill 6): a module
declared behind a cfg the cluster turns off had its two stubs filled; the
per-fn pass then printed that it skipped them as not active in the cluster's
cfg-set AND dispatched both as `refill: true` — 31.6 of the run's 32.0
minutes — because deadness was the difference between every STUB record and
the cluster-active stub records, so a filled dead fn was in neither set; the
re-fill collector inferred "filled" from absence in the cluster-filtered stub
list; and the TDD frontier had no membership gate, so the same locator took a
dispatch slot, 31 route-run targets and held two tests' escalation.

Every fixture here is invented (`feat_x`, `m.rs`); each test carries the
pre-fix reading as its negative control.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pipeline_core.application.reconstruct.cluster_membership import (
    cluster_dead_sites,
)
from pipeline_core.application.reconstruct.impl_fill import cluster_scoped_splice
from pipeline_core.application.reconstruct.phase_c_tdd import (
    PhaseCTddDeps,
    run_phase_c_tdd,
)
from pipeline_core.domain.attribution import (
    AttributionResult,
    FailureFrontier,
    FailureKind,
    FormatterPair,
)
from pipeline_core.domain.cfg_dead_sites import CfgDeadSites, FrontierAdmission
from pipeline_core.domain.cluster_membership_report import ClusterMembershipReport
from pipeline_core.domain.enums import FillUnit
from pipeline_core.domain.models import (
    BuildCluster,
    ClusterFillResult,
    FnLocator,
    FnSite,
    ModRecord,
)
from pipeline_core.domain.pair_fill import dispatch_including_pairs
from pipeline_core.domain.reconstruction import SpliceOutcome
from pipeline_core.domain.run_health import HealthKind, analyze
from pipeline_core.domain.slice_session import slices_with_pair_hops
from pipeline_core.domain.stub_inventory import StubInventory
from tests.test_fill_engine_usecases import FakeFill, _engine

_SRC = Path("/w/.skeleton-filled/c/src")
_CL = SimpleNamespace(name="c", cfg_true=[], cfg_false=["feat_x"])
_MODS = [ModRecord(name="m", file="m.rs", cfg_predicates=("feat_x",))]
_RECS: list[dict[str, Any]] = [
    # `#[cfg(feat_x)] mod m;` — one stub and one body some route filled.
    {"file": "m.rs", "fn_name": "hollow", "cfg_predicates": [],
     "has_unimplemented_body": True},
    {"file": "m.rs", "fn_name": "written", "cfg_predicates": [],
     "has_unimplemented_body": False},
    # One name, two arms under complementary gates: one arm compiles.
    {"file": "lib.rs", "fn_name": "arm", "cfg_predicates": ["feat_x"]},
    {"file": "lib.rs", "fn_name": "arm", "cfg_predicates": ["not(feat_x)"]},
    # A predicate this cluster cannot decide.
    {"file": "lib.rs", "fn_name": "maybe", "cfg_predicates": ["unknown_y"]},
    {"file": "lib.rs", "fn_name": "gated", "cfg_predicates": ["feat_x"]},
    {"file": "lib.rs", "fn_name": "plain", "cfg_predicates": []},
]


def _dead() -> CfgDeadSites:
    return cluster_dead_sites(_SRC, _CL, enumerate_fns=lambda _d: list(_RECS),
                              mod_records=lambda _d: list(_MODS))


def _old_stub_census(recs: list[dict[str, Any]]) -> frozenset[tuple[str, str]]:
    """The deleted verdict, reproduced as the negative control: every stub
    record minus the cluster-active stub records (the active ones here are
    the stubs outside the gated module)."""
    stubs = [r for r in recs if r.get("has_unimplemented_body")]
    active = {(r["file"], r["fn_name"]) for r in stubs if r["file"] != "m.rs"}
    return frozenset((r["file"], r["fn_name"]) for r in stubs) - active


# -- (a) the model ----------------------------------------------------------

def test_a_gated_module_is_dead_whether_its_fn_is_a_stub_or_filled() -> None:
    dead = _dead()
    assert ("m.rs", "hollow") in dead
    assert ("m.rs", "written") in dead
    assert FnLocator("src/m.rs", "written") in dead   # any `src/` spelling
    assert ("lib.rs", "gated") in dead

    # NEGATIVE CONTROL: the stub census calls the filled fn live.
    assert ("m.rs", "written") not in _old_stub_census(_RECS)


def test_complementary_gates_and_undecidable_predicates_stay_live() -> None:
    dead = _dead()
    assert ("lib.rs", "arm") not in dead
    assert ("lib.rs", "maybe") not in dead
    assert ("lib.rs", "plain") not in dead


def test_no_cluster_or_no_extractor_knows_no_dead_site() -> None:
    assert not cluster_dead_sites(_SRC, None, enumerate_fns=lambda _d: _RECS,
                                  mod_records=lambda _d: _MODS)
    assert not cluster_dead_sites(_SRC, _CL, enumerate_fns=lambda _d: None,
                                  mod_records=lambda _d: _MODS)


# -- (b) the per-fn collector and the splice --------------------------------

_HOLLOW = FnLocator("src/m.rs", "hollow")
_WRITTEN = FnLocator("src/m.rs", "written")


def _engine_over_records() -> Any:
    fns = [{"file": "src/m.rs", "fn_name": "hollow", "sig_text": "fn hollow()"},
           {"file": "src/m.rs", "fn_name": "written", "sig_text": "fn written()"}]
    return _engine(FakeFill(fns=fns, stub_records=[fns[0]]), FillUnit.FN)


def test_a_dead_stub_missing_from_the_cluster_stub_list_is_not_a_refill(
        tmp_path: Path) -> None:
    eng = _engine_over_records()
    tests = FailureFrontier.of_direct({_HOLLOW: {"t1"}})
    out = eng._collect_refill_targets(
        tmp_path, [], {_HOLLOW}, tests,
        every_stub_key=eng._every_stub_key(tmp_path))
    assert out == []

    # NEGATIVE CONTROL: "filled" read off the cluster-filtered list (empty
    # here) admits the dead stub as `refill: true` — the recorded dispatch.
    control = eng._collect_refill_targets(
        tmp_path, [], {_HOLLOW}, tests, every_stub_key=frozenset())
    assert [(s.key, s.refill) for s in control] == [(_HOLLOW, True)]


def test_a_filled_dead_body_is_refused_with_the_stubs_not_re_filled(
        tmp_path: Path) -> None:
    eng = _engine_over_records()
    tests = FailureFrontier.of_direct({_WRITTEN: {"t1"}})
    dead = _dead()
    stubs, refills, refused = eng._per_fn_targets(
        tmp_path, [], {_WRITTEN}, tests, crash_blamed=frozenset(),
        is_cfg_dead=lambda k: k in dead)
    assert (stubs, refills) == ([], [])
    assert [s.key for s in refused] == [_WRITTEN]

    # NEGATIVE CONTROL: a stub-only verdict never names the filled body, so
    # it is dispatched as a re-fill.
    old = _old_stub_census(_RECS)
    _s, c_refills, c_refused = eng._per_fn_targets(
        tmp_path, [], {_WRITTEN}, tests, crash_blamed=frozenset(),
        is_cfg_dead=lambda k: (k.file.removeprefix("src/"), k.fn_name) in old)
    assert [r.key for r in c_refills] == [_WRITTEN] and c_refused == []


def test_the_splice_refuses_a_body_into_a_filled_dead_site() -> None:
    calls: list[str] = []

    def _splice(file_path, container, fn_name, body, where_clause=None, **kw):
        calls.append(fn_name)
        return SpliceOutcome(True, "ok", len(body))

    guarded = cluster_scoped_splice(_splice, inactive=_dead(),
                                    target_src_dir=_SRC, log=lambda _m: None)
    out = guarded(_SRC / "m.rs", "impl Show for T", "written", "x",
                  allow_nonstub=True)
    assert (out.ok, out.reason, calls) == (False, "not_in_cluster_cfg", [])

    # NEGATIVE CONTROL: guarded by the stub census, the refill splice lands.
    control = cluster_scoped_splice(
        _splice, inactive=CfgDeadSites(_old_stub_census(_RECS)),
        target_src_dir=_SRC, log=lambda _m: None)
    assert control(_SRC / "m.rs", "impl Show for T", "written", "x",
                   allow_nonstub=True).ok
    assert calls == ["written"]


# -- (c) the inserts the locator filter does not see ------------------------

_LIVE = FnSite("src/a.rs", "show", "impl Show for A")
_DEAD = FnSite("src/m.rs", "written", "impl Show for T")
_PAIR = FormatterPair(dispatcher=_LIVE, implementer=_DEAD)
_LIVE_LOC = FnLocator(_LIVE.file, _LIVE.fn_name)
_DEAD_LOC = FnLocator(_DEAD.file, _DEAD.fn_name)
_DEAD_SET = CfgDeadSites(frozenset({("src/m.rs", "written")}))


def _cluster() -> BuildCluster:
    return BuildCluster(name="c", env_extra={}, tarpaulin_exclude_files=[],
                        excluded_cfg=[], active_cfg=[], prompt_nightly_rule="")


def _drive(tmp_path: Path, monkeypatch: Any, *,
           dead: CfgDeadSites | None) -> tuple[list[Any], list[str], list[Any]]:
    handoffs: list[Any] = []
    logs: list[str] = []
    events: list[Any] = []

    def _fill(**kw: Any) -> ClusterFillResult:
        handoffs.append(kw.get("tdd_frontier_tests"))
        return ClusterFillResult(attempts=1)

    deps = PhaseCTddDeps(
        attribute_failures=lambda *a, **k: AttributionResult(
            {_LIVE_LOC: {"t1"}}, FailureKind.BEHAVIOR, {_LIVE: {"t1"}},
            pairs=frozenset({_PAIR})),
        build_test_reverse_map=lambda *a, **k: {},
        # The real filter drops dead sites from the MAP; the pair is not in
        # the map, which is the hole this test is about.
        filter_test_targets=lambda frontier, src, **_k: dict(frontier),
        check_graceful_stop=lambda *a, **k: None,
        check_auto_operator=lambda *a, **k: None,
        converge_in_cluster=lambda *a, **k: None,
        count_relevant_notes=lambda *a, **k: 0,
        effective_stalls=lambda budget, module: budget,
        emit=lambda ev, *a, **k: events.append((ev, k.get("data"))),
        log=lambda m, *a, **k: logs.append(m),
        order_frontier=lambda eligible, counts: list(eligible.items()),
        record_tdd_regression=lambda *a, **k: "",
        reset_exhaustion_flags=lambda *a, **k: [],
        run_cargo=lambda *a, **k: (
            101, "test t1 ... FAILED\ntest result: FAILED. 3 passed; 1 failed"),
        scan_unimplemented_for_cluster=lambda *a, **k: StubInventory(),
        scan_unimplemented_functions=lambda *a, **k: StubInventory(),
        task2_fill_fns_one_by_one=_fill,
        cfg_dead_sites=(None if dead is None else (lambda _sd, _cl: dead)),
    )
    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "0")
    monkeypatch.setenv("TDD_MAX_STALLS", "1")
    monkeypatch.setenv("TDD_FLAT_SCORE_REPEATS", "0")
    run_phase_c_tdd(
        deps, project_dir=tmp_path, target_src_dir=tmp_path, tests_dir_files=[],
        cluster_tests=[("tests/t.rs", "fn t1() {}")], module="m",
        cluster=_cluster(), check_cmd="cargo check", cluster_env={},
        leaf_counts=None, fr_path=None,
        test_cmd_builder=lambda stems, include_lib=False: "cargo test",
        tag="[c]")
    return [h for h in handoffs if h is not None], logs, events


def test_a_pair_with_a_dead_member_reaches_no_route(
        tmp_path: Path, monkeypatch: Any) -> None:
    handed, logs, events = _drive(tmp_path, monkeypatch, dead=_DEAD_SET)
    assert handed, "the fill route was never called"
    for fr in handed:
        assert not fr.pairs
        assert _DEAD_LOC not in fr.dispatch
        assert _LIVE_LOC in fr.dispatch
    # (d) one admission line per iteration, with the denominator.
    lines = [m for m in logs if "frontier unit(s) not active" in m]
    assert lines, logs
    rep = ClusterMembershipReport.parse(lines[0])
    assert rep is not None and rep.frontier
    assert (rep.cluster_name, rep.total, rep.skipped) == (
        "c", 2, ("src/m.rs::written",))
    per_iter = [d["iter"] for e, d in events
                if e == "phase_c.tdd.cfg_dead_admission"]
    assert len(per_iter) == len(set(per_iter))

    # NEGATIVE CONTROL: with no dead-site verdict the pair travels, and
    # `dispatch_including_pairs` writes its dead member into the dispatch map.
    control_dir = tmp_path / "control"
    control_dir.mkdir()
    c_handed, _l, _e = _drive(control_dir, monkeypatch, dead=None)
    assert any(_PAIR in fr.pairs and _DEAD_LOC in fr.dispatch
               for fr in c_handed)


def test_the_admitted_pairs_close_every_pair_consumer() -> None:
    adm = FrontierAdmission(_DEAD_SET)
    pairs = adm.admit_pairs({_PAIR})
    assert pairs == frozenset()
    eligible = {_LIVE_LOC: {"t1"}}
    assert _DEAD_LOC not in dispatch_including_pairs(
        eligible, {_LIVE_LOC}, pairs, {_LIVE: {"t1"}})
    assert FailureFrontier.build(eligible, {}, pairs=pairs).pairs == frozenset()
    hopped = slices_with_pair_hops(
        {"t1": {_LIVE}}, ((p.dispatcher, p.implementer) for p in pairs))
    assert hopped["t1"] == frozenset({_LIVE})

    # NEGATIVE CONTROL: the raw pair set puts the dead endpoint in all three.
    raw = frozenset({_PAIR})
    assert _DEAD_LOC in dispatch_including_pairs(
        eligible, {_LIVE_LOC}, raw, {_LIVE: {"t1"}})
    assert FailureFrontier.build(eligible, {}, pairs=raw).pairs == raw
    assert _DEAD in slices_with_pair_hops(
        {"t1": {_LIVE}}, ((p.dispatcher, p.implementer) for p in raw))["t1"]


def test_a_measured_owner_admit_and_a_session_slice_drop_the_dead_site() -> None:
    adm = FrontierAdmission(_DEAD_SET)
    assert adm.admit({_DEAD_LOC: {"t1"}, _LIVE_LOC: {"t1"}}) == {_LIVE_LOC: {"t1"}}
    assert adm.admit_sites({_LIVE, _DEAD}) == frozenset({_LIVE})
    rep = adm.report("c")
    assert rep is not None
    # Proposed twice, reported once.
    assert rep.skipped == ("src/m.rs::written",) and rep.total == 2


def test_nothing_refused_reports_nothing() -> None:
    adm = FrontierAdmission(_DEAD_SET)
    adm.admit({_LIVE_LOC: {"t1"}})
    assert adm.report("c") is None


def test_the_health_detector_does_not_read_the_frontier_line_as_the_stub_scan(
) -> None:
    line = ClusterMembershipReport("c", 1, ("src/m.rs::written",),
                                   frontier=True).log_line()
    kinds = {f.kind for f in analyze([line], [])}
    assert HealthKind.MEMBERSHIP_FILTER_EMPTIED not in kinds

    # NEGATIVE CONTROL: the stub-scan form of the same numbers is emptied.
    stub_line = ClusterMembershipReport("c", 1, ("src/m.rs::written",)).log_line()
    assert HealthKind.MEMBERSHIP_FILTER_EMPTIED in {
        f.kind for f in analyze([stub_line], [])}
