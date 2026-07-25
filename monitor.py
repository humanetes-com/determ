#!/usr/bin/env python3
"""Pipeline / GPU monitor.  Keys: 1/2/3 views  c/g/w/s/p/a actions  ↑↓ navigate  q quit"""
import contextlib
import json as _json
import os
import select
import signal
import subprocess
import sys
import termios
import time
import tty
from typing import Any

_LOG_DIR     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
_RUN_REGISTRY = os.getenv("PIPELINE_RUN_REGISTRY", "/tmp/determ_pipelines.json")
REFRESH      = 2


def _discover_running_runs() -> list[dict]:
    """Read the pipeline registry and return entries whose PIDs are still alive."""
    try:
        with open(_RUN_REGISTRY) as fh:
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

# P6 data-vs-render split: the events.jsonl readers live in the data module; this
# file keeps the renderer + live loop. `_`-aliases preserve the in-file call sites.
# (Tests point the readers via pipeline_core.monitor.events_data.configure / globals.)
from pipeline_core.monitor import events_data as _ed  # noqa: E402
from pipeline_core.monitor import run_data as _rd  # noqa: E402
from pipeline_core.monitor.field_glossary import (  # noqa: E402
    field_glossary_window as _field_glossary_window,
)
from pipeline_core.monitor.field_glossary import (
    hint_lines as _hint_lines,
)
from pipeline_core.monitor.health_view import health_lines as _health_lines  # noqa: E402
from pipeline_core.monitor.legend import legend_window as _legend_window  # noqa: E402
from pipeline_core.monitor.render import (  # noqa: E402
    ansi_seq_len as _ansi_seq_len,
)
from pipeline_core.monitor.render import (
    bg_white,
    bold,
    cyan,
    dim,
    green,
    red,
    rev,
    yellow,
)
from pipeline_core.monitor.render import (
    call_start_value as _call_start_value,
)
from pipeline_core.monitor.render import (
    driver_counter as _driver_counter,
)
from pipeline_core.monitor.render import (
    event_format as _event_format,
)
from pipeline_core.monitor.render import (
    plain as _plain,
)
from pipeline_core.monitor.render import (
    state_done_key as _state_done_key,
)
from pipeline_core.monitor.vllm_status import (  # noqa: E402
    vllm_status_rows as _vllm_status,
)

_ed.configure(events_log=EVENTS_LOG)
_pipeline_start_time = _ed.pipeline_start_time
_read_recent_events = _ed.read_recent_events
_coverage_from_events = _ed.coverage_from_events
_phase_c_state = _ed.phase_c_state
_latest_compile_file_stats = _ed.latest_compile_file_stats
_latest_test_file_stats = _ed.latest_test_file_stats
_latest_flow_audit = _ed.latest_flow_audit

_rd.configure(script_dir=os.path.dirname(os.path.abspath(__file__)),
              pipeline_log=PIPELINE_LOG)
_parse_coverage_json = _rd.parse_coverage_json
_baked_cluster_order = _rd.baked_cluster_order
_find_project_dir = _rd.find_project_dir
_parse_pipeline_state = _rd.parse_pipeline_state
_current_stage_label = _rd.current_stage_label
_task_progression = _ed.task_progression       # debt#7: structured-event sources
_pipeline_log_facts = _ed.pipeline_log_facts
_vllm_runtime = _ed.vllm_runtime
_log_tail_meta = _rd.log_tail_meta
_read_backend = _rd.read_backend
_pipeline_running = _rd.pipeline_running
_detect_cached_repo_url = _rd.detect_cached_repo_url

_GPU_BW = {"3090": 936.2, "3080": 760.0, "4090": 1008.0, "4080": 716.8,
           "A100": 2000.0, "H100": 3350.0}

# ── ANSI helpers ──────────────────────────────────────────────────────────────

def _w():
    try: return os.get_terminal_size().columns
    except: return 80

def _h():
    try: return os.get_terminal_size().lines
    except: return 24

def bar(pct, width=18, label: str = ""):
    filled = round(pct / 100 * width)
    s = list("█" * filled + "░" * (width - filled))
    if label and len(label) <= width:
        start = 0
        for i, ch in enumerate(label):
            s[start + i] = ch
    return "".join(s)

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

# ── Box helpers ───────────────────────────────────────────────────────────────

def box(title, rows):
    w = _w() - 2
    _ln(f"┌─ {bold(title)} {'─' * max(0, w - len(title) - 3)}┐")
    for row in rows:
        pad = w - 1 - len(_plain(row))
        _ln(f"│ {row}{' ' * max(pad, 0)}│")
    _ln("└" + "─" * w + "┘")

def _truncate_visible(s: str, max_visible: int) -> str:
    """Truncate `s` so its plain (ANSI-stripped) length is at most max_visible.
    Preserves ANSI escapes that have already been opened by appending a reset.
    """
    if max_visible <= 0:
        return ""
    if len(_plain(s)) <= max_visible:
        return s
    out: list[str] = []
    visible = 0
    i = 0
    while i < len(s) and visible < max_visible:
        if s[i] == "\033":
            # Copy the whole ANSI sequence verbatim (up to and including the
            # final letter).
            seq = _ansi_seq_len(s, i)
            if seq:
                out.append(s[i:i + seq])
                i += seq
                continue
        out.append(s[i])
        visible += 1
        i += 1
    out.append("\033[0m")  # safety reset
    return "".join(out)


