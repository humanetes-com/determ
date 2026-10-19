#!/usr/bin/env bash
# Rewrite a pipeline workspace repo's history without cargo build output.
#
# WHY THIS EXISTS
#   Until 2026-10-19 the workspace commits (`adapters/workspace.py`
#   git_commit / git_commit_paths) staged every cluster crate root's
#   `target/` (`.skeleton-filled/<c>/target`, `.skeleton/<c>/target`,
#   `.best_tree/<c>/target`): the workspace's `.gitignore` is the crate's own
#   `/target/`, anchored at the root. After run `1680589-7a6824f8`
#   `tmp/anyhow` tracked 26,329 build files and held a 31 GiB pack. The
#   pipeline now writes `target/` into the repo's `info/exclude` and untracks
#   it on the next commit; that stops the growth but leaves every old commit
#   carrying the binaries. This script reclaims the space.
#
#   Past runs' checkpoints in that repo are mostly reachable ONLY from the
#   reflog (a restart resets the branch to a pre-fill tag), and the trackers
#   cite them by commit id. A plain rewrite + gc would delete them. So every
#   reflog-only commit tip first gets a ref under refs/archive/reflog/, is
#   rewritten with the rest, and stays reachable; and every rewritten commit
#   is written to a map file, old id -> new id.
#
# USAGE
#   bash scripts/purge_workspace_build_output.sh <workspace-dir>
#
#   Preconditions: no pipeline process using that workspace; a clean working
#   tree (`git status --porcelain` empty); a backup of `<workspace>/.git`
#   taken by the caller (this script does not take one: it is the size of
#   the repo). Rewrites every ref (branches, tags, remotes, the archive refs)
#   with `git filter-branch --index-filter`, then drops refs/original,
#   expires the reflog and `gc --prune=now`. Irreversible without the backup.
#   The cargo build cache under the cluster roots is deleted from the working
#   tree by the final checkout (the rewritten HEAD no longer tracks it);
#   the next cargo run rebuilds it.
#
# HOW TO INTERPRET THE OUTPUT
#   archive refs: N          reflog-only tips given a ref before the rewrite
#   tracked build paths before/after   ls-files of the build-output patterns
#                                      at HEAD; after must be 0
#   commits still carrying build output: must be 0 (checked over --all)
#   map: <file> (K lines)     old-id new-id per commit whose content changed
#                             (an unchanged commit keeps its id and is not
#                             listed); translate a cited id with
#                             `grep ^<old> <file>`
#   annotated tags restored   tags on an unchanged commit given back their
#                             original (signed) object
#   size before/after         `git count-objects -vH` size-pack
#   A non-zero "after" or "still carrying" means the patterns missed a path:
#   restore from the backup and fix BUILD_OUTPUT_EXCLUDES first.
set -euo pipefail

ws="${1:?usage: purge_workspace_build_output.sh <workspace-dir>}"
cd "$ws"
[ -z "$(git status --porcelain)" ] || { echo "refusing: working tree not clean" >&2; exit 2; }

# Same patterns as pipeline_core/domain/workspace_layout.BUILD_OUTPUT_EXCLUDES.
PATTERNS=(target/ 'target-*/')
xargs_x=()
for p in "${PATTERNS[@]}"; do xargs_x+=(-x "$p"); done

# Beside the workspace, never inside it: the next `git add -A` would commit it.
map="$(realpath -m "$PWD/..")/$(basename "$PWD")-commit-map-$(date +%Y%m%d).txt"
: > "$map"

echo "size before: $(git count-objects -vH | sed -n 's/^size-pack: //p')"
echo "tracked build paths before: $(git ls-files -c -i "${xargs_x[@]}" | wc -l)"

# Reflog-only tips -> refs/archive/reflog/<old-id>.
n=0
reachable="$(mktemp)"; git rev-list --all > "$reachable"
for sha in $(git reflog --all --format=%H | sort -u | grep -vxFf "$reachable"); do
  git update-ref "refs/archive/reflog/$sha" "$sha"
  n=$((n + 1))
done
rm -f "$reachable"
echo "archive refs: $n"

# The exclude rule, so the rewritten checkout leaves the cache ignored.
excl="$(git rev-parse --git-path info/exclude)"
mkdir -p "$(dirname "$excl")"; touch "$excl"
for p in "${PATTERNS[@]}"; do grep -qxF "$p" "$excl" || echo "$p" >> "$excl"; done

export FILTER_BRANCH_SQUELCH_WARNING=1
# xargs -r: a commit with no build output (every upstream one) removes nothing;
# `git rm` given an empty pathspec list exits non-zero and aborts the rewrite.
idx_filter="git ls-files -z -c -i $(printf '%q ' "${xargs_x[@]}")| xargs -0 -r git rm -r -q --cached --ignore-unmatch --"
# A commit whose tree and parents come out unchanged keeps its own id:
# commit-tree would drop its signature and mint a new id for identical content
# (the signed upstream commits and their release tags).
cf="$(mktemp)"
cat > "$cf" <<'CF'
tree=$1; shift
ps=(); while [ $# -gt 0 ]; do ps+=("$2"); shift 2; done
ot=$(git rev-parse "$GIT_COMMIT^{tree}")
read -r -a ops <<< "$(git rev-list --parents -n1 "$GIT_COMMIT" | cut -s -d' ' -f2-)"
if [ "$tree" = "$ot" ] && [ "${ps[*]}" = "${ops[*]}" ]; then
  cat > /dev/null; new=$GIT_COMMIT
else
  args=(); for p in "${ps[@]}"; do args+=(-p "$p"); done
  new=$(git commit-tree "$tree" "${args[@]}")
  echo "$GIT_COMMIT $new" >> "$PURGE_MAP"
fi
echo "$new"
CF
export PURGE_MAP="$map"
# Tag objects as they are, so a tag whose commit keeps its id gets its own
# object back: filter-branch's tag filter re-creates annotated tags without
# their signature even when the target is unchanged (105 signed upstream
# release tags of tmp/anyhow, 2026-10-19).
tags_before="$(mktemp)"
git for-each-ref --format='%(refname) %(objectname) %(*objectname)' refs/tags > "$tags_before"
commit_filter="bash '$cf' \"\$@\""
git filter-branch -f --index-filter "$idx_filter" --commit-filter "$commit_filter" \
  --tag-name-filter cat -- --all

carrying=0
# Not --all: refs/original still holds the pre-rewrite tips until the prune.
for c in $(git rev-list --branches --tags --remotes --glob=refs/archive/*); do
  if git ls-tree -r --name-only "$c" | grep -qE '(^|/)target(-[^/]*)?/'; then carrying=$((carrying + 1)); fi
done
echo "commits still carrying build output: $carrying"
echo "tracked build paths after: $(git ls-files -c -i "${xargs_x[@]}" | wc -l)"
echo "map: $map ($(wc -l < "$map") lines)"
[ "$carrying" -eq 0 ] || { echo "NOT pruning: build output survived the rewrite" >&2; exit 3; }

restored=0
while read -r ref obj peeled; do
  [ -n "$peeled" ] || continue
  [ "$(git rev-parse "$ref^{commit}" 2>/dev/null)" = "$peeled" ] || continue
  [ "$(git rev-parse "$ref")" = "$obj" ] && continue
  git update-ref "$ref" "$obj"; restored=$((restored + 1))
done < "$tags_before"
echo "annotated tags restored to their own object: $restored"

git for-each-ref --format='%(refname)' refs/original | xargs -r -n1 git update-ref -d
git reflog expire --expire=now --all
git gc --prune=now
echo "size after: $(git count-objects -vH | sed -n 's/^size-pack: //p')"
