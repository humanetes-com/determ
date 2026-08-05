"""Pure ANSI / pipeline-line text helpers for the monitor TUI.

These are the regex-free (R7) presentation-layer primitives the renderer reuses:
visible-width measurement (ANSI escape stripping), the color-wrapping helpers,
the two structured scrapes of pipeline-state / call-start log lines, and the
event-log line formatter. They hold no I/O and no module state, so they live in
:mod:`pipeline_core.monitor` beside the data readers (run_data / events_data)
rather than in the god-file ``monitor.py``.
"""

from __future__ import annotations

from typing import Any


def bold(s: str) -> str:      return f"\033[1m{s}\033[0m"
def dim(s: str) -> str:       return f"\033[2m{s}\033[0m"
def underline(s: str) -> str: return f"\033[4m{s}\033[0m"
def green(s: str) -> str:     return f"\033[32m{s}\033[0m"
def red(s: str) -> str:       return f"\033[31m{s}\033[0m"
def cyan(s: str) -> str:      return f"\033[36m{s}\033[0m"
def yellow(s: str) -> str:    return f"\033[33m{s}\033[0m"
def rev(s: str) -> str:       return f"\033[7m{s}\033[0m"   # reverse video (highlight)
def bg_white(s: str) -> str:  return f"\033[30;107m{s}\033[0m"  # black on bright white


def event_format(ev: dict[str, Any]) -> str:
    """Render a single event-log entry as one human-readable colored line."""
    ts = ev.get("ts", "")
    # Time portion only: HH:MM:SS.mmm
    if "T" in ts:
        time_part = ts.split("T", 1)[1].split("+")[0].split(".")
        ts_s = time_part[0] + ("." + time_part[1][:3] if len(time_part) > 1 else "")
    else:
        ts_s = ts[:12]

    verb = ev.get("event", "")
    if verb.endswith(".done") or verb.endswith(".pass") or verb.endswith(".success"):
        verb_col = green
    elif verb.endswith(".fail") or verb.endswith(".error"):
        verb_col = red
    elif verb.endswith(".start") or verb.endswith(".request"):
        verb_col = yellow
    elif verb.startswith("guard.") or verb.startswith("llm."):
        verb_col = cyan
    else:
        verb_col = bold

    fields = []
    for k in ("task", "cluster", "module", "file", "attempt"):
        v = ev.get(k)
        if v not in (None, "", "-"):
            fields.append(f"{k}={v}")
    if ev.get("duration_ms") is not None:
        fields.append(f"{ev['duration_ms']}ms")

    # Short data summary: prefer a couple of high-signal fields.
    data = ev.get("data") or {}
    data_bits = []
    for k in ("pct", "passed", "ok", "covered", "coverable", "pass_count", "fail_count",
              "returncode", "reason", "model"):
        if k in data:
            v = data[k]
            if isinstance(v, str) and len(v) > 30:
                v = v[:27] + "..."
            data_bits.append(f"{k}={v}")
    # Compact prompt content-addressed summary when present.
    for k in ("prompt", "response"):
        sub = data.get(k)
        if isinstance(sub, dict) and "char_count" in sub:
            data_bits.append(f"{k}={sub['char_count']}c")

    head = f"{dim(ts_s)} {verb_col(bold(verb)):<22}"
    tail = "  ".join(fields)
    extra = "  ".join(data_bits)
    return f"{head}  {dim(tail) if tail else ''}{('  ' + dim(extra)) if extra else ''}".rstrip()


def ansi_seq_len(s: str, i: int) -> int:
    r"""Length of the CSI escape sequence at ``s[i]`` (``\033[`` params final-letter),
    or 0 if none (regex-free, R7; mirrors ``\033\[[0-9;]*[a-zA-Z]``)."""
    if not s.startswith("\033[", i):
        return 0
    j = i + 2
    while j < len(s) and (s[j].isdigit() or s[j] == ";"):
        j += 1
    c = s[j] if j < len(s) else ""
    if ("a" <= c <= "z") or ("A" <= c <= "Z"):
        return j + 1 - i
    return 0


def plain(s: str) -> str:
    """``s`` with every ANSI CSI escape removed — i.e. its visible characters."""
    out: list[str] = []
    i = 0
    while i < len(s):
        seq = ansi_seq_len(s, i)
        if seq:
            i += seq
            continue
        out.append(s[i])
        i += 1
    return "".join(out)


def state_done_key(line: str) -> str | None:
    r"""Cluster key from a ``[<key>[/...]] done`` pipeline_state line, else None
    (regex-free, R7; mirrors ``\[([^\]/]+)(?:/[^\]]*)?\]\s+done\b``)."""
    if not line.startswith("["):
        return None
    rest = line[1:]
    e = 0
    while e < len(rest) and rest[e] not in "]/":
        e += 1
    if e == 0:
        return None                       # [^\]/]+ requires ≥1 char
    key = rest[:e]
    k = e
    if k < len(rest) and rest[k] == "/":  # optional /[^\]]*
        k += 1
        while k < len(rest) and rest[k] != "]":
            k += 1
    if k >= len(rest) or rest[k] != "]":
        return None
    tail = rest[k + 1:]
    stripped = tail.lstrip()
    if stripped == tail:                  # \s+ requires ≥1 whitespace
        return None
    nxt = stripped[4:5]
    if stripped.startswith("done") and not (nxt.isalnum() or nxt == "_"):  # done\b
        return key
    return None


