"""Log / state / backend readers for the monitor — the rest of the data half.

Companion to :mod:`events_data`: where that reads the structured `events.jsonl`, this
reads the free-form pipeline log, `pipeline_state.md`, the coverage JSON dumps, the
guard log, and `.env`/`/proc`/git for backend + run facts. Everything here returns plain
data (no ANSI); `monitor.py` formats it.

NOTE (debt #7): the pipeline-log readers below scrape free-form prose with regex — the log
is not a structured format with a real parser. The clean fix is to emit these facts as
structured events and read THOSE; tracked, out of scope for the relocation.

Config (`SCRIPT_DIR`, `PIPELINE_LOG`, `GUARDS_LOG`) is set by :func:`configure` at startup;
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
GUARDS_LOG: str = os.path.join(_ROOT, "logs", "pipeline_guards.log")

TASKS_ORDER: list[str] = [
    "task0a", "task0b", "task0c", "strip",
    "task1", "task2", "task3", "task4",
]


def configure(*, script_dir: str, pipeline_log: str, guards_log: str) -> None:
    """Point the readers at the live run's paths (called from monitor startup)."""
    global SCRIPT_DIR, PIPELINE_LOG, GUARDS_LOG
    SCRIPT_DIR = script_dir
    PIPELINE_LOG = pipeline_log
    GUARDS_LOG = guards_log


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


def parse_pipeline_log() -> dict[str, Any]:
    """Parse PIPELINE_LOG → attempt, file progress, uncovered-lines history."""
    attempt = "?"
    max_attempt = "?"
    errors = 0
    current_file = "?"
    files_this_pass: list[str] = []
    all_files: list[str] = []
    cluster_files: list[str] = []
    file_history: dict[str, list[int]] = {}
    queue_history: dict[str, list[int]] = {}
    quick_cov_history: dict[str, list[float]] = {}
    current_cluster = ""
    coverage = ""
    last_line = ""
    age: float = 0.0
    inner_retry: str = "?"
    inner_retry_max: str = "?"

    def _r() -> dict[str, Any]:
        return {
            "attempt": attempt, "max_attempt": max_attempt, "errors": errors,
            "current_file": current_file, "files_this_pass": files_this_pass,
            "all_files": all_files, "cluster_files": cluster_files,
            "file_history": file_history, "queue_history": queue_history,
            "current_cluster": current_cluster, "coverage": coverage,
            "last_line": last_line, "age": age,
            "quick_cov_history": quick_cov_history,
            "inner_retry": inner_retry, "inner_retry_max": inner_retry_max,
        }

    if not os.path.exists(PIPELINE_LOG):
        return _r()
    try:
        age = time.time() - os.path.getmtime(PIPELINE_LOG)
        # Read only the last 512 KB — the log can grow to many GB.
        _TAIL_BYTES = 512 * 1024
        with open(PIPELINE_LOG, "rb") as _fh:
            _fh.seek(0, 2)
            _size = _fh.tell()
            _fh.seek(max(0, _size - _TAIL_BYTES))
            _raw = _fh.read()
        lines = _raw.decode("utf-8", errors="replace").splitlines()
        if _size > _TAIL_BYTES:
            lines = lines[1:]  # drop potentially partial first line
    except Exception:
        return _r()
    last_line = next((ln.strip() for ln in reversed(lines) if ln.strip()), "")
    this_pass: list[str] = []
    _cluster_files: list[str] = []
    for line in lines:
        # Fresh pipeline start — reset all accumulated state so the previous run's
        # data in the log tail does not bleed into the current run's display.
        if "PIPELINE STARTED" in line:
            attempt = "?"
            max_attempt = "?"
            errors = 0
            current_cluster = ""
            coverage = ""
            this_pass = []
            _cluster_files = []
            all_files.clear()
        if m := re.search(r'Attempt\s+(\d+)/(\d+)', line):
            attempt, max_attempt = m.group(1), m.group(2)
            this_pass = []
            inner_retry = "?"; inner_retry_max = "?"
        if "Compile error" in line:
            errors += 1
        if m := re.search(r'Starting compiler cluster:\s*(\w+)', line):
            new_cl = m.group(1)
            if new_cl != current_cluster:
                current_cluster = new_cl
                _cluster_files = []
        if m := re.search(r'\[task0c planner\]\s+(src/\S+\.rs):', line):
            f = m.group(1)
            if f not in this_pass:
                this_pass.append(f)
            if f not in all_files:
                all_files.append(f)
            if f not in _cluster_files:
                _cluster_files.append(f)
            current_file = f
        # "File X/Y: src/..." — every file processed in a pass, including skipped ones
        if m := re.search(r'File \d+/\d+: (src/\S+\.rs)', line):
            f = m.group(1)
            if f not in _cluster_files:
                _cluster_files.append(f)
            current_file = f
            inner_retry = "?"; inner_retry_max = "?"
        if m := re.search(r'quick-retry\s+(\d+)/(\d+)\s+—', line):
            inner_retry, inner_retry_max = m.group(1), m.group(2)
        if m := re.search(r'Quick-retry \d+: (?:improvement|no improvement)', line):
            inner_retry = "?"; inner_retry_max = "?"
        if m := re.search(r'Appended gen_tests\S* to (src/\S+\.rs)\s+\((\d+) uncovered', line):
            f, n = m.group(1), int(m.group(2))
            if f not in file_history:
                file_history[f] = []
            file_history[f].append(n)
        if m := re.search(r'(src/\S+\.rs)\s+quick-cov:\s*\d+/\d+\s+\(([\d.]+)%\)', line):
            f, pct = m.group(1), float(m.group(2))
            if f not in quick_cov_history:
                quick_cov_history[f] = []
            # Only append if value changed
            if not quick_cov_history[f] or quick_cov_history[f][-1] != pct:
                quick_cov_history[f].append(pct)
        if m := re.search(r'Coverage:\s*([\d.]+)%', line):
            coverage = m.group(1) + "%"
        if m := re.search(r'Queue: \d+ file\(s\) — (.+)', line):
            for fm in re.finditer(r'(\w+\.rs)\((\d+)\)', m.group(1)):
                fname, count = fm.group(1), int(fm.group(2))
                if fname not in queue_history:
                    queue_history[fname] = []
                queue_history[fname].append(count)
    files_this_pass[:] = this_pass
    cluster_files[:] = _cluster_files
    return _r()


