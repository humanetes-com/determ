#!/usr/bin/env python3
"""Pipeline / GPU monitor.  Keys: 1/2/3 views  c/g/w/s/p/a actions  ↑↓ navigate  q quit"""
import contextlib
import json as _json
import os
import queue
import signal
import subprocess
import sys
import termios
import time
import tty
from typing import Any

_LOG_DIR     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
REFRESH      = 2


def _discover_running_runs() -> list[dict]:
    """Read the pipeline registry and return entries whose PIDs are still alive."""
    try:
        with open(_rp.run_registry_path()) as fh:
            reg = _json.load(fh)
        if not isinstance(reg, list):
            return []
    except Exception:
        return []
    live: list[dict] = []
    for r in reg:
        pid = r.get("pid")
        if pid and os.path.exists(f"/proc/{pid}"):
            live.append(r)
    return live


def _pick_run(runs: list[dict]) -> dict | None:
    """Auto-pick when only one run is live; otherwise prompt the user."""
    if not runs:
        return None
    if len(runs) == 1:
        return runs[0]
    print("Multiple pipelines running:")
    for i, r in enumerate(runs):
        argv = " ".join(r.get("argv", []) or [])
        log = r.get("stdout_log") or r.get("pipeline_log") or "?"
        age = int(time.time() - r.get("started_at", 0))
        print(f"  [{i}] pid={r.get('pid')} project={r.get('project','?')} "
              f"age={age}s log={log}\n      argv={argv}")
    try:
        choice = input("Select index (default 0): ").strip() or "0"
        idx = int(choice)
        return runs[idx] if 0 <= idx < len(runs) else runs[0]
    except (EOFError, ValueError, KeyboardInterrupt):
        return runs[0]


def _pick_log(run: dict | None, key: str, default_path: str) -> str:
    """Prefer the registered run's log; fall back to default."""
    if run:
        v = run.get(key)
        if isinstance(v, str) and os.path.exists(v):
            return v
        # stdout_log is special: when present it's the live combined log.
        stdout_log = run.get("stdout_log")
        if (key == "pipeline_log" and isinstance(stdout_log, str)
                and os.path.exists(stdout_log)):
            return stdout_log
    return default_path


# Discovery order: explicit env var → registry → default. Env wins so users
# can still force a specific file when they want to.
_DISCOVERED = None if os.getenv("PIPELINE_LOG") else _pick_run(_discover_running_runs())

PIPELINE_LOG = os.getenv("PIPELINE_LOG", _pick_log(
    _DISCOVERED, "pipeline_log", os.path.join(_LOG_DIR, "pipeline_anyhow.log")))
# When a live run has a stdout_log, that's the actual live output.
if _DISCOVERED and not os.getenv("PIPELINE_LOG"):
    _stdout = _DISCOVERED.get("stdout_log")
    if _stdout and os.path.exists(_stdout):
        PIPELINE_LOG = _stdout
STREAM_LOG   = os.getenv("LLM_STREAM_LOG", os.getenv("OLLAMA_STREAM_LOG", _pick_log(
    _DISCOVERED, "stream_log", os.path.join(_LOG_DIR, "stream.log"))))
EVENTS_LOG   = os.getenv("PIPELINE_EVENTS_LOG", _pick_log(
    _DISCOVERED, "events_log", os.path.join(_LOG_DIR, "events.jsonl")))

# Data-vs-render split: the events.jsonl readers live in the data module; this
# file keeps the renderer + live loop. `_`-aliases preserve the in-file call sites.
# (Tests point the readers via pipeline_core.monitor.events_data.configure / globals.)
from pipeline_core.domain import graceful_stop  # noqa: E402
from pipeline_core.domain import runtime_paths as _rp  # noqa: E402
from pipeline_core.monitor import bottom_bar as _bb  # noqa: E402
from pipeline_core.monitor import events_data as _ed  # noqa: E402
from pipeline_core.monitor import exit_record as _exit  # noqa: E402
from pipeline_core.monitor import iteration_steps as _steps  # noqa: E402
from pipeline_core.monitor import phase_panel as _pp  # noqa: E402
from pipeline_core.monitor import phase_view as _pv  # noqa: E402
from pipeline_core.monitor import run_data as _rd  # noqa: E402
from pipeline_core.monitor.engine_metrics import (  # noqa: E402
    engine_rows as _engine_rows,
)
from pipeline_core.monitor.field_glossary import (  # noqa: E402
    field_glossary_window as _field_glossary_window,
)
from pipeline_core.monitor.files_overview import (  # noqa: E402
    files_overview_rows as _files_overview_rows,
)
from pipeline_core.monitor.frame import (  # noqa: E402
    SplitLayout,
    box,
    content_width,
    scroll_box,
    side_by_side,
    tail_lines,
    wrap_visible,
)
from pipeline_core.monitor.generation_panel import (  # noqa: E402
    generation_rows as _generation_rows,
)
from pipeline_core.monitor.health_view import health_lines as _health_lines  # noqa: E402
from pipeline_core.monitor.health_view import health_window as _health_window  # noqa: E402
from pipeline_core.monitor.key_input import REPORTING_OFF as _REPORTING_OFF  # noqa: E402
from pipeline_core.monitor.key_input import KeyReader as _KeyReader  # noqa: E402
from pipeline_core.monitor.legend import legend_window as _legend_window  # noqa: E402
from pipeline_core.monitor.render import (
    bg_white,
    bold,
    cyan,
    dim,
    green,
    red,
    yellow,
)
from pipeline_core.monitor.render import (
    driver_counter as _driver_counter,
)
from pipeline_core.monitor.render import (
    event_format as _event_format,
)
from pipeline_core.monitor.render import (
    fill_rows as _fill_rows,
)
from pipeline_core.monitor.render import (
    in_flight_row as _in_flight_row,
)
from pipeline_core.monitor.render import (
    iteration_progress_rows as _iteration_progress_rows,
)
from pipeline_core.monitor.render import (
    iteration_step_row as _iteration_step_row,
)
from pipeline_core.monitor.render import (
    loop_hierarchy_rows as _loop_hierarchy_rows,
)
from pipeline_core.monitor.render import (
    oracle_row as _oracle_row,
)
from pipeline_core.monitor.render import (
    plain as _plain,
)
from pipeline_core.monitor.render import (
    progress_bar as _bar,
)
from pipeline_core.monitor.render import (
    schedule_row as _schedule_row,
)
from pipeline_core.monitor.render import (
    score_row as _score_row,
)
from pipeline_core.monitor.schedule_health import (  # noqa: E402
    cached_schedule_health as _schedule_health,
)
from pipeline_core.monitor.schedule_health import (  # noqa: E402
    schedule_health_line as _schedule_health_line,
)
from pipeline_core.monitor.vllm_status import (  # noqa: E402
    vllm_status_rows as _vllm_status,
)
from pipeline_core.monitor.worker_streams import (  # noqa: E402
    worker_rows as _worker_rows,
)
from pipeline_core.monitor.worker_streams import (
    worker_snapshots as _worker_snapshots,
)

