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
from pipeline_core.domain.models import (
    BuildCluster,
    ClusterFillResult,
    FnLocator,
)
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
                escalate=None, fill=None, logs=None,
                enumerate_fns=None, test_files=None,
                target_src_dir: Path | None = None,
                frontier: dict[FnLocator, set[str]] | None = None,
                guard: str = "0", best_mark: Any = None, restore: Any = None,
                ) -> ClusterFillResult:
    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "0")
    monkeypatch.setenv("TDD_MAX_STALLS", "2")
    # The flat-score GPU guard off BY DEFAULT: these sweeps repeat a reading on
    # purpose (the oracle shrink has to reproduce before the crash counts as
    # deterministic), so the guard would stop the run at the second reading —
    # correctly, and one iteration before the evidence this module is about
    # reaches a dispatch. `tests/test_tdd_gpu_guard.py` owns the guard; the one
    # case here that arms it (`guard="2"`) does so because the state under test
    # — a restore inside an open re-fill window — needs a retrigger fire AND a
    # trip in one run, and only this harness can produce the fire.
    monkeypatch.setenv("TDD_FLAT_SCORE_REPEATS", guard)

    def _attr(*a: Any, **k: Any):
        attribution_kwargs.append(k)
        if frontier:
            return AttributionResult(dict(frontier), FailureKind.RUNTIME,
                                     dict(frontier))
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
        log=(logs.append if logs is not None else (lambda *a, **k: None)),
        order_frontier=lambda eligible, counts: list(eligible.items()),
        record_tdd_regression=lambda *a, **k: "",
        reset_exhaustion_flags=lambda *a, **k: [],
        run_cargo=run_cargo,
        scan_unimplemented_for_cluster=lambda *a, **k: [],
        scan_unimplemented_functions=lambda *a, **k: [],
        task2_fill_fns_one_by_one=fill or (
            lambda **kw: ClusterFillResult(attempts=1)),
        checkpoint_fill_tree=lambda pd, cl, d, m: checkpoints.append(m.pass_count),
        active_test_inventory=inventory,
        retrigger_layout=retrigger,
        localize_crashes=localize,
        escalate_to_producers=escalate,
        enumerate_fns=enumerate_fns,
        test_file_index=((lambda *a, **k: dict(test_files))
                         if test_files is not None else None),
        load_best_tree_mark=(lambda *a, **k: best_mark) if best_mark else None,
        restore_best_tree_pair=restore,
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
    # A short oracle blocks only an EQUAL count. With no stored mark this
    # sweep's 2 is a verified floor over an empty slot, so it checkpoints —
    # requiring a full oracle unconditionally froze the heal tier at 18/105
    # while live sweeps read 81 and 88 under a chronic one-binary crash
    # (2026-08-09/10 on `anyhow`). The equal-count churn veto is pinned in
    # test_tdd_ratchet.py; "short oracle never CLOSES the cluster" (the
    # assertion above) is unchanged.
    assert ckpts == [2]


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


def _unattributed_uaf_site(test_name: str = "t3"):
    """A frames-empty origin-unattributed site — what the localizer returns
    for the frame-blind crash class since it stopped dropping them."""
    from pipeline_core.domain.crash_site import CrashSite
    return CrashSite(
        binary_label="tests/test_downcast.rs", test_name=test_name,
        signal="AddressSanitizer: heap-use-after-free",
        frames=(), origin_frames=(), origin_capable=True,
        raw_report="freed by thread T0 here:\n    #0 free asan.cpp:52")


