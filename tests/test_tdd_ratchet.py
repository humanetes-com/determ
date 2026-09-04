"""Pins for the TDD-iter batch ratchet helpers and the
confirm-before-revert rule (a crash-killed binary drops a NONDETERMINISTIC
subset of tests, so a single breached reading is noise until reproduced —
observed live: 22 vs 23 vanished on back-to-back sweeps of an unchanged tree,
and the ratchet wiped every sibling-blocked fill each iter, locking the
plateau)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pipeline_core.application.reconstruct.tdd_ratchet import (
    breach_withheld,
    ratchet_breach,
    restore_src_tree,
    snapshot_due,
    snapshot_src_tree,
)
from pipeline_core.domain.attribution import AttributionResult, FailureKind
from pipeline_core.domain.layout_retrigger import RefillGrace
from pipeline_core.domain.stub_inventory import StubInventory


def _mk_src(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.rs").write_text("orig-a", encoding="utf-8")
    (src / "sub").mkdir()
    (src / "sub" / "b.rs").write_text("orig-b", encoding="utf-8")
    return src


def test_snapshot_captures_all_rs_recursively(tmp_path: Path) -> None:
    src = _mk_src(tmp_path)
    snap = snapshot_src_tree(src)
    assert snap is not None
    assert {Path(p).name for p in snap} == {"a.rs", "b.rs"}


def test_snapshot_none_when_root_absent(tmp_path: Path) -> None:
    assert snapshot_src_tree(tmp_path / "nope") is None


def test_restore_only_rewrites_changed_files(tmp_path: Path) -> None:
    src = _mk_src(tmp_path)
    snap = snapshot_src_tree(src)
    assert snap is not None
    # a.rs regresses (a bad fill), b.rs unchanged.
    (src / "a.rs").write_text("BAD-FILL", encoding="utf-8")
    reverted = restore_src_tree(snap)
    assert reverted == 1                                   # only a.rs rewritten
    assert (src / "a.rs").read_text(encoding="utf-8") == "orig-a"
    assert (src / "sub" / "b.rs").read_text(encoding="utf-8") == "orig-b"


def test_restore_noop_when_nothing_changed(tmp_path: Path) -> None:
    src = _mk_src(tmp_path)
    snap = snapshot_src_tree(src)
    assert snap is not None
    assert restore_src_tree(snap) == 0

    # A file created AFTER the snapshot is left alone (never in the snapshot).
    (src / "c.rs").write_text("new", encoding="utf-8")
    assert restore_src_tree(snap) == 0
    assert (src / "c.rs").exists()


_G = frozenset({"a", "b", "c"})


def test_ratchet_breach_conditions() -> None:
    assert ratchet_breach(14, 14, _G, _G) == ""
    assert ratchet_breach(15, 14, _G, _G) == ""          # improvement, no breach
    assert "pass_count 13<14" in ratchet_breach(13, 14, _G, _G)


def test_costless_crash_is_not_a_breach() -> None:
    """The live deadlock: filling `error.rs::construct` made a binary that was
    passing NOTHING start aborting. Every green test at the snapshot is still
    green, so the tree lost nothing and the fill must survive. Under the old
    "snapshot was crash-free ⇒ any vanish convicts" rule this reverted every
    iteration and the loop re-filled the same three fns forever."""
    assert ratchet_breach(36, 36, _G, _G) == ""


def test_lateral_swap_convicts_at_equal_count() -> None:
    """Count holds while a green test goes away and another arrives — the tree
    churned, so it is not a new baseline. Only a STRICT gain forgives a loss,
    otherwise the loop can thrash forever at a constant score."""
    now = frozenset({"a", "b", "d"})
    breach = ratchet_breach(14, 14, now, _G)
    assert "lateral swap" in breach and "'c'" in breach
    # ...and a strict gain does forgive it.
    assert ratchet_breach(15, 14, now, _G) == ""


def test_crash_partials_cannot_manufacture_a_signal() -> None:
    """Names come from COMPLETED binaries only, so a crashed binary's varying
    partial output is absent from both sides of the comparison rather than
    reading as a nondeterministic loss."""
    assert ratchet_breach(36, 36, frozenset(), frozenset()) == ""


def test_snapshot_due_takes_equal_score_work() -> None:
    assert snapshot_due(0, -1, "sha0", None)        # no snapshot yet
    assert snapshot_due(5, 3, "shaA", "shaB")       # strict improvement
    # The bug this exists for: a stub fill holds the score but changes the tree.
    # Staying on the old snapshot would let a later breach revert re-stub it.
    assert snapshot_due(5, 5, "shaNEW", "shaOLD")
    assert not snapshot_due(5, 5, "shaSAME", "shaSAME")  # nothing moved
    assert not snapshot_due(4, 5, "shaNEW", "shaOLD")    # a regression is not a baseline


def test_breach_withheld_only_after_a_retrigger() -> None:
    """The premise the ratchet convicts on — "this sweep measures last iter's
    fills" — is false on the sweep right after a layout retrigger re-stubs the
    component's fns on purpose. Withheld there, charged everywhere else."""
    _restub = RefillGrace.for_restub(3, fills_so_far=0)
    assert breach_withheld("pass_count 13<14", _restub)
    assert breach_withheld("pass_count 13<14", RefillGrace.none()) == ""
    # No breach, nothing to withhold — the window never invents a verdict.
    assert breach_withheld("", _restub) == ""


def test_breach_withheld_needs_a_restub_to_point_at() -> None:
    """A FIRED-but-failed attempt re-stubbed nothing, so it silences the crash
    streak (no back-to-back re-fire) but has no deliberate re-stub to excuse a
    ratchet breach with. Withholding there would forgive a real regression."""
    _failed = RefillGrace.for_failed_attempt(fills_so_far=7)
    assert _failed.active                     # the streak still stands down
    assert not _failed.bodies_in_flight       # ...and nothing else does
    assert breach_withheld("pass_count 13<14", _failed) == ""


def test_refill_window_is_sized_to_the_bodies_it_restubbed() -> None:
    """The defect this exists for: a FIXED one-iter window is sized to the
    mechanism's smallest case. Live on `anyhow` run `1135197`, a 47-body re-stub
    landed 3 re-fills in its one graced iter, the ratchet then reverted the whole
    `src/` tree — new definition included — and the identical cycle ran twice,
    burning both capped `StdError` attempts."""
    assert RefillGrace.for_restub(47, 0, per_iter=4, max_iters=12).iters_left == 12
    assert RefillGrace.for_restub(12, 0, per_iter=4, max_iters=12).iters_left == 3
    # The smallest re-stub keeps the original semantics EXACTLY — one iter.
    assert RefillGrace.for_restub(3, 0, per_iter=4, max_iters=12).iters_left == 1
    assert RefillGrace.for_restub(1, 0, per_iter=4, max_iters=12).iters_left == 1
    # ...and the ceiling binds however large the re-stub is.
    assert RefillGrace.for_restub(999, 0, per_iter=1, max_iters=4).iters_left == 4


def test_the_defaults_cover_the_case_they_were_measured_on() -> None:
    """The sizing is only "sized to the work" if the CAP does not decide it, and
    it silently did: `ceil(47/4)` is exactly the old 12-iter cap, so the window
    for the very re-stub the design was measured on was clipped — and at the
    measured 3 re-fills/iter, 47 bodies need 16 iters, not 12. The defaults now
    size against the measured rate and the cap stays a runaway bound only."""
    _live = RefillGrace.for_restub(47, 0)     # 47 bodies, default rate
    assert _live.iters_left == 16
    assert not _live.clipped
    # The old defaults sized 47 bodies to exactly their own ceiling, which is why
    # the clip was invisible: nothing was cut, and one more body would have been.
    _at_the_bound = RefillGrace.for_restub(47, 0, per_iter=4, max_iters=12)
    assert _at_the_bound.iters_left == 12 and not _at_the_bound.clipped
    # A cap that DOES decide the size says so, so a future 60-body re-stub cannot
    # look like a sized window.
    _clipped = RefillGrace.for_restub(60, 0, per_iter=4, max_iters=12)
    assert _clipped.iters_left == 12 and _clipped.clipped
    assert not RefillGrace.for_restub(3, 0).clipped