def box_lines(title, rows):
    """Same as box() but returns list of strings without printing."""
    w = _w() - 2
    out = [f"┌─ {bold(title)} {'─' * max(0, w - len(title) - 3)}┐"]
    # Inner content width: w - 2 ("│ " left pad and " │" right) → w - 1 visible.
    inner_w = max(0, w - 2)
    for row in rows:
        visible = len(_plain(row))
        if visible > inner_w:
            row = _truncate_visible(row, max(0, inner_w - 1)) + "…"
            visible = len(_plain(row))
        pad = inner_w - visible
        out.append(f"│ {row}{' ' * max(pad, 0)} │")
    out.append("└" + "─" * w + "┘")
    return out

# ── Key reading (handles arrow escape sequences) ──────────────────────────────

def _read_key():
    """Read one logical keypress; returns a string. Arrow keys → 'UP'/'DOWN'/'PGUP'/'PGDN'.

    Uses os.read() throughout to bypass Python's BufferedReader — without this,
    select.select() misses continuation bytes that the buffer has already consumed.
    """
    fd = sys.stdin.fileno()
    if not select.select([fd], [], [], 0)[0]:
        return ""
    ch = os.read(fd, 1).decode("latin-1")
    if ch != "\033":
        return ch
    # Escape sequence — read more with short timeout
    if not select.select([fd], [], [], 0.05)[0]:
        return "ESC"
    ch2 = os.read(fd, 1).decode("latin-1")
    if ch2 != "[":
        return "ESC"
    if not select.select([fd], [], [], 0.05)[0]:
        return "ESC"
    ch3 = os.read(fd, 1).decode("latin-1")
    if ch3 == "A":  return "UP"
    if ch3 == "B":  return "DOWN"
    if ch3 == "5":
        os.read(fd, 1)   # consume trailing ~
        return "PGUP"
    if ch3 == "6":
        os.read(fd, 1)
        return "PGDN"
    return "ESC"

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
            f"GPU{idx}  [{col(bar(ui))}] {ui:3}%  "
            f"MEM-BW {mcol(f'{mi:3}%')}  "
            f"VRAM {int(used)/1024:.1f}/{int(total)/1024:.1f} GB  Temp {temp}°C"
        )
        card_bw = next((v for k, v in _GPU_BW.items() if k in name), None)
        if card_bw:
            bw_sum += card_bw * mi / 100
    if bw_sum > 0 and mem_gb > 0:
        rows.append(f"  {bold('≈ tokens/s:')} {bw_sum/mem_gb:.1f}")
    rows.append(dim("  ") + green("■ >60%") + dim("  ") + yellow("■ 20–60%") + dim("  □ idle"))
    return rows


