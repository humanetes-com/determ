"""The fill's ground: a baseline is read only on the tree the last verdict left.

Run `4012759-5c5177c6` (2026-10-21/22, cluster `stable`, fill 9): the joint
ask spliced a reverted candidate onto the shared tree before the fill and
judged it only after, so every baseline of the fill read 337 against the
sweep's 343, a pair body breaking the same three tests was kept blind, and
the next sweep's batch ratchet reverted four files. These tests pin the
contract that makes that state unreadable as a premise: while a fill's
ground is bound, taking the tree's splice lock restores bytes no verdict
left, releasing it accepts the holder's, and every measurement names its
role. The negative control replays the pre-fill splice with no ground bound
and shows the premise reading the candidate's bytes.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline_core.application.reconstruct.fill_ground_lock import (
    check_measurement,
    fill_ground,
    ground_lock_for,
    read_tree,
)
from pipeline_core.application.reconstruct.regression_guard import baseline
from pipeline_core.domain.fill_ground import (
    FillGround,
    GroundRoleError,
    MeasureRole,
)


def _tree(root: Path) -> Path:
    src = root / "src"
    src.mkdir(parents=True)
    (src / "lib.rs").write_text("LIB\n")
    (src / "fmt.rs").write_text("FMT_SWEEP\n")
    return root


def _sink() -> tuple[list[str], list[tuple[str, dict]], object, object]:
    logs: list[str] = []
    events: list[tuple[str, dict]] = []

    def emit(name: str, data: dict | None = None, **_k: object) -> None:
        events.append((name, dict(data or {})))
    return logs, events, logs.append, emit


def test_the_domain_names_a_write_outside_accept_and_passes_a_byte_exact_revert() -> None:
    g = FillGround.open({"src/a.rs": b"A", "src/b.rs": b"B"})
    brk = g.diff({"src/a.rs": b"A2", "src/b.rs": b"B"})
    assert brk is not None and brk.files == ("src/a.rs",)
    assert g.diff({"src/a.rs": b"A", "src/b.rs": b"B"}) is None, "a revert is the ground"
    g.accept({"src/a.rs": b"A2"})
    assert g.diff({"src/a.rs": b"A2", "src/b.rs": b"B"}) is None
    assert g.diff({"src/a.rs": b"A2", "src/b.rs": b"B", "src/c.rs": b"C"}).files == (
        "src/c.rs",), "a new file is a difference"


def test_a_write_outside_every_hold_is_restored_before_the_next_holder_reads(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    logs, events, log, emit = _sink()
    with fill_ground(root, log=log, emit=emit, cluster="stable", iteration=9) as g:
        # The pre-fill joint splice of fill 9: written by no route, judged
        # by no gate.
        (root / "src" / "fmt.rs").write_text("CANDIDATE\n")
        lock = ground_lock_for(root)
        with lock:
            assert (root / "src" / "fmt.rs").read_text() == "FMT_SWEEP\n"
            check_measurement(root, MeasureRole.PREMISE)
        assert g.breaks == 1 and g.broken_files == ["src/fmt.rs"]
    assert any("HEALTH ‼ UNJUDGED_WRITE" in m for m in logs)
    assert [n for n, _ in events] == ["phase_c.fill.unjudged_write"]
    assert events[0][1]["files"] == ["src/fmt.rs"]


def test_a_holders_keep_is_accepted_at_release_and_survives_the_fill_return(tmp_path: Path) -> None:
    """A keep, a salvage, an annotated stub: whatever the holder left is its
    verdict. No keep site calls anything for it."""
    root = _tree(tmp_path)
    logs, events, log, emit = _sink()
    with fill_ground(root, log=log, emit=emit) as g:
        lock = ground_lock_for(root)
        with lock:
            check_measurement(root, MeasureRole.PREMISE)
            (root / "src" / "fmt.rs").write_text("KEPT\n")
            check_measurement(root, MeasureRole.JUDGMENT)
        lock.acquire()
        (root / "src" / "lib.rs").write_text("SALVAGED\n")
        lock.release()
        with lock:
            check_measurement(root, MeasureRole.PREMISE)
        assert g.breaks == 0 and g.premises == 2 and g.judgments == 1
    assert (root / "src" / "fmt.rs").read_text() == "KEPT\n"
    assert (root / "src" / "lib.rs").read_text() == "SALVAGED\n"
    assert not events


def test_a_hold_ended_by_an_exception_accepts_nothing(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    logs, events, log, emit = _sink()
    with fill_ground(root, log=log, emit=emit) as g:
        lock = ground_lock_for(root)
        with pytest.raises(RuntimeError), lock:
            (root / "src" / "fmt.rs").write_text("HALF A WRITE\n")
            raise RuntimeError("route crashed mid-write")
    assert (root / "src" / "fmt.rs").read_text() == "FMT_SWEEP\n", \
        "the fill-return premise restored the interrupted write"
    assert g.breaks == 1


def test_a_hold_ended_by_a_stop_keeps_the_judged_state_its_stop_path_left(tmp_path: Path) -> None:
    """A graceful stop propagates after the route put the tree back to its
    best judged state (the session's stop path); that state is a verdict."""
    root = _tree(tmp_path)
    logs, events, log, emit = _sink()
    with (pytest.raises(SystemExit),
          fill_ground(root, log=log, emit=emit) as g,
          ground_lock_for(root)):
        (root / "src" / "fmt.rs").write_text("BEST JUDGED\n")
        raise SystemExit(0)
    assert (root / "src" / "fmt.rs").read_text() == "BEST JUDGED\n"
    assert g.breaks == 0


def test_every_measurement_names_its_role_and_holds_the_lock(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    logs, events, log, emit = _sink()
    with pytest.raises(TypeError):
        baseline(lambda *a, **k: (0, ""), 1, True, "cargo test", root, {})  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        check_measurement(root, "premise")  # type: ignore[arg-type]
    with fill_ground(root, log=log, emit=emit):
        with pytest.raises(GroundRoleError):
            check_measurement(root, MeasureRole.PREMISE)
        with ground_lock_for(root):
            (root / "src" / "lib.rs").write_text("OWN WRITE\n")
            check_measurement(root, MeasureRole.PREMISE)
    assert any("PREMISE_AFTER_WRITE" in m for m in logs)
    assert [n for n, _ in events] == ["phase_c.fill.premise_after_write"]


def test_outside_a_fill_the_lock_is_a_plain_lock(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    (root / "src" / "fmt.rs").write_text("TASK3 WRITE\n")
    with ground_lock_for(root):
        check_measurement(root, MeasureRole.PREMISE)
    assert (root / "src" / "fmt.rs").read_text() == "TASK3 WRITE\n"


def test_negative_control_without_a_ground_the_premise_reads_the_candidate(tmp_path: Path) -> None:
    """The pre-fix shape: the joint candidate is spliced before the fill and
    nothing is bound, so the first route's premise is read on the
    candidate's bytes — fill 9's 337 against the sweep's 343."""
    root = _tree(tmp_path)
    sweep = read_tree(root)
    (root / "src" / "fmt.rs").write_text("CANDIDATE\n")
    with ground_lock_for(root):
        check_measurement(root, MeasureRole.PREMISE)
        premise_tree = read_tree(root)
    assert premise_tree != sweep
    assert FillGround.open(sweep).diff(premise_tree).files == ("src/fmt.rs",)


def test_the_ground_names_its_files_and_its_identity() -> None:
    from pipeline_core.domain.fill_ground import UnjudgedWrite, tree_digest
    g = FillGround.open({"src/a.rs": b"A", "src/gone.rs": None})
    assert g.knows("src/a.rs") and not g.knows("src/b.rs")
    assert g.identity == tree_digest({"src/gone.rs": None, "src/a.rs": b"A"})
    brk = g.diff({"src/a.rs": b"B"})
    assert brk is not None and brk.payload()["files"] == ["src/a.rs"]
    err = UnjudgedWrite(("src/a.rs",), "restore failed")
    assert err.files == ("src/a.rs",) and "src/a.rs" in str(err)


def test_a_revert_that_did_not_land_is_restored_at_release_not_accepted(
        tmp_path: Path) -> None:
    """The per-fn failure path released the lock after `_revert_own_body`
    logged `REVERT FAILED`, and the release stored the bytes the revert was
    meant to remove as the ground (review of the 2026-10-22 implementation).
    The one revert writer reports the failure; the release restores that
    file from the accepted bytes and counts the break. NEGATIVE CONTROL: the
    same hold with no report accepts the failed attempt's body."""
    from pipeline_core.adapters.fill_engine_roles import MutatorAdapter
    from pipeline_core.domain.reconstruction import SpliceOutcome

    def refuse(*_a: object, **_k: object) -> SpliceOutcome:
        return SpliceOutcome(False, "not_found", 0)

    mut = MutatorAdapter(
        locate_fn_by_record_fn=lambda *_a: None, locate_fn_fn=lambda *_a: None,
        splice_fn_body_at_fn=refuse, splice_fn_body_fn=refuse,
        replace_fn_signature_fn=lambda *_a: False,
        extract_signature_block_fn=lambda _r: None,
        lock_for_fn=ground_lock_for)
    root = _tree(tmp_path)
    logs, events, log, emit = _sink()
    fmt = root / "src" / "fmt.rs"
    with fill_ground(root, log=log, emit=emit, cluster="stable", iteration=1) as g:
        lock = ground_lock_for(root)
        with lock:
            fmt.write_text("FAILED ATTEMPT\n")
            out = mut.revert_body_scoped(fmt, "debug", "", "fn debug()", "STUB")
            assert not out.ok
        assert fmt.read_text() == "FMT_SWEEP\n"
        assert g.failed_reverts == 1 and g.breaks == 1
        assert g.ground.stored("src/fmt.rs") == b"FMT_SWEEP\n"
        # NEGATIVE CONTROL: no report, and the release stores the leftover.
        with lock:
            fmt.write_text("FAILED ATTEMPT\n")
        assert g.ground.stored("src/fmt.rs") == b"FAILED ATTEMPT\n"
    assert any("did not land" in m for m in logs)


def test_an_attempt_hold_left_by_an_exception_is_released_without_a_verdict(
        tmp_path: Path) -> None:
    """The per-fn attempt holds the lock by take/drop across a dozen exits;
    an exception between them left the non-reentrant lock held, and with a
    ground bound the fill's exit premise waited on it. Abandoned, the hold
    stores nothing and the next taker restores the attempt's write."""
    from pipeline_core.application.reconstruct.fill_ground_lock import AttemptHold
    root = _tree(tmp_path)
    _logs, _events, log, emit = _sink()
    with fill_ground(root, log=log, emit=emit) as g:
        hold = AttemptHold(ground_lock_for(root))
        with pytest.raises(RuntimeError):
            try:
                hold.take()
                (root / "src" / "fmt.rs").write_text("HALF WRITTEN\n")
                raise RuntimeError("model call failed")
            finally:
                hold.abandon()
        assert not hold.held
        lock = ground_lock_for(root)
        assert lock.acquire(blocking=False), "the hold was released"
        lock.release()
        assert (root / "src" / "fmt.rs").read_text() == "FMT_SWEEP\n"
        assert g.ground.stored("src/fmt.rs") == b"FMT_SWEEP\n"


def test_a_failed_restore_at_the_fill_return_is_reported_not_raised(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Raising from the fill-return premise turned a finished fill into
    `cluster_failed`. It is reported as an unrestorable write instead."""
    import pipeline_core.application.reconstruct.fill_ground_lock as fgl
    root = _tree(tmp_path)
    logs, events, log, emit = _sink()
    with fill_ground(root, log=log, emit=emit, cluster="stable", iteration=3):
        (root / "src" / "fmt.rs").write_text("AFTER THE LAST HOLD\n")

        def broken(_pre: object) -> None:
            raise OSError("read-only file system")
        monkeypatch.setattr(fgl, "restore", broken)
    names = [n for n, _ in events]
    assert "phase_c.fill.unjudged_stop" in names
    assert any("after the fill" in m for m in logs)
    assert ground_lock_for(root).session is None


def test_an_unreadable_file_is_neither_restored_nor_accepted(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _tree(tmp_path)
    _logs, events, log, emit = _sink()
    real = Path.read_bytes

    def flaky(self: Path) -> bytes:
        if self.name == "fmt.rs":
            raise OSError("transient")
        return real(self)
    with fill_ground(root, log=log, emit=emit) as g:
        (root / "src" / "fmt.rs").write_text("KEPT ELSEWHERE\n")
        monkeypatch.setattr(Path, "read_bytes", flaky)
        with ground_lock_for(root):
            pass
        monkeypatch.setattr(Path, "read_bytes", real)
        assert (root / "src" / "fmt.rs").read_text() == "KEPT ELSEWHERE\n"
        assert g.ground.stored("src/fmt.rs") == b"FMT_SWEEP\n"
        assert g.breaks == 0


def test_a_regression_gate_outside_the_hold_is_refused_while_a_ground_is_bound(
        tmp_path: Path) -> None:
    from pipeline_core.application.reconstruct.regression_guard import gate
    from pipeline_core.domain.test_results import TestStateReading
    root = _tree(tmp_path)
    _logs, _events, log, emit = _sink()

    def no_cargo(*_a: object, **_k: object) -> tuple[int, str]:
        raise AssertionError("must not measure")
    with fill_ground(root, log=log, emit=emit), pytest.raises(GroundRoleError):
        gate(no_cargo, lambda *_a: None, 60, True, "cargo test", root, {},
             TestStateReading(()))
