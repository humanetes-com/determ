"""Log / state / backend readers for the monitor — the rest of the data half.

Companion to :mod:`events_data`: where that reads the structured `events.jsonl`, this
reads `pipeline_state.md`, the coverage JSON dumps, the raw pipeline-log tail, and
`.env`/`/proc`/git for backend + run facts. Everything here returns plain data (no
ANSI); `monitor.py` formats it.

NOTE (debt #7): the task0c progress facts + vLLM-runtime facts that used to be
regex-scraped here now come from structured events (see events_data.pipeline_log_facts
/ vllm_runtime). What remains reads pipeline_state.md (the pipeline's own structured
state file) or the raw log tail for presentation only (log_tail_meta).

Config (`SCRIPT_DIR`, `PIPELINE_LOG`) is set by :func:`configure` at startup;
functions resolve it at call time so tests can repoint the readers.
"""
from __future__ import annotations

import glob
import json as _json
import os
import re
import subprocess
import time
from typing import Any

from . import events_data

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SCRIPT_DIR: str = _ROOT
PIPELINE_LOG: str = os.path.join(_ROOT, "logs", "pipeline_anyhow.log")


def configure(*, script_dir: str, pipeline_log: str) -> None:
    """Point the readers at the live run's paths (called from monitor startup)."""
    global SCRIPT_DIR, PIPELINE_LOG
    SCRIPT_DIR = script_dir
    PIPELINE_LOG = pipeline_log


def parse_coverage_json(project_dir: str | None) -> dict[str, tuple[int, int]]:
    """Read per-file coverage from the most recent grcov/tarpaulin JSON output.
    Returns {rel_path: (covered, coverable)}, e.g. {'src/error.rs': (248, 285)}.
    Files older than the current pipeline process start time are ignored (stale from prior run)."""
    result: dict[str, tuple[int, int]] = {}
    main_mtime: float = 0.0
    _run_start = events_data.pipeline_start_time()

    # grcov coveralls JSON (pipeline writes /tmp/grcov_{module}.json).
    # Exclude quick/filecache variants — those are not full measurements.
    _main_paths = [p for p in glob.glob("/tmp/grcov_*.json")
                   if not os.path.basename(p).startswith(("grcov_quick_", "grcov_filecache_"))
                   and (_run_start == 0.0 or os.path.getmtime(p) >= _run_start)]
    for path in sorted(_main_paths, key=os.path.getmtime, reverse=True)[:1]:
        try:
            main_mtime = os.path.getmtime(path)
            with open(path) as _f:
                data = _json.load(_f)
            for sf in data.get("source_files", []):
                name      = sf.get("name", "")
                cov       = sf.get("coverage", [])
                covered   = sum(1 for c in cov if isinstance(c, int) and c > 0)
                coverable = sum(1 for c in cov if c is not None)
                if coverable > 0 and name:
                    result[name] = (covered, coverable)
        except Exception:
            pass

    # Merge per-file quick-cov updates that are newer than the last full measurement.
    for _qpath in [p for p in glob.glob("/tmp/grcov_filecache_*.json")
                   if _run_start == 0.0 or os.path.getmtime(p) >= _run_start]:
        try:
            with open(_qpath) as _f:
                _qdata = _json.load(_f)
            for _rel, _d in _qdata.items():
                if _d.get("ts", 0) > main_mtime and _d.get("coverable", 0) > 0:
                    result[_rel] = (_d["covered"], _d["coverable"])
        except Exception:
            pass

    if result:
        return result

    # tarpaulin JSON fallback (pipeline writes /tmp/tarp_{module}/tarpaulin-report.json)
    _tarp_paths = [p for p in glob.glob("/tmp/tarp_*/tarpaulin-report.json")
                   if _run_start == 0.0 or os.path.getmtime(p) >= _run_start]
    for path in sorted(_tarp_paths, key=os.path.getmtime, reverse=True)[:1]:
        try:
            with open(path) as _f:
                data = _json.load(_f)
            for f in data.get("files", []):
                parts     = f.get("path", [])
                name      = os.path.join(*parts) if parts else ""
                if project_dir and name.startswith(str(project_dir) + os.sep):
                    name = os.path.relpath(name, project_dir)
                covered   = f.get("covered", 0)
                coverable = f.get("coverable", 0)
                if coverable > 0 and name:
                    result[name] = (covered, coverable)
            if result:
                return result
        except Exception:
            pass

    return result


