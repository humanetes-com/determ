"""Event-log readers for the monitor — the data half of the TUI.

Every function here is pure-of-rendering: it reads `events.jsonl` (and `/proc` for the
run start time) and returns plain dicts/lists/strings. No ANSI, no curses. The renderer
in `monitor.py` formats the results.

Config (`EVENTS_LOG`) is a module global set by :func:`configure` at startup. Functions
resolve it at call time, so monkeypatching `events_data.EVENTS_LOG` /
`events_data.pipeline_start_time` in a test redirects the readers at a fixture.
"""
from __future__ import annotations

import json as _json
import os
import re
import subprocess
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

# Default mirrors monitor.py's logs/ layout; monitor injects the discovered path.
EVENTS_LOG: str = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "logs", "events.jsonl",
)


def configure(*, events_log: str) -> None:
    """Point the readers at the live run's event log (called from monitor startup)."""
    global EVENTS_LOG
    EVENTS_LOG = events_log


def pipeline_start_time() -> float:
    """Return the mtime of /proc/{pid} for the running pipeline process (= process start time).
    Returns 0.0 if no pipeline process is found."""
    try:
        out = subprocess.run(["pgrep", "-f", "pipeline.py"], capture_output=True, text=True).stdout
        for pid in out.strip().split():
            proc_dir = f"/proc/{pid}"
            if os.path.exists(proc_dir):
                return os.path.getmtime(proc_dir)
    except Exception:
        pass
    return 0.0


def read_recent_events(want: Callable[[str], bool]) -> list[dict[str, Any]]:
    """Tail events.jsonl (last 1 MB) → this run's records whose event name passes `want`.

    Records older than the most recent pipeline start are dropped so panels
    reflect the live run, not historical noise.
    """
    if not os.path.exists(EVENTS_LOG):
        return []
    try:
        run_start = pipeline_start_time()
    except Exception:
        run_start = 0
    try:
        with open(EVENTS_LOG, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 1024 * 1024))  # last 1 MB
            raw = fh.read()
    except Exception:
        return []
    events: list[dict[str, Any]] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        try:
            rec = _json.loads(line)
        except Exception:
            continue
        if not want(rec.get("event", "")):
            continue
        if run_start > 0:
            try:
                ts = datetime.fromisoformat(rec["ts"].replace("Z", "+00:00")).timestamp()
                if ts < run_start:
                    continue
            except Exception:
                pass
        events.append(rec)
    return events


def coverage_from_events() -> str:
    """Latest module coverage from the structured coverage.measured event (R8.4).

    Returns e.g. "83.9%" or "" when this run has emitted no measurement yet
    (callers fall back to the legacy text scrapes for old logs).
    """
    evs = read_recent_events(lambda n: n == "coverage.measured")
    if not evs:
        return ""
    pct = (evs[-1].get("data") or {}).get("pct")
    try:
        return f"{float(pct):.1f}%" if pct is not None else ""
    except (TypeError, ValueError):
        return ""


