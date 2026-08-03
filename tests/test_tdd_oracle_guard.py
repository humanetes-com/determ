"""Sweep-level oracle integrity in the TDD loop.

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
from pipeline_core.domain.attribution import AttributionResult, FailureKind
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
                retrigger=None, reverse_map=None, localize=None,
                target_src_dir: Path | None = None,
                ) -> ClusterFillResult:
    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "0")
    monkeypatch.setenv("TDD_MAX_STALLS", "2")

    def _attr(*a: Any, **k: Any):
        attribution_kwargs.append(k)
        return AttributionResult({}, FailureKind.NO_FRONTIER, {})

    deps = PhaseCTddDeps(
        attribute_failures=_attr,
        build_test_reverse_map=lambda *a, **k: dict(reverse_map or {}),
        filter_test_targets=lambda frontier, src: frontier,
        check_graceful_stop=lambda *a, **k: None,
        check_auto_operator=lambda *a, **k: None,
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
        localize_crashes=localize,
    )
    return run_phase_c_tdd(
        deps, project_dir=tmp_path,
        target_src_dir=target_src_dir if target_src_dir is not None else tmp_path,
        tests_dir_files=[],
        cluster_tests=[("tests/t.rs", "fn t1() {}")], module="m",
        cluster=_cluster(), check_cmd="cargo check", cluster_env={},
        leaf_counts=None, fr_path=None,
        test_cmd_builder=lambda stems, include_lib=False: "cargo test",
        tag="[stable]")


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
                inventory=lambda pd, cl, stems, lib=False: (
                    4, frozenset({"t1", "t2", "t3", "t4"})),
                attribution_kwargs=attrs, checkpoints=ckpts, events=events)
    assert attrs and all(k.get("extra_failed") == frozenset({"t4"})
                         for k in attrs)
    vanish_evs = [d for ev, d in events if ev == "phase_c.tdd.oracle_vanished"]
    assert vanish_evs and vanish_evs[0]["vanished"] == ["t4"]
    assert vanish_evs[0]["cause"] == "crash"
    assert vanish_evs[0]["unnamed_shortfall"] == 0
    # The iter measurement's failed set carries the vanished test too.
    iters = [d for ev, d in events if ev == "phase_c.tdd.iter"]
    assert iters and "t4" in iters[0]["failed"]


def test_unnamed_shortfall_names_its_own_cause_in_the_event(
        tmp_path: Path, monkeypatch) -> None:
    # Every expected NAME reports, yet the total is one short: the static
    # inventory over-counted. No test vanished, so `vanished` is empty — and
    # the event must say so rather than leave a reader to infer a crash.
    out = ("test t1 ... ok\ntest t2 ... FAILED\n"
           "test result: FAILED. 1 passed; 1 failed; 0 ignored")
    events: list[tuple[str, dict]] = []
    _run_driver(monkeypatch, tmp_path,
                run_cargo=lambda *a, **k: (101, out),
                inventory=lambda pd, cl, stems, lib=False: (3, frozenset({"t1", "t2"})),
                attribution_kwargs=[], checkpoints=[], events=events)
    evs = [d for ev, d in events if ev == "phase_c.tdd.oracle_vanished"]
    assert evs and evs[0]["vanished"] == []
    assert evs[0]["unnamed_shortfall"] == 1
    assert evs[0]["cause"] == "inventory_overcount"


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
                inventory=lambda pd, cl, stems, lib=False: (
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
                inventory=lambda pd, cl, stems, lib=False: (2, frozenset({"t1", "t2"})),
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

    def _retrig(pd, mod, cl, sd, output, crash_evidence=None, **_kw):
        calls.append({"evidence": crash_evidence})
        fired = len(calls) == 1
        return RetriggerResult(fired=fired, succeeded=fired,
                               reverted_count=1, cause="RUNTIME_CRASH")

    events: list[tuple[str, dict]] = []
    _run_driver(monkeypatch, tmp_path,
                run_cargo=_cargo,
                inventory=lambda pd, cl, stems, lib=False: (2, frozenset({"t1", "t2"})),
                attribution_kwargs=[], checkpoints=[], events=events,
                retrigger=_retrig,
                reverse_map={"t2": {("src/error.rs", "downcast_ref")}})
    assert calls, "retrigger never fired on a reproducible first-sweep shrink"
    # sweep + confirmation re-sweep happened before the first retrigger call
    assert len(cargo_runs) >= 2


def test_unnamed_shortfall_never_fires_the_crash_retrigger(
        tmp_path: Path, monkeypatch) -> None:
    """A count-only shortfall names no test, so it is not crash evidence: the
    CrashEvidence would carry an empty vanished/suspect set and the re-author
    would aim at nothing. It is also CONSTANT (a static over-count does not
    heal), so gating on the raw missing count made EVERY iteration pay an extra
    confirmation sweep and declare a deterministic crash forever."""
    from pipeline_core.application.retrigger_layout import RetriggerResult
    monkeypatch.setenv("PHASE_C_CRASH_RETRIGGER_AFTER", "2")
    # Both expected NAMES report; the expected COUNT is one higher.
    out = ("test t1 ... ok\ntest t2 ... FAILED\n"
           "test result: FAILED. 1 passed; 1 failed; 0 ignored")
    cargo_runs: list[int] = []
    calls: list[Any] = []

    def _cargo(*a: Any, **k: Any):
        cargo_runs.append(1)
        return 101, out

    def _retrig(pd, mod, cl, sd, output, crash_evidence=None, **_kw):
        calls.append(crash_evidence)
        return RetriggerResult(fired=False, succeeded=False,
                               reverted_count=0, cause="")

    events: list[tuple[str, dict]] = []
    _run_driver(monkeypatch, tmp_path,
                run_cargo=_cargo,
                inventory=lambda pd, cl, stems, lib=False: (3, frozenset({"t1", "t2"})),
                attribution_kwargs=[], checkpoints=[], events=events,
                retrigger=_retrig)
    assert not any(c is not None for c in calls), (
        "crash retrigger was handed evidence built from an unnamed shortfall")
    # One sweep per iter — no confirmation re-sweep was provoked.
    assert len(cargo_runs) == len(
        [1 for ev, _ in events if ev == "phase_c.tdd.iter"])


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

    def _retrig(pd, mod, cl, sd, output, crash_evidence=None, **_kw):
        calls.append(1)
        fired = len(calls) <= 2
        return RetriggerResult(fired=fired, succeeded=fired,
                               reverted_count=1, cause="RUNTIME_CRASH")

    events: list[tuple[str, dict]] = []
    _run_driver(monkeypatch, tmp_path,
                run_cargo=lambda *a, **k: (101, out),
                inventory=lambda pd, cl, stems, lib=False: (2, frozenset({"t1", "t2"})),
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

    def _retrig(pd, mod, cl, sd, output, crash_evidence=None, **_kw):
        calls.append({"evidence": crash_evidence})
        from pipeline_core.application.retrigger_layout import RetriggerResult
        return RetriggerResult(fired=False)

    events: list[tuple[str, dict]] = []
    _run_driver(monkeypatch, tmp_path,
                run_cargo=_cargo,
                inventory=lambda pd, cl, stems, lib=False: (2, frozenset({"t1", "t2"})),
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

    def _retrig(pd, mod, cl, sd, output, crash_evidence=None, **_kw):
        calls.append({"evidence": crash_evidence})
        # Succeed once, then report not-fired so the loop can stall-exit.
        fired = len(calls) == 1
        return RetriggerResult(fired=fired, succeeded=fired,
                               reverted_count=1, cause="RUNTIME_CRASH")

    events: list[tuple[str, dict]] = []
    _run_driver(monkeypatch, tmp_path,
                run_cargo=lambda *a, **k: (101, out),
                inventory=lambda pd, cl, stems, lib=False: (2, frozenset({"t1", "t2"})),
                attribution_kwargs=[], checkpoints=[], events=events,
                retrigger=_retrig,
                reverse_map={"t2": {("src/error.rs", "downcast_ref")}})
    assert calls, "retrigger never called despite persistent vanished tests"
    ev = calls[0]["evidence"]
    assert ev is not None and ev.vanished == frozenset({"t2"})
    assert ("src/error.rs", "downcast_ref") in ev.suspect_fns
    assert any(ev_name == "phase_c.layout_retrigger.fired"
               for ev_name, _ in events)


def test_unnamed_shortfall_separates_inventory_gap_from_crash() -> None:
    """Every expected NAME reported, yet the trailer total is short by one:
    nothing crashed, the inventory just could not enumerate that test. Keeping
    the two apart is what stops the crash localizer (gdb + an ASAN rebuild)
    being sent after a fault that cannot exist."""
    o = reconcile_sweep(3, frozenset({"a", "b"}),
                        passed=["a"], failed=["b"], executed_count=2)
    assert o.vanished == frozenset()
    assert o.deficit == 1
    assert o.unnamed_shortfall == 1
    assert o.missing_count == 1          # still charged as failing — no fraud
    assert not o.intact


def test_crashed_binary_owns_the_shortfall_its_dup_names_hide() -> None:
    """The live misdiagnosis: `tests/test_downcast.rs` (7 tests) aborted, but 5
    of its bare names are also defined in surviving binaries, so they stayed in
    the observed set and only 2 read as vanished. The remaining 4 lost
    executions were reported as a permanent static-inventory over-count — the
    log literally said "not a crash" about a crash, the crash localizer was
    skipped, and run_health raised a phantom inventory_overcount every sweep.
    While a binary died, the deficit belongs to that crash."""
    o = reconcile_sweep(6, frozenset({"uniq", "dup"}),
                        passed=["dup"], failed=[], executed_count=1,
                        crashed_binaries=1)
    assert o.vanished == frozenset({"uniq"})   # `dup` still observed elsewhere
    assert o.deficit == 5
    assert o.unnamed_shortfall == 0            # attributed to the crash
    assert o.missing_count == 5                # still charged as failing
    # Same numbers with every binary intact ARE an inventory over-count.
    clean = reconcile_sweep(6, frozenset({"uniq", "dup"}),
                            passed=["dup"], failed=[], executed_count=1)
    assert clean.unnamed_shortfall == 4


def test_named_vanish_is_not_double_counted_as_unnamed() -> None:
    o = reconcile_sweep(3, frozenset({"a", "b", "c"}),
                        passed=["a"], failed=[], executed_count=1)
    assert o.vanished == frozenset({"b", "c"})
    assert o.unnamed_shortfall == 0      # the deficit IS the two vanished names
    assert o.missing_count == 2


def test_unnamed_shortfall_does_not_invoke_the_crash_localizer(
        tmp_path: Path, monkeypatch) -> None:
    """3 expected, 3 named tests all report, trailer says 2 → a count-only
    shortfall. gdb + an ASAN rebuild cannot find a fault that never happened,
    so the localizer must not be paid for every sweep to say so."""
    out = ("test t1 ... ok\ntest t2 ... ok\ntest t3 ... FAILED\n"
           "test result: FAILED. 2 passed; 1 failed; 0 ignored")
    calls: list[Any] = []

    def localize(*a: Any, **k: Any) -> tuple[Any, ...]:
        calls.append(a)
        return ()

    _run_driver(monkeypatch, tmp_path,
                run_cargo=lambda *a, **k: (101, out),
                inventory=lambda pd, cl, stems, lib=False: (
                    4, frozenset({"t1", "t2", "t3"})),
                attribution_kwargs=[], checkpoints=[], events=[],
                localize=localize)
    assert calls == []


def test_named_vanish_still_invokes_the_crash_localizer(
        tmp_path: Path, monkeypatch) -> None:
    out = ("test t1 ... ok\ntest t2 ... ok\n"
           "test result: FAILED. 2 passed; 0 failed; 0 ignored")
    calls: list[Any] = []

    def localize(*a: Any, **k: Any) -> tuple[Any, ...]:
        calls.append(a)
        return ()

    _run_driver(monkeypatch, tmp_path,
                run_cargo=lambda *a, **k: (101, out),
                inventory=lambda pd, cl, stems, lib=False: (
                    3, frozenset({"t1", "t2", "t3"})),
                attribution_kwargs=[], checkpoints=[], events=[],
                localize=localize)
    assert calls, "a named vanished test is exactly what the localizer is for"


_BUILD_BROKE = (
    "error[E0277]: `NonNull<ErrorImpl>` cannot be sent between threads safely\n"
    "   --> tests/test_autotrait.rs:5:5\n"
    'error: could not compile `anyhow` (test "test_autotrait") due to 1 error\n')


def test_unbuildable_cluster_fires_the_layout_retrigger_on_the_first_sweep(
        tmp_path: Path, monkeypatch) -> None:
    # A tree whose test binaries do not compile reports no verdict for ANY test,
    # so every further fill iteration is spent on a tree that cannot score. The
    # retrigger must act at iter 1 rather than wait out the stall budget.
    from pipeline_core.application.retrigger_layout import RetriggerResult
    calls: list[dict[str, Any]] = []

    def _retrig(pd, mod, cl, sd, output, crash_evidence=None,
                compile_evidence=None, **_kw):
        calls.append({"compile": compile_evidence, "crash": crash_evidence})
        fired = len(calls) == 1
        return RetriggerResult(fired=fired, succeeded=fired, reverted_count=3,
                               cause="DESIGN_CONFLICT")

    events: list[tuple[str, dict]] = []
    _run_driver(monkeypatch, tmp_path,
                run_cargo=lambda *a, **k: (101, _BUILD_BROKE),
                inventory=lambda pd, cl, stems, lib=False: (1, frozenset({"t1"})),
                attribution_kwargs=[], checkpoints=[], events=events,
                retrigger=_retrig)
    assert calls, "no retrigger on a cluster whose test binaries do not build"
    ev = calls[0]["compile"]
    assert ev is not None and calls[0]["crash"] is None
    assert ev.lib_clean and ev.consecutive_sweeps == 1
    assert "E0277" in ev.digest and "ErrorImpl" in ev.digest
    fired = [d for name, d in events if name == "phase_c.layout_retrigger.fired"]
    assert fired and fired[0]["cause"] == "DESIGN_CONFLICT"


def test_broken_lib_never_fires_the_layout_retrigger(
        tmp_path: Path, monkeypatch) -> None:
    # The lib failing to compile means a filled BODY is a live explanation, so
    # re-authoring the layout would revert a whole component off one bad fill.
    from pipeline_core.application.retrigger_layout import RetriggerResult
    calls: list[dict[str, Any]] = []

    def _retrig(pd, mod, cl, sd, output, crash_evidence=None,
                compile_evidence=None, **_kw):
        calls.append({"compile": compile_evidence})
        return RetriggerResult(fired=False)

    lib_broke = _BUILD_BROKE + "error: could not compile `anyhow` (lib) due to 1\n"
    _run_driver(monkeypatch, tmp_path,
                run_cargo=lambda *a, **k: (101, lib_broke),
                inventory=lambda pd, cl, stems, lib=False: (1, frozenset({"t1"})),
                attribution_kwargs=[], checkpoints=[], events=[],
                retrigger=_retrig)
    assert not any(c["compile"] is not None for c in calls)


def test_inventory_is_walked_over_the_cluster_tree_not_the_workspace_root(
        tmp_path: Path, monkeypatch) -> None:
    """The sweep's denominator must be counted over the tree cargo builds.

    The workspace root cannot answer the question. Its `src/` holds a one-line
    `lib.rs` stub plus the orphaned `gen_tests` siblings; every parent module
    carrying a sibling's `#[cfg(...)]` cluster gate exists only in the
    per-cluster tree. Walked from the root, all 19 siblings fell through the
    inventory's undeclared-sibling branch, which counts unconditionally and
    never evaluates a gate — so the 18 tests in `error.gen_tests_1..4` and the
    2 in `wrapper.gen_tests_4`, gated off nightly and absent from its build,
    were counted on every sweep. Measured on the live tree 2026-08-01: the
    root answered 124 where the nightly tree answers 104, and each sweep
    charged the 20-test difference as crash-vanished and aimed a crash
    retrigger at tests the binary never contained.
    """
    seen: list[Path] = []

    def _inventory(pd, cl, stems, lib=False):
        seen.append(pd)
        return (1, frozenset({"t1"}))

    cluster_tree = tmp_path / ".skeleton-filled" / "stable"
    cluster_tree.mkdir(parents=True)
    _run_driver(monkeypatch, tmp_path,
                run_cargo=lambda *a, **k: (0, "test t1 ... ok\n"
                                           "test result: ok. 1 passed; 0 failed"),
                inventory=_inventory,
                attribution_kwargs=[], checkpoints=[], events=[],
                target_src_dir=cluster_tree)
    assert seen, "the sweep never consulted the inventory"
    assert seen[0] == cluster_tree, (
        f"inventory walked {seen[0]}, not the cluster tree {cluster_tree}")