def test_the_window_closes_on_the_wall_clock_it_was_never_bounded_by() -> None:
    """THE SECOND DENOMINATOR. The window is sized in ITERATIONS and nothing
    bounded what an iteration costs. Run `1920662-40f711b2` (2026-08-16/17) ran
    588 minutes for 6 iterations — 98 min/iteration — so the 18 iterations its
    54-body re-stub bought were ~29 hours with the batch ratchet, the stall
    budget and the crash streak all standing down."""
    g = RefillGrace.for_restub(54, fills_so_far=0, granted_at_minute=10.0,
                               max_minutes=360.0)
    assert g.iters_left == 18
    # Inside the ceiling the window behaves exactly as before.
    assert g.consume(5, 100.0).active
    assert not g.early_close_reason(100.0)
    # …and at it, the window ends with its iteration budget unspent, saying so.
    reason = g.early_close_reason(370.0)
    assert "wall-clock ceiling" in reason and "18 iter(s) of the budget" in reason
    assert not g.consume(5, 370.0).active
    # NEGATIVE CONTROL — the pre-fix rule is `max_minutes=0`, and under it the
    # same 6-hour-old window is still open. Without this the case above asserts
    # a close that an unconditionally-closing window would also produce.
    old = RefillGrace.for_restub(54, fills_so_far=0, granted_at_minute=10.0)
    assert old.consume(5, 370.0).active, (
        "the control no longer reproduces the unbounded window")


def test_the_window_closes_once_the_re_climb_regains_its_own_high_water() -> None:
    """THE THIRD DENOMINATOR. Once the score is back at the mark the window was
    re-based from, the re-fills have finished paying by the window's own
    definition of paying and every gate it holds down describes the tree again.

    It needs a DIP first, and that qualifier is not decoration: walk
    `3368-753757e6` reverted 25 bodies with pass 39 on both sides of the fire,
    so an unqualified rule would close a 9-iteration window on its first sweep
    and protect nothing."""
    g = RefillGrace.for_restub(54, fills_so_far=0, highwater_at_grant=83)
    # Never dipped — the re-stub cost no tests — so the clause stays silent.
    assert not g.early_close_reason(pass_count=83)
    assert g.consume(5, pass_count=83).active
    # The recorded shape: 83 -> re-stub -> 25 -> 33 -> ... -> 83.
    g = g.consume(5, pass_count=25)
    assert g.active and g.dipped
    assert not g.early_close_reason(pass_count=33)
    assert "re-climb reached 83" in g.early_close_reason(pass_count=83)
    assert not g.consume(9, pass_count=83).active
    # NEGATIVE CONTROL — a grant with no high-water recorded (-1) can never
    # close this way, which is what every caller that does not measure one gets.
    blind = RefillGrace.for_restub(54, fills_so_far=0)
    assert blind.consume(5, pass_count=999).active


def test_refill_window_survives_only_while_re_fills_land() -> None:
    """What keeps a sized window safe. Progress is counted in FILLS, not pass
    count: the re-stubbed bodies are mostly consumers blocked on still-stubbed
    producers, so they fill in dependency order for several iters before any test
    can move (25 of 31 frontier fns, live). An iter that fills nothing has no
    re-author in flight, whatever budget is left."""
    g = RefillGrace.for_restub(47, fills_so_far=100, per_iter=4, max_iters=12)
    assert g.iters_left == 12
    g = g.consume(103)               # 3 re-fills landed — still in flight
    assert g.active and g.iters_left == 11
    g = g.consume(120)               # 17 more
    assert g.active and g.iters_left == 10
    assert not g.consume(120).active  # nothing landed → window closes now
    # The budget is a hard bound even while re-fills keep landing.
    g = RefillGrace.for_restub(4, fills_so_far=0, per_iter=4, max_iters=12)
    assert g.iters_left == 1
    assert not g.consume(99).active
    # An already-closed window stays closed.
    assert not RefillGrace.none().consume(500).active


def _ledger_writer():
    """The real tree adapters, wired as the re-seed's `TreeWriter`.

    Real ones rather than fakes: what these tests are about is a body going
    back onto a tree and surviving a build check, and a fake splice would test
    the test."""
    from pipeline_core.adapters.fn_reseed import (
        current_bodies,
        current_signatures,
        splice_bodies,
    )
    from pipeline_core.application.reconstruct.body_reseed import TreeWriter
    return TreeWriter(splice=splice_bodies,
                      signatures=current_signatures, bodies=current_bodies)


def _oracle_fp_adapter():
    from pipeline_core.adapters.oracle_fingerprint import cluster_oracle_fp
    return cluster_oracle_fp


def _driver_deps(run_cargo, inventory, events, retrigger=None, *,
                 frontier=None, fills_per_iter=0, graceful_stop=None, fill=None,
                 best_mark=None, checkpoints=None, resets=None, stubs=None,
                 retires=None, logs=None, reset_returns=None,
                 localize=None, escalate=None, reverse_map=None,
                 floor=None, enumerate_fns=None, ledger=None, tree_stubs=None,
                 coverage=None, attribute=None):
    from pipeline_core.application.reconstruct.phase_c_tdd import PhaseCTddDeps
    from pipeline_core.domain.models import ClusterFillResult

    def _checkpoint(pd, cluster_name, filled_dir, mark):
        """The durable best-tree save. Recording the MARK rather than a bare
        call count is what lets a test say which tree was stored: the defect
        this harness exists for is a worse tree replacing a better one, and
        both spellings of that are one call."""
        if checkpoints is not None:
            checkpoints.append(mark)

    def _retire(pd, by_file, cluster_name, *, cfg_dead=frozenset()):
        # `cfg_dead` is the cluster's positive census of cfg-inactive stubs,
        # threaded so a cfg-dead blocker reads as cleared; the replay records
        # the map and ignores the census (no test models cfg here).
        if retires is not None:
            retires.append((cluster_name, {f: set(n) for f, n in by_file.items()}))
        return 0

    def _reset(pd, reason):
        # The reason names the iter and the two pass counts it compared, so a
        # test can pin WHICH sweep bought the release, not merely that one did.
        # `reset_returns` are the RELEASED KEYS the disk store reports — the
        # driver reopens its loop-local gates for exactly these identities.
        if resets is not None:
            resets.append(reason)
        return list(reset_returns or [])

    return PhaseCTddDeps(
        # `attribute` overrides the whole attribution result — a walk about
        # the attribution product (the hop's pairs, the binding it read)
        # supplies its own; every other test keeps the static frontier.
        attribute_failures=attribute or (lambda *a, **k: AttributionResult(
            dict(frontier or {}), FailureKind.NO_FRONTIER,
            dict(frontier or {}))),
        build_test_reverse_map=lambda *a, **k: dict(reverse_map or {}),
        filter_test_targets=lambda frontier, src: frontier,
        check_graceful_stop=graceful_stop or (lambda *a, **k: None),
        check_auto_operator=lambda *a, **k: None,
        converge_in_cluster=lambda *a, **k: None,
        count_relevant_notes=lambda *a, **k: 0,
        effective_stalls=lambda budget, module: budget,
        emit=lambda ev, **k: events.append((ev, k.get("data") or {})),
        log=(logs.append if logs is not None else (lambda *a, **k: None)),
        order_frontier=lambda eligible, counts: list(eligible.items()),
        record_tdd_regression=lambda *a, **k: "",
        reset_exhaustion_flags=_reset,
        run_cargo=run_cargo,
        scan_unimplemented_for_cluster=lambda *a, **k: StubInventory.of_units(stubs or []),
        # The whole-tree stub scan. `[]` is the pre-ledger default every other
        # test wants (it feeds `newly_filled`); `tree_stubs` is for the tests
        # that need the loop to SEE stubs — the re-seed asks this dep which
        # units it may put a body back into.
        scan_unimplemented_functions=lambda *a, **k: StubInventory.of_units(tree_stubs or []),
        task2_fill_fns_one_by_one=fill or (lambda **kw: ClusterFillResult(
            attempts=1, fns_filled=fills_per_iter)),
        checkpoint_fill_tree=_checkpoint,
        # None means "the driver cannot see the slot", which is the pre-seed
        # behaviour every test that does not exercise the durable tier wants.
        load_best_tree_mark=(None if best_mark is None
                             else (lambda pd, cl: best_mark)),
        retire_unblocked_convictions=_retire,
        active_test_inventory=inventory,
        retrigger_layout=retrigger,
        localize_crashes=localize,
        measure_coverage_slice=coverage,
        escalate_to_producers=escalate,
        # The extractor's per-fn records, so a replay can model the
        # landed-change reading. None (the default) is the pre-existing walk
        # behaviour: no body comparison, so `landed`/`declined` stay empty and
        # every carried cursor entry reads as "produced nothing".
        enumerate_fns=enumerate_fns,
        # `floor` is a mutable dict standing in for the on-disk per-cluster
        # floor file — pass the SAME dict to two runs to model two processes
        # sharing `.phase_c_logs`, which is what Stage A persistence is for.
        # `ledger` is a mutable dict standing in for the on-disk proven-body
        # ledger (`logs/body_ledger/<project>/`), the same shape `floor` uses
        # for the floor file: pass the SAME dict to two runs to model two
        # processes sharing it. Absent means the ledger is not wired at all,
        # which is the pre-ledger behaviour every other test wants.
        **({} if ledger is None else {
            "ledger_load": lambda pd, cl: [
                e for e in ledger.get("rows", []) if e.key.cluster == cl],
            "ledger_append": lambda pd, rows: (
                ledger.setdefault("rows", []).extend(rows) or len(rows)),
            "ledger_size": lambda pd: (len(ledger.get("rows", [])), 0),
            "layout_items": lambda pd: dict(ledger.get("items", {})),
            "tree_writer": _ledger_writer(),
            "check_tree_builds": (
                None if ledger.get("check") is None
                else (lambda pd, cl: ledger["check"]())),
            # The REAL oracle adapter over the test's tmp tree, for the same
            # reason `_ledger_writer` wires the real splice: what the wiring
            # tests are about is the driver reaching the adapters. A test
            # moves the oracle by writing a gen_tests sibling into `src/`.
            "oracle_fp": _oracle_fp_adapter(),
        }),
        **({} if floor is None else {
            "load_tdd_floor": lambda *a, **k: floor.get("row"),
            "save_tdd_floor": lambda p, c, row: floor.__setitem__("row", row),
        }),
    )


