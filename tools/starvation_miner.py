#!/usr/bin/env python3
"""Starvation miner CLI.

Ranks WHY fills failed in a run: for each phase_c fill-fail event, correlates
the LLM exchanges that produced it (same thread, inside the fill window,
prompt mentions the unit), classifies the last attempt into a
StarvationCause, and prints the ranked evidence table.

    .venv/bin/python tools/starvation_miner.py                 # latest run
    .venv/bin/python tools/starvation_miner.py --run-id 187469-e7e7dad0
    .venv/bin/python tools/starvation_miner.py --json out.json
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline_core.application.starvation_report import (  # noqa: E402
    mine_run,
    render_report,
)

_ROOT = Path(__file__).resolve().parent.parent


def _iter_events(path: Path):
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def _latest_run_id(path: Path) -> str | None:
    run_id: str | None = None
    for e in _iter_events(path):
        rid = e.get("run_id")
        if isinstance(rid, str) and rid:
            run_id = rid
    return run_id


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--events", type=Path, default=_ROOT / "logs" / "events.jsonl")
    ap.add_argument("--prompts", type=Path, default=_ROOT / "logs" / "prompts")
    ap.add_argument("--json", type=Path, default=None,
                    help="also write the post-mortems as JSON")
    args = ap.parse_args()

    run_id = args.run_id or _latest_run_id(args.events)
    if not run_id:
        print("no run_id found in event stream (old events lack run_id)")
        return 1

    def read_blob(sha: str) -> str | None:
        p: Path = args.prompts / f"{sha}.txt"
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    pms = mine_run(_iter_events(args.events), run_id, read_blob)
    print(render_report(pms, run_id))
    if args.json:
        args.json.write_text(json.dumps(
            [dataclasses.asdict(pm) for pm in pms], indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
