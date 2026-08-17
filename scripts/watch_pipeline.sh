#!/usr/bin/env bash
# WHY this script exists
# ---------------------
# A live run has to be watched at the ITERATION BOUNDARY, and a filter that
# matches only the `iter N: cargo test rc=...` summary cannot tell a crashloop
# from a slow iteration — silence and health look identical. Two runs paid for
# that lesson: `1692702-94e6163a` sat 3h36m with `stall` pinned at 0/3 and no
# self-terminating path, and `1896714-8b14ff74` destroyed 79 filled bodies in a
# single layout-retrigger fire that took 16m23s to become visible. Both are
# loud in the log the moment they happen; nobody was reading the right lines.
#
# So this filter carries three families, not one:
#   1. the state vector (`phase-c/tdd] iter ...`) — one line per iteration;
#   2. the DESTRUCTIVE events (a retrigger fire, a re-stub, a high-water
#      re-base, a ratchet revert) — each one invalidates the score every other
#      gate is reading;
#   3. the TERMINAL signatures, including the ones that mean the run ended
#      WELL (`NO_VERDICT_STOP`, `MODULE_PLATEAU_ABORT`) and the ones that mean
#      it died (`Traceback`, `TIMED OUT`, process gone).
#
# The process-liveness poll is the part a `tail -f` cannot supply: when the
# pipeline exits — gracefully, on a guard, or on a SIGKILL — the log simply
# stops growing, and a tail emits nothing at all. The poll turns that silence
# into one explicit `PIPELINE_GONE` event.
#
# USAGE
# -----
#   bash scripts/watch_pipeline.sh [LOG] [MATCH]
#     LOG    log file to follow      (default logs/pipeline_anyhow.log)
#     MATCH  pgrep -f pattern for the pipeline process
#                                    (default "pipeline.py --project anyhow")
#
# Intended as the `command` of an in-session Monitor, persistent, so every
# stdout line becomes one notification. It owns no GPU and spends no model
# calls: it only reads. Runs until the pipeline process disappears, then exits
# 0 after emitting PIPELINE_GONE. Safe to run against a log that does not exist
# yet (`tail -F` waits for it), which is what makes it survivable across a
# relaunch that rotates the log.
#
# HOW TO INTERPRET THE OUTPUT
# ---------------------------
# Every line is `HH:MM:SS | <log line>`, with the component tuple collapsed to
# the literal `<component>` and the result truncated to 300 chars. Collapsing
# BEFORE truncating is not cosmetic: `[t1.5]` and `[layout-retrigger]` lines
# name their component as a 34-element tuple that is itself ~290 characters, so
# a plain `cut -c1-300` shows the tuple and nothing else. The first arming of
# this watch lost exactly that — the event `00:07:15 | [t1.5] stable/('Error',
# 'Backtrace', ...` carried no verb at all, and the outcome it was reporting
# (`authored scaffold did not compile (attempt 3)`) sat past the cut. The
# component is noise at the watch level; the verb after the colon is the whole
# signal.
#
#   iter N: cargo test rc=... pass=X fail=Y oracle=Z (improved=..., stall=a/b);
#   frontier=F fn(s) (merged), E skipped as exhausted
#     The state vector. Read `pass` for the score, `stall=a/b` for the
#     terminator's clock, and `frontier`/`skipped as exhausted` as a PAIR
#     across iterations — a frontier that grows while `skipped` grows is the
#     deadlock shape, a frontier that falls with fills landing is the healthy
#     re-climb. The score alone distinguishes neither.
#
#   layout retrigger fired / reverted N fn(s) / high-water re-based
#     A DELIBERATE destruction. The next sweep's `pass` describes a different
#     tree than the last one did; any comparison across that line is void.
#     Expect a large drop and expect it to be exempted from the ratchet.
#
#   NO_VERDICT_STOP / MODULE_PLATEAU_ABORT / plateau
#     The run terminated ITSELF. This is the success case for a watch: the
#     loop proved it was not moving instead of burning the night proving it.
#
#   NEVER LANDED / re-author died / DROPPING / refus
#     The fire spent an authoring round and applied nothing. One is a fact;
#     a streak is the "fire that never lands" class, and the streak counter is
#     printed on the refund line itself.
#
#   PIPELINE_GONE
#     No process matches MATCH any more. Pair it with the last log line above
#     it: a `NO_VERDICT_STOP` immediately before means a clean self-stop, and
#     nothing at all before means a kill, an OOM, or a crash outside the log.
#
# Absence of output is NOT evidence of health for more than one iteration —
# iterations run minutes, not hours. If nothing arrives for longer than the
# recent inter-iteration gap, read the log directly.

set -u

LOG="${1:-logs/pipeline_anyhow.log}"
MATCH="${2:-pipeline.py --project anyhow}"

cd "$(dirname "$0")/.." || exit 1

SIGNATURES='phase-c/tdd\] iter|layout retrigger fired|re-author died|NEVER LANDED|high-water re-based|NOT re-admitting|NO_VERDICT_STOP|MODULE_PLATEAU_ABORT|PLATEAU|GPU_GUARD_STOP|VERDICT MISMATCH|TIMED OUT|Traceback|batch ratchet — breach|reverted [0-9]+ (referencing )?fn\(s\)|DROPPING their component|refuses the fire|oracle shrink reproduced|authored scaffold did not compile|authored layout compiles'

tail -F -n 0 "$LOG" 2>/dev/null \
  | grep -E --line-buffered "$SIGNATURES" \
  | sed -E -u "s/\(('[^)]*')\)/<component>/g; s/\[('[^]]*')\]/<component>/g" \
  | awk '{ "date +%H:%M:%S" | getline t; close("date +%H:%M:%S");
           print t " | " substr($0, 1, 300); fflush() }' &
TAIL_PID=$!

trap 'kill -- -$$ 2>/dev/null' EXIT

while true; do
    if ! pgrep -f "$MATCH" > /dev/null 2>&1; then
        sleep 5
        if ! pgrep -f "$MATCH" > /dev/null 2>&1; then
            sleep 2
            echo "$(date +%H:%M:%S) | PIPELINE_GONE — no process matches '$MATCH'"
            kill "$TAIL_PID" 2>/dev/null
            exit 0
        fi
    fi
    sleep 20
done