def _run_tdd(monkeypatch, tmp_path: Path, outputs, events, retrigger=None, *,
             frontier=None, fills_per_iter=0, max_stalls="2",
             graceful_stop=None, fill=None, src_text: str = "orig",
             mutate_text: str = "FILLED", **kw) -> None:
    """Drive the TDD loop with ratchet ON over a scripted cargo-output list.
    Each cargo call pops the next output (last one repeats); the call ALSO
    mutates src/a.rs when the popped entry says so — simulating a fill that
    landed between the snapshot and a later sweep."""
    from pipeline_core.application.reconstruct.phase_c_tdd import run_phase_c_tdd
    from pipeline_core.domain.models import BuildCluster
    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "1")
    monkeypatch.setenv("TDD_MAX_STALLS", max_stalls)
    # The flat-score GPU guard would stop these runs at their second
    # identical reading, which is exactly what it is for and exactly what
    # this module is not about: the mechanisms under test here sit BELOW
    # it in the same iteration (the plateau budget, the ratchet, the
    # conviction rules), so the guard is off and their controls keep
    # measuring what they were written to measure. `tests/test_tdd_gpu_guard.py`
    # owns the guard itself.
    monkeypatch.setenv("TDD_FLAT_SCORE_REPEATS", "0")
    # Same reason, the protector's other clock: these fixtures land bodies
    # against a per-binary vector that never moves, so `UnmovedVerdicts` would
    # stop the run at its third judged sweep — correctly, and before the
    # plateau budget and the ratchet this module pins have had their say.
    # `tests/test_tdd_no_verdict_clock.py` owns that clock.
    monkeypatch.setenv("TDD_NO_VERDICT_SWEEPS", "0")
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "a.rs").write_text(src_text, encoding="utf-8")
    seq = list(outputs)

    def _cargo(*a: Any, **k: Any):
        entry = seq.pop(0) if len(seq) > 1 else seq[0]
        out, mutate, rc = entry if len(entry) == 3 else (*entry, 101)
        if mutate:
            (src / "a.rs").write_text(mutate_text, encoding="utf-8")
        return rc, out

    run_phase_c_tdd(
        _driver_deps(_cargo, lambda pd, cl, stems, lib=False: (2, frozenset({"t1", "t2"})),
                     events, retrigger, frontier=frontier,
                     fills_per_iter=fills_per_iter,
                     graceful_stop=graceful_stop, fill=fill, **kw),
        project_dir=tmp_path, target_src_dir=tmp_path, tests_dir_files=[],
        cluster_tests=[("tests/t.rs", "fn t1() {}")], module="m",
        cluster=BuildCluster(
            name="stable", env_extra={}, tarpaulin_exclude_files=[],
            excluded_cfg=[], active_cfg=[], prompt_nightly_rule=""),
        check_cmd="cargo check", cluster_env={}, leaf_counts=None, fr_path=None,
        test_cmd_builder=lambda stems, include_lib=False: "cargo test", tag="[stable]")


_INTACT = ("test t1 ... ok\ntest t2 ... FAILED\n"
           "test result: FAILED. 1 passed; 1 failed; 0 ignored")
# t2's binary crashed — it vanishes from the output (oracle shrinks by 1).
# t2 was FAILING at the snapshot, so the crash cost the tree nothing.
_SHRUNK = ("test t1 ... ok\n"
           "test result: FAILED. 1 passed; 0 failed; 0 ignored")
# t1's binary crashed instead — a test that was PASSING at the snapshot lost
# its verdict, so this shrink IS a net loss.
_LOST_PASS = ("test t2 ... FAILED\n"
              "test result: FAILED. 0 passed; 1 failed; 0 ignored")


def test_unreproduced_breach_does_not_revert(tmp_path: Path, monkeypatch) -> None:
    # Iter 1 snapshots the intact tree; a fill lands; iter 2's first sweep
    # loses a passing test (noise) but the confirm re-sweep is intact again →
    # the fill must survive and no batch_revert event fires.
    events: list[tuple[str, dict]] = []
    _run_tdd(monkeypatch, tmp_path,
             [(_INTACT, False), (_LOST_PASS, True), (_INTACT, False)], events)
    assert (tmp_path / "src" / "a.rs").read_text(encoding="utf-8") == "FILLED"
    assert not any(ev == "phase_c.tdd.batch_revert" for ev, _ in events)


def test_reproduced_breach_reverts_to_snapshot(tmp_path: Path, monkeypatch) -> None:
    # Iter 2's loss REPRODUCES on the confirm re-sweep → the revert fires
    # and the mutated file returns to the snapshotted best state.
    events: list[tuple[str, dict]] = []
    _run_tdd(monkeypatch, tmp_path,
             [(_INTACT, False), (_LOST_PASS, True), (_LOST_PASS, False)], events)
    assert (tmp_path / "src" / "a.rs").read_text(encoding="utf-8") == "orig"
    reverts = [d for ev, d in events if ev == "phase_c.tdd.batch_revert"]
    assert reverts and "pass_count 0<1" in reverts[0]["breach"]


def test_crash_that_costs_nothing_keeps_the_fill(tmp_path: Path, monkeypatch) -> None:
    """The live deadlock, end to end. The fill makes an all-red binary abort;
    every test that was green at the snapshot is still green. The old rule
    ("snapshot was crash-free ⇒ any vanish convicts") reverted here on EVERY
    iteration, so the route re-filled the same fns and lost them again forever
    at zero payoff. A crash costs exactly what it was passing — here, nothing."""
    events: list[tuple[str, dict]] = []
    _run_tdd(monkeypatch, tmp_path,
             [(_INTACT, False), (_SHRUNK, True), (_SHRUNK, False)], events)
    assert (tmp_path / "src" / "a.rs").read_text(encoding="utf-8") == "FILLED"
    assert not any(ev == "phase_c.tdd.batch_revert" for ev, _ in events)


_ZERO = ("test t1 ... FAILED\ntest t2 ... FAILED\n"
         "test result: FAILED. 0 passed; 2 failed; 0 ignored")


