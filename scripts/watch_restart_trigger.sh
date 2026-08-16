#!/usr/bin/env bash
#
# WHY THIS EXISTS
# ---------------
# The decision "is it time to restart this run" was being taken by reading the
# log from scratch each time, and the reading that matters arrives once every
# few hours. Run `669347-cf33eae3` (2026-08-15) produced TWO scored sweeps in
# 6h24m and 197 model calls; between them there is nothing on the summary line
# to read, and after them the answer was already decided. The four runs before
# it each re-derived the same conclusion by hand and each restarted onto the
# same tree.
#
# The specific thing being watched for is the attractor. Eight of the twelve
# scored sweeps between 2026-08-14 14:32 and 2026-08-15 16:54 read exactly
# pass=39, across four runs, and the whole 79->39 deficit is two binaries that
# no longer execute (`unittests src/lib.rs` 37/0 and `tests/test_convert.rs`
# 4/0 vanish from the `iter N binaries:` vector; every surviving binary is
# byte-identical to the 79 reading). A sweep that lands on 39 again is
# therefore not new information — it is the run telling you it has nothing
# left to say, and the GPU-hours after it are spent re-purchasing a number
# already in the log.
#
# It also watches for the mechanism that CREATES the attractor, because it is
# one-way more often than not. Run `1082156-4da4faf7` iter 1 fired the crash
# retrigger on a 79/101 tree, re-authored `['StdError']` and reverted 25
# bodies; nothing was above 39 in the 30 hours after it. The loop can now take
# such a tree back — `restore_cluster_best_tree` puts both halves of
# `.best_tree/<cluster>` back and the flat-score guard calls it before it stops
# — but only when the slot holds the `_layout/` half, and it spends that
# restore ONCE per process. So a fire is still worth waking for BEFORE its
# re-fill window closes: if the slot cannot supply a pair, or the restore has
# already been spent, the re-stub is as permanent as it ever was, and the
# operator lever (`scripts/restore_cluster_tree_from_sha.sh`) is the only route
# left.
#
# USAGE
# -----
#   bash scripts/watch_restart_trigger.sh            # poll forever, stdout = events
#
# Environment knobs (all optional):
#   PIPELINE_LOG      default logs/pipeline_anyhow.log
#   PIPELINE_EVENTS   default logs/events.jsonl
#   WATCH_POLL_S      default 60    seconds between polls
#   WATCH_STALE_MIN   default 150   minutes without a scored sweep before the
#                                   first cadence trigger. Set from a measured
#                                   fill-pass interval, not a hope: on run
#                                   `1064759-0f23fb68` the dispatch cap did
#                                   bound the batch correctly (12 dispatched,
#                                   12 filled) and the pass still took ~90
#                                   minutes — 25 calls at ~3.4 min each, run
#                                   effectively serially because the batch
#                                   landed inside one file and the per-impl
#                                   route is concurrent ACROSS files but serial
#                                   WITHIN one. 90 was tried first and fired on
#                                   a healthy pass. The threshold has to sit
#                                   above the normal interval or it reports the
#                                   loop working as the loop stalling.
#   WATCH_FLOOR       default 79    the score the run is being held to. Seeded
#                                   from the tree it STARTED on, not from zero:
#                                   a sweep is only progress if it beats the
#                                   position that was restored to reach it.
#
# Costs nothing and owns no GPU: it reads two files and runs `pgrep`. Safe to
# run beside a live pipeline. It never writes, never stops the run, and never
# decides anything on its own — it only wakes the reader at the points where
# the decision is actually available.
#
# HOW TO INTERPRET THE OUTPUT
# ---------------------------
# EVERY scored sweep is emitted, because a sweep is the only measurement the
# loop takes and the whole point of watching is to read each one. Each carries
# the full state vector, so the reading needs no other source:
#
#   SWEEP-UP    beat the best score seen since the watch was armed. This is
#               the only class that means reconstruction happened.
#   SWEEP-FLAT  equalled it. Expected for a sweep or two after a re-stub;
#               three in a row is a plateau, and the frontier and the
#               exhausted count on the same line say which kind.
#   SWEEP-DOWN  fell below it. Read it against the last retrigger-restub
#               before deciding it is bad news — a deliberate re-stub is
#               SUPPOSED to drop the score, and the drop is only a finding if
#               no re-fill window is open.
#
#   TRIGGER  a restart is now the cheaper option; act or say why not.
#   INFO     context that changes what a later event means, emitted so the
#            event does not have to carry its own history.
#
# The TRIGGER classes, and what each one means you should do:
#   TRIGGER stale=Nmin        No scored sweep in N minutes. The run is alive
#                             but not measuring; every minute past here is
#                             unobserved. Check whether the batching work is
#                             in the running process before concluding the
#                             loop is stuck — a run launched before it carries
#                             none of it.
#   TRIGGER retrigger-restub  A layout retrigger reverted bodies. This is the
#                             79->39 mechanism. The tree just lost its score
#                             on purpose and nothing can put it back; the next
#                             few sweeps decide whether the re-climb holds.
#   TRIGGER terminal          MODULE_PLATEAU_ABORT / VERDICT MISMATCH /
#                             TIMED OUT / Traceback / GPU_GUARD_STOP. The
#                             run is over or wedged. GPU_GUARD_STOP is the
#                             benign one: the loop stopped ITSELF because a
#                             dispatch was about to be spent on a score the
#                             last sweeps had already read. Nothing is
#                             convicted and every fill is on disk, so the
#                             decision it hands over is what to CHANGE
#                             before relaunching, not whether to relaunch.
#   TRIGGER process-gone      No `pipeline.py --project` process. Read the tail
#                             and `logs/deaths.jsonl` before relaunching — a
#                             deliberate stop and a mechanical failure are
#                             different facts to the retrigger's budget.
#
# Silence means the loop is filling and has not reached a measurement. That is
# the expected state for hours at a time and is NOT success; the cadence
# trigger is what stops silence being read as progress.
#
set -u