def baked_cluster_order() -> list[str]:
    """Authoritative cluster list+order from the pipeline log's
    'loaded N baked cluster(s): a, b, c' line. [] if unavailable — the panel
    then falls back to clusters seen in events so far."""
    try:
        with open(PIPELINE_LOG, encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except Exception:
        return []
    _matches = list(re.finditer(r"loaded \d+ baked cluster\(s\):\s*([^\n]+)", txt))
    last = _matches[-1] if _matches else None
    if not last:
        return []
    return [c.strip() for c in last.group(1).split(",") if c.strip()]


def find_project_dir() -> str | None:
    """Return path to the most-recently-active workspace in tmp/, or None."""
    tmp = os.path.join(SCRIPT_DIR, "tmp")
    if not os.path.isdir(tmp):
        return None
    best: tuple[float, str] | None = None
    for name in os.listdir(tmp):
        p = os.path.join(tmp, name, "pipeline_state.md")
        if os.path.isfile(p):
            mtime = os.path.getmtime(p)
            if best is None or mtime > best[0]:
                best = (mtime, os.path.join(tmp, name))
    return best[1] if best else None


def parse_pipeline_state(project_dir: str | None) -> dict[str, Any]:
    """Parse pipeline_state.md → cluster list, done set, current cluster, latest coverage."""
    clusters: list[str] = []
    done: set[str] = set()
    current = ""
    coverage = ""
    r: dict[str, Any] = {"clusters": clusters, "done": done, "current": current, "coverage": coverage}
    if not project_dir:
        return r
    try:
        with open(os.path.join(project_dir, "pipeline_state.md")) as _f:
            text = _f.read()
    except Exception:
        return r
    m = re.search(r'\d+ clusters?: ([^\n]+)', text)
    if m:
        clusters[:] = [c.strip() for c in m.group(1).split(",")]
    for line in text.splitlines():
        m = re.match(r'\[task0c/(\w+)\]\s+(done|in_progress)', line)
        if m and m.group(1) in clusters:
            if m.group(2) == "done":
                done.add(m.group(1))
            elif m.group(2) == "in_progress" and not current:
                current = m.group(1)
                r["current"] = current
    # Most recent coverage: scan state lines in reverse for first task0c entry with a %
    for line in reversed(text.splitlines()):
        if "task0c" in line:
            cv = re.search(r'(\d+\.\d+)%', line)
            if cv:
                r["coverage"] = cv.group(1) + "%"
                break
    return r


def current_stage_label() -> str:
    """Raw `current:` line from pipeline_state.md, e.g.
    'task2c/anyhow / merged-tree fill'. '' if unavailable."""
    pd = find_project_dir()
    if not pd:
        return ""
    try:
        with open(os.path.join(pd, "pipeline_state.md")) as _f:
            for line in _f:
                if line.startswith("current:"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return ""


def log_tail_meta() -> dict[str, Any]:
    """Raw-log presentation facts the monitor shows verbatim: the last non-empty
    log line and the log's staleness (seconds since last write). These are NOT
    regex-scraped facts — the structured progress facts moved to
    events_data.pipeline_log_facts() (debt#7). Returns {last_line, age}."""
    if not os.path.exists(PIPELINE_LOG):
        return {"last_line": "", "age": 0.0}
    try:
        age = time.time() - os.path.getmtime(PIPELINE_LOG)
        _TAIL_BYTES = 64 * 1024  # last non-empty line only — small tail suffices
        with open(PIPELINE_LOG, "rb") as _fh:
            _fh.seek(0, 2)
            _size = _fh.tell()
            _fh.seek(max(0, _size - _TAIL_BYTES))
            _raw = _fh.read()
        lines = _raw.decode("utf-8", errors="replace").splitlines()
    except Exception:
        return {"last_line": "", "age": 0.0}
    last_line = next((ln.strip() for ln in reversed(lines) if ln.strip()), "")
    return {"last_line": last_line, "age": age}


def read_backend() -> str:
    """The BACKEND value from .env (lowercased), defaulting to 'ollama'."""
    try:
        with open(os.path.join(SCRIPT_DIR, ".env")) as _f:
            for line in _f:
                m = re.match(r'^\s*BACKEND\s*=\s*(\S+)', line)
                if m:
                    return m.group(1).strip().lower()
    except Exception:
        pass
    return "ollama"


def pipeline_running() -> bool:
    """True if a pipeline.py process is alive."""
    try:
        subprocess.check_output(["pgrep", "-f", "pipeline.py"], stderr=subprocess.DEVNULL)
        return True
    except subprocess.CalledProcessError:
        return False


def detect_cached_repo_url() -> str:
    """Return the remote origin URL of the first git repo found in tmp/, or ''."""
    tmp_dir = os.path.join(SCRIPT_DIR, "tmp")
    if not os.path.isdir(tmp_dir):
        return ""
    for name in sorted(os.listdir(tmp_dir)):
        candidate = os.path.join(tmp_dir, name)
        if not os.path.isdir(os.path.join(candidate, ".git")):
            continue
        try:
            url = subprocess.check_output(
                ["git", "remote", "get-url", "origin"],
                cwd=candidate, stderr=subprocess.DEVNULL
            ).decode().strip()
            if url:
                return url
        except Exception:
            pass
    return ""
