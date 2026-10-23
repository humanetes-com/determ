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
         logs: list[str] | None = None,
         cargo: tuple[int, str] | None = None,
         reference: Any = None, rows: Any = None,
         compile_errors: Any = None, score_only: bool = False,
         sweeps: list[str] | None = None) -> ClusterFillResult:
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
        run_cargo=lambda *a, **k: (
            sweeps.append(str(a[0])) if sweeps is not None else None,
            (101, _FLAT) if cargo is None else cargo)[1],
        scan_unimplemented_for_cluster=lambda *a, **k: StubInventory(),
        scan_unimplemented_functions=lambda *a, **k: StubInventory(),
        task2_fill_fns_one_by_one=_fill,
        behaviour_specs_for=(lambda pd, cl: _specs()) if owed else None,
        reference_measurement=reference,
        route_ledger_rows=rows,
        last_compile_errors=compile_errors,
    )
    return run_phase_c_tdd(
        deps, project_dir=tmp_path, target_src_dir=tmp_path, tests_dir_files=[],
        cluster_tests=[("tests/t.rs", "fn t1() {}")], module="m",
        cluster=_cluster(), check_cmd="cargo check", cluster_env={},
        leaf_counts=None, fr_path=None,
        test_cmd_builder=lambda stems, include_lib=False: "cargo test",
        tag="[stable]", score_only=score_only)


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
    # A crash spent no budget: it is named, not called exhausted, so the
    # generator neither checkpoints it nor reports it as exhausted.
    assert result.crashed.startswith("UnicodeDecodeError")
    assert "Traceback" in result.crash_traceback
    assert not result.exhausted and result.failed_tests
    assert len(fills) == 2, "no fill after the crash"
    crashed = [d for ev, d in events if ev == "task2a.cluster_crashed"]
    assert len(crashed) == 1
    assert crashed[0]["cluster"] == "stable"
    assert "UnicodeDecodeError" in crashed[0]["exception"]
    assert "Traceback" in crashed[0]["traceback"]


def test_score_only_reads_the_entry_sweep_and_fills_nothing(
        tmp_path: Path, monkeypatch) -> None:
    """After an earlier cluster's pipeline crash (decided 2026-10-17), a
    cluster is read by one sweep, reports its red list, and makes no fill
    call. Control: the same cluster without the flag fills."""
    events: list[tuple[str, dict]] = []
    fills: list[dict] = []
    logs: list[str] = []
    sweeps: list[str] = []
    result = _run(tmp_path, monkeypatch, owed=True, events=events, fills=fills,
                  logs=logs, score_only=True, sweeps=sweeps)
    assert fills == []
    assert len(sweeps) == 1, f"one sweep, nothing after it: {sweeps}"
    assert result.scored_only and not result.exhausted and not result.crashed
    assert result.failed_tests == frozenset({"t1"})
    scored = [d for ev, d in events if ev == "phase_c.tdd.score_only"]
    assert scored and scored[0]["failed"] == ["t1"]
    assert any("SCORED ONLY" in line and "red: t1" in line for line in logs)

    control_fills: list[dict] = []
    _run(tmp_path, monkeypatch, owed=True, events=[], fills=control_fills)
    assert control_fills, "negative control: without the flag the cluster fills"


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
    assert not [d for ev, d in events if ev == "task2a.cluster_crashed"]


def test_with_sessions_on_each_function_climbs_the_ladder_before_it_is_spent(
        tmp_path: Path, monkeypatch) -> None:
    """The ladder: `render` is offered on the unit rung `DEFAULT_MISSES`
    times (those offers land nothing and no route planned them). The
    session slice in this script is empty, so the session rung is absent
    once — not spent by another `DEFAULT_MISSES` of offers no route
    planned — and only then does `fmt` follow. The red test is never
    offered. The cluster ends on the exit, not on the backstop."""
    monkeypatch.setenv("SLICE_SESSION", "1")
    events: list[tuple[str, dict]] = []
    fills: list[dict] = []
    _run(tmp_path, monkeypatch, owed=True, events=events, fills=fills)
    asked = [d for ev, d in events if ev == "phase_c.tdd.asked"]
    k = DEFAULT_MISSES
    assert [a["missed"] for a in asked[:k]] == [["src/a.rs::render"]] * k
    assert asked[k]["missed"] == ["src/a.rs::render"]
    assert any("route_absent" in s for s in asked[k]["spent_by_misses"])
    assert asked[k + 1]["missed"] == ["src/a.rs::fmt"]
    assert len(asked) == 2 * (k + 1)
    owed = [d for ev, d in events if ev == "phase_c.tdd.owed"]
    rungs = [d["tests"]["t1"].get("rungs") for d in owed]
    assert rungs[0] == {"src/a.rs::render": "unit"}
    assert rungs[k] == {"src/a.rs::render": "session"}
    assert rungs[k + 1] == {"src/a.rs::fmt": "unit"}
    iters = [d for ev, d in events if ev == "phase_c.tdd.iter"]
    assert iters[-1]["kind"] == "cluster_failed"
    assert len(iters) == 2 * (k + 1) + 1
    ledger = [d for ev, d in events if ev == "phase_c.tdd.red_ledger"]
    assert len(ledger) == 1
    assert [e["class"] for e in ledger[0]["entries"]] == ["never_offered"]


_GREEN = (
    "running 1 test\n"
    "test t1 ... ok\n\n"
    "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n")
