#!/usr/bin/env python3
"""Watch a live fill's pass trajectory and stop the run when it goes flat.

WHY
    A TDD fill takes a whole-oracle reading after every keep
    (`[phase-c/fill-reading] pass A -> B`), so the score is visible mid-fill;
    the next `[phase-c/tdd] iter N: cargo test` sweep reads the same tree and
    cannot reveal a jump the fill readings did not already show. On run
    1490879-d61526c9 (2026-11-05) fill 1 sat at 69 after ~31 readings while
    walking the construct -> vtable -> ptr producer chain; the comparable
    from-scratch run 1604111-bb3716ef had the same shape (flat 61/67 for 26
    readings, 69 at ~33) and then climbed to 91 by the end of the fill. The
    question "does the chain close and cash, or does it stay flat" is answered
    by the fill readings, and an unattended night should not spend GPU on a
    fill that stopped cashing. The flat stretch of the comparison run lasted
    about an hour, which is why the default flat window is two checks (40
    min) rather than one.

USAGE
    python scripts/watch_fill_trajectory.py [--baseline N] [--first-check-min 60]
        [--every-min 20] [--flat-checks 2] [--sweep-min-gain 10]
        [--deadline-min 300] [--log logs/pipeline_anyhow.log] [--dry-run]
    Arm as a background command from the session. Reads only the log; the one
    action it takes is `touch /tmp/pipeline_graceful_stop` (the documented
    graceful stop; continue.sh clears a leftover sentinel before any new
    launch). --dry-run never touches it. Readings are taken after the last
    `[phase-c/ledger] iter 0: cluster entry` line across the log and its two
    newest rotations, so a log rotation mid-watch loses nothing. The sweep the
    watch waits for is the first `iter N: cargo test` summary with N greater
    than the iteration current at arm time. Liveness is
    `pgrep -f "[p]ipeline.py --project"` (bracketed so it cannot match itself).

HOW TO INTERPRET THE OUTPUT
    One `CHECK` line per scheduled check: minutes since arm, the current pass
    (last fill reading or sweep), the value it is compared against, verdict.
    Exactly one terminal line, then exit:
      OK sweep iter=N pass=X (>= baseline+gain)       exit 0  stop monitoring, healthy
      STOPPED sweep iter=N pass=X below baseline+gain exit 2  sentinel touched
      STOPPED flat: pass X not above Y for K check(s) exit 2  sentinel touched
      GONE pipeline not running at <min> min, pass=X  exit 1  read the death row
      DEADLINE <min> min, no sweep, pass=X            exit 4  predicate-bug bound; re-arm or read
    A STOPPED line means the fill stopped cashing: the next step is the
    per-fill reading of what the last keeps were blocked on, not a relaunch.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

SENTINEL = Path("/tmp/pipeline_graceful_stop")
ENTRY = "[phase-c/ledger] iter 0: cluster entry"
FILL_RE = re.compile(r"\[phase-c/fill-reading\] pass \d+ -> (\d+)")
SWEEP_RE = re.compile(r"\[phase-c/tdd\] iter (\d+): cargo test .*?pass=(\d+)")


def read_run_lines(log: Path) -> list[str]:
    lines: list[str] = []
    for p in (log.with_name(log.name + ".2"), log.with_name(log.name + ".1"), log):
        if p.exists():
            lines.extend(p.read_text(errors="replace").splitlines())
    start = max((i for i, ln in enumerate(lines) if ENTRY in ln), default=-1)
    return lines[start + 1:]


def state(log: Path) -> tuple[int | None, int | None, int | None]:
    """(current pass, latest sweep iter, latest sweep pass)."""
    cur = it = sp = None
    for ln in read_run_lines(log):
        m = SWEEP_RE.search(ln)
        if m:
            it, sp = int(m.group(1)), int(m.group(2))
            cur = sp
            continue
        m = FILL_RE.search(ln)
        if m:
            cur = int(m.group(1))
    return cur, it, sp


def alive() -> bool:
    r = subprocess.run(["pgrep", "-f", "[p]ipeline.py --project"],
                       capture_output=True, text=True)
    return r.returncode == 0


def stop(dry: bool) -> None:
    if not dry:
        SENTINEL.touch()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="logs/pipeline_anyhow.log")
    ap.add_argument("--baseline", type=int, default=None,
                    help="pass to beat at the first check (default: current)")
    ap.add_argument("--first-check-min", type=float, default=60)
    ap.add_argument("--every-min", type=float, default=20)
    ap.add_argument("--flat-checks", type=int, default=2)
    ap.add_argument("--sweep-min-gain", type=int, default=10)
    ap.add_argument("--deadline-min", type=float, default=300)
    ap.add_argument("--poll-s", type=float, default=60)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    log = Path(a.log)

    cur, arm_iter, _ = state(log)
    baseline = a.baseline if a.baseline is not None else (cur or 0)
    arm_iter = arm_iter or 0
    print(f"ARMED baseline={baseline} current={cur} sweep-iter-at-arm={arm_iter} "
          f"first-check={a.first_check_min}m every={a.every_min}m "
          f"flat-checks={a.flat_checks} sweep-gain>={a.sweep_min_gain} "
          f"deadline={a.deadline_min}m dry-run={a.dry_run}", flush=True)

    t0 = time.monotonic()
    next_check = a.first_check_min
    history: list[int] = [baseline]  # value at arm, then one per check
    while True:
        mins = (time.monotonic() - t0) / 60
        cur, it, sp = state(log)
        if it is not None and it > arm_iter and sp is not None:
            if sp >= baseline + a.sweep_min_gain:
                print(f"OK sweep iter={it} pass={sp} (>= {baseline}+{a.sweep_min_gain})", flush=True)
                return 0
            stop(a.dry_run)
            print(f"STOPPED sweep iter={it} pass={sp} below {baseline}+{a.sweep_min_gain}", flush=True)
            return 2
        if not alive():
            print(f"GONE pipeline not running at {mins:.0f} min, pass={cur}", flush=True)
            return 1
        if mins >= a.deadline_min:
            print(f"DEADLINE {mins:.0f} min, no sweep, pass={cur} — watcher bound hit, run left alive", flush=True)
            return 4
        if mins >= next_check:
            value = cur if cur is not None else history[-1]
            # first check: must beat the arm-time baseline; later: must beat
            # the value flat-checks ago
            ref = history[0] if len(history) == 1 else history[max(0, len(history) - a.flat_checks)]
            ok = value > ref
            print(f"CHECK {mins:.0f}m pass={value} vs {ref} -> {'rising' if ok else 'FLAT'}", flush=True)
            history.append(value)
            if not ok:
                stop(a.dry_run)
                k = 1 if len(history) == 2 else a.flat_checks
                print(f"STOPPED flat: pass {value} not above {ref} for {k} check(s)", flush=True)
                return 2
            next_check += a.every_min
        time.sleep(a.poll_s)


if __name__ == "__main__":
    sys.exit(main())
