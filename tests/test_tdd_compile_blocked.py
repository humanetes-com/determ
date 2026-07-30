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
         retrigger=None, red: str = _RED) -> None:
    """Drive the loop over a permanently red tree with a two-fn frontier."""
    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "0")
    monkeypatch.setenv("TDD_MAX_STALLS", "4")
    (tmp_path / "src").mkdir(exist_ok=True)

    def _order(eligible, counts):
        dispatched.append(dict(eligible))
        return list(eligible.items())

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
        log=lambda *a, **k: None,
        order_frontier=_order,
        record_tdd_regression=lambda *a, **k: "",
        reset_exhaustion_flags=lambda *a, **k: 0,
        run_cargo=lambda *a, **k: (101, red),
        scan_unimplemented_for_cluster=lambda *a, **k: [],
        scan_unimplemented_functions=lambda *a, **k: [],
        task2_fill_fns_one_by_one=lambda **kw: ClusterFillResult(attempts=1),
        checkpoint_fill_tree=lambda *a, **k: None,
        active_test_inventory=lambda pd, cl, stems, lib=False: (
            2, frozenset({"t1", "t2"})),
        retrigger_layout=retrigger,
        implicated_fns=lambda out, tsd: list(implicated),
    )
    run_phase_c_tdd(
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