_FLOOR = (
    "     Running tests/t.rs (target/debug/deps/t-1)\n"
    "\nrunning 2 tests\n"
    "test t_ok ... ok\n"
    "test t1 ... FAILED\n\n"
    "failures:\n\n"
    "---- t1 stdout ----\n"
    "thread 't1' panicked at tests/t.rs:3:5:\n"
    "assertion `left == right` failed\n\n"
    "failures:\n    t1\n\n"
    "test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out\n")


def test_a_green_cluster_emits_an_empty_red_ledger(
        tmp_path: Path, monkeypatch) -> None:
    events: list[tuple[str, dict]] = []
    fills: list[dict] = []
    result = _run(tmp_path, monkeypatch, owed=True, events=events, fills=fills,
                  cargo=(0, _GREEN))
    ledger = [d for ev, d in events if ev == "phase_c.tdd.red_ledger"]
    assert len(ledger) == 1 and ledger[0]["close_kind"] == "green"
    assert ledger[0]["entries"] == []
    assert not result.failed_tests and not result.exhausted
    iters = [d for ev, d in events if ev == "phase_c.tdd.iter"]
    assert iters[-1]["kind"] == "pass"


def test_a_first_sweep_floor_names_the_originals_failure_withdrawn(
        tmp_path: Path, monkeypatch) -> None:
    """The original fails `t1`. The floor closes on the first sweep, before
    the loop has a slice, and the ledger says withdrawn."""
    from pipeline_core.domain.reference_floor import (
        ClusterReference,
        ReferenceMeasurement,
        pairs_of,
    )
    from pipeline_core.domain.same_failure import ReferenceFailure
    from pipeline_core.domain.test_results import parse_binary_results
    passed, failed, _, _ = pairs_of(parse_binary_results(_FLOOR))
    ref = ReferenceMeasurement(key="k", clusters={"stable": ClusterReference(
        cluster="stable", passed=passed, failures=dict.fromkeys(failed, ReferenceFailure()),
        announced=len(passed) + len(failed),
        reported=len(passed) + len(failed), rc=101)})
    events: list[tuple[str, dict]] = []
    fills: list[dict] = []
    _run(tmp_path, monkeypatch, owed=True, events=events, fills=fills,
         cargo=(101, _FLOOR), reference=lambda _pd: ref)
    iters = [d for ev, d in events if ev == "phase_c.tdd.iter"]
    assert iters[-1]["kind"] == "reference_floor"
    ledger = [d for ev, d in events if ev == "phase_c.tdd.red_ledger"]
    assert len(ledger) == 1 and ledger[0]["close_kind"] == "reference floor"
    assert [e["class"] for e in ledger[0]["entries"]] == ["withdrawn"]
    assert not fills, "the floor closes before a fill"


def test_a_compile_miss_is_repaired_once_and_the_diagnostic_reaches_the_next_ask(
        tmp_path: Path, monkeypatch) -> None:
    """The first offer that does not compile is not a miss: the next ask
    carries the compiler's `Invented` diagnostic, and one failed repair
    then counts as one miss. A compiling reply (`fmt`) counts its misses
    and no repair turn."""
    from pipeline_core.domain.fill_schedule import (
        RouteKind,
        RouteLedgerRow,
        RunOutcome,
    )
    monkeypatch.setenv("SLICE_SESSION", "0")

    def _row(iteration: int, fn: str, *, calls: int,
             compile: bool) -> RouteLedgerRow:
        return RouteLedgerRow(
            kind=RouteKind.PER_FN, label="per-fn src/a.rs",
            targets=(("src/a.rs", fn),), bodies_sha="b", oracle_fp="o",
            outcome=RunOutcome.DECLINED, iteration=iteration,
            cluster="stable", model_calls=calls,
            classes={"compile": 1} if compile else {"wrong": 1})

    def _rows(_name: str, _cluster: str) -> list[RouteLedgerRow]:
        rows = [_row(i, "render", calls=calls, compile=True)
                for i, calls in enumerate((1, 2, 2, 2), start=1)]
        base = 1 + DEFAULT_MISSES + 1
        rows.extend(_row(base + j, "fmt", calls=1, compile=False)
                    for j in range(DEFAULT_MISSES))
        return rows

    events: list[tuple[str, dict]] = []
    fills: list[dict] = []
    _run(tmp_path, monkeypatch, owed=True, events=events, fills=fills,
         rows=_rows,
         compile_errors=lambda _pd: {
             _TARGET: "cannot find type `Invented` in this scope"})
    asked = [d for ev, d in events if ev == "phase_c.tdd.asked"]
    k = DEFAULT_MISSES
    assert [a["missed"] for a in asked[:k + 1]] == [["src/a.rs::render"]] * (k + 1)
    assert not asked[0]["spent_by_misses"]
    assert any("compile" in s for s in asked[k]["spent_by_misses"])
    note = fills[1]["tdd_frontier_tests"].unit_notes[_TARGET]
    assert "Invented" in note and "did not compile" in note
    assert asked[k + 1]["missed"] == ["src/a.rs::fmt"]
    assert any("no_landing" in s for s in asked[k + k]["spent_by_misses"])
    assert not any("compile" in s for s in asked[k + k]["spent_by_misses"])
    ledger = [d for ev, d in events if ev == "phase_c.tdd.red_ledger"]
    assert [e["class"] for e in ledger[0]["entries"]] == ["model_limit"]
