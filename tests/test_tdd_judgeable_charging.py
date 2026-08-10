"""Driver-level pins for judgeable charging, the two-store release, the
red-build budget and the tainted-sweep gates.

All four behaviors were observed missing on run `2219473-e43ea61c`
(2026-08-10): 15 units blanket-convicted at a stall exit whose last two
iterations dispatched nothing; a re-admission that released 11 disk flags
and reopened zero frontier entries; a nightly phase spending three
iterations at 0/113 under an unchanged pair of rustc errors; and a
`VERDICT MISMATCH` line that gated nothing while every consumer charged the
sweep it disclaimed."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.phase_c_tdd import (
    PhaseCTddDeps,
    run_phase_c_tdd,
)
from pipeline_core.domain.attribution import AttributionResult, FailureKind
from pipeline_core.domain.models import BuildCluster, ClusterFillResult, FnLocator

_WRONG = FnLocator("src/a.rs", "wrongfn")

_CONSISTENT_FAIL = (
    "    Running tests/t.rs (t/t-1)\n"
    "running 4 tests\n"
    "test a ... ok\ntest b ... ok\ntest c ... ok\n"
    "test t1 ... FAILED\n"
    "test result: FAILED. 3 passed; 1 failed")

# Counters claim three passes; the printed verdicts show one, in a COMPLETED
# binary — the counter/verdict mismatch no crash explains.
_TAINTED_FAIL = (
    "    Running tests/t.rs (t/t-1)\n"
    "running 4 tests\n"
    "test a ... ok\n"
    "test t1 ... FAILED\n"
    "test result: FAILED. 3 passed; 1 failed")

_RED_BUILD = (
    "error[E0605]: non-primitive cast: `E` as `&dyn core::error::Error`\n"
    " --> src/backtrace.rs:21:9\n"
    "error: could not compile `anyhow` (lib) due to 1 previous error\n")


def _cluster() -> BuildCluster:
    return BuildCluster(
        name="stable", env_extra={}, tarpaulin_exclude_files=[],
        excluded_cfg=[], active_cfg=[], prompt_nightly_rule="")


def _deps(fill_calls: list[frozenset[FnLocator]], *, logs: list[str],
          run_cargo, reset=lambda *a, **k: []) -> PhaseCTddDeps:
    def _fill(**kw: Any) -> ClusterFillResult:
        fill_calls.append(frozenset(kw.get("tdd_frontier_keys") or ()))
        return ClusterFillResult(attempts=1)

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
        log=logs.append,
        order_frontier=lambda eligible, counts: list(eligible.items()),
        record_tdd_regression=lambda *a, **k: "",
        reset_exhaustion_flags=reset,
        run_cargo=run_cargo,
        scan_unimplemented_for_cluster=lambda *a, **k: [],
        scan_unimplemented_functions=lambda *a, **k: [],
        task2_fill_fns_one_by_one=_fill,
    )


def _run(deps: PhaseCTddDeps, tmp_path: Path, monkeypatch,
         max_stalls: str = "3") -> ClusterFillResult:
    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "0")
    monkeypatch.setenv("TDD_MAX_STALLS", max_stalls)
    monkeypatch.setenv("TDD_REFILL_MAX_ATTEMPTS", "2")
    return run_phase_c_tdd(
        deps, project_dir=tmp_path, target_src_dir=tmp_path, tests_dir_files=[],
        cluster_tests=[("tests/t.rs", "fn t1() {}")], module="m",
        cluster=_cluster(), check_cmd="cargo check", cluster_env={},
        leaf_counts=None, fr_path=None,
        test_cmd_builder=lambda stems, include_lib=False: "cargo test",
        tag="[stable]")


def test_a_release_reopens_the_loop_local_gates(tmp_path, monkeypatch) -> None:
    """The two-store fix: once `wrongfn` is refill-capped into the loop-local
    exhausted set, a warranted disk release naming it must reopen it for
    dispatch. Pre-fix, the release touched only the disk flags — live,
    're-admitted 11' dispatched zero."""
    fill_calls: list[frozenset[FnLocator]] = []
    logs: list[str] = []
    fills = [0]

    def _fill(**kw: Any) -> ClusterFillResult:
        fill_calls.append(frozenset(kw.get("tdd_frontier_keys") or ()))
        fills[0] += 1
        # Land one fill per pass so the re-admission gate's "fills since
        # reset" half stays satisfiable.
        return ClusterFillResult(attempts=1, fns_filled=1)

    # Score climbs by one every sweep, so `best > watermark` holds at each
    # boundary and every release the disk can grant is granted.
    outs = iter([1, 2, 3, 4, 5, 6])

    def _cargo(*a: Any, **k: Any):
        n = min(next(outs, 6), 4)
        body = "\n".join(
            [f"test t{i} ... ok" for i in range(1, n + 1)]
            + [f"test t{i} ... FAILED" for i in range(n + 1, 5)])
        return 101, ("    Running tests/t.rs (t/t-1)\nrunning 4 tests\n"
                     + body
                     + f"\ntest result: FAILED. {n} passed; {4 - n} failed")

    deps = replace(
        _deps(fill_calls, logs=logs, run_cargo=_cargo,
              reset=lambda *a, **k: ["src/a.rs::wrongfn::"]),
        task2_fill_fns_one_by_one=_fill)
    _run(deps, tmp_path, monkeypatch)
    dispatched = [c for c in fill_calls if _WRONG in c]
    # Cap is 2; without the release the third dispatch is impossible (pinned
    # by test_refill_target_exhausts_after_cap). The release reopens it.
    assert len(dispatched) >= 3, (fill_calls, logs)
    assert any("also reopened in the loop-local frontier filter" in ln
               for ln in logs), logs


def test_a_release_naming_another_unit_reopens_nothing(
        tmp_path, monkeypatch) -> None:
    """Control: the reopening is keyed on the RELEASED identities, not on the
    release event — a release for a different unit leaves the capped one
    exhausted (the pre-fix outcome for every unit, now the outcome only for
    units the disk did not name)."""
    fill_calls: list[frozenset[FnLocator]] = []
    logs: list[str] = []

    def _fill(**kw: Any) -> ClusterFillResult:
        fill_calls.append(frozenset(kw.get("tdd_frontier_keys") or ()))
        return ClusterFillResult(attempts=1, fns_filled=1)

    outs = iter([1, 2, 3, 4, 5, 6])

    def _cargo(*a: Any, **k: Any):
        n = min(next(outs, 6), 4)
        body = "\n".join(
            [f"test t{i} ... ok" for i in range(1, n + 1)]
            + [f"test t{i} ... FAILED" for i in range(n + 1, 5)])
        return 101, ("    Running tests/t.rs (t/t-1)\nrunning 4 tests\n"
                     + body
                     + f"\ntest result: FAILED. {n} passed; {4 - n} failed")

    deps = replace(
        _deps(fill_calls, logs=logs, run_cargo=_cargo,
              reset=lambda *a, **k: ["src/other.rs::someone_else::"]),
        task2_fill_fns_one_by_one=_fill)
    _run(deps, tmp_path, monkeypatch)
    assert len([c for c in fill_calls if _WRONG in c]) == 2, fill_calls


def test_tainted_sweep_charges_no_refill_attempt(tmp_path, monkeypatch) -> None:
    """A sweep whose counters and printed verdicts disagree (no crash to
    explain the gap) dispatches, but the dispatch is not CHARGED — the unit
    outlives the cap by exactly the tainted iterations."""
    fill_calls: list[frozenset[FnLocator]] = []
    logs: list[str] = []
    deps = _deps(fill_calls, logs=logs,
                 run_cargo=lambda *a, **k: (101, _TAINTED_FAIL))
    _run(deps, tmp_path, monkeypatch)
    # Cap is 2, stall budget 3: with charging gated on a reconciled sweep the
    # unit is dispatched on every iteration the loop runs (stall exit ends
    # the run), not capped after two.
    assert len([c for c in fill_calls if _WRONG in c]) >= 3, fill_calls
    assert any("VERDICT MISMATCH" in ln for ln in logs)


def test_tainted_sweep_writes_no_floor(tmp_path, monkeypatch) -> None:
    floors: list[dict] = []
    logs: list[str] = []
    deps = replace(
        _deps([], logs=logs, run_cargo=lambda *a, **k: (101, _TAINTED_FAIL)),
        save_tdd_floor=lambda pd, cl, d: floors.append(d))
    _run(deps, tmp_path, monkeypatch)
    assert floors == [], floors


def test_consistent_sweep_still_writes_the_floor(tmp_path, monkeypatch) -> None:
    floors: list[dict] = []
    logs: list[str] = []
    deps = replace(
        _deps([], logs=logs,
              run_cargo=lambda *a, **k: (101, _CONSISTENT_FAIL)),
        save_tdd_floor=lambda pd, cl, d: floors.append(d))
    _run(deps, tmp_path, monkeypatch)
    assert floors, "a reconciled sweep must persist the floor"


def test_red_build_refusal_stops_after_three_identical_digests(
        tmp_path, monkeypatch) -> None:
    """Three consecutive sweeps under an IDENTICAL compile digest end the
    cluster loudly instead of running out the stall budget on 0-test sweeps
    — nightly's three iterations at 0/113 under the same two E0605 casts."""
    logs: list[str] = []
    calls = [0]

    def _cargo(*a: Any, **k: Any):
        calls[0] += 1
        return 101, _RED_BUILD
    deps = _deps([], logs=logs, run_cargo=_cargo)
    _run(deps, tmp_path, monkeypatch, max_stalls="10")
    assert any("RED — the cluster cannot compile" in ln for ln in logs), logs
    # Three sweeps, no fourth: the refusal beat the (deliberately large)
    # stall budget.
    assert calls[0] == 3, calls


