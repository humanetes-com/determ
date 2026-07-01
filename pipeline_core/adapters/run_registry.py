"""FileRunRegistry — the shared live-run registry (a JSON list at
``/tmp/determ_pipelines.json`` by default) that ``monitor.py`` reads to
auto-discover running pipelines. Owns the registry's disk I/O: prune dead pids,
append this run's coordinates, and unregister on process exit.

Relocated from pipeline.py (C1). The caller builds the coordinate ``entry``
dict (it holds the run-context paths); this adapter owns only the load / prune /
write / atexit lifecycle so registry bookkeeping never breaks a real run."""
from __future__ import annotations

import atexit
import json
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.log import log


@dataclass(frozen=True, slots=True)
class RunRegistryEntry:
    """The coordinate record one pipeline run publishes to the live-run registry
    so ``monitor.py`` can auto-discover it. Names what used to be an ad-hoc 12-key
    dict built inline in pipeline.py. ``as_dict`` is the on-disk JSON shape — its
    key order matches the historical dict so the persisted registry (which the
    monitor reads by key) stays byte-stable."""

    pid: int
    project: str
    project_dir: str
    pipeline_log: str
    run_log: str | None
    stdout_log: str | None
    stream_log: str
    events_log: str
    guard_log: str
    argv: list[str]
    cwd: str
    started_at: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "project": self.project,
            "project_dir": self.project_dir,
            "pipeline_log": self.pipeline_log,
            "run_log": self.run_log,
            "stdout_log": self.stdout_log,
            "stream_log": self.stream_log,
            "events_log": self.events_log,
            "guard_log": self.guard_log,
            "argv": self.argv,
            "cwd": self.cwd,
            "started_at": self.started_at,
        }


class FileRunRegistry:
    def __init__(self, path: Path) -> None:
        self._path = path

    def register(self, entry: dict[str, Any]) -> None:
        """Prune dead pids (and any stale duplicate of our own), append
        ``entry``, and register an atexit hook that removes our pid. Fail-safe:
        any I/O error is logged and swallowed — never let bookkeeping abort a run."""
        try:
            pid = entry["pid"]
            reg = self._load()
            reg = [r for r in reg
                   if r.get("pid") != pid and Path(f"/proc/{r.get('pid')}").exists()]
            reg.append(entry)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(reg, indent=2))
            atexit.register(self._unregister, pid)
        except Exception as e:
            with suppress(Exception):
                log(f"  (registry write failed: {e})")

    def _load(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text())
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _unregister(self, pid: int) -> None:
        try:
            if not self._path.exists():
                return
            cur = [r for r in self._load() if r.get("pid") != pid]
            self._path.write_text(json.dumps(cur, indent=2))
        except Exception:
            pass
