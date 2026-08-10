#!/usr/bin/env bash
# Run the pipeline as a CHILD and record how it ended.
#
# Why this exists: over the 48 h to 2026-07-31, 23 % of wall clock (656 min of
# gaps >= 10 min out of 2878) had nothing running, and two of those gaps were
# 198 min and 330 min. The process does not exit — it VANISHES mid-fill, with
# no traceback in run.log and no shutdown line. Every diagnosis so far has been
# archaeology over event gaps, and the one clue previously believed to mark a
# death (`vllm.budget` being the last event) turned out to be the ambient
# periodic event: it precedes 9 of 11 gaps, including gaps where the process
# never died at all.
#
# The reason nothing knows why it died is that nothing was its parent.
# `continue.sh` launched it with `nohup ... &` and then either exited or tailed
# a log, so the exit status — the single fact that separates OOM-kill (137)
# from SIGTERM (143) from a clean exit (0) from a Python traceback (1) — was
# discarded by the shell that could have read it.
#
# This wrapper is that parent. It waits, reads `$?`, and appends one JSON line
# per death to logs/deaths.jsonl. It relaunches NOTHING: the supervisor's
# relaunch half needs a decision about unattended GPU spend, and is deliberately
# not taken here.
#
# The child runs in its OWN SESSION, and that is the whole point of the
# `setsid` below. On 2026-08-05 at 03:05:36 UTC run 525803-9a51be5a and this
# supervisor (pid 525793) vanished in the same instant, 10 h 09 m in, and
# deaths.jsonl has no row for it: the process group went away together, and the
# only component that could have said what happened was inside it. A supervisor
# that dies with its child cannot report the deaths that are not already
# understood — which are exactly the ones worth reporting. Separate sessions
# mean a group-wide signal can reach one or the other but not both.
#
# Deliberate stops are unaffected: `lib/pipeline_pids.sh` kills the resolved
# python interpreter pids, never a process group, so it still reaches the child
# across the session boundary.
#
#   bash lib/record_death.sh <log_dir> <python> <script> [args...]
set -u

LOG_DIR="$1"; shift
DEATHS="$LOG_DIR/deaths.jsonl"
# Write-ahead half of the record. A death row written at exit cannot report a
# death that takes the recorder too — 2026-08-09: run 1159463 (the 18→76
# climb) vanished with its supervisor between 08:41 and the 22:44 relaunch,
# and deaths.jsonl still ends at the PREVIOUS run. KILL is untrappable and the
# TERM/INT/HUP traps below never ran, so the exit-time design has a hole
# exactly the width of "both processes died at once" (a pattern-kill matching
# this script's own argv, a cgroup-level OOM kill, power loss). The journal
# closes it: a birth row lands BEFORE the child starts, and the next launch
# reconciles any birth with no matching death into a synthesized row.
BIRTHS="$LOG_DIR/births.jsonl"
# Overridable so tests can run this script without clobbering the live
# pipeline's pid file (readers resolve real pids via pgrep; this file is the
# convenience copy).
PIDFILE="${DETERM_PIPELINE_PIDFILE:-/tmp/determ_pipeline.pid}"
SENTINEL=/tmp/pipeline_graceful_stop
# The other deliberate stop. `graceful_stop_pipeline` asks the pipeline to exit
# at its next checkpoint and marks it with SENTINEL; `kill_pipeline` — the
# documented way to stop a run so it STAYS stopped — signals instead, and used
# to leave no mark at all. Stopping the 2026-08-05 run that way produced a death
# row correct in every other respect (uptime_s 33230, signal TERM, heartbeat
# removed) carrying "deliberate_stop": false, so a later session mining
# deaths.jsonl for unexplained deaths would count the operator's own stop as one.
OPERATOR_SENTINEL=/tmp/pipeline_operator_stop
PIPELINE_LOG="$LOG_DIR/pipeline_anyhow.log"
HEARTBEAT="$LOG_DIR/supervisor_heartbeat"
HEARTBEAT_EVERY=60

_json_escape() {
    # Minimal JSON string escaping for the fields below (no control chars
    # beyond newline/tab reach here — they come from log text and dmesg).
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' \
                           -e 's/\t/\\t/g' | tr '\n' ' '
}

_started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
_started_s="$(date +%s)"
# Which code was running — the pipeline's own identity signal, so a death can
# be attributed to a version rather than to a date.
_code_id="$(md5sum pipeline.py 2>/dev/null | cut -c1-8)"
# dmesg cursor: only lines that arrive AFTER launch can be about this death.
_dmesg_before="$(dmesg 2>/dev/null | wc -l)"

