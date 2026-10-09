#!/usr/bin/env python3
"""Print the red ledger of every cluster end of a run.

WHY THIS EXISTS
  Every t2a cluster end writes `phase_c.tdd.red_ledger`: per red test, the
  class that holds it (pipeline_core/domain/red_ledger.py). Three classes
  are the pipeline's fault and never an acceptable end: refused_gain (a
  reverted run had turned the test green), compile_loss (every offer failed
  to compile) and never_offered (no route ever wrote its functions).
  model_limit is the model's; open means the cluster ended while something
  was still owed; withdrawn means the original crate fails it too. A red
  run whose ledger holds only model_limit and withdrawn entries is red
  because of the model, not the pipeline.

USAGE
  python3 scripts/red_ledger.py logs/events.jsonl RUN_ID
  (scripts/pattern_census.py reads `ledgers_by_run` for its P47 row.)

HOW TO INTERPRET
  One block per cluster end: the close kind, the count per class, then each
  red test with its class and why. PIPELINE lines are the defects to fix
  before anything else; the summary counts them.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from typing import Any

PIPELINE = frozenset({"refused_gain", "compile_loss", "never_offered"})


def ledgers_by_run(events_path: str, runs: set[str] | None = None,
                   ) -> dict[str, list[dict[str, Any]]]:
    """run id -> the `phase_c.tdd.red_ledger` event data of that run, in order."""
    out: dict[str, list[dict[str, Any]]] = {}
    with open(events_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"phase_c.tdd.red_ledger"' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            rid = str(ev.get("run_id", ""))
            if runs and rid not in runs:
                continue
            out.setdefault(rid, []).append(ev.get("data") or {})
    return out


def pipeline_entries(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in ledger.get("entries") or [] if e.get("class") in PIPELINE]


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    ledgers = ledgers_by_run(argv[1], {argv[2]}).get(argv[2], [])
    if not ledgers:
        print(f"no red ledger recorded for {argv[2]}")
        return 1
    total_pipeline = 0
    for led in ledgers:
        entries = led.get("entries") or []
        counts = Counter(str(e.get("class")) for e in entries)
        print(f"[{led.get('cluster')}] close={led.get('close_kind')}: "
              f"{len(entries)} red: "
              + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
        for e in entries:
            tag = "PIPELINE " if e.get("class") in PIPELINE else ""
            print(f"  {tag}{e.get('class')}: {e.get('test')} — {e.get('why')}")
        total_pipeline += len(pipeline_entries(led))
    print(f"summary: {total_pipeline} pipeline-class entr(y/ies) across "
          f"{len(ledgers)} cluster end(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
