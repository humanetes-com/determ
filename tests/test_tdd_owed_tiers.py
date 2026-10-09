"""The driver owes a red test's narrowest tier to the batch, records the ask,
and ends the cluster FAILED when nothing new can be asked — with every
plateau stop off, as production runs (`TDD_MAX_STALLS=0`, flat-score guard
off). Before this, such a run looped until an external TERM: three cold
starts since 2026-10-04 ended that way inside `stable`, and nightly was never
scored."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pipeline_core.application.reconstruct.phase_c_tdd import (
    PhaseCTddDeps,
    run_phase_c_tdd,
)
from pipeline_core.domain.attribution import AttributionResult, FailureKind
from pipeline_core.domain.candidate_tiers import DEFAULT_MISSES
from pipeline_core.domain.models import BuildCluster, ClusterFillResult, FnLocator
from pipeline_core.domain.stub_inventory import StubInventory

_FLAT = (
    "running 4 tests\n"
    "test t1 ... FAILED\n\n"
    "failures:\n\n"
    "---- t1 stdout ----\n"
    "thread 't1' panicked at tests/t.rs:3:5:\n"
    "assertion `left == right` failed\n"
    "  left: \"msg left: 1 right: 2\"\n"
    " right: \"msg (1 vs 2)\"\n\n"
    "failures:\n    t1\n\n"
    "test result: FAILED. 3 passed; 1 failed; 0 ignored\n")
_TARGET = FnLocator("src/a.rs", "render")
_SHARED = FnLocator("src/a.rs", "fmt")


def _cluster() -> BuildCluster:
    return BuildCluster(
        name="stable", env_extra={}, tarpaulin_exclude_files=[],
        excluded_cfg=[], active_cfg=[], prompt_nightly_rule="")


def _specs() -> list[Any]:
    # `render` runs only under feature F; `fmt` under F and G.
    return [SimpleNamespace(behaviour_id="e1", features=("F",),
                            exercises={"src/a.rs::render", "src/a.rs::fmt"}),
            SimpleNamespace(behaviour_id="e2", features=("G",),
                            exercises={"src/a.rs::fmt"})]


def _run(tmp_path: Path, monkeypatch, *, owed: bool,
         events: list[tuple[str, dict]], fills: list[dict],
         raise_on: int = 0, exc: BaseException | None = None,
         logs: list[str] | None = None) -> ClusterFillResult:
    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "0")
    monkeypatch.setenv("TDD_FLAT_SCORE_REPEATS", "0")
    monkeypatch.setenv("TDD_MAX_STALLS", "0" if owed else "3")
    monkeypatch.setenv("PHASE_C_TDD_BACKSTOP", "20")

    def _fill(**kw: Any) -> ClusterFillResult:
        fills.append(kw)
        if raise_on and len(fills) == raise_on and exc is not None:
            raise exc
        return ClusterFillResult(attempts=1, dispatched_fns=tuple(
            (u.file, u.fn_name) for u in kw.get("tdd_frontier_keys") or ()))

    deps = PhaseCTddDeps(
        attribute_failures=lambda *a, **k: AttributionResult(
            {_TARGET: {"t1"}, _SHARED: {"t1"}}, FailureKind.RUNTIME,
            {_TARGET: {"t1"}, _SHARED: {"t1"}}),
        build_test_reverse_map=lambda *a, **k: {},
        filter_test_targets=lambda frontier, src, **_k: frontier,
        check_graceful_stop=lambda *a, **k: None,
        check_auto_operator=lambda *a, **k: None,
        converge_in_cluster=lambda *a, **k: None,
        count_relevant_notes=lambda *a, **k: 0,
        effective_stalls=lambda budget, module: budget,
        emit=lambda ev, **k: events.append((ev, k.get("data") or {})),
        log=lambda *a, **k: (logs.append(str(a[0])) if logs is not None and a
                             else None),
        order_frontier=lambda eligible, counts_: list(eligible.items()),
        record_tdd_regression=lambda *a, **k: "",
        reset_exhaustion_flags=lambda *a, **k: [],
        run_cargo=lambda *a, **k: (101, _FLAT),
        scan_unimplemented_for_cluster=lambda *a, **k: StubInventory(),
        scan_unimplemented_functions=lambda *a, **k: StubInventory(),
        task2_fill_fns_one_by_one=_fill,
        behaviour_specs_for=(lambda pd, cl: _specs()) if owed else None,
    )
    return run_phase_c_tdd(
        deps, project_dir=tmp_path, target_src_dir=tmp_path, tests_dir_files=[],
        cluster_tests=[("tests/t.rs", "fn t1() {}")], module="m",
        cluster=_cluster(), check_cmd="cargo check", cluster_env={},
        leaf_counts=None, fr_path=None,
        test_cmd_builder=lambda stems, include_lib=False: "cargo test",
        tag="[stable]")


def test_the_narrowest_tier_is_owed_and_offers_landing_nothing_end_the_cluster_failed(
        tmp_path: Path, monkeypatch) -> None:
    """The scripted fill lands no body. Each owed function is offered
    `DEFAULT_MISSES` fills under the unchanged evidence, then spent; the
    next tier follows; then the cluster ends FAILED, on the exit and not on
    the backstop. Before, one dispatch spent a function, so a single refused
    reply ended its turn."""
    monkeypatch.setenv("SLICE_SESSION", "0")   # one rung: no session route
    events: list[tuple[str, dict]] = []
    fills: list[dict] = []
    result = _run(tmp_path, monkeypatch, owed=True, events=events, fills=fills)

    owed = [d for ev, d in events if ev == "phase_c.tdd.owed"]
    assert owed[0]["owed"] == ["src/a.rs::render"], \
        "the function only one feature's examples run is the first tier"
    assert _TARGET in fills[0]["due"] and _SHARED not in fills[0]["due"]
    asked = [d for ev, d in events if ev == "phase_c.tdd.asked"]
    assert [a["missed"] for a in asked[:DEFAULT_MISSES]] == \
        [["src/a.rs::render"]] * DEFAULT_MISSES
    assert asked[DEFAULT_MISSES - 1]["spent_by_misses"]
    assert asked[DEFAULT_MISSES]["missed"] == ["src/a.rs::fmt"]
    assert not any(a["new"] for a in asked), "nothing landed, nothing asked"

    failed = [d for ev, d in events if ev == "phase_c.tdd.cluster_failed"]
    assert len(failed) == 1, "with nothing new to ask, the cluster ends — FAILED"
    iters = [d for ev, d in events if ev == "phase_c.tdd.iter"]
    assert iters[-1]["kind"] == "cluster_failed"
    assert len(iters) == 2 * DEFAULT_MISSES + 1
    assert result.exhausted and result.failed_tests
    ledger = [d for ev, d in events if ev == "phase_c.tdd.red_ledger"]
    assert len(ledger) == 1 and ledger[0]["close_kind"] == "cluster_failed"
    assert [e["class"] for e in ledger[0]["entries"]] == ["never_offered"], \
        "no route row planned a run for the owed functions: the pipeline's"


def test_the_owed_function_is_handed_the_red_tests_words(
        tmp_path: Path, monkeypatch) -> None:
    """The evidence promise, driver half: the engine's frontier for the owed
    unit names the red test, and the frontier carries what that test said
    (the observed value beside the expected one). The render half — a
    frontier test's words printed in the prompt — is
    `tests/test_refill_evidence.py`."""
    events: list[tuple[str, dict]] = []
    fills: list[dict] = []
    _run(tmp_path, monkeypatch, owed=True, events=events, fills=fills)
    ft = fills[0]["tdd_frontier_tests"]
    assert "t1" in ft.tests_for(_TARGET)
    words = ft.evidence_for("t1")
    assert "msg left: 1 right: 2" in words and "msg (1 vs 2)" in words


def test_without_a_baked_suite_the_dispatch_is_todays(
        tmp_path: Path, monkeypatch) -> None:
    """Control: a driver with no suite loader owes nothing, never exits on
    exhaustion, and ends on today's plateau stop."""
    events: list[tuple[str, dict]] = []
    fills: list[dict] = []
    _run(tmp_path, monkeypatch, owed=False, events=events, fills=fills)
    assert not [ev for ev, _ in events if ev.startswith("phase_c.tdd.owed")]
    assert not [ev for ev, _ in events if ev == "phase_c.tdd.cluster_failed"]
    assert not fills[0].get("due")
    iters = [d for ev, d in events if ev == "phase_c.tdd.iter"]
    assert iters[-1]["kind"] == "stall_exit"


