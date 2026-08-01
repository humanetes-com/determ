#!/usr/bin/env bash
# What killed the pipeline, per recorded death.
#
# Reads logs/deaths.jsonl, written by lib/record_death.sh. Before that wrapper
# existed the pipeline had no parent, so no exit status was ever read and every
# downtime diagnosis was archaeology over gaps in events.jsonl — which is how
# `vllm.budget` came to be believed a death signature when it is merely the
# ambient periodic event (it precedes 9 of 11 gaps, including gaps where
# nothing died).
#
# An empty report is itself a reading: no deaths have been recorded since the
# wrapper was wired in, so any earlier gap is not attributable from here.
set -u
cd "$(dirname "$0")/.." || exit 2
.venv/bin/python - "$@" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path("logs/deaths.jsonl")
if not path.exists():
    print("no deaths recorded yet (logs/deaths.jsonl absent)")
    print("  the pipeline is launched under lib/record_death.sh by continue.sh;")
    print("  a run that started before that was wired in leaves no record here.")
    raise SystemExit(0)

rows = []
for line in path.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        rows.append(json.loads(line))
    except ValueError:
        print(f"  [skipped an unparseable line: {line[:60]}…]")

if not rows:
    print("logs/deaths.jsonl is empty")
    raise SystemExit(0)

limit = int(sys.argv[1]) if len(sys.argv) > 1 else 20
by_cause: dict[str, int] = {}
total_up = 0
for r in rows[-limit:]:
    up = int(r.get("uptime_s", 0))
    total_up += up
    status = int(r.get("exit_status", -1))
    sig = r.get("signal") or ""
    if r.get("deliberate_stop"):
        cause = "deliberate stop"
    elif status == 0:
        cause = "clean exit"
    elif sig:
        cause = f"signal {sig}"
    else:
        cause = f"exit {status}"
    by_cause[cause] = by_cause.get(cause, 0) + 1
    print(f"  {r.get('started_at', '?')} → {r.get('ended_at', '?')}  "
          f"{up // 3600}h{up % 3600 // 60:02d}m  code={r.get('code_id', '?')}  "
          f"{cause}")
    if r.get("dmesg"):
        print(f"      kernel: {r['dmesg'][:150]}")
    if r.get("log_tail"):
        print(f"      last  : {r['log_tail'][:150]}")

print(f"\n{len(rows[-limit:])} death(s), mean uptime "
      f"{total_up // max(1, len(rows[-limit:])) // 60} min")
for cause, n in sorted(by_cause.items(), key=lambda kv: -kv[1]):
    print(f"  {n:3d}  {cause}")
PY