def test_equal_score_fill_survives_a_later_revert(tmp_path: Path, monkeypatch) -> None:
    # Iter 2's fill holds the pass count (the fingerprint of a stub fill: the
    # panic is gone, no test moved yet) so it must become the new best state.
    # Iter 3 then net-regresses; restoring must land on the FILLED tree, never
    # re-stub it — otherwise the next iter re-fills the same fn, forever.
    events: list[tuple[str, dict]] = []
    _run_tdd(monkeypatch, tmp_path,
             [(_INTACT, False), (_INTACT, True), (_ZERO, False)], events)
    assert (tmp_path / "src" / "a.rs").read_text(encoding="utf-8") == "FILLED"
    reverts = [d for ev, d in events if ev == "phase_c.tdd.batch_revert"]
    assert reverts and "pass_count 0<1" in reverts[0]["breach"]


def _one_shot_retrigger(src: Path):
    """A layout retrigger that fires once and does what the real one does:
    re-stub the fns referencing the re-authored component (here, rewrite the
    file) so they re-fill under the new shape."""
    from pipeline_core.application.retrigger_layout import RetriggerResult
    fired: list[int] = []

    def _retrig(*a: Any, **k: Any) -> RetriggerResult:
        if fired:
            return RetriggerResult(fired=False, reason="capped")
        fired.append(1)
        (src / "a.rs").write_text("RESTUBBED", encoding="utf-8")
        return RetriggerResult(fired=True, succeeded=True, reverted_count=3,
                               cause="RUNTIME_CRASH")
    return _retrig


def test_retrigger_restub_is_not_charged_to_last_iters_fills(
        tmp_path: Path, monkeypatch) -> None:
    """The live cycle this pins out. Iter 1's crash fires the retrigger, which
    re-stubs 3 fns on purpose; iter 2's sweep is therefore BELOW the snapshot
    by construction. Charging it ran `restore_src_tree` over all of `src/`,
    which took the re-authored DEFINITION with it — the crash returned, the
    capped attempt was spent, and the next attempt repeated the cycle
    (`ChainState` attempts 1 and 2, one iter apart, pass_count pinned at its
    high-water mark the whole time). Given its one iter, the re-fills recover
    the count and the new layout is kept."""
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    events: list[tuple[str, dict]] = []
    _run_tdd(monkeypatch, tmp_path,
             [(_SHRUNK, False), (_SHRUNK, False), (_LOST_PASS, False),
              (_INTACT, False)], events,
             retrigger=_one_shot_retrigger(src))
    assert (src / "a.rs").read_text(encoding="utf-8") == "RESTUBBED"
    withheld = [d for ev, d in events if ev == "phase_c.tdd.batch_withheld"]
    assert withheld and "pass_count 0<1" in withheld[0]["breach"]
    assert not any(ev == "phase_c.tdd.batch_revert" for ev, _ in events)


def _big_restub_retrigger(src: Path, reverted: int):
    """A retrigger that fires once and re-stubs `reverted` bodies — the real
    shape of a large component re-author (`StdError`: 47)."""
    from pipeline_core.application.retrigger_layout import RetriggerResult
    fired: list[int] = []

    def _retrig(*a: Any, **k: Any) -> RetriggerResult:
        if fired:
            return RetriggerResult(fired=False, reason="capped")
        fired.append(1)
        (src / "a.rs").write_text("RESTUBBED", encoding="utf-8")
        return RetriggerResult(fired=True, succeeded=True,
                               reverted_count=reverted, cause="RUNTIME_CRASH")
    return _retrig


def test_big_restub_gets_more_than_one_iter_while_re_fills_land(
        tmp_path: Path, monkeypatch) -> None:
    """The wiring for the sized window, which is where it can silently become a
    no-op: the progress test must read the loop's OWN fill counter. A 47-body
    re-stub with re-fills landing every iter must be withheld repeatedly and the
    re-authored tree must survive — live, one iter bought 3 of 47 re-fills, the
    ratchet reverted all of `src/`, and the cycle burnt both capped attempts."""
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    events: list[tuple[str, dict]] = []
    from pipeline_core.domain.models import FnLocator
    _run_tdd(monkeypatch, tmp_path,
             [(_SHRUNK, False), (_LOST_PASS, False)], events,
             retrigger=_big_restub_retrigger(src, 47),
             frontier={FnLocator("src/a.rs", "f"): {"t2"}},
             fills_per_iter=3, max_stalls="6")
    names = [ev for ev, _ in events]
    withheld = [d for ev, d in events if ev == "phase_c.tdd.batch_withheld"]
    assert len(withheld) > 1, "a 47-body re-stub got only one iter again"
    assert withheld[0]["restubbed"] == 47
    # Every withheld iter spends exactly one of the sized budget.
    _granted = withheld[0]["grace_iters_left"]
    assert _granted == 16, "47 bodies at the measured 3/iter"
    assert [d["grace_iters_left"] for d in withheld] == list(
        range(_granted, _granted - len(withheld), -1))
    # The re-author is safe only WHILE the window is open — and the fixture's fn
    # caps out its re-fill attempts, so the window closes on the first iter that
    # lands nothing and the ordinary rule takes over from the next sweep. That
    # ordering is the whole safety argument: never a revert under an open window,
    # always one once it shuts.
    assert names.index("phase_c.tdd.refill_window_closed") < names.index(
        "phase_c.tdd.batch_revert")
    assert names.index("phase_c.tdd.batch_withheld") < names.index(
        "phase_c.tdd.refill_window_closed")


def test_window_closes_the_iter_re_fills_stop_landing(
        tmp_path: Path, monkeypatch) -> None:
    """What makes a 12-iter budget safe. The same 47-body re-stub, but no fill
    lands: the window closes on its first scored iter and the ordinary rule
    convicts, so a stalled re-author cannot hold the ratchet open for its whole
    budget."""
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    events: list[tuple[str, dict]] = []
    from pipeline_core.domain.models import FnLocator
    _run_tdd(monkeypatch, tmp_path,
             [(_SHRUNK, False), (_LOST_PASS, False)], events,
             retrigger=_big_restub_retrigger(src, 47),
             frontier={FnLocator("src/a.rs", "f"): {"t2"}},
             fills_per_iter=0, max_stalls="6")
    assert [ev for ev, _ in events].count("phase_c.tdd.batch_withheld") == 1
    closed = [d for ev, d in events if ev == "phase_c.tdd.refill_window_closed"]
    assert closed and closed[0]["fills_this_iter"] == 0
    assert any(ev == "phase_c.tdd.batch_revert" for ev, _ in events)
    assert (src / "a.rs").read_text(encoding="utf-8") == "orig"


def test_open_window_does_not_burn_the_stall_budget(
        tmp_path: Path, monkeypatch) -> None:
    """The window is 12 iters and the stall budget is 3, so the budget had to
    learn about it too — the third consumer of the same depressed count. Left
    accumulating, the loop takes its stall exit MID-window, and that exit is a
    bare `break`: the cluster would hand on the re-stub's low point, below the
    high-water the window exists to protect. With max_stalls=1 the old
    accounting would exit on the first graced iter."""
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    events: list[tuple[str, dict]] = []
    from pipeline_core.domain.models import FnLocator
    _run_tdd(monkeypatch, tmp_path,
             [(_SHRUNK, False), (_LOST_PASS, False)], events,
             retrigger=_big_restub_retrigger(src, 47),
             frontier={FnLocator("src/a.rs", "f"): {"t2"}},
             fills_per_iter=3, max_stalls="1")
    kinds = [d.get("kind") for ev, d in events if ev == "phase_c.tdd.iter"]
    assert [ev for ev, _ in events].count("phase_c.tdd.batch_withheld") > 1, \
        "the stall budget exited the loop inside the re-fill window"
    # ...and once the window closes the budget resumes and does its job.
    assert "stall_exit" in kinds