def test_deterministic_unattributed_crash_synthesizes_chain_blame(
        tmp_path: Path, monkeypatch) -> None:
    # The frame-blind crash class end to end (run `635043-5f7c61f5`,
    # 2026-08-14): shrink reproduces on the immediate re-sweep (deterministic)
    # + the localizer's site is origin-unattributed with zero frames → the
    # driver synthesizes a chain-derived blame (attributed fns + producers),
    # arms the evidence pack, and hands the retrigger a CrashEvidence whose
    # signature CHANGES — the closed walk's own stated unlock.
    from dataclasses import replace as _dc_replace

    from pipeline_core.application.retrigger_layout import RetriggerResult
    from pipeline_core.domain.layout_retrigger import crash_signature
    monkeypatch.setenv("PHASE_C_CRASH_RETRIGGER_AFTER", "2")
    out = ("test t1 ... ok\ntest t2 ... FAILED\n"
           "test result: FAILED. 1 passed; 1 failed; 0 ignored")
    evidences: list[Any] = []
    fills: list[dict[str, Any]] = []
    logs: list[str] = []

    def _retrig(pd, mod, cl, sd, output, crash_evidence=None, **_kw):
        evidences.append(crash_evidence)
        return RetriggerResult(fired=False, reason="walk closed")

    def _fill(**kw):
        fills.append(kw)
        return ClusterFillResult(attempts=1)

    events: list[tuple[str, dict]] = []
    _run_driver(monkeypatch, tmp_path,
                run_cargo=lambda *a, **k: (101, out),
                inventory=lambda pd, cl, stems, lib=False: (
                    3, frozenset({"t1", "t2", "t3"})),
                attribution_kwargs=[], checkpoints=[], events=events,
                retrigger=_retrig,
                reverse_map={"t3": {("src/error.rs", "new")}},
                localize=lambda *a, **k: (_unattributed_uaf_site(),),
                escalate=lambda src, tname, units: {
                    FnLocator("src/error.rs", "construct_from_std"): {tname}},
                fill=_fill, logs=logs)

    chain_evs = [d for ev, d in events if ev == "phase_c.crash.chain_blame"]
    assert chain_evs, "chain-derived synthesis never fired"
    assert chain_evs[0]["provenance"] == "chain_derived"
    assert set(chain_evs[0]["blamed"]) == {
        "src/error.rs::construct_from_std", "src/error.rs::new"}
    assert any("CHAIN-DERIVED" in ln for ln in logs)

    # The retrigger received the chain in its OWN field, and the signature
    # moved off the blameless one the walk's refutations were booked under.
    with_chain = [e for e in evidences
                  if e is not None and e.chain_blamed]
    assert with_chain, "no CrashEvidence carried the chain"
    ev0 = with_chain[0]
    assert ev0.chain_blamed == frozenset({
        FnLocator("src/error.rs", "construct_from_std"),
        FnLocator("src/error.rs", "new")})
    assert FnLocator("src/error.rs", "construct_from_std") \
        not in ev0.blamed_fns
    assert crash_signature(ev0) != crash_signature(
        _dc_replace(ev0, chain_blamed=frozenset()))

    # The evidence pack armed: dispatch received the chain keys with the
    # CHAIN-DERIVED text and the raw report, and the conviction channel
    # (`crash_blamed`) stayed empty — an inference must not convict.
    armed = [kw for kw in fills if kw.get("crash_evidence")]
    assert armed, "no fill call carried crash evidence"
    ce = armed[0]["crash_evidence"]
    key = FnLocator("src/error.rs", "construct_from_std")
    assert key in ce and "CHAIN-DERIVED" in ce[key]
    assert "freed by thread T0" in ce[key]
    assert key in armed[0]["tdd_frontier_keys"]
    assert not armed[0].get("crash_blamed")


_DOUBLE_DROP_REPORT = """\
==1==ERROR: AddressSanitizer: heap-use-after-free on address 0x7b at pc 0x55
WRITE of size 8 at 0x7b thread T5
    #2 0x1 in <alloc::sync::Arc<core::sync::atomic::Atomic<bool>> as core::ops::drop::Drop>::drop /rustc/9e/library/alloc/src/sync.rs:2831:32
    #5 0x2 in test_downcast::test_drop /w/tests/test_downcast.rs:85:1
freed by thread T5 here:
    #0 0x3 in free /rustc/llvm/compiler-rt/lib/asan/asan_malloc_linux.cpp:51:3
    #5 0x4 in <alloc::sync::Arc<core::sync::atomic::Atomic<bool>> as core::ops::drop::Drop>::drop /rustc/9e/library/alloc/src/sync.rs:2874:18
    #6 0x5 in test_downcast::test_drop /w/tests/test_downcast.rs:83:5
SUMMARY: AddressSanitizer: heap-use-after-free
"""


def _double_drop_site(test_name: str = "test_drop"):
    from pipeline_core.domain.crash_site import CrashSite
    return CrashSite(
        binary_label="tests/test_downcast.rs", test_name=test_name,
        signal="AddressSanitizer: heap-use-after-free",
        frames=(), origin_frames=(), origin_capable=True,
        raw_report=_DOUBLE_DROP_REPORT)


def test_the_subject_channel_fires_with_the_inventory_anchor(
        tmp_path: Path, monkeypatch) -> None:
    """The pin 173.2 asked for and could not write: nothing localizes, the
    shrink is deterministic, and the subject channel ARMS — the log line plus
    the subject fn reaching a fill's `crash_evidence`.

    Its negative control is the wiring as it stood: with no file index the
    anchor is a module path, the vanished set is bare by construction, and the
    channel produces nothing — which is why it had never fired once since it
    shipped 2026-08-10."""
    monkeypatch.setenv("PHASE_C_CRASH_RETRIGGER_AFTER", "9")
    out = ("test t1 ... ok\ntest t2 ... FAILED\n"
           "test result: FAILED. 1 passed; 1 failed; 0 ignored")
    logs: list[str] = []
    fills: list[dict[str, Any]] = []

    def _fill(**kw):
        fills.append(kw)
        return ClusterFillResult(attempts=1)

    _run_driver(monkeypatch, tmp_path,
                run_cargo=lambda *a, **k: (101, out),
                inventory=lambda pd, cl, stems, lib=False: (
                    3, frozenset({"t1", "t2", "t3"})),
                attribution_kwargs=[], checkpoints=[], events=[],
                reverse_map={"t3": {("src/error.rs", "construct")}},
                localize=lambda *a, **k: (),
                test_files={"t3": "src/error.gen_tests_4.rs"},
                fill=_fill, logs=logs)

    assert any("synthesized crash evidence for subject fn(s)" in ln
               for ln in logs), "the channel never armed"
    armed = [kw for kw in fills if kw.get("crash_evidence")]
    assert armed
    assert FnLocator("src/error.rs", "construct") in armed[0]["crash_evidence"]


