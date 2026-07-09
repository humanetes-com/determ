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
    kill $_pids 2>/dev/null || true
    sleep 2
    pkill -9 -f "$_PIPELINE_PGREP" 2>/dev/null || true
    sleep 1
}
