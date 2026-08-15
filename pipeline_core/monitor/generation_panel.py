"""The Generation box — the call the panel follows, token by token.

Moved out of ``monitor.py`` (god-file, LOC ratchet: it may only shrink) with
one behavioural change, which is the reason for the move.

WHICH FILE IT FOLLOWS. It used to read `logs/stream.log` and nothing else. That
is the MAIN thread's file: `_stream_path_for_thread`
(adapters/llm_transports.py) gives every worker thread its own
`logs/stream.<thread>.log`, and under the parallel fill engine the main thread
generates nothing for hours at a time. Measured on run 669347-cf33eae3
(2026-08-15 16:10 local): the box read `phase=CODE 6,436 chars last-tok 46m04s
ago` from a base file last written at 15:23, while four `stream.impl-fill-
stable-w*.log` were seconds old. The panel now follows the freshest stream file
of the two kinds and names which thread it is showing, so "no new tokens" means
the ENGINE is quiet rather than "the thread this panel happens to watch is".

The per-worker breakdown lives on the Pipeline console
(:mod:`pipeline_core.monitor.worker_streams`); this box stays what it was — one
call, its budget, and the text as it arrives.
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
from pipeline_core.monitor.worker_streams import freshest_stream_log, stream_phase

__all__ = ["generation_rows"]

_prev_len = 0

_PHASE_COLOR = {"CODE": green, "THINKING": yellow, "FOLLOWUP": cyan, "TIMEOUT": red}


def _fmt_eta(secs: float) -> str:
    if secs < 60:
        return f"{int(secs)}s"
    if secs < 3600:
        return f"{int(secs / 60)}m{int(secs % 60):02d}s"
    return f"{int(secs / 3600)}h{int((secs % 3600) / 60):02d}m"


def generation_rows(*, stream_log: str, width: int, now: float,
                    runtime: dict[str, Any], refresh: float,
                    think_cap_chars: int, tail_lines: int = 5) -> list[str]:
    """Rows for the Generation box: header, budget/think bar, last text lines."""
    global _prev_len
    path, thread = freshest_stream_log(stream_log, now)
    try:
        stat = os.stat(path)
        with open(path, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except FileNotFoundError:
        return [dim("no stream log yet")]
    except OSError as exc:
        return [red(f"stream log error: {exc}")]
    if not content.strip():
        return [dim("stream log empty")]

    age = now - stat.st_mtime
    age_s = f"{int(age)}s" if age < 60 else f"{int(age / 60)}m{int(age % 60):02d}s"
    started = 0.0
    if (cs := call_start_value(content)) is not None:
        try:
            started = float(cs)
        except ValueError:
            started = 0.0
    elapsed = max(1.0, now - started) if started > 0 else max(1.0, age)
    ela_s = (f"{int(elapsed / 60)}m{int(elapsed % 60):02d}s" if elapsed >= 60
             else f"{int(elapsed)}s")
    cur_chars = len(content)
    cpt = runtime.get("cpt") or 4.0
    inst_rate = max(0, cur_chars - _prev_len) / refresh / cpt
    smooth_rate = cur_chars / cpt / elapsed
    _prev_len = cur_chars
    rate_str = f"~{inst_rate:.0f} tok/s" if inst_rate > 0.5 else dim("no new tokens")
    phase = stream_phase(content)
    phase_s = _PHASE_COLOR.get(phase, dim)(phase)

    progress_rows: list[str] = []
    if phase == "THINKING" and "<think>" in content:
        tc = max(0, cur_chars - content.find("<think>") - 7)
        pct = min(100.0, tc / think_cap_chars * 100)
        eta = (_fmt_eta(max(0.0, think_cap_chars - tc) / smooth_rate)
               if smooth_rate > 0.1 else "?")
        progress_rows.append(
            f"  think [{progress_bar(pct, 20)}] {pct:4.0f}%  "
            f"{tc:,}/{think_cap_chars:,} chars  {rate_str}  ETA {eta}  elapsed {ela_s}")
    else:
        max_tok = runtime.get("max_tokens")
        tg = cur_chars / cpt
        if max_tok:
            pct = min(100.0, tg / max_tok * 100)
            eta = (_fmt_eta(max(0.0, max_tok - tg) / smooth_rate)
                   if smooth_rate > 0.1 else "?")
            progress_rows.append(
                f"  budget [{progress_bar(pct, 20)}] {pct:4.0f}%  {tg:,.0f}/{max_tok:,} tok"
                f"  avg {smooth_rate:.1f} tok/s  ETA ≤ {eta}  elapsed {ela_s}")

    display = content
    for marker in ("[FOLLOWUP", "[CONTENT]", "</think>"):
        if marker in display:
            display = display.split(marker)[-1].lstrip("\n")
            break
    inner = width - 4
    dlines = [ln for ln in display.splitlines() if ln.strip()][-tail_lines:]
    retries = runtime.get("retries")
    cpt_s = f"cpt={cpt:.2f}" if runtime.get("cpt") else dim("cpt=?")
    retry_s = f"  {red(f'400-retries={retries}')}" if retries else ""
    who = dim(f"[{thread}]") if thread else dim("[main]")
    rows = [f"phase={phase_s}  {who}  {cur_chars:,} chars   last-tok {age_s} ago   "
            f"{rate_str}   {cpt_s}{retry_s}"]
    rows += progress_rows
    rows += [dim((ln[:inner - 2] + "…") if len(ln) > inner else ln)
             for ln in dlines] or [dim("(empty)")]
    return rows
