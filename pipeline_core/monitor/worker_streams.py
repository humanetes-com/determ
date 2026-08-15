"""Per-worker generation gauges — one console row per live fill thread.

WHY THIS EXISTS. The transports have written a SEPARATE stream log per thread
since the fill engine went parallel: `_stream_path_for_thread`
(adapters/llm_transports.py) sends the main thread to `logs/stream.log` and
worker N to `logs/stream.<thread>.log`, truncating the file at each call and
opening it with `[CALL_START: <epoch>]`. Every panel kept reading the base file
alone. Under the parallel engine the main thread generates nothing, so on run
669347-cf33eae3 (2026-08-15, 16:10 local) the Generation panel read `last-tok
46m04s ago` off a base log last written at 15:23 — while four workers were
mid-generation, `logs/stream.impl-fill-stable-w*.log` were seconds old, and the
events log was 40 s old. The panel was not stale; it was pointed at an idle
file.

WHAT A ROW MEASURES. The per-thread file is truncated per call, so its SIZE is
the live length of the answer being generated right now and its mtime is the
last token's arrival. Both move between two refreshes of a 2 s loop, which is
what makes these the only rows on the panel that visibly tick while a fill is in
flight. Tokens are chars/cpt, where cpt is the transport's own measured
chars-per-token (`vllm.cpt_update`, ~3.45 on Muse-Glimmer) rather than a
constant 4 — the same figure the budget logic uses.

WHAT IT DOES NOT CLAIM. A row is a reading of a file, not of a thread: a worker
whose file has not moved is reported by the age of its last write, never as
"stalled". The label a row carries comes from the in-flight unit the events
report for that thread; when the events carry no thread for a unit (older
records), the row shows the thread name alone rather than guessing which unit
is on it.
"""

from __future__ import annotations

import os
from typing import Any

from pipeline_core.monitor.render import (
    call_start_value,
    cyan,
    dim,
    green,
    progress_bar,
    red,
    yellow,
)

__all__ = ["freshest_stream_log", "stream_phase", "worker_rows", "worker_snapshots"]

# How long after its last write a per-thread file still counts as a live
# worker. The parallel engine's calls run minutes each and a long THINKING
# stretch is silent for tens of seconds; three minutes keeps a slow worker on
# screen (with its age visible) without resurrecting yesterday's threads, whose
# files persist until that thread name is reused.
ACTIVE_WINDOW_S = 180.0

# Only ever read the tail of a stream file: a followup-heavy call can run to
# hundreds of KB and this runs once per worker per refresh.
_TAIL_BYTES = 64 * 1024

# thread -> (ts, size) of the previous sample, for the per-second char rate.
_prev: dict[str, tuple[float, int]] = {}


def stream_phase(content: str) -> str:
    """Which half of a call the text is in: THINKING, CODE, FOLLOWUP, TIMEOUT.

    Same marker precedence the Generation panel has always used, lifted here so
    the two cannot drift apart.
    """
    if "[TIMEOUT" in content:
        return "TIMEOUT"
    if "[FOLLOWUP" in content:
        return "FOLLOWUP"
    if "[CONTENT]" in content:
        return "CODE"
    if "<think>" in content and "</think>" not in content:
        return "THINKING"
    if "</think>" in content or ("[CALL:" in content and len(content) > 60):
        return "CODE"
    return "THINKING"


def _thread_of(path: str, base_log: str) -> str:
    """`logs/stream.impl-fill-stable-w1.log` → `impl-fill-stable-w1`."""
    root, dot, ext = base_log.rpartition(".")
    prefix, suffix = (f"{root}.", f".{ext}") if dot else (f"{base_log}.", "")
    name = path
    if name.startswith(prefix):
        name = name[len(prefix):]
    if suffix and name.endswith(suffix):
        name = name[: -len(suffix)]
    return name


def worker_snapshots(*, base_log: str, now: float,
                     active_window_s: float = ACTIVE_WINDOW_S) -> list[dict[str, Any]]:
    """One dict per per-thread stream file written within the active window.

    Keys: thread, chars, age_s (since last token), elapsed_s (since CALL_START,
    None when the file carries no marker), phase, rate_cps (chars/second against
    the previous sample, None on the first sighting). Newest activity first.
    """
    directory = os.path.dirname(base_log) or "."
    root, dot, ext = base_log.rpartition(".")
    prefix, suffix = ((f"{os.path.basename(root)}.", f".{ext}") if dot
                      else (f"{os.path.basename(base_log)}.", ""))
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for name in names:
        if not name.startswith(prefix) or (suffix and not name.endswith(suffix)):
            continue
        path = os.path.join(directory, name)
        if path == base_log:
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue
        age = now - st.st_mtime
        if age > active_window_s:
            continue
        try:
            with open(path, "rb") as fh:
                size = st.st_size
                fh.seek(max(0, size - _TAIL_BYTES))
                content = fh.read().decode("utf-8", errors="replace")
                if size > _TAIL_BYTES:      # the CALL_START marker is at byte 0
                    fh.seek(0)
                    head = fh.read(120).decode("utf-8", errors="replace")
                else:
                    head = content
        except OSError:
            continue
        thread = _thread_of(path, base_log)
        elapsed: float | None = None
        if (cs := call_start_value(head)) is not None:
            try:
                elapsed = max(0.0, now - float(cs))
            except ValueError:
                elapsed = None
        rate: float | None = None
        before = _prev.get(thread)
        if before is not None:
            dt = now - before[0]
            # A truncated file (next call opened) shrinks — that is a new call,
            # not a negative rate.
            if dt >= 0.5 and st.st_size >= before[1]:
                rate = (st.st_size - before[1]) / dt
        # Only advance the baseline once per half-second of wall clock. Two
        # panels sample within the same frame (the console's worker rows and the
        # Generation box's freshest-file pick); a second call that overwrote the
        # baseline with an identical reading would leave the NEXT frame with
        # dt≈0 and no rate at all, i.e. the rate column would blink out on every
        # other refresh.
        if before is None or now - before[0] >= 0.5:
            _prev[thread] = (now, st.st_size)
        out.append({
            "thread": thread,
            "chars": st.st_size,
            "age_s": age,
            "elapsed_s": elapsed,
            "phase": stream_phase(content),
            "rate_cps": rate,
        })
    out.sort(key=lambda w: w["age_s"])
    return out


