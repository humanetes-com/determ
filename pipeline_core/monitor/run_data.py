"""Log / state / backend readers for the monitor — the rest of the data half.

Companion to :mod:`events_data`: where that reads the structured `events.jsonl`, this
reads `pipeline_state.md`, the coverage JSON dumps, the raw pipeline-log tail, and
`.env`/`/proc`/git for backend + run facts. Everything here returns plain data (no
ANSI); `monitor.py` formats it.

NOTE: the task0c progress facts + vLLM-runtime facts that used to be
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
import subprocess
import time
from typing import Any

from pipeline_core.domain import reasoning_cap

from . import events_data, render

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
    then falls back to clusters seen in events so far. (Structured line scan,
    equivalent to `loaded \\d+ baked cluster\\(s\\):\\s*([^\\n]+)`, last match wins.)"""
    try:
        with open(PIPELINE_LOG, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except Exception:
        return []
    marker = " baked cluster(s):"
    found: str | None = None
    for line in lines:
        i = line.find(marker)
        if i < 0:
            continue
        before = line[:i]
        li = before.rfind("loaded ")
        if li < 0 or not before[li + len("loaded "):].isdigit():
            continue
        found = line[i + len(marker):].lstrip()  # \s* after the colon
    if found is None:
        return []
    return [c.strip() for c in found.split(",") if c.strip()]


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


def phase_progress(project_dir: str | None) -> tuple[set[str], str]:
    """`(done phase keys, current stage line)` from pipeline_state.md.

    The pipeline's own state file is authoritative for phase status: the
    `[key] done` entries give real per-phase completion and the `current:`
    line gives the active stage. The rendering half is
    `render.phase_bar_rows`; this half is here so the strip's I/O sits with
    the other state readers rather than in the TUI god-file.

    `task2-4` is the combined reconstruct marker — one key for the whole
    task2a…task4 span — and is expanded by the caller that knows the phase
    sequence, not here.
    """
    done_keys: set[str] = set()
    cur = ""
    if not project_dir:
        return done_keys, cur
    try:
        with open(os.path.join(project_dir, "pipeline_state.md")) as _f:
            for line in _f:
                key = render.state_done_key(line)
                if key is not None:
                    done_keys.add(key)
                if line.startswith("current:"):
                    cur = line.split(":", 1)[1].strip()
    except Exception:
        pass
    return done_keys, cur


def _cluster_list_value(text: str) -> str | None:
    """The CSV after `<N> cluster[s]: ` (regex-free, equivalent to
    `\\d+ clusters?: ([^\\n]+)` — first line, ≥1 char captured), else None."""
    for line in text.splitlines():
        for kw in ("clusters:", "cluster:"):  # greedy `s?` → prefer the plural
            i = line.find(kw)
            if i <= 0 or line[i - 1] != " ":   # `\d+ ` → one space before the keyword
                continue
            if not line[:i - 1] or not line[:i - 1][-1].isdigit():
                continue
            rest = line[i + len(kw):]
            if rest.startswith(" ") and rest[1:]:  # `: ` then ([^\n]+)
                return rest[1:]
    return None


def _task0c_state_line(line: str) -> tuple[str, str] | None:
    """`(cluster, status)` from a `[task0c/<name>] <done|in_progress>` line
    (regex-free, equivalent to `\\[task0c/(\\w+)\\]\\s+(done|in_progress)`), else None."""
    prefix = "[task0c/"
    if not line.startswith(prefix):
        return None
    rest = line[len(prefix):]
    k = 0
    while k < len(rest) and (rest[k].isalnum() or rest[k] == "_"):  # \w+
        k += 1
    if k == 0 or not rest[k:].startswith("]"):
        return None
    after = rest[k + 1:]
    stripped = after.lstrip()
    if stripped == after:  # \s+ requires ≥1 whitespace char
        return None
    for status in ("done", "in_progress"):
        if stripped.startswith(status):
            return rest[:k], status
    return None


def _decimal_percent(line: str) -> str | None:
    """The first `\\d+\\.\\d+` immediately before a `%` (regex-free), else None."""
    i = 0
    while (p := line.find("%", i)) >= 0:
        frac = p
        while frac > 0 and line[frac - 1].isdigit():
            frac -= 1
        if frac < p and frac > 0 and line[frac - 1] == ".":
            whole = frac - 1
            while whole > 0 and line[whole - 1].isdigit():
                whole -= 1
            if whole < frac - 1:  # ≥1 integer digit
                return line[whole:p]
        i = p + 1
    return None


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
    cluster_csv = _cluster_list_value(text)  # `\d+ clusters?: ([^\n]+)`
    if cluster_csv is not None:
        clusters[:] = [c.strip() for c in cluster_csv.split(",")]
    for line in text.splitlines():
        parsed = _task0c_state_line(line)  # `\[task0c/(\w+)\]\s+(done|in_progress)`
        if parsed is None:
            continue
        name, status = parsed
        if name not in clusters:
            continue
        if status == "done":
            done.add(name)
        elif status == "in_progress" and not current:
            current = name
            r["current"] = current
    # Most recent coverage: scan state lines in reverse for first task0c entry with a %
    for line in reversed(text.splitlines()):
        if "task0c" in line:
            pct = _decimal_percent(line)  # `(\d+\.\d+)%`
            if pct is not None:
                r["coverage"] = pct + "%"
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
    events_data.pipeline_log_facts(). Returns {last_line, age}."""
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
    """The BACKEND value from .env (lowercased), defaulting to 'ollama'.
    (Structured key=value scan, equivalent to `^\\s*BACKEND\\s*=\\s*(\\S+)`.)"""
    try:
        with open(os.path.join(SCRIPT_DIR, ".env")) as _f:
            for line in _f:
                s = line.lstrip()                  # ^\s*
                if not s.startswith("BACKEND"):
                    continue
                after = s[len("BACKEND"):].lstrip()  # \s*
                if not after.startswith("="):
                    continue
                value = after[1:].split()           # = \s* then (\S+) = first token
                if value:
                    return value[0].lower()
    except Exception:
        pass
    return "ollama"


_CUT_STOP_KEYS = ("LLM_CUT_STOP_SHARE", "LLM_CUT_STOP_WINDOW")


def read_cut_stop_knobs() -> reasoning_cap.CutStopKnobs:
    """`LLM_CUT_STOP_SHARE`/`_WINDOW` as the pipeline reads
    them at cluster entry (`reasoning_cap.cut_stop_knobs_from_env`), sourced
    from `.env` on disk: the monitor is a separate process, never forked
    from the pipeline's, so it inherits none of its environment and no
    dotenv loader runs here (unlike `pipeline.py`/`model_interact.py`)."""
    env: dict[str, str] = {}
    try:
        with open(os.path.join(SCRIPT_DIR, ".env")) as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                key, _, val = s.partition("=")
                key = key.strip()
                if key in _CUT_STOP_KEYS:
                    env[key] = val.strip()
    except OSError:
        pass
    return reasoning_cap.cut_stop_knobs_from_env(env)


def pipeline_running() -> bool:
    """True if a REGISTERED pipeline.py process is alive.

    Judged by the live-run registry (every run appends its pid on start and
    unregisters atexit — ``adapters/run_registry.py``), each recorded pid
    verified against ``/proc`` — the same rule ``monitor.py``'s own run
    discovery applies to the same file. Deliberately NOT ``pgrep -f
    pipeline.py``: that is a substring match over full command lines, and any
    shell whose command line merely QUOTES the script's name satisfies it. On
    2026-08-18 it held the monitor's "running" badge up for over an hour
    after run ``2953362-63d4b47e`` exited 0 — the match was a session watcher
    shell whose own ``pgrep`` loop contained ``pipeline.py --project anyhow``
    as text. A registry entry whose pid is dead (a run killed before atexit)
    is not "running" either; ``/proc`` is the arbiter.
    """
    try:
        with open(os.getenv("PIPELINE_RUN_REGISTRY",
                            "/tmp/determ_pipelines.json")) as fh:
            reg = _json.load(fh)
    except Exception:
        return False
    if not isinstance(reg, list):
        return False
    return any(isinstance(r, dict) and r.get("pid")
               and os.path.exists(f"/proc/{r['pid']}")
               for r in reg)


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
