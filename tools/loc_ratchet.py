#!/usr/bin/env python3
"""LOC ratchet — the god-files may only shrink (FINAL-RESULT PLAN P0).

Gates the line counts of the refactor targets against `.loc_ratchet.json`
(repo root, committed). Mirrors coverage_ratchet.py semantics, inverted
(lower is better):

  - current > baseline + TOLERANCE  → FAIL (the god-file grew)
  - current < baseline              → baseline auto-ratchets DOWN (commit it)
  - no baseline yet                 → initialized from current, passes

TOLERANCE absorbs small urgent fixes landing before their extraction phase;
the baseline never moves up, so growth cannot compound.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = ROOT / ".loc_ratchet.json"
TOLERANCE = 25  # lines — room for an urgent fix, not for a new feature

TARGETS = ["pipeline.py", "monitor.py", "model_interact.py"]


def main() -> int:
    baseline: dict[str, int] = {}
    if BASELINE_PATH.exists():
        baseline = json.loads(BASELINE_PATH.read_text())
    failed = False
    updated = False
    for rel in TARGETS:
        cur = len((ROOT / rel).read_text(encoding="utf-8").splitlines())
        floor = baseline.get(rel)
        if floor is None:
            baseline[rel] = cur
            updated = True
            print(f"  OK    {rel}: {cur} lines (ratchet initialized)")
        elif cur > floor + TOLERANCE:
            print(f"  FAIL  {rel}: {cur} lines > ratchet {floor} (+{TOLERANCE} tolerance). "
                  f"Extract code into pipeline_core/ instead of growing the god-file.")
            failed = True
        elif cur < floor:
            baseline[rel] = cur
            updated = True
            print(f"  OK    {rel}: {cur} lines (ratchet lowered from {floor})")
        else:
            print(f"  OK    {rel}: {cur} lines (ratchet {floor})")
    if updated:
        BASELINE_PATH.write_text(json.dumps(baseline, indent=2) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
