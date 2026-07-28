#!/usr/bin/env bash
# Grep the FULL run history (all rotated archives + the active log), every hit
# labelled with the run it belongs to. The cross-run counterpart to
# tools/log_segment.sh, which only ever shows the CURRENT run.
#
# Wrapper only — the logic is tools/log_history.py. It exists so the autopilot
# invokes one short, static-analysis-safe command:
#
#   bash tools/log_history.sh 'floor stalled'
#   bash tools/log_history.sh REVERTED reverted --runs 3
#
# Patterns are plain substrings, OR-ed (repo rule: no regex) — pass several
# rather than writing an alternation.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec .venv/bin/python tools/log_history.py "$@"
