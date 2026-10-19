"""Re-opening a phase re-opens its parts.

The module loop's re-entry marked `task2a/<module>` pending and left
`task2a/<module>/<cluster>` done, so the next iteration skipped every cluster
as "checkpoint valid" while logging that it re-entered them (run
`240506-87538963`, 2026-10-19, log lines 1729-1740). The fixture is the
workspace's own state file after that run.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.adapters.state import mark_phase, read_state, reopen_phase

STATE = """# pipeline_state — anyhow
# 2026-10-19 22:40:47

[task0c/stable]  done          hash=903c2262d5d54556  1 modules
[task0c/stable/anyhow]  done          hash=d95bb255d15ba1be  96.8%
[task1/anyhow]  done          hash=903c2262d5d54556  10761 chars
[task2a/anyhow/stable]  done          hash=-  validated
[task2a/anyhow/nightly]  done          hash=-  validated
[task2a/anyhow]  done          hash=-  2 cluster(s)
[task2a/anyhowx]  done          hash=-  another module
[task2b/anyhow]  done          hash=-  12 file(s)

current: t3 / Test run
"""


def _statuses(tmp_path: Path) -> dict[str, str]:
    return {k: v["status"] for k, v in read_state(tmp_path)["phases"].items()}


def test_reopening_a_phase_reopens_every_part_and_nothing_else(tmp_path: Path):
    (tmp_path / "pipeline_state.md").write_text(STATE)
    reset = reopen_phase(tmp_path, "task2a/anyhow", detail="auto-reset")
    st = _statuses(tmp_path)
    assert sorted(reset) == ["task2a/anyhow", "task2a/anyhow/nightly",
                             "task2a/anyhow/stable"]
    assert st["task2a/anyhow/stable"] == st["task2a/anyhow/nightly"] == "pending"
    assert st["task2a/anyhow"] == "pending"
    # A key that only shares the prefix is another phase.
    assert st["task2a/anyhowx"] == "done"
    assert st["task1/anyhow"] == "done" and st["task2b/anyhow"] == "done"
    assert read_state(tmp_path)["current"] == "t3 / Test run"


def test_the_old_reset_left_the_parts_done(tmp_path: Path):
    """Negative control: what the re-entry did before."""
    (tmp_path / "pipeline_state.md").write_text(STATE)
    mark_phase(tmp_path, "task2a/anyhow", "pending", detail="auto-reset")
    st = _statuses(tmp_path)
    assert st["task2a/anyhow"] == "pending"
    assert st["task2a/anyhow/stable"] == st["task2a/anyhow/nightly"] == "done"


def test_a_phase_with_no_entry_is_written_pending(tmp_path: Path):
    (tmp_path / "pipeline_state.md").write_text(STATE)
    assert reopen_phase(tmp_path, "task2c/anyhow") == ["task2c/anyhow"]
    assert _statuses(tmp_path)["task2c/anyhow"] == "pending"
