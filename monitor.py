#!/usr/bin/env python3
"""Pipeline / GPU monitor.  Keys: 1/2/3 views  c/g/r/s actions  ↑↓ navigate  q quit"""
import glob, json as _json, os, re, select, signal, subprocess, sys, termios, time, tty
from datetime import datetime

_LOG_DIR     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
PIPELINE_LOG = os.getenv("PIPELINE_LOG", os.path.join(_LOG_DIR, "pipeline_anyhow.log"))
STREAM_LOG   = os.getenv("LLM_STREAM_LOG", os.getenv("OLLAMA_STREAM_LOG", os.path.join(_LOG_DIR, "stream.log")))
_GUARDS_LOG  = os.getenv("PIPELINE_GUARD_LOG", os.path.join(_LOG_DIR, "pipeline_guards.log"))
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
def bg_white(s): return f"\033[30;107m{s}\033[0m"  # black text on bright white background

def bar(pct, width=18, label: str = ""):
    filled = round(pct / 100 * width)
    s = list("█" * filled + "░" * (width - filled))
    if label and len(label) <= width:
        start = 0
        for i, ch in enumerate(label):
            s[start + i] = ch
    return "".join(s)

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
    _ln(f"┌─ {bold(title)} {'─' * max(0, w - len(title) - 3)}┐")
    for row in rows:
        pad = w - 1 - len(_plain(row))
        _ln(f"│ {row}{' ' * max(pad, 0)}│")
    _ln("└" + "─" * w + "┘")

def box_lines(title, rows):
    """Same as box() but returns list of strings without printing."""
    w = _w() - 2
    out = [f"┌─ {bold(title)} {'─' * max(0, w - len(title) - 3)}┐"]
    for row in rows:
        pad = w - 1 - len(_plain(row))
        out.append(f"│ {row}{' ' * max(pad, 0)}│")
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


def _parse_coverage_json(project_dir):
    """Read per-file coverage from the most recent grcov/tarpaulin JSON output.
    Returns {rel_path: (covered, coverable)}, e.g. {'src/error.rs': (248, 285)}."""
    result: dict[str, tuple[int, int]] = {}
    main_mtime: float = 0.0

    # grcov coveralls JSON (pipeline writes /tmp/grcov_{module}.json).
    # Exclude quick/filecache variants — those are not full measurements.
    _main_paths = [p for p in glob.glob("/tmp/grcov_*.json")
                   if not os.path.basename(p).startswith(("grcov_quick_", "grcov_filecache_"))]
    for path in sorted(_main_paths, key=os.path.getmtime, reverse=True)[:1]:
        try:
            main_mtime = os.path.getmtime(path)
            data = _json.load(open(path))
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
    for _qpath in glob.glob("/tmp/grcov_filecache_*.json"):
        try:
            for _rel, _d in _json.load(open(_qpath)).items():
                if _d.get("ts", 0) > main_mtime and _d.get("coverable", 0) > 0:
                    result[_rel] = (_d["covered"], _d["coverable"])
        except Exception:
            pass

    if result:
        return result

    # tarpaulin JSON fallback (pipeline writes /tmp/tarp_{module}/tarpaulin-report.json)
    for path in sorted(glob.glob("/tmp/tarp_*/tarpaulin-report.json"), key=os.path.getmtime, reverse=True)[:1]:
        try:
            data = _json.load(open(path))
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


def _find_project_dir():
    """Return path to the most-recently-active workspace in tmp/, or None."""
    tmp = os.path.join(SCRIPT_DIR, "tmp")
    if not os.path.isdir(tmp):
        return None
    best = None
    for name in os.listdir(tmp):
        p = os.path.join(tmp, name, "pipeline_state.md")
        if os.path.isfile(p):
            mtime = os.path.getmtime(p)
            if best is None or mtime > best[0]:
                best = (mtime, os.path.join(tmp, name))
    return best[1] if best else None


def _parse_pipeline_state(project_dir):
    """Parse pipeline_state.md → cluster list, done set, current cluster, latest coverage."""
    clusters: list[str] = []
    done: set[str] = set()
    current = ""
    coverage = ""
    r: dict[str, object] = {"clusters": clusters, "done": done, "current": current, "coverage": coverage}
    if not project_dir:
        return r
    try:
        text = open(os.path.join(project_dir, "pipeline_state.md")).read()
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