_ed.configure(events_log=EVENTS_LOG)
_pipeline_start_time = _ed.pipeline_start_time
_read_recent_events = _ed.read_recent_events
_coverage_from_events = _ed.coverage_from_events
_phase_c_state = _ed.phase_c_state
_latest_compile_file_stats = _ed.latest_compile_file_stats
_latest_test_file_stats = _ed.latest_test_file_stats
_latest_flow_audit = _ed.latest_flow_audit
_cut_rate_reading = _ed.cut_rate_reading

_rd.configure(script_dir=os.path.dirname(os.path.abspath(__file__)),
              pipeline_log=PIPELINE_LOG)
_coverage_reading = _ed.coverage_reading
_baked_cluster_order = _rd.baked_cluster_order
_find_project_dir = _rd.find_project_dir
_parse_pipeline_state = _rd.parse_pipeline_state
_current_stage_label = _rd.current_stage_label
_task_progression = _ed.task_progression       # structured-event sources
_pipeline_log_facts = _ed.pipeline_log_facts
_vllm_runtime = _ed.vllm_runtime
_vllm_budget_by_thread = _ed.vllm_budget_by_thread
_log_tail_meta = _rd.log_tail_meta
_read_backend = _rd.read_backend
_pipeline_running = _rd.pipeline_running
_detect_cached_repo_url = _rd.detect_cached_repo_url
_read_cut_stop_knobs = _rd.read_cut_stop_knobs

_GPU_BW = {"3090": 936.2, "3080": 760.0, "4090": 1008.0, "4080": 716.8,
           "A100": 2000.0, "H100": 3350.0}

# ── Terminal size ─────────────────────────────────────────────────────────────

def _term_w():
    try: return os.get_terminal_size().columns
    except OSError: return 80

def _h():
    try: return os.get_terminal_size().lines
    except OSError: return 24

# ── Flicker-free diff renderer ────────────────────────────────────────────────

_buf: list[str] = []

def _ln(s=""):
    _buf.append(s)

def _flush_frame():
    # Repaint in place: home the cursor, write every visible line clearing to
    # end-of-line, then clear from the cursor to end-of-screen. Overwriting in
    # place means no flicker, and clearing per-line + below means a shorter or
    # shifted frame never leaves stale/duplicated rows behind (a line-diff by
    # index does — when content shifts it can skip a row it thinks is unchanged).
    h = _h()
    # Join with newlines but emit none after the last row — a trailing newline
    # on the terminal's bottom row scrolls the whole screen up.
    body = "\n".join(f"{line}\033[K" for line in _buf[:h])
    sys.stdout.write(f"\033[H{body}\033[J")
    sys.stdout.flush()
    _buf.clear()

# ── Key reading (handles arrow escape sequences) ──────────────────────────────
#
# Threaded off the render loop — see pipeline_core/monitor/key_input.py for why.

_key_reader = _KeyReader()

# ── Data collectors ───────────────────────────────────────────────────────────

