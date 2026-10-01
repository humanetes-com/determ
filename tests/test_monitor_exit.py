"""The monitor never exits without saying why, and never exits on a bad frame.

It used to vanish with a cleared screen and no record (2026-09-30): a quit key
decoded out of a terminal report, a signal and a crash all looked the same.
And a frame whose repaint failed repainted its error message with the same
failing write, outside any handler, which ended the process.
"""
import signal

import pytest

import monitor as m
from pipeline_core.monitor import exit_record


def test_a_quit_carries_the_raw_input_it_was_decoded_from():
    reason = exit_record.describe(exit_record.key_quit("q", b"\x1b[M q2"))
    assert "q key" in reason and repr(b"\x1b[M q2") in reason


def test_sigterm_is_a_quit_the_render_loop_cannot_swallow():
    # SIGTERM raises from wherever the main thread is — often mid-frame,
    # inside the loop's `except Exception`.
    with pytest.raises(exit_record.Quit) as exc:
        exit_record.on_sigterm(signal.SIGTERM, None)
    assert not isinstance(exc.value, Exception)
    assert exit_record.describe(exc.value) == "quit: signal SIGTERM"


def test_a_crash_is_recorded_with_its_traceback(tmp_path):
    try:
        raise BlockingIOError(11, "Resource temporarily unavailable")
    except BlockingIOError as e:
        reason = exit_record.describe(e)
    log = tmp_path / "monitor_exit.log"
    exit_record.record(str(log), reason)
    text = log.read_text()
    assert "crash: " in text and "BlockingIOError" in text


def test_a_repaint_that_fails_does_not_end_the_monitor(monkeypatch):
    def refused():
        raise BlockingIOError(11, "Resource temporarily unavailable")
    monkeypatch.setattr(m, "_flush_frame", refused)
    m._repaint_error(BlockingIOError(11, "Resource temporarily unavailable"))
    assert m._buf == []