def test_without_the_anchor_the_subject_channel_stays_silent(
        tmp_path: Path, monkeypatch) -> None:
    """The negative control — the pre-fix wiring, reproduced by omission."""
    monkeypatch.setenv("PHASE_C_CRASH_RETRIGGER_AFTER", "9")
    out = ("test t1 ... ok\ntest t2 ... FAILED\n"
           "test result: FAILED. 1 passed; 1 failed; 0 ignored")
    logs: list[str] = []
    _run_driver(monkeypatch, tmp_path,
                run_cargo=lambda *a, **k: (101, out),
                inventory=lambda pd, cl, stems, lib=False: (
                    3, frozenset({"t1", "t2", "t3"})),
                attribution_kwargs=[], checkpoints=[], events=[],
                reverse_map={"t3": {("src/error.rs", "construct")}},
                localize=lambda *a, **k: (), logs=logs)

    assert not [ln for ln in logs
                if "synthesized crash evidence for subject" in ln]


def test_the_chain_is_derived_from_the_crashed_test_not_only_the_vanished_set(
        tmp_path: Path, monkeypatch) -> None:
    """Run `1064759-0f23fb68`: `vanished` named `test_large_alignment` and
    `test_no_backtrace_fallback` while every site named `test_drop` — the one
    test whose chain holds the fault. The oracle drops that name for a sound
    reason (its bare name also lives in a surviving binary, so it is not
    PROVABLY missing) which says nothing about its value as a crash subject."""
    monkeypatch.setenv("PHASE_C_CRASH_RETRIGGER_AFTER", "2")
    out = ("test t1 ... ok\ntest t2 ... FAILED\n"
           "test result: FAILED. 1 passed; 1 failed; 0 ignored")
    escalated: list[str] = []
    events: list[tuple[str, dict]] = []

    def _escalate(src, tname, units):
        escalated.append(tname)
        return {FnLocator("src/error.rs", f"producer_of_{tname}"): {tname}}

    _run_driver(monkeypatch, tmp_path,
                run_cargo=lambda *a, **k: (101, out),
                inventory=lambda pd, cl, stems, lib=False: (
                    3, frozenset({"t1", "t2", "t3"})),
                attribution_kwargs=[], checkpoints=[], events=events,
                reverse_map={},
                localize=lambda *a, **k: (
                    _unattributed_uaf_site(test_name="test_drop"),),
                escalate=_escalate)

    assert "test_drop" in escalated, \
        "the test the binary died running never reached the derivation"
    assert "t3" in escalated, "the vanished set must still contribute"
    chain = [d for ev, d in events if ev == "phase_c.crash.chain_blame"]
    assert chain and "src/error.rs::producer_of_test_drop" in chain[0]["blamed"]


def test_a_double_drop_narrows_the_pack_to_the_destruction_family(
        tmp_path: Path, monkeypatch) -> None:
    """A construction chain is the set of fns that BUILD the value; this fault
    class is a value destroyed twice, so the culprit is on a teardown path no
    producer escalation reaches. Live, the chain named 50 fns and the only
    drop-family member among them was `Error::drop`."""
    monkeypatch.setenv("PHASE_C_CRASH_RETRIGGER_AFTER", "2")
    out = ("test t1 ... ok\ntest t2 ... FAILED\n"
           "test result: FAILED. 1 passed; 1 failed; 0 ignored")
    events: list[tuple[str, dict]] = []
    fills: list[dict[str, Any]] = []
    logs: list[str] = []

    def _fill(**kw):
        fills.append(kw)
        return ClusterFillResult(attempts=1)

    _run_driver(monkeypatch, tmp_path,
                run_cargo=lambda *a, **k: (101, out),
                inventory=lambda pd, cl, stems, lib=False: (
                    3, frozenset({"t1", "t2", "t3"})),
                attribution_kwargs=[], checkpoints=[], events=events,
                reverse_map={},
                localize=lambda *a, **k: (_double_drop_site(),),
                escalate=lambda src, tname, units: {
                    FnLocator("src/error.rs", f"construct_{i}"): {tname}
                    for i in range(20)},
                enumerate_fns=lambda src: [
                    {"file": "src/error.rs", "fn_name": "object_drop_front",
                     "body_text": "Box::from_raw(p)"},
                    {"file": "src/error.rs", "fn_name": "object_drop",
                     "body_text": "Box::from_raw(p)"},
                    {"file": "src/error.rs", "fn_name": "construct_0",
                     "body_text": "{}"},
                ],
                fill=_fill, logs=logs)

    narrowed = [d for ev, d in events
                if ev == "phase_c.crash.destruction_narrowed"]
    assert narrowed, "the double-drop reading never narrowed the pack"
    assert narrowed[0]["chain_count"] == 20
    assert narrowed[0]["subjects"] == [
        "src/error.rs::object_drop", "src/error.rs::object_drop_front"]
    assert "Arc<core::sync::atomic::Atomic<bool>>" in narrowed[0]["freed_type"]
    assert narrowed[0]["free_site"] == "tests/test_downcast.rs:83"

    chain = [d for ev, d in events if ev == "phase_c.crash.chain_blame"]
    assert chain and set(chain[0]["blamed"]) == {
        "src/error.rs::object_drop", "src/error.rs::object_drop_front"}, \
        "the construction closure must be REPLACED, not widened"
    armed = [kw for kw in fills if kw.get("crash_evidence")]
    assert armed
    key = FnLocator("src/error.rs", "object_drop_front")
    assert key in armed[0]["crash_evidence"]
    assert "DOUBLE DROP" in armed[0]["crash_evidence"][key]
    assert not armed[0].get("crash_blamed"), "an inference must not convict"