def call_start_value(content: str) -> str | None:
    r"""The float-ish token from the first well-formed ``[CALL_START: <num>]``
    marker, else None (regex-free, R7; mirrors ``\[CALL_START: ([\d.]+)\]``)."""
    marker = "[CALL_START: "
    start = 0
    while (i := content.find(marker, start)) >= 0:
        j = i + len(marker)
        k = j
        while k < len(content) and (content[k].isdigit() or content[k] == "."):
            k += 1
        if k > j and k < len(content) and content[k] == "]":
            return content[j:k]
        start = i + 1
    return None


def driver_counter(state: dict[str, Any]) -> str:
    """The live outer loop's own counter.

    Under the TDD driver the per-fn engine runs with max_passes=1 and emits no
    pass.start at all once the frontier holds only behavioural re-fills, so
    `pass=n/N` renders as `?/?` for hours on a perfectly healthy run. The tdd
    iteration (+ its stall budget) is the counter that actually advances there.
    """
    if state.get("driver") == "tdd":
        stall = state.get("tdd_stall")
        maxs = state.get("tdd_max_stalls")
        budget = (f"  stall={stall}/{maxs}"
                  if stall is not None and maxs is not None else "")
        # Never substitute a plausible number for a missing one — inventing
        # "iter=1" is the same defect as the "?/?" this replaced: a counter
        # asserting a value the events never reported.
        return f"iter={state.get('tdd_iter') or '?'}{budget}"
    pass_n = state.get("pass_n") or "?"
    maxp = state.get("max_passes") or "?"
    return f"pass={pass_n}/{maxp}"


def progress_bar(pct: float, width: int = 18, label: str = "") -> str:
    """Filled/empty block bar, optionally with a label overlaid from the left."""
    filled = round(pct / 100 * width)
    s = list("█" * filled + "░" * (width - filled))
    if label and len(label) <= width:
        for i, ch in enumerate(label):
            s[i] = ch
    return "".join(s)


def loop_hierarchy_rows(
    *,
    clusters: list[str],
    done_set: set[str],
    cur_cl: str,
    prog: dict[str, Any],
    phase_c: dict[str, Any],
) -> list[str]:
    """The cluster → attempt → file → retry rows.

        cluster  ✓stable  ▶nightly  ○...       [██░░░░░░] 1/3
          └ attempt                            [████░░░░] 2/10
              └ file  context.rs               [████████] 4/8
                   └ retry                     [██░░░░░░] 1/2   (only when active)

    Levels 1-3 are fed ONLY by `task0c.*` events, and a RESUMED run skips
    task0c entirely — the clusters are baked and the finished ones are
    checkpointed. Rendering them then gives `attempt ?/? · file ? 0/0` for the
    whole run: honest and useless. The counter that DOES move under the TDD
    driver is its own iteration, and that is what takes their place.
    """
    rows: list[str] = []
    n_clusters = len(clusters)
    n_done_cl = len(done_set)
    cl_i = next((i for i, n in enumerate(clusters) if n == cur_cl), n_done_cl)
    cl_pct = (cl_i / n_clusters * 100) if n_clusters else 0
    cl_parts: list[str] = []
    for name in clusters:
        ab = name[:10]
        if name in done_set:
            cl_parts.append(green(f"✓{ab}"))
        elif name == cur_cl:
            cl_parts.append(yellow(bold(f"▶{ab}")))
        else:
            cl_parts.append(dim(f"○{ab}"))
    cl_bar = f"[{progress_bar(cl_pct, 10)}] {cl_i}/{n_clusters}" if n_clusters else ""
    rows.append(f"  cluster  {'  '.join(cl_parts)}  {dim(cl_bar)}")

    if not prog.get("has_task0c", True):
        bits = [f"iter {phase_c.get('tdd_iter') or 0}"]
        if phase_c.get("tdd_pass") is not None:
            bits.append(f"pass {phase_c['tdd_pass']}")
        stall, max_stall = phase_c.get("tdd_stall"), phase_c.get("tdd_max_stalls")
        if stall is not None and max_stall:
            bits.append(f"stall {stall}/{max_stall}")
        rows.append(f"    └ tdd  {dim('(no task0c this run)')}  {'  '.join(bits)}")
        return rows

    try:
        att_i, att_t = int(prog["attempt"]), int(prog["max_attempt"])
    except (ValueError, TypeError, KeyError):
        att_i = att_t = 0
    att_pct = (att_i / att_t * 100) if att_t else 0
    rows.append(f"    └ attempt  [{progress_bar(att_pct, 14)}] "
                f"{prog['attempt']}/{prog['max_attempt']}")

    _cl_files = prog["cluster_files"]
    all_f = (_cl_files if (cur_cl and cur_cl not in ("stable", "") and _cl_files)
             else prog["all_files"])
    this_p = prog["files_this_pass"]
    ft = max(len(all_f), len(this_p))
    fi = len(this_p)
    cf = prog["current_file"].replace("src/", "") if prog["current_file"] != "?" else "?"
    file_pct = (fi / ft * 100) if ft else 0
    rows.append(f"        └ file  {cyan(cf):<18}  [{progress_bar(file_pct, 12)}] {fi}/{ft}")

    ir, ir_max = prog.get("inner_retry", "?"), prog.get("inner_retry_max", "?")
    if ir != "?" and ir_max != "?":
        try:
            r_pct = int(ir) / int(ir_max) * 100
        except (ValueError, TypeError, ZeroDivisionError):
            r_pct = 0
        rows.append(f"             └ retry  [{progress_bar(r_pct, 10)}] {ir}/{ir_max}")
    return rows
