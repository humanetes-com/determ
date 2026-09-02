#!/usr/bin/env bash
# WHY: the behaviour-fix reading needs a harvest from task0c, but the operator
#   wants the pre-fix t2a filled trees preserved for comparison first. This
#   script blocks until the nightly cluster finishes task2a, snapshots the
#   reconstruction pair on disk, tags the anyhow workspace, then runs
#   restart_from.sh task0c --force so the next launch re-enters at harvest.
#
# USAGE: bash scripts/watch_t2a_then_restart_task0c.sh
#   Env:
#     POLL              seconds between polls (default 30)
#     DEADLINE          max watch seconds (default 21600 = 6h)
#     SNAPSHOT_NAME     compare_models seed name (default t2a-pre-behaviour-fix)
#     WORKSPACE_TAG     git tag in tmp/anyhow (default t2a-pre-behaviour-fix)
#     SKIP_RESTART      set 1 to snapshot/tag only (no restart_from)
#   Preconditions: pipeline running on the old code path; nightly cluster still
#   in task2a (pipeline_state current line names nightly). Costs no GPU.
#
# HOW TO INTERPRET THE OUTPUT:
#   WAIT lines — still filling; nightly task2a not done yet.
#   TRIGGER nightly-t2a-done — [task2a/anyhow/nightly] is done in
#     pipeline_state.md; graceful stop sent so task2b does not start.
#   SNAPSHOT — compare_models.sh seed written under .compare-seeds/.
#   TAG — annotated tag on tmp/anyhow after a workspace commit.
#   RESTART — restart_from.sh task0c --force launched (unless SKIP_RESTART=1).
#   Exit 0 = snapshot (+ restart) completed. Exit 4 = DEADLINE without nightly
#   done. Exit 2 = pipeline gone before nightly finished.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

POLL="${POLL:-30}"
DEADLINE="${DEADLINE:-21600}"
SNAPSHOT_NAME="${SNAPSHOT_NAME:-t2a-pre-behaviour-fix}"
WORKSPACE_TAG="${WORKSPACE_TAG:-t2a-pre-behaviour-fix}"
STATE="${WORKSPACE_STATE:-$ROOT/tmp/anyhow/pipeline_state.md}"
WS="${WORKSPACE_DIR:-$ROOT/tmp/anyhow}"

find_pipeline_pid() {
  pgrep -f '[p]ipeline\.py --project' | head -1
}

nightly_t2a_done() {
  [[ -f "$STATE" ]] || return 1
  grep -q '^\[task2a/anyhow/nightly\].*done' "$STATE"
}

start=$(date +%s)
printf 'INFO armed %s — watching %s for [task2a/anyhow/nightly] done; deadline=%ss poll=%ss\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$STATE" "$DEADLINE" "$POLL"

while true; do
  now=$(date +%s)
  elapsed=$((now - start))
  if (( elapsed >= DEADLINE )); then
    printf 'TIMEOUT %s — nightly task2a not done within %ss; pipeline still up=%s\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$DEADLINE" "$(find_pipeline_pid || echo none)"
    exit 4
  fi

  if nightly_t2a_done; then
    line=$(grep '^\[task2a/anyhow/nightly\]' "$STATE" || true)
    printf 'TRIGGER nightly-t2a-done %s — %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$line"
    break
  fi

  pid="$(find_pipeline_pid || true)"
  if [[ -z "$pid" ]]; then
    if nightly_t2a_done; then
      break
    fi
    printf 'ERROR %s — pipeline exited before [task2a/anyhow/nightly] done\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    tail -n 5 "$STATE" 2>/dev/null || true
    exit 2
  fi

  cur=$(grep '^current:' "$STATE" 2>/dev/null || echo 'current: ?')
  printf 'WAIT %s elapsed=%ss pid=%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$elapsed" "$pid" "$cur"
  sleep "$POLL"
done

# Stop before task2b mutates the workspace merge surface.
if find_pipeline_pid >/dev/null 2>&1; then
  printf 'INFO %s — graceful stop (preserve fills, block task2b)\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  touch /tmp/pipeline_graceful_stop
  stop_start=$(date +%s)
  while find_pipeline_pid >/dev/null 2>&1; do
    if (( $(date +%s) - stop_start > 3600 )); then
      printf 'ERROR %s — pipeline still running 1h after graceful stop; snapshot refused\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
      exit 3
    fi
    sleep 5
  done
fi

printf 'INFO %s — pipeline stopped; taking snapshot\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

printf 'SNAPSHOT %s — compare_models.sh snapshot %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$SNAPSHOT_NAME"
bash "$ROOT/compare_models.sh" snapshot "$SNAPSHOT_NAME"

if [[ -d "$WS/.git" ]]; then
  printf 'TAG %s — committing tmp/anyhow and tagging %s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$WORKSPACE_TAG"
  git -C "$WS" add -A
  if git -C "$WS" diff --cached --quiet; then
    printf 'INFO workspace clean after add; tagging HEAD\n'
  else
    git -C "$WS" commit -m "baseline: t2a filled trees before behaviour-fix task0c restart

Nightly cluster task2a complete. Snapshot seed: ${SNAPSHOT_NAME}.
Compare via: bash compare_models.sh restore ${SNAPSHOT_NAME}
Or: git -C tmp/anyhow archive ${WORKSPACE_TAG} .skeleton .skeleton-filled"
  fi
  git -C "$WS" tag -f "$WORKSPACE_TAG" -m "t2a filled baseline before behaviour-fix harvest ($(date -u +%Y-%m-%d))"
  printf 'INFO workspace tag %s -> %s\n' "$WORKSPACE_TAG" "$(git -C "$WS" rev-parse --short "$WORKSPACE_TAG")"
fi

if [[ "${SKIP_RESTART:-0}" == 1 ]]; then
  printf 'DONE %s — snapshot/tag only (SKIP_RESTART=1)\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  exit 0
fi

printf 'GATE %s — pre_run.sh\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
bash "$ROOT/pre_run.sh"

printf 'RESTART %s — restart_from.sh task0c --force\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
bash "$ROOT/restart_from.sh" task0c --force

printf 'DONE %s — snapshot %s, tag %s, task0c restart launched\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$SNAPSHOT_NAME" "$WORKSPACE_TAG"
