#!/usr/bin/env python3
"""Coverage ratchet — per-target floors that only go up (slice 13).

Reads the combined coverage data produced by the check.sh pytest run and
gates each target against `.coverage_ratchet.json` (repo root, committed):

  - current < baseline - TOLERANCE  → FAIL (refactor lost coverage)
  - current > baseline              → baseline auto-ratchets up (commit it)
  - no baseline yet                 → initialized from current, passes

So newly tested code (e.g. the fill-loop harness) permanently raises the
floor, and a refactor that drops tests cannot pass. MIN_FLOORS are absolute
minima that apply even before a baseline exists.

`pipeline_all` (pipeline.py + pipeline_core/*) is the RELOCATION-INVARIANT
gate: moving covered code out of the pipeline.py god-file into pipeline_core
does NOT change the combined number, so the strangler-fig refactor isn't
penalised for shrinking pipeline.py — only a genuine coverage loss drops it.
(A pipeline.py-only floor wrongly falls every time covered code relocates; the
god-file's shrinking is already enforced by the LOC ratchet, not here.)
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = ROOT / ".coverage_ratchet.json"
TOLERANCE = 0.2  # percentage points — guards against line-count jitter

# label → coverage include-glob
TARGETS: dict[str, str] = {
    "pipeline_all": "pipeline.py,pipeline_core/*",  # relocation-invariant combined gate
    "pipeline_core": "pipeline_core/*",
    "pipeline_core.domain": "pipeline_core/domain/*",
}
# Absolute minima (pre-ratchet guarantees). domain keeps its historical 95.
MIN_FLOORS: dict[str, float] = {"pipeline_core.domain": 95.0}


def measured(include: str) -> float:
    out = subprocess.run(
        [sys.executable, "-m", "coverage", "report",
         f"--include={include}", "--format=total"],
        capture_output=True, text=True, cwd=ROOT, check=True,
    )
    return float(out.stdout.strip())


def main() -> int:
    baseline: dict[str, float] = {}
    if BASELINE_PATH.exists():
        baseline = json.loads(BASELINE_PATH.read_text())
    failed = False
    updated = False
    for label, include in TARGETS.items():
        cur = measured(include)
        floor = max(baseline.get(label, 0.0), MIN_FLOORS.get(label, 0.0))
        if cur + TOLERANCE < floor:
            print(f"  FAIL  {label}: {cur:.1f}% < ratchet floor {floor:.1f}%")
            failed = True
        elif cur > baseline.get(label, 0.0):
            baseline[label] = round(cur, 1)
            updated = True
            print(f"  OK    {label}: {cur:.1f}% (ratchet raised to {cur:.1f}%)")
        else:
            print(f"  OK    {label}: {cur:.1f}% (floor {floor:.1f}%)")
    if updated:
        BASELINE_PATH.write_text(json.dumps(baseline, indent=2) + "\n")
        print(f"  baseline updated: {BASELINE_PATH.name} (commit it)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
