"""`lib/restart_from_lib.sh` — the stage → phase-key mapping shared by
restart_from.sh, run_hardcrate.sh and run_microcrate.sh, exercised through a
real bash. `task0c` is the newest stage: it clears the augmentation's own
keys (`[task0c/<cluster>]`, `[task0c/<cluster>/<module>]`) plus everything
after, and the launcher pairs it with --restart-from-pre-augment, which is
what actually brings the original source back."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parents[1] / "lib" / "restart_from_lib.sh"


def _keys(stage: str) -> tuple[int, str]:
    r = subprocess.run(
        ["bash", "-c", f'source "{_LIB}"; restart_from_keys "$1"', "_", stage],
        capture_output=True, text=True,
    )
    return r.returncode, r.stdout.strip()


def test_task0c_clears_the_augmentation_and_everything_after_but_not_the_harvest():
    """task0d's keys are not cleared: the restore lands on the behaviour
    restore point and keeps them (restart_from.sh task0d is the re-harvest)."""
    assert _keys("task0c") == (0, "task0c bake strip task1 task2a task2b task2c task2-4")


def test_task0d_clears_the_harvest_and_everything_task0c_clears():
    rc, keys = _keys("task0d")
    assert rc == 0 and keys.split() == ["task0d", *_keys("task0c")[1].split()]


def test_short_labels_are_the_same_stages():
    assert _keys("t0c") == _keys("task0c")
    assert _keys("t0d") == _keys("task0d")


def test_task0b_and_earlier_are_still_refused():
    assert _keys("task0b")[0] == 2
    assert _keys("task0a")[0] == 2


@pytest.mark.parametrize("stage", ["task1", "task2", "task2c", "task4"])
def test_later_stages_never_clear_task0c(stage):
    rc, keys = _keys(stage)
    assert rc == 0 and "task0c" not in keys.split()


_LIVE_STATE = """# pipeline_state — anyhow
# 2026-08-26 15:41:14

[cfg_scan]  done          hash=a4b46572a4794e73  2 clusters: stable, nightly
[test_cmd]  done          hash=b6fa9024e0dacaf3  cargo test
[task0a-context]  done          hash=-  3052 chars
[task0a]  done          hash=-  1466 chars
[task0b]  done          hash=-  3268 chars
[task0c/stable]  done          hash=a4b46572a4794e73  1 modules
[task0c/stable/anyhow]  done          hash=b6fa9024e0dacaf3  97.8%
[task0c/nightly]  done          hash=a4b46572a4794e73  1 modules
[task0c/nightly/anyhow]  done          hash=b6fa9024e0dacaf3  97.8%
[strip/anyhow]  done          hash=5094a2ef48b980d1  
[task1/anyhow]  done          hash=a4b46572a4794e73  27862 chars

current: task2a/anyhow / cluster stable (1/2)
"""


def test_restart_from_clear_task0c_on_the_live_state_shape(tmp_path):
    """`restart_from_clear` with the task0c keys, on the live tmp/anyhow
    state file of 2026-08-26 key for key: every task0c/… line goes (both the
    per-cluster and the per-cluster-per-module forms), strip and task1 go,
    the pre-task0c entries stay, `current:` is blanked."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "pipeline_state.md").write_text(_LIVE_STATE, encoding="utf-8")
    r = subprocess.run(
        ["bash", "-c", f'source "{_LIB}"; restart_from_clear "$1" $(restart_from_keys task0c)',
         "_", str(ws)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    kept = [ln for ln in (ws / "pipeline_state.md").read_text(encoding="utf-8").splitlines()
            if ln.startswith("[")]
    assert [ln.split("]")[0] + "]" for ln in kept] == [
        "[cfg_scan]", "[test_cmd]", "[task0a-context]", "[task0a]", "[task0b]"]
    assert "current: \n" in (ws / "pipeline_state.md").read_text(encoding="utf-8") + "\n"
    assert "removed 6 phase line(s)" in r.stdout


@pytest.mark.parametrize("stage", ["task2", "task2a", "t2", "t2a"])
def test_per_cluster_fill_stages_drop_their_products(stage):
    r = subprocess.run(
        ["bash", "-c", f'source "{_LIB}"; restart_from_drops_t2a "$1"', "_", stage],
    )
    assert r.returncode == 0


@pytest.mark.parametrize(
    "stage", ["task2b", "task2c", "t2b", "t2c", "task3", "task4", "task1", "task0c", "t0c"])
def test_later_and_earlier_stages_do_not_drop_the_per_cluster_fill(stage):
    r = subprocess.run(
        ["bash", "-c", f'source "{_LIB}"; restart_from_drops_t2a "$1"', "_", stage],
    )
    assert r.returncode == 1


def test_dropping_t2a_products_leaves_the_layout_and_the_augmentation(tmp_path):
    """The removal is the task0c analogue for a phase with no restore commit:
    the filled tree, heal's snapshot, and the records that would resume the
    deleted tree's dispatches go; the layout and the functional requirements
    stay. restart_from_clear does not do this — hardcrate --from calls only
    that, and a resume must keep the fills."""
    ws = tmp_path / "ws"
    (ws / ".skeleton-filled" / "stable").mkdir(parents=True)
    (ws / ".skeleton-filled" / "stable" / "body.rs").write_text("fn filled() {}\n", encoding="utf-8")
    (ws / ".best_tree" / "stable").mkdir(parents=True)
    (ws / ".best_tree" / "stable" / "body.rs").write_text("fn best() {}\n", encoding="utf-8")
    (ws / ".skeleton").mkdir()
    (ws / ".skeleton" / "layout.rs").write_text("fn layout() {}\n", encoding="utf-8")
    (ws / ".skeleton" / "_tdd_floor_stable.json").write_text("{}\n", encoding="utf-8")
    (ws / ".phase_c_logs").mkdir()
    (ws / ".phase_c_logs" / "retrospectives.json").write_text("{}\n", encoding="utf-8")
    (ws / ".phase_c_logs" / "ask_ledger_stable.json").write_text("{}\n", encoding="utf-8")
    (ws / ".phase_c_logs" / "red_ledger_stable.json").write_text("{}\n", encoding="utf-8")
    (ws / "functional_requirements_demo.md").write_text("fr\n", encoding="utf-8")

    r = subprocess.run(
        ["bash", "-c", f'source "{_LIB}"; restart_from_drop_t2a_products "$1"',
         "_", str(ws)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    assert not (ws / ".skeleton-filled").exists()
    assert not (ws / ".best_tree").exists()
    assert not (ws / ".skeleton" / "_tdd_floor_stable.json").exists()
    assert not (ws / ".phase_c_logs" / "retrospectives.json").exists()
    assert not (ws / ".phase_c_logs" / "ask_ledger_stable.json").exists()
    assert not (ws / ".phase_c_logs" / "red_ledger_stable.json").exists()
    assert (ws / ".skeleton" / "layout.rs").read_text(encoding="utf-8") == "fn layout() {}\n"
    assert (ws / "functional_requirements_demo.md").read_text(encoding="utf-8") == "fr\n"

    (ws / ".skeleton-filled").mkdir()
    (ws / ".skeleton-filled" / "kept.rs").write_text("fn kept() {}\n", encoding="utf-8")
    cleared = subprocess.run(
        ["bash", "-c",
         f'source "{_LIB}"; restart_from_clear "$1" $(restart_from_keys task2a)',
         "_", str(ws)],
        capture_output=True, text=True,
    )
    assert cleared.returncode == 0, cleared.stderr
    assert (ws / ".skeleton-filled" / "kept.rs").read_text(encoding="utf-8") == "fn kept() {}\n"


def test_anyhow_launcher_drops_t2a_and_hardcrate_resume_does_not():
    """The wipe is gated on the anyhow launcher's task2/task2a path.
    run_hardcrate.sh --from calls restart_from_clear only, so a hardcrate
    resume cannot inherit the deletion."""
    root = Path(__file__).resolve().parents[1]
    restart = (root / "restart_from.sh").read_text(encoding="utf-8")
    hard = (root / "run_hardcrate.sh").read_text(encoding="utf-8")
    assert 'restart_from_drops_t2a "$_STAGE" && [[ "$_KEEP_FILLS" != true ]]' in restart
    assert "restart_from_drop_t2a_products" in restart
    assert "restart_from_drop_t2a_products" not in hard
    assert "restart_from_clear" in hard


def test_restart_from_and_redo_pass_no_tail_to_continue():
    """A Cursor terminal abort SIGTERMs a tailed continue.sh process group
    (2026-08-29, run 929966-abe13d11, exit 143). Both launchers that exec
    continue.sh must pass --no-tail so that class cannot recur from the
    script."""
    root = Path(__file__).resolve().parents[1]
    restart = (root / "restart_from.sh").read_text(encoding="utf-8")
    redo = (root / "redo.sh").read_text(encoding="utf-8")
    assert "_LAUNCH_ARGS=(--y --no-tail)" in restart
    assert "continue.sh --y --keep-augmented --no-tail" in redo
    assert "exec \"$SCRIPT_DIR/continue.sh\" --y --keep-augmented --no-tail" in redo
