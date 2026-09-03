#!/usr/bin/env bash
# WHY: two independent fixes need comparable baselines before a full rebuild.
#   (1) Behaviour capture→oracle (task0d emit, entry 236): commits from
#       4103fb6 through 580fb7e — captures were prompt text only until
#       emit_behaviour_tests.py; harvest reading was deferred until t2a finished.
#   (2) Session site join (entry 238 / P18): attributed_units_for_session —
#       eligible filters site_frontier instead of being a unit source; uncommitted
#       at session time on top of 07ac840.
#   This script freezes the filled reconstruction pair (.skeleton +
#   .skeleton-filled + .best_tree + .phase_c_logs) and tags tmp/anyhow so a
#   task0c restart can be compared against the pre-rebuild trees.
#
# USAGE: stop the pipeline first (touch /tmp/pipeline_graceful_stop, wait), then:
#   bash scripts/tag_pre_rebuild_baselines.sh
#   Env:
#     SNAPSHOT_NAME   compare_models seed (default t2a-pre-rebuild-2026-09-03)
#     WORKSPACE_TAG   git tag in tmp/anyhow (default t2a-pre-rebuild-2026-09-03)
#     DETERM_TAG      annotated tag on the determ repo (default fix-session-join-238)
#   Preconditions: pipeline NOT running; tmp/anyhow has .skeleton-filled.
#   Costs no GPU.
#
# HOW TO INTERPRET THE OUTPUT:
#   SNAPSHOT — seed under .compare-seeds/<name>/ (tree.tar + seed.txt).
#   TAG workspace — annotated tag on tmp/anyhow after commit of dirty tree.
#   TAG determ — annotated tag on pipeline HEAD (notes uncommitted 238 diff if any).
#   After this, rebuild with: bash pre_run.sh && bash restart_from.sh task0c --force
#   Compare: bash compare_models.sh restore <SNAPSHOT_NAME>  (arm A) vs the new run.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

SNAPSHOT_NAME="${SNAPSHOT_NAME:-t2a-pre-rebuild-2026-09-03}"
WORKSPACE_TAG="${WORKSPACE_TAG:-t2a-pre-rebuild-2026-09-03}"
DETERM_TAG="${DETERM_TAG:-fix-session-join-238}"
WS="${WORKSPACE_DIR:-$ROOT/tmp/anyhow}"

if pgrep -f '[p]ipeline\.py --project' >/dev/null 2>&1; then
  echo "[error] pipeline is running — stop it before snapshot (torn tree)." >&2
  echo "  touch /tmp/pipeline_graceful_stop && wait for exit" >&2
  exit 1
fi

CODE_ID="$(git rev-parse --short HEAD)"
STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

printf 'SNAPSHOT %s — compare_models.sh snapshot %s\n' "$STAMP" "$SNAPSHOT_NAME"
bash "$ROOT/compare_models.sh" snapshot "$SNAPSHOT_NAME"

if [[ -d "$WS/.git" ]]; then
  MSG="baseline: t2a-filled trees before task0c rebuild ($(date -u +%Y-%m-%d))

Nightly+stable task2a complete on this tree; behaviour harvest is the 2026-08-27
suite (identity probes only — task0d skipped on continue.sh resume). Pipeline
code at determ ${CODE_ID}; session-join fix (238/P18) may still be uncommitted.

Compare seed: ${SNAPSHOT_NAME}
Restore: bash compare_models.sh restore ${SNAPSHOT_NAME}

Rebuild command (behaviour re-harvest + full fill):
  bash pre_run.sh && bash restart_from.sh task0c --force"

  printf 'TAG %s — committing tmp/anyhow and tagging %s\n' "$STAMP" "$WORKSPACE_TAG"
  git -C "$WS" add -A
  if git -C "$WS" diff --cached --quiet; then
    printf 'INFO workspace clean after add; tagging HEAD\n'
  else
    git -C "$WS" commit -m "$MSG"
  fi
  git -C "$WS" tag -f "$WORKSPACE_TAG" -m "$MSG"
  printf 'INFO workspace tag %s -> %s\n' "$WORKSPACE_TAG" "$(git -C "$WS" rev-parse --short "$WORKSPACE_TAG")"
fi

UNCOMMITTED=""
if ! git diff --quiet || [[ -n "$(git ls-files --others --exclude-standard)" ]]; then
  UNCOMMITTED=" (238/P18 session-join diff still uncommitted — commit before relying on code_id)"
fi

DETERM_MSG="pipeline baseline before task0c behaviour+session rebuild

determ HEAD: ${CODE_ID}${UNCOMMITTED}
anyhow workspace tag: ${WORKSPACE_TAG}
compare seed: ${SNAPSHOT_NAME}

Behaviour fix (236): emit_behaviour_tests env-twin + Debug probe ranking (580fb7e).
Session fix (238): attributed_units_for_session join + P18 census.
Harvest reading OPEN until restart_from.sh task0c."

git tag -f "$DETERM_TAG" -m "$DETERM_MSG"
printf 'TAG determ %s -> %s\n' "$DETERM_TAG" "$(git rev-parse --short "$DETERM_TAG")"

printf 'DONE %s — seed %s, workspace tag %s, determ tag %s\n' \
  "$STAMP" "$SNAPSHOT_NAME" "$WORKSPACE_TAG" "$DETERM_TAG"
printf 'NEXT: commit 238 batch if needed, bash pre_run.sh, bash restart_from.sh task0c --force\n'
