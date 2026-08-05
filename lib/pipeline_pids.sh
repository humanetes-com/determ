#!/usr/bin/env bash
# Shared pipeline-process detection. Matches ONLY a real interpreter process
# whose argv STARTS with a python binary running pipeline.py — never a
# monitoring shell, pgrep, watcher, or tail whose argv merely CONTAINS the
# string "pipeline.py" (the guard-collision trap: a loose `pgrep -f
# pipeline.py` once matched a background `until pgrep ...pipeline.py...`
# waiter, so graceful_stop waited forever on a process that never exits and
# continue.sh's launch flock was never released).
_PIPELINE_PGREP='^[^ ]*python[0-9.]* [^ ]*pipeline\.py'

pipeline_pids() {
    pgrep -f "$_PIPELINE_PGREP" 2>/dev/null || true
}

pipeline_running() {
    pgrep -f "$_PIPELINE_PGREP" > /dev/null 2>&1
}

kill_pipeline() {
    # SIGTERM then SIGKILL, only on real pipeline interpreter processes.
    local _pids
    _pids="$(pipeline_pids | tr '\n' ' ')"
    [[ -z "$_pids" ]] && return 0
    # Mark the stop as the operator's before signalling, so the death recorder
    # can tell it from a crash. Its own sentinel, NOT the graceful-stop one:
    # that file also tells a LIVE pipeline to exit at its next checkpoint, and
    # this path is about to signal the process rather than ask it. Removed
    # afterwards so it cannot outlive the stop and label the next death.
    touch /tmp/pipeline_operator_stop 2>/dev/null || true
    kill $_pids 2>/dev/null || true
    sleep 2
    pkill -9 -f "$_PIPELINE_PGREP" 2>/dev/null || true
    sleep 1
    rm -f /tmp/pipeline_operator_stop 2>/dev/null || true
}
