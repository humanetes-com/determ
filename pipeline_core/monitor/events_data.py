"""Event-log readers for the monitor — the data half of the TUI.

Every function here is pure-of-rendering: it reads `events.jsonl` (and `/proc` for the
run start time) and returns plain dicts/lists/strings. No ANSI, no curses. The renderer
in `monitor.py` formats the results.

Config (`EVENTS_LOG`) is a module global set by :func:`configure` at startup. Functions
resolve it at call time, so monkeypatching `events_data.EVENTS_LOG` /
`events_data.pipeline_start_time` in a test redirects the readers at a fixture.
"""
from __future__ import annotations

import contextlib
import json as _json
import os
import subprocess
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from pipeline_core.domain.reasoning_cap import CutRate, CutStopKnobs, mark_of
from pipeline_core.monitor import iteration_steps as _steps
from pipeline_core.monitor.fill_progress import FillProgress, fold_fill

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
    """Wall-clock start of the running pipeline process, or 0.0 if none.

    Two defects made this return a time HOURS after the real start, and every
    panel keyed to "this run" then rendered blank because every one of the
    run's events sorted as older than its own start:

    * `/proc/{pid}` mtime is not the process start time. The supervisor's
      (`lib/record_death.sh`) read 2026-08-04 21:15:33 local for a process that
      began at 18:56:21 — the directory's mtime moves during the process's
      life. Field 22 of `/proc/{pid}/stat` is the real start, in clock ticks
      since boot.
    * `pgrep -f pipeline.py` matches every command line CONTAINING the string:
      the `record_death.sh … pipeline.py` supervisor wrapper, and any shell
      whose own command mentions it. The first pid listed is the lowest, which
      is the supervisor, not the pipeline. Match the python invocation.

    Callers should prefer scoping by `run_id` (:func:`read_recent_events`) —
    that needs no clock and cannot drift. This stays for the panels that want
    an elapsed-time reading.
    """
    try:
        out = subprocess.run(
            ["pgrep", "-f", r"python.*pipeline\.py"],
            capture_output=True, text=True).stdout
        for pid in out.strip().split():
            try:
                with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
                    stat = fh.read()
                # comm can contain spaces AND ')' — split after the last ')'.
                fields = stat[stat.rindex(")") + 1:].split()
                start_ticks = int(fields[19])          # field 22, 1-indexed
            except (OSError, ValueError, IndexError):
                continue
            ticks = os.sysconf("SC_CLK_TCK") or 100
            with open("/proc/stat", encoding="utf-8") as fh:
                boot = next((float(ln.split()[1]) for ln in fh
                             if ln.startswith("btime ")), 0.0)
            if boot:
                return boot + start_ticks / ticks
    except Exception:
        pass
    return 0.0


_TASKS_ORDER = ("task0a", "task0b", "task0c", "strip", "task1", "task2", "task3", "task4")


def task_progression() -> dict[str, Any]:
    """Derive the overall task-progression from STRUCTURED events — the
    sole source (replaced `run_data.parse_task_pipeline`'s log regex-scrape).

    Replays this run's `task.start`/`task.done`/`module.iter`/`cluster.done` records
    into the shape the monitor consumes: {states, current, iteration, module,
    task0c_clusters_done, task3_pass}. When no stage events exist yet (run just
    started, or none landed) returns the all-pending baseline. Linear-pipeline
    model: a `task.start` marks that task in_progress and every canonical
    predecessor done; `module.iter` resets the per-iteration tasks (task2/3/4)."""
    evs = read_recent_events(
        lambda n: n in ("task.start", "task.done", "module.iter", "cluster.done"))
    states: dict[str, str] = dict.fromkeys(_TASKS_ORDER, "pending")
    current: str | None = None
    iteration: int | None = None
    module: str | None = None
    task0c_clusters_done = 0
    task3_pass: bool | None = None
    for ev in evs:
        name = ev.get("event", "")
        if name == "module.iter":
            module = ev.get("module", module)
            it = ev.get("iteration")
            if it is not None:
                iteration = int(it)
            for rt in ("task2", "task3", "task4"):
                states[rt] = "pending"
            current = None
        elif name == "task.start":
            t = ev.get("task")
            if t in states:
                for pred in _TASKS_ORDER[:_TASKS_ORDER.index(t)]:
                    states[pred] = "done"
                states[t] = "in_progress"
                current = t
        elif name == "task.done":
            t = ev.get("task")
            if t in states:
                states[t] = "done"
            if t == "task3":
                task3_pass = bool((ev.get("data") or {}).get("passed"))
        elif name == "cluster.done":
            task0c_clusters_done += 1
    return {
        "states": states, "current": current, "iteration": iteration,
        "module": module, "task0c_clusters_done": task0c_clusters_done,
        "task3_pass": task3_pass,
    }


