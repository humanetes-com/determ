"""Pins for the red-build frontier narrowing in the TDD driver.

While a cluster's tree does not compile, no test reports a verdict, so the
attribution frontier is reachability rather than evidence: `nightly` spent 8.7 h
and 354 model calls on a 72-fn frontier of which the compiler had implicated
none. The driver therefore narrows to the fns rustc names — but the narrowing
ADDS keys, and what it adds must still obey the two rules the frontier already
had, and its emptiness must not be read as a verdict on the first sweep.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.phase_c_tdd import (
    PhaseCTddDeps,
    run_phase_c_tdd,
)
from pipeline_core.domain.attribution import AttributionResult, FailureKind
from pipeline_core.domain.models import BuildCluster, ClusterFillResult, FnLocator
from pipeline_core.domain.stub_inventory import StubInventory

_LIB = FnLocator("src/error.rs", "construct")
_OTHER = FnLocator("src/context.rs", "ext_context")
_TEST_FN = FnLocator("src/error.gen_tests_a.rs", "test_downcast")

# A sweep in which the LIB itself did not build — the seeded-`nightly` shape.
_RED = (
    "error[E0599]: no method named `context` found for type parameter `E`\n"
    " --> src/context.rs:24:14\n"
    "error: could not compile `anyhow` (lib) due to 1 previous error\n"
)
# The lib built and a test binary did not: bodies live in the lib, so no fill
# explains this one. It is the cause-A layout retrigger's own case.
_RED_TEST_BIN = (
    "error[E0277]: `NonNull<ErrorImpl>` cannot be sent between threads safely\n"
    " --> tests/test_autotrait.rs:9:5\n"
    'error: could not compile `anyhow` (test "test_autotrait") due to 1 previous error\n'
)


def _cluster() -> BuildCluster:
    return BuildCluster(
        name="nightly", env_extra={}, tarpaulin_exclude_files=[],
        excluded_cfg=[], active_cfg=[], prompt_nightly_rule="")


def _run(monkeypatch, tmp_path: Path, *, implicated, events, dispatched,
         retrigger=None, red: str = _RED, run_cargo=None, order=None,
         logs=None) -> ClusterFillResult:
    """Drive the loop over a permanently red tree with a two-fn frontier."""
    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "0")
    monkeypatch.setenv("TDD_MAX_STALLS", "4")
    (tmp_path / "src").mkdir(exist_ok=True)

    def _order(eligible, counts):
        dispatched.append(dict(eligible))
        return list(eligible.items()) if order is None else order(eligible)

    deps = PhaseCTddDeps(
        attribute_failures=lambda *a, **k: AttributionResult(
            {_LIB: {"t1"}, _OTHER: {"t1"}}, FailureKind.NO_FRONTIER,
            {_LIB: {"t1"}, _OTHER: {"t1"}}),
        build_test_reverse_map=lambda *a, **k: {},
        # The real filter: a gen_tests fn is the ORACLE and may never be filled.
        filter_test_targets=lambda frontier, src: {
            k: v for k, v in frontier.items() if ".gen_tests_" not in k.file},
        check_graceful_stop=lambda *a, **k: None,
        check_auto_operator=lambda *a, **k: None,
        converge_in_cluster=lambda *a, **k: None,
        count_relevant_notes=lambda *a, **k: 0,
        effective_stalls=lambda budget, module: budget,
        emit=lambda ev, **k: events.append((ev, k.get("data") or {})),
        log=(lambda *a, **k: None) if logs is None else logs.append,
        order_frontier=_order,
        record_tdd_regression=lambda *a, **k: "",
        reset_exhaustion_flags=lambda *a, **k: [],
        run_cargo=run_cargo or (lambda *a, **k: (101, red)),
        scan_unimplemented_for_cluster=lambda *a, **k: StubInventory(),
        scan_unimplemented_functions=lambda *a, **k: StubInventory(),
        task2_fill_fns_one_by_one=lambda **kw: ClusterFillResult(attempts=1),
        checkpoint_fill_tree=lambda *a, **k: None,
        active_test_inventory=lambda pd, cl, stems, lib=False: (
            2, frozenset({"t1", "t2"})),
        retrigger_layout=retrigger,
        implicated_fns=lambda out, tsd: list(implicated),
    )
    return run_phase_c_tdd(
        deps, project_dir=tmp_path, target_src_dir=tmp_path, tests_dir_files=[],
        cluster_tests=[("tests/t.rs", "fn t1() {}")], module="m",
        cluster=_cluster(), check_cmd="cargo check", cluster_env={},
        leaf_counts=None, fr_path=None,
        test_cmd_builder=lambda stems, include_lib=False: "cargo test",
        tag="[nightly]")


def test_only_the_implicated_fn_is_dispatched_while_red(
        tmp_path: Path, monkeypatch) -> None:
    events: list[tuple[str, dict]] = []
    dispatched: list[dict[Any, Any]] = []
    _run(monkeypatch, tmp_path, implicated=[_LIB], events=events,
         dispatched=dispatched)
    assert dispatched and all(set(d) == {_LIB} for d in dispatched), \
        "a red sweep must aim at the compiler's fn, not the reachable ones"
    narrowed = [d for ev, d in events
                if ev == "phase_c.tdd.compile_blocked_frontier"]
    assert narrowed and narrowed[0]["withheld"] == 1
    assert narrowed[0]["aimed"] == 1


def test_an_implicated_test_fn_is_never_dispatched(
        tmp_path: Path, monkeypatch) -> None:
    """A red build's diagnostics point AT the test binaries, so the narrowing
    adds gen_tests fns — behind the test-oracle filter, which ran earlier. An
    error inside a test is not a licence to rewrite the thing being scored by."""
    events: list[tuple[str, dict]] = []
    dispatched: list[dict[Any, Any]] = []
    _run(monkeypatch, tmp_path, implicated=[_LIB, _TEST_FN], events=events,
         dispatched=dispatched)
    assert dispatched, "the lib fn is still actionable"
    assert all(_TEST_FN not in d for d in dispatched)
    assert all(set(d) == {_LIB} for d in dispatched)


def test_first_unaimable_red_sweep_does_not_stop_the_cluster(
        tmp_path: Path, monkeypatch) -> None:
    """`implicated_fns` is wired to the TEXT scan here (a raw sweep carries no
    structured diagnostics), and that scan attributes a macro-expansion error to
    NOTHING. So "nothing implicated" is a blind spot as often as a diagnosis —
    stopping on the first one preempts the cause-A layout retrigger, which is
    the mechanism built for a red tree and fires on the FIRST broken sweep."""
    events: list[tuple[str, dict]] = []
    fired: list[int] = []

    def _retrig(*a: Any, **k: Any):
        from pipeline_core.application.retrigger_layout import RetriggerResult
        fired.append(1)
        return RetriggerResult(fired=False, reason="no_private_layout")

    _run(monkeypatch, tmp_path, implicated=[], events=events, dispatched=[],
         retrigger=_retrig, red=_RED_TEST_BIN)
    exits = [d for ev, d in events if ev == "phase_c.tdd.compile_blocked_exit"]
    assert fired, "the layout retrigger must get its sweep before the exit"
    assert exits, "a red tree that stays unaimable still has to stop"
    assert exits[0]["iter"] == 2
    assert exits[0]["broken_sweeps"] == 2
    assert exits[0]["compile_errors"] == 1


def test_compile_blocked_exit_reports_the_baseline_as_broken(
        tmp_path: Path, monkeypatch) -> None:
    """The caller checkpoints a cluster on this result, and a checkpoint is read
    on every later run as "already reconstructed — skipping". So the stop reason
    has to survive the return: on 2026-08-01 `nightly` exited here at pass=0 and
    was still recorded `done — validated`, and the next run skipped it straight
    to the merge. `cargo check` could not have caught it either — it builds the
    lib, and what failed were the TEST binaries."""
    events: list[tuple[str, dict]] = []

    def _retrig(*a: Any, **k: Any):
        from pipeline_core.application.retrigger_layout import RetriggerResult
        return RetriggerResult(fired=False, reason="no_private_layout")

    result = _run(monkeypatch, tmp_path, implicated=[], events=events,
                  dispatched=[], retrigger=_retrig, red=_RED_TEST_BIN)
    assert [d for ev, d in events if ev == "phase_c.tdd.compile_blocked_exit"]
    assert result.baseline_broken is True


def test_a_cluster_that_finishes_normally_is_not_flagged_broken(
        tmp_path: Path, monkeypatch) -> None:
    """The flag must mean the compile-blocked exit specifically — every other
    exit (stall, budget, green) leaves a cluster that IS checkpointable."""
    events: list[tuple[str, dict]] = []
    result = _run(monkeypatch, tmp_path, implicated=[_LIB], events=events,
                  dispatched=[])
    assert result.baseline_broken is False


# --- the verdict is the build phase, not the whole capture -----------------

# A sweep in which every target the command asked for ran (the harness's
# `tests/t.rs` plus the `--lib` binary) and a test printed a NESTED cargo's
# `could not compile` — the shape of run `2131920-86818224`'s iter-2 sweep,
# where trybuild's build of the crate without the cluster's `--cfg` failed
# `E0063` while all 32 binaries completed.
_NESTED = (
    "   Compiling anyhow v1.0.104 (/w)\n"
    "    Finished `test` profile [unoptimized + debuginfo] target(s)\n"
    "     Running unittests src/lib.rs (target/debug/deps/anyhow-1)\n"
    "\n"
    "running 0 tests\n"
    "\n"
    "test result: ok. 0 passed; 0 failed; 0 ignored\n"
    "\n"
    "     Running tests/t.rs (target/debug/deps/t-1)\n"
    "\n"
    "running 2 tests\n"
    "test t1 ...    Compiling anyhow v1.0.104 (/scratch)\n"
    "error[E0063]: missing field `object_backtrace` in initializer of `ErrorVTable`\n"
    " --> /w/src/error.rs:202:41\n"
    "error: could not compile `anyhow` (lib) due to 1 previous error\n"
    "FAILED\n"
    "test t2 ... ok\n"
    "\n"
    "test result: FAILED. 1 passed; 1 failed; 0 ignored\n"
)


def test_a_nested_build_inside_a_running_test_does_not_narrow_the_frontier(
        tmp_path: Path, monkeypatch) -> None:
    """Every expected target ran, so there is no red build to narrow to: the
    attribution frontier (both fns) is dispatched whole, no `build is RED`
    line, no `compile_blocked_frontier` event — even though the text scan
    would implicate a fn from the nested world's diagnostic."""
    events: list[tuple[str, dict]] = []
    dispatched: list[dict[Any, Any]] = []
    logs: list[str] = []
    _run(monkeypatch, tmp_path, implicated=[_LIB], events=events,
         dispatched=dispatched, run_cargo=lambda *a, **k: (101, _NESTED),
         logs=logs)
    assert dispatched and all(set(d) == {_LIB, _OTHER} for d in dispatched), \
        dispatched
    assert not [d for ev, d in events
                if ev == "phase_c.tdd.compile_blocked_frontier"]
    assert not any("build is RED" in ln for ln in logs)
    iters = [d for ev, d in events if ev == "phase_c.tdd.iter"]
    assert iters and iters[0]["build_red"] is False
    assert iters[0]["build_missing"] == []
    assert iters[0]["frontier_size"] == 2