def _docs_richness_row() -> str:
    """One-line latest doc-richness vs convergence (R13.3 docs.richness event).

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


def _phase_c_panel_rows(state: dict[str, Any]) -> list[str]:
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

    # Cumulative progress bar (across runs):
    #   filled_cumulative = filled_count_at_pass_start + this_pass_done
    # Authoritative source = `filled_count` from phase_c.pass.start (snapshot
    # of "how many fns in this cluster are NOT still unimplemented" at the
    # moment the pass started). Adding this-pass fills tracks progress
    # mid-pass without ever crossing the total.
    #
    # Legacy fallback for events emitted before that field existed:
    # `total - pass_stubs_total + done`. This is wrong when the TDD driver
    # narrows pass_stubs_total to a per-pass frontier (1-2 fns) instead of
    # the full remaining-stubs count — the math then claims ~99% with only
    # 1 frontier slot, which is what was hiding the real cluster progress.
    # The `· frontier: A/B` segment appended below (both the primary and legacy
    # branches) reads:
    #   A = fills done + failed in the current fill pass
    #       (pass_fills_done + pass_fills_failed) — counts each per-fn fill, each
    #       impl-block MEMBER, and each whole-file fn individually.
    #   B = the number of stubs the fill engine QUEUED for this pass
    #       (pass_stubs_total) — that's the frontier fns PLUS their leaf-first
    #       prerequisites and any behavioral re-fill targets, so B is usually
    #       larger than the raw tdd frontier_size.
    # So "frontier: 6/9" = 6 of the 9 queued stubs have been attempted this pass.
    # Under the tdd driver pass.start fires once per iter, so A resets per iter.
    _pt = state.get("pass_stubs_total", 0) or 0
    _pd = state.get("pass_fills_done", 0) or 0
    _pf = state.get("pass_fills_failed", 0) or 0
    _total = state.get("total_fns_in_cluster", 0) or 0
    _filled_at_start = state.get("filled_count_at_pass_start", 0) or 0
    _all_stubs_at_start = state.get("all_stubs_remaining_at_pass_start", 0) or 0
    if _total > 0 and _filled_at_start >= 0 and (_filled_at_start + _all_stubs_at_start) > 0:
        _cum_filled = min(_total, _filled_at_start + _pd)
        _cum_pct = _cum_filled / _total if _total else 0
        _bar_w = 28
        _bar_filled = int(_bar_w * _cum_pct)
        _bar = "█" * _bar_filled + "░" * (_bar_w - _bar_filled)
        # `this pass` denominator: prefer the frontier size (pass_stubs_total)
        # — that's the actionable workload for the current TDD iteration.
        rows.append(f"           progress  [{_bar}]  "
                    f"{_cum_filled}/{_total} ({int(_cum_pct * 100)}%) "
                    f"cluster  · frontier: {_pd + _pf}/{_pt}")
    elif _total > 0 and _pt > 0:
        # Legacy fallback path (no filled_count in events).
        _cum_filled = max(0, _total - _pt + _pd)
        _cum_pct = _cum_filled / _total if _total else 0
        _bar_w = 28
        _bar_filled = int(_bar_w * _cum_pct)
        _bar = "█" * _bar_filled + "░" * (_bar_w - _bar_filled)
        rows.append(f"           progress  [{_bar}]  "
                    f"{_cum_filled}/{_total} ({int(_cum_pct * 100)}%) "
                    f"cluster (legacy)  · frontier: {_pd + _pf}/{_pt}")
    elif _pt > 0:
        _done_this_pass = _pd + _pf
        _pct = _done_this_pass / _pt
        _bar_w = 28
        _filled = int(_bar_w * _pct)
        _bar = "█" * _filled + "░" * (_bar_w - _filled)
        rows.append(f"           progress  [{_bar}]  {_done_this_pass}/{_pt} ({int(_pct * 100)}%)")

    first = state.get("fills_first", 0)
    rec = state.get("fills_recovered", 0)
    failed = state.get("fills_failed", 0)
    skip = state.get("fills_skipped", 0)
    total_done = first + rec
    rows.append(f"           fills  "
                f"{green(f'✓ first={first}')}  "
                f"{yellow(f'↻ recovered={rec}')}  "
                f"{red(f'✗ failed={failed}')}  "
                f"{dim(f'⊘ skip={skip}')}  "
                f"{dim(f'(done={total_done})')}")
    notes = state.get("notes", 0)
    # In-flight fills — the parallel set (up to PIPELINE_PARALLELISM at once),
    # longest-running first (the block eating the iter). ↻ = behavioral re-fill,
    # ⊞ = impl block, plain = per-fn; each annotated with elapsed.
    flight = state.get("in_flight") or []
    if flight:
        _ifb = []
        for u in flight[:4]:
            _a = u.get("age_s")
            _age = ("?" if _a is None else f"{_a}s" if _a < 60 else f"{_a // 60}m")
            _k = u.get("kind")
            _tag = ("↻" if u.get("refill")
                    else "⊞" if _k == "impl" else "▤" if _k == "file" else "")
            _lab = (u.get("label") or "?").splitlines()[0][:30]
            _ifb.append(f"{_tag}{_lab}({_age})")
        _more = f"  +{len(flight) - 4}" if len(flight) > 4 else ""
        rows.append(f"           in-flight {cyan('  '.join(_ifb))}{dim(_more)}   notes={notes}")
    else:
        rows.append(f"           in-flight {dim('idle')}   notes={notes}")
    hot = state.get("hot_fn")
    if hot:
        rows.append(f"           hot fn   {dim(hot[0])} ({hot[1]} attempts)")
    recent = state.get("recent_fills") or []
    if recent:
        bits = "  ".join(f"{fn}:{body}c×{a}" for fn, body, a in recent)
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


_PHASE_SEQ = ["task0a", "task0b", "task0c", "strip", "task1",
              "task2a", "task2b", "task2c", "task3", "task4"]


def _phase_bar() -> list[str]:
    """Render the full pipeline phase sequence with ✓done / ▶current / ·pending,
    read from pipeline_state.md (authoritative): the `[key] done` entries give
    real per-phase status, the `current:` line gives the active stage."""
    pd = _find_project_dir()
    done_keys: set[str] = set()
    cur = ""
    if pd:
        try:
            with open(os.path.join(pd, "pipeline_state.md")) as _f:
                for line in _f:
                    key = _state_done_key(line)
                    if key is not None:
                        done_keys.add(key)
                    if line.startswith("current:"):
                        cur = line.split(":", 1)[1].strip()
        except Exception:
            pass
    # The combined reconstruct marker means the whole loop finished.
    if "task2-4" in done_keys:
        done_keys |= {"task2a", "task2b", "task2c", "task3", "task4"}
    stage = cur.split("/", 1)[0].strip() if cur else ""
    if stage in ("task2", "task2-4"):
        stage = "task2c"
    cells = []
    for p in _PHASE_SEQ:
        short = p.replace("task", "t")
        if p == stage and p not in done_keys:
            cells.append(cyan("▶" + short))
        elif p in done_keys:
            cells.append(green("✓" + short))
        else:
            cells.append(dim("·" + short))
    rows = ["  phase    " + " ".join(cells)]
    _detail = cur.split("/", 2)[-1].strip() if cur.count("/") >= 1 else ""
    if _detail and _detail != cur:
        rows.append("           " + dim(_detail))
    return rows


def pipeline_status():
    prog  = _pipeline_log_facts()       # debt#7: task0c progress from structured events
    prog.update(_log_tail_meta())       # + raw-log presentation (last_line, age)
    state = _parse_pipeline_state(_find_project_dir())
    running   = _pipeline_running()
    status_str = green("RUNNING") if running else red("STOPPED")
    w = _w() - 4

    if not os.path.exists(PIPELINE_LOG):
        return [f"{status_str}   {red('log not found')}"]

    # Coverage: the structured coverage.measured event is authoritative (R8.4);
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
    rows.extend(_vllm_status(_w() - 4))

    # Full pipeline phase sequence (t0a … t4) with the current stage highlighted.
    rows.extend(_phase_bar())

    # ── Header: status + coverage ────────────────────────────────────────────
    _cov_data = _parse_coverage_json(_find_project_dir())
    _total_cov  = sum(c for c, _ in _cov_data.values())
    _total_cvbl = sum(v for _, v in _cov_data.values())
    _lines_str  = f"  {_total_cov}/{_total_cvbl}" if _total_cvbl else ""

    # Projection: sum of per-file coverage (which includes quick-cov updates
    # newer than the last full grcov run) gives the live total. If it's
    # strictly higher than the value carried in pipeline_state.md / log, show
    # it as a parenthesised target alongside the committed value.
    _proj_str = ""
    if _total_cvbl > 0:
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
    # _phase_bar() above is the single, granular phase display. tp is still
    # parsed below for the fix-phase detection further down.)
    # debt#7: task progression is now derived purely from structured events.
    tp = _task_progression()

    # ── P3.11: Phase C per-fn panel (visible when the skeleton backend
    # is running). Pulls structured data from logs/events.jsonl.
    try:
        _pc = _phase_c_state()
        for r in _phase_c_panel_rows(_pc):
            rows.append(r)
    except Exception as _e:
        rows.append(dim(f"  phase-c  (panel error: {_e})"))

    _dr = _docs_richness_row()
    if _dr:
        rows.append(_dr)

    # ── Loop hierarchy: cluster → attempt → file → retry ────────────────────
    #
    #  cluster  ✓stable  ▶nightly  ○...       [██░░░░░░] 1/3
    #    └ attempt                             [████░░░░] 2/10
    #        └ file  context.rs               [████████] 4/8
    #             └ retry                     [██░░░░░░] 1/2   (only when active)

    # Level 0 — cluster row
    n_clusters = len(clusters)
    n_done_cl  = len(done_set)
    cl_i       = next((i for i, n in enumerate(clusters) if n == cur_cl), n_done_cl)
    cl_pct     = (cl_i / n_clusters * 100) if n_clusters else 0
    cl_parts: list[str] = []
    for name in clusters:
        ab = name[:10]
        if name in done_set:   cl_parts.append(green(f"✓{ab}"))
        elif name == cur_cl:   cl_parts.append(yellow(bold(f"▶{ab}")))
        else:                  cl_parts.append(dim(f"○{ab}"))
    cl_names = "  ".join(cl_parts)
    cl_bar   = f"[{bar(cl_pct, 10)}] {cl_i}/{n_clusters}" if n_clusters else ""
    rows.append(f"  cluster  {cl_names}  {dim(cl_bar)}")

    # Level 1 — attempt row
    try:    att_i = int(prog["attempt"]); att_t = int(prog["max_attempt"])
    except: att_i = 0; att_t = 0
    att_pct = (att_i / att_t * 100) if att_t else 0
    att_bar = f"[{bar(att_pct, 14)}] {prog['attempt']}/{prog['max_attempt']}"
    rows.append(f"    └ attempt  {att_bar}")

    # Level 2 — file row
    _cl_files = prog["cluster_files"]
    all_f  = _cl_files if (cur_cl and cur_cl not in ("stable", "") and _cl_files) else prog["all_files"]
    this_p = prog["files_this_pass"]
    ft = max(len(all_f), len(this_p))
    fi = len(this_p)
    cf = prog["current_file"].replace("src/", "") if prog["current_file"] != "?" else "?"
    file_pct = (fi / ft * 100) if ft else 0
    file_bar = f"[{bar(file_pct, 12)}] {fi}/{ft}"
    rows.append(f"        └ file  {cyan(cf):<18}  {file_bar}")

    # Level 3 — inner retry (only when active)
    ir, ir_max = prog.get("inner_retry", "?"), prog.get("inner_retry_max", "?")
    if ir != "?" and ir_max != "?":
        try:    r_pct = int(ir) / int(ir_max) * 100
        except: r_pct = 0
        retry_bar = f"[{bar(r_pct, 10)}] {ir}/{ir_max}"
        rows.append(f"             └ retry  {retry_bar}")

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
    proj_dir = _find_project_dir()

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

    cov_data = _parse_coverage_json(proj_dir)
    if cov_data and not ((_compile_stats or _test_stats) and _in_fix_phase):
        cur_base = os.path.basename(prog["current_file"]) if prog["current_file"] != "?" else ""
        # For non-default clusters, restrict to files the cluster is actually targeting.
        # If the cluster hasn't processed any files yet (e.g. harness fix running), show all.
        _cluster_filter: set[str] | None = None
        if cur_cl and cur_cl not in ("stable", ""):
            cf = prog["cluster_files"]
            if cf:
                _cluster_filter = set(cf)

        # Sort: pipeline processing order for current pass (same index as file counter 8/8),
        # then files from older passes not yet reprocessed, then unknown files by coverage asc.
        # Only show src/ files — tests/ and build.rs are excluded from pipeline coverage.
        _this_pass_order = {f: i for i, f in enumerate(prog["files_this_pass"])}
        _n = len(_this_pass_order)
        _extra = {f: _n + i for i, f in enumerate(prog["all_files"]) if f not in _this_pass_order}
        _order = {**_this_pass_order, **_extra}
        _fallback = _n + len(_extra)
        sorted_files = sorted(
            [(name, cov, cvbl) for name, (cov, cvbl) in cov_data.items()
             if name.startswith("src/")
             and (_cluster_filter is None or name in _cluster_filter)],
            key=lambda x: _order.get(x[0], _fallback + (x[1] / x[2] if x[2] else 0)),
        )
        # Ensure files_this_pass entries always appear (even if not in grcov JSON)
        _in_sorted = {name for name, *_ in sorted_files}
        for _f in prog["files_this_pass"]:
            if _f not in _in_sorted and _f.startswith("src/"):
                if _cluster_filter is None or _f in _cluster_filter:
                    sorted_files.append((_f, 0, 0))
        sorted_files.sort(key=lambda x: _order.get(x[0], _fallback + (x[1] / x[2] if x[2] else 0)))
        max_name = max((len(os.path.basename(n)) for n, *_ in sorted_files), default=10)
        n_total  = len(sorted_files)
        n_done   = sum(1 for _, cov, cvbl in sorted_files if cov >= cvbl)
        rows.append(dim(f"  {'─' * max(4, min(w - 2, 60))}"))
        rows.append(f"  files overview — {n_done}/{n_total} complete")
        queue_hist   = prog["queue_history"]
        qcov_hist    = prog.get("quick_cov_history", {})
        for name, covered, coverable in sorted_files:
            short   = os.path.basename(name)
            is_cur  = (short == cur_base)
            arrow   = yellow("▶") if is_cur else " "
            hist    = queue_hist.get(short, [])
            trend   = dim("  " + "→ ".join(str(c) for c in hist[-5:])) if hist else ""
            qhist   = qcov_hist.get(name, qcov_hist.get(short, []))
            if len(qhist) >= 2:
                qtrend = dim(f"  {qhist[-2]:.1f}%→ ") + f"{qhist[-1]:.1f}%"
            elif len(qhist) == 1:
                qtrend = dim(f"  {qhist[-1]:.1f}%")
            else:
                qtrend = ""
            if coverable == 0:
                rows.append(f"{arrow} {short.ljust(max_name)}  [{bar(0, 14, ' n/a ')}] {dim('  n/a')}{qtrend}{trend}")
                continue
            pct     = covered / coverable * 100
            done    = covered >= coverable
            col     = green if done else (yellow if pct >= 80 else red)
            tick    = green(" ✓") if done else ""
            _label  = f"{pct:.1f}%"
            rows.append(
                f"{arrow} {short.ljust(max_name)}  [{col(bar(pct, 14, _label))}]"
                f"  {covered}/{coverable}{tick}{qtrend}{trend}"
            )

    return rows


_prev_len        = 0
_THINK_CAP_CHARS = 40_000

def _fmt_eta(secs):
    if secs < 60:   return f"{int(secs)}s"
    if secs < 3600: return f"{int(secs/60)}m{int(secs%60):02d}s"
    return f"{int(secs/3600)}h{int((secs%3600)/60):02d}m"

def generation():
    global _prev_len
    try:
        stat = os.stat(STREAM_LOG)
        with open(STREAM_LOG) as f:
            content = f.read()
    except FileNotFoundError:
        return [dim("no stream log yet")]
    except Exception as e:
        return [red(f"stream log error: {e}")]
    if not content.strip():
        return [dim("stream log empty")]
    now     = time.time()
    age     = now - stat.st_mtime
    age_s   = f"{int(age)}s" if age < 60 else f"{int(age/60)}m{int(age%60):02d}s"
    started = 0.0
    if (cs := _call_start_value(content)) is not None:
        started = float(cs)
    elapsed     = max(1.0, now - started) if started > 0 else max(1.0, age)
    ela_s       = f"{int(elapsed/60)}m{int(elapsed%60):02d}s" if elapsed >= 60 else f"{int(elapsed)}s"
    cur_chars   = len(content)
    inst_rate   = max(0, cur_chars - _prev_len) / REFRESH / 4
    smooth_rate = cur_chars / 4 / elapsed
    _prev_len   = cur_chars
    rate_str    = f"~{inst_rate:.0f} tok/s" if inst_rate > 0.5 else dim("no new tokens")
    in_think    = '<think>' in content and '</think>' not in content
    had_think   = '</think>' in content
    if   "[TIMEOUT"  in content:                                    phase = red("TIMEOUT")
    elif "[FOLLOWUP" in content:                                    phase = cyan("FOLLOWUP")
    elif "[CONTENT]" in content:                                    phase = green("CODE")
    elif in_think:                                                  phase = yellow("THINKING")
    elif had_think or ("[CALL:" in content and cur_chars > 60):     phase = green("CODE")
    else:                                                           phase = yellow("THINKING")
    _vr = _vllm_runtime()  # debt#7: {max_tokens, cpt, retries} from structured events
    progress_rows = []
    if in_think:
        tc   = max(0, cur_chars - content.find('<think>') - 7)
        pct  = min(100, tc / _THINK_CAP_CHARS * 100)
        eta  = _fmt_eta(max(0, _THINK_CAP_CHARS - tc) / smooth_rate) if smooth_rate > 0.1 else "?"
        progress_rows.append(
            f"  think [{bar(pct, 20)}] {pct:4.0f}%  {tc:,}/{_THINK_CAP_CHARS:,} chars"
            f"  {rate_str}  ETA {eta}  elapsed {ela_s}")
    else:
        max_tok = _vr["max_tokens"]
        tg      = cur_chars / 4
        if max_tok:
            pct = min(100, tg / max_tok * 100)
            eta = _fmt_eta(max(0, max_tok - tg) / smooth_rate) if smooth_rate > 0.1 else "?"
            progress_rows.append(
                f"  budget [{bar(pct, 20)}] {pct:4.0f}%  {tg:,.0f}/{max_tok:,} tok"
                f"  avg {smooth_rate:.1f} tok/s  ETA ≤ {eta}  elapsed {ela_s}")
    display = content
    for marker in ("[FOLLOWUP", "[CONTENT]", "</think>"):
        if marker in display:
            display = display.split(marker)[-1].lstrip("\n")
            break
    w      = _w() - 4
    dlines = [ln for ln in display.splitlines() if ln.strip()][-5:]
    cpt, retries = _vr["cpt"], _vr["retries"]
    cpt_s    = f"cpt={cpt:.2f}" if cpt else dim("cpt=?")
    retry_s  = f"  {red(f'400-retries={retries}')}" if retries else ""
    rows  = [f"phase={phase}   {cur_chars:,} chars   last-tok {age_s} ago   {rate_str}   {cpt_s}{retry_s}"]
    rows += progress_rows
    rows += [dim((ln[:w-2] + "…") if len(ln) > w else ln) for ln in dlines] or [dim("(empty)")]
    return rows

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
_view        = "monitor"      # monitor|pipeline|stream|events|legend|health|fields
_scroll      = {"pipeline": 0, "stream": 0, "events": 0, "legend": 0, "health": 0, "fields": 0}   # lines scrolled up from bottom (0=bottom)
_show_hints  = True           # inline field-hint strip under the monitor view (toggle: [t])
_confirm          = None   # pending confirmation: "continue" | "wipe" | "stop" | "poweroff" | None
_graceful_pending = os.path.exists("/tmp/pipeline_graceful_stop")  # survives monitor restart

# Auto-operator toggle: sentinel presence enables stall-triggered operator_run.sh
# passes, mirroring the graceful-stop sentinel's touch/remove convention above.
# The watch-and-fire logic runs inside the PIPELINE process itself (phase_c_tdd's
# TDD loop, via pipeline_core.adapters.auto_operator) so it stays live whether or
# not the monitor is attached; a fresh pipeline start seeds this file from `.env`
# AUTO_DEFAULT. The monitor is just the remote toggle switch + status display.
_AUTO_SENTINEL = "/tmp/pipeline_automode"

def _auto_armed() -> bool:
    return os.path.exists(_AUTO_SENTINEL)

def _set_auto_armed(on: bool) -> None:
    if on:
        open(_AUTO_SENTINEL, "w").close()
    else:
        with contextlib.suppress(FileNotFoundError):
            os.remove(_AUTO_SENTINEL)

# Power-off-on-finish toggle: sentinel ("1"/"0") overrides .env SHUTDOWN_ON_FINISH,
# mirroring pipeline._shutdown_on_finish_enabled. Monitor reads it every frame.
_SHUTDOWN_SENTINEL = "/tmp/pipeline_shutdown_on_finish"

def _shutdown_armed() -> bool:
    try:
        with open(_SHUTDOWN_SENTINEL) as f:
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
    with open(_SHUTDOWN_SENTINEL, "w") as f:
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

def _bottom_bar():
    running = _pipeline_running()

    parts = []
    for vid, key, label in VIEWS:
        tag = f"[{key}]{label}"
        parts.append(rev(bold(tag)) if vid == _view else dim(tag))
    s_col = red if running else dim
    parts += [
        rev(bold("[l]egend")) if _view == "legend" else dim("[l]egend"),
        rev(bold("[h]ealth")) if _view == "health" else dim("[h]ealth"),
        rev(bold("[f]ields")) if _view == "fields" else dim("[f]ields"),
        cyan("[t]ips on") if _show_hints else dim("[t]ips off"),
        green("[c]ontinue"),
        cyan("[g]raceful"),
        yellow("[w]ipe"),
        s_col("[s]top"),
        red("[p]⏻armed") if _shutdown_armed() else dim("[p]oweroff"),
        cyan("[a]uto ON") if _auto_armed() else dim("[a]uto"),
        dim("[↑↓/PgUp/PgDn]"),
        dim("[q]uit"),
    ]
    legend = "  ".join(parts)
    msg = f"  {_action_msg}" if _action_msg else ""
    return [msg, legend]

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


def _log_lines(log_path, tail_bytes: int = 2 * 1024 * 1024):
    """Return word-wrapped lines from a log file (last tail_bytes only)."""
    w = _w()
    try:
        with open(log_path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - tail_bytes))
            raw_bytes = fh.read()
        raw_str = raw_bytes.decode("utf-8", errors="replace")
        raw = raw_str.splitlines()
        if size > tail_bytes:
            raw = raw[1:]  # drop partial first line
    except FileNotFoundError:
        return [red(f"  {log_path} not found")]
    except Exception as e:
        return [red(f"  error: {e}")]
    out = []
    for line in raw:
        if len(line) <= w:
            out.append(line)
        else:
            for i in range(0, len(line), w):
                out.append(line[i:i+w])
    return out


def _event_lines(events_path: str, tail_bytes: int = 1024 * 1024) -> list[str]:
    """Word-wrap formatted events from the tail of events.jsonl."""
    w = _w()
    try:
        with open(events_path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - tail_bytes))
            raw_bytes = fh.read()
        text = raw_bytes.decode("utf-8", errors="replace")
        raw = text.splitlines()
        if size > tail_bytes:
            raw = raw[1:]  # drop partial first line
    except FileNotFoundError:
        return [red(f"  {events_path} not found")]
    except Exception as e:
        return [red(f"  error: {e}")]

    out: list[str] = []
    import json as _json
    for line in raw:
        if not line.strip():
            continue
        try:
            ev = _json.loads(line)
        except Exception:
            out.append(dim(line[:w]))
            continue
        formatted = _event_format(ev)
        plain_len = len(_plain(formatted))
        if plain_len <= w:
            out.append(formatted)
        else:
            # Naive wrap on plain length — color spans rarely break mid-token in practice.
            for i in range(0, len(formatted), w):
                out.append(formatted[i:i + w])
    return out


def _event_box(events_path: str, view_key: str, title: str, inner: int, header=()):
    """Render an events.jsonl viewer with the same scroll semantics as _log_box."""
    w = _w() - 2
    scroll_inner = max(1, inner - len(header))

    lines  = _event_lines(events_path)
    total  = len(lines)
    offset = min(_scroll[view_key], max(0, total - scroll_inner))
    _scroll[view_key] = offset

    end   = total - offset
    start = max(0, end - scroll_inner)
    view  = lines[start:end]

    if total > scroll_inner:
        pct       = int(100 * end / total)
        s_plain   = f" {pct}%  end" if offset == 0 else f" {pct}%  ↑{offset}"
        s_display = dim(s_plain)
    else:
        s_plain, s_display = "", ""

    dashes = max(0, w - len(title) - len(s_plain) - 3)
    _ln(f"┌─ {bold(title)}{s_display} {'─' * dashes}┐")
    for h_line in header:
        pad = w - 1 - len(_plain(h_line))
        _ln(f"│ {h_line}{' ' * max(pad, 0)}│")
    for ln in view:
        pad = w - 1 - len(_plain(ln))
        _ln(f"│ {ln}{' ' * max(pad, 0)}│")
    for _ in range(scroll_inner - len(view)):
        _ln(f"│{' ' * w}│")
    _ln("└" + "─" * w + "┘")


def _log_box(log_path, view_key, title, inner, header=()):
    """Render an accordion log box with exactly `inner` content rows into _buf.

    `header` rows are fixed (not scrollable) at the top of the box content.
    The scrollable region gets inner - len(header) rows.
    """
    w = _w() - 2
    scroll_inner = max(1, inner - len(header))

    lines  = _log_lines(log_path)
    total  = len(lines)
    offset = min(_scroll[view_key], max(0, total - scroll_inner))
    _scroll[view_key] = offset

    end   = total - offset
    start = max(0, end - scroll_inner)
    view  = lines[start:end]

    if total > scroll_inner:
        pct         = int(100 * end / total)
        s_plain     = f" {pct}%  end" if offset == 0 else f" {pct}%  ↑{offset}"
        s_display   = dim(s_plain)
    else:
        s_plain, s_display = "", ""

    dashes = max(0, w - len(title) - len(s_plain) - 3)
    _ln(f"┌─ {bold(title)}{s_display} {'─' * dashes}┐")
    for h_line in header:
        pad = w - 1 - len(_plain(h_line))
        _ln(f"│ {h_line}{' ' * max(pad, 0)}│")
    for ln in view:
        pad = w - 1 - len(_plain(ln))
        _ln(f"│ {ln}{' ' * max(pad, 0)}│")
    for _ in range(scroll_inner - len(view)):   # pad to fill exact height
        _ln(f"│{' ' * w}│")
    _ln("└" + "─" * w + "┘")


def _render_all():
    h = _h()

    # ── Box 1: GPU (always compact) ──
    gpu_rows = gpu()
    for row in box_lines("GPU", gpu_rows):
        _ln(row)
    gpu_h = len(gpu_rows) + 2

    # ── Boxes 2 & 3: layout depends on view ──
    pipeline_rows = pipeline_status()
    gen_rows      = generation()
    pipeline_h    = len(pipeline_rows) + 2
    gen_h         = len(gen_rows) + 2

    if _view == "monitor":
        for row in box_lines("Pipeline", pipeline_rows):
            _ln(row)
        for row in box_lines("Generation", gen_rows):
            _ln(row)
        if _show_hints:
            _plain_text = "\n".join(_plain(r) for r in pipeline_rows + gen_rows)
            _hints = _hint_lines(_plain_text, _w() - 4)
            if _hints:
                for row in box_lines("field hints — [t] hide, [f] full glossary", [dim(hh) for hh in _hints]):
                    _ln(row)
        while len(_buf) < h - BOTTOM_LINES:
            _ln()

    elif _view == "pipeline":
        # Box 2 expands; Box 3 compact at bottom
        inner = max(1, h - gpu_h - gen_h - 2 - BOTTOM_LINES)
        _log_box(PIPELINE_LOG, "pipeline", "Pipeline Log", inner,
                 header=(pipeline_rows[:1]))
        for row in box_lines("Generation", gen_rows):
            _ln(row)

    elif _view == "stream":
        # Box 2 compact; Box 3 expands
        for row in box_lines("Pipeline", pipeline_rows):
            _ln(row)
        inner = max(1, h - gpu_h - pipeline_h - 2 - BOTTOM_LINES)
        _log_box(STREAM_LOG, "stream", "Stream Log", inner,
                 header=(gen_rows[:1]))

    elif _view == "events":
        # Box 2 compact; events box expands at the bottom.
        for row in box_lines("Pipeline", pipeline_rows):
            _ln(row)
        inner = max(1, h - gpu_h - pipeline_h - 2 - BOTTOM_LINES)
        _event_box(EVENTS_LOG, "events", "Events (logs/events.jsonl)", inner,
                   header=(pipeline_rows[:1] if pipeline_rows else ()))

    elif _view in ("health", "legend", "fields"):
        inner = max(1, h - gpu_h - 2 - BOTTOM_LINES)
        if _view == "health":
            title, rows = ("Run health (60s cache) — tools/run_health.py",
                           _health_lines(PIPELINE_LOG,
                                         SCRIPT_DIR + "/tmp/anyhow")[:inner])
        elif _view == "fields":
            title, rows = ("Field glossary — what each stat/label on screen means",
                            _field_glossary_window(_w() - 4, inner, _scroll["fields"]))
        else:
            title, rows = ("Legend — what each phase performs",
                           _legend_window(_w() - 4, inner, _scroll["legend"]))
        for row in box_lines(title, rows):
            _ln(row)
        while len(_buf) < h - BOTTOM_LINES:
            _ln()

    for row in _bottom_bar():
        _ln(row)

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
    global _action_msg, _view, _confirm, _graceful_pending, _show_hints
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
            global _graceful_pending
            _graceful_pending = True
            open("/tmp/pipeline_graceful_stop", "w").close()
            _detach(["bash", os.path.join(SCRIPT_DIR, "continue.sh"), "--graceful"])
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
                os.remove("/tmp/pipeline_graceful_stop")
            _action_msg = dim("graceful cancelled")
        elif _pipeline_running():
            _confirm    = "graceful"
            _action_msg = cyan("◑ graceful continue — finish current attempt then reload — confirm? [y/n]")
        else:
            _detach(["bash", os.path.join(SCRIPT_DIR, "continue.sh"), "--y"])
            _action_msg = green("▶ continue.sh launched (pipeline was not running)")
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
    elif key == "f":
        _view = "monitor" if _view == "fields" else "fields"
    elif key == "t":
        _show_hints = not _show_hints
    elif key == "a":
        _set_auto_armed(not _auto_armed())
        _action_msg = (green("[a]uto mode ON — pipeline will fire operator_run.sh on stall")
                      if _auto_armed() else dim("[a]uto mode off"))
    elif key in ("1", "2", "3", "4"):
        _view = {"1": "monitor", "2": "pipeline", "3": "stream", "4": "events"}[key]
    elif key == "UP"   and _view in _scroll: _scroll[_view] += SCROLL_STEP
    elif key == "DOWN" and _view in _scroll: _scroll[_view] = max(0, _scroll[_view] - SCROLL_STEP)
    elif key == "PGUP" and _view in _scroll: _scroll[_view] += available_lines()
    elif key == "PGDN" and _view in _scroll: _scroll[_view] = max(0, _scroll[_view] - available_lines())

def available_lines():
    h = _h()
    gpu_h = len(gpu()) + 2
    if _view == "pipeline":
        gen_h = len(generation()) + 2
        return max(1, h - gpu_h - gen_h - 2 - BOTTOM_LINES)
    elif _view == "stream" or _view == "events":
        p_h = len(pipeline_status()) + 2
        return max(1, h - gpu_h - p_h - 2 - BOTTOM_LINES)
    elif _view in ("legend", "fields"):
        return max(1, h - gpu_h - 2 - BOTTOM_LINES)
    return 1

# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    global _action_msg, _term_settings, _graceful_pending, _view
    signal.signal(signal.SIGHUP, signal.SIG_IGN)   # survive terminal events from child procs
    old_settings = termios.tcgetattr(sys.stdin)
    _term_settings = old_settings
    sys.stdout.write("\033[2J\033[?25l")
    sys.stdout.flush()
    _demo_idx = 0
    _demo_next = time.monotonic() + DEMO_INTERVAL
    try:
        tty.setcbreak(sys.stdin.fileno())
        while True:
            # --demo: advance to the next of views 1/2/3 every DEMO_INTERVAL s.
            if _DEMO and time.monotonic() >= _demo_next:
                _demo_idx = (_demo_idx + 1) % len(_DEMO_VIEWS)
                _view = _DEMO_VIEWS[_demo_idx]
                _demo_next = time.monotonic() + DEMO_INTERVAL
            try:
                _render_all()
                _flush_frame()
            except Exception as e:
                _buf.clear()
                _ln(red(f"  render error: {e}"))
                _flush_frame()

            if _graceful_pending:
                if not os.path.exists("/tmp/pipeline_graceful_stop"):
                    _graceful_pending = False
                    _action_msg = ""
                else:
                    _action_msg = bg_white(" ◑ graceful — waiting for pipeline to restart… ")
            elif _confirm == "continue":
                _action_msg = green("▶ continue (will kill running pipeline) — confirm? [y/n]")
            elif _confirm == "graceful":
                _action_msg = cyan("◑ graceful continue — finish current attempt then reload — confirm? [y/n]")
            elif _confirm == "wipe":
                _action_msg = yellow("↺ wipe all generated code and restart — confirm? [y/n]")
            elif _confirm == "wipe-augmented":
                _action_msg = yellow(
                    "↺ augmentation found: [a]ugmentation restore, "
                    "[h]ard reset, [other] cancel"
                )
            elif _confirm == "stop":
                _action_msg = red("■ stop pipeline — confirm? [y/n]")
            elif _confirm == "poweroff":
                _action_msg = red("⏻ power off machine when the run finishes — confirm? [y/n]")
            else:
                _action_msg = ""

            try:
                # In demo mode wake exactly at the next view switch (≤ REFRESH)
                # so cycling stays crisp while the panel still refreshes live.
                timeout: float = REFRESH
                if _DEMO:
                    timeout = max(0.1, min(float(REFRESH), _demo_next - time.monotonic()))
                ready, _, _ = select.select([sys.stdin], [], [], timeout)
                if ready:
                    key = _read_key()
                    if key == "q":
                        raise KeyboardInterrupt
                    _run_action(key)
            except Exception:
                pass
    except KeyboardInterrupt:
        pass
    finally:
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
