#!/usr/bin/env python3
"""Pipeline / GPU monitor.  Keys: c/r/s/l/g/m  ↑↓ scroll  ctrl+c quit"""
import os, re, select, signal, subprocess, sys, termios, time, tty
from datetime import datetime

PIPELINE_LOG = "/tmp/pipeline_anyhow.log"
STREAM_LOG   = os.getenv("LLM_STREAM_LOG", os.getenv("OLLAMA_STREAM_LOG", "/tmp/stream.log"))
REFRESH      = 2

_GPU_BW = {"3090": 936.2, "3080": 760.0, "4090": 1008.0, "4080": 716.8,
           "A100": 2000.0, "H100": 3350.0}

# ── ANSI helpers ──────────────────────────────────────────────────────────────

def _w():
    try: return os.get_terminal_size().columns
    except: return 80

def _h():
    try: return os.get_terminal_size().lines
    except: return 24

def bold(s):     return f"\033[1m{s}\033[0m"
def dim(s):      return f"\033[2m{s}\033[0m"
def underline(s):return f"\033[4m{s}\033[0m"
def green(s):    return f"\033[32m{s}\033[0m"
def red(s):      return f"\033[31m{s}\033[0m"
def cyan(s):     return f"\033[36m{s}\033[0m"
def yellow(s):   return f"\033[33m{s}\033[0m"
def rev(s):      return f"\033[7m{s}\033[0m"   # reverse video (highlight)

def bar(pct, width=18):
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)

def _plain(s):
    return re.sub(r'\033\[[0-9;]*[a-zA-Z]', '', s)

# ── Flicker-free diff renderer ────────────────────────────────────────────────

_buf: list[str] = []
_prev_buf: list[str] = []

def _ln(s=""):
    _buf.append(s)

def _flush_frame():
    global _prev_buf
    out = []
    h = _h()
    for i, line in enumerate(_buf[:h]):
        prev = _prev_buf[i] if i < len(_prev_buf) else None
        if line != prev:
            out.append(f"\033[{i+1};1H{line}\033[K")
    for i in range(len(_buf), min(len(_prev_buf), h)):
        out.append(f"\033[{i+1};1H\033[K")
    if out:
        sys.stdout.write("".join(out))
        sys.stdout.flush()
    _prev_buf = list(_buf)
    _buf.clear()

# ── Box helpers ───────────────────────────────────────────────────────────────

def box(title, rows):
    w = _w() - 2
    _ln(f"┌─ {bold(title)} {'─' * max(0, w - len(title) - 4)}┐")
    for row in rows:
        pad = w - 2 - len(_plain(row))
        _ln(f"│ {row}{' ' * max(pad - 1, 0)}│")
    _ln("└" + "─" * w + "┘")

def box_lines(title, rows):
    """Same as box() but returns list of strings without printing."""
    w = _w() - 2
    out = [f"┌─ {bold(title)} {'─' * max(0, w - len(title) - 4)}┐"]
    for row in rows:
        pad = w - 2 - len(_plain(row))
        out.append(f"│ {row}{' ' * max(pad - 1, 0)}│")
    out.append("└" + "─" * w + "┘")
    return out

# ── Key reading (handles arrow escape sequences) ──────────────────────────────

def _read_key():
    """Read one logical keypress; returns a string. Arrow keys → 'UP'/'DOWN'/'PGUP'/'PGDN'."""
    if not select.select([sys.stdin], [], [], 0)[0]:
        return ""
    ch = sys.stdin.read(1)
    if ch != "\033":
        return ch
    # Escape sequence — read more with short timeout
    if not select.select([sys.stdin], [], [], 0.05)[0]:
        return "ESC"
    ch2 = sys.stdin.read(1)
    if ch2 != "[":
        return "ESC"
    if not select.select([sys.stdin], [], [], 0.05)[0]:
        return "ESC"
    ch3 = sys.stdin.read(1)
    if ch3 == "A":  return "UP"
    if ch3 == "B":  return "DOWN"
    if ch3 == "5":
        sys.stdin.read(1)   # consume trailing ~
        return "PGUP"
    if ch3 == "6":
        sys.stdin.read(1)
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


