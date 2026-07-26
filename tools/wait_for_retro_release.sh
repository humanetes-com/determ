#!/usr/bin/env bash
# Silent wait until the per-block route reaches the exhaustion gate in the
# CURRENT run segment, then report which way the gate went. Prints NOTHING until
# it has a verdict (a chatty poller costs tokens every time it is read back).
#
# The gate lives in the per-block route only, which runs as the FALLBACK after
# file-first spends its attempts — so the verdict is minutes-to-an-hour out, not
# immediate. GOOD = releases > 0 and skips == 0.
#
# Usage: wait_for_retro_release.sh [max_seconds]
set -u
cd "$(dirname "$0")/.." || exit 1
deadline=$(( $(date +%s) + ${1:-3600} ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  skips=$(bash tools/log_segment.sh 'prior retro marked exhausted' | wc -l)
  rel=$(bash tools/log_segment.sh 'not honoured .stale_' | wc -l)
  if [ "$skips" -gt 0 ] || [ "$rel" -gt 0 ]; then
    break
  fi
  sleep 30
done
echo "skipped-before-attempt : $(bash tools/log_segment.sh 'prior retro marked exhausted' | wc -l)  (previous run: 10)"
echo "released (stale_*)     : $(bash tools/log_segment.sh 'not honoured .stale_' | wc -l)"
echo "--- sample releases ---"
bash tools/log_segment.sh 'not honoured .stale_' | head -8
echo "--- fills ---"
bash tools/log_segment.sh 'FILLED' | tail -6
echo "--- sweeps ---"
bash tools/log_segment.sh 'cargo test rc' | tail -3