def test_without_the_fn_enumerator_the_chain_is_unchanged(
        tmp_path: Path, monkeypatch) -> None:
    """The negative control for the narrowing, and the pre-fix behaviour: the
    same double-drop report with no way to read the crate's own declarations
    leaves the full construction closure in place."""
    monkeypatch.setenv("PHASE_C_CRASH_RETRIGGER_AFTER", "2")
    out = ("test t1 ... ok\ntest t2 ... FAILED\n"
           "test result: FAILED. 1 passed; 1 failed; 0 ignored")
    events: list[tuple[str, dict]] = []
    _run_driver(monkeypatch, tmp_path,
                run_cargo=lambda *a, **k: (101, out),
                inventory=lambda pd, cl, stems, lib=False: (
                    3, frozenset({"t1", "t2", "t3"})),
                attribution_kwargs=[], checkpoints=[], events=events,
                reverse_map={},
                localize=lambda *a, **k: (_double_drop_site(),),
                escalate=lambda src, tname, units: {
                    FnLocator("src/error.rs", f"construct_{i}"): {tname}
                    for i in range(20)})

    assert not [d for ev, d in events
                if ev == "phase_c.crash.destruction_narrowed"]
    chain = [d for ev, d in events if ev == "phase_c.crash.chain_blame"]
    assert chain and len(chain[0]["blamed"]) == 20


def test_no_chain_blame_without_the_unattributed_site(
        tmp_path: Path, monkeypatch) -> None:
    # Negative control — the pre-fix driver input, reproduced by injection:
    # the old localizer DROPPED the frames-empty site, so the driver saw an
    # empty localization. Same deterministic crash, no synthesis, no armed
    # evidence, and every CrashEvidence stays on the blameless signature the
    # walk closed under (the recorded outcome of run `635043-5f7c61f5`).
    from pipeline_core.application.retrigger_layout import RetriggerResult
    from pipeline_core.domain.layout_retrigger import crash_signature
    monkeypatch.setenv("PHASE_C_CRASH_RETRIGGER_AFTER", "2")
    out = ("test t1 ... ok\ntest t2 ... FAILED\n"
           "test result: FAILED. 1 passed; 1 failed; 0 ignored")
    evidences: list[Any] = []
    fills: list[dict[str, Any]] = []

    def _retrig(pd, mod, cl, sd, output, crash_evidence=None, **_kw):
        evidences.append(crash_evidence)
        return RetriggerResult(fired=False, reason="walk closed")

    def _fill(**kw):
        fills.append(kw)
        return ClusterFillResult(attempts=1)

    events: list[tuple[str, dict]] = []
    _run_driver(monkeypatch, tmp_path,
                run_cargo=lambda *a, **k: (101, out),
                inventory=lambda pd, cl, stems, lib=False: (
                    3, frozenset({"t1", "t2", "t3"})),
                attribution_kwargs=[], checkpoints=[], events=events,
                retrigger=_retrig,
                reverse_map={"t3": {("src/error.rs", "new")}},
                localize=lambda *a, **k: (),
                escalate=lambda src, tname, units: {
                    FnLocator("src/error.rs", "construct_from_std"): {tname}},
                fill=_fill)

    assert not [d for ev, d in events if ev == "phase_c.crash.chain_blame"]
    assert all(e is None or not e.chain_blamed for e in evidences)
    # One unchanged signature across every gate arrival = the walk's
    # refutations and zero-payoff streak keep applying; nothing re-opens.
    sigs = {crash_signature(e) for e in evidences if e is not None}
    assert len(sigs) == 1
    assert not any(kw.get("crash_evidence") for kw in fills)


# --- the producer aim on the FRAME-derived route -----------------------------
# Run `1408670-3c074ece` (2026-08-16), cluster `stable`, all five sweeps
# identical: `tests/test_downcast.rs` died SIGABRT in `test_drop` and the
# localizer blamed `src/error.rs::downcast_ref` — frame-derived, and correct as
# a frame. The fault was `object_downcast` returning
# `Some(Ref::from_raw(NonNull::dangling()))`, which `downcast_ref` calls on the
# line above the one that faulted. 6h02m, 101 fills, 20 dispatches of the impl
# block holding the blamed fn, zero test-verdict changes, and `object_downcast`
# never dispatched once. The destruction narrowing shipped for the
# blame-NOBODY route cannot reach here: this route HAS a blame.
_MISALIGNED_OUT = (
    "test t1 ... ok\ntest t2 ... FAILED\n"
    "test result: FAILED. 1 passed; 1 failed; 0 ignored\n"
    "thread 'test_drop' panicked at src/error.rs:440:34:\n"
    "misaligned pointer dereference: address must be a multiple of 0x8 "
    "but is 0x1\n"
    "thread caused non-unwinding panic. aborting.\n")

