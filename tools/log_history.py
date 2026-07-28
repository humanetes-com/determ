#!/usr/bin/env python3
"""Grep the FULL run history — every rotated archive plus the active log — with
each hit labelled by the run it belongs to.

    .venv/bin/python tools/log_history.py PATTERN [PATTERN ...] [--log L]
                                          [--max N] [--runs N]

`tools/log_segment.sh` answers "what is happening right now" and clips to the
last `PIPELINE STARTED`. This answers "has this happened before", which is the
question a plateau spanning several runs can only be seen through.

The old rule banned whole-log greps for a real reason: an unlabelled hit from a
run that ended yesterday reads exactly like a live one, and acting on it wastes
a pass. The hazard is the MISSING ATTRIBUTION, not the width of the window — so
every line here is prefixed with its run id and start time:

    run 2160981-4881cdd7 (2026-07-28 10:10:26) │ …construct: floor stalled…

Patterns are plain SUBSTRINGS, OR-ed (repo rule: no regex). Pass several
instead of alternation: `log_history.py REVERTED reverted`.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from log_files import read_all  # noqa: E402

from pipeline_core.domain.run_history import parse_run_summaries, split_runs  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("patterns", nargs="*",
                    help="plain substrings, OR-ed; omit to print every line")
    ap.add_argument("--log", default="logs/pipeline_anyhow.log")
    ap.add_argument("--max", type=int, default=60,
                    help="cap on printed hits (newest kept)")
    ap.add_argument("--runs", type=int, default=0,
                    help="only the N most recent runs (0 = all)")
    args = ap.parse_args()

    lines = read_all(Path(args.log))
    runs = split_runs(lines)
    summaries = parse_run_summaries(lines)
    if args.runs > 0:
        runs, summaries = runs[-args.runs:], summaries[-args.runs:]

    hits: list[str] = []
    for seg, summ in zip(runs, summaries, strict=True):
        label = f"run {summ.run_id or '?'} ({summ.started or '?'})"
        for ln in seg:
            if not args.patterns or any(p in ln for p in args.patterns):
                hits.append(f"{label} │ {ln.strip()}")

    if not hits:
        print(f"no match in {len(runs)} run(s) of {args.log}(+archives)")
        return 0
    shown = hits[-args.max:]
    if len(hits) > len(shown):
        print(f"… {len(hits) - len(shown)} earlier hit(s) omitted "
              f"(--max {args.max})")
    for h in shown:
        print(h)
    return 0


if __name__ == "__main__":
    sys.exit(main())
