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
from pathlib import Path
from typing import Any

from pipeline_core.log import log


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