_PIPELINE_LOG_FACT_EVENTS = (
    "task0c.attempt", "task0c.compile_error", "compiler.cluster.start",
    "task0c.planner", "task0c.file", "task0c.quick_retry",
    "task0c.quick_retry_done", "task0c.gen_tests_appended",
    "task0c.quick_cov", "task0c.queue",
)


def pipeline_log_facts() -> dict[str, Any]:
    """The task0c progress facts the monitor's loop panels consume,
    replayed from STRUCTURED events (replaces run_data.parse_pipeline_log's regex
    scrapes of the free-form log). `read_recent_events` already scopes to this run,
    so no PIPELINE-STARTED reset is needed. `last_line`/`age` are NOT facts — they
    are raw-log presentation and stay in run_data.log_tail_meta()."""
    attempt = max_attempt = "?"
    # Whether this run has a task0c phase AT ALL. Every field below is fed only
    # by `task0c.*` events, and a RESUMED run skips task0c entirely — the
    # clusters are baked and the finished ones are checkpointed — so on such a
    # run the loop-hierarchy rows render `attempt ?/? · file ? 0/0` forever.
    # They are honest and useless; the caller uses this to render the live
    # counter that DOES move (the TDD iter) instead.
    saw_task0c = False
    errors = 0
    current_file = "?"
    files_this_pass: list[str] = []
    all_files: list[str] = []
    cluster_files: list[str] = []
    file_history: dict[str, list[int]] = {}
    queue_history: dict[str, list[int]] = {}
    quick_cov_history: dict[str, list[float]] = {}
    current_cluster = ""
    inner_retry = inner_retry_max = "?"

    for ev in read_recent_events(lambda n: n in _PIPELINE_LOG_FACT_EVENTS):
        name = ev.get("event", "")
        d = ev.get("data") or {}
        f = ev.get("file")
        if name.startswith("task0c."):
            saw_task0c = True
        if name == "task0c.attempt":
            attempt, max_attempt = str(d.get("attempt", "?")), str(d.get("max_attempt", "?"))
            files_this_pass = []
            inner_retry = inner_retry_max = "?"
        elif name == "task0c.compile_error":
            errors += 1
        elif name == "compiler.cluster.start":
            cl = ev.get("cluster") or ""
            if cl and cl != current_cluster:
                current_cluster = cl
                cluster_files = []
        elif name == "task0c.planner" and f:
            for bucket in (files_this_pass, all_files, cluster_files):
                if f not in bucket:
                    bucket.append(f)
            current_file = f
        elif name == "task0c.file":
            if f:
                if f not in cluster_files:
                    cluster_files.append(f)
                current_file = f
            inner_retry = inner_retry_max = "?"
        elif name == "task0c.quick_retry":
            inner_retry, inner_retry_max = str(d.get("retry", "?")), str(d.get("max", "?"))
        elif name == "task0c.quick_retry_done":
            inner_retry = inner_retry_max = "?"
        elif name == "task0c.gen_tests_appended" and f is not None:
            file_history.setdefault(f, []).append(int(d.get("uncovered", 0)))
        elif name == "task0c.quick_cov" and f is not None:
            pct = float(d.get("pct", 0.0))
            hist = quick_cov_history.setdefault(f, [])
            if not hist or hist[-1] != pct:
                hist.append(pct)
        elif name == "task0c.queue":
            for fname, count in d.get("files", []):
                queue_history.setdefault(fname, []).append(int(count))

    return {
        "attempt": attempt, "max_attempt": max_attempt, "errors": errors,
        "current_file": current_file, "files_this_pass": files_this_pass,
        "all_files": all_files, "cluster_files": cluster_files,
        "file_history": file_history, "queue_history": queue_history,
        "current_cluster": current_cluster, "quick_cov_history": quick_cov_history,
        "inner_retry": inner_retry, "inner_retry_max": inner_retry_max,
        "has_task0c": saw_task0c,
    }