_LIVE_FNS = [
    {"file": "src/error.rs", "fn_name": "downcast_ref",
     "body_text": "let vtbl = unsafe { crate::error::vtable(self.inner.ptr) };\n"
                  "if let Some(down) = unsafe { (vtbl.object_downcast)(r, target) } {\n"
                  "    return Some(unsafe { &*(down.as_ptr() as *const E) });\n}"},
    {"file": "src/error.rs", "fn_name": "object_downcast",
     "body_text": "Some(unsafe { Ref::from_raw(core::ptr::NonNull::dangling()) })"},
    {"file": "src/error.rs", "fn_name": "context_downcast",
     "body_text": "Some(unsafe { Ref::from_raw(core::ptr::NonNull::dangling()) })"},
    {"file": "src/error.rs", "fn_name": "vtable", "body_text": "unsafe { &*e.vtable }"},
    {"file": "src/chain.rs", "fn_name": "next", "body_text": "self.state.next()"},
]


def _frame_blamed_site(line: int = 440):
    from pipeline_core.domain.crash_site import CrashFrame, CrashSite
    return CrashSite(
        binary_label="tests/test_downcast.rs", test_name="test_drop",
        signal="SIGABRT",
        frames=(CrashFrame("src/error.rs", line, "downcast_ref",
                           "<anyhow::Error>::downcast_ref::<E>"),),
        origin_frames=(), origin_capable=False)


def test_a_frame_blame_aims_at_the_producer_it_cannot_name(
        tmp_path: Path, monkeypatch) -> None:
    """The frame READS the bad pointer; the fn that WROTE it is one call away
    and in no stack. Both halves must arrive: `object_downcast` as a subject,
    and the placeholder reading as the statement of fact that says why."""
    monkeypatch.setenv("PHASE_C_CRASH_RETRIGGER_AFTER", "9")
    events: list[tuple[str, dict]] = []
    fills: list[dict[str, Any]] = []
    logs: list[str] = []

    def _fill(**kw):
        fills.append(kw)
        return ClusterFillResult(attempts=1)

    _run_driver(monkeypatch, tmp_path,
                run_cargo=lambda *a, **k: (101, _MISALIGNED_OUT),
                inventory=lambda pd, cl, stems, lib=False: (
                    3, frozenset({"t1", "t2", "t3"})),
                attribution_kwargs=[], checkpoints=[], events=events,
                reverse_map={},
                localize=lambda *a, **k: (_frame_blamed_site(),),
                enumerate_fns=lambda src: list(_LIVE_FNS),
                fill=_fill, logs=logs)

    aimed = [d for ev, d in events if ev == "phase_c.crash.producer_aimed"]
    assert aimed, "the frame-derived route aimed at no producer at all"
    assert aimed[0]["blamed"] == ["src/error.rs::downcast_ref"]
    subs = set(aimed[0]["subjects"])
    assert "src/error.rs::object_downcast" in subs, \
        "the fn that fabricated the pointer never entered the pack"
    # The sentinel family enters on the placeholder reading, whether or not the
    # blamed fn calls it: the pointer may be handed through any number of hops.
    assert "src/error.rs::context_downcast" in subs
    assert "src/chain.rs::next" not in subs, "an unmentioned fn is not a subject"
    assert aimed[0]["placeholder_pointer"] == {
        "address": 1, "required_align": 8, "deref_site": "src/error.rs:440"}

    armed = [kw for kw in fills if kw.get("crash_evidence")]
    assert armed
    key = FnLocator("src/error.rs", "object_downcast")
    assert key in armed[0]["crash_evidence"]
    text = armed[0]["crash_evidence"][key]
    assert "PRODUCER CANDIDATE" in text and "PLACEHOLDER POINTER" in text
    # EVIDENCE, never blame: the inference may prompt, enter the frontier and
    # void exhaustion, and it may not convict, crash-block or revert.
    assert FnLocator("src/error.rs", "object_downcast") not in (
        armed[0].get("crash_blamed") or frozenset())
    assert any("producer candidate" in ln for ln in logs)


def test_the_blamed_fns_own_evidence_outranks_the_producer_guess(
        tmp_path: Path, monkeypatch) -> None:
    """`setdefault` semantics, asserted where it matters: the blamed fn is its
    own callee-set's neighbour in the records, and its real frame evidence must
    not be overwritten by the weaker producer text."""
    monkeypatch.setenv("PHASE_C_CRASH_RETRIGGER_AFTER", "9")
    events: list[tuple[str, dict]] = []
    fills: list[dict[str, Any]] = []

    def _fill(**kw):
        fills.append(kw)
        return ClusterFillResult(attempts=1)

    _run_driver(monkeypatch, tmp_path,
                run_cargo=lambda *a, **k: (101, _MISALIGNED_OUT),
                inventory=lambda pd, cl, stems, lib=False: (
                    3, frozenset({"t1", "t2", "t3"})),
                attribution_kwargs=[], checkpoints=[], events=events,
                reverse_map={},
                localize=lambda *a, **k: (_frame_blamed_site(),),
                enumerate_fns=lambda src: list(_LIVE_FNS),
                fill=_fill)

    armed = [kw for kw in fills if kw.get("crash_evidence")][0]
    blamed_text = armed["crash_evidence"][FnLocator("src/error.rs", "downcast_ref")]
    assert "PRODUCER CANDIDATE" not in blamed_text
    assert "SIGABRT" in blamed_text