def test_control_the_whole_capture_substring_narrows_to_the_nested_builds_fn(
        tmp_path: Path, monkeypatch) -> None:
    """The pre-fix verdict injected: a build whose `build_output` is the WHOLE
    capture reads the nested `could not compile` as this crate's, and the
    driver narrows to the one fn the nested build implicated — `2131920`'s
    `frontier=1`, `62 withheld` shape."""
    from pipeline_core.application.reconstruct import phase_c_tdd
    from pipeline_core.domain.test_results import SweepBuild, parse_binary_results

    def _whole_capture(out: str, expected):
        return SweepBuild(
            expected=frozenset(expected),
            built=frozenset(b.label for b in parse_binary_results(out)),
            build_output=out)

    monkeypatch.setattr(phase_c_tdd, "sweep_build", _whole_capture)
    events: list[tuple[str, dict]] = []
    dispatched: list[dict[Any, Any]] = []
    logs: list[str] = []
    _run(monkeypatch, tmp_path, implicated=[_LIB], events=events,
         dispatched=dispatched, run_cargo=lambda *a, **k: (101, _NESTED),
         logs=logs)
    assert dispatched and all(set(d) == {_LIB} for d in dispatched)
    narrowed = [d for ev, d in events
                if ev == "phase_c.tdd.compile_blocked_frontier"]
    assert narrowed and narrowed[0]["withheld"] == 1
    assert any("build is RED" in ln for ln in logs)


