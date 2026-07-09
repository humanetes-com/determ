# Graceful stop helper — sourced by continue.sh and restart.sh.
#
# Function:
#   graceful_stop_pipeline [interactive]
#     interactive=1 (default): during the wait, accept 'c' (cancel) or
#     'f' (force-kill) on stdin.
#     interactive=0: just wait until pipeline exits.
#
# Exit codes:
#   0  pipeline stopped (or none was running)
#   1  user cancelled — pipeline still running, caller should abort

_SENTINEL=/tmp/pipeline_graceful_stop

# shellcheck source=lib/pipeline_pids.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/pipeline_pids.sh"

graceful_stop_pipeline() {
    local interactive="${1:-1}"

    if ! pipeline_running; then
        return 0
    fi

    local _pid
    _pid="$(pipeline_pids | head -1)"
    touch "$_SENTINEL"
    echo "Graceful stop requested (PID $_pid). Pipeline will exit at the next"
    echo "checkpoint (between fn fills, ~30-60s)."
    if [[ "$interactive" == "1" ]]; then
        echo "  Press [c]+enter to CANCEL the stop (keeps pipeline running)."
        echo "  Press [f]+enter to FORCE-KILL the pipeline now."
        echo "  Or wait — pipeline will exit on its own."
    fi

    while pipeline_running; do
        if [[ "$interactive" == "1" ]] && [[ -t 0 ]]; then
            # 2-second read so we re-check the pipeline status frequently.
            local _key=""
            if read -r -t 2 _key; then
                case "${_key,,}" in
                    c|cancel)
                        rm -f "$_SENTINEL"
                        echo "Stop cancelled. Pipeline left running (PID $_pid)."
                        return 1
                        ;;
                    f|force)
                        echo "Force-killing pipeline (PID $_pid)..."
                        rm -f "$_SENTINEL"
                        kill "$_pid" 2>/dev/null || true
                        sleep 2
                        pkill -9 -f "$_PIPELINE_PGREP" 2>/dev/null || true
                        sleep 1
                        return 0
                        ;;
                    *)
                        echo "  (unrecognised — press c, f, or wait)"
                        ;;
                esac
            fi
        else
            sleep 3
        fi
    done

    rm -f "$_SENTINEL"
    echo "Pipeline stopped gracefully."
    return 0
}
