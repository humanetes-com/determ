#!/usr/bin/env bash
# WHY: the /interactive-run skill needs the assistant woken IN-SESSION when the
#   pipeline stops, with a self-describing trigger line — a cold `claude -p`
#   monitor re-derives the run from scratch every wake and reports symptoms
#   (recorded 2026-08-17; the in-session watcher is the standing alternative).
#   This script is the wake mechanism: it blocks while the pipeline runs and
#   EXITS when the pipeline is gone, so a Bash run_in_background task (or
#   Monitor) armed on it delivers exactly one notification per stop.
#
# USAGE: bash scripts/interactive_run_watch.sh [--poll SECONDS]
#   Arm it with the Bash tool's run_in_background from the session that wants
#   the wake — the script itself is a plain blocking watcher; it has no
#   daemonizing mode. Any flag it does not know is a hard error (exit 64),
#   never silently ignored: on 2026-08-18 a session invoked it with `--arm`
#   (a flag a skill doc described but nothing implemented) and the old
#   silent-ignore parsing would have blocked the calling turn in the
#   foreground for the pipeline's whole lifetime instead of failing fast.
#   Preconditions: a pipeline launched via continue.sh (it finds the newest
#   `pipeline.py --project` process; if none is up yet it waits up to
#   $STARTUP_GRACE (default 120 s) for one to appear, so it can be armed
#   immediately after `continue.sh --y --no-tail` returns).
#   Env: POLL (default 5) seconds between checks; STARTUP_GRACE (default 120);
#   DEADLINE (default 3600) seconds the death-watch may run before it wakes
#   and reports a timeout — the bound on a predicate bug, per the standing
#   watcher rule (unbounded watchers have sat stuck for hours, e.g. the
#   self-matching pgrep of 2026-08-18, found 4h53m old). A healthy pipeline
#   outliving the deadline is NOT an error: the wake says so and the loop is
#   continued by re-arming.
#   Cost: zero GPU, one pgrep per poll. Safe to kill (TaskStop) at any time;
#   it never signals the pipeline or anything else.
#
# HOW TO INTERPRET THE OUTPUT: silent while the pipeline runs. On exit it
#   prints, in order:
#   - the trigger line "THIS IS PART OF INTERACTIVE RUN: the pipeline stopped —
#     load handover and check." — the post-/clear context's cue to load
#     handover.md and re-enter the /interactive-run skill loop;
#   - `watched pid=<pid> gone after <seconds>s` — which process it watched;
#   - the last record of logs/deaths.jsonl (exit_status/signal/deliberate_stop/
#     log_tail) when the death-watch wrote one, `no death record (yet)` when it
#     did not — deaths.jsonl can lag the process exit by a few seconds, so an
#     absent record means "read the log", not "no record exists".
#   Exit 0 = pipeline observed then gone (the normal wake). Exit 2 = no
#   pipeline appeared within STARTUP_GRACE — the launch failed; check
#   continue.sh output and the gate (a stale stamp exits 3) before re-arming.
#   Exit 4 = deadline expired with the pipeline still alive — re-arm to keep
#   watching. Exit 64 = unknown argument.

set -u
POLL="${POLL:-5}"
STARTUP_GRACE="${STARTUP_GRACE:-120}"
DEADLINE="${DEADLINE:-3600}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --poll)
      if [ -z "${2:-}" ]; then
        echo "usage: $0 [--poll SECONDS]  (--poll needs a value)" >&2; exit 64
      fi
      POLL="$2"; shift 2 ;;
    *)
      echo "usage: $0 [--poll SECONDS]  (unknown argument: $1 — this script has no daemon/--arm mode; arm it via a background task)" >&2
      exit 64 ;;
  esac
done

find_pid() {
  pgrep -f 'python.*pipeline\.py --project' | head -1
}

waited=0
pid="$(find_pid)"
while [ -z "$pid" ] && [ "$waited" -lt "$STARTUP_GRACE" ]; do
  sleep "$POLL"; waited=$((waited + POLL)); pid="$(find_pid)"
done
if [ -z "$pid" ]; then
  echo "THIS IS PART OF INTERACTIVE RUN: no pipeline appeared within ${STARTUP_GRACE}s — the launch failed; check continue.sh output and the pre-run gate."
  exit 2
fi

start=$(date +%s)
while kill -0 "$pid" 2>/dev/null; do
  if [ $(( $(date +%s) - start )) -ge "$DEADLINE" ]; then
    echo "THIS IS PART OF INTERACTIVE RUN: watcher deadline expired after ${DEADLINE}s — pipeline pid=${pid} is STILL ALIVE. Not an error; re-arm the watcher to keep the loop."
    exit 4
  fi
  sleep "$POLL"
done
# Give the supervisor's death-watch a moment to append its record.
sleep 3

echo "THIS IS PART OF INTERACTIVE RUN: the pipeline stopped — load handover and check."
echo "watched pid=${pid} gone after $(( $(date +%s) - start ))s"
if [ -s logs/deaths.jsonl ]; then
  echo "last death record:"
  tail -1 logs/deaths.jsonl
else
  echo "no death record (yet) — read logs/pipeline_anyhow.log directly."
fi
