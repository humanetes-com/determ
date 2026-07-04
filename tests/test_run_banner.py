"""Startup banner format pin (adapters/run_banner). The `PIPELINE STARTED`
line is load-bearing: the watcher and the which-code-is-running check grep it
and compare its md5 to the on-disk pipeline.py."""
from __future__ import annotations

import hashlib
from pathlib import Path

from pipeline_core.adapters.run_banner import banner_lines


def test_banner_layout_and_md5(tmp_path: Path) -> None:
    f = tmp_path / "pipeline.py"
    f.write_text("print('x')\n")
    lines = banner_lines(f, "42-abc", ["pipeline.py", "--clean"], 4)
    assert len(lines) == 7
    bar = "=" * 60
    assert lines[0] == f"\n{bar}" and lines[1] == bar
    assert lines[5] == bar and lines[6] == bar
    md5_8 = hashlib.md5(f.read_bytes()).hexdigest()[:8]
    started = lines[2]
    assert started.startswith("  PIPELINE STARTED — ")
    assert f"pipeline.py={md5_8}" in started
    assert started.endswith("run_id=42-abc")
    assert " UTC  " in started
    assert lines[3].endswith("pipeline.py --clean")
    assert lines[4] == "  PIPELINE_PARALLELISM=4"


def test_banner_survives_missing_git_repo(tmp_path: Path) -> None:
    f = tmp_path / "pipeline.py"
    f.write_text("x = 1\n")
    line = banner_lines(f, "r", [], 1)[2]
    # tmp_path has no .git → the sha may resolve from a parent repo or be
    # absent; either way the md5 stamp and run_id framing must hold.
    assert "pipeline.py=" in line and line.endswith("run_id=r")