def pipeline_status():
    try:
        subprocess.check_output(["pgrep", "-f", "pipeline.py"], stderr=subprocess.DEVNULL)
        running = True
    except subprocess.CalledProcessError:
        running = False
    status_str = green("RUNNING") if running else red("STOPPED")
    if not os.path.exists(PIPELINE_LOG):
        return [f"{status_str}   {red('log not found')}"]
    with open(PIPELINE_LOG) as f:
        content = f.read()
    lines = content.splitlines()
    coverage, attempt, module, cur_file = "?", "?", "?", "?"
    errors = 0
    for line in lines:
        if m := re.search(r'Coverage:\s*([\d.]+)%', line):      coverage = m.group(1) + "%"
        if m := re.search(r'Attempt\s+(\d+/\d+)', line):        attempt  = m.group(1)
        if m := re.search(r'MODULE:\s*(\S+)', line):             module   = m.group(1)
        if m := re.search(r'\[task0c planner\]\s+(\S+):', line): cur_file = m.group(1)
        if "Compile error" in line:                              errors  += 1
    age   = time.time() - os.path.getmtime(PIPELINE_LOG)
    age_s = f"{int(age)}s" if age < 60 else f"{int(age/60)}m{int(age%60)}s"
    last  = next((l.strip() for l in reversed(lines) if l.strip()), "")
    w     = _w() - 6
    cov_col = green if coverage != "?" and float(coverage[:-1]) >= 80 else yellow
    return [
        f"{status_str}   module={bold(module)}   attempt={attempt}   "
        f"coverage={cov_col(coverage)}   errors={errors}",
        f"file={cyan(cur_file)}   log-age={age_s}",
        dim((last[:w] + "…") if len(last) > w else last),
    ]


_prev_len        = 0
_THINK_CAP_CHARS = 40_000

def _parse_max_tokens():
    try:
        with open("/tmp/pipeline_guards.log") as f:
            lines = f.readlines()
        for line in reversed(lines):
            if m := re.search(r'max_tokens \d+ → (\d+)', line):
                return int(m.group(1))
    except Exception:
        pass
    return None

def _parse_vllm_cpt():
    retries, cpt = 0, None
    try:
        with open("/tmp/pipeline_guards.log") as f:
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
    if m := re.search(r'\[CALL_START: ([\d.]+)\]', content):
        started = float(m.group(1))
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
    progress_rows = []
    if in_think:
        tc   = max(0, cur_chars - content.find('<think>') - 7)
        pct  = min(100, tc / _THINK_CAP_CHARS * 100)
        eta  = _fmt_eta(max(0, _THINK_CAP_CHARS - tc) / smooth_rate) if smooth_rate > 0.1 else "?"
        progress_rows.append(
            f"  think [{bar(pct, 20)}] {pct:4.0f}%  {tc:,}/{_THINK_CAP_CHARS:,} chars"
            f"  {rate_str}  ETA {eta}  elapsed {ela_s}")
    else:
        max_tok = _parse_max_tokens()
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
    dlines = [l for l in display.splitlines() if l.strip()][-5:]
    cpt, retries = _parse_vllm_cpt()
    cpt_s    = f"cpt={cpt:.2f}" if cpt else dim("cpt=?")
    retry_s  = f"  {red(f'400-retries={retries}')}" if retries else ""
    rows  = [f"phase={phase}   {cur_chars:,} chars   last-tok {age_s} ago   {rate_str}   {cpt_s}{retry_s}"]
    rows += progress_rows
    rows += [dim((l[:w-2] + "…") if len(l) > w else l) for l in dlines] or [dim("(empty)")]
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
_view        = "monitor"      # "monitor" | "pipeline" | "stream"
_scroll      = {"pipeline": 0, "stream": 0}   # lines scrolled up from bottom (0=bottom)
_confirm     = None           # pending confirmation: "continue" | "restart" | "stop" | None

VIEWS = [
    ("monitor",  "m", "Monitor"),
    ("pipeline", "l", "Pipeline Log"),
    ("stream",   "g", "Stream Log"),
]

# ── Always-visible bottom bar ─────────────────────────────────────────────────
# 3 lines: divider, tab row, action row

