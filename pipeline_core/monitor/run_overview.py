"""The run overview that opens the monitor's health tab: is the pipeline running,
finished or stopped, which repository it works on, when it started and where
the generated code lives.

Before this the health tab opened on findings and phase costs, so a monitor left
open on a completed run showed no sign that the run was over. The status here is
one of four words, and "finished" is told apart from "stopped" by the pipeline's
own closing line: a process that is gone without it was stopped, whatever the
reason (graceful stop, kill, crash, a stop knob).

Pure over its inputs apart from `repo_url` (one `git remote get-url`) and the
log read inside `StatusReader`. The reader rereads the log only when the file
changed, and at most every `LIVE_REREAD_S` while the process runs: the log of a
live run grows every second, and the start line of a relaunch only has to be
picked up once it is written.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime, timezone
from pathlib import Path

__all__ = [
    "FINISHED", "LIVE_REREAD_S", "NOT_STARTED", "RUNNING", "START_MARKER", "STOPPED",
    "OverviewFeed", "StatusReader", "last_run_segment", "overview_lines", "repo_url",
    "run_started", "status_of",
]

RUNNING = "running"
FINISHED = "finished"
STOPPED = "stopped"
NOT_STARTED = "not started"

LIVE_REREAD_S = 30.0

START_MARKER = "PIPELINE STARTED — "
# Written by the reconstruction step when task 3 passes and the run completes.
_SUCCESS_MARKER = "SUCCESS: all tests passed"


def last_run_segment(lines: list[str]) -> list[str]:
    """The lines from the LAST `PIPELINE STARTED` marker on. A log without one
    is the tail of a run whose header was rotated out (the log rotates inside
    a long run), so the whole log is that run's: dropping it would call a run
    that ended in SUCCESS "not started". An empty log is []."""
    start = 0
    for i, ln in enumerate(lines):
        if START_MARKER in ln:
            start = i
    return lines[start:]


def status_of(running: bool, segment: list[str]) -> str:
    """running / finished / stopped / not started."""
    if running:
        return RUNNING
    if not segment:
        return NOT_STARTED
    return FINISHED if any(_SUCCESS_MARKER in ln for ln in segment) else STOPPED


def run_started(segment: list[str], tz: timezone | None = None,
               fallback_iso: str = "") -> str:
    """The run's start as `YYYY-MM-DD HH:MM:SS <zone>` in `tz` (the machine's
    zone when None). The log writes it in UTC (`PIPELINE STARTED — 2026-08-08
    09:04:53 UTC`); a segment without that line (rotated out) falls back to
    `fallback_iso`, the ISO UTC timestamp of the run's first recorded event.
    "" when neither gives a time."""
    head = segment[0] if segment else ""
    utc: datetime | None = None
    if START_MARKER in head:
        raw = head[head.find(START_MARKER) + len(START_MARKER):].split(" UTC")[0].strip()
        with contextlib.suppress(ValueError):
            utc = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    if utc is None and fallback_iso:
        with contextlib.suppress(ValueError):
            utc = datetime.fromisoformat(fallback_iso)
    return utc.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z") if utc else ""


def repo_url(project_dir: str | None) -> str:
    """The workspace clone's `origin` URL, or "" when it has none."""
    if not project_dir or not os.path.isdir(os.path.join(project_dir, ".git")):
        return ""
    try:
        return subprocess.check_output(
            ["git", "remote", "get-url", "origin"], cwd=project_dir,
            stderr=subprocess.DEVNULL, timeout=5).decode().strip()
    except (OSError, subprocess.SubprocessError):
        return ""


class StatusReader:
    """Status and start of the run in `log_path`, with the log read only when
    the process is down and the file moved since the last read."""

    def __init__(self, log_path: str | Path) -> None:
        self._log = Path(log_path)
        self._key: tuple[int, int] | None = None
        self._segment: list[str] = []
        self._read_at = 0.0

    def segment(self) -> list[str]:
        try:
            st = self._log.stat()
        except OSError:
            self._key, self._segment = None, []
            return []
        key = (st.st_mtime_ns, st.st_size)
        if key != self._key:
            try:
                text = self._log.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            self._key, self._segment = key, last_run_segment(text.splitlines())
            self._read_at = time.monotonic()
        return self._segment

    def read(self, running: bool) -> tuple[str, list[str]]:
        """`(status, segment)`. While running, the segment last read is reused
        for `LIVE_REREAD_S`: rereading a log that grows every second is what
        this class avoids."""
        if running and self._segment and time.monotonic() - self._read_at < LIVE_REREAD_S:
            return RUNNING, self._segment
        seg = self.segment()
        return status_of(running, seg), seg


def overview_lines(status: str, segment: list[str], project_dir: str | None,
                   repo: str, tz: timezone | None = None,
                   first_event_iso: str = "") -> list[str]:
    """The block that heads the health tab, followed by one blank line."""
    started = run_started(segment, tz, first_event_iso) or "unknown"
    return [
        f"Status:         {status}",
        f"Repository:     {repo or 'unknown'}",
        f"Started:        {started}",
        f"Generated code: {project_dir or 'unknown'}",
        "",
    ]


class OverviewFeed:
    """What the monitor shows at the head of the health tab, and when the
    status moved. `status_changed` is True once per change between two
    readings: the first reading after the monitor starts is not a change, so
    opening the monitor on a finished run does not move the view."""

    def __init__(self, log_path: str | Path, events_log: str,
                 find_project_dir: Callable[[], str | None],
                 is_running: Callable[[], bool]) -> None:
        self._reader = StatusReader(log_path)
        self._events_log = events_log
        self._find_project_dir = find_project_dir
        self._is_running = is_running
        self._repos: dict[str, str] = {}   # workspace dir -> origin URL, one git call each
        self._last: str | None = None

    def read(self) -> tuple[str, list[str]]:
        """`(status, the overview block)`."""
        status, seg = self._reader.read(self._is_running())
        proj = self._find_project_dir()
        if proj and proj not in self._repos:
            self._repos[proj] = repo_url(proj)
        first = ""
        if not seg or START_MARKER not in seg[0]:
            from pipeline_core.monitor.run_data import read_phase_records
            recs = read_phase_records(self._events_log)
            first = str(recs[0].get("ts", "")) if recs else ""
        return status, overview_lines(status, seg, proj, self._repos.get(proj or "", ""),
                                      first_event_iso=first)

    def status_changed(self) -> bool:
        status, _ = self.read()
        changed = self._last is not None and status != self._last
        self._last = status
        return changed
