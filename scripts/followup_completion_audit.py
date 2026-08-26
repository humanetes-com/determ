#!/usr/bin/env python3
"""Which fill replies were written by the thinking-exhausted FOLLOWUP, and under what budget?

WHY THIS EXISTS
---------------
`LLMController.run` (`pipeline_core/llm/controller.py`) sends a forced followup
whenever the main stream ended `length` (or capped) after reasoning: the trace
is re-fed as a user turn and the model is asked for "only the code" with
`enable_thinking=False`. Two facts about that completion are invisible in the
record:

  1. On the strength dialect (Muse-Glimmer) there is no "thinking off":
     `enable_thinking=False` maps to `reasoning_strength: low`, which still
     reasons (`llm_transports.py:_reasoning_delta`), and `request_max_tokens`
     gives the followup NO thinking reserve — `request_max` = the content
     budget (8192 with `VLLM_CONTENT_BUDGET=8192`) for reasoning AND code.
  2. `_followup` builds a local `StreamMeta()` and emits nothing; the
     `llm.stream_meta` row carries the MAIN stream's `finish_reason` and the
     FOLLOWUP's `content_chars` (`len(result)` after the override), and
     `_persist_thinking` stores only the main trace under the response sha.

Measured 2026-08-26 on run `82157-476ddc16`, the escalated constructor call
(`49385be5…`): `request_max 16384, reasoning_chars 71584, finish_reason
length, content_chars 8192`; the guard line `[llm/followup] thinking_len=71584
result_len=8192` proves the 21-block reply IS the followup's, i.e. the main
stream emitted no code and the only code on record for that prompt came from
an 8192-token low-rung completion. Every escalated stall diagnosed as "the
model re-emitted the wrap" was read through that completion path.

This script computes that per call, from `logs/events.jsonl` joined with
`logs/pipeline_guards.log` on `thinking_len == reasoning_chars`, and reads the
followup's own `llm.followup_meta` rows when a build emits them. No GPU, no
model, writes nothing.

USAGE
-----
    .venv/bin/python scripts/followup_completion_audit.py \
        [--run 82157-476ddc16 ...] [--thread-prefix file-first] \
        [--events logs/events.jsonl] [--guards logs/pipeline_guards.log] \
        [--content-budget 8192] [--prose-cpt 4.5] [--verbose]

  --run            restrict to these run ids (repeatable); default: all rows.
  --thread-prefix  restrict to threads starting with this (e.g. `file-first`).
  --content-budget the followup's `request_max` when no thinking reserve is
                   added (VLLM_CONTENT_BUDGET); used only for the summary line.
  --prose-cpt      chars-per-token assumed for REASONING text when bounding the
                   main stream's leftover content budget (prose tokenizes
                   longer than code; 4.5 is a generous upper bound so the
                   leftover is an upper bound too).

HOW TO INTERPRET THE OUTPUT
---------------------------
Per row (with --verbose or a --run filter):
  prompt_sha[:8] thread                 which call.
  main: <finish> req=<request_max> reason=<chars> content=<chars>
                                        the main stream's record.
  reason_tok≈<a>..<b>                   reasoning tokens bounded by the prompt's
                                        own chars/token (low cpt → more tokens)
                                        and --prose-cpt (fewer tokens).
  main_left≤<n> tok                     request_max − the smaller estimate:
                                        an UPPER bound on the code the main
                                        stream could have emitted before the
                                        cut. ≤ a few hundred means the main
                                        stream wrote no usable reply.
  followup: reply=<yes/no>              guard-log join: `result_len ==
                                        content_chars` means the reply on
                                        record is the FOLLOWUP's output.
  followup request_max=<n> think=<b>    `(recorded)`: read from the row's own
                                        `llm.followup_meta` — the budget and
                                        rung the followup actually ran under.
                                        `(assumed: ...)`: no followup_meta for
                                        this call (a build before the event),
                                        so the pre-event policy is stated:
                                        content budget, no thinking reserve,
                                        strength low.
  followup trace persisted: yes (N)     `logs/thinking/<response sha>.followup.txt`
                                        exists — the followup's OWN reasoning,
                                        N chars, readable beside the main
                                        trace `<response sha>.txt`.
  followup trace persisted: no          no `.followup.txt`, and `<sha>.txt`
                                        equals the main reasoning length — the
                                        followup's own reasoning is not on
                                        disk anywhere (nothing to read to see
                                        whether it converged). "differs from
                                        main trace (unverified)" means the file
                                        under that sha has another length
                                        (`_persist_thinking` keeps the FIRST
                                        writer of a sha), not that a followup
                                        trace was stored.
  followup_meta: <finish_reason> ...    present only once the controller emits
                                        `llm.followup_meta`; `length` here is
                                        a followup that was itself cut.
                                        `reason=` is the followup's OWN
                                        reasoning chars, `refed=` how much of
                                        the main trace its request carried,
                                        `fn_blocks=` `<FN ` blocks in its reply.

Summary:
  stream_meta rows / followup=True      how often the forced completion ran.
  reply is the followup's               joined via the guard log; the count of
                                        recorded replies that were written
                                        with no thinking reserve.
  main stream wrote ≤512 tok            calls where the diagnosis in the trace
                                        never reached a code block at all.
  followup finish reasons               only when `llm.followup_meta` exists.

Decision rule: a stall attributed to "the model kept the given body" on a call
where `reply is the followup's` is a COMPLETION-POLICY reading, not a capability
reading, until the followup runs with a thinking reserve and its trace is
persisted. Re-run after a build that changes `_followup` and compare the
`reply is the followup's` and `main stream wrote ≤512 tok` counts per run.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

_GUARD_RE = re.compile(r"\[llm/followup\] model=\S+ thinking_len=(\d+) result_len=(\d+)")


def _load_events(path: Path, runs: set[str]):
    req: dict[tuple[str, str], int] = {}
    resp: dict[tuple[str, str], tuple[str, int]] = {}
    metas: list[dict] = []
    fu_meta: dict[tuple[str, str], dict] = {}
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"llm.' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            rid = ev.get("run_id", "")
            if runs and rid not in runs:
                continue
            kind = ev.get("event")
            d = ev.get("data") or {}
            sha = d.get("prompt_sha") or (d.get("prompt") or {}).get("sha256", "")
            if kind == "llm.request":
                req[(rid, sha)] = int((d.get("prompt") or {}).get("char_count") or 0)
            elif kind == "llm.response":
                r = d.get("response") or {}
                resp[(rid, sha)] = (r.get("sha256", ""), int(r.get("char_count") or 0))
            elif kind == "llm.stream_meta":
                metas.append({"run_id": rid, "thread": ev.get("thread", ""),
                              "ts": ev.get("ts", ""), **d})
            elif kind == "llm.followup_meta":
                fu_meta[(rid, sha)] = d
    return req, resp, metas, fu_meta


def _load_guards(path: Path) -> dict[int, list[int]]:
    out: dict[int, list[int]] = defaultdict(list)
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = _GUARD_RE.search(line)
                if m:
                    out[int(m.group(1))].append(int(m.group(2)))
    except OSError:
        pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--run", action="append", default=[])
    ap.add_argument("--thread-prefix", default="")
    ap.add_argument("--events", type=Path, default=REPO / "logs" / "events.jsonl")
    ap.add_argument("--guards", type=Path, default=REPO / "logs" / "pipeline_guards.log")
    ap.add_argument("--thinking-dir", type=Path, default=REPO / "logs" / "thinking")
    ap.add_argument("--content-budget", type=int,
                    default=int(os.getenv("VLLM_CONTENT_BUDGET", "8192")))
    ap.add_argument("--prose-cpt", type=float, default=4.5)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    runs = set(a.run)
    req, resp, metas, fu_meta = _load_events(a.events, runs)
    guards = _load_guards(a.guards)
    rows = [m for m in metas if m.get("thread", "").startswith(a.thread_prefix)]
    per_row = a.verbose or bool(runs)

    n_fu = n_reply_fu = n_main_starved = n_trace_missing = 0
    fu_finish: Counter[str] = Counter()
    main_finish: Counter[str] = Counter()
    for m in rows:
        rid, sha = m["run_id"], m.get("prompt_sha", "")
        followup = bool(m.get("followup"))
        main_finish[str(m.get("finish_reason"))] += 1
        if not followup:
            continue
        n_fu += 1
        ptoks = int(m.get("prompt_tokens") or 0)
        pchars = req.get((rid, sha), 0)
        rchars = int(m.get("reasoning_chars") or 0)
        cchars = int(m.get("content_chars") or 0)
        rmax = int(m.get("request_max") or 0)
        cpt = (pchars / ptoks) if (pchars and ptoks) else 0.0
        hi = int(rchars / cpt) if cpt else 0            # more tokens (code-like cpt)
        lo = int(rchars / a.prose_cpt)                   # fewer tokens (prose cpt)
        main_left = max(0, rmax - lo) if rmax else -1
        starved = 0 <= main_left <= 512
        n_main_starved += starved
        reply_fu = cchars in guards.get(rchars, [])
        n_reply_fu += reply_fu
        rsha, _ = resp.get((rid, sha), ("", 0))
        tpath = a.thinking_dir / f"{rsha}.txt" if rsha else None
        tlen = tpath.stat().st_size if tpath and tpath.exists() else -1
        # The followup's own trace lives beside the main one under the same
        # reply sha, `<sha>.followup.txt`, once a build persists it.
        fu_tpath = a.thinking_dir / f"{rsha}.followup.txt" if rsha else None
        fu_tlen = fu_tpath.stat().st_size if fu_tpath and fu_tpath.exists() else -1
        trace_missing = fu_tlen < 0 and tlen >= 0 and abs(tlen - rchars) <= 16
        n_trace_missing += trace_missing
        fm = fu_meta.get((rid, sha))
        if fm:
            fu_finish[str(fm.get("finish_reason"))] += 1
        if per_row:
            print(f"{sha[:8]} {m.get('thread','')} run={rid}")
            print(f"  main: {m.get('finish_reason')} req={rmax} reason={rchars} "
                  f"content={cchars}  reason_tok≈{lo}..{hi}  main_left≤{main_left} tok"
                  + ("  [main wrote no usable reply]" if starved else ""))
            if fm:
                fu_budget = (f"followup request_max={fm.get('request_max')} "
                             f"think={fm.get('enable_thinking')} (recorded)")
            else:
                fu_budget = (f"followup request_max={a.content_budget} "
                             "(assumed: no thinking reserve, strength low)")
            if fu_tlen >= 0:
                fu_trace = f"yes ({fu_tlen} chars)"
            elif trace_missing:
                fu_trace = "no"
            else:
                fu_trace = "n/a" if tlen < 0 else "differs from main trace (unverified)"
            print(f"  followup: reply={'YES (guard result_len matches)' if reply_fu else 'no/unknown'}"
                  f"; {fu_budget}; followup trace persisted: {fu_trace}"
                  + (f"; followup_meta: {fm.get('finish_reason')} content={fm.get('content_chars')}"
                     f" reason={fm.get('reasoning_chars')} refed={fm.get('reasoning_chars_refed')}"
                     f" fn_blocks={fm.get('fn_blocks')}"
                     if fm else ""))
    print(f"\n{len(rows)} stream_meta row(s)"
          + (f" in run(s) {sorted(runs)}" if runs else "")
          + (f" thread~{a.thread_prefix}*" if a.thread_prefix else "")
          + "; main finish_reason: " + ", ".join(f"{k} {v}" for k, v in sorted(main_finish.items())))
    print(f"  followup=True: {n_fu}")
    print(f"  reply is the followup's (guard join): {n_reply_fu}")
    print(f"  main stream wrote ≤512 tok (reasoning ate the request): {n_main_starved}")
    print(f"  followup trace not persisted (thinking file == main trace): {n_trace_missing}")
    print("  followup finish reasons: " + (", ".join(f"{k} {v}" for k, v in sorted(fu_finish.items()))
                                          if fu_finish else "none recorded (no llm.followup_meta rows)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