def gpu():
    try:
        raw = subprocess.check_output(
            ["nvidia-smi",
             "--query-gpu=index,name,utilization.gpu,utilization.memory,"
             "memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            text=True, stderr=subprocess.DEVNULL, timeout=3,
        ).strip().splitlines()
    except Exception as e:
        return [red(f"nvidia-smi failed: {e}")]

    rows = []
    bw_sum = mem_gb = 0.0
    for line in raw:
        idx, name, util, mem_bw, used, total, temp = [p.strip() for p in line.split(",", 6)]
        ui, mi = int(util), int(mem_bw)
        col  = green if ui > 60 else yellow if ui > 20 else dim
        mcol = green if mi > 60 else yellow if mi > 20 else dim
        rows.append(
            f"GPU{idx}  [{col(_bar(ui))}] {ui:3}%  "
            f"MEM-BW {mcol(f'{mi:3}%')}  "
            f"VRAM {int(used)/1024:.1f}/{int(total)/1024:.1f} GB  Temp {temp}°C"
        )
        card_bw = next((v for k, v in _GPU_BW.items() if k in name), None)
        if card_bw:
            bw_sum += card_bw * mi / 100
    if bw_sum > 0 and mem_gb > 0:
        rows.append(f"  {bold('≈ tokens/s:')} {bw_sum/mem_gb:.1f}")
    return rows


def _docs_richness_row() -> str:
    """One-line latest doc-richness vs convergence (docs.richness event).

    Returns "" when this run has emitted no docs.richness yet.
    """
    evs = _read_recent_events(lambda n: n == "docs.richness")
    if not evs:
        return ""
    d = evs[-1].get("data") or {}
    arrow = (green("▲") if d.get("improved")
             else dim("="))
    return (f"  docs  fr={d.get('fr_chars', 0)}c  notes={d.get('notes', 0)}  "
            f"rules={d.get('genesis_rules', 0)}  "
            f"pass={d.get('prev_pass_count', 0)}→{d.get('pass_count', 0)} {arrow}")


_ERR_NAMES = {
    "E0277": "trait bound", "E0599": "no method", "E0046": "missing trait items",
    "E0053": "sig mismatch", "E0308": "type mismatch", "E0425": "name not found",
    "E0609": "no field", "E0201": "dup impl", "E0119": "conflicting impl",
    "E0433": "bad path", "E0282": "need annotation", "E0432": "unresolved import",
}


def _phase_c_panel_rows(state: dict[str, Any], width: int) -> list[str]:
    """Render the Phase C panel rows. Empty list when no Phase C activity."""
    if not state.get("active"):
        return []
    rows: list[str] = []
    cluster = state.get("cluster") or "?"
    age = state.get("last_event_age_s")
    age_s = (f"{age}s" if age is not None and age < 60
             else f"{age // 60}m" if age is not None else "?")
    rows.append(f"  reconstruct  cluster={cluster}  {_driver_counter(state)}  "
                f"last={age_s}")

    # One level below the `phase` strip at the top: that one stops at t2a,
    # and t2a is a multi-hour loop whose five steps the panel could not name.
    # Directly under the reconstruct header because it answers the same
    # question that header opens — where in the loop are we.
    _step = _iteration_step_row(state, time.time())
    if _step:
        rows.append(_step)

    # And one level below THAT: which fill route, and which body in it.
    rows.extend(_fill_rows(state, time.time()))

    # Per-cluster overview — where we are across all clusters. Prefer the
    # authoritative baked order/total from the log; fall back to events-seen.
    _seen = _baked_cluster_order() or (state.get("clusters_seen") or [])
    if _seen:
        _done = set(state.get("clusters_done") or [])
        _marks = []
        for c in _seen:
            _short = c.replace("anyhow_", "")
            if c in _done:
                _marks.append(green("✓" + _short))
            elif c == state.get("cluster"):
                _marks.append(cyan("▶" + _short))
            else:
                _marks.append(dim("·" + _short))
        rows.append(f"           clusters {len(_done)}/{len(_seen)} done   "
                    + "  ".join(_marks))

    # TDD trajectory — the headline "are we converging" overview: passing /
    # failing tests over iterations, the active frontier, and stall budget.
    _by_cl = state.get("tdd_by_cluster") or {}
    if _by_cl:
        rows.append("           tdd      "
                    + dim("per-cluster passes (each cluster resets its own floor):"))
        _order = [c for c in (_baked_cluster_order() or list(_by_cl))
                  if c in _by_cl]
        _done = set(state.get("clusters_done") or [])
        _active = state.get("cluster")
        for _cn in _order:
            _cs = _by_cl[_cn]
            _p, _f = _cs.get("pass"), _cs.get("fail")
            _h = _cs.get("hist") or []
            _tr = ""
            if len(_h) >= 2:
                _dd = _h[-1] - _h[-2]
                _tr = (green(f" ↑{_dd}") if _dd > 0
                       else red(f" ↓{-_dd}") if _dd < 0 else dim(" ="))
            _mark = (cyan("▶") if _cn == _active
                     else green("✓") if _cn in _done else dim("·"))
            _short = _cn.replace("anyhow_", "")[:14].ljust(14)
            _line = (f"             {_mark} {_short} {green(f'pass={_p}')}{_tr}  "
                     f"{red(f'fail={_f}')}  iter={_cs.get('iter')}  "
                     f"stall={_cs.get('stall')}")
            if len(_h) >= 2:
                _line += "  " + dim("→ ".join(str(x) for x in _h[-8:]))
            rows.append(_line)

    # Current error class — what the model is fighting right now.
    _ec = state.get("last_error_code")
    if _ec:
        _codes = state.get("err_codes") or {}
        _top = ", ".join(f"{k}×{v}" for k, v in
                         sorted(_codes.items(), key=lambda x: -x[1])[:4])
        rows.append(f"           err      {yellow(_ec)} "
                    f"{dim('(' + _ERR_NAMES.get(_ec, '?') + ')')}   {dim('top: ' + _top)}")

    # The console rows, ordered by how fast each one moves: the score is the
    # target and steps once per sweep; the iteration/sweep rows move every few
    # minutes as fill units land; the engine and worker rows move on every
    # refresh. A panel whose slowest reading is its only reading cannot tell a
    # working run from a hung one, which is what `69/69` for 46 minutes meant on
    # run 669347-cf33eae3.
    _score = _score_row(state)
    if _score:
        rows.append(_score)
    # Beneath the score, and only when the sweep counted fewer tests than the
    # cluster declares: how big the shortfall is and what it is made of.
    _oracle = _oracle_row(state)
    if _oracle:
        rows.append(_oracle)
    # What the fill schedule has bought this run (cashed / spent minutes,
    # re-buys, delayed payers, the dry streak); red `BURN` = GPU spent with
    # no verdict moving.
    _sched = _schedule_row(_schedule_health_line(_schedule_health(EVENTS_LOG)))
    if _sched:
        rows.append(_sched)
    # Intra-iteration progress (render.iteration_progress_rows): pace bar
    # (elapsed vs median iteration length — moves every refresh), fills
    # counter with last-fill age, frontier context; plus a `bodies` bar only
    # while the initial fill is still climbing. The pass count itself is NOT
    # repeated here — the tdd row above already carries it with its history.
    _now = time.time()
    rows.extend(_iteration_progress_rows(state, _now))
    rows.extend(_engine_rows(_now))
    _vr = _vllm_runtime()
    _flight = state.get("in_flight") or []
    _snaps = _worker_snapshots(base_log=STREAM_LOG, now=_now)
    rows.extend(_worker_rows(
        _snaps, units_by_thread={u["thread"]: u for u in _flight if u.get("thread")},
        cpt=_vr.get("cpt"), request_max=_vr.get("max_tokens"),
        budget_by_thread=_vllm_budget_by_thread(), width=content_width(width)))
    _covered = {s["thread"] for s in _snaps}

    first = state.get("fills_first", 0)
    rec = state.get("fills_recovered", 0)
    failed = state.get("fills_failed", 0)
    skip = state.get("fills_skipped", 0)
    total_done = first + rec
    notes = state.get("notes", 0)
    rows.append(f"           fills  "
                f"{green(f'✓ first={first}')}  "
                f"{yellow(f'↻ recovered={rec}')}  "
                f"{red(f'✗ failed={failed}')}  "
                f"{dim(f'⊘ skip={skip}')}  "
                f"{dim(f'(done={total_done})')}  {dim(f'notes={notes}')}")
    _if = _in_flight_row(state, _covered)
    if _if:
        rows.append(_if)
    hot = state.get("hot_fn")
    if hot:
        rows.append(f"           hot fn   {dim(hot[0])} ({hot[1]} attempts)")
    recent = state.get("recent_fills") or []
    if recent:
        # First line, bounded: an impl-block label is the container's whole
        # signature, and a generic one carries its `where` clause across three
        # lines — pasted in raw they broke the box open on run 669347-cf33eae3
        # (`impl<A, B> BothDebug for (A, B)\nwhere\n    A: Debug,`).
        bits = "  ".join(f"{fn.splitlines()[0][:32]}:{body}c×{a}"
                         for fn, body, a in recent)
        rows.append(f"           recent   {dim(bits)}")
    test_rc = state.get("last_test_rc")
    if test_rc is not None:
        if test_rc == 0:
            rows.append(f"           test     {green('PASSED')}")
        else:
            tf = state.get("last_test_failed") or 0
            rows.append(f"           test     {red(f'rc={test_rc} failed={tf}')}")
    return rows


def _flow_audit_rows(data: dict[str, Any]) -> list[str]:
    """Compact verdict row(s): `audit: C1✓ C2! C3✓ …` + first FAIL evidence."""
    icons = {"PASS": green("✓"), "FAIL": red("✗"), "WARN": yellow("!"), "SKIP": dim("·")}
    cells, first_fail = [], ""
    for v in data.get("verdicts", []):
        tag = v.get("check", "?").split(" ")[0]
        cells.append(f"{tag}{icons.get(v.get('status'), '?')}")
        if not first_fail and v.get("status") == "FAIL":
            first_fail = v.get("evidence", "")
    rows = [f"  audit: {'  '.join(cells)}  ({data.get('context', '')})"]
    if first_fail:
        rows.append(red(f"    ✗ {first_fail[:100]}"))
    return rows


def pipeline_status(width: int):
    prog  = _pipeline_log_facts()       # task0c progress, read from structured events
    prog.update(_log_tail_meta())       # + raw-log presentation (last_line, age)
    state = _parse_pipeline_state(_find_project_dir())
    running   = _pipeline_running()
    status_str = green("RUNNING") if running else red("STOPPED")
    w = content_width(width)

    if not os.path.exists(PIPELINE_LOG):
        return [f"{status_str}   {red('log not found')}"]

    # Coverage: the structured coverage.measured event is authoritative;
    # the pipeline_state.md / log-text scrapes remain only for legacy logs.
    cov = _coverage_from_events() or state["coverage"] or "?"
    try:    cov_col = green if float(cov.rstrip("%")) >= 80 else yellow
    except: cov_col = dim

    age   = prog["age"]
    age_s = f"{int(age)}s" if age < 60 else f"{int(age/60)}m{int(age%60)}s"

    clusters = state["clusters"]
    done_set = state["done"]
    cur_cl   = state["current"] or prog["current_cluster"]

    rows = []

    # vLLM backend status (loading indicator / docker logs when not ready)
    rows.extend(_vllm_status(w))

    # The whole-run phase strip, the active phase's sub-steps, and the book
    # bar while that step is open. Drawn from the phase registry and this
    # run's phase events (`phase.enter` / `exit` / `progress`).
    rows.extend(_pv.strip_rows(_pp.load_board(EVENTS_LOG, _find_project_dir(), running), w, time.time()))

    # ── Header: status + coverage ────────────────────────────────────────────
    _cov_reading = _coverage_reading()   # this run's events only, never a file on disk
    _cov_data = _cov_reading.files
    _total_cov  = sum(c for c, _ in _cov_data.values())
    _total_cvbl = sum(v for _, v in _cov_data.values())
    _lines_str  = (f"  files {len(_cov_data)}/{_cov_reading.file_count}" if _cov_reading.blind
                   else f"  {_total_cov}/{_total_cvbl}" if _total_cvbl else "")
    # Projection: the latest whole-crate reading (a quick check measures every
    # file) when it is above the committed value. A blind map is a subset of
    # the crate, so it says `files n/m` above and projects nothing.
    _proj_str = ""
    if _total_cvbl > 0 and not _cov_reading.blind:
        proj_pct = _total_cov / _total_cvbl * 100.0
        try:
            cur_pct = float(cov.rstrip("%"))
        except (ValueError, AttributeError):
            cur_pct = -1.0
        if proj_pct - cur_pct >= 0.05:   # only show if a meaningful uplift
            proj_col = green if proj_pct >= 80 else yellow
            _proj_str = dim("(→ ") + proj_col(f"{proj_pct:.1f}%") + dim(")")
    rows.append(
        f"{status_str}  cov={cov_col(bold(cov))}{_proj_str}{dim(_lines_str)}  "
        f"age={age_s}  errors={prog['errors']}"
    )

    # (The old log-parsed "phases" line was removed — the state-driven
    # the phase strip above is the single, granular phase display. tp is still
    # parsed below for the fix-phase detection further down.)
    # Task progression is now derived purely from structured events.
    tp = _task_progression()

    # ── Phase C per-fn panel (visible when the skeleton backend
    # is running). Pulls structured data from logs/events.jsonl.
    # Bound before the try: the loop-hierarchy rows below read it too, and a
    # panel error must not turn into a NameError that blanks the whole screen.
    _pc: dict[str, Any] = {}
    try:
        _pc = _phase_c_state()
        # The sweep is the one iteration step that publishes no event — cargo
        # is running — so the loop's own `iter N: running cargo test...` line,
        # while it is the log's LAST line, is what says so, and the log's
        # staleness is then the time spent in the step. Applied here rather
        # than in the fold because only this layer reads the raw log.
        _lstep = _steps.step_of_log_line(prog.get("last_line") or "")
        if _lstep:
            _pc["tdd_step"] = _lstep
            _pc["tdd_step_since"] = None
            _pc["tdd_step_age_s"] = prog.get("age")
        for r in _phase_c_panel_rows(_pc, width):
            rows.append(r)
    except Exception as _e:
        rows.append(dim(f"  phase-c  (panel error: {_e})"))

    _dr = _docs_richness_row()
    if _dr:
        rows.append(_dr)

    rows.extend(_loop_hierarchy_rows(
        clusters=clusters, done_set=done_set, cur_cl=cur_cl,
        prog=prog, phase_c=_pc))

    # Row 4: uncovered-lines history for last 4 files  (e.g. error.rs:92→ 72→ 45)
    hist = prog["file_history"]
    if hist:
        parts = []
        for fname, counts in list(hist.items())[-4:]:
            short = fname.replace("src/", "")
            trend = "→ ".join(str(c) for c in counts[-4:])
            entry = f"{short}:{trend}"
            parts.append(green(entry) if counts and counts[-1] == 0 else entry)
        hist_row = "  " + "  ".join(parts)
        if len(_plain(hist_row)) > w:
            parts = []
            for fname, counts in list(hist.items())[-3:]:
                short = fname.replace("src/", "")[:8]
                trend = "→ ".join(str(c) for c in counts[-3:])
                entry = f"{short}:{trend}"
                parts.append(green(entry) if counts and counts[-1] == 0 else entry)
            hist_row = "  " + "  ".join(parts)
        rows.append(hist_row)

    # Row 5: last log line
    last = prog["last_line"]
    if last:
        rows.append(dim((last[:w] + "…") if len(last) > w else last))

    # ── Files overview ──────────────────────────────────────────────────────

    # Flow self-audit verdicts (pipeline --audit) — shown whenever present.
    _audit_data = _latest_flow_audit()
    if _audit_data:
        rows.extend(_flow_audit_rows(_audit_data))

    # Task2/fix-loop: show per-file compile error stats instead of coverage.
    _compile_stats = _latest_compile_file_stats()
    _test_stats = _latest_test_file_stats()
    _in_fix_phase = (tp["states"].get("task2") == "in_progress"
                     or tp["states"].get("task3") == "in_progress")

    def _tests_marker(file_stats: dict | None) -> str:
        """`tests 3/5` cell for one file's tests.file_stats row; "" if no data."""
        if not file_stats:
            return ""
        p, f = file_stats.get("passed", 0), file_stats.get("failed", 0)
        if p + f == 0:
            return ""
        cell = f"tests {p}/{p + f}"
        colored: str = green(cell) if f == 0 else red(cell)
        return colored

    _tmap = {fd["file"]: fd for fd in (_test_stats or {}).get("files", [])}
    if _compile_stats and _in_fix_phase:
        files = _compile_stats.get("files", [])
        clean = _compile_stats.get("clean", 0)
        total = _compile_stats.get("total", 0)
        attempt_n = _compile_stats.get("attempt", "?")
        rows.append(dim(f"  {'─' * max(4, min(w - 2, 60))}"))
        hdr = f"  compile overview — {green(str(clean))}/{total} clean  (attempt {attempt_n})"
        if _test_stats:
            tp_, tt_ = _test_stats.get("passed", 0), _test_stats.get("total", 0)
            hdr += f"  tests {(green if tp_ == tt_ else red)(f'{tp_}/{tt_}')} passing"
        rows.append(hdr)
        max_name = max((len(f["file"].split("/")[-1]) for f in files), default=10)
        shown = set()
        for fd in sorted(files, key=lambda f: (-f["errors"], f["file"])):
            short = fd["file"].split("/")[-1]
            errs  = fd["errors"]
            lines = fd["lines"]
            fns   = fd["fns"]
            shown.add(fd["file"])
            marker = green("✓ clean") if errs == 0 else red("█" * min(errs, 6)) + f" {errs}"
            tcell = _tests_marker(_tmap.get(fd["file"])) if errs == 0 else ""
            rows.append(f"  {short.ljust(max_name)}  {str(lines).rjust(4)}L  {str(fns).rjust(3)}fn  {marker}  {tcell}".rstrip())
        # Test-bearing files outside the compile table (tests/*.rs, doc-tests).
        for f in sorted(_tmap.keys() - shown):
            tcell = _tests_marker(_tmap[f])
            if tcell:
                rows.append(f"  {f.split('/')[-1].ljust(max_name)}  {' ' * 11}  {tcell}")
    elif _test_stats and _in_fix_phase:
        # No compile stats yet (e.g. first t3 run passed straight through the
        # fix loop) — show the test overview on its own.
        tp_, tt_ = _test_stats.get("passed", 0), _test_stats.get("total", 0)
        ctx = _test_stats.get("context", "")
        rows.append(dim(f"  {'─' * max(4, min(w - 2, 60))}"))
        rows.append(f"  test overview — {(green if tp_ == tt_ else red)(f'{tp_}/{tt_}')} passing  ({ctx})")
        tfiles = sorted(_tmap.values(), key=lambda r: (-r.get("failed", 0), r["file"]))
        max_name = max((len(r["file"].split("/")[-1]) for r in tfiles), default=10)
        for r in tfiles:
            tcell = _tests_marker(r)
            if tcell:
                rows.append(f"  {r['file'].split('/')[-1].ljust(max_name)}  {tcell}")

    cov_data = _cov_data
    if cov_data and not ((_compile_stats or _test_stats) and _in_fix_phase):
        rows.extend(_files_overview_rows(prog, cov_data, cur_cl, w))

    return rows


# The think-phase bar's denominator. Not a server limit — a display ceiling for
# a phase with no declared budget, kept here because it is a presentation
# choice; the rest of the box's state (the char counter, the ETA smoothing)
# moved into the panel module with the code that reads it.
_THINK_CAP_CHARS = 40_000


def generation(width: int, fill_rows: int | None = None):
    """The Generation box — see pipeline_core.monitor.generation_panel.

    It follows the freshest stream file rather than the base one: under the
    parallel fill engine every token goes to a per-thread sibling and the base
    file is the idle main thread's.

    The LLM_CUT_STOP gauge is fetched here (alongside the vLLM runtime facts)
    and handed to the panel rather than rendered in `pipeline_status` — it
    belongs beside the live call it narrates (phase=, cpt=, elapsed), not
    beside the coverage/score gauges, which is where it sat before it moved.
    """
    _cut_knobs = _read_cut_stop_knobs()
    return _generation_rows(
        stream_log=STREAM_LOG, width=width, now=time.time(),
        runtime=_vllm_runtime(), refresh=REFRESH,
        think_cap_chars=_THINK_CAP_CHARS, fill_rows=fill_rows,
        cut_rate=_cut_rate_reading(_cut_knobs), cut_knobs=_cut_knobs)

# ── State ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
_action_msg  = ""

def _detach(cmd):
    """Launch cmd fully detached from the terminal — no stdin/stdout, new session."""
    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,   # setsid() — detach from controlling terminal
    )
