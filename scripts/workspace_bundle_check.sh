#!/usr/bin/env bash
# Is the workspace repo ready to be bundled as the restore point for a tree that
# is about to be retired — and what would a bundle NOT carry?
#
# WHY THIS EXISTS
#   A cold run that re-authors the layout retires the current tree as a live
#   instance; the plan's restore is `git bundle create … --all` of tmp/<project>.
#   A bundle carries COMMITS, and the workspace is permanently dirty: on
#   2026-08-26, before the planned cold run, `tmp/anyhow` had six MODIFIED tracked
#   files uncommitted (.best_tree/stable/{_meta.json,context.rs,error.rs,lib.rs},
#   .skeleton/_tdd_floor_stable.json, pipeline_state.md — 95+/104−) while the
#   plan's only check was "`git status --untracked-files=all` is empty", which
#   passes on modified tracked files and would have bundled a floor and a best
#   tree older than the ones on disk. The retro store (`.phase_c_logs/`) and
#   `pipeline_knowledge.sqlite` are tracked and ride along only when committed;
#   the body ledger is OUTSIDE the workspace (`logs/body_ledger/<project>/`) and
#   is never in the bundle — and survives every wipe, which is its own hazard
#   (`scripts/ledger_reseed_exposure.py`).
#
# USAGE
#   bash scripts/workspace_bundle_check.sh [PROJECT_DIR=tmp/anyhow] [--bundle PATH]
#     Read-only by default: prints the verdict and never commits (commits are
#     user-owned). With --bundle PATH it runs `git bundle create PATH --all` and
#     `git bundle verify` — a non-destructive write of one file outside the repo.
#
# HOW TO INTERPRET THE OUTPUT
#   HEAD / last checkpoint      the newest commit the bundle would carry, and the
#                               pipeline's last own checkpoint (`pipeline: …`).
#   restore tags                task0c-augmented-* (post-t0c restore, exists),
#                               task0a-clone-* (pre-t0c restore — absent until
#                               the tag writer ships), passing-* (locks).
#   modified tracked (N)        files a bundle would carry at their COMMITTED
#                               content, not the on-disk one. N>0 → COMMIT FIRST.
#   untracked / ignored         what no commit holds at all (ignored = target/,
#                               Cargo.lock by the workspace .gitignore).
#   tracked stores              whether the retro store, the knowledge sqlite,
#                               the floor and the best tree are tracked (and
#                               therefore bundle-able once committed).
#   verdict                     READY (working tree == HEAD) or COMMIT FIRST.
set -u
PROJECT_DIR="tmp/anyhow"; BUNDLE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bundle) BUNDLE="$2"; shift 2 ;;
    *) PROJECT_DIR="$1"; shift ;;
  esac
done
[[ -d "$PROJECT_DIR/.git" ]] || { echo "not a git repo: $PROJECT_DIR" >&2; exit 2; }
g() { git -C "$PROJECT_DIR" "$@"; }
echo "workspace: $PROJECT_DIR"
echo "HEAD: $(g log -1 --format='%h %s')"
echo "last checkpoint: $(g log --format='%h %s' --grep='^pipeline:' -1)"
echo "restore tags: augmented=[$(g tag -l 'task0c-augmented-*' | tr '\n' ' ')] clone=[$(g tag -l 'task0a-clone-*' | tr '\n' ' ')] locks=[$(g tag -l 'passing-*' | tr '\n' ' ')]"
mod=$(g status --short --untracked-files=no | grep -c '^ *[MADRC]' || true)
unt=$(g status --short --untracked-files=all | grep -c '^??' || true)
ign=$(g status --short --ignored | grep -c '^!!' || true)
echo "modified tracked ($mod):"; g status --short --untracked-files=no | sed 's/^/  /'
[[ "$mod" -gt 0 ]] && { echo "  diffstat:"; g diff --stat | tail -1 | sed 's/^/  /'; }
echo "untracked ($unt):"; g status --short --untracked-files=all | grep '^??' | sed 's/^/  /' || true
echo "ignored ($ign):"; g status --short --ignored | grep '^!!' | sed 's/^/  /' || true
echo "tracked stores:"
for f in .phase_c_logs/retrospectives.json pipeline_knowledge.sqlite; do
  if g ls-files --error-unmatch "$f" >/dev/null 2>&1; then echo "  tracked   $f"; else echo "  UNTRACKED $f"; fi
done
for d in .skeleton .skeleton-filled .best_tree; do
  n=$(g ls-files "$d" | wc -l); echo "  $d: $n tracked file(s)"
done
echo "body ledger (outside the workspace, never bundled): $(ls -1 logs/body_ledger/*/index.jsonl 2>/dev/null | tr '\n' ' ')"
if [[ "$mod" -gt 0 ]]; then
  echo "verdict: COMMIT FIRST — $mod modified tracked file(s) would be bundled at HEAD's content, not the on-disk content"
  rc=1
else
  echo "verdict: READY — working tree matches HEAD; a bundle carries the on-disk tree"
  rc=0
fi
if [[ -n "$BUNDLE" ]]; then
  g bundle create "$BUNDLE" --all && g bundle verify "$BUNDLE" && echo "bundle written: $BUNDLE ($(du -h "$BUNDLE" | cut -f1))"
fi
exit $rc