def _bottom_bar():
    w = _w()
    running = True
    try:
        subprocess.check_output(["pgrep", "-f", "pipeline.py"], stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        running = False

    # Tab row
    tabs = []
    for vid, key, label in VIEWS:
        tag = f" [{key}] {label} "
        tabs.append(rev(bold(tag)) if vid == _view else dim(f" [{key}] {label} "))
    tab_row = "  ".join(tabs)

    # Action row
    s_label = red("[s] stop") if running else dim("[s] stop")
    scroll_hint = dim("  ↑↓ scroll") if _view in ("pipeline", "stream") else ""
    actions = f"  {green('[c] continue')}  {yellow('[r] restart')}  {s_label}{scroll_hint}  {dim('[ctrl+c] quit')}"

    feedback = f"  {_action_msg}" if _action_msg else ""

    return ["─" * w, tab_row + feedback, actions]

# ── Views ─────────────────────────────────────────────────────────────────────

BOTTOM_LINES = 3   # divider + tabs + actions

def _render_monitor():
    now = datetime.now().strftime("%H:%M:%S")
    _ln(f" {bold('Pipeline Monitor')}  {dim(now)}")
    _ln()
    box("GPU", gpu())
    _ln()
    box("Pipeline", pipeline_status())
    _ln()
    box("Generation", generation())
    # pad to keep bottom bar anchored
    h = _h()
    while len(_buf) < h - BOTTOM_LINES:
        _ln()
    for row in _bottom_bar():
        _ln(row)


def _log_lines(log_path):
    """Return word-wrapped lines from a log file."""
    w = _w()
    try:
        with open(log_path) as f:
            raw = f.read().splitlines()
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


def _render_log_view(log_path, view_key):
    gpu_strip  = box_lines("GPU", gpu())
    # header(1) + blank(1) + gpu_strip + blank(1) + bottom_bar(BOTTOM_LINES)
    overhead   = 1 + 1 + len(gpu_strip) + 1 + BOTTOM_LINES
    available  = max(2, _h() - overhead)

    now = datetime.now().strftime("%H:%M:%S")
    _ln(f" {bold(VIEWS[[v[0] for v in VIEWS].index(view_key)][2])}  {dim(now)}")
    _ln()
    for row in gpu_strip:
        _ln(row)
    _ln()

    lines  = _log_lines(log_path)
    total  = len(lines)
    offset = min(_scroll[view_key], max(0, total - available))
    _scroll[view_key] = offset          # clamp in case file shrank

    # offset=0 → show tail; offset>0 → scroll up by that many lines
    end   = total - offset
    start = max(0, end - available)
    view  = lines[start:end]

    # scroll indicator
    if total > available:
        pct = int(100 * end / total)
        indicator = dim(f" ↑ {offset} lines above  ({pct}%)")
    else:
        indicator = dim(" (all lines visible)")

    _ln(indicator)
    for l in view:
        _ln(l)

    # pad to keep bottom bar anchored
    h = _h()
    while len(_buf) < h - BOTTOM_LINES:
        _ln()
    for row in _bottom_bar():
        _ln(row)

# ── Actions ───────────────────────────────────────────────────────────────────

SCROLL_STEP = 5

def _run_action(key):
    global _action_msg, _view, _confirm
    if _confirm == "restart":
        if key == "y":
            _detach(["bash", os.path.join(SCRIPT_DIR, "restart.sh"), "--y"])
            _action_msg = yellow("↺ restart.sh launched")
        else:
            _action_msg = dim("restart cancelled")
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
    if key == "c":
        if _pipeline_running():
            _confirm    = "continue"
            _action_msg = green("▶ continue (will kill running pipeline) — confirm? [y/n]")
        else:
            _detach(["bash", os.path.join(SCRIPT_DIR, "continue.sh"), "--y"])
            _action_msg = green("▶ continue.sh launched")
    elif key == "r":
        _confirm    = "restart"
        _action_msg = yellow("↺ restart — confirm? [y/n]")
    elif key == "s":
        _confirm    = "stop"
        _action_msg = red("■ stop pipeline — confirm? [y/n]")
    elif key in ("m", "l", "g"):
        _view = {"m": "monitor", "l": "pipeline", "g": "stream"}[key]
    elif key == "UP"   and _view in _scroll: _scroll[_view] += SCROLL_STEP
    elif key == "DOWN" and _view in _scroll: _scroll[_view] = max(0, _scroll[_view] - SCROLL_STEP)
    elif key == "PGUP" and _view in _scroll: _scroll[_view] += available_lines()
    elif key == "PGDN" and _view in _scroll: _scroll[_view] = max(0, _scroll[_view] - available_lines())

def available_lines():
    gpu_strip = box_lines("GPU", gpu())
    return max(2, _h() - (1 + 1 + len(gpu_strip) + 1 + BOTTOM_LINES))

# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    global _action_msg
    signal.signal(signal.SIGHUP, signal.SIG_IGN)   # survive terminal events from child procs
    old_settings = termios.tcgetattr(sys.stdin)
    sys.stdout.write("\033[2J\033[?25l")
    sys.stdout.flush()
    try:
        tty.setcbreak(sys.stdin.fileno())
        while True:
            try:
                if _view == "pipeline":
                    _render_log_view(PIPELINE_LOG, "pipeline")
                elif _view == "stream":
                    _render_log_view(STREAM_LOG, "stream")
                else:
                    _render_monitor()
                _flush_frame()
            except Exception as e:
                _buf.clear()
                _ln(red(f"  render error: {e}"))
                _flush_frame()

            if _confirm == "restart":
                _action_msg = yellow("↺ restart — confirm? [y/n]")
            elif _confirm == "stop":
                _action_msg = red("■ stop pipeline — confirm? [y/n]")
            else:
                _action_msg = ""

            try:
                ready, _, _ = select.select([sys.stdin], [], [], REFRESH)
                if ready:
                    _run_action(_read_key())
            except Exception:
                pass
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        sys.stdout.write("\033[?25h\033[2J\033[H\n")
        sys.stdout.flush()

if __name__ == "__main__":
    main()
