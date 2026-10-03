#!/usr/bin/env python3
"""WHY: task0c holds a met line target while an unexecuted function is inside
its attempt budget. The budget must count passes that were AIMED at the
function (a prompt carried it and a block came back), not blocks that happened
to compile. This reads the per-cluster function records the run left on disk
and the task0c attempt log, and prints, per function ever held:
the attempts charged, the outcome of each, and the verdict the retrospective
attached. Records written before outcomes were recorded show no outcomes.

USAGE: .venv/bin/python scripts/measure_fn_budget_accounting.py [workspace]
       (default: tmp/anyhow)

HOW TO INTERPRET: a pending function with fewer charges than the passes
the log shows for its file, or any `compile_failed` outcome that is not a
charged attempt, is the defect. A function that closed on its own (executed)
is absent from the listing: it never reached the budget (negative control).
After the fix every line has charges == passes aimed, and a pending function
never carries more than TASK0C_FN_ATTEMPTS-1 charges at a reading.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline_core.domain.fn_execution import FnCoverageRecord  # noqa: E402
from pipeline_core.domain.models import FnSite  # noqa: E402


def _label(site: FnSite) -> str:
    return f"{site.file}::{site.fn_name}" + (f" [{site.container}]" if site.container else "")


def main() -> int:
    ws = Path(sys.argv[1] if len(sys.argv) > 1 else "tmp/anyhow")
    d = ws / ".cluster_augmented"
    for p in sorted(d.glob("*.fns.json")) if d.is_dir() else ():
        rec = FnCoverageRecord.from_json(p.read_text(encoding="utf-8"))
        if rec is None:
            print(f"{p.name}: another criterion, skipped")
            continue
        print(f"{p.name}: {rec.summary()}")
        for tag, rows in (("pending", [(x.target.site, x.attempts, x.outcomes, x.verdict) for x in rec.pending]),
                          ("unreached", [(x.site, x.attempts, x.outcomes, x.verdict) for x in rec.unreached])):
            for site, attempts, outcomes, verdict in rows:
                print(f"  {tag:9} {_label(site)}: charged {len(attempts)} {list(attempts)} "
                      f"outcomes {list(outcomes) or 'not recorded'} verdict {verdict or '-'}")
    log = ws / ".task0c_attempts.jsonl"
    if log.is_file():
        by = Counter()
        for line in log.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            by[(row.get("file"), row.get("outcome"))] += 1
        print("attempt log, blocks per (file, outcome):")
        for (f, o), n in sorted(by.items(), key=lambda kv: str(kv[0])):
            print(f"  {f} {o}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
