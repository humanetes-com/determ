#!/usr/bin/env python3
"""Pipeline / Ollama / GPU monitor — refreshes every 2s, Ctrl+C to quit."""
import json, os, re, select, subprocess, sys, termios, time, tty, urllib.request
from datetime import datetime

PIPELINE_LOG = "/tmp/pipeline_anyhow.log"
STREAM_LOG   = os.getenv("LLM_STREAM_LOG", os.getenv("OLLAMA_STREAM_LOG", "/tmp/stream.log"))
OLLAMA_URL   = "http://localhost:11434"
REFRESH      = 2

# Peak memory bandwidth (GB/s) per card — used for tokens/s estimate.
_GPU_BW = {"3090": 936.2, "3080": 760.0, "4090": 1008.0, "4080": 716.8,
           "A100": 2000.0, "H100": 3350.0}

# ── ANSI helpers ──────────────────────────────────────────────────────────────

def _w():
    try: return os.get_terminal_size().columns
    except: return 80

def _home():  print("\033[H", end="")          # move to top-left, no erase
def _erase():  print("\033[J", end="", flush=True)  # clear from cursor to screen bottom
def bold(s):  return f"\033[1m{s}\033[0m"
def dim(s):   return f"\033[2m{s}\033[0m"
def green(s): return f"\033[32m{s}\033[0m"
def red(s):   return f"\033[31m{s}\033[0m"
def cyan(s):  return f"\033[36m{s}\033[0m"
def yellow(s):return f"\033[33m{s}\033[0m"

def bar(pct, width=18):
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)

def _ln(s=""):
    """Print a line, erasing any leftover characters from the previous frame."""
    print(s + "\033[K")

def box(title, rows):
    w = _w() - 2
    _ln(f"┌─ {bold(title)} {'─' * (w - len(title) - 4)}┐")
    for row in rows:
        plain = re.sub(r'\033\[[0-9;]*m', '', row)
        pad   = w - 2 - len(plain)
        _ln(f"│ {row}{' ' * max(pad - 1, 0)}│")
    _ln("└" + "─" * w + "┘")

# ── Data collectors ───────────────────────────────────────────────────────────

def _ollama_ps():
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/ps", timeout=2) as r:
            return json.loads(r.read()).get("models", [])
    except Exception:
        return None  # unreachable

def _ollama_parallel():
    """Read OLLAMA_NUM_PARALLEL from the running ollama serve process."""
    try:
        pid = subprocess.check_output(
            ["pgrep", "-x", "ollama"], text=True, stderr=subprocess.DEVNULL
        ).split()[0]
        env = open(f"/proc/{pid}/environ").read().split("\0")
        for e in env:
            if e.startswith("OLLAMA_NUM_PARALLEL="):
                return int(e.split("=", 1)[1])
    except Exception:
        pass
    return 1  # Ollama default

def ollama():
    models = _ollama_ps()
    if models is None:
        return [red("unreachable")]
    if not models:
        return [dim("no model loaded")]

    m    = models[0]
    name = m.get("name", "?")
    ctx  = m.get("context_length", 0)
    exp  = m.get("expires_at", "")[:19].replace("T", " ")
    vram = m.get("size_vram", 0)

    try:
        ss    = subprocess.check_output(["ss", "-tnp"], text=True, stderr=subprocess.DEVNULL)
        conns = ss.count(":11434") - 1
    except Exception:
        conns = "?"

    parallel   = _ollama_parallel()
    generating = isinstance(conns, int) and conns > 2
    status_str = green("GENERATING") if generating else dim("idle")
    vram_gb    = f"{vram/1024**3:.1f} GB" if vram else "?"

    return [
        f"{bold(name)}   ctx={ctx:,}   vram={vram_gb}   {status_str}",
        f"parallel-slots={parallel}   conns={conns}   {dim('expires ' + exp)}",
    ]

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

    rows      = []
    bw_sum    = 0.0
    mem_gb    = 0.0
    num_gpus  = len(raw)

    for line in raw:
        idx, name, util, mem_bw, used, total, temp = [p.strip() for p in line.split(",", 6)]
        util_i   = int(util)
        mem_bw_i = int(mem_bw)
        col      = green if util_i > 60 else yellow if util_i > 20 else dim
        mem_col  = green if mem_bw_i > 60 else yellow if mem_bw_i > 20 else dim
        rows.append(
            f"GPU{idx}  [{col(bar(util_i))}] {util_i:3}%  "
            f"MEM-BW {mem_col(f'{mem_bw_i:3}%')}  "
            f"VRAM {int(used)/1024:.1f}/{int(total)/1024:.1f} GB  "
            f"Temp {temp}°C"
        )
        card_bw = next((v for k, v in _GPU_BW.items() if k in name), None)
        if card_bw:
            bw_sum += card_bw * mem_bw_i / 100
        # accumulate model VRAM from /api/ps for tokens/s denominator
        if not mem_gb:
            models = _ollama_ps()
            if models:
                mem_gb = models[0].get("size_vram", 0) / 1024**3

    # tokens/s estimate: actual_BW_GBs / model_size_GB
    if bw_sum > 0 and mem_gb > 0:
        toks = bw_sum / mem_gb
        rows.append(f"  {bold('≈ tokens/s:')} {toks:.1f}  "
                    f"{dim(f'({bw_sum:.0f} GB/s used / {mem_gb:.1f} GB model)')}")

    rows.append(dim("  legend: util/mem-bw  ") + green("■ >60% active") + dim("  ") + yellow("■ 20–60% partial") + dim("  □ <20% idle"))
    return rows

