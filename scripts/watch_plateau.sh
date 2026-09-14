#!/usr/bin/env bash
# WHY: the plateau-loop skill must wake IN-SESSION after three consecutive
#   flat judged sweeps (stall>=3 on the `[phase-c/tdd] iter N: cargo test`
#   line) WHILE the pipeline is still running, so the session can read the
#   fill logs before GPU is spent on a fourth flat fill. TDD_MAX_STALLS=0
#   on purpose: a stop knob would end the run before that reading. The
#   existing watch_pipeline.sh prints every iteration; this script is silent
#   until the trip, then exits, so one arm is one wake.
#   2026-09-14: a leftover graceful-stop sentinel killed a 12s relaunch
#   (520338-26bbb7bb). This watcher never touches that file.
#
# USAGE: bash scripts/watch_plateau.sh
#   Arm from the session with the monitor tool (persistent) or as a
#   background command. No daemon mode; unknown flags exit 64.
#   Env: LOG (default logs/pipeline_anyhow.log), POLL (default 5),
#   STARTUP_GRACE (default 120), STALL_TRIP (default 3), DEADLINE (default
#   0 = no wall-clock kill; a positive value is the predicate-bug bound).
#   --check-line LINE  parse one summary line and print TRIP or WAIT, then
#   exit (for tests; does not touch the live log).
#
# HOW TO INTERPRET THE OUTPUT: silent until a terminal event. Then exactly
#   one line, then exit.
#   DONE: THIS IS PART OF PLATEAU LOOP: stall=N iter=I — load CLAUDE.md and
#     focus.md  — stall on the latest judged sweep is >= STALL_TRIP. The
#     pipeline is still alive. Analyze, then stop only if the logs show a
#     defect.
#   FAILED: THIS IS PART OF PLATEAU LOOP: pipeline gone before stall trip
#     — process disappeared without stall>=STALL_TRIP. Classify the stop;
#     do not treat it as a plateau.
#   FAILED: THIS IS PART OF PLATEAU LOOP: no pipeline appeared within Ns
#     — launch did not come up.
#   Exit 0 = stall trip. Exit 1 = pipeline gone / launch failed. Exit 4 =
#   deadline with pipeline still alive and stall still below the trip
#   (re-arm). Exit 64 = unknown argument.
set -u

POLL="${POLL:-5}"
STARTUP_GRACE="${STARTUP_GRACE:-120}"
STALL_TRIP="${STALL_TRIP:-3}"
DEADLINE="${DEADLINE:-0}"
LOG="${LOG:-logs/pipeline_anyhow.log}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT" || exit 1
# shellcheck source=lib/pipeline_pids.sh
source "$ROOT/lib/pipeline_pids.sh"

parse_stall() {
    printf '%s' "$1" | sed -n 's/.*stall=\([0-9][0-9]*\)\/.*/\1/p'
}

parse_iter() {
    printf '%s' "$1" | sed -n 's/.*\[phase-c\/tdd\] iter \([0-9][0-9]*\): cargo test.*/\1/p'
}

is_summary() {
    printf '%s' "$1" | grep -q '\[phase-c/tdd\] iter [0-9][0-9]*: cargo test'
}

check_line() {
    local line="$1" stall iter
    if ! is_summary "$line"; then
        echo "WAIT"
        return 1
    fi
    stall="$(parse_stall "$line")"
    iter="$(parse_iter "$line")"
    if [ -n "$stall" ] && [ "$stall" -ge "$STALL_TRIP" ]; then
        echo "TRIP stall=${stall} iter=${iter:-?}"
        return 0
    fi
    echo "WAIT stall=${stall:-?} iter=${iter:-?}"
    return 1
}

emit_trip() {
    local line="$1" stall iter
    stall="$(parse_stall "$line")"
    iter="$(parse_iter "$line")"
    echo "DONE: THIS IS PART OF PLATEAU LOOP: stall=${stall} iter=${iter:-?} — load CLAUDE.md and focus.md"
}

last_summary_this_run() {
    [ -f "$LOG" ] || return 1
    awk '
        /PIPELINE STARTED/ { buf = "" }
        /\[phase-c\/tdd\] iter [0-9]+: cargo test/ { buf = $0 }
        END { if (buf != "") print buf }
    ' "$LOG"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --check-line)
            if [ -z "${2:-}" ]; then
                echo "usage: $0 [--check-line LINE]" >&2
                exit 64
            fi
            check_line "$2"
            exit $?
            ;;
        --poll)
            if [ -z "${2:-}" ]; then
                echo "usage: $0 [--poll SECONDS]" >&2
                exit 64
            fi
            POLL="$2"; shift 2 ;;
        *)
            echo "usage: $0 [--poll SECONDS] [--check-line LINE]" >&2
            exit 64 ;;
    esac
done

find_pid() {
    pipeline_pids | head -1
}

waited=0
pid="$(find_pid)"
while [ -z "$pid" ] && [ "$waited" -lt "$STARTUP_GRACE" ]; do
    sleep "$POLL"; waited=$((waited + POLL)); pid="$(find_pid)"
done
if [ -z "$pid" ]; then
    echo "FAILED: THIS IS PART OF PLATEAU LOOP: no pipeline appeared within ${STARTUP_GRACE}s — the launch failed"
    exit 1
fi

existing="$(last_summary_this_run || true)"
if [ -n "$existing" ] && check_line "$existing" >/dev/null; then
    emit_trip "$existing"
    exit 0
fi

start=$(date +%s)
# Follow only new lines; the initial scan above already consumed the current run's last summary.
# Process substitution keeps this loop in the main shell so a trip exit is the script's exit.
exec 3< <(tail -F -n 0 "$LOG" 2>/dev/null)
while true; do
    if [ "$DEADLINE" -gt 0 ]; then
        now=$(date +%s)
        if [ $((now - start)) -ge "$DEADLINE" ]; then
            echo "FAILED: THIS IS PART OF PLATEAU LOOP: watcher deadline expired after ${DEADLINE}s — pipeline still alive, stall below ${STALL_TRIP}. Re-arm."
            exit 4
        fi
    fi
    if IFS= read -r -t "$POLL" line <&3; then
        if is_summary "$line" && check_line "$line" >/dev/null; then
            emit_trip "$line"
            exit 0
        fi
        continue
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
        sleep 2
        if [ -z "$(find_pid)" ]; then
            echo "FAILED: THIS IS PART OF PLATEAU LOOP: pipeline gone before stall trip — classify the stop"
            exit 1
        fi
        pid="$(find_pid)"
    fi
done