_view        = "monitor"      # monitor|pipeline|stream|events|legend|health|fields|phases
_phases = _pp.PhasePanel()    # the [e] view's scroll and expand state
# Scrolled this far up, a view's window clamps to its first row: the health
# view opens on its findings, not on the end of the phase-cost table.
HEALTH_TOP = 1_000_000
_scroll      = {"pipeline": 0, "stream": 0, "events": 0, "legend": 0, "health": HEALTH_TOP, "fields": 0}   # lines scrolled up from bottom (0=bottom)
# Split layout (toggle: [v], or MONITOR_SPLIT=1 at start): [1]Monitor stays in
# a narrow left column and the selected detail view renders in the wide right
# one; MONITOR_SPLIT_WIDTH sets the left width (0 = the cap), [<]/[>] resize
# it. The geometry and its bounds are pipeline_core.monitor.frame.SplitLayout.
_layout      = SplitLayout(enabled=os.getenv("MONITOR_SPLIT", "0") == "1",
                           left=int(os.getenv("MONITOR_SPLIT_WIDTH", "0") or 0))
_last_detail = "stream"       # the detail view the right column shows while _view is monitor
_page        = 1              # rows of the last drawn scroll region: what PgUp/PgDn move
_confirm          = None   # pending confirmation: "continue" | "wipe" | "stop" | "poweroff" | None
_graceful_pending = graceful_stop.sentinel_path().exists()  # survives monitor restart