def pipeline():
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
        if m := re.search(r'Coverage:\s*([\d.]+)%', line):     coverage  = m.group(1) + "%"
        if m := re.search(r'Attempt\s+(\d+/\d+)', line):       attempt   = m.group(1)
        if m := re.search(r'MODULE:\s*(\S+)', line):            module    = m.group(1)
        if m := re.search(r'\[task0c planner\]\s+(\S+):', line):cur_file  = m.group(1)
        if "Compile error" in line:                             errors   += 1

    age  = time.time() - os.path.getmtime(PIPELINE_LOG)
    age_s = f"{int(age)}s" if age < 60 else f"{int(age/60)}m{int(age%60)}s"

    last = next((l.strip() for l in reversed(lines) if l.strip()), "")
    w = _w() - 6
    last_display = (last[:w] + "…") if len(last) > w else last

    cov_col = green if coverage != "?" and float(coverage[:-1]) >= 80 else yellow
    return [
        f"{status_str}   module={bold(module)}   attempt={attempt}   "
        f"coverage={cov_col(coverage)}   compile-errors={errors}",
        f"file={cyan(cur_file)}   log-age={age_s}",
        dim(last_display),
    ]

# ── Stream log reader ─────────────────────────────────────────────────────────
# model_interact.py writes /tmp/stream.log (overwrite per call).
# Any number of readers — monitor, logger, debugger — can open the same file
# simultaneously without socket-ownership conflicts.

_prev_len        = 0
_THINK_CAP_CHARS = 40_000  # must match _MAX_THINK_CHARS in model_interact.py


def _parse_max_tokens():
    """Most recent effective max_tokens from guards log (after cap)."""
    try:
        with open("/tmp/pipeline_guards.log") as f:
            lines = f.readlines()
        for line in reversed(lines):
            m = re.search(r'max_tokens \d+ → (\d+)', line)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return None


