#!/usr/bin/env python3
"""Flow auditor CLI — checks a pipeline run's BEHAVIOR against flow invariants.

Reads what the run already recorded (events.jsonl, the content-addressed
logs/prompts/ store, the pipeline log) and issues verdicts on the properties
that unit tests structurally cannot see — "every component works but the
information doesn't flow between them" (the session-63 finding: 15 iterations
plateaued at 67/123 because t4's genesis lessons never reached a coding
prompt and every iteration wiped the fix loop's progress).

Checks
  C1 repo-spec-channel   coding prompts after a t4 genesis update carry
                         <REPOSITORY_SPECIFICATIONS>
  C2 genesis-propagation distinctive genesis.md lines appear in later coding prompts
  C3 floor-monotonicity  iteration pass counts never regress without a
                         ratchet.restore correcting it
  C4 plateau             N+ consecutive identical pass counts flagged
  C5 no-wipe             no "Cleaning up ... for retry" after run start
  C6 ratchet-sanity      ratchet.save strictly improves; restore only below best

Usage:
  python tools/flow_audit.py [--events logs/events.jsonl] [--prompts logs/prompts]
      [--log logs/pipeline_anyhow.log] [--project tmp/anyhow]
      [--since ISO8601] [--plateau 3]

Exit code: 1 if any check FAILs (WARN does not fail — findings, not errors).

The core lives in pipeline_core/audit.py (shared with the pipeline's --audit
self-audit and the monitor's audit row); this file is the CLI.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline_core.audit import (  # noqa: E402,F401  (re-exported for tests)
    FAIL,
    PASS,
    SKIP,
    WARN,
    RunData,
    Verdict,
    audit,
    iteration_series,
    load_run,
    parse_run_start,
)


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--events", type=Path, default=here / "logs/events.jsonl")
    ap.add_argument("--prompts", type=Path, default=here / "logs/prompts")
    ap.add_argument("--log", type=Path, default=here / "logs/pipeline_anyhow.log")
    ap.add_argument("--project", type=Path, default=None,
                    help="workspace dir (enables genesis-propagation probe)")
    ap.add_argument("--since", default="", help="ISO ts; default: last PIPELINE STARTED in --log")
    ap.add_argument("--plateau", type=int, default=3)
    args = ap.parse_args(argv)

    verdicts = audit(args.events, args.prompts, args.log,
                     args.project, args.since, args.plateau)
    width = max(len(v.check) for v in verdicts)
    failed = False
    for v in verdicts:
        print(f"{v.check.ljust(width)}  {v.status:4s}  {v.evidence}")
        failed |= v.status == FAIL
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
