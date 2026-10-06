"""A stub the cluster's function record proved no test can execute is not a
fill unit. Run `450922-661c5e9f` (2026-10-05, cluster `stable`): task0c proved
`src/backtrace.rs::_assert_send_sync` and the three `Clone::clone` bodies in
`src/ptr.rs` unreachable from rustc's MIR and the public index, and filtered
their lines from the coverage reading; the fill did not read that record, so
iterations 6 and 8 dispatched the assert, no route opened it, and it was left
admitted with no exhaustion charge.

The loop test replays iteration 6's per-fn batch (the 12 targets of the
`phase_c.fill.schedule` event) against the record task0c stored for that run
(`tests/fixtures/fills/450922-661c5e9f_stable_fns.json`). The clones are the
control on the other side: proved, but filled, so no stub and nothing to
withhold.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.phase_c_tdd import (
    PhaseCTddDeps,
    run_phase_c_tdd,
)
from pipeline_core.domain.attribution import AttributionResult, FailureKind
from pipeline_core.domain.cfg_dead_sites import CfgDeadSites, FrontierAdmission
from pipeline_core.domain.fn_execution import FnCoverageRecord
from pipeline_core.domain.models import BuildCluster, ClusterFillResult, FnLocator, FnSite
from pipeline_core.domain.stub_inventory import StubInventory, StubSite

_RECORD = Path(__file__).parent / "fixtures" / "fills" / "450922-661c5e9f_stable_fns.json"

# Iteration 6's per-fn batch, as `phase_c.fill.schedule` recorded it.
_BATCH = (
    ("src/backtrace.rs", "_assert_send_sync"),
    ("src/error.rs", "context_backtrace"),
    ("src/error.rs", "context_chain_downcast"),
    ("src/error.rs", "error_mut"),
    ("src/error.rs", "object_boxed"),
    ("src/error.rs", "object_downcast"),
    ("src/error.rs", "object_drop"),
    ("src/error.rs", "object_reallocate_boxed"),
    ("src/error.rs", "object_ref"),
    ("src/error.rs", "source"),
    ("src/error.rs", "vtable"),
    ("src/wrapper.rs", "source"),
)
_ASSERT = FnLocator("src/backtrace.rs", "_assert_send_sync")
_REAL_STUB = FnLocator("src/error.rs", "object_ref")


def _record() -> FnCoverageRecord:
    rec = FnCoverageRecord.from_json(_RECORD.read_text(encoding="utf-8"))
    assert rec is not None
    return rec


def _stubs() -> StubInventory:
    return StubInventory(tuple(StubSite(FnSite(f, fn, "")) for f, fn in _BATCH))


def _cluster() -> BuildCluster:
    return BuildCluster(name="stable", env_extra={}, tarpaulin_exclude_files=[],
                        excluded_cfg=[], active_cfg=[], prompt_nightly_rule="")


def _drive(tmp_path: Path, monkeypatch: Any, *, record: FnCoverageRecord | None,
           ) -> tuple[list[Any], list[str], list[Any]]:
    handoffs: list[Any] = []
    logs: list[str] = []
    events: list[Any] = []
    frontier = {FnLocator(f, fn): {f"t_{fn}"} for f, fn in _BATCH}
    sites = {FnSite(f, fn, ""): {f"t_{fn}"} for f, fn in _BATCH}

    def _fill(**kw: Any) -> ClusterFillResult:
        handoffs.append(kw.get("tdd_frontier_tests"))
        return ClusterFillResult(attempts=1)

    deps = PhaseCTddDeps(
        attribute_failures=lambda *a, **k: AttributionResult(
            dict(frontier), FailureKind.BEHAVIOR, dict(sites)),
        build_test_reverse_map=lambda *a, **k: {},
        filter_test_targets=lambda fr, src, **_k: dict(fr),
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
            101, "test t_object_ref ... FAILED\n"
                 "test result: FAILED. 3 passed; 12 failed"),
        scan_unimplemented_for_cluster=lambda *a, **k: _stubs(),
        scan_unimplemented_functions=lambda *a, **k: _stubs(),
        task2_fill_fns_one_by_one=_fill,
        cfg_dead_sites=lambda _sd, _cl: CfgDeadSites(),
        augmentation_fn_record=(None if record is None
                                else (lambda _pd, _c, _m: record)),
    )
    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "0")
    monkeypatch.setenv("TDD_MAX_STALLS", "1")
    monkeypatch.setenv("TDD_FLAT_SCORE_REPEATS", "0")
    run_phase_c_tdd(
        deps, project_dir=tmp_path, target_src_dir=tmp_path, tests_dir_files=[],
        cluster_tests=[("tests/t.rs", "fn t_object_ref() {}")], module="anyhow",
        cluster=_cluster(), check_cmd="cargo check", cluster_env={},
        leaf_counts=None, fr_path=None,
        test_cmd_builder=lambda stems, include_lib=False: "cargo test",
        tag="[stable]")
    return [h for h in handoffs if h is not None], logs, events


def test_iteration_6s_batch_does_not_dispatch_the_proved_assert(
        tmp_path: Path, monkeypatch: Any) -> None:
    handed, logs, events = _drive(tmp_path, monkeypatch, record=_record())
    assert handed, "the fill route was never called"
    for fr in handed:
        assert _ASSERT not in fr.dispatch
        assert _REAL_STUB in fr.dispatch
    assert any("withheld 1 stub(s) the function record proved" in m
               and "src/backtrace.rs::_assert_send_sync" in m for m in logs), logs
    assert any(ev == "phase_c.tdd.proved_unreachable_admission"
               and d["withheld"] == ["src/backtrace.rs::_assert_send_sync"]
               for ev, d in events)

    # NEGATIVE CONTROL: today's frontier, no record read — the assert is
    # one of the batch, beside the stub that shares it.
    control_dir = tmp_path / "control"
    control_dir.mkdir()
    c_handed, c_logs, _e = _drive(control_dir, monkeypatch, record=None)
    assert any(_ASSERT in fr.dispatch and _REAL_STUB in fr.dispatch for fr in c_handed)
    assert not any("proved no test can execute" in m for m in c_logs)


def test_a_proved_function_with_a_body_and_a_shared_locator_are_not_withheld() -> None:
    rec = _record()
    # The clones are proved and filled: no stub site, nothing returned.
    assert rec.proved_stub_units(s.site for s in _stubs().sites) == frozenset({_ASSERT})
    # A reachable stub at a locator the record proved one impl of stays:
    # `clone` under `Own` is proved, `clone` under another impl is not.
    own = FnSite("src/ptr.rs", "clone", "impl Clone for Own<T>")
    other = FnSite("src/ptr.rs", "clone", "impl Clone for Other")
    assert rec.proved_stub_units([own]) == frozenset({FnLocator("src/ptr.rs", "clone")})
    assert rec.proved_stub_units([own, other]) == frozenset()
    # The `src/` spelling does not split a site from its proof.
    assert rec.proved_stub_units([FnSite("backtrace.rs", "_assert_send_sync", "")]) == (
        frozenset({FnLocator("backtrace.rs", "_assert_send_sync")}))


def test_every_insert_refuses_a_proved_unit_and_reports_it_apart_from_cfg() -> None:
    adm = FrontierAdmission(CfgDeadSites(), proved=frozenset({_ASSERT}))
    assert adm.admit({_ASSERT: {"t"}, _REAL_STUB: {"t"}}) == {_REAL_STUB: {"t"}}
    assert adm.admit_sites({FnSite(_ASSERT.file, _ASSERT.fn_name, ""),
                            FnSite(_REAL_STUB.file, _REAL_STUB.fn_name, "")}) == frozenset(
        {FnSite(_REAL_STUB.file, _REAL_STUB.fn_name, "")})
    assert adm.without_proved({_ASSERT: {"t"}}) == {}
    assert adm.proved_report() == ("src/backtrace.rs::_assert_send_sync",)
    # The cfg-dead line, parsed back as a cfg-set reading, does not carry it.
    assert adm.report("stable") is None