cd "$(dirname "$0")/.." || exit 1

LOG="${PIPELINE_LOG:-logs/pipeline_anyhow.log}"
EVENTS="${PIPELINE_EVENTS:-logs/events.jsonl}"
POLL="${WATCH_POLL_S:-60}"
STALE_MIN="${WATCH_STALE_MIN:-150}"
FLOOR="${WATCH_FLOOR:-79}"
best="$FLOOR"       # best score seen since arming; sweeps are judged on it

# `iter N binaries:` is in here as well as the `cargo test` summary because a
# sweep followed by a retrigger fire never PRINTS its summary — the retrigger's
# `continue` swallows it, and the `phase_c.tdd.iter` event is emitted later, at
# the end of the fire. Watching only the summary therefore reads a swept-and-
# retriggered iteration as no sweep at all: run `1064759-0f23fb68` iter 6 swept
# at 79 and fired, and the cadence trigger reported 150 minutes of silence over
# a loop that had measured on time. The binaries line is printed once per
# sweep, before the fire, and carries the per-binary vector.
PATTERN='iter [0-9]+: cargo test|iter [0-9]+ binaries:|GPU_GUARD_STOP|MODULE_PLATEAU_ABORT|VERDICT MISMATCH|TIMED OUT|Traceback|high-water re-based|NOT re-admitting|CRASH layout retrigger fired|reverted [0-9]+ referencing fn|re-fill window open|batch ratchet — breach|cluster complete|task2a checkpoint|final sweep'

# Where the log already is: only lines appended AFTER arming are events.
seen=$(wc -l < "$LOG" 2>/dev/null || echo 0)

stale_step=0      # which staleness threshold has already fired
dead_fired=0

