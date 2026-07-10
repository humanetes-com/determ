"""Pins for the monitor's [l]egend view content (pipeline_core/monitor/legend.py)."""
from __future__ import annotations

from pipeline_core.monitor.legend import PHASES, legend_lines


def test_every_pipeline_phase_has_a_legend_entry():
    tags = {t for t, _ in PHASES}
    for expected in ("task0a", "task0b", "task0c", "cfg-scan", "bake", "strip",
                     "task1", "task2a", "task2b", "task2c", "task3", "task4",
                     "skeleton-validate", "retro"):
        assert expected in tags


def test_lines_fit_width_and_wrap_with_aligned_continuations():
    width = 60
    lines = legend_lines(width)
    assert lines
    tag_w = max(len(t) for t, _ in PHASES) + 2
    for ln in lines:
        assert len(ln) <= width + tag_w  # tag column + wrapped body never exceeds
    # every phase appears exactly once as a line prefix
    firsts = [ln for ln in lines if not ln.startswith(" ")]
    assert len(firsts) == len(PHASES)


def test_legend_window_scrolls_from_bottom():
    from pipeline_core.monitor.legend import legend_lines, legend_window
    rows = legend_lines(60)
    assert legend_window(60, 5, 0) == rows[-5:]          # bottom
    assert legend_window(60, 5, 3) == rows[-8:-3]        # scrolled up 3
    assert legend_window(60, len(rows) + 10, 0) == rows  # viewport bigger than content