# Auto-operator toggle: sentinel presence enables stall-triggered operator_run.sh
# passes, mirroring the graceful-stop sentinel's touch/remove convention above.
# The watch-and-fire logic runs inside the PIPELINE process itself (phase_c_tdd's
# TDD loop, via pipeline_core.adapters.auto_operator) so it stays live whether or
# not the monitor is attached; a fresh pipeline start seeds this file from `.env`
# AUTO_DEFAULT. The monitor is just the remote toggle switch + status display.
def _auto_armed() -> bool:
    return _rp.control_path(_rp.AUTOMODE).exists()

def _set_auto_armed(on: bool) -> None:
    if on:
        _rp.control_path(_rp.AUTOMODE).touch()
    else:
        with contextlib.suppress(FileNotFoundError):
            _rp.control_path(_rp.AUTOMODE).unlink()

# Power-off-on-finish toggle: sentinel ("1"/"0") overrides .env SHUTDOWN_ON_FINISH,
# mirroring pipeline._shutdown_on_finish_enabled. Monitor reads it every frame.
def _shutdown_armed() -> bool:
    try:
        with open(_rp.control_path(_rp.SHUTDOWN_ON_FINISH)) as f:
            return f.read().strip() == "1"
    except OSError:
        pass
    try:
        with open(os.path.join(SCRIPT_DIR, ".env")) as f:
            for line in f:
                key, sep, val = line.strip().partition("=")
                if sep and key.strip() == "SHUTDOWN_ON_FINISH":
                    return val.strip() == "1"
    except OSError:
        pass
    return False

def _set_shutdown_armed(on: bool) -> None:
    with open(_rp.control_path(_rp.SHUTDOWN_ON_FINISH), "w") as f:
        f.write("1" if on else "0")
_term_settings = None         # saved terminal settings for cooked-mode URL prompt