def test_exiting_with_an_open_window_returns_the_best_tree(
        tmp_path: Path, monkeypatch) -> None:
    """Abandoning the bet must return the baseline. The reachable path is a cargo
    TIMEOUT, which a big re-stub makes MORE likely, not less — the re-stubbed
    bodies panic inside drop/vtable code and a binary can hang instead of
    aborting. That exit is a bare `break`, so without this the cluster hands on
    the worst tree of the run: a re-author whose bodies never finished
    re-filling, scoring below the high-water by construction."""
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    events: list[tuple[str, dict]] = []
    from pipeline_core.domain.models import FnLocator
    # iter 1 snapshots the intact tree; iter 2's shrink is confirmed by the
    # accelerated re-sweep and fires the retrigger (47 bodies re-stubbed); iter 3
    # then times out with the window still open.
    _run_tdd(monkeypatch, tmp_path,
             [(_INTACT, False), (_SHRUNK, False), (_SHRUNK, False),
              ("", False, -1)], events,
             retrigger=_big_restub_retrigger(src, 47),
             frontier={FnLocator("src/a.rs", "f"): {"t2"}},
             fills_per_iter=3, max_stalls="6")
    kinds = [d.get("kind") for ev, d in events if ev == "phase_c.tdd.iter"]
    assert "timeout" in kinds, "the fixture did not reach the timeout break"
    assert any(ev == "phase_c.layout_retrigger.fired" for ev, _ in events)
    restored = [d for ev, d in events if ev == "phase_c.tdd.open_window_restored"]
    assert restored and restored[0]["restubbed"] == 47
    assert (src / "a.rs").read_text(encoding="utf-8") == "orig"


_GREEN = ("test t1 ... ok\ntest t2 ... ok\n"
          "test result: ok. 2 passed; 0 failed; 0 ignored")


def test_a_green_exit_is_not_an_abandoned_bet(
        tmp_path: Path, monkeypatch) -> None:
    """The other side of the restore, and the reason it cannot key on the window
    alone: the LAST re-fill of a re-author greens the cluster while its window is
    still open. That exit is the bet PAYING — restoring there would roll the
    winning tree back to the pre-re-author baseline (harmless only for as long as
    the same sweep also re-snapshots) and log that it did."""
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    events: list[tuple[str, dict]] = []
    from pipeline_core.domain.models import FnLocator
    _run_tdd(monkeypatch, tmp_path,
             [(_INTACT, False), (_SHRUNK, False), (_SHRUNK, False),
              (_GREEN, False, 0)], events,
             retrigger=_big_restub_retrigger(src, 47),
             frontier={FnLocator("src/a.rs", "f"): {"t2"}},
             fills_per_iter=3, max_stalls="6")
    kinds = [d.get("kind") for ev, d in events if ev == "phase_c.tdd.iter"]
    assert "pass" in kinds, "the fixture never reached the green exit"
    assert not any(ev == "phase_c.tdd.open_window_restored" for ev, _ in events)
    # The re-authored tree is what the cluster hands on, untouched.
    assert (src / "a.rs").read_text(encoding="utf-8") == "RESTUBBED"


@pytest.mark.parametrize("stop_at", ["iter boundary", "fill pass"])
def test_a_graceful_stop_mid_window_returns_the_best_tree(
        tmp_path: Path, monkeypatch, stop_at: str) -> None:
    """A graceful stop is `sys.exit(0)`: it leaves the loop by RAISING, past every
    `break` — so an exit restore that only runs after the loop does not run at all,
    and the resumed run opens on the re-stub's low point instead of the high-water
    (`111 -> 20`, live). The fill pass is where a stop lands in practice because it
    owns the hours of the iter and runs its own stop checks; the iter boundary is
    the other reachable point."""
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    events: list[tuple[str, dict]] = []
    from pipeline_core.domain.models import ClusterFillResult, FnLocator

    def _window_is_open() -> bool:
        return any(ev == "phase_c.layout_retrigger.fired" for ev, _ in events)

    def _stop_at_boundary(*a: Any, **k: Any) -> None:
        if stop_at == "iter boundary" and _window_is_open():
            raise SystemExit(0)

    def _stop_in_fill(**kw: Any) -> ClusterFillResult:
        if stop_at == "fill pass" and _window_is_open():
            raise SystemExit(0)
        return ClusterFillResult(attempts=1, fns_filled=3)

    # iter 1 snapshots the intact tree; iter 2's shrink is confirmed on the
    # re-sweep and fires the retrigger; from iter 3 the re-stub costs a passing
    # test, as a real one does (111 -> 20 live) — so the baseline cannot be
    # re-taken over the re-stubbed tree and the stop has something to return to.
    with pytest.raises(SystemExit):
        _run_tdd(monkeypatch, tmp_path,
                 [(_INTACT, False), (_SHRUNK, False), (_SHRUNK, False),
                  (_LOST_PASS, False)], events,
                 retrigger=_big_restub_retrigger(src, 47),
                 frontier={FnLocator("src/a.rs", "f"): {"t2"}},
                 max_stalls="6", graceful_stop=_stop_at_boundary,
                 fill=_stop_in_fill)
    restored = [d for ev, d in events if ev == "phase_c.tdd.open_window_restored"]
    assert restored, f"a stop at the {stop_at} kept the re-stubbed tree"
    assert restored[0]["restubbed"] == 47
    assert (src / "a.rs").read_text(encoding="utf-8") == "orig"


def _failing_retrigger(events: list[tuple[str, dict]]):
    """A retrigger that FIRES and does not land: the mechanical splice failure
    (`StdError could not be located in context.rs`). It refunds the attempt on
    purpose, so it re-fires on identical evidence — which is what makes the one
    iter of silence load-bearing. Records the iter of each crash-site call."""
    from pipeline_core.application.retrigger_layout import RetriggerResult
    fired_at: list[int] = []

    def _retrig(*a: Any, **k: Any) -> RetriggerResult:
        if k.get("crash_evidence") is not None:
            fired_at.append(
                1 + sum(1 for ev, _ in events if ev == "phase_c.tdd.iter"))
        return RetriggerResult(fired=True, succeeded=False,
                               reason="could not be located in context.rs")
    return _retrig, fired_at


def test_a_failed_attempt_is_not_re_fired_in_the_next_iter(
        tmp_path: Path, monkeypatch) -> None:
    """The grant that does NOT `continue`, and the one place the window can be
    spent by the very iter that opened it — which made it a no-op: the crash site
    then re-fired in FIVE consecutive iters, each one a full layout-authoring call
    on evidence a fill pass had not yet had the chance to change, exhausting the
    component's capped attempts in as many iters. One iter of window has to mean
    one iter of reads."""
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    events: list[tuple[str, dict]] = []
    from pipeline_core.domain.models import FnLocator
    _retrig, fired_at = _failing_retrigger(events)
    _run_tdd(monkeypatch, tmp_path, [(_SHRUNK, False)], events,
             retrigger=_retrig,
             frontier={FnLocator("src/a.rs", "f"): {"t2"}},
             fills_per_iter=3, max_stalls="4")
    assert len(fired_at) > 1, "the fixture never re-fired, so it pins nothing"
    assert all(b - a > 1 for a, b in zip(fired_at, fired_at[1:], strict=False)), \
        f"a failed attempt re-fired back-to-back: fired in iters {fired_at}"
    # It bought silence, not a plateau: nothing was re-stubbed, so the stall
    # budget kept accruing and still ends the cluster.
    assert "stall_exit" in [d.get("kind") for ev, d in events
                            if ev == "phase_c.tdd.iter"]


def test_a_fired_retrigger_still_reports_its_iter(
        tmp_path: Path, monkeypatch) -> None:
    """Every retrigger path `continue`s before the loop's normal iter emit, so
    `events.jsonl` silently skipped exactly the iters the mechanism fired in. Run
    `1135197` set its peak (112) and both of its 111s in such iters, and all three
    had to be reconstructed from log text because `tools/pass_delta.py`,
    `tools/run_health.py` and the audit's iteration series read this event."""
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    events: list[tuple[str, dict]] = []
    from pipeline_core.domain.models import FnLocator
    _run_tdd(monkeypatch, tmp_path,
             [(_INTACT, False), (_SHRUNK, False), (_SHRUNK, False)], events,
             retrigger=_big_restub_retrigger(src, 47),
             frontier={FnLocator("src/a.rs", "f"): {"t2"}},
             fills_per_iter=3, max_stalls="6")
    assert any(ev == "phase_c.layout_retrigger.fired" for ev, _ in events), \
        "the fixture never fired the retrigger"
    fired = [d for ev, d in events
             if ev == "phase_c.tdd.iter" and d.get("kind") == "retrigger_crash"]
    assert fired, "the iter a retrigger fired in emitted no phase_c.tdd.iter"
    # Same payload as the normal emit — a consumer must not need to know which
    # path produced it — plus the size of the re-stub that made the iter special.
    assert fired[0]["retrigger_reverted"] == 47
    for _key in ("pass_count", "fail_count", "tree_sha", "oracle_size",
                 "binaries", "stall"):
        assert _key in fired[0], _key


