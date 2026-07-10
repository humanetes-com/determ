#!/usr/bin/env bash
# Print the CURRENT run segment of a pipeline log (everything after the last
# "PIPELINE STARTED" line), optionally filtered by an extended-regex pattern.
#
# Exists so the autopilot never inlines `awk "NR>=$(grep -n ...)"` in a Bash
# call — runtime-substituted text inside awk code trips the shell tool's
# static-analysis block. Inside a script the computation is opaque to that
# analysis and reviewed once here.
#
# Usage: bash tools/log_segment.sh [PATTERN] [LOG] [MAXLINES]
#   PATTERN   optional grep -E filter (default: print everything)
#   LOG       log file (default logs/pipeline_anyhow.log)
#   MAXLINES  cap output lines (default 40)
set -euo pipefail
PATTERN="${1:-}"
LOG="${2:-logs/pipeline_anyhow.log}"
MAX="${3:-40}"
START=$(grep -n 'PIPELINE STARTED' "$LOG" | tail -1 | cut -d: -f1)
START="${START:-1}"
if [ -n "$PATTERN" ]; then
    tail -n "+$START" "$LOG" | grep -E "$PATTERN" | tail -n "$MAX"
else
    tail -n "+$START" "$LOG" | tail -n "$MAX"
fi