# --- the refusal counts sweeps that fills were attempted between -----------

_GREEN = (
    "     Running tests/t.rs (target/debug/deps/t-1)\n"
    "running 2 tests\n"
    "test t1 ... ok\ntest t2 ... ok\n"
    "test result: ok. 2 passed; 0 failed; 0 ignored\n"
)


def _red_then_green(reds: int):
    calls = [0]

    def _cargo(*a: Any, **k: Any) -> tuple[int, str]:
        calls[0] += 1
        assert calls[0] <= reds + 3, "the loop did not settle after the green sweep"
        return (101, _RED) if calls[0] <= reds else (0, _GREEN)
    return _cargo, calls


def test_identical_red_sweeps_with_nothing_dispatched_do_not_refuse(
        tmp_path: Path, monkeypatch) -> None:
    """Four identical red sweeps, the compiler implicating a fn the ranking
    never dispatches (`2131920`: the implicated unit was already filled and
    graded flat, so no route had a run for it — `dispatching 0 of 0`). The
    refusal's sentence is "the fill passes between these sweeps changed
    nothing"; with no fill between them it has no evidence and must not fire."""
    events: list[tuple[str, dict]] = []
    logs: list[str] = []
    cargo, calls = _red_then_green(4)
    _run(monkeypatch, tmp_path, implicated=[_LIB], events=events,
         dispatched=[], run_cargo=cargo, order=lambda eligible: [],
         logs=logs)
    # The plateau budget (`TDD_MAX_STALLS=4`) ends this walk at the fourth
    # sweep — a stop that describes what happened (nothing dispatched) — and
    # the fourth red sweep is reached. The control below stops at the third.
    assert calls[0] == 4, calls
    assert not [d for ev, d in events if ev == "phase_c.tdd.red_build_refusal"]
    assert not any("RED — the cluster cannot compile" in ln for ln in logs)
    assert any("pass_count plateau" in ln for ln in logs), logs[-3:]


def test_control_identical_red_sweeps_separated_by_attempts_refuse_on_the_third(
        tmp_path: Path, monkeypatch) -> None:
    """Same sweeps, a unit dispatched before each: the third identical reading
    is a verdict on two real fill passes and the refusal fires there."""
    events: list[tuple[str, dict]] = []
    cargo, calls = _red_then_green(4)
    _run(monkeypatch, tmp_path, implicated=[_LIB], events=events,
         dispatched=[], run_cargo=cargo)
    refusals = [d for ev, d in events if ev == "phase_c.tdd.red_build_refusal"]
    assert refusals and refusals[0]["iter"] == 3
    assert calls[0] == 3
