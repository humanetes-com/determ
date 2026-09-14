"""continue.sh must not hand a leftover stop request to a new process.

Run 520338-26bbb7bb (2026-09-14) launched 8s after 485054 was SIGTERM'd
mid-pair-fill (exit 143, deliberate). The stop sentinel from that kill was
still on disk; the replacement reached cluster entry, then exited at TDD
iter 1 after 12s with `stop sentinel detected`. graceful_stop_pipeline
unlinks only after a wait it itself started, so --force, a touch then a
later launch, and a launch with nothing running all inherited the file.

Negative control: the pre-fix continue.sh had no call to
clear_graceful_stop_sentinel between the last running-pipeline guard and
record_death.sh. The helper did not exist.
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def test_continue_clears_leftover_stop_sentinel_before_launch():
    text = (_ROOT / "continue.sh").read_text(encoding="utf-8")
    launch = text.index("lib/record_death.sh")
    pre = text[:launch]
    guard = pre.rfind("if pipeline_running; then")
    assert guard != -1
    window = pre[guard:]
    assert "clear_graceful_stop_sentinel" in window
    helper = (_ROOT / "lib" / "graceful_stop.sh").read_text(encoding="utf-8")
    body = helper.split("clear_graceful_stop_sentinel()")[1].split("}", 1)[0]
    assert 'rm -f "$_SENTINEL"' in body