# --demo: auto-cycle through views 1→2→3 (Monitor → Pipeline Log → Stream Log)
# on a fixed interval, so a screencast shows every panel without keypresses.
_DEMO          = False
_DEMO_VIEWS    = ["monitor", "pipeline", "stream"]   # views 1, 2, 3
DEMO_INTERVAL  = 5            # seconds shown per view in demo mode

VIEWS = [
    ("monitor",  "1", "Monitor"),
    ("pipeline", "2", "Pipeline Log"),
    ("stream",   "3", "Stream Log"),
    ("events",   "4", "Events"),
]

def _prompt_url() -> str:
    """Temporarily restore cooked terminal mode, read a URL from the user, return it.

    Shows the cached repo URL as a default — pressing Enter accepts it.
    Restores cbreak mode before returning.
    """
    suggest = _detect_cached_repo_url()
    if _term_settings is not None:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, _term_settings)
    sys.stdout.write("\033[?25h")   # show cursor
    h = _h()
    hint = f" [{suggest}]" if suggest else ""
    sys.stdout.write(f"\033[{h};0H\033[2K  ↺ Repo URL{hint}: ")
    sys.stdout.flush()
    try:
        url = sys.stdin.readline().strip()
    except Exception:
        url = ""
    tty.setcbreak(sys.stdin.fileno())
    sys.stdout.write("\033[?25l")   # hide cursor
    sys.stdout.flush()
    return url or suggest


# ── Always-visible bottom bar ─────────────────────────────────────────────────
# 2 lines: action/feedback msg + fixed legend (never mutates — confirm state
# shows only in the msg line above, keys stay the same)

def _bottom_bar() -> list[str]:
    split = _split_widths() is not None
    return _bb.bottom_rows(
        views=VIEWS, shown={"monitor", _pane_view()} if split else {_view},
        running=_pipeline_running(), split=split, split_enabled=_layout.enabled,
        shutdown_armed=_shutdown_armed(), auto_armed=_auto_armed(),
        demo=_DEMO, demo_interval=DEMO_INTERVAL, action_msg=_action_msg)

# ── Views ─────────────────────────────────────────────────────────────────────
#
# Layout (order never changes):
#   Box 1  GPU          — always compact, always present
#   Box 2  Pipeline     — compact in monitor/stream; accordion log in pipeline view
#   Box 3  Generation   — compact in monitor/pipeline; accordion log in stream view
#   ──── action msg ────
#   ──── legend     ────
#
BOTTOM_LINES = 2   # action/feedback msg + fixed legend


def _log_lines(log_path, width: int, tail_bytes: int = 2 * 1024 * 1024):
    """A log file's tail, wrapped to a box of outer `width`."""
    try:
        raw = tail_lines(log_path, tail_bytes)
    except FileNotFoundError:
        return [red(f"  {log_path} not found")]
    except Exception as e:
        return [red(f"  error: {e}")]
    return [row for line in raw for row in wrap_visible(line, content_width(width))]


def _event_lines(events_path: str, width: int, tail_bytes: int = 1024 * 1024) -> list[str]:
    """The tail of events.jsonl, formatted and wrapped to a box of outer `width`."""
    try:
        raw = tail_lines(events_path, tail_bytes)
    except FileNotFoundError:
        return [red(f"  {events_path} not found")]
    except Exception as e:
        return [red(f"  error: {e}")]
    w = content_width(width)
    out: list[str] = []
    for line in raw:
        if not line.strip():
            continue
        try:
            formatted = _event_format(_json.loads(line))
        except Exception:
            formatted = dim(line)
        out.extend(wrap_visible(formatted, w))
    return out


def _scroll_detail(view, title, lines, width, height, header=()) -> list[str]:
    """A scrolling detail box; stores the clamped offset and the page size the
    scroll keys move by (the box's own scroll region, as last drawn)."""
    global _page
    rows, _scroll[view] = scroll_box(title, lines, width, height,
                                     offset=_scroll[view], header=header)
    _page = max(1, height - len(header))
    return rows


def _pane_view() -> str:
    """The detail view the split layout's right column shows."""
    return _view if _view != "monitor" else _last_detail


def _split_widths() -> tuple[int, int] | None:
    return _layout.widths(_term_w())


def _monitor_column(width, height, gpu_rows, pipeline_rows, gen_rows) -> list[str]:
    """View 1: GPU, Pipeline and Generation."""
    rows = [*box("GPU", gpu_rows, width), *box("Pipeline", pipeline_rows, width),
            *box("Generation", gen_rows, width)]
    return rows[:height] + [""] * (height - len(rows))


def _detail_box(view, width, inner, pipeline_rows, gen_rows) -> list[str]:
    """The one expanding box of a detail view, with `inner` content rows."""
    if view == "pipeline":
        return _scroll_detail("pipeline", "Pipeline Log", _log_lines(PIPELINE_LOG, width),
                              width, inner, pipeline_rows[:1])
    if view == "stream":
        return _scroll_detail("stream", "Stream Log", _log_lines(STREAM_LOG, width),
                              width, inner, gen_rows[:1])
    if view == "events":
        return _scroll_detail("events", "Events (logs/events.jsonl)",
                              _event_lines(EVENTS_LOG, width), width, inner,
                              pipeline_rows[:1])
    global _page
    _page = inner
    cw = content_width(width)
    if view == "health":
        _lines = _health_lines(PIPELINE_LOG, os.path.join(SCRIPT_DIR, "tmp", "anyhow"),
                               EVENTS_LOG, running=_pipeline_running())
        # Clamp so the first scroll key moves the view from where it is drawn.
        _scroll["health"] = min(_scroll["health"], max(0, len(_lines) - inner))
        title, rows = ("Run health + phase cost (60s cache) — tools/run_health.py, "
                       "tools/perf_ledger.py phases",
                       _health_window(_lines, inner, _scroll["health"]))
    elif view == "fields":
        title, rows = ("Field glossary — what each stat/label on screen means",
                       _field_glossary_window(cw, inner, _scroll["fields"]))
    elif view == "phases":
        title, rows = _phases.window(
            _pp.load_board(EVENTS_LOG, _find_project_dir(), _pipeline_running()),
            cw, inner, time.time())
    else:
        title, rows = ("Legend — what each phase performs",
                       _legend_window(cw, inner, _scroll["legend"]))
    return box(title, rows, width)


# Rows of the Pipeline box the stream view shows: pipeline_rows grows with the
# run (files overview, per-cluster overview, ...), gen_rows does not, which is
# why only the stream view's compact Pipeline box needs a cap.
PIPELINE_COMPACT_MAX = 8