def _parse_vllm_cpt():
    """Most recent adaptive chars-per-token EWM value and 400-retry count."""
    retries = 0
    cpt = None
    try:
        with open("/tmp/pipeline_guards.log") as f:
            lines = f.readlines()
        for line in lines:
            if "vllm/400_retry" in line:
                retries += 1
        for line in reversed(lines):
            # cpt_update: "ewm=2.00→2.36"  or 400_retry: "cpt_ewm=2.00→2.36"
            m = re.search(r'(?:ewm|cpt_ewm)=[\d.]+→([\d.]+)', line)
            if m:
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
        stat    = os.stat(STREAM_LOG)
        mtime   = stat.st_mtime
        with open(STREAM_LOG) as f:
            content = f.read()
    except FileNotFoundError:
        return [dim("no stream log yet — waiting for pipeline…")]
    except Exception as e:
        return [red(f"stream log error: {e}")]

    if not content.strip():
        return [dim("stream log empty")]

    now = time.time()
    age = now - mtime
    age_s = f"{int(age)}s" if age < 60 else f"{int(age/60)}m{int(age%60):02d}s"

    # Parse call start timestamp written by model_interact at the top of each call
    started = 0.0
    m = re.search(r'\[CALL_START: ([\d.]+)\]', content)
    if m:
        started = float(m.group(1))
    elapsed = max(1.0, now - started) if started > 0 else max(1.0, age)
    ela_s   = f"{int(elapsed/60)}m{int(elapsed%60):02d}s" if elapsed >= 60 else f"{int(elapsed)}s"

    cur_chars   = len(content)
    inst_rate   = max(0, cur_chars - _prev_len) / REFRESH / 4  # tok/s this window
    smooth_rate = cur_chars / 4 / elapsed                       # tok/s since call start
    _prev_len   = cur_chars
    rate_str    = f"~{inst_rate:.0f} tok/s" if inst_rate > 0.5 else dim("no new tokens")

    # Phase detection: compat path uses <think>…</think>; native uses [CONTENT] marker
    in_think  = '<think>' in content and '</think>' not in content
    had_think = '</think>' in content
    if "[TIMEOUT" in content:
        phase = red("TIMEOUT")
    elif "[FOLLOWUP" in content:
        phase = cyan("FOLLOWUP")
    elif "[CONTENT]" in content:
        phase = green("CODE")
    elif in_think:
        phase = yellow("THINKING")
    elif had_think or ("[CALL:" in content and cur_chars > 60):
        phase = green("CODE")
    else:
        phase = yellow("THINKING")

    # Progress bars ─────────────────────────────────────────────────────────────
    progress_rows = []
    if in_think:
        think_start = content.find('<think>') + 7
        think_chars = max(0, cur_chars - think_start)
        think_pct   = min(100, think_chars / _THINK_CAP_CHARS * 100)
        cap_remain  = max(0, _THINK_CAP_CHARS - think_chars)
        eta_think   = _fmt_eta(cap_remain / smooth_rate) if smooth_rate > 0.1 else "?"
        progress_rows.append(
            f"  think  [{bar(think_pct, 20)}] {think_pct:4.0f}%"
            f"  {think_chars:,} / {_THINK_CAP_CHARS:,} chars"
            f"  {rate_str}  cap-ETA {eta_think}  elapsed {ela_s}"
        )
    else:
        max_tok    = _parse_max_tokens()
        tokens_gen = cur_chars / 4
        if max_tok:
            pct     = min(100, tokens_gen / max_tok * 100)
            tok_rem = max(0, max_tok - tokens_gen)
            eta_s   = _fmt_eta(tok_rem / smooth_rate) if smooth_rate > 0.1 else "?"
            progress_rows.append(
                f"  budget [{bar(pct, 20)}] {pct:4.0f}%"
                f"  {tokens_gen:,.0f} / {max_tok:,} tok"
                f"  avg {smooth_rate:.1f} tok/s  ETA ≤ {eta_s}  elapsed {ela_s}"
            )

    # Show only content after the last section boundary
    display = content
    for marker in ("[FOLLOWUP", "[CONTENT]", "</think>"):
        if marker in display:
            display = display.split(marker)[-1].lstrip("\n")
            break

    w     = _w() - 4
    lines = [l for l in display.splitlines() if l.strip()][-5:]
    _cpt, _retries = _parse_vllm_cpt()
    _cpt_str = (f"cpt={_cpt:.2f}" if _cpt else dim("cpt=?"))
    _retry_str = (f"  {red(f'400-retries={_retries}')}" if _retries else "")
    rows  = [f"phase={phase}   {cur_chars:,} chars recv   last-tok {age_s} ago   {rate_str}   {_cpt_str}{_retry_str}"]
    rows += progress_rows
    rows += [dim((l[:w - 2] + "…") if len(l) > w else l) for l in lines] or [dim("(empty)")]
    return rows

# ── Action menu ───────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

_action_msg = ""   # last action feedback, shown for one refresh cycle

def _read_key():
    """Return a single keypress without blocking, or '' if none available."""
    if select.select([sys.stdin], [], [], 0)[0]:
        return sys.stdin.read(1)
    return ""

def _run_action(key):
    global _action_msg
    if key == "c":
        subprocess.Popen(
            ["bash", os.path.join(SCRIPT_DIR, "continue.sh"), "--y"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _action_msg = green("▶ continue.sh launched")
    elif key == "r":
        subprocess.Popen(
            ["bash", os.path.join(SCRIPT_DIR, "restart.sh"), "--y"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _action_msg = yellow("↺ restart.sh launched")
    elif key == "s":
        try:
            subprocess.run(["pkill", "-f", "pipeline.py"], check=True)
            _action_msg = red("■ pipeline stopped")
        except subprocess.CalledProcessError:
            _action_msg = dim("pipeline was not running")

def _menu_row():
    running = True
    try:
        subprocess.check_output(["pgrep", "-f", "pipeline.py"], stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        running = False
    s_label = red("[s] stop") if running else dim("[s] stop")
    return f"  {green('[c] continue')}   {yellow('[r] restart')}   {s_label}   {dim('[ctrl+c] quit')}"

# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    old_settings = termios.tcgetattr(sys.stdin)
    print("\033[2J\033[?25l", end="", flush=True)  # clear once + hide cursor
    try:
        tty.setcbreak(sys.stdin.fileno())  # single-char input, no echo
        while True:
            _home()
            now = datetime.now().strftime("%H:%M:%S")
            _ln(f" {bold('Pipeline Monitor')}  {dim(now)}")
            _ln()
            box("Ollama", ollama())
            _ln()
            box("GPU", gpu())
            _ln()
            box("Pipeline", pipeline())
            _ln()
            box("Generation", generation())
            _ln()
            global _action_msg
            _ln(_menu_row())
            if _action_msg:
                _ln(f"  {_action_msg}")
                _action_msg = ""
            else:
                _ln()
            _erase()

            # Wait up to REFRESH seconds, wake immediately on keypress.
            ready, _, _ = select.select([sys.stdin], [], [], REFRESH)
            if ready:
                key = sys.stdin.read(1)
                _run_action(key)
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        print("\033[?25h\033[J\nBye.")  # restore cursor + clear below

if __name__ == "__main__":
    main()
