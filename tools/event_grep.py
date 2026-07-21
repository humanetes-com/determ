#!/usr/bin/env python3
"""Filter `logs/events.jsonl` by run, event name and payload substring.

Exists because the events log is tens of MB and the only way to see what the
model ACTUALLY sent (`llm.response`) is to pull the record out of it. Reading
it with shell tools either dumps megabytes into context or needs a compound
pipeline; this bounds the output at the source.

Usage:
  tools/event_grep.py --event llm.response --contains next_back --tail 2
  tools/event_grep.py --event phase_c.file.fill.done --run-id <id> --keys-only

Defaults to the LATEST run_id present in the file.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

_LOG = Path(__file__).resolve().parent.parent / "logs" / "events.jsonl"


def _latest_run_id(path: Path) -> str | None:
    last = None
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            idx = line.find('"run_id": "')
            if idx != -1:
                end = line.find('"', idx + 11)
                last = line[idx + 11:end]
    return last


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", default=None, help="exact event name")
    ap.add_argument("--contains", default=None,
                    help="substring that must appear in the raw record")
    ap.add_argument("--run-id", default=None, help="default: latest in file")
    ap.add_argument("--all-runs", action="store_true")
    ap.add_argument("--tail", type=int, default=5, help="keep last N matches")
    ap.add_argument("--chars", type=int, default=4000,
                    help="cap per-record payload chars (0 = unlimited)")
    ap.add_argument("--keys-only", action="store_true",
                    help="print ts + event + data keys, never the payload")
    ap.add_argument("--log", default=str(_LOG))
    args = ap.parse_args()

    path = Path(args.log)
    run_id = None if args.all_runs else (args.run_id or _latest_run_id(path))
    hits: list[dict] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if run_id and f'"run_id": "{run_id}"' not in line:
                continue
            if args.event and f'"event": "{args.event}"' not in line:
                continue
            if args.contains and args.contains not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            hits.append(rec)
            if len(hits) > args.tail:
                hits.pop(0)

    print(f"# run_id={run_id or 'ALL'} matches_kept={len(hits)}")
    for rec in hits:
        data = rec.get("data", {})
        head = f"--- {rec.get('ts')} {rec.get('event')} tid={rec.get('tid')}"
        if args.keys_only:
            print(f"{head} keys={sorted(data)}")
            continue
        print(head)
        for key, val in data.items():
            text = val if isinstance(val, str) else json.dumps(val)
            if args.chars and len(text) > args.chars:
                text = text[:args.chars] + f"\n…[{len(text) - args.chars} more chars]"
            print(f"  {key}: {text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