def test_retrigger_grace_lasts_exactly_one_iter(
        tmp_path: Path, monkeypatch) -> None:
    """The other half: a SMALL re-stub buys the re-author one iter of re-fills,
    not immunity — `for_restub` sizes 3 bodies to exactly the original one iter.
    If the tree is still short at the next iter the ordinary rule convicts —
    against the same untouched snapshot, so nothing was lost by waiting. Without
    this the fix would simply disable the ratchet."""
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    events: list[tuple[str, dict]] = []
    _run_tdd(monkeypatch, tmp_path,
             [(_SHRUNK, False), (_SHRUNK, False), (_LOST_PASS, False),
              (_LOST_PASS, False)], events,
             retrigger=_one_shot_retrigger(src))
    assert [ev for ev, _ in events].count("phase_c.tdd.batch_withheld") == 1
    reverts = [d for ev, d in events if ev == "phase_c.tdd.batch_revert"]
    assert reverts and "pass_count 0<1" in reverts[0]["breach"]
    assert (src / "a.rs").read_text(encoding="utf-8") == "orig"


# --------------------------------------------------------------------------- #
# The DURABLE best tree. `.best_tree/<cluster>/` outlives the process; the two
# variables that decided whether a sweep may replace it did not. See
# `domain/best_tree_mark.py`.
# --------------------------------------------------------------------------- #
def test_worse_first_sweep_does_not_overwrite_the_durable_best_tree(
        tmp_path: Path, monkeypatch) -> None:
    """The 81 -> 21 case at the driver level.

    `.best_tree/<cluster>/` outlives the process; `_checkpoint_best` and
    `_checkpoint_sha` did not, and `snapshot_due` reads a `None` sha as "no
    snapshot yet". So the first sweep of every process replaced the stored tree
    however much worse it was — and the save is a replacement, not a merge.
    Live on `anyhow` 2026-08-07: run `241132-f9fae0a6` left `.best_tree/stable`
    at 81 passing on a 108-test oracle, run `259637-36db9aad` resumed it, the
    crash retrigger re-stubbed 56 bodies on purpose, and the 21-passing sweep
    that followed became the stored tree — the same sweep the batch ratchet
    WITHHELD from charging, because a deliberate re-stub is not a regression.
    """
    from pipeline_core.domain.best_tree_mark import BestTreeMark
    events: list[tuple[str, dict]] = []
    marks: list[Any] = []
    _run_tdd(monkeypatch, tmp_path, [(_INTACT, False)], events,
             best_mark=BestTreeMark(pass_count=2, iteration=9,
                                    tree_sha="208bdf5ec9a2ec18", oracle_size=2),
             checkpoints=marks)
    assert marks == []


def test_beating_the_durable_best_tree_still_checkpoints(
        tmp_path: Path, monkeypatch) -> None:
    from pipeline_core.domain.best_tree_mark import BestTreeMark
    events: list[tuple[str, dict]] = []
    marks: list[Any] = []
    _run_tdd(monkeypatch, tmp_path, [(_INTACT, False)], events,
             best_mark=BestTreeMark(pass_count=0, iteration=3,
                                    tree_sha="old", oracle_size=2),
             checkpoints=marks)
    assert len(marks) == 1
    assert marks[0].oracle_size == 2 and marks[0].tree_sha != "old"


def test_no_durable_mark_keeps_the_pre_seed_behaviour(
        tmp_path: Path, monkeypatch) -> None:
    events: list[tuple[str, dict]] = []
    marks: list[Any] = []
    _run_tdd(monkeypatch, tmp_path, [(_INTACT, False)], events,
             checkpoints=marks)
    assert len(marks) == 1


def test_a_verified_floor_beats_a_stale_mark_despite_a_short_oracle(
        tmp_path: Path, monkeypatch) -> None:
    """The freeze, at the driver level. Missing tests are counted as failing
    BEFORE pass_count is read, and the counter is trailer-only — a crashed
    binary contributes zero passes — so a sweep's pass_count is a verified
    floor: every counted pass ran green in a completed binary. A floor
    strictly above the stored mark dominates it whatever the missing tests
    would have said. Live on `anyhow`, 2026-08-09/10: the mark froze at
    18/105 (written 08:56 UTC) while sweeps read 81/105 and 88/106 under a
    chronic `tests/test_context.rs` crash — heal's restore tier sat 70 tests
    behind the live tree, and the veto that held it there logged nothing."""
    from pipeline_core.domain.best_tree_mark import BestTreeMark
    events: list[tuple[str, dict]] = []
    marks: list[Any] = []
    # Sweep reports only t1 of the expected {t1, t2}: pass=1, one test
    # unmeasured. The stored mark is 0 — strictly dominated.
    _run_tdd(monkeypatch, tmp_path, [(_SHRUNK, False)], events,
             best_mark=BestTreeMark(pass_count=0, iteration=1,
                                    tree_sha="old", oracle_size=2),
             checkpoints=marks)
    assert len(marks) == 1, (
        "a verified floor above the stored count must replace the mark even "
        "while the oracle is short — pre-fix the veto froze the slot here")
    assert marks[0].pass_count == 1 and marks[0].missing_count == 1


def test_an_equal_count_over_a_short_oracle_still_may_not_churn_the_slot(
        tmp_path: Path, monkeypatch) -> None:
    """The half the veto keeps: an unmeasurable tree at an EQUAL count must
    not replace a full-oracle tree (snapshot_due's tree-moved branch would
    otherwise re-snapshot on every sweep of a chronic crasher). And the
    refusal must be SAID — the live freeze sat unnoticed from 08:56 UTC to
    the next session because the veto declined silently."""
    from pipeline_core.domain.best_tree_mark import BestTreeMark
    events: list[tuple[str, dict]] = []
    marks: list[Any] = []
    logs: list[str] = []
    _run_tdd(monkeypatch, tmp_path, [(_SHRUNK, False)], events,
             best_mark=BestTreeMark(pass_count=1, iteration=1,
                                    tree_sha="a-different-tree", oracle_size=2),
             checkpoints=marks, logs=logs)
    assert marks == []
    assert any("NOT replaced" in ln and "unmeasured" in ln for ln in logs), (
        "the withheld checkpoint must log both counts, not decline silently")


# --------------------------------------------------------------------------- #
# The pass high-water's LINEAGE. A retrigger's deliberate re-stub makes the
# high-water a fact about a tree the pipeline itself deleted; four consumers
# read it and the fourth deadlocks. See `domain/pass_highwater.py`.
# --------------------------------------------------------------------------- #
# A four-test oracle: a re-stub must be able to cost more than one sweep can
# win back, which the two-test outputs above cannot express.
def _sweep(passed: int, named: int = 4) -> str:
    """A cargo run where `named` of the four tests reported and `passed` of
    them passed. `named < 4` is a crashed binary — the oracle shrinks, which is
    the evidence the crash retrigger fires on."""
    lines = [f"test t{i} ... {'ok' if i <= passed else 'FAILED'}"
             for i in range(1, named + 1)]
    verdict = "ok" if passed == named else "FAILED"
    lines.append(f"test result: {verdict}. {passed} passed; "
                 f"{named - passed} failed; 0 ignored")
    return "\n".join(lines)


_FOUR_TESTS = frozenset({"t1", "t2", "t3", "t4"})