def vllm_runtime() -> dict[str, Any]:
    """vLLM runtime facts (adaptive token budget, chars-per-token EWM,
    400-retry count) from STRUCTURED events — replaces run_data.parse_max_tokens /
    parse_vllm_cpt's guard-log scrapes. Returns {max_tokens, cpt, retries}."""
    max_tokens: int | None = None
    cpt: float | None = None
    retries = 0
    for ev in read_recent_events(lambda n: n in ("vllm.budget", "vllm.cpt_update", "vllm.input_retry")):
        name = ev.get("event", "")
        d = ev.get("data") or {}
        if name == "vllm.input_retry":
            retries += 1
        if "request_max" in d:
            with contextlib.suppress(TypeError, ValueError):
                max_tokens = int(d["request_max"])
        if "cpt" in d:
            with contextlib.suppress(TypeError, ValueError):
                cpt = float(d["cpt"])
    return {"max_tokens": max_tokens, "cpt": cpt, "retries": retries}


def vllm_budget_by_thread() -> dict[str, int]:
    """thread -> the `max_tokens` of the request that thread most recently made.

    :func:`vllm_runtime` returns the last budget seen by ANY thread, which was
    fine while one thread generated at a time. Under the parallel fill engine
    four workers hold four requests at once and the budget adapts per request,
    so a per-worker bar drawn against the global last reading is drawn against
    another worker's ceiling. The per-thread map is what each worker's own row
    divides by; the global stays the fallback for a thread with no budget event
    in the window.
    """
    out: dict[str, int] = {}
    for ev in read_recent_events(lambda n: n == "vllm.budget"):
        thread = ev.get("thread")
        raw = (ev.get("data") or {}).get("request_max")
        if not thread or raw is None:
            continue
        with contextlib.suppress(TypeError, ValueError):
            out[str(thread)] = int(raw)
    return out