def test_a_fill_that_raises_ends_the_cluster_failed_with_its_traceback(
        tmp_path: Path, monkeypatch) -> None:
    """Run `39786-fd38e72f`: a `UnicodeDecodeError` inside fill 5 aborted
    stable and no record kept the traceback or the red tests. Now the
    cluster ends `cluster_failed` (kind `crashed`) with both, and returns."""
    events: list[tuple[str, dict]] = []
    fills: list[dict] = []
    logs: list[str] = []
    result = _run(tmp_path, monkeypatch, owed=True, events=events, fills=fills,
                  raise_on=2, logs=logs,
                  exc=UnicodeDecodeError("utf-8", b"\xc0", 0, 1, "invalid start byte"))
    failed = [d for ev, d in events if ev == "phase_c.tdd.cluster_failed"]
    assert len(failed) == 1 and failed[0]["kind"] == "crashed"
    assert "UnicodeDecodeError" in failed[0]["exception"]
    assert "Traceback" in failed[0]["traceback"]
    assert failed[0]["tests"], "the red list of the last sweep"
    assert any("cluster_failed (crashed)" in line and "Traceback" in line
               for line in logs)
    assert result.exhausted and result.failed_tests
    assert len(fills) == 2, "no fill after the crash"


def test_control_a_stop_still_leaves_the_process(
        tmp_path: Path, monkeypatch) -> None:
    """The graceful stop is `SystemExit`: it is not a crash of the cluster."""
    import pytest
    events: list[tuple[str, dict]] = []
    fills: list[dict] = []
    with pytest.raises(SystemExit):
        _run(tmp_path, monkeypatch, owed=True, events=events, fills=fills,
             raise_on=2, exc=SystemExit(0))
    assert not [d for ev, d in events if ev == "phase_c.tdd.cluster_failed"]