def _run_per_iter(monkeypatch, tmp_path: Path, sweeps, events, retrigger=None,
                  **kw) -> list[str]:
    """Drive the loop with one cargo output per ITERATION (the last repeats).

    `_run_tdd` scripts one entry per cargo CALL, which the ratchet's
    confirm-before-revert re-sweep makes unreadable the moment a pin is about
    which ITERATION measured what — a breach silently eats the next entry and
    every reading after it shifts. The iteration is taken from the loop's own
    `iter N: running cargo test` line, so a confirm re-sweep re-serves its
    iteration's reading instead of consuming the next one."""
    logs: list[str] = []
    cur = [0]

    def _cargo(*a: Any, **k: Any):
        for line in reversed(logs):
            if "running cargo test" in line and "iter " in line:
                cur[0] = int(line.split("iter ", 1)[1].split(":", 1)[0])
                break
        return 101, sweeps[min(max(cur[0], 1) - 1, len(sweeps) - 1)]

    from pipeline_core.application.reconstruct.phase_c_tdd import run_phase_c_tdd
    from pipeline_core.domain.models import BuildCluster, FnLocator
    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "1")
    monkeypatch.setenv("TDD_MAX_STALLS", kw.pop("max_stalls", "3"))
    # Guard off — see the note at the module's other driver helper.
    monkeypatch.setenv("TDD_FLAT_SCORE_REPEATS", "0")
    # The harness frontier is one fn, so the per-locator behavioural re-fill cap
    # would exhaust it two iterations in and every later iteration would skip
    # the fill phase — starving the re-admission gate of the "fills landed"
    # half of its rule for reasons that have nothing to do with these pins.
    monkeypatch.setenv("TDD_REFILL_MAX_ATTEMPTS", "99")
    (tmp_path / "src").mkdir(exist_ok=True)
    run_phase_c_tdd(
        _driver_deps(_cargo,
                     lambda pd, cl, stems, lib=False: (4, _FOUR_TESTS),
                     events, retrigger, logs=logs,
                     frontier={FnLocator("src/a.rs", "f"): {"t2"}},
                     fills_per_iter=2, **kw),
        project_dir=tmp_path, target_src_dir=tmp_path, tests_dir_files=[],
        cluster_tests=[("tests/t.rs", "fn t1() {}")], module="m",
        cluster=BuildCluster(
            name="stable", env_extra={}, tarpaulin_exclude_files=[],
            excluded_cfg=[], active_cfg=[], prompt_nightly_rule=""),
        check_cmd="cargo check", cluster_env={}, leaf_counts=None, fr_path=None,
        test_cmd_builder=lambda stems, include_lib=False: "cargo test",
        tag="[stable]")
    return logs


def _restub_retrigger(src: Path, *, on_call: int = 1, reverted: int = 3):
    """A retrigger that fires on its `on_call`-th consideration and does what
    the real one does — re-stub the fns referencing the re-authored component
    so they re-fill under the new shape."""
    from pipeline_core.application.retrigger_layout import RetriggerResult
    calls: list[int] = []

    def _retrig(*a: Any, **k: Any) -> RetriggerResult:
        calls.append(1)
        if len(calls) != on_call:
            return RetriggerResult(fired=False, reason="capped")
        (src / "a.rs").write_text("RESTUBBED", encoding="utf-8")
        return RetriggerResult(fired=True, succeeded=True,
                               reverted_count=reverted, cause="RUNTIME_CRASH")
    return _retrig


# 4/4, then a crashed binary, then the re-stub's collapse, then the re-climb.
# The retrigger fires on its SECOND consideration, so the free re-admission
# grant is spent first — which is the live setup: an unseeded baseline grants
# one release unconditionally, and the deadlock is what happens after it is
# gone.
_RESTUB_RUN = [_sweep(4), _sweep(3, named=3), _sweep(3), _sweep(3),
               _sweep(1), _sweep(2)]


def test_a_deliberate_restub_rebases_the_pass_high_water(
        tmp_path: Path, monkeypatch) -> None:
    """Iter 4's retrigger re-stubs 3 bodies on purpose and iter 5 therefore
    reads below the 4 the tree scored at iter 1. Left standing, that 4 is a
    fact about a tree this run no longer has.

    Live on `anyhow` cluster `stable`, run `259637-36db9aad` (2026-08-07): 56 of
    178 filled bodies re-stubbed, 81 -> 21, then 23 / 26 / 26 with 109 fills
    landing in one of those iterations."""
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    events: list[tuple[str, dict]] = []
    _run_per_iter(monkeypatch, tmp_path, _RESTUB_RUN, events,
                  retrigger=_restub_retrigger(src, on_call=2))
    rebased = [d for ev, d in events if ev == "phase_c.tdd.highwater_rebased"]
    assert len(rebased) == 1, "one re-base, on the first sweep after the re-stub"
    assert rebased[0]["stale_best"] == 4 and rebased[0]["new_best"] == 1
    assert rebased[0]["restubbed"] == 3


def test_the_re_climb_can_buy_a_re_admission_again(
        tmp_path: Path, monkeypatch) -> None:
    """The consumer the re-base exists for, and the only one that DEADLOCKS.

    `readmission_warranted` releases the disk-persisted `fully_exhausted`
    convictions only on a score move past the baseline. With the baseline left
    at the pre-re-stub high-water the release can never be bought — and on the
    live run the units holding the cluster's whole remaining test set were
    exactly the convicted ones, so passing 81 needed them and releasing them
    needed passing 81. Every sweep from iteration 4 to the run's death logged
    `NOT re-admitting exhausted fn(s)`.

    Here: the free grant is spent at iter 2 against the pre-re-stub 4, the
    re-stub drops the tree to 1, and the re-climb to 2 must buy a release. On
    the un-re-based figure that is `2 > 4` — false for the rest of the run."""
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    events: list[tuple[str, dict]] = []
    resets: list[str] = []
    logs = _run_per_iter(monkeypatch, tmp_path, _RESTUB_RUN, events,
                         retrigger=_restub_retrigger(src, on_call=2),
                         resets=resets)
    assert any("pass -1 -> 4" in r for r in resets), \
        f"the free grant was not spent before the re-stub: {resets}"
    assert any("pass 1 -> 2" in r for r in resets), \
        f"the re-climb never bought a re-admission: {resets}"
    # ...and no sweep after the re-base is still measured against the 4.
    _stale = [ln for ln in logs
              if "NOT re-admitting" in ln and "no further than 4" in ln
              and int(ln.split("iter ", 1)[1].split(":", 1)[0]) > 5]
    assert not _stale, f"still judging the re-climb against the deleted tree: {_stale}"


def test_a_flat_re_climb_still_buys_no_re_admission(
        tmp_path: Path, monkeypatch) -> None:
    """The rule the re-base must not soften. A release is bought with PAYOFF,
    not activity — the defect on `anyhow/nightly` was 51 fills moving the score
    by +0, re-admitting the fns that had already failed and taking the frontier
    41 -> 73 with iteration cost following to 224 min. A re-climb that lands
    fills and moves no test is that case, re-based or not."""
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    events: list[tuple[str, dict]] = []
    resets: list[str] = []
    _run_per_iter(monkeypatch, tmp_path, _RESTUB_RUN[:4], events,
                  retrigger=_restub_retrigger(src, on_call=2), resets=resets)
    assert [d for ev, d in events if ev == "phase_c.tdd.highwater_rebased"]
    _after = [r for r in resets if "pass -1 -> 4" not in r]
    assert not _after, f"a flat re-climb bought a re-admission: {resets}"


def test_a_fired_but_failed_attempt_rebases_nothing(
        tmp_path: Path, monkeypatch) -> None:
    """`restubbed == 0` re-stubbed nothing, so its sweep measures the same tree
    as the one before it. Re-basing there would hand the loop a free reset of a
    floor it never lost — the mirror of why `breach_withheld` excuses nothing
    for a fired-but-failed attempt."""
    from pipeline_core.application.retrigger_layout import RetriggerResult
    calls: list[int] = []

    def _failed(*a: Any, **k: Any) -> RetriggerResult:
        calls.append(1)
        if len(calls) != 1:
            return RetriggerResult(fired=False, reason="capped")
        return RetriggerResult(fired=True, succeeded=True, reverted_count=0,
                               cause="RUNTIME_CRASH")

    events: list[tuple[str, dict]] = []
    _run_per_iter(monkeypatch, tmp_path, _RESTUB_RUN, events, retrigger=_failed)
    assert not [d for ev, d in events if ev == "phase_c.tdd.highwater_rebased"]


