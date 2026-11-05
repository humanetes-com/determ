#!/usr/bin/env python3
"""Does a body a crash settlement took off a unit reach that unit's next writers?

WHY. A crash settlement writes a refuted body-ledger row carrying the body
that crashed and the crash. Until 2026-11-05 its only prompt reader was the
held-stub branch of the sweep, so a FILLED unit put back to its previous text
(a NARROWED settlement) was never told. Run `630350-93c5a6ee`: fill 2's second
attempt at `src/error.rs::context_drop_rest` compiled and crashed
(heap-use-after-free); fill 3's per-fn re-fill opened without it and failed
on the same E0609 the first attempt had hit. The fix renders the note as its
own `<REFUTED_BODY>` section in every writer (`domain/refuted_body.py`). This
script is the reading of that fix on any run, and its negative control on any
run before it.

USAGE.
    python scripts/refuted_note_reach.py [--run RUN_ID] [--project anyhow]
        [--events logs/events.jsonl] [--ledger logs/body_ledger/<project>]
        [--prompts logs/prompts]

Reads only files on disk; no GPU, no model, no cargo. Without --run it takes
the last run that wrote a crash refutation. A large events file is streamed
and filtered by the run id substring first.

HOW TO INTERPRET.
    One line per crash-refuted row of the run:
        <file>::<fn> iter=<i> later_prompts=<n> carried=<k> marker=<yes|no>
    later_prompts  prompts of the same run, sent after the sweep that followed
                   the refutation, whose text names the fn (`fn <name>` or the
                   bare name in a write list);
    carried        how many of those contain the note: the `<REFUTED_BODY>`
                   tag, or (older runs) the note's first line;
    marker         whether any later prompt carries the tag at all.
    Last line: `reach k/n`. Decision rule: on a run with the fix, every
    writer prompt of the unit should carry it (k == n, modulo prompts that
    only quote the fn as read-only text, which the line cannot tell apart);
    k == 0 with n > 0 is the pre-fix class this script was written for.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_NOTE_FIRST = "was taken off the tree"
_OLD_NOTE_FIRST = "was put back to this stub"


def _refuted_rows(index: Path, run: str | None) -> list[dict]:
    rows = []
    for line in index.read_text(encoding="utf-8").splitlines():
        if '"refuted"' not in line or "crash_evidence" not in line:
            continue
        r = json.loads(line)
        if r.get("state") != "refuted" or not r.get("crash_evidence"):
            continue
        if not any(k == "crash_origin" for _t, k in r.get("verdict_delta") or ()):
            continue
        rows.append(r)
    if run is None and rows:
        run = rows[-1].get("run_id")
    return [r for r in rows if r.get("run_id") == run]


def _scan_events(events: Path, run: str) -> tuple[dict[int, str], list[tuple[str, str]]]:
    """Iteration start times (`phase_c.tdd.iter`) and (ts, prompt_sha) of the
    run's LLM requests."""
    iters: dict[int, str] = {}
    reqs: list[tuple[str, str]] = []
    with events.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if run not in line:
                continue
            if '"phase_c.tdd.iter"' not in line and '"llm.request"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("run_id") != run:
                continue
            d = e.get("data") or {}
            if e.get("event") == "phase_c.tdd.iter":
                it = d.get("iter") or d.get("iteration")
                if isinstance(it, int):
                    iters.setdefault(it, e["ts"])
            elif e.get("event") == "llm.request" and d.get("prompt_sha"):
                reqs.append((e["ts"], d["prompt_sha"]))
    return iters, reqs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run")
    ap.add_argument("--project", default="anyhow")
    ap.add_argument("--events", default="logs/events.jsonl")
    ap.add_argument("--ledger")
    ap.add_argument("--prompts", default="logs/prompts")
    a = ap.parse_args()
    ledger = Path(a.ledger or f"logs/body_ledger/{a.project}")
    rows = _refuted_rows(ledger / "index.jsonl", a.run)
    if not rows:
        print("no crash-refuted rows for that run")
        return 0
    run = rows[0]["run_id"]
    iters, reqs = _scan_events(Path(a.events), run)
    total_n = total_k = 0
    for r in rows:
        name = r["fn"]
        after = iters.get(int(r.get("iteration") or 0) + 1)
        n = k = 0
        marker = False
        for ts, sha in reqs:
            if after is None or ts < after:
                continue
            p = Path(a.prompts) / f"{sha}.txt"
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if f"fn {name}" not in text and f"`{name}`" not in text:
                continue
            n += 1
            has = ("<REFUTED_BODY>" in text or _NOTE_FIRST in text
                   or _OLD_NOTE_FIRST in text)
            k += has
            marker |= "<REFUTED_BODY>" in text
        total_n += n
        total_k += k
        print(f"{r['file']}::{name} iter={r.get('iteration')} "
              f"later_prompts={n} carried={k} marker={'yes' if marker else 'no'}")
    print(f"run {run}: reach {total_k}/{total_n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