def phase_c_state() -> dict[str, Any]:
    """Roll up Phase C per-fn events into a dashboard summary.

    Reads only events emitted SINCE the most recent pipeline start so the
    panel reflects the live run, not historical noise.
    """
    out: dict[str, Any] = {
        "active": False, "cluster": None, "pass_n": None, "max_passes": None,
        "fills_done": 0, "fills_first": 0, "fills_recovered": 0,
        "fills_failed": 0, "fills_skipped": 0, "stubs_remaining": 0,
        "notes": 0, "current_fn": None, "current_attempt": None,
        "recent_fills": [], "hot_fn": None,
        "last_test_rc": None, "last_test_failed": None,
        "last_event_age_s": None,
        # P4: progress bar fields (per-pass progress against stubs_at_pass_start).
        "pass_stubs_total": 0,
        "pass_fills_done": 0,
        "pass_fills_failed": 0,
        # Cumulative progress denominator (total fns in cluster scope).
        "total_fns_in_cluster": 0,
        # Authoritative cluster-scope filled count emitted by pass.start.
        # Preferred over the inferred `total - pass_stubs_total + done` math
        # below — that math gives wrong answers when the TDD driver narrows
        # `pass_stubs_total` to a per-pass frontier.
        "filled_count_at_pass_start": 0,
        "all_stubs_remaining_at_pass_start": 0,
        # TDD outer-loop trajectory (phase_c.tdd.iter) — the headline "are we
        # converging" signal: passing/failing tests over iterations.
        "tdd_iter": 0, "tdd_pass": None, "tdd_fail": None, "tdd_frontier": None,
        "tdd_stall": None, "tdd_kind": None, "tdd_pass_hist": [],
        # Per-cluster TDD trajectory so each cluster's pass floor is shown on its
        # own row — a fresh cluster starting lower never reads as a regression of
        # the previous one (the per-cluster loops reset their floor by design).
        "tdd_by_cluster": {},
        # Per-cluster overview ("where are we") + current error class.
        "clusters_seen": [], "clusters_done": [], "last_error_code": None,
        "err_codes": {},
    }
    events = read_recent_events(
        lambda n: n.startswith("phase_c.") or n == "cluster.done")

    if not events:
        return out
    out["active"] = True

    fn_attempts: dict[str, int] = {}
    in_flight_fn: str | None = None
    last_ts_iso: str | None = None
    for e in events:
        ev = e["event"]
        d = e.get("data", {})
        last_ts_iso = e.get("ts") or last_ts_iso
        # Cluster ordering: record first-seen order so we can show k/N progress.
        _cl = d.get("cluster") or e.get("cluster")
        if _cl and _cl not in out["clusters_seen"]:
            out["clusters_seen"].append(_cl)
        if ev == "cluster.done":
            _dc = e.get("cluster") or d.get("cluster")
            if _dc and _dc not in out["clusters_done"]:
                out["clusters_done"].append(_dc)
            continue
        if ev == "phase_c.pass.start":
            out["cluster"] = d.get("cluster") or out["cluster"]
            out["pass_n"] = d.get("pass_n") or out["pass_n"]
            out["max_passes"] = d.get("max_passes") or out["max_passes"]
            # Snapshot the pass's stub total so the progress bar has a
            # fixed denominator. Reset the in-pass counters tracked below
            # (otherwise prior pass's counts leak into this pass's bar).
            out["pass_stubs_total"] = int(d.get("stubs", 0))
            out["pass_fills_done"] = 0
            out["pass_fills_failed"] = 0
            # Cumulative progress denominator (total fns in cluster's cfg
            # scope, including ones filled in prior runs). Lets the bar
            # show real cumulative state instead of resetting to 0 per run.
            out["total_fns_in_cluster"] = int(d.get("total_fns_in_cluster", 0))
            # Authoritative cluster-scope filled / remaining counts at the
            # moment this pass started. Newer events carry these directly;
            # older events (before the disambiguation) won't, and we fall
            # back to the inferred math.
            out["filled_count_at_pass_start"] = int(d.get("filled_count", 0))
            out["all_stubs_remaining_at_pass_start"] = int(d.get("all_stubs_remaining", 0))
        elif ev == "phase_c.fn.fill.start":
            in_flight_fn = d.get("fn")
            out["cluster"] = d.get("cluster") or out["cluster"]
        elif ev == "phase_c.fn.fill.done":
            fn = d.get("fn") or "?"
            attempts = d.get("fix_attempts", 1)
            out["fills_done"] += 1
            out["pass_fills_done"] = out.get("pass_fills_done", 0) + 1
            if d.get("recovered"):
                out["fills_recovered"] += 1
            else:
                out["fills_first"] += 1
            fn_attempts[fn] = fn_attempts.get(fn, 0) + attempts
            out["recent_fills"].append((fn, d.get("body_chars", 0), attempts))
            in_flight_fn = None
        elif ev == "phase_c.fn.fill.fail":
            fn = d.get("fn") or "?"
            attempts = d.get("fix_attempts", 0)
            out["fills_failed"] += 1
            out["pass_fills_failed"] = out.get("pass_fills_failed", 0) + 1
            fn_attempts[fn] = fn_attempts.get(fn, 0) + attempts
            in_flight_fn = None
            # Current/last error class — extract the rustc code the model is
            # fighting, from the failed attempt's error preview.
            _m = re.findall(r"E\d{4}", d.get("last_error_preview") or "")
            if _m:
                out["last_error_code"] = _m[0]
                for _c in _m:
                    out["err_codes"][_c] = out["err_codes"].get(_c, 0) + 1
        elif ev == "phase_c.fn.fill.skip":
            out["fills_skipped"] += 1
            in_flight_fn = None
        elif ev == "phase_c.note.added":
            out["notes"] += 1
        elif ev == "phase_c.pass.done":
            out["stubs_remaining"] = d.get("remaining", out["stubs_remaining"])
        elif ev == "phase_c.pass.test":
            out["last_test_rc"] = d.get("rc")
            out["last_test_failed"] = len(d.get("failed_tests") or [])
        elif ev == "phase_c.tdd.iter":
            _tcl = d.get("cluster") or out["cluster"]
            out["cluster"] = _tcl or out["cluster"]
            out["tdd_iter"] = d.get("iter", out["tdd_iter"])
            _pc = d.get("pass_count")
            if _pc is not None:
                out["tdd_pass"] = _pc
                out["tdd_pass_hist"].append(_pc)
            out["tdd_fail"] = d.get("fail_count", out["tdd_fail"])
            out["tdd_frontier"] = d.get("frontier_size", out["tdd_frontier"])
            out["tdd_stall"] = d.get("stall", out["tdd_stall"])
            out["tdd_kind"] = d.get("kind", out["tdd_kind"])
            if _tcl:
                _cs = out["tdd_by_cluster"].setdefault(
                    _tcl, {"iter": 0, "pass": None, "fail": None,
                           "frontier": None, "stall": None, "hist": []})
                _cs["iter"] = d.get("iter", _cs["iter"])
                if _pc is not None:
                    _cs["pass"] = _pc
                    _cs["hist"].append(_pc)
                _cs["fail"] = d.get("fail_count", _cs["fail"])
                _cs["frontier"] = d.get("frontier_size", _cs["frontier"])
                _cs["stall"] = d.get("stall", _cs["stall"])

    out["current_fn"] = in_flight_fn
    if in_flight_fn:
        out["current_attempt"] = fn_attempts.get(in_flight_fn, 0)
    if out["recent_fills"]:
        out["recent_fills"] = out["recent_fills"][-5:]
    if fn_attempts:
        hot = max(fn_attempts.items(), key=lambda x: x[1])
        out["hot_fn"] = hot
    if last_ts_iso:
        try:
            ts = datetime.fromisoformat(last_ts_iso.replace("Z", "+00:00")).timestamp()
            out["last_event_age_s"] = int(time.time() - ts)
        except Exception:
            pass
    return out