def test_a_tree_that_cannot_build_is_not_a_new_baseline(
        tmp_path: Path, monkeypatch) -> None:
    """A compile-broken sweep reports 0 because nothing RAN. Adopting it as the
    lineage's baseline would make the next broken build read as a 0-to-0
    plateau on a cluster that is merely un-buildable — the same reason
    `improved` and the persisted floor both refuse such a sweep. The re-base
    waits for a reading instead."""
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    events: list[tuple[str, dict]] = []
    _run_per_iter(monkeypatch, tmp_path,
                  [*_RESTUB_RUN[:4],
                   "error: could not compile `anyhow` (lib test)", _sweep(2)],
                  events, retrigger=_restub_retrigger(src, on_call=2))
    rebased = [d for ev, d in events if ev == "phase_c.tdd.highwater_rebased"]
    assert rebased and rebased[0]["new_best"] == 2, \
        "the broken sweep measured nothing and must not become the baseline"


def test_the_plateau_exit_still_fires_on_a_flat_re_climb(
        tmp_path: Path, monkeypatch) -> None:
    """Termination is the thing a re-based floor could plausibly cost, so it is
    pinned. Once the re-climb flattens, `improved` goes false against the
    RE-BASED figure and the budget runs out on schedule — the loop must not
    become immortal because its floor moved down once."""
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    events: list[tuple[str, dict]] = []
    _run_per_iter(monkeypatch, tmp_path, _RESTUB_RUN[:4], events,
                  retrigger=_restub_retrigger(src, on_call=2), max_stalls="2")
    assert any(d.get("kind") == "stall_exit"
               for ev, d in events if ev == "phase_c.tdd.iter"), "no plateau exit"
    iters = [d for ev, d in events if ev == "phase_c.tdd.iter"]
    assert len(iters) < 12, f"the loop did not terminate: {len(iters)} iters"


def test_the_loop_offers_the_stub_set_to_the_conviction_release(
        tmp_path: Path, monkeypatch) -> None:
    """Wiring for the blocked-on release, which is where it can silently become
    a no-op: it must be handed the sweep's OWN stub set, keyed by file the way
    the retro store spells it."""
    from pipeline_core.domain.models import FnLocator
    events: list[tuple[str, dict]] = []
    retires: list[tuple[str, dict]] = []
    _run_tdd(monkeypatch, tmp_path, [(_INTACT, False)], events,
             retires=retires,
             stubs=[FnLocator("src/error.rs", "construct_from_std"),
                    FnLocator("src/error.rs", "provide"),
                    FnLocator("src/context.rs", "with_context")])
    assert retires, "the release was never offered the stub set"
    assert retires[0] == ("stable", {
        "src/error.rs": {"construct_from_std", "provide"},
        "src/context.rs": {"with_context"}}), \
        "the sweep's own stub set, scoped to the sweeping cluster"


# --- closure withhold: the flat region of a coupled component ----------------
# While a component (anyhow's Error/vtable web) is being closed, a KEPT
# crash-blocked fill can legitimately drop the sweep's pass count: the tests
# it costs vanish with a binary whose only panic is a still-hollow stub's
# `not implemented`. Charging that breach restores the pre-closure tree and
# erases the very fills the closure needs — the ratchet becomes the lottery
# reset. Withheld ONLY while the evidence holds: every lost test crash-
# vanished (none flipped red in a completed binary), the capture's panics
# are stub panics exclusively, AND the hollow set strictly shrank.


_HSHRINK = {"hollow_now": 13, "hollow_at_snapshot": 15}
_HFLAT = {"hollow_now": 15, "hollow_at_snapshot": 15}


def test_hollow_count_sums_stubbed_fns_across_files() -> None:
    from pipeline_core.application.reconstruct.tdd_ratchet import hollow_count
    texts = {
        "a.rs": "fn f() {\n    unimplemented!()\n}\nfn g() {\n    1\n}\n",
        "b.rs": "fn h() {\n    unimplemented!()\n}\n",
    }
    assert hollow_count(texts) == 2
    assert hollow_count(None) == 0
    assert hollow_count({}) == 0


def test_closure_withhold_forgives_stub_crash_vanished_breach() -> None:
    from pipeline_core.application.reconstruct.tdd_ratchet import closure_withheld
    reason = closure_withheld(
        "pass_count 11<13",
        lost=frozenset({"x", "y"}),
        judged_now=frozenset({"a", "b"}),
        stub_crash_sites=frozenset({("src/error.rs", 64)}),
        **_HSHRINK)
    assert reason and "src/error.rs:64" in reason


def test_closure_withhold_charges_when_hollow_did_not_shrink() -> None:
    # The live 83→23 of run 1160135-7b10cdfa: every lost test vanished with a
    # stub panic, but the hollow set was still 15. That is last iter's fills,
    # not a component closing. Pre-fix this returned a withhold string.
    from pipeline_core.application.reconstruct.tdd_ratchet import closure_withheld
    assert closure_withheld(
        "pass_count 23<83",
        lost=frozenset({"x", "y"}),
        judged_now=frozenset({"a", "b"}),
        stub_crash_sites=frozenset({("src/error.rs", 287),
                                    ("src/error.rs", 402)}),
        **_HFLAT) == ""


def test_closure_withhold_charges_when_a_lost_test_flipped_red() -> None:
    # A lost test that RECEIVED a verdict in a completed binary is a real
    # regression, not crash noise — the breach stands even while hollow
    # shrinks (the case that would otherwise withhold).
    from pipeline_core.application.reconstruct.tdd_ratchet import closure_withheld
    assert closure_withheld(
        "pass_count 12<13",
        lost=frozenset({"x"}),
        judged_now=frozenset({"x"}),
        stub_crash_sites=frozenset({("src/error.rs", 64)}),
        **_HSHRINK) == ""


def test_closure_withhold_charges_without_stub_evidence() -> None:
    from pipeline_core.application.reconstruct.tdd_ratchet import closure_withheld
    assert closure_withheld(
        "pass_count 11<13",
        lost=frozenset({"x", "y"}),
        judged_now=frozenset(),
        stub_crash_sites=frozenset(),
        **_HSHRINK) == ""


def test_closure_withhold_needs_a_breach_and_a_loss() -> None:
    from pipeline_core.application.reconstruct.tdd_ratchet import closure_withheld
    assert closure_withheld(
        "", lost=frozenset({"x"}), judged_now=frozenset(),
        stub_crash_sites=frozenset({("src/error.rs", 64)}),
        **_HSHRINK) == ""
    assert closure_withheld(
        "pass_count 11<13", lost=frozenset(), judged_now=frozenset(),
        stub_crash_sites=frozenset({("src/error.rs", 64)}),
        **_HSHRINK) == ""


_STUB_BODY = "fn construct_from_boxed() {\n    unimplemented!()\n}\n"
_STUB_MUTATED = _STUB_BODY + "// rewritten accessor\n"
_STUB_CRASH_LOST = (
    "test t1 ... \n"
    "thread 't1' panicked at src/a.rs:2:5:\n"
    "not implemented\n"
    "test result: FAILED. 0 passed; 0 failed; 0 ignored")


def test_stub_crash_vanished_breach_reverts_when_hollow_stayed_flat(
        tmp_path: Path, monkeypatch) -> None:
    """The plan's fixture at loop grain: a reproduced pass-count drop whose
    only panics are stub panics, with the hollow set unchanged, must CHARGE
    and restore the snapshot. Pre-fix this withheld and left the dropped
    tree on disk (run 1160135-7b10cdfa, 83→23, hollow 15→15)."""
    events: list[tuple[str, dict]] = []
    _run_tdd(monkeypatch, tmp_path,
             [(_INTACT, False), (_STUB_CRASH_LOST, True),
              (_STUB_CRASH_LOST, False)], events,
             src_text=_STUB_BODY, mutate_text=_STUB_MUTATED)
    assert (tmp_path / "src" / "a.rs").read_text(encoding="utf-8") == _STUB_BODY
    assert any(ev == "phase_c.tdd.batch_revert" for ev, _ in events)
    assert not any(ev == "phase_c.tdd.batch_withheld" for ev, _ in events)