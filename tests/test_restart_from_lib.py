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


def test_task0c_clears_the_augmentation_and_everything_after():
    assert _keys("task0c") == (0, "task0c task0d bake strip task1 task2a task2b task2c task2-4")


def test_short_label_t0c_is_the_same_stage():
    assert _keys("t0c") == _keys("task0c")


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
