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


def _driver_deps(run_cargo, inventory, events, retrigger=None, *,
                 frontier=None, fills_per_iter=0, graceful_stop=None, fill=None):
    from pipeline_core.application.reconstruct.phase_c_tdd import PhaseCTddDeps
    from pipeline_core.domain.models import ClusterFillResult
    return PhaseCTddDeps(
        attribute_failures=lambda *a, **k: AttributionResult(
            dict(frontier or {}), FailureKind.NO_FRONTIER,
            dict(frontier or {})),
        build_test_reverse_map=lambda *a, **k: {},
        filter_test_targets=lambda frontier, src: frontier,
        check_graceful_stop=graceful_stop or (lambda *a, **k: None),
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
        task2_fill_fns_one_by_one=fill or (lambda **kw: ClusterFillResult(
            attempts=1, fns_filled=fills_per_iter)),
        checkpoint_fill_tree=lambda pd, cl, d, p, i: None,
        active_test_inventory=inventory,
        retrigger_layout=retrigger,
    )


def _run_tdd(monkeypatch, tmp_path: Path, outputs, events, retrigger=None, *,
             frontier=None, fills_per_iter=0, max_stalls="2",
             graceful_stop=None, fill=None) -> None:
    """Drive the TDD loop with ratchet ON over a scripted cargo-output list.
    Each cargo call pops the next output (last one repeats); the call ALSO
    mutates src/a.rs when the popped entry says so — simulating a fill that
    landed between the snapshot and a later sweep."""
    from pipeline_core.application.reconstruct.phase_c_tdd import run_phase_c_tdd
    from pipeline_core.domain.models import BuildCluster
    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "1")
    monkeypatch.setenv("TDD_MAX_STALLS", max_stalls)
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "a.rs").write_text("orig", encoding="utf-8")
    seq = list(outputs)

    def _cargo(*a: Any, **k: Any):
        entry = seq.pop(0) if len(seq) > 1 else seq[0]
        out, mutate, rc = entry if len(entry) == 3 else (*entry, 101)
        if mutate:
            (src / "a.rs").write_text("FILLED", encoding="utf-8")
        return rc, out

    run_phase_c_tdd(
        _driver_deps(_cargo, lambda pd, cl, stems, lib=False: (2, frozenset({"t1", "t2"})),
                     events, retrigger, frontier=frontier,
                     fills_per_iter=fills_per_iter,
                     graceful_stop=graceful_stop, fill=fill),
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