def _tabbed_body(width, height, gpu_rows, pipeline_rows, gen_rows) -> list[str]:
    """One column: GPU on top, then the current view's boxes, one of them expanding."""
    if _view == "monitor":
        return _monitor_column(width, height, gpu_rows, pipeline_rows, gen_rows)
    rows = box("GPU", gpu_rows, width)
    if _view == "pipeline":
        compact = box("Generation", gen_rows, width)
        rows += _detail_box("pipeline", width, max(1, height - len(rows) - len(compact) - 2),
                            pipeline_rows, gen_rows) + compact
    elif _view in ("stream", "events"):
        shown = pipeline_rows
        if _view == "stream" and len(pipeline_rows) > PIPELINE_COMPACT_MAX:
            hidden = len(pipeline_rows) - PIPELINE_COMPACT_MAX
            shown = pipeline_rows[:PIPELINE_COMPACT_MAX] + [
                dim(f"  … {hidden} more row(s) — see [1]Monitor")]
        rows += box("Pipeline", shown, width)
        rows += _detail_box(_view, width, max(1, height - len(rows) - 2),
                            pipeline_rows, gen_rows)
    else:
        rows += _detail_box(_view, width, max(1, height - len(rows) - 2),
                            pipeline_rows, gen_rows)
    return rows + [""] * (height - len(rows))


def _frame(total_w: int, h: int) -> list[str]:
    """Every row of one frame at terminal size (total_w, h)."""
    widths = _layout.widths(total_w)
    panel_w = widths[0] if widths else total_w
    gpu_rows = gpu()
    pipeline_rows = pipeline_status(panel_w)
    body_h = h - BOTTOM_LINES
    # The monitor column's Generation box takes whatever height the GPU and
    # Pipeline boxes leave, down to the bottom bar; other layouts keep it compact.
    fill = None
    if widths or _view == "monitor":
        fill = body_h - (len(gpu_rows) + 2) - (len(pipeline_rows) + 2) - 2
    gen_rows = generation(panel_w, fill_rows=fill)
    if widths:
        left_w, right_w = widths
        body = side_by_side(
            _monitor_column(left_w, body_h, gpu_rows, pipeline_rows, gen_rows),
            _detail_box(_pane_view(), right_w, max(1, body_h - 2),
                        pipeline_rows, gen_rows),
            left_w, right_w, body_h)
    else:
        body = _tabbed_body(total_w, body_h, gpu_rows, pipeline_rows, gen_rows)
    return body + _bottom_bar()


def _render_all():
    _buf.extend(_frame(_term_w(), _h()))

# ── Actions ───────────────────────────────────────────────────────────────────

SCROLL_STEP = 5


def _augmentation_tag() -> str | None:
    """Return the single task0c-augmented-* tag in the workspace, or None.

    Returns None for multi-module workspaces (>1 tag) — augmentation restore
    is single-module only in this version.
    """
    try:
        proj = _find_project_dir()
        if not proj or not os.path.isdir(os.path.join(proj, ".git")):
            return None
        out = subprocess.run(
            ["git", "tag", "-l", "task0c-augmented-*"],
            cwd=proj, capture_output=True, text=True, timeout=5,
        ).stdout.strip().splitlines()
        tags = [t for t in out if t]
        if len(tags) == 1:
            return tags[0]
        return None
    except Exception:
        return None


def _run_action(key):
    global _last_detail
    _apply_key(key)
    if _view != "monitor":
        _last_detail = _view


def _apply_key(key):
    global _action_msg, _view, _confirm, _graceful_pending, _layout
    global _DEMO
    if _DEMO and key in ("1", "2", "3", "4", "l", "h", "f", "e"):
        _DEMO = False   # picking a view yourself means you want it to stay
    if _confirm == "wipe":
        if key == "y":
            _confirm = None
            # Use the cached default repo URL with no interactive prompt — the
            # blocking readline was confusing the workflow (the yellow confirm
            # message stayed visible while the URL prompt waited silently at
            # the bottom of the screen). For non-default URLs, use
            # `bash wipe.sh --repo URL` from the CLI.
            url = _detect_cached_repo_url() or "https://github.com/dtolnay/anyhow"
            _detach(["bash", os.path.join(SCRIPT_DIR, "wipe.sh"), "--repo", url])
            _action_msg = yellow(f"↺ wiping and restarting with {url}")
        else:
            _action_msg = dim("wipe cancelled")
            _confirm = None
        return
    if _confirm == "wipe-augmented":
        if key == "a":
            _confirm = None
            _detach(["bash", os.path.join(SCRIPT_DIR, "wipe.sh"), "--keep-augmented"])
            _action_msg = cyan("↺ restoring augmentation, restarting from task1")
        elif key == "h":
            _confirm = None
            url = _detect_cached_repo_url() or "https://github.com/dtolnay/anyhow"
            _detach(["bash", os.path.join(SCRIPT_DIR, "wipe.sh"), "--repo", url])
            _action_msg = yellow(f"↺ hard reset, restarting with {url}")
        else:
            _action_msg = dim("wipe cancelled")
            _confirm = None
        return
    if _confirm == "stop":
        if key == "y":
            try:
                subprocess.run(["pkill", "-f", "pipeline.py"], check=True)
                _action_msg = red("■ pipeline stopped")
            except subprocess.CalledProcessError:
                _action_msg = dim("pipeline was not running")
        else:
            _action_msg = dim("stop cancelled")
        _confirm = None
        return
    if _confirm == "continue":
        if key == "y":
            _detach(["bash", os.path.join(SCRIPT_DIR, "continue.sh"), "--y"])
            _action_msg = green("▶ continue.sh launched")
        else:
            _action_msg = dim("continue cancelled")
        _confirm = None
        return
    if _confirm == "graceful":
        if key == "y":
            _graceful_pending = True
            graceful_stop.sentinel_path().touch()
            _action_msg = cyan("◑ graceful stop requested — pipeline will exit at next checkpoint")
        else:
            _action_msg = dim("graceful cancelled")
        _confirm = None
        return
    if _confirm == "poweroff":
        if key == "y":
            _set_shutdown_armed(True)
            _action_msg = red("⏻ power-off on finish ARMED")
        else:
            _action_msg = dim("power-off arm cancelled")
        _confirm = None
        return
    if key == "c":
        if _pipeline_running():
            _confirm    = "continue"
            _action_msg = green("▶ continue (will kill running pipeline) — confirm? [y/n]")
        else:
            _detach(["bash", os.path.join(SCRIPT_DIR, "continue.sh"), "--y"])
            _action_msg = green("▶ continue.sh launched")
    elif key == "g":
        if _graceful_pending:
            _graceful_pending = False
            with contextlib.suppress(FileNotFoundError):
                graceful_stop.sentinel_path().unlink()
            _action_msg = dim("graceful cancelled")
        elif _pipeline_running():
            _confirm    = "graceful"
            _action_msg = cyan("◑ graceful stop — finish current attempt then exit — confirm? [y/n]")
        else:
            _action_msg = dim("pipeline was not running")
    elif key == "w":
        tag = _augmentation_tag()
        if tag:
            _confirm    = "wipe-augmented"
            _action_msg = yellow(
                f"↺ augmentation found ({tag}): "
                f"[a]ugmentation restore → restart from task1, "
                f"[h]ard reset → re-clone repo, [other] cancel"
            )
        else:
            _confirm    = "wipe"
            _action_msg = yellow("↺ wipe all generated code and restart — confirm? [y/n]")
    elif key == "s":
        _confirm    = "stop"
        _action_msg = red("■ stop pipeline — confirm? [y/n]")
    elif key == "p":
        if _shutdown_armed():
            _set_shutdown_armed(False)
            _action_msg = dim("⏻ power-off on finish disabled")
        else:
            _confirm    = "poweroff"
            _action_msg = red("⏻ power off machine when the run finishes — confirm? [y/n]")
    elif key == "l":
        _view = "monitor" if _view == "legend" else "legend"
    elif key == "h":
        _view = "monitor" if _view == "health" else "health"
        _scroll["health"] = HEALTH_TOP   # findings first
    elif key == "d":
        _DEMO = not _DEMO
        _action_msg = (yellow(f"[d]emo mode enabled — switching every {DEMO_INTERVAL}s")
                       if _DEMO else dim("[d]emo mode stopped — the view stays where it is"))
    elif key == "e":
        _view = "monitor" if _view == "phases" else "phases"
        _phases.reset()
    elif key == "x" and _view == "phases":
        _phases.toggle_expand()
    elif key == "f":
        _view = "monitor" if _view == "fields" else "fields"
    elif key == "a":
        _set_auto_armed(not _auto_armed())
        _action_msg = (green("[a]uto mode ON — pipeline will fire operator_run.sh on stall")
                      if _auto_armed() else dim("[a]uto mode off"))
    elif key == "v":
        _layout = _layout.toggled()
        _action_msg = (yellow(f"split on, but the terminal is too narrow (needs "
                              f"{_layout.min_total()} columns) — tabs until it widens")
                       if _layout.enabled and _split_widths() is None else "")
    elif key in ("<", ">") and _layout.enabled:
        _layout = _layout.resized(1 if key == ">" else -1, _term_w())
    elif key in ("1", "2", "3", "4"):
        _view = {"1": "monitor", "2": "pipeline", "3": "stream", "4": "events"}[key]
    elif key in ("UP", "DOWN", "PGUP", "PGDN"):
        # The scroll keys move the view whose box is drawn: the right column's
        # in the split layout. PgUp/PgDn move by that box's last drawn height.
        target = _pane_view() if _split_widths() is not None else _view
        if target == "phases":
            step = SCROLL_STEP if key in ("UP", "DOWN") else _page
            _phases.scroll(-step if key in ("UP", "PGUP") else step)
        elif target in _scroll:
            step = SCROLL_STEP if key in ("UP", "DOWN") else _page
            up = key in ("UP", "PGUP")
            _scroll[target] = max(0, _scroll[target] + (step if up else -step))