def test_without_the_fn_enumerator_no_producer_is_aimed(
        tmp_path: Path, monkeypatch) -> None:
    """The negative control: the wiring exactly as it stood for the 6h02m run.
    A frame blame, the same panic text, and the pack is the blamed fn's own
    frames — which is how `object_downcast` went undispatched."""
    monkeypatch.setenv("PHASE_C_CRASH_RETRIGGER_AFTER", "9")
    events: list[tuple[str, dict]] = []
    fills: list[dict[str, Any]] = []

    def _fill(**kw):
        fills.append(kw)
        return ClusterFillResult(attempts=1)

    _run_driver(monkeypatch, tmp_path,
                run_cargo=lambda *a, **k: (101, _MISALIGNED_OUT),
                inventory=lambda pd, cl, stems, lib=False: (
                    3, frozenset({"t1", "t2", "t3"})),
                attribution_kwargs=[], checkpoints=[], events=events,
                reverse_map={},
                localize=lambda *a, **k: (_frame_blamed_site(),),
                enumerate_fns=None, fill=_fill)

    assert not [d for ev, d in events if ev == "phase_c.crash.producer_aimed"]
    armed = [kw for kw in fills if kw.get("crash_evidence")]
    assert armed, "the frame evidence itself must be unaffected"
    assert FnLocator("src/error.rs", "object_downcast") not in armed[0]["crash_evidence"]


def test_an_ordinary_crash_aims_at_callees_without_a_placeholder_claim(
        tmp_path: Path, monkeypatch) -> None:
    """The callee aim is the general fix and stands alone; the placeholder
    reading is an extra statement of fact that must NOT be made when the
    alignment check did not print. Without it the sentinel family is not
    pulled in either — only what the blamed body actually names."""
    monkeypatch.setenv("PHASE_C_CRASH_RETRIGGER_AFTER", "9")
    events: list[tuple[str, dict]] = []
    fills: list[dict[str, Any]] = []

    def _fill(**kw):
        fills.append(kw)
        return ClusterFillResult(attempts=1)

    plain = ("test t1 ... ok\ntest t2 ... FAILED\n"
             "test result: FAILED. 1 passed; 1 failed; 0 ignored\n"
             "thread 'test_drop' panicked at src/error.rs:440:34:\n"
             "explicit panic\n")
    _run_driver(monkeypatch, tmp_path,
                run_cargo=lambda *a, **k: (101, plain),
                inventory=lambda pd, cl, stems, lib=False: (
                    3, frozenset({"t1", "t2", "t3"})),
                attribution_kwargs=[], checkpoints=[], events=events,
                reverse_map={},
                localize=lambda *a, **k: (_frame_blamed_site(),),
                enumerate_fns=lambda src: list(_LIVE_FNS),
                fill=_fill)

    aimed = [d for ev, d in events if ev == "phase_c.crash.producer_aimed"]
    assert aimed and aimed[0]["placeholder_pointer"] is None
    subs = set(aimed[0]["subjects"])
    assert "src/error.rs::object_downcast" in subs
    assert "src/error.rs::context_downcast" not in subs
    armed = [kw for kw in fills if kw.get("crash_evidence")][0]
    text = armed["crash_evidence"][FnLocator("src/error.rs", "object_downcast")]
    assert "PRODUCER CANDIDATE" in text and "PLACEHOLDER POINTER" not in text


def test_crash_subjects_are_dispatched_in_the_sweep_that_named_them(
        tmp_path: Path, monkeypatch) -> None:
    """180.2. The evidence pack gave its subjects a prompt, a frontier entry,
    an exhaustion void and ELIGIBILITY — and nothing gave them PRIORITY, so
    they queued behind a frontier ranked on a different question and the batch
    cap decided how many years later they would be reached.

    Run `1692702-94e6163a` (2026-08-16) is the measurement: the chain blame
    named the five-fn destruction family on iterations 2 and 3, and iteration
    2's batch of 12 out of 57 carried `0 skipped — free fns` while four of the
    five are free fns. `object_drop_front` — byte-identical to `object_drop`
    since the 79-passing checkpoint, and the only one of the five that is wrong
    — has not been dispatched in any run since `2219473-e43ea61c`.

    Here the frontier attribution hands over is 30 units wide and the crash
    subject is appended after all of them, so the pre-fix ranking cannot reach
    it inside a batch of 12."""
    monkeypatch.setenv("PHASE_C_CRASH_RETRIGGER_AFTER", "9")
    events: list[tuple[str, dict]] = []
    fills: list[dict[str, Any]] = []

    def _fill(**kw):
        fills.append(kw)
        return ClusterFillResult(attempts=1)

    wide = {FnLocator("src/other.rs", f"u{i}"): {"t2"} for i in range(30)}
    _run_driver(monkeypatch, tmp_path,
                run_cargo=lambda *a, **k: (101, _MISALIGNED_OUT),
                inventory=lambda pd, cl, stems, lib=False: (
                    3, frozenset({"t1", "t2", "t3"})),
                attribution_kwargs=[], checkpoints=[], events=events,
                reverse_map={}, frontier=wide,
                localize=lambda *a, **k: (_frame_blamed_site(),),
                enumerate_fns=lambda src: list(_LIVE_FNS),
                fill=_fill)

    dispatched = [kw for kw in fills if kw.get("tdd_frontier_keys")]
    assert dispatched, "no batch was dispatched at all"
    keys = dispatched[0]["tdd_frontier_keys"]
    # The blamed frame itself, and the producer the frame cannot name.
    assert FnLocator("src/error.rs", "downcast_ref") in keys
    assert FnLocator("src/error.rs", "object_downcast") in keys, \
        "the producer was eligible and still did not make the batch"
    # NEGATIVE CONTROL, the pre-fix behaviour on the same frontier: ranked
    # after 30 attributed units, neither is inside a batch of 12.
    from pipeline_core.domain.dispatch_batch import take_batch
    unpromoted = [*wide, FnLocator("src/error.rs", "downcast_ref"),
                  FnLocator("src/error.rs", "object_downcast")]
    assert FnLocator("src/error.rs", "object_downcast") not in \
        take_batch(unpromoted, set(), 12).keys


def test_a_best_tree_restore_inside_a_refill_window_closes_the_window(
        tmp_path: Path, monkeypatch) -> None:
    """The restore block's own rule is "every consumer of the score, told in
    the same breath" — and the re-fill window was the consumer nobody told.
    A window granted for a 50-body re-stub describes the lineage the restore
    just replaced; left open it keeps the ratchet, the stall budget and the
    crash streak standing down over a tree that holds no re-stub, until a
    regained-mark close that needs `mark >= highwater_at_grant` (and a prior
    dip) or the wall clock. The combination became reachable when the flat
    guard stopped excusing graced sweeps: a trip — and so a restore — can now
    happen inside an open window. Sequence here: deterministic first-sweep
    shrink fires the retrigger (window granted), flat readings repeat, the
    guard trips, the better tree on disk is restored — and the window must go
    down with the tree it was granted against."""
    from pipeline_core.application.retrigger_layout import RetriggerResult
    from pipeline_core.domain.best_tree_mark import BestTreeMark
    monkeypatch.setenv("PHASE_C_CRASH_RETRIGGER_AFTER", "2")
    monkeypatch.setenv("TDD_REFILL_MAX_ATTEMPTS", "99")
    out = ("test t1 ... FAILED\n"
           "test result: FAILED. 0 passed; 1 failed; 0 ignored")
    fired: list[int] = []

    def _retrig(pd, mod, cl, sd, output, crash_evidence=None, **_kw):
        if fired:
            return RetriggerResult(fired=False, succeeded=False,
                                   reverted_count=0, cause="")
        fired.append(1)
        return RetriggerResult(fired=True, succeeded=True,
                               reverted_count=50, cause="RUNTIME_CRASH")

    events: list[tuple[str, dict]] = []
    _run_driver(
        monkeypatch, tmp_path,
        run_cargo=lambda *a, **k: (101, out),
        inventory=lambda pd, cl, stems, lib=False: (2, frozenset({"t1", "t2"})),
        attribution_kwargs=[], checkpoints=[], events=events,
        retrigger=_retrig,
        reverse_map={"t2": {("src/error.rs", "downcast_ref")}},
        frontier={FnLocator("src/a.rs", f"p{i}"): {"t1"} for i in range(3)},
        fill=lambda **kw: ClusterFillResult(attempts=1, fns_filled=1,
                                            bytes_filled=10),
        guard="2",
        best_mark=BestTreeMark(pass_count=2, iteration=1, tree_sha="a" * 16,
                               oracle_size=2),
        restore=lambda pd, cl, f: (50, 12, []),
    )
    rows = [d for ev, d in events if ev == "phase_c.tdd.best_tree_restored"]
    assert rows, "the scenario must reach the restore to test anything"
    closes = [d for ev, d in events
              if ev == "phase_c.tdd.refill_window_closed"]
    assert any(d.get("early_close") == "tree replaced by best-tree restore"
               for d in closes), (
        "the restore replaced the tree the window was granted against and "
        "left the window open — the ratchet, the stall budget and the crash "
        "streak keep standing down over a tree with no re-stub in it")


_BINS_OUT = ("     Running unittests src/lib.rs (target/debug/deps/x)\n"
             "running 2 tests\n"
             "test t1 ... ok\n"
             "test t2 ... FAILED\n"
             "test result: FAILED. 1 passed; 1 failed; 0 ignored\n")