def read_recent_events(want: Callable[[str], bool]) -> list[dict[str, Any]]:
    """Tail events.jsonl (last 1 MB) → this run's records whose event name passes `want`.

    Scoped by `run_id`: every record the emitter writes carries the run's id,
    so "this run" is the newest id present in the window and needs no clock.
    It replaced a comparison against :func:`pipeline_start_time`, which read the
    supervisor's `/proc` mtime and could land HOURS after the real start — on
    2026-08-04 it returned 19:15:33 UTC for a run begun at 16:56:21, so every
    one of that run's events was dropped as "historical" and the whole Phase C
    panel rendered empty (`?/?`, `0/0`, no pass count) while the run was
    healthy and filling. A timestamp answers "recent"; only the id answers
    "same run", and restarts here are frequent enough that the difference shows.

    Falls back to the timestamp filter for records written before the emitter
    carried a `run_id`.
    """
    if not os.path.exists(EVENTS_LOG):
        return []
    try:
        with open(EVENTS_LOG, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            # 8 MB ≈ a week at this pipeline's measured append rate (run
            # 1159463-b1e352e5 wrote <1 MB in 11 h). At 1 MB the window was
            # shorter than one long TDD iteration: on 2026-08-09 it held only
            # the newest of two tdd.iter stamps 9 h into iter 3, so the pace
            # bar (needs two stamps for a median) vanished exactly when the
            # iteration ran long.
            fh.seek(max(0, size - 8 * 1024 * 1024))
            raw = fh.read()
    except Exception:
        return []
    parsed: list[dict[str, Any]] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        try:
            parsed.append(_json.loads(line))
        except Exception:
            continue
    # The LAST non-empty run_id in the window is the live run's; a restart
    # inside the window leaves the previous run's records ahead of it.
    run_id = next((r.get("run_id") for r in reversed(parsed) if r.get("run_id")), "")
    run_start = 0.0
    if not run_id:
        try:
            run_start = pipeline_start_time()
        except Exception:
            run_start = 0.0
    events: list[dict[str, Any]] = []
    for rec in parsed:
        if not want(rec.get("event", "")):
            continue
        if run_id:
            if rec.get("run_id") != run_id:
                continue
        elif run_start > 0:
            try:
                ts = datetime.fromisoformat(rec["ts"].replace("Z", "+00:00")).timestamp()
                if ts < run_start:
                    continue
            except Exception:
                pass
        events.append(rec)
    return events


def coverage_from_events() -> str:
    """Latest module coverage from the structured coverage.measured event.

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


def _record_error_codes(out: dict[str, Any], d: dict[str, Any]) -> None:
    """Tally the rustc E-codes on a fill-fail event. Prefers the structured
    `error_codes` field (emitted by the fill engine); no regex."""
    codes = d.get("error_codes") or []
    if not codes:
        return
    out["last_error_code"] = codes[0]
    for c in codes:
        out["err_codes"][c] = out["err_codes"].get(c, 0) + 1


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
        # Progress bar fields (per-pass progress against stubs_at_pass_start).
        "pass_stubs_total": 0,
        # pass_fills_done/failed count each per-fn fill, each impl-block MEMBER,
        # and each whole-file fn — so the `frontier: A/B` progress segment stays
        # correct across all three fill paths (not just per-fn). Reset per
        # sweep: on tdd.iter, and again on any pass.start within it. tdd.iter
        # is the one that always arrives — a fully-bodied tree emits no
        # pass.start at all (see the tdd.iter handler).
        "pass_fills_done": 0,
        "pass_fills_failed": 0,
        # In-flight fills (parallel — up to PIPELINE_PARALLELISM at once): each is
        # {label, kind: fn|impl, refill, age_s, file}. The set, not one cursor.
        "in_flight": [],
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
        "tdd_max_stalls": None,
        # The oracle the last sweep COUNTED (`oracle_size` = pass + fail) and
        # the oracle the tree declares (`oracle_static`). The score bar is
        # drawn against the counted one with the gap named beside it — and the
        # gap is split by cause from the three fields below it, never reported
        # as one lump called "unreachable" (see monitor/oracle_gap.py: the
        # crash's own lost tests are inside `fail`, while the gap is the
        # verdicts a crashed binary printed before aborting plus the tests
        # that never ran).
        "tdd_oracle": None, "tdd_oracle_static": None,
        # `incomplete` = binaries that opened a section and printed no
        # trailer; the pair vectors are (binary, test) from the same parse the
        # counters come from, so a pair whose binary is incomplete is a verdict
        # that was observed and could not be counted.
        "tdd_incomplete_binaries": [], "tdd_passed_pairs": [],
        "tdd_failed_pairs": [],
        # Which step of the current TDD iteration the loop was last OBSERVED
        # in (monitor/iteration_steps.py), and the ISO stamp of the event that
        # put it there — the panel renders time-in-step from it.
        "tdd_step": None, "tdd_step_since": None,
        # ISO ts of each tdd.iter event this run, in arrival order. Consecutive
        # gaps are whole-iteration durations (the summary prints once per
        # iter), which is what the panel's `~N/iter` median is computed from.
        "tdd_iter_ts": [],
        # Which outer loop is driving this run. Under "tdd" the per-fn engine is
        # an inner detail invoked with max_passes=1 — and it short-circuits
        # without a pass.start whenever the frontier is all behavioural re-fills
        # (zero stubs), so pass_n/max_passes stay None for the whole run. The
        # live counter to render then is the tdd iter, never "?/?".
        "driver": None,
        # Per-cluster TDD trajectory so each cluster's pass floor is shown on its
        # own row — a fresh cluster starting lower never reads as a regression of
        # the previous one (the per-cluster loops reset their floor by design).
        "tdd_by_cluster": {},
        # Per-cluster overview ("where are we") + current error class.
        "clusters_seen": [], "clusters_done": [], "last_error_code": None,
        "err_codes": {},
        # Cumulative body-fills landed (phase_c.fill.progress) — mid-sweep
        # progress independent of the per-sweep pass reading.
        "fills_landed": 0,
        # Where inside the `fill` step the loop is: which of the three routes,
        # how many units each has closed of the count it planned, and the
        # fix-loop gauge of every unit still in flight
        # (:mod:`pipeline_core.monitor.fill_progress`). The step strip reports
        # `fill` for nearly the whole iteration, so this is the level the
        # panel's remaining questions are answered at.
        "fill": FillProgress(),
        # ISO ts of the newest completed fill unit (done or fail, any of the
        # three fill shapes). Its age is the finest-grain stall signal the
        # events carry: fills tick every few minutes on a healthy iteration.
        "last_fill_ts_iso": None,
    }
    # `loop.iter` is the two fill routes' per-attempt gauge (floor + stall,
    # `fill_one_fn.py::_on_fn_failure` and `impl_fill.py::_on_impl_failure`).
    # It is the only reading that says whether a unit 40 minutes into its fix
    # loop is converging, and the name does not start with `phase_c.` — so a
    # filter written as a single prefix dropped it and the panel had the fill's
    # elapsed time and nothing about its direction.
    events = read_recent_events(
        lambda n: n.startswith("phase_c.") or n in ("cluster.done", "loop.iter"))

    if not events:
        return out
    out["active"] = True

    fn_attempts: dict[str, int] = {}
    # label -> {kind, refill, start_iso, file}; parallel fills coexist here.
    in_flight_units: dict[str, dict[str, Any]] = {}
    last_ts_iso: str | None = None
    for e in events:
        ev = e["event"]
        d = e.get("data", {})
        last_ts_iso = e.get("ts") or last_ts_iso
        # Which step of the TDD iteration this event proves the loop was in.
        # An event the map does not know leaves the step alone — that rule is
        # what keeps a newly-added event name from moving the strip by
        # accident. The stamp moves only on a CHANGE, so the panel's
        # time-in-step measures the step and not the last event inside it.
        _step = _steps.step_of_event(ev, d)
        if _step is not None and _step != out["tdd_step"]:
            out["tdd_step"] = _step
            out["tdd_step_since"] = e.get("ts")
        # Cluster ordering: record first-seen order so we can show k/N progress.
        _cl = d.get("cluster") or e.get("cluster")
        if _cl and _cl not in out["clusters_seen"]:
            out["clusters_seen"].append(_cl)
        if ev in ("phase_c.fn.fill.done", "phase_c.fn.fill.fail",
                  "phase_c.file.fill.done", "phase_c.impl.fill.done",
                  "phase_c.impl.fill.fail") and e.get("ts"):
            out["last_fill_ts_iso"] = e["ts"]
        if ev == "cluster.done":
            _dc = e.get("cluster") or d.get("cluster")
            if _dc and _dc not in out["clusters_done"]:
                out["clusters_done"].append(_dc)
            continue
        if ev == "phase_c.pass.start":
            # The tdd driver also drives the per-fn engine, so it wins the label.
            out["driver"] = out["driver"] or "per-fn"
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
            fn = d.get("fn") or "?"
            in_flight_units[fn] = {
                "kind": "fn", "refill": bool(d.get("refill")),
                "start_iso": e.get("ts"), "file": d.get("file"),
                # The emitting thread, so the console can join this unit to the
                # per-thread stream log the transport writes for it
                # (adapters/llm_transports.py::_stream_path_for_thread).
                "thread": e.get("thread"),
            }
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
            in_flight_units.pop(fn, None)
        elif ev == "phase_c.fn.fill.fail":
            fn = d.get("fn") or "?"
            attempts = d.get("fix_attempts", 0)
            out["fills_failed"] += 1
            out["pass_fills_failed"] = out.get("pass_fills_failed", 0) + 1
            fn_attempts[fn] = fn_attempts.get(fn, 0) + attempts
            in_flight_units.pop(fn, None)
            _record_error_codes(out, d)
        elif ev == "phase_c.fn.fill.skip":
            out["fills_skipped"] += 1
            in_flight_units.pop(d.get("fn") or "?", None)
        elif ev == "phase_c.impl.fill.start":
            # A file-first fill for this file that fell back to per-block fills →
            # drop the now-superseded file unit so it isn't shown alongside its blocks.
            for _k in [k for k, v in in_flight_units.items()
                       if v.get("kind") == "file" and v.get("file") == d.get("file")]:
                in_flight_units.pop(_k, None)
            _lbl = d.get('container') or '?'
            in_flight_units[_lbl] = {
                "kind": "impl", "refill": bool(d.get("refill_count")),
                "start_iso": e.get("ts"), "file": d.get("file"),
                "members": int(d.get("stub_count", 0)),
                "thread": e.get("thread"),
            }
            out["cluster"] = d.get("cluster") or out["cluster"]
        elif ev == "phase_c.file.fill.start":
            _lbl = f"file {d.get('file') or '?'}"
            in_flight_units[_lbl] = {
                "kind": "file", "refill": False,
                "start_iso": e.get("ts"), "file": d.get("file"),
                "thread": e.get("thread"),
            }
            out["cluster"] = d.get("cluster") or out["cluster"]
        elif ev == "phase_c.file.fill.done":
            _lbl = f"file {d.get('file') or '?'}"
            _fns = int(d.get("fns_filled", 0)) or 1
            out["fills_done"] += _fns
            out["pass_fills_done"] = out.get("pass_fills_done", 0) + _fns
            out["fills_first"] += _fns
            in_flight_units.pop(_lbl, None)
        elif ev in ("phase_c.file.fill.revert", "phase_c.file.fill.mismatch"):
            # Terminal for the file unit's current attempt but emits no
            # done/fail. Unhandled, the unit sat "in-flight" for 8.6h on
            # 2026-08-09 (src/lib.rs, reverted 13:30 UTC, shown until the
            # next tdd.iter cleared it). No counter moves: the file's fns
            # are re-dispatched by a later start or by the per-fn pass,
            # which is where their outcome gets counted.
            in_flight_units.pop(f"file {d.get('file') or '?'}", None)
        elif ev == "phase_c.impl.fill.done":
            _lbl = d.get('container') or '?'
            _members = int(d.get("members_filled", 0)) or 1
            out["fills_done"] += _members
            out["pass_fills_done"] = out.get("pass_fills_done", 0) + _members
            out["fills_first"] += _members
            out["recent_fills"].append(
                (_lbl, 0, d.get("fix_attempts", 1)))
            in_flight_units.pop(_lbl, None)
        elif ev == "phase_c.impl.fill.fail":
            _lbl = d.get('container') or '?'
            out["fills_failed"] += 1
            out["pass_fills_failed"] = out.get("pass_fills_failed", 0) + 1
            in_flight_units.pop(_lbl, None)
            _record_error_codes(out, d)
        elif ev == "phase_c.impl.fill.already_complete":
            # Terminal without done/fail: the block had no stubs left to fill.
            # Unhandled, the unit reads as in-flight until the next tdd.iter
            # (5.6h for `impl From<Error> for Box<...>` on 2026-08-09).
            in_flight_units.pop(d.get('container') or '?', None)
        elif ev == "phase_c.fill.progress":
            # Cumulative fills-landed for the cluster — the mid-sweep progress
            # signal (pass= only re-measures per multi-hour sweep). Last value
            # wins; monotonic within a cluster.
            out["fills_landed"] = d.get("fills_landed", out.get("fills_landed", 0))
        elif ev == "phase_c.note.added":
            out["notes"] += 1
        elif ev == "phase_c.pass.done":
            out["stubs_remaining"] = d.get("remaining", out["stubs_remaining"])
        elif ev == "phase_c.pass.test":
            out["last_test_rc"] = d.get("rc")
            out["last_test_failed"] = len(d.get("failed_tests") or [])
        elif ev == "phase_c.tdd.iter":
            out["driver"] = "tdd"
            out["tdd_max_stalls"] = d.get("max_stalls", out["tdd_max_stalls"])
            _tcl = d.get("cluster") or out["cluster"]
            out["cluster"] = _tcl or out["cluster"]
            out["tdd_iter"] = d.get("iter", out["tdd_iter"])
            if e.get("ts"):
                out["tdd_iter_ts"].append(e["ts"])
            # Fresh iter → drop any in-flight units left dangling from the
            # prior iter's sweep, and reset the per-sweep fill counters and
            # their batch denominator here rather than trusting a pass.start
            # to arrive and do it. The tdd driver does NOT emit one at the
            # head of every iter, which is what this comment used to assume:
            # the per-fn engine emits pass.start only when it has something to
            # fill, so an iteration whose frontier is all behavioural re-fills
            # emits none at all (run 500301-6a66bf86, 2026-08-15, iter 1: a
            # 61-fn frontier, nine file units at `0 stub(s)` each, zero
            # pass.start). Without this reset those counters run cumulative
            # across every such iteration while the frontier they are rendered
            # against is per-iteration, so the sweep bar would read further
            # past its own batch on each one. The cluster-scope snapshot
            # (filled_count / all_stubs_remaining) is deliberately NOT reset —
            # it is a slow whole-cluster reading, and last iteration's is worth
            # more than none.
            in_flight_units.clear()
            out["pass_fills_done"] = 0
            out["pass_fills_failed"] = 0
            out["pass_stubs_total"] = 0
            _pc = d.get("pass_count")
            if _pc is not None:
                out["tdd_pass"] = _pc
                out["tdd_pass_hist"].append(_pc)
            out["tdd_fail"] = d.get("fail_count", out["tdd_fail"])
            out["tdd_frontier"] = d.get("frontier_size", out["tdd_frontier"])
            out["tdd_stall"] = d.get("stall", out["tdd_stall"])
            out["tdd_kind"] = d.get("kind", out["tdd_kind"])
            out["tdd_oracle"] = d.get("oracle_size", out["tdd_oracle"])
            out["tdd_oracle_static"] = d.get("oracle_static", out["tdd_oracle_static"])
            # Replaced wholesale, never merged: each of the three describes
            # THIS sweep's parse, and a stale pair vector beside a fresh
            # oracle would decompose the gap with the wrong binaries.
            out["tdd_incomplete_binaries"] = list(d.get("incomplete") or [])
            out["tdd_passed_pairs"] = list(d.get("passed_pairs") or [])
            out["tdd_failed_pairs"] = list(d.get("failed_pairs") or [])
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

    out["fill"] = fold_fill(events)
    _now = time.time()
    _flight: list[dict[str, Any]] = []
    for _lbl, _u in in_flight_units.items():
        _age = None
        if _u.get("start_iso"):
            try:
                _st = datetime.fromisoformat(
                    _u["start_iso"].replace("Z", "+00:00")).timestamp()
                _age = int(_now - _st)
            except Exception:
                _age = None
        _flight.append({"label": _lbl, "kind": _u["kind"],
                        "refill": _u.get("refill", False),
                        "age_s": _age, "file": _u.get("file"),
                        "thread": _u.get("thread")})
    # Longest-running first — that's the block eating the iter.
    _flight.sort(key=lambda x: (x["age_s"] is None, -(x["age_s"] or 0)))
    out["in_flight"] = _flight
    if _flight:
        out["current_fn"] = _flight[0]["label"]
        out["current_attempt"] = fn_attempts.get(_flight[0]["label"], 0)
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


def cut_rate_reading(knobs: CutStopKnobs) -> CutRate:
    """Replay this run's `llm.stream_meta` rows through the same fold the
    live process's LLM_CUT_STOP arms (`reasoning_cap.CutRate`,
    `_observe_health` in `reasoning_cap.py`) — the monitor's gauge and the
    pipeline's own stop are then the same computation over the same rows,
    not a re-derived rate that can read differently from what actually
    fires the stop.

    Rows tagged `bookkeeping` (the self-note extractor's empty-then-retry
    ladder, `adapters/model_notes_store.py`) are skipped, exactly as
    `record_stream_meta` skips them live — via the flag the controller now
    stamps on the event, not a caller-site guess reconstructed after the
    fact. `followup` rows (`llm.followup_meta`) are a different event name
    and are never read here: `_observe_health` (the live fold this mirrors)
    is only ever called from `record_stream_meta`, never
    `record_followup_meta`.

    `rate.seen` undercounts a long run's true call count: `read_recent_events`
    only tails the last 8 MB of events.jsonl, while the live process's `seen`
    is cumulative since cluster entry. Harmless for `min_calls` gating
    (default 25, reached within a handful of KB) and irrelevant to `share`,
    which is computed over `marks` (the last `window` calls) either way."""
    rate = CutRate(knobs.window, knobs.share_pct, knobs.min_calls)
    for ev in read_recent_events(lambda n: n == "llm.stream_meta"):
        data = ev.get("data") or {}
        if data.get("bookkeeping"):
            continue
        rate = rate.add(mark_of(data, ""))
    return rate


def latest_flow_audit() -> dict[str, Any] | None:
    """Most recent flow_audit.report payload (pipeline --audit), or None."""
    if not (evs := read_recent_events(lambda n: n == "flow_audit.report")):
        return None
    data = evs[-1].get("data")
    return data if isinstance(data, dict) else None