def latest_compile_file_stats() -> dict[str, Any] | None:
    """Return the most recent compile.file_stats event payload, or None."""
    try:
        _run_start = pipeline_start_time()
        with open(EVENTS_LOG, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 128 * 1024))
            raw = fh.read()
        result = None
        for line in raw.decode("utf-8", errors="replace").splitlines():
            try:
                rec = _json.loads(line)
            except Exception:
                continue
            if rec.get("event") != "compile.file_stats":
                continue
            if _run_start > 0:
                try:
                    ts = datetime.fromisoformat(rec["ts"].replace("Z", "+00:00"))
                    if ts.timestamp() < _run_start:
                        continue
                except Exception:
                    pass
            result = rec.get("data") or {}
        return result if result else None
    except Exception:
        return None


def latest_test_file_stats() -> dict[str, Any] | None:
    """Most recent tests.file_stats event payload (per-file pass/fail), or None."""
    if not (evs := read_recent_events(lambda n: n == "tests.file_stats")):
        return None
    data = evs[-1].get("data")
    return data if isinstance(data, dict) else None


def latest_flow_audit() -> dict[str, Any] | None:
    """Most recent flow_audit.report payload (pipeline --audit), or None."""
    if not (evs := read_recent_events(lambda n: n == "flow_audit.report")):
        return None
    data = evs[-1].get("data")
    return data if isinstance(data, dict) else None