# Same counts, different verdict set — what a sweep that actually rebuilt the
# restored tree looks like (t2 flipped, t1 regressed: any movement at all).
_BINS_OUT_MOVED = ("     Running unittests src/lib.rs (target/debug/deps/x)\n"
                   "running 2 tests\n"
                   "test t1 ... FAILED\n"
                   "test t2 ... ok\n"
                   "test result: FAILED. 1 passed; 1 failed; 0 ignored\n")


def test_a_restore_the_next_sweep_cannot_see_is_called_unmeasured(
        tmp_path: Path, monkeypatch) -> None:
    """Run `2620196-de4a7ca2` (2026-08-17): the restore replaced 57 body files,
    the next sweep arrived 0.436s later with a per-binary vector byte-identical
    to the pre-restore sweep's (`copy2` kept the snapshot's day-old mtimes, so
    cargo rebuilt nothing), and the once-per-process guard was spent judging
    the DISCARDED tree. The mtime bump removes the cause; this tripwire is what
    makes any recurrence loud: the sweep is announced as an unmeasured restore,
    excused from the flat window, and the high-water re-base is deferred to the
    first sweep that could actually see the restore."""
    from pipeline_core.domain.best_tree_mark import BestTreeMark
    events: list[tuple[str, dict]] = []
    _run_driver(
        monkeypatch, tmp_path,
        run_cargo=lambda *a, **k: (101, _BINS_OUT),
        inventory=lambda pd, cl, stems, lib=False: (2, frozenset({"t1", "t2"})),
        attribution_kwargs=[], checkpoints=[], events=events,
        reverse_map={"t2": {("src/error.rs", "downcast_ref")}},
        frontier={FnLocator("src/a.rs", f"p{i}"): {"t2"} for i in range(3)},
        fill=lambda **kw: ClusterFillResult(attempts=1, fns_filled=1,
                                            bytes_filled=10),
        guard="2",
        best_mark=BestTreeMark(pass_count=2, iteration=1, tree_sha="a" * 16,
                               oracle_size=2),
        restore=lambda pd, cl, f: (57, 12, []),
    )
    restored = [d for ev, d in events if ev == "phase_c.tdd.best_tree_restored"]
    assert restored, "the scenario must reach the restore to test anything"
    restore_iter = restored[0]["iter"]
    unmeasured = [d for ev, d in events
                  if ev == "phase_c.tdd.restore_unmeasured"]
    assert unmeasured and unmeasured[0]["iter"] == restore_iter + 1, (
        "a 57-file restore followed by a byte-identical per-binary vector was "
        "scored as a real reading — the guard spends itself on a measurement "
        "of the discarded tree")
    assert unmeasured[0]["restored_bodies"] == 57
    # The high-water must NOT be re-based to the stale count on that sweep;
    # the re-base stays due and lands on the first measured sweep after it.
    rebases = [d for ev, d in events if ev == "phase_c.tdd.highwater_rebased"]
    assert all(d["iter"] != restore_iter + 1 for d in rebases), (
        "the high-water was re-based to a count measured on the discarded "
        "tree — the whole re-climb is now judged from the wrong lineage")


def test_a_restore_the_next_sweep_DOES_see_is_not_called_unmeasured(
        tmp_path: Path, monkeypatch) -> None:
    """Negative control: when the post-restore sweep's vector moved (the
    binaries rebuilt — what the mtime bump guarantees), the tripwire stays
    silent and the ordinary re-base happens on that sweep."""
    from pipeline_core.domain.best_tree_mark import BestTreeMark
    events: list[tuple[str, dict]] = []
    box = {"restored": False}

    def _restore(pd, cl, f):
        box["restored"] = True
        return (57, 12, [])

    _run_driver(
        monkeypatch, tmp_path,
        run_cargo=lambda *a, **k: (
            101, _BINS_OUT_MOVED if box["restored"] else _BINS_OUT),
        inventory=lambda pd, cl, stems, lib=False: (2, frozenset({"t1", "t2"})),
        attribution_kwargs=[], checkpoints=[], events=events,
        reverse_map={"t2": {("src/error.rs", "downcast_ref")}},
        frontier={FnLocator("src/a.rs", f"p{i}"): {"t2"} for i in range(3)},
        fill=lambda **kw: ClusterFillResult(attempts=1, fns_filled=1,
                                            bytes_filled=10),
        guard="2",
        best_mark=BestTreeMark(pass_count=2, iteration=1, tree_sha="a" * 16,
                               oracle_size=2),
        restore=_restore,
    )
    restored = [d for ev, d in events if ev == "phase_c.tdd.best_tree_restored"]
    assert restored, "the scenario must reach the restore to test anything"
    restore_iter = restored[0]["iter"]
    assert not [d for ev, d in events
                if ev == "phase_c.tdd.restore_unmeasured"], (
        "a sweep whose vector moved measured the restore — announcing it as "
        "unmeasured would excuse readings the flat guard must count")
    rebases = [d for ev, d in events if ev == "phase_c.tdd.highwater_rebased"]
    assert any(d["iter"] == restore_iter + 1 for d in rebases), (
        "the measured post-restore sweep must perform the deferred re-base")