def _parse_pipeline_log():
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

    def _r() -> dict[str, object]:
        return {
            "attempt": attempt, "max_attempt": max_attempt, "errors": errors,
            "current_file": current_file, "files_this_pass": files_this_pass,
            "all_files": all_files, "cluster_files": cluster_files,
            "file_history": file_history, "queue_history": queue_history,
            "current_cluster": current_cluster, "coverage": coverage,
            "last_line": last_line, "age": age,
            "quick_cov_history": quick_cov_history,
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
    last_line = next((l.strip() for l in reversed(lines) if l.strip()), "")
    this_pass: list[str] = []
    _cluster_files: list[str] = []
    for line in lines:
        if m := re.search(r'Attempt\s+(\d+)/(\d+)', line):
            attempt, max_attempt = m.group(1), m.group(2)
            this_pass = []
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


def pipeline_status():
    prog  = _parse_pipeline_log()
    state = _parse_pipeline_state(_find_project_dir())
    running   = _pipeline_running()
    status_str = green("RUNNING") if running else red("STOPPED")
    w = _w() - 4

    if not os.path.exists(PIPELINE_LOG):
        return [f"{status_str}   {red('log not found')}"]

    # Coverage: pipeline_state.md is authoritative (updated after every measurement);
    # fall back to log scan only if state has no value yet.
    cov = state["coverage"] or prog["coverage"] or "?"
    try:    cov_col = green if float(cov.rstrip("%")) >= 80 else yellow
    except: cov_col = dim

    age   = prog["age"]
    age_s = f"{int(age)}s" if age < 60 else f"{int(age/60)}m{int(age%60)}s"

    clusters = state["clusters"]
    done_set = state["done"]
    cur_cl   = state["current"] or prog["current_cluster"]

    rows = []

    # vLLM backend status (loading indicator / docker logs when not ready)
    rows.extend(_vllm_status())

    # Row 1: compact status line
    rows.append(
        f"{status_str}  {bold(cur_cl or '?')}  cov={cov_col(bold(cov))}  "
        f"attempt {prog['attempt']}/{prog['max_attempt']}  "
        f"age={age_s}  errors={prog['errors']}"
    )

    # Row 2: cluster symbols  ✓done  ▶current  ○pending
    if clusters:
        parts = []
        for name in clusters:
            if name in done_set:      parts.append(green(f"✓{name}"))
            elif name == cur_cl:      parts.append(yellow(f"▶{name}"))
            else:                     parts.append(dim(f"○{name}"))
        row2 = "  clusters  " + "  ".join(parts)
        if len(_plain(row2)) > w:
            # abbreviate to 8 chars per name
            parts = []
            for name in clusters:
                ab = name[:8]
                if name in done_set:   parts.append(green(f"✓{ab}"))
                elif name == cur_cl:   parts.append(yellow(f"▶{ab}"))
                else:                  parts.append(dim(f"○{ab}"))
            row2 = "  clusters  " + "  ".join(parts)
        rows.append(row2)

    # Row 3: file progress bar within current pass
    # For non-stable clusters use cluster_files as the denominator (avoids stale all_files count).
    _cl_files = prog["cluster_files"]
    all_f  = _cl_files if (cur_cl and cur_cl not in ("stable", "") and _cl_files) else prog["all_files"]
    this_p = prog["files_this_pass"]
    ft = max(len(all_f), len(this_p))
    fi = len(this_p)
    if ft > 0:
        pct = min(100, fi / ft * 100)
        cf  = prog["current_file"].replace("src/", "") if prog["current_file"] != "?" else "?"
        rows.append(f"  files     [{bar(pct, 16)}] {fi}/{ft}  {cyan(cf)}")

    # Row 4: uncovered-lines history for last 4 files  (e.g. error.rs:92→72→45)
    hist = prog["file_history"]
    if hist:
        parts = []
        for fname, counts in list(hist.items())[-4:]:
            short = fname.replace("src/", "")
            trend = "→".join(str(c) for c in counts[-4:])
            entry = f"{short}:{trend}"
            parts.append(green(entry) if counts and counts[-1] == 0 else entry)
        hist_row = "  " + "  ".join(parts)
        if len(_plain(hist_row)) > w:
            parts = []
            for fname, counts in list(hist.items())[-3:]:
                short = fname.replace("src/", "")[:8]
                trend = "→".join(str(c) for c in counts[-3:])
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
    cov_data = _parse_coverage_json(proj_dir)
    if cov_data:
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
            trend   = dim("  " + "→".join(str(c) for c in hist[-5:])) if hist else ""
            qhist   = qcov_hist.get(name, qcov_hist.get(short, []))
            if len(qhist) >= 2:
                qtrend = dim(f"  {qhist[-2]:.1f}%→") + f"{qhist[-1]:.1f}%"
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

def _read_backend() -> str:
    try:
        for line in open(os.path.join(SCRIPT_DIR, ".env")):
            m = re.match(r'^\s*BACKEND\s*=\s*(\S+)', line)
            if m:
                return m.group(1).strip().lower()
    except Exception:
        pass
    return "ollama"

_vllm_cache: tuple[float, list[str]] = (0.0, [])

def _vllm_status() -> list[str]:
    """Return status rows for the vLLM backend; empty if backend != vllm.

    Cached: health check runs at most once per 3 s; docker log fetch at most once per 8 s.
    Never blocks the render loop for more than ~0.3 s.
    """
    global _vllm_cache
    if _read_backend() != "vllm":
        return []
    import socket, urllib.request
    now = time.time()
    cache_age = now - _vllm_cache[0]

    # Fast TCP pre-check — fails instantly if nothing listens on :8000.
    def _tcp_open() -> bool:
        try:
            s = socket.create_connection(("127.0.0.1", 8000), timeout=0.3)
            s.close()
            return True
        except OSError:
            return False

    # Return cached result if fresh enough (3 s when healthy, 3 s when loading).
    if cache_age < 3.0:
        return _vllm_cache[1]

    healthy = False
    if _tcp_open():
        try:
            urllib.request.urlopen("http://localhost:8000/health", timeout=0.5)
            healthy = True
        except Exception:
            pass

    if healthy:
        result = [green("vLLM  ✓  healthy")]
        _vllm_cache = (now, result)
        return result

    rows: list[str] = [yellow("vLLM  ◑  loading (not yet ready)")]

    # Only re-fetch docker logs every 8 s (they're slow).
    if cache_age >= 8.0 or not _vllm_cache[1]:
        try:
            cid = ""
            cid_file = "/tmp/vllm_container.id"
            if os.path.exists(cid_file):
                cid = open(cid_file).read().strip()
            if not cid:
                cid = subprocess.check_output(
                    ["docker", "ps", "--filter", "ancestor=vllm/vllm-openai:latest",
                     "--format", "{{.ID}}", "--latest"],
                    stderr=subprocess.DEVNULL, timeout=2, text=True
                ).strip()
            if cid:
                logs = subprocess.check_output(
                    ["docker", "logs", "--tail", "6", cid],
                    stderr=subprocess.STDOUT, timeout=2, text=True
                ).splitlines()
                w = _w() - 4
                for line in logs:
                    rows.append(dim((line[:w-1] + "…") if len(line) > w else line))
        except Exception:
            pass

    _vllm_cache = (now, rows)
    return rows

def _parse_max_tokens():
    try:
        with open(_GUARDS_LOG) as f:
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
        with open(_GUARDS_LOG) as f:
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
_confirm          = None   # pending confirmation: "continue" | "wipe" | "stop" | None
_graceful_pending = os.path.exists("/tmp/pipeline_graceful_stop")  # survives monitor restart
_term_settings = None         # saved terminal settings for cooked-mode URL prompt

VIEWS = [
    ("monitor",  "1", "Monitor"),
    ("pipeline", "2", "Pipeline Log"),
    ("stream",   "3", "Stream Log"),
]

def _pipeline_running() -> bool:
    try:
        subprocess.check_output(["pgrep", "-f", "pipeline.py"], stderr=subprocess.DEVNULL)
        return True
    except subprocess.CalledProcessError:
        return False


def _detect_cached_repo_url() -> str:
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
        green("[c]ontinue"),
        cyan("[g]raceful"),
        yellow("[w]ipe"),
        s_col("[s]top"),
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
    for l in view:
        pad = w - 1 - len(_plain(l))
        _ln(f"│ {l}{' ' * max(pad, 0)}│")
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

    for row in _bottom_bar():
        _ln(row)

# ── Actions ───────────────────────────────────────────────────────────────────

SCROLL_STEP = 5

def _run_action(key):
    global _action_msg, _view, _confirm, _graceful_pending
    if _confirm == "wipe":
        if key == "y":
            _confirm = None
            url = _prompt_url()   # blocks briefly in cooked mode — no render loop interference
            if url:
                _detach(["bash", os.path.join(SCRIPT_DIR, "wipe.sh"), "--repo", url])
                _action_msg = yellow(f"↺ wiping and restarting with {url}")
            else:
                _action_msg = dim("wipe cancelled — no URL")
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
            try:
                os.remove("/tmp/pipeline_graceful_stop")
            except FileNotFoundError:
                pass
            _action_msg = dim("graceful cancelled")
        elif _pipeline_running():
            _confirm    = "graceful"
            _action_msg = cyan("◑ graceful continue — finish current attempt then reload — confirm? [y/n]")
        else:
            _detach(["bash", os.path.join(SCRIPT_DIR, "continue.sh"), "--y"])
            _action_msg = green("▶ continue.sh launched (pipeline was not running)")
    elif key == "w":
        _confirm    = "wipe"
        _action_msg = yellow("↺ wipe all generated code and restart — confirm? [y/n]")
    elif key == "s":
        _confirm    = "stop"
        _action_msg = red("■ stop pipeline — confirm? [y/n]")
    elif key in ("1", "2", "3"):
        _view = {"1": "monitor", "2": "pipeline", "3": "stream"}[key]
        global _prev_buf
        _prev_buf = []   # force full redraw on next frame — clears previous view's artifacts
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
    elif _view == "stream":
        p_h = len(pipeline_status()) + 2
        return max(1, h - gpu_h - p_h - 2 - BOTTOM_LINES)
    return 1

# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    global _action_msg, _term_settings, _graceful_pending
    signal.signal(signal.SIGHUP, signal.SIG_IGN)   # survive terminal events from child procs
    old_settings = termios.tcgetattr(sys.stdin)
    _term_settings = old_settings
    sys.stdout.write("\033[2J\033[?25l")
    sys.stdout.flush()
    try:
        tty.setcbreak(sys.stdin.fileno())
        while True:
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
            elif _confirm == "stop":
                _action_msg = red("■ stop pipeline — confirm? [y/n]")
            else:
                _action_msg = ""

            try:
                ready, _, _ = select.select([sys.stdin], [], [], REFRESH)
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
    main()