# ── Main loop ─────────────────────────────────────────────────────────────────

EXIT_LOG = os.path.join(SCRIPT_DIR, "logs", "monitor_exit.log")   # see exit_record


def _repaint_error(e: Exception) -> None:
    """Show a failed frame's error in place of the frame. A repaint that fails
    too (the terminal refused the write) is not a reason to exit: the next
    iteration draws again."""
    _buf.clear()
    _ln(red(f"  render error: {e}"))
    with contextlib.suppress(Exception):
        _flush_frame()
    _buf.clear()


def main():
    global _action_msg, _term_settings, _graceful_pending, _view
    signal.signal(signal.SIGHUP, signal.SIG_IGN)   # survive terminal events from child procs
    signal.signal(signal.SIGTERM, _exit.on_sigterm)     # restore the terminal on a kill
    old_settings = termios.tcgetattr(sys.stdin)
    _term_settings = old_settings
    sys.stdout.write(_REPORTING_OFF + "\033[2J\033[?25l")
    sys.stdout.flush()
    exit_reason = "quit: unknown"
    _demo_idx = 0
    _demo_next = time.monotonic() + DEMO_INTERVAL
    try:
        tty.setcbreak(sys.stdin.fileno())
        _key_reader.start()
        while True:
            # --demo: advance to the next of views 1/2/3 every DEMO_INTERVAL s.
            if _DEMO and time.monotonic() >= _demo_next:
                _demo_idx = (_demo_idx + 1) % len(_DEMO_VIEWS)
                _view = _DEMO_VIEWS[_demo_idx]
                _demo_next = time.monotonic() + DEMO_INTERVAL
            elif not _DEMO:
                _demo_next = time.monotonic() + DEMO_INTERVAL   # re-armed by [d]
            try:
                _render_all()
                _flush_frame()
            except Exception as e:
                _repaint_error(e)

            if _graceful_pending:
                if not graceful_stop.sentinel_path().exists():
                    _graceful_pending = False
                    _action_msg = ""
                else:
                    _action_msg = bg_white(" ◑ graceful — waiting for pipeline to exit… ")
            elif _confirm is None:   # a pending confirmation keeps the prompt _apply_key set
                _action_msg = ""

            try:
                # In demo mode wake exactly at the next view switch (≤ REFRESH)
                # so cycling stays crisp while the panel still refreshes live.
                timeout: float = REFRESH
                if _DEMO:
                    timeout = max(0.1, min(float(REFRESH), _demo_next - time.monotonic()))
                # Block on the queue the reader thread fills, not on stdin
                # directly — the thread already decoded the key while this
                # loop was busy rendering, so the first `get` here returns as
                # soon as anything is pending instead of waiting out the rest
                # of `timeout`. Then drain whatever else queued up during that
                # same render pass so a burst of presses (e.g. holding PgDn)
                # all land before the next, possibly slow, render.
                key = _key_reader.get(timeout=timeout)
                if key == "q":
                    raise _exit.key_quit("q", _key_reader.recent_input())
                _run_action(key)
                while True:
                    try:
                        key = _key_reader.get_nowait()
                    except queue.Empty:
                        break
                    if key == "q":
                        raise _exit.key_quit("q", _key_reader.recent_input())
                    _run_action(key)
            except queue.Empty:
                pass
            except Exception as e:
                _action_msg = red(f"key error: {e}")
    except (_exit.Quit, KeyboardInterrupt) as e:
        exit_reason = _exit.describe(e)
    except BaseException as e:
        exit_reason = _exit.describe(e)
        raise
    finally:
        _exit.record(EXIT_LOG, exit_reason)
        _key_reader.stop()
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        sys.stdout.write("\033[?25h\033[2J\033[H\n")
        sys.stdout.flush()

if __name__ == "__main__":
    # --demo cycles views 1→2→3 every DEMO_INTERVAL s. Optional override:
    #   --demo=12   or   --demo 12   (seconds per view).
    _args = sys.argv[1:]
    for _i, _a in enumerate(_args):
        if _a == "--demo":
            _DEMO = True
            if _i + 1 < len(_args) and _args[_i + 1].isdigit():
                DEMO_INTERVAL = int(_args[_i + 1])
        elif _a.startswith("--demo="):
            _DEMO = True
            _val = _a.split("=", 1)[1]
            if _val.isdigit():
                DEMO_INTERVAL = int(_val)
    main()
