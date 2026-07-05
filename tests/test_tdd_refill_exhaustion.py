"""debt #46 residual: the TDD driver must BOUND behavioral re-fill retries.

A frontier fn that is already filled (non-stub) with a compiling-but-wrong body
is a re-fill target. If its test never passes it reappears in the frontier every
iter — without exhaustion tracking it would burn one LLM call per iter until the
stall budget alone exits. `run_phase_c_tdd` counts per-fn re-fill attempts and
marks the fn exhausted at the cap so the D3 filter stops dispatching it.

This is also the first direct test of the TDD loop (deps are all Callables).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.phase_c_tdd import (
    PhaseCTddDeps,
    run_phase_c_tdd,
)
from pipeline_core.domain.models import BuildCluster, FnLocator

_WRONG = FnLocator("src/a.rs", "wrongfn")


def _cluster() -> BuildCluster:
    return BuildCluster(
        name="stable", env_extra={}, tarpaulin_exclude_files=[],
        excluded_cfg=[], active_cfg=[], prompt_nightly_rule="")


def _deps(fill_calls: list[frozenset[FnLocator]]) -> PhaseCTddDeps:
    def _fill(**kw: Any) -> dict[str, Any]:
        fill_calls.append(frozenset(kw.get("tdd_frontier_keys") or ()))
        return {"fns_filled": 0, "attempts": 1, "bytes_filled": 0, "errors": []}

    # Constant failing test → pass_count never improves after iter 1 → the loop
    # stalls out; `wrongfn` is attributed every iter and is never a stub.
    return PhaseCTddDeps(
        attribute_failures=lambda *a, **k: ({_WRONG: {"t1"}}, "runtime"),
        build_test_reverse_map=lambda *a, **k: {},
        filter_test_targets=lambda frontier, src: frontier,
        check_graceful_stop=lambda *a, **k: None,
        converge_in_cluster=lambda *a, **k: None,
        count_relevant_notes=lambda *a, **k: 0,
        effective_stalls=lambda budget, module: budget,
        emit=lambda *a, **k: None,
        log=lambda *a, **k: None,
        order_frontier=lambda eligible, counts: list(eligible.items()),
        record_tdd_regression=lambda *a, **k: "",
        run_cargo=lambda *a, **k: (
            101, "test t1 ... FAILED\ntest result: FAILED. 3 passed; 1 failed"),
        scan_unimplemented_for_cluster=lambda *a, **k: [],
        scan_unimplemented_functions=lambda *a, **k: [],   # wrongfn is NOT a stub
        task2_fill_fns_one_by_one=_fill,
    )


def _run(deps: PhaseCTddDeps, tmp_path: Path, monkeypatch) -> dict[str, Any]:
    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "0")   # isolate the residual
    monkeypatch.setenv("TDD_MAX_STALLS", "3")
    monkeypatch.setenv("TDD_REFILL_MAX_ATTEMPTS", "2")
    return run_phase_c_tdd(
        deps, project_dir=tmp_path, target_src_dir=tmp_path, tests_dir_files=[],
        cluster_tests=[("tests/t.rs", "fn t1() {}")], module="m",
        cluster=_cluster(), check_cmd="cargo check", cluster_env={},
        leaf_counts=None, fr_path=None,
        test_cmd_builder=lambda stems: "cargo test", tag="[stable]")


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
        test_cmd_builder=lambda stems: "cargo test", tag="[stable]")
    assert len([c for c in fill_calls if _WRONG in c]) == 4
