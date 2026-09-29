#!/usr/bin/env python3
"""Silent watcher: is the fill schedule paying, or is the GPU burning?

WHY: run `82925-203ed7b8` (2026-09-28) spent 6 of its first 7 fills with under
half of the minutes on runs that paid, and every schedule decision passed the
invariant checker, so nothing on the monitor or in the log flagged it. The
reading lives in `domain/schedule_yield.py`; this script polls it so a session
does not have to spend tokens looking.

USAGE: .venv/bin/python scripts/watch_schedule_health.py [--poll S]
       [--deadline S] [--dry-minutes M] [--idle-minutes M]
  Arm with the Bash tool's run_in_background: it prints NOTHING while the
  pipeline is healthy and EXITS on the first finding, which is the one wake
  (re-arm after handling it). Zero GPU, reads the route ledger tail and the
  log tail every POLL (default 120 s), never signals anything. DEADLINE
  (default 3600 s) bounds the wait: on expiry it prints `WATCH TIMEOUT` and
  exits 4 (a healthy run outliving the deadline is not an error; re-arm).

HOW TO INTERPRET: one line, `SCHED-WATCH <kind>: <detail>`, exit 0 for any
  finding, 4 on timeout, 2 when no ledger row exists yet after the grace.
  kinds:
  - BURN: M minutes (default 90) of wall clock since the last cashed route
    run closed (or since the run started, nothing cashed), counting the run
    still in flight. The live "burning without progress" reading; answer
    with the per-fill analysis. It was fill-granular first and missed run
    24951-a72c4642 (2026-09-29): fill 1 cashed +7 at 05:30, then 50 flat
    minutes that a fill-level streak could not see.
  - IDLE: the pipeline is up but no route run has closed for M minutes
    (default 120): a long fill or a hang; check the fill route row.
  - INVARIANT: a `HEALTH ‼ SCHEDULE_INVARIANT` line since the run started; the
    schedule's construction has a hole and the checker line is the finding.
  - DELAYED PAYER: a run cashed after the schedule had deferred a sibling of
    it for k fills (new since arming); the detail names fills and reasons.
    Lagging by nature: it is known only once the run pays.
  - DEAD: no `pipeline.py --project` process; the death record is in
    logs/deaths.jsonl.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline_core.domain.schedule_yield import dry_verdict  # noqa: E402
from pipeline_core.monitor.schedule_health import read_schedule_health  # noqa: E402

LOG = ROOT / "logs" / "pipeline_anyhow.log"
EVENTS = ROOT / "logs" / "events.jsonl"


def alive() -> bool:
    # bracketed first letter: the pattern cannot match this process's own argv
    return subprocess.run(["pgrep", "-f", "[p]ipeline.py --project"],
                          capture_output=True).returncode == 0


def live_run() -> tuple[str, str, str]:
    """(run_id, start ts ISO, this run's log text) of the newest launch. The
    ledger outlives runs, so its rows count only when they carry this id."""
    try:
        text = LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", "", ""
    start = max(text.rfind("PIPELINE STARTED"), 0)
    head = text[start:start + 400]
    m = re.search(r"STARTED — (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) UTC", head)
    r = re.search(r"run_id=(\S+)", head)
    ts = m.group(1).replace(" ", "T") + "+00:00" if m else ""
    return (r.group(1) if r else ""), ts, text[start:]


def _when(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def minutes_since(ts: str) -> float:
    try:
        t = datetime.fromisoformat(ts)
    except ValueError:
        return 0.0
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return (datetime.now(UTC) - t).total_seconds() / 60


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--poll", type=float, default=120)
    ap.add_argument("--deadline", type=float, default=3600)
    ap.add_argument("--dry-minutes", type=float, default=90)
    ap.add_argument("--idle-minutes", type=float, default=120)
    a = ap.parse_args()
    t0 = time.monotonic()
    seen_payers: int | None = None
    seen_run = ""
    while time.monotonic() - t0 < a.deadline:
        if not alive():
            print("SCHED-WATCH DEAD: no pipeline.py --project process")
            return 0
        run_id, started, text = live_run()
        if "SCHEDULE_INVARIANT" in text:
            print("SCHED-WATCH INVARIANT: SCHEDULE_INVARIANT in this run's log")
            return 0
        h = read_schedule_health(EVENTS)
        if h is None or h.run_id != run_id:
            idle = minutes_since(started)
            if started and idle >= a.idle_minutes:
                print(f"SCHED-WATCH IDLE: run {run_id} closed no route run in "
                      f"{idle:.0f} min")
                return 0
        else:
            d = dry_verdict(h.reading, datetime.now(UTC),
                            run_start=_when(started), minutes=a.dry_minutes)
            if d.burning:
                print(f"SCHED-WATCH BURN: run {h.run_id} {d.detail}")
                return 0
            idle = minutes_since(h.last_row_started)
            if idle >= a.idle_minutes:
                print(f"SCHED-WATCH IDLE: run {h.run_id} last route run started "
                      f"{idle:.0f} min ago")
                return 0
            if h.run_id != seen_run:
                seen_run, seen_payers = h.run_id, len(h.reading.payers)
            elif seen_payers is not None and len(h.reading.payers) > seen_payers:
                p = h.reading.payers[-1]
                print(f"SCHED-WATCH DELAYED PAYER: run {h.run_id} fill {p.fill} "
                      f"{p.label[:50]} waited {p.delay_fills} fill(s) "
                      f"({','.join(p.reasons)})")
                return 0
        time.sleep(a.poll)
    print(f"SCHED-WATCH TIMEOUT after {a.deadline:.0f}s, pipeline "
          f"{'up' if alive() else 'down'} — re-arm to keep watching")
    return 4


if __name__ == "__main__":
    sys.exit(main())