last_sweep_age_min() {
    python3 - "$EVENTS" <<'PY' 2>/dev/null
import sys, json, time, io, os
path = sys.argv[1]
try:
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        fh.seek(max(0, size - 4_000_000))
        blob = fh.read().decode("utf-8", "replace")
except OSError:
    sys.exit(1)
ts = None
for line in blob.splitlines():
    if '"phase_c.tdd.iter"' not in line:
        continue
    try:
        ts = json.loads(line).get("ts") or ts
    except ValueError:
        continue
if not ts:
    sys.exit(1)
# 2026-08-15T16:54:21.228+00:00
import datetime
try:
    when = datetime.datetime.fromisoformat(ts)
except ValueError:
    sys.exit(1)
now = datetime.datetime.now(datetime.timezone.utc)
print(int((now - when).total_seconds() // 60))
PY
}

printf 'INFO armed — floor=%s stale=%smin poll=%ss; log at %s lines\n' \
    "$FLOOR" "$STALE_MIN" "$POLL" "$seen"

while true; do
    # --- 1. new log lines worth reading ---------------------------------
    now_lines=$(wc -l < "$LOG" 2>/dev/null || echo "$seen")
    if [ "$now_lines" -lt "$seen" ]; then
        # log rotated under us; re-baseline rather than replay the new file
        printf 'INFO log rotated (%s -> %s lines) — re-baselining\n' "$seen" "$now_lines"
        seen="$now_lines"
    elif [ "$now_lines" -gt "$seen" ]; then
        while IFS= read -r line; do
            [ -n "$line" ] || continue
            case "$line" in
                *"iter "*": cargo test"*)
                    p=$(printf '%s' "$line" | sed -n 's/.*pass=\([0-9]\+\).*/\1/p')
                    stale_step=0
                    if [ -z "$p" ]; then
                        printf 'INFO sweep line carried no pass= —%s\n' "${line#*\]}"
                    elif [ "$p" -gt "$best" ]; then
                        printf 'SWEEP-UP pass=%s (best was %s, +%s) —%s\n' \
                            "$p" "$best" "$((p - best))" "${line#*\]}"
                        best="$p"
                    elif [ "$p" -eq "$best" ]; then
                        printf 'SWEEP-FLAT pass=%s (equals best) —%s\n' \
                            "$p" "${line#*\]}"
                    else
                        printf 'SWEEP-DOWN pass=%s (best %s, -%s) —%s\n' \
                            "$p" "$best" "$((best - p))" "${line#*\]}"
                    fi
                    ;;
                *" binaries:"*)
                    # A sweep happened even if its summary is about to be
                    # swallowed by a fire. Clears the cadence counter so the
                    # stall trigger measures silence, not swallowed lines.
                    stale_step=0
                    printf 'INFO sweep taken (vector) —%s\n' "${line#*\]}"
                    ;;
                *"reverted "*" referencing fn"*|*"CRASH layout retrigger fired"*)
                    printf 'TRIGGER retrigger-restub —%s\n' "${line#*\]}"
                    ;;
                *MODULE_PLATEAU_ABORT*|*"VERDICT MISMATCH"*|*"TIMED OUT"*|*Traceback*|*GPU_GUARD_STOP*)
                    printf 'TRIGGER terminal —%s\n' "${line#*\]}"
                    ;;
                *)
                    printf 'INFO%s\n' "${line#*\]}"
                    ;;
            esac
        done < <(tail -n +"$((seen + 1))" "$LOG" 2>/dev/null | grep -aE "$PATTERN")
        seen="$now_lines"
    fi

    # --- 2. cadence: alive but not measuring ----------------------------
    age=$(last_sweep_age_min || true)
    if [ -n "${age:-}" ]; then
        step=$(( age / STALE_MIN ))
        if [ "$step" -gt "$stale_step" ]; then
            stale_step="$step"
            printf 'TRIGGER stale=%smin — no scored sweep since then; the run is filling but not measuring\n' "$age"
        fi
    fi

    # --- 3. the process itself ------------------------------------------
    if pgrep -f "pipeline.py --project" >/dev/null 2>&1; then
        dead_fired=0
    elif [ "$dead_fired" -eq 0 ]; then
        dead_fired=1
        printf 'TRIGGER process-gone — no pipeline.py --project process; check the log tail and logs/deaths.jsonl\n'
    fi

    sleep "$POLL"
done
