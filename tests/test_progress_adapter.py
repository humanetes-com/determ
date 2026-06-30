"""C2: ChunkProgress + render_progress relocated from pipeline.py."""
from __future__ import annotations

import pipeline_core.adapters.progress as prog
from pipeline_core.adapters.progress import ChunkProgress, render_progress
from pipeline_core.adapters.progress import _fmt_duration as fmt


def test_fmt_duration_units() -> None:
    assert fmt(5) == "5s"
    assert fmt(125) == "2m05s"
    assert fmt(3725) == "1h02m"


def test_render_progress_bar_and_eta() -> None:
    s = render_progress(45, 276, 124.0)
    assert "45/276" in s and "(16%)" in s and "ETA" in s


def test_render_progress_no_avg_omits_eta() -> None:
    s = render_progress(0, 0, None)
    assert "ETA" not in s and "0/0" in s


def test_chunk_progress_log_mode_emits_lines(monkeypatch) -> None:
    lines: list[str] = []
    monkeypatch.setattr(prog, "log", lambda m: lines.append(m))
    cp = ChunkProgress(2, progress_mode=False)
    cp.start_chunk()
    cp.finish_chunk(1)
    cp.finish_chunk(2)
    cp.finish()  # no-op outside progress mode
    assert len(lines) == 2 and "1/2" in lines[0]


def test_chunk_progress_progress_mode_uses_carriage_return(capsys) -> None:
    cp = ChunkProgress(2, progress_mode=True)
    cp.start_chunk()
    cp.finish_chunk(1)
    cp.finish()
    out = capsys.readouterr().out
    assert "\r" in out