# Settle births that never reached a death row. Runs BEFORE this launch's
# child starts, so a synthesized row can only describe a PREVIOUS process.
# A birth is settled when a death row carries its started_at + child pid; it
# is an orphan when neither its supervisor nor its child is still running.
# (Pid reuse can hold a row open one launch longer — it is retried next
# time, never dropped.) No exit status exists for an orphan — nothing was
# alive to read it — so ended_at is bounded by the pipeline log's last
# write, and the signal field says exactly what is known: the supervisor
# died WITH the child, which the exit-time recorder cannot report.
_reconcile_births() {
    [[ -s "$BIRTHS" ]] || return 0
    local _keep="" _line _sup _child _born _bcode _ended _born_s _ended_s _up _del
    while IFS= read -r _line; do
        [[ -n "$_line" ]] || continue
        _sup="$(printf '%s' "$_line" | sed -n 's/.*"supervisor_pid":\([0-9]*\).*/\1/p')"
        _child="$(printf '%s' "$_line" | sed -n 's/.*"child_pid":\([0-9]*\).*/\1/p')"
        _born="$(printf '%s' "$_line" | sed -n 's/.*"started_at":"\([^"]*\)".*/\1/p')"
        _bcode="$(printf '%s' "$_line" | sed -n 's/.*"code_id":"\([^"]*\)".*/\1/p')"
        [[ -n "$_child" && -n "$_born" ]] || continue   # unparseable: drop
        if [[ -s "$DEATHS" ]] \
                && grep -q "\"started_at\":\"$_born\".*\"pid\":$_child," "$DEATHS"; then
            continue                                    # settled
        fi
        if kill -0 "$_sup" 2>/dev/null || kill -0 "$_child" 2>/dev/null; then
            _keep+="$_line"$'\n'                        # still running
            continue
        fi
        _ended="$(date -u -r "$PIPELINE_LOG" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
                  || date -u +%Y-%m-%dT%H:%M:%SZ)"
        _born_s="$(date -d "$_born" +%s 2>/dev/null || echo 0)"
        _ended_s="$(date -d "$_ended" +%s 2>/dev/null || echo 0)"
        _up=$(( _ended_s > _born_s ? _ended_s - _born_s : 0 ))
        _del=false
        [[ -e "$SENTINEL" || -e "$OPERATOR_SENTINEL" ]] && _del=true
        {
            printf '{'
            printf '"started_at":"%s",'    "$_born"
            printf '"ended_at":"%s",'      "$_ended"
            printf '"uptime_s":%d,'        "$_up"
            printf '"pid":%d,'             "$_child"
            printf '"exit_status":%d,'     -1
            printf '"signal":"%s",'        "UNKNOWN_KILLED_WITH_SUPERVISOR"
            printf '"deliberate_stop":%s,' "$_del"
            printf '"code_id":"%s",'       "$(_json_escape "$_bcode")"
            printf '"dmesg":"",'
            printf '"log_tail":"%s",'      "$(_json_escape "$(tail -3 "$PIPELINE_LOG" 2>/dev/null)")"
            printf '"synthesized":true'
            printf '}\n'
        } >> "$DEATHS"
        printf '[death-watch] RECONCILED an unrecorded death: pipeline %s (born %s) vanished WITH its supervisor — no exit status exists; ended_at bounded by the log'\''s last write (%s).\n' \
            "$_child" "$_born" "$_ended" >> "$PIPELINE_LOG"
    done < "$BIRTHS"
    printf '%s' "$_keep" > "$BIRTHS"
}
_reconcile_births

# `--wait` matters: plain `setsid` forks and exits immediately, which would
# throw away the exit status this whole wrapper exists to read. With it, setsid
# waits for the child and exits WITH the child's status, so `wait` below still
# sees 137 vs 143 vs 1. Falls back to a direct launch where setsid is absent —
# losing the session split but never the recording.
if command -v setsid > /dev/null 2>&1; then
    setsid --wait "$@" >> "$PIPELINE_LOG" 2>&1 &
    _pid=$!
    # setsid may fork (when it is already a group leader) or exec in place. The
    # pid file must name the PIPELINE either way, because that is what readers
    # signal and poll.
    _child="$(pgrep -P "$_pid" 2>/dev/null | head -1)"
    _pipeline_pid="${_child:-$_pid}"
else
    "$@" >> "$PIPELINE_LOG" 2>&1 &
    _pid=$!
    _pipeline_pid=$_pid
fi
# The write-ahead birth row, BEFORE the pid file — the pid file is the
# signal readers act on, so by the time anything can aim at this run the
# journal already holds it. From here on, this run can never vanish
# unexplained: either the wait below writes its death row, or the next
# launch's reconciliation synthesizes one from this line.
{
    printf '{'
    printf '"started_at":"%s",'   "$_started_at"
    printf '"supervisor_pid":%d,' "$$"
    printf '"child_pid":%d,'      "$_pipeline_pid"
    printf '"code_id":"%s",'      "$(_json_escape "$_code_id")"
    printf '"cmd":"%s"'           "$(_json_escape "$*")"
    printf '}\n'
} >> "$BIRTHS"

# The child's PID, for anyone who wants it without pgrep. `pipeline_pids`
# still resolves the real interpreter, and is what every other caller uses.
echo "$_pipeline_pid" > "$PIDFILE"

# A death nobody recorded is at least BOUNDED in time by this. The 03:05:36
# disappearance was pinned only because the per-worker stream logs happened to
# stop mid-sentence; a heartbeat says "the supervisor was alive at T" without
# depending on that luck.
_heartbeat_loop() {
    while :; do
        printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$_pipeline_pid" \
            > "$HEARTBEAT"
        sleep "$HEARTBEAT_EVERY"
    done
}
_heartbeat_loop &
_hb_pid=$!

# A signal aimed at this supervisor is forwarded to the child and then WAITED
# OUT, so the death still gets a row. Without the trap a stop delivered here
# would end the recorder first and leave the one death it was watching for
# unrecorded.
_forward() {
    kill -"$1" "$_pipeline_pid" 2>/dev/null || true
}
trap '_forward TERM' TERM
trap '_forward INT' INT
trap '_forward HUP' HUP

wait "$_pid"
_status=$?
# `wait` returns >128 when interrupted by a trapped signal rather than by the
# child exiting; keep waiting until the child is really gone.
while kill -0 "$_pipeline_pid" 2>/dev/null; do
    wait "$_pid"
    _status=$?
done
kill "$_hb_pid" 2>/dev/null || true
rm -f "$HEARTBEAT"

_ended_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
_uptime=$(( $(date +%s) - _started_s ))

# 128+N means signal N. The three that matter here: 9 (SIGKILL — the OOM
# killer, or a --force stop), 15 (SIGTERM — a graceful/forced stop, or a
# process-group signal from a `timeout` wrapper), 6 (SIGABRT).
_signal=""
if (( _status > 128 )); then
    _signal="$(kill -l $(( _status - 128 )) 2>/dev/null || echo "SIG$(( _status - 128 ))")"
fi

# A stop we ASKED for is not a death. Recorded either way, labelled, because
# "23 % downtime" means something different if half of it was deliberate.
_deliberate=false
[[ -e "$SENTINEL" ]] && _deliberate=true
[[ -e "$OPERATOR_SENTINEL" ]] && _deliberate=true

# Kernel evidence, restricted to lines that appeared during this run. An OOM
# kill names the process and its PID; nothing else in the log will.
_dmesg_new="$(dmesg 2>/dev/null | tail -n +$(( _dmesg_before + 1 )) \
    | grep -iE 'out of memory|oom-kill|killed process|segfault|general protection' \
    | tail -5)"

_log_tail="$(tail -3 "$PIPELINE_LOG" 2>/dev/null)"

{
    printf '{'
    printf '"started_at":"%s",'   "$_started_at"
    printf '"ended_at":"%s",'     "$_ended_at"
    printf '"uptime_s":%d,'       "$_uptime"
    printf '"pid":%d,'            "$_pipeline_pid"
    printf '"exit_status":%d,'    "$_status"
    printf '"signal":"%s",'       "$(_json_escape "$_signal")"
    printf '"deliberate_stop":%s,' "$_deliberate"
    printf '"code_id":"%s",'      "$(_json_escape "$_code_id")"
    printf '"dmesg":"%s",'        "$(_json_escape "$_dmesg_new")"
    printf '"log_tail":"%s"'      "$(_json_escape "$_log_tail")"
    printf '}\n'
} >> "$DEATHS"

# Also say it in the pipeline log, where a human is already looking.
{
    printf '[death-watch] pipeline %d ended after %dm%02ds — exit=%d%s%s\n' \
        "$_pipeline_pid" $(( _uptime / 60 )) $(( _uptime % 60 )) "$_status" \
        "${_signal:+ signal=$_signal}" \
        "$( [[ "$_deliberate" == true ]] && echo ' (deliberate stop)' )"
    [[ -n "$_dmesg_new" ]] && printf '[death-watch] kernel: %s\n' "$_dmesg_new"
} >> "$PIPELINE_LOG"

exit "$_status"