def test_with_sessions_on_each_function_climbs_the_ladder_before_it_is_spent(
        tmp_path: Path, monkeypatch) -> None:
    """The ladder: `render` is offered on the unit rung, then on the session
    rung (no session route exists in this script, so each rung is spent by
    its misses), and only then does `fmt` follow. The cluster still ends on
    the exit, not on the backstop."""
    monkeypatch.setenv("SLICE_SESSION", "1")
    events: list[tuple[str, dict]] = []
    fills: list[dict] = []
    _run(tmp_path, monkeypatch, owed=True, events=events, fills=fills)
    asked = [d for ev, d in events if ev == "phase_c.tdd.asked"]
    k = DEFAULT_MISSES
    assert [a["missed"] for a in asked[:2 * k]] == [["src/a.rs::render"]] * (2 * k)
    assert asked[2 * k]["missed"] == ["src/a.rs::fmt"]
    owed = [d for ev, d in events if ev == "phase_c.tdd.owed"]
    rungs = [d["tests"]["t1"].get("rungs") for d in owed[:2 * k]]
    assert rungs[0] == {"src/a.rs::render": "unit"}
    assert rungs[k] == {"src/a.rs::render": "session"}
    iters = [d for ev, d in events if ev == "phase_c.tdd.iter"]
    assert iters[-1]["kind"] == "cluster_failed"
    assert len(iters) == 4 * k + 1
