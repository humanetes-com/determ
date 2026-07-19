"""Sweep-level oracle integrity (FR-024 in the TDD loop).

A test binary that crashes or stops compiling silently removes its tests from
the cargo output; comparing pass counts over the shrunken denominator hid a
kept segfaulting fill (anyhow oracle 68→50). Pins: the pure reconciliation,
vanished tests flowing into attribution as failures, rc==0 with a short oracle
NOT closing the cluster, and the durable checkpoint refusing a short-oracle
tree.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.phase_c_tdd import (
    PhaseCTddDeps,
    run_phase_c_tdd,
)
from pipeline_core.domain.models import BuildCluster, ClusterFillResult
from pipeline_core.domain.oracle_ledger import reconcile_sweep


def test_reconcile_sweep_vanished_deficit_and_normalization() -> None:
    o = reconcile_sweep(4, frozenset({"t1", "m::t2", "t3", "t4"}),
                        passed=["t1", "t2"], failed=["mod2::t3"])
    assert o.vanished == frozenset({"t4"})
    assert o.deficit == 1
    assert o.missing_count == 1
    assert not o.intact
    ok = reconcile_sweep(3, frozenset({"a", "b", "c"}),
                         passed=["a", "b"], failed=["c"])
    assert ok.intact and ok.missing_count == 0
    # Ignored tests count as reported for the deficit (they have a verdict).
    ig = reconcile_sweep(3, frozenset({"a", "b"}),
                         passed=["a", "b"], failed=[], ignored_count=1)
    assert ig.deficit == 0


def test_cluster_ignored_test_is_not_vanished() -> None:
    """A test cfg-gated off for THIS cluster (`ignored, requires nightly`)
    reports no pass/fail verdict — without its NAME it is indistinguishable
    from a crash-vanished test and gets charged as a failure, fabricating a
    deterministic-crash signal for a test that was never meant to run here."""
    o = reconcile_sweep(
        3, frozenset({"test_a", "test_backtrace", "ui"}),
        passed=["test_a"], failed=[], ignored_count=2,
        ignored_names=frozenset({"test_backtrace", "ui"}), executed_count=1)
    assert o.vanished == frozenset()
    assert o.deficit == 0
    assert o.intact, "cluster-ignored tests must not shrink the oracle"
    # Without the names it is the old behaviour: two phantom failures.
    blind = reconcile_sweep(
        3, frozenset({"test_a", "test_backtrace", "ui"}),
        passed=["test_a"], failed=[], ignored_count=2, executed_count=1)
    assert blind.vanished == frozenset({"test_backtrace", "ui"})


def test_duplicate_bare_names_across_binaries_both_count() -> None:
    """`observed` is a SET of BARE names, so two binaries each defining
    `test_drop` collapse to one entry. Measuring that deduped size against an
    un-deduped expected_count silently inflates the deficit once per duplicate
    — the trailer-authoritative executed total must win."""
    expected = frozenset({"a::test_drop", "b::test_drop", "c::test_other"})
    o = reconcile_sweep(3, expected,
                        passed=["a::test_drop", "b::test_drop"],
                        failed=["c::test_other"], executed_count=3)
    assert o.deficit == 0 and o.intact
    # The deduped fallback is exactly the bug being pinned against.
    deduped = reconcile_sweep(3, expected,
                              passed=["a::test_drop", "b::test_drop"],
                              failed=["c::test_other"])
    assert deduped.deficit == 1


def _cluster() -> BuildCluster:
    return BuildCluster(
        name="stable", env_extra={}, tarpaulin_exclude_files=[],
        excluded_cfg=[], active_cfg=[], prompt_nightly_rule="")


def _run_driver(monkeypatch, tmp_path: Path, *, run_cargo, inventory,
                attribution_kwargs: list[dict[str, Any]],
                checkpoints: list[int], events: list[tuple[str, dict]],
                retrigger=None, reverse_map=None,
                ) -> ClusterFillResult:
    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "0")
    monkeypatch.setenv("TDD_MAX_STALLS", "2")

    def _attr(*a: Any, **k: Any):
        attribution_kwargs.append(k)
        return {}, "no_frontier"

    deps = PhaseCTddDeps(
        attribute_failures=_attr,
        build_test_reverse_map=lambda *a, **k: dict(reverse_map or {}),
        filter_test_targets=lambda frontier, src: frontier,
        check_graceful_stop=lambda *a, **k: None,
        converge_in_cluster=lambda *a, **k: None,
        count_relevant_notes=lambda *a, **k: 0,
        effective_stalls=lambda budget, module: budget,
        emit=lambda ev, **k: events.append((ev, k.get("data") or {})),
        log=lambda *a, **k: None,
        order_frontier=lambda eligible, counts: list(eligible.items()),
        record_tdd_regression=lambda *a, **k: "",
        reset_exhaustion_flags=lambda *a, **k: 0,
        run_cargo=run_cargo,
        scan_unimplemented_for_cluster=lambda *a, **k: [],
        scan_unimplemented_functions=lambda *a, **k: [],
        task2_fill_fns_one_by_one=lambda **kw: ClusterFillResult(attempts=1),
        checkpoint_fill_tree=lambda pd, cl, d, p, i: checkpoints.append(p),
        active_test_inventory=inventory,
        retrigger_layout=retrigger,
    )
    return run_phase_c_tdd(
        deps, project_dir=tmp_path, target_src_dir=tmp_path, tests_dir_files=[],
        cluster_tests=[("tests/t.rs", "fn t1() {}")], module="m",
        cluster=_cluster(), check_cmd="cargo check", cluster_env={},
        leaf_counts=None, fr_path=None,
        test_cmd_builder=lambda stems: "cargo test", tag="[stable]")


def test_vanished_tests_fail_the_sweep_and_reach_attribution(
        tmp_path: Path, monkeypatch) -> None:
    # Expected 4 tests; the output reports only 3 (t4's binary crashed).
    out = ("test t2 ... ok\ntest t3 ... ok\ntest t1 ... FAILED\n"
           "test result: FAILED. 2 passed; 1 failed; 0 ignored")
    attrs: list[dict[str, Any]] = []
    ckpts: list[int] = []
    events: list[tuple[str, dict]] = []
    _run_driver(monkeypatch, tmp_path,
                run_cargo=lambda *a, **k: (101, out),
                inventory=lambda pd, cl, stems: (
                    4, frozenset({"t1", "t2", "t3", "t4"})),
                attribution_kwargs=attrs, checkpoints=ckpts, events=events)
    assert attrs and all(k.get("extra_failed") == frozenset({"t4"})
                         for k in attrs)
    vanish_evs = [d for ev, d in events if ev == "phase_c.tdd.oracle_vanished"]
    assert vanish_evs and vanish_evs[0]["vanished"] == ["t4"]
    # The iter measurement's failed set carries the vanished test too.
    iters = [d for ev, d in events if ev == "phase_c.tdd.iter"]
    assert iters and "t4" in iters[0]["failed"]


def test_rc0_with_short_oracle_never_closes_cluster(
        tmp_path: Path, monkeypatch) -> None:
    # cargo says green — but one expected test never reported.
    out = ("test t1 ... ok\ntest t2 ... ok\n"
           "test result: ok. 2 passed; 0 failed; 0 ignored")
    attrs: list[dict[str, Any]] = []
    ckpts: list[int] = []
    events: list[tuple[str, dict]] = []
    _run_driver(monkeypatch, tmp_path,
                run_cargo=lambda *a, **k: (0, out),
                inventory=lambda pd, cl, stems: (
                    3, frozenset({"t1", "t2", "t3"})),
                attribution_kwargs=attrs, checkpoints=ckpts, events=events)
    assert not any(d.get("kind") == "pass" for ev, d in events
                   if ev == "phase_c.tdd.iter")
    # Short-oracle trees are never checkpointed as "best".
    assert ckpts == []


def test_intact_oracle_keeps_legacy_done_path(
        tmp_path: Path, monkeypatch) -> None:
    out = ("test t1 ... ok\ntest t2 ... ok\n"
           "test result: ok. 2 passed; 0 failed; 0 ignored")
    events: list[tuple[str, dict]] = []
    _run_driver(monkeypatch, tmp_path,
                run_cargo=lambda *a, **k: (0, out),
                inventory=lambda pd, cl, stems: (2, frozenset({"t1", "t2"})),
                attribution_kwargs=[], checkpoints=[], events=events)
    assert any(d.get("kind") == "pass" for ev, d in events
               if ev == "phase_c.tdd.iter")


def test_reproducible_first_sweep_shrink_fires_retrigger_immediately(
        tmp_path: Path, monkeypatch) -> None:
    # First shrunk sweep + the shrink reproducing on the instant re-sweep =
    # deterministic crash: the retrigger fires at iter 1 instead of burning a
    # full fill pass waiting for a second sweep.
    from pipeline_core.application.retrigger_layout import RetriggerResult
    monkeypatch.setenv("PHASE_C_CRASH_RETRIGGER_AFTER", "2")
    out = ("test t1 ... FAILED\n"
           "test result: FAILED. 0 passed; 1 failed; 0 ignored")
    cargo_runs: list[int] = []
    calls: list[dict[str, Any]] = []

    def _cargo(*a: Any, **k: Any):
        cargo_runs.append(1)
        return 101, out

    def _retrig(pd, mod, cl, sd, output, crash_evidence=None):
        calls.append({"evidence": crash_evidence})
        fired = len(calls) == 1
        return RetriggerResult(fired=fired, succeeded=fired,
                               reverted_count=1, cause="RUNTIME_CRASH")

    events: list[tuple[str, dict]] = []
    _run_driver(monkeypatch, tmp_path,
                run_cargo=_cargo,
                inventory=lambda pd, cl, stems: (2, frozenset({"t1", "t2"})),
                attribution_kwargs=[], checkpoints=[], events=events,
                retrigger=_retrig,
                reverse_map={"t2": {("src/error.rs", "downcast_ref")}})
    assert calls, "retrigger never fired on a reproducible first-sweep shrink"
    # sweep + confirmation re-sweep happened before the first retrigger call
    assert len(cargo_runs) >= 2


def test_retrigger_cooldown_skips_confirm_now_for_one_fill_pass(
        tmp_path: Path, monkeypatch) -> None:
    # After a retrigger attempt the component's fns are reverted to stubs, so
    # the NEXT sweep shrinks regardless of the new layout — confirm-now must
    # not burn the capped attempt budget in back-to-back iters (observed
    # live: attempts 1+2 fired at iters 1+2 with zero fills between). Fired
    # events must never land on consecutive iters.
    from pipeline_core.application.retrigger_layout import RetriggerResult
    monkeypatch.setenv("PHASE_C_CRASH_RETRIGGER_AFTER", "2")
    out = ("test t1 ... FAILED\n"
           "test result: FAILED. 0 passed; 1 failed; 0 ignored")
    calls: list[int] = []

    def _retrig(pd, mod, cl, sd, output, crash_evidence=None):
        calls.append(1)
        fired = len(calls) <= 2
        return RetriggerResult(fired=fired, succeeded=fired,
                               reverted_count=1, cause="RUNTIME_CRASH")

    events: list[tuple[str, dict]] = []
    _run_driver(monkeypatch, tmp_path,
                run_cargo=lambda *a, **k: (101, out),
                inventory=lambda pd, cl, stems: (2, frozenset({"t1", "t2"})),
                attribution_kwargs=[], checkpoints=[], events=events,
                retrigger=_retrig,
                reverse_map={"t2": {("src/error.rs", "downcast_ref")}})
    fired_iters = [d["iter"] for ev, d in events
                   if ev == "phase_c.layout_retrigger.fired"]
    assert len(fired_iters) == 2
    assert fired_iters[1] - fired_iters[0] >= 2, \
        f"attempts fired on consecutive iters: {fired_iters}"


def test_transient_shrink_confirm_sweep_intact_does_not_fire(
        tmp_path: Path, monkeypatch) -> None:
    # First sweep shrinks, but the instant re-sweep reports every expected
    # test — transient, no retrigger this iter.
    monkeypatch.setenv("PHASE_C_CRASH_RETRIGGER_AFTER", "2")
    shrunk = ("test t1 ... FAILED\n"
              "test result: FAILED. 0 passed; 1 failed; 0 ignored")
    intact = ("test t1 ... FAILED\ntest t2 ... ok\n"
              "test result: FAILED. 1 passed; 1 failed; 0 ignored")
    outs = [shrunk, intact]
    calls: list[dict[str, Any]] = []

    def _cargo(*a: Any, **k: Any):
        return 101, outs.pop(0) if outs else intact

    def _retrig(pd, mod, cl, sd, output, crash_evidence=None):
        calls.append({"evidence": crash_evidence})
        from pipeline_core.application.retrigger_layout import RetriggerResult
        return RetriggerResult(fired=False)

    events: list[tuple[str, dict]] = []
    _run_driver(monkeypatch, tmp_path,
                run_cargo=_cargo,
                inventory=lambda pd, cl, stems: (2, frozenset({"t1", "t2"})),
                attribution_kwargs=[], checkpoints=[], events=events,
                retrigger=_retrig,
                reverse_map={"t2": {("src/error.rs", "downcast_ref")}})
    # The stall-exit path may still consult the retrigger via the error-text
    # channel; what must NOT happen is a crash-evidence (cause C) call.
    assert all(c["evidence"] is None for c in calls), \
        "crash retrigger fired on a transient one-sweep shrink"


def test_persistent_vanish_fires_crash_retrigger_with_suspects(
        tmp_path: Path, monkeypatch) -> None:
    # Two consecutive short-oracle sweeps → the driver calls the layout
    # retrigger with typed CrashEvidence (vanished names + reverse-map
    # suspects); a successful re-author resets the stall budget so the loop
    # continues instead of stall-exiting.
    from pipeline_core.application.retrigger_layout import RetriggerResult
    monkeypatch.setenv("PHASE_C_CRASH_RETRIGGER_AFTER", "2")
    out = ("test t1 ... FAILED\n"
           "test result: FAILED. 0 passed; 1 failed; 0 ignored")
    calls: list[dict[str, Any]] = []

    def _retrig(pd, mod, cl, sd, output, crash_evidence=None):
        calls.append({"evidence": crash_evidence})
        # Succeed once, then report not-fired so the loop can stall-exit.
        fired = len(calls) == 1
        return RetriggerResult(fired=fired, succeeded=fired,
                               reverted_count=1, cause="RUNTIME_CRASH")

    events: list[tuple[str, dict]] = []
    _run_driver(monkeypatch, tmp_path,
                run_cargo=lambda *a, **k: (101, out),
                inventory=lambda pd, cl, stems: (2, frozenset({"t1", "t2"})),
                attribution_kwargs=[], checkpoints=[], events=events,
                retrigger=_retrig,
                reverse_map={"t2": {("src/error.rs", "downcast_ref")}})
    assert calls, "retrigger never called despite persistent vanished tests"
    ev = calls[0]["evidence"]
    assert ev is not None and ev.vanished == frozenset({"t2"})
    assert ("src/error.rs", "downcast_ref") in ev.suspect_fns
    assert any(ev_name == "phase_c.layout_retrigger.fired"
               for ev_name, _ in events)
