#!/usr/bin/env bash
# WHY: a live fill has to be read at the next decision, not on a timer and
#   not on every log line. The 2026-09-20 session asked to be woken when the
#   parked session keep, the next judged sweep, a compiling-retry prompt, or
#   a death became visible. watch_pipeline.sh prints every iteration;
#   watch_plateau.sh waits for stall>=3. This script is silent until one of
#   those checkpoints appears after the arm offset, then exits so one arm is
#   one wake. It never signals the pipeline and never writes the graceful-stop
#   file.
#
# USAGE: bash scripts/watch_next_checkpoint.sh
#   Arm from the session with the monitor tool. No daemon mode; unknown flags
#   exit 64. Env: LOG (default logs/pipeline_anyhow.log), POLL (default 10),
#   STARTUP_GRACE (default 120), DEADLINE (default 3600 — the predicate-bug
#   bound; a healthy fill outliving it is not an error, re-arm). OFFSET is
#   the last line already read; default is the current line count at start so
#   already-printed lines cannot trip.
#
# HOW TO INTERPRET THE OUTPUT: silent until a terminal event. Then exactly
#   one stdout line, then exit.
#   DONE: checkpoint <kind> — <collapsed log line>
#     A fill-route ended, a judged sweep printed, a compiling-retry prompt
#     was described as rustc or as a run fault, or a stop signature appeared.
#     Crime-scene the log from that line; then re-arm.
#   DONE: deadline <Ns> still running
#     The bound expired with the pipeline still alive. Crime-scene, re-arm.
#   FAILED: pipeline gone — <hint>
#     Process disappeared. Classify the stop; do not wait for a checkpoint
#     that will never come.
#   FAILED: no pipeline appeared within Ns
#     Launch did not come up.
#   Exit 0 = checkpoint or deadline. Exit 1 = pipeline gone / launch failed.
#   Exit 64 = unknown argument.
set -u

POLL="${POLL:-10}"
STARTUP_GRACE="${STARTUP_GRACE:-120}"
DEADLINE="${DEADLINE:-3600}"
LOG="${LOG:-logs/pipeline_anyhow.log}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT" || exit 1
# shellcheck source=lib/pipeline_pids.sh
source "$ROOT/lib/pipeline_pids.sh"

while [ "$#" -gt 0 ]; do
    case "$1" in
        *)
            echo "usage: $0  (unknown argument: $1 — no daemon mode; arm via the monitor tool)" >&2
            exit 64
            ;;
    esac
done

waited=0
while ! pipeline_running && [ "$waited" -lt "$STARTUP_GRACE" ]; do
    sleep "$POLL"
    waited=$((waited + POLL))
done
if ! pipeline_running; then
    echo "FAILED: no pipeline appeared within ${STARTUP_GRACE}s"
    exit 1
fi

if [ -z "${OFFSET:-}" ]; then
    if [ -f "$LOG" ]; then
        OFFSET="$(wc -l < "$LOG" | tr -d ' ')"
    else
        OFFSET=0
    fi
fi

start="$(date +%s)"
seen_alive=1

classify() {
    local line="$1"
    case "$line" in
        *'[phase-c/route]'*) echo route ;;
        *'[phase-c/tdd] iter '*' cargo test'*) echo iter ;;
        *'LAST_ERROR'*|*'Your previous attempt compiled'*|*'broke cargo check'*|*'could not be made to compile'*) echo retry_wording ;;
        *'[death-watch]'*) echo death ;;
        *FILL_PHASE_STOP*|*GPU_GUARD_STOP*|*NO_VERDICT_STOP*|*NO_FRONTIER_STOP*|*MODULE_PLATEAU_ABORT*) echo stop ;;
        *Traceback*|*VERDICT\ MISMATCH*|*TIMED\ OUT*) echo crash ;;
        *'layout retrigger fired'*|*'high-water re-based'*|*'NOT re-admitting'*) echo destruction ;;
        *) echo other ;;
    esac
}

is_checkpoint() {
    printf '%s' "$1" | grep -Eq \
        '\[phase-c/route\]|\[phase-c/tdd\] iter [0-9]+: cargo test|LAST_ERROR|Your previous attempt compiled|broke cargo check|could not be made to compile|\[death-watch\]|FILL_PHASE_STOP|GPU_GUARD_STOP|NO_VERDICT_STOP|NO_FRONTIER_STOP|MODULE_PLATEAU_ABORT|Traceback|VERDICT MISMATCH|TIMED OUT|layout retrigger fired|high-water re-based|NOT re-admitting'
}

collapse() {
    printf '%s' "$1" | tr '\t' ' ' | tr -s ' ' | cut -c1-240
}

while :; do
    now="$(date +%s)"
    elapsed=$((now - start))
    if [ "$DEADLINE" -gt 0 ] && [ "$elapsed" -ge "$DEADLINE" ]; then
        echo "DONE: deadline ${DEADLINE}s still running"
        exit 0
    fi
    if pipeline_running; then
        seen_alive=1
    elif [ "$seen_alive" -eq 1 ]; then
        sleep 5
        if ! pipeline_running; then
            hint="no death-watch line yet"
            if [ -f "$LOG" ]; then
                hint="$(tail -n 1 "$LOG" | tr '\t' ' ' | tr -s ' ' | cut -c1-200)"
            fi
            echo "FAILED: pipeline gone — ${hint}"
            exit 1
        fi
    fi
    if [ -f "$LOG" ]; then
        total="$(wc -l < "$LOG" | tr -d ' ')"
        if [ "$total" -gt "$OFFSET" ]; then
            start_read=$((OFFSET + 1))
            while IFS= read -r line; do
                if is_checkpoint "$line"; then
                    kind="$(classify "$line")"
                    echo "DONE: checkpoint ${kind} — $(collapse "$line")"
                    exit 0
                fi
            done < <(sed -n "${start_read},${total}p" "$LOG")
            OFFSET="$total"
        fi
    fi
    sleep "$POLL"
done
