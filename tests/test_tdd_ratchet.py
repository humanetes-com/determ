"""Pins for the TDD-iter batch ratchet helpers and the
confirm-before-revert rule (a crash-killed binary drops a NONDETERMINISTIC
subset of tests, so a single breached reading is noise until reproduced —
observed live: 22 vs 23 vanished on back-to-back sweeps of an unchanged tree,
and the ratchet wiped every sibling-blocked fill each iter, locking the
plateau)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.tdd_ratchet import (
    ratchet_breach,
    restore_src_tree,
    snapshot_due,
    snapshot_src_tree,
)
from pipeline_core.domain.attribution import AttributionResult, FailureKind


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


def _driver_deps(run_cargo, inventory, events):
    from pipeline_core.application.reconstruct.phase_c_tdd import PhaseCTddDeps
    from pipeline_core.domain.models import ClusterFillResult
    return PhaseCTddDeps(
        attribute_failures=lambda *a, **k: AttributionResult(
            {}, FailureKind.NO_FRONTIER, {}),
        build_test_reverse_map=lambda *a, **k: {},
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
        checkpoint_fill_tree=lambda pd, cl, d, p, i: None,
        active_test_inventory=inventory,
        retrigger_layout=None,
    )


def _run_tdd(monkeypatch, tmp_path: Path, outputs, events) -> None:
    """Drive the TDD loop with ratchet ON over a scripted cargo-output list.
    Each cargo call pops the next output (last one repeats); the call ALSO
    mutates src/a.rs when the popped entry says so — simulating a fill that
    landed between the snapshot and a later sweep."""
    from pipeline_core.application.reconstruct.phase_c_tdd import run_phase_c_tdd
    from pipeline_core.domain.models import BuildCluster
    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "1")
    monkeypatch.setenv("TDD_MAX_STALLS", "2")
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "a.rs").write_text("orig", encoding="utf-8")
    seq = list(outputs)

    def _cargo(*a: Any, **k: Any):
        out, mutate = seq.pop(0) if len(seq) > 1 else seq[0]
        if mutate:
            (src / "a.rs").write_text("FILLED", encoding="utf-8")
        return 101, out

    run_phase_c_tdd(
        _driver_deps(_cargo, lambda pd, cl, stems, lib=False: (2, frozenset({"t1", "t2"})),
                     events),
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