def read_log_tail() -> list[str]:
    """Read the last 512 KB of PIPELINE_LOG as a list of lines. Empty if missing."""
    if not os.path.exists(PIPELINE_LOG):
        return []
    try:
        _TAIL_BYTES = 512 * 1024
        with open(PIPELINE_LOG, "rb") as _fh:
            _fh.seek(0, 2)
            _size = _fh.tell()
            _fh.seek(max(0, _size - _TAIL_BYTES))
            _raw = _fh.read()
        lines = _raw.decode("utf-8", errors="replace").splitlines()
        if _size > _TAIL_BYTES:
            lines = lines[1:]  # drop potentially partial first line
        return lines
    except Exception:
        return []


def parse_task_pipeline(lines: list[str]) -> dict[str, Any]:
    """Scan log lines and return overall task progression.

    Returns {states, current, iteration, module, task0c_clusters_done, task3_pass}.
    """
    states: dict[str, str] = dict.fromkeys(TASKS_ORDER, "pending")
    current: str | None = None
    iteration: int | None = None
    module: str | None = None
    task0c_clusters_done: int = 0
    task3_pass: bool | None = None

    def _start(name: str) -> None:
        nonlocal current
        states[name] = "in_progress"
        current = name

    def _done(name: str) -> None:
        states[name] = "done"

    for line in lines:
        # Pipeline reset clears all task state.
        if "PIPELINE STARTED" in line:
            for t in TASKS_ORDER:
                states[t] = "pending"
            current = None
            iteration = None
            module = None
            task0c_clusters_done = 0
            task3_pass = None
            continue

        if m := re.search(r'MODULE\s+(\S+)\s+[—-]\s+ITERATION\s+(\d+)', line):
            module = m.group(1)
            iteration = int(m.group(2))
            # New iteration → task2/task3/task4 reset to pending. task1 is
            # per-module not per-iteration — its requirements file persists across
            # iterations (the LLM call short-circuits with "already extracted").
            for t in ("task2", "task3", "task4"):
                states[t] = "pending"
            current = None
            continue

        if "TASK 0a:" in line or "TASK 0a (context):" in line:
            _start("task0a")
        elif "TASK 0b" in line:
            _done("task0a")
            _start("task0b")
        elif re.search(r'TASK 0c \[', line):
            _done("task0b")
            _start("task0c")
        elif re.search(r'pipeline: task0c/', line) and "[git] committed" in line:
            task0c_clusters_done += 1
        elif re.search(r'pipeline: stripped', line) and "[git] committed" in line:
            _done("task0c")
            _done("strip")
        elif "TASK 1:" in line and "TASK 1.5" not in line:
            # "TASK 1: requirements for ... already extracted — skipping" → already done
            if "already extracted" in line:
                _done("task1")
            else:
                _start("task1")
        elif "TASK 1.5:" in line:
            pass  # treat as part of task1
        elif re.search(r'pipeline: task1 ', line) and "[git] committed" in line or "TASK 1 output validated OK" in line:
            _done("task1")
        elif re.search(r'TASK 2:', line):
            _start("task2")
        elif "TASK 2 inner loop exhausted" in line:
            # task2 failed → task4 will run next
            pass
        elif re.search(r'pipeline: task2 ', line) and "[git] committed" in line:
            _done("task2")
        elif re.search(r'TASK 3:', line):
            _done("task2")
            _start("task3")
            task3_pass = None
        elif re.search(r'Tests PASSED', line):
            _done("task3")
            task3_pass = True
        elif re.search(r'Tests FAILED', line):
            states["task3"] = "done"
            task3_pass = False
        elif re.search(r'TASK 4:', line):
            _start("task4")
        elif "genesis.md updated" in line:
            _done("task4")

    return {
        "states":  states,
        "current": current,
        "iteration": iteration,
        "module":  module,
        "task0c_clusters_done": task0c_clusters_done,
        "task3_pass": task3_pass,
    }


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


def parse_max_tokens() -> int | None:
    """Latest adaptive max_tokens cap from the guard log, or None."""
    try:
        with open(GUARDS_LOG) as f:
            lines = f.readlines()
        for line in reversed(lines):
            if m := re.search(r'max_tokens \d+ → (\d+)', line):
                return int(m.group(1))
    except Exception:
        pass
    return None


def parse_vllm_cpt() -> tuple[float | None, int]:
    """Return (latest chars-per-token EWM, count of vLLM 400 retries) from the guard log."""
    retries, cpt = 0, None
    try:
        with open(GUARDS_LOG) as f:
            lines = f.readlines()
        for line in lines:
            if "vllm/400_retry" in line:
                retries += 1
        for line in reversed(lines):
            if m := re.search(r'(?:ewm|cpt_ewm)=[\d.]+→([\d.]+)', line):
                cpt = float(m.group(1))
                break
    except Exception:
        pass
    return cpt, retries


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
