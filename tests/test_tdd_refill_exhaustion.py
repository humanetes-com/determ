"""debt #46 residual: the TDD driver must BOUND behavioral re-fill retries.

A frontier fn that is already filled (non-stub) with a compiling-but-wrong body
is a re-fill target. If its test never passes it reappears in the frontier every
iter — without exhaustion tracking it would burn one LLM call per iter until the
stall budget alone exits. `run_phase_c_tdd` counts per-fn re-fill attempts and
marks the fn exhausted at the cap so the D3 filter stops dispatching it.

This is also the first direct test of the TDD loop (deps are all Callables).
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.phase_c_tdd import (
    PhaseCTddDeps,
    run_phase_c_tdd,
)
from pipeline_core.application.retrigger_layout import RetriggerResult
from pipeline_core.domain.attribution import AttributionResult, FailureKind
from pipeline_core.domain.models import BuildCluster, ClusterFillResult, FnLocator

_WRONG = FnLocator("src/a.rs", "wrongfn")


def _cluster() -> BuildCluster:
    return BuildCluster(
        name="stable", env_extra={}, tarpaulin_exclude_files=[],
        excluded_cfg=[], active_cfg=[], prompt_nightly_rule="")


def _deps(fill_calls: list[frozenset[FnLocator]]) -> PhaseCTddDeps:
    def _fill(**kw: Any) -> ClusterFillResult:
        fill_calls.append(frozenset(kw.get("tdd_frontier_keys") or ()))
        return ClusterFillResult(attempts=1)

    # Constant failing test → pass_count never improves after iter 1 → the loop
    # stalls out; `wrongfn` is attributed every iter and is never a stub.
    return PhaseCTddDeps(
        attribute_failures=lambda *a, **k: AttributionResult(
            {_WRONG: {"t1"}}, FailureKind.RUNTIME, {_WRONG: {"t1"}}),
        build_test_reverse_map=lambda *a, **k: {},
        filter_test_targets=lambda frontier, src: frontier,
        check_graceful_stop=lambda *a, **k: None,
        check_auto_operator=lambda *a, **k: None,
        converge_in_cluster=lambda *a, **k: None,
        count_relevant_notes=lambda *a, **k: 0,
        effective_stalls=lambda budget, module: budget,
        emit=lambda *a, **k: None,
        log=lambda *a, **k: None,
        order_frontier=lambda eligible, counts: list(eligible.items()),
        record_tdd_regression=lambda *a, **k: "",
        reset_exhaustion_flags=lambda *a, **k: 0,
        run_cargo=lambda *a, **k: (
            101, "test t1 ... FAILED\ntest result: FAILED. 3 passed; 1 failed"),
        scan_unimplemented_for_cluster=lambda *a, **k: [],
        scan_unimplemented_functions=lambda *a, **k: [],   # wrongfn is NOT a stub
        task2_fill_fns_one_by_one=_fill,
    )


def _run(deps: PhaseCTddDeps, tmp_path: Path, monkeypatch) -> ClusterFillResult:
    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "0")   # isolate the residual
    monkeypatch.setenv("TDD_MAX_STALLS", "3")
    monkeypatch.setenv("TDD_REFILL_MAX_ATTEMPTS", "2")
    return run_phase_c_tdd(
        deps, project_dir=tmp_path, target_src_dir=tmp_path, tests_dir_files=[],
        cluster_tests=[("tests/t.rs", "fn t1() {}")], module="m",
        cluster=_cluster(), check_cmd="cargo check", cluster_env={},
        leaf_counts=None, fr_path=None,
        test_cmd_builder=lambda stems, include_lib=False: "cargo test", tag="[stable]")


def test_refill_target_exhausts_after_cap(tmp_path: Path, monkeypatch) -> None:
    fill_calls: list[frozenset[FnLocator]] = []
    _run(_deps(fill_calls), tmp_path, monkeypatch)
    # wrongfn is dispatched for re-fill exactly TDD_REFILL_MAX_ATTEMPTS (2) times;
    # thereafter it is exhausted → D3 skips it, so no further fill dispatch.
    dispatched = [c for c in fill_calls if _WRONG in c]
    assert len(dispatched) == 2, fill_calls


def test_refill_cap_env_raises_attempts(tmp_path: Path, monkeypatch) -> None:
    fill_calls: list[frozenset[FnLocator]] = []
    monkeypatch.setenv("TDD_MAX_STALLS", "10")   # let more iters run
    monkeypatch.setenv("TDD_REFILL_MAX_ATTEMPTS", "4")
    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "0")
    run_phase_c_tdd(
        _deps(fill_calls), project_dir=tmp_path, target_src_dir=tmp_path,
        tests_dir_files=[], cluster_tests=[("tests/t.rs", "fn t1() {}")],
        module="m", cluster=_cluster(), check_cmd="cargo check", cluster_env={},
        leaf_counts=None, fr_path=None,
        test_cmd_builder=lambda stems, include_lib=False: "cargo test", tag="[stable]")
    assert len([c for c in fill_calls if _WRONG in c]) == 4


def test_reset_exhaustion_fires_once_after_progress_then_stays_quiet(
        tmp_path: Path, monkeypatch) -> None:
    # Progress lands once (iter 1's fill), then plateaus. The reset should
    # fire exactly once — right after that progress is observed — and never
    # again once nothing new lands, same bound as the refill cap above but
    # for the retrospective-cache exhaustion gate instead of the local
    # per-fn re-fill counter.
    call_n = [0]
    reset_reasons: list[str] = []

    def _fill(**kw: Any) -> ClusterFillResult:
        call_n[0] += 1
        return ClusterFillResult(fns_filled=1 if call_n[0] == 1 else 0, attempts=1)

    def _reset_exhaustion_flags(pd, reason):
        reset_reasons.append(reason)
        return 1

    deps = replace(
        _deps([]),
        task2_fill_fns_one_by_one=_fill,
        reset_exhaustion_flags=_reset_exhaustion_flags,
    )
    _run(deps, tmp_path, monkeypatch)
    assert len(reset_reasons) == 1, reset_reasons


def test_layout_retrigger_resets_stall_and_continues_the_loop(
        tmp_path: Path, monkeypatch) -> None:
    # F11 M3: on the FIRST stall-exit, a successful retrigger must reset the
    # stall budget and let the loop keep going (proven by MORE cargo runs
    # than a bare TDD_MAX_STALLS=3 would allow) instead of marking fns
    # exhausted and breaking. On the SECOND stall-exit (simulating a spent
    # cap), it reports not-fired and the loop exits exactly as before F11 M3.
    cargo_calls = [0]
    retrig_calls: list[tuple] = []
    events: list[tuple[str, dict]] = []

    def _run_cargo(*a, **k):
        cargo_calls[0] += 1
        return (101, "test t1 ... FAILED\ntest result: FAILED. 3 passed; 1 failed")

    def _retrigger(project_dir, module, cluster_name, target_src_dir, error_text):
        retrig_calls.append((cluster_name, error_text))
        if len(retrig_calls) == 1:
            return RetriggerResult(fired=True, succeeded=True, reverted_count=2,
                                   cause="LAYOUT_EXHAUSTION")
        return RetriggerResult(fired=False, reason="cap reached")

    deps = replace(
        _deps([]),
        run_cargo=_run_cargo,
        retrigger_layout=_retrigger,
        emit=lambda name, data=None: events.append((name, data or {})),
    )
    _run(deps, tmp_path, monkeypatch)
    # 1st stall-exit retriggers (continues); 2nd stall-exit (cap spent) does
    # not — strictly more cargo runs than the bare no-retrigger baseline
    # below (4), proving the loop kept going past what would have exited it.
    assert cargo_calls[0] > 4
    assert len(retrig_calls) == 2
    fired_events = [d for n, d in events if n == "phase_c.layout_retrigger.fired"]
    assert len(fired_events) == 1
    assert fired_events[0]["reverted_count"] == 2


def test_omitting_retrigger_layout_preserves_prior_stall_exit_behaviour(
        tmp_path: Path, monkeypatch) -> None:
    # retrigger_layout defaults to None — the exit must behave EXACTLY as
    # before F11 M3 (regression guard on the shared stall-exit hook).
    cargo_calls = [0]

    def _run_cargo(*a, **k):
        cargo_calls[0] += 1
        return (101, "test t1 ... FAILED\ntest result: FAILED. 3 passed; 1 failed")

    deps = replace(_deps([]), run_cargo=_run_cargo)
    assert deps.retrigger_layout is None
    _run(deps, tmp_path, monkeypatch)
    assert cargo_calls[0] == 4  # exits at the bare TDD_MAX_STALLS, no retrigger