def freshest_stream_log(base_log: str, now: float,
                        active_window_s: float = ACTIVE_WINDOW_S) -> tuple[str, str]:
    """(path, thread) of the most recently written stream file, base included.

    `thread` is "" for the base file (the main thread). Falls back to the base
    path when no per-thread file has been written inside the window, so a
    single-threaded phase behaves exactly as before this existed.
    """
    best_path, best_thread, best_mtime = base_log, "", 0.0
    try:
        best_mtime = os.stat(base_log).st_mtime
    except OSError:
        best_mtime = 0.0
    for w in worker_snapshots(base_log=base_log, now=now,
                              active_window_s=active_window_s):
        mtime = now - float(w["age_s"])
        if mtime > best_mtime:
            root, dot, ext = base_log.rpartition(".")
            best_path = (f"{root}.{w['thread']}.{ext}" if dot
                         else f"{base_log}.{w['thread']}")
            best_thread, best_mtime = str(w["thread"]), mtime
    return best_path, best_thread


def _dur(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"
    return f"{seconds / 3600:.1f}h"


_PHASE_COLOR = {"CODE": green, "THINKING": yellow, "FOLLOWUP": cyan, "TIMEOUT": red}


def worker_rows(snapshots: list[dict[str, Any]], *,
                units_by_thread: dict[str, dict[str, Any]] | None = None,
                cpt: float | None = None,
                request_max: int | None = None,
                budget_by_thread: dict[str, int] | None = None,
                width: int = 120) -> list[str]:
    """Render the worker gauges. Empty when no per-thread file is live.

    The bar is tokens generated against THIS request's `max_tokens`
    (`vllm.budget`, keyed by thread — four workers hold four budgets at once),
    which is a real denominator: it is the ceiling the server truncates at, and
    a row reaching it is the engine row's `length-capped` counter about to
    move. Without a budget reading there is no bar — the count and the rate
    stand alone rather than being scaled against a guess.

    The token count is an ESTIMATE and is printed as one (`≈`). It is the
    stream file's byte count over the transport's rolling chars-per-token, and
    that average is taken across all traffic while any one answer has its own
    density: run 669347-cf33eae3's `impl<A, B> NotBothDebug` call measured 3.85
    chars/token against a rolling 3.29, so the estimate read 19.1k against a
    16,384 ceiling the server had in fact just enforced. The bar caps at 100%
    and says `≈at cap` rather than pretending to a precision the file cannot
    give; the exact figure is the engine row's counter, which comes from the
    server's own tokenizer.
    """
    if not snapshots:
        return []
    units = units_by_thread or {}
    budgets = budget_by_thread or {}
    chars_per_token = cpt if (cpt and cpt > 0) else 4.0
    label_w = max(18, min(34, width - 78))
    rows: list[str] = []
    for w in snapshots:
        thread = str(w["thread"])
        unit = units.get(thread) or {}
        label = str(unit.get("label") or thread).splitlines()[0]
        tag = ("↻" if unit.get("refill")
               else "⊞" if unit.get("kind") == "impl"
               else "▤" if unit.get("kind") == "file"
               else " ")
        short = thread.rpartition("-")[2] or thread
        tokens = w["chars"] / chars_per_token
        phase = str(w["phase"])
        # Pad BEFORE colouring: an f-string width counts the ANSI escape bytes,
        # so `f"{cyan(x):<34}"` pads a 5-char label to nothing and the columns
        # to the right of it stop lining up.
        seg = [f"{short:<3}{tag}{cyan(f'{label[:label_w]:<{label_w}}')}",
               _PHASE_COLOR.get(phase, dim)(f"{phase:<8}")]
        budget = budgets.get(thread) or request_max
        if budget:
            pct = min(100.0, tokens / budget * 100)
            at_cap = tokens >= budget
            bar = f"[{progress_bar(pct, 12)}] {pct:3.0f}%"
            seg.append(yellow(f"{bar} ≈at cap") if at_cap else bar)
        seg.append(f"≈{tokens:>6,.0f} tok")
        rate = w["rate_cps"]
        seg.append(f"{rate / chars_per_token:>5.0f} tok/s" if rate is not None
                   else dim("    · tok/s"))
        elapsed = w["elapsed_s"]
        seg.append(dim(f"{_dur(elapsed):>6}" if elapsed is not None else "     ?"))
        age = float(w["age_s"])
        # The silence, not the elapsed time, is what says a worker has stopped
        # producing: a 20-minute call that wrote a token a second ago is healthy.
        if age >= 30:
            seg.append(yellow(f"quiet {_dur(age)}"))
        rows.append("           " + "  ".join(seg))
    return rows