def test_changing_digests_do_not_trip_the_red_budget(
        tmp_path, monkeypatch) -> None:
    """Control: a red build whose errors CHANGE between sweeps is being
    repaired — the budget must not fire."""
    logs: list[str] = []
    outs = iter([
        _RED_BUILD,
        _RED_BUILD.replace("E0605", "E0308"),
        _RED_BUILD.replace("backtrace", "chain"),
        _CONSISTENT_FAIL,
    ])
    deps = _deps([], logs=logs,
                 run_cargo=lambda *a, **k: (101, next(outs, _CONSISTENT_FAIL)))
    _run(deps, tmp_path, monkeypatch)
    assert not any("RED — the cluster cannot compile" in ln for ln in logs), logs


def test_stall_exit_convicts_only_the_dispatched(tmp_path, monkeypatch) -> None:
    """The blanket conviction is windowed: the exit line accounts for what
    the stalled window actually dispatched."""
    fill_calls: list[frozenset[FnLocator]] = []
    logs: list[str] = []
    deps = _deps(fill_calls, logs=logs,
                 run_cargo=lambda *a, **k: (101, _CONSISTENT_FAIL))
    _run(deps, tmp_path, monkeypatch)
    exit_lines = [ln for ln in logs if "pass_count plateau" in ln]
    assert exit_lines, logs
    assert "were dispatched in the stalled window" in exit_lines[0]
