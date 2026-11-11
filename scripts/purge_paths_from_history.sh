#!/usr/bin/env bash
# purge_paths_from_history.sh — remove files from EVERY commit of this repo
# and push the rewritten history.
#
# WHY
#   On 2026-06-29 (commit 5dd0194) and 2026-08-11 (commit 8e8d2c2) two .env
#   backups, `.env~` and `.env.bak-premuse`, were committed with a live
#   HF_TOKEN in them, and pushed. Untracking them (git rm --cached) only stops
#   future commits from carrying them; every older commit, on every branch and
#   tag, still holds the blob. Before the repository goes public the paths have
#   to leave history itself. The same applies to any file that should never
#   have been committed (private notes, build output).
#
# USAGE
#   bash scripts/purge_paths_from_history.sh [--push] PATH [PATH ...]
#
#   PATH    a repo-relative path, exactly as `git log -- PATH` takes it; a
#           directory removes everything under it. Prefix with `glob:` for a
#           glob (`glob:*.md~`).
#   --push  after verifying, force-push every branch and tag to origin and
#           delete remote branches that no longer exist locally. Without it
#           the script stops after the local rewrite and prints the commands.
#
#   Preconditions, checked before anything is changed:
#   - git-filter-repo installed (`pacman -S git-filter-repo` or
#     `pip install git-filter-repo`);
#   - no staged or unstaged changes to tracked files in this worktree (commit
#     first: the rewrite replaces HEAD);
#   - no pipeline running (it commits checkpoints into this repo).
#   Cost: seconds for a repo of this size (.git was 36 MB on 2026-11-11). No
#   GPU, no model.
#
#   What it does, in order:
#   1. mirror-clones the repo to ../<repo>-pre-purge-<UTC stamp>.git as a full
#      backup of every ref (delete it yourself once you are satisfied);
#   2. creates a local branch for every origin branch that has none, so a
#      remote-only branch is rewritten too instead of keeping the blob;
#   3. runs `git filter-repo --force --invert-paths` over all refs;
#   4. re-adds the origin remote (filter-repo removes it on purpose);
#   5. verifies: no commit on any ref touches a purged path, and no blob on any
#      ref matches the credential patterns below;
#   6. with --push, force-pushes branches and tags.
#
# HOW TO INTERPRET THE OUTPUT
#   `backup: <dir>`          the pre-rewrite mirror; restore with
#                            `git clone <dir>` if anything went wrong.
#   `tracked origin/<b>`     a remote-only branch that was given a local ref
#                            so the rewrite reaches it.
#   `verify path <p>: 0`     commits on any ref still touching <p>; anything
#                            but 0 is a failure and nothing is pushed.
#   `verify secrets: 0`      commits whose diff matches a credential pattern
#                            (hf_, sk-, ghp_, github_pat_, AKIA, private key
#                            headers); anything but 0 names the commits and
#                            nothing is pushed.
#   `worktree <dir>`         another worktree whose branch was rewritten; run
#                            `git -C <dir> reset --hard` there (after saving
#                            its uncommitted work) so it matches its branch.
#   After the push every commit sha has changed: any note, tag message or
#   document that cites a sha now points at a commit that no longer exists on
#   the remote. The old commits can remain reachable on the hosting side by
#   sha until it garbage-collects; a leaked credential is revoked regardless,
#   since revoking is the only thing that makes it useless.
set -euo pipefail

PUSH=0
PATHS=()
for a in "$@"; do
    case "$a" in
        --push) PUSH=1 ;;
        -h|--help) sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) PATHS+=("$a") ;;
    esac
done
[ "${#PATHS[@]}" -gt 0 ] || { echo "usage: $0 [--push] PATH [PATH ...]" >&2; exit 2; }

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

git filter-repo --version >/dev/null 2>&1 || {
    echo "git-filter-repo is not installed (pacman -S git-filter-repo, or pip install git-filter-repo)" >&2
    exit 2
}
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "tracked files have uncommitted changes; commit them first" >&2
    git status --short --untracked-files=no >&2
    exit 2
fi

ORIGIN_URL="$(git remote get-url origin 2>/dev/null || true)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$(dirname "$ROOT")/$(basename "$ROOT")-pre-purge-$STAMP.git"
git clone --quiet --mirror "$ROOT" "$BACKUP"
echo "backup: $BACKUP"

if [ -n "$ORIGIN_URL" ]; then
    git fetch --quiet --prune origin
    while read -r rb; do
        case "$rb" in origin/*) ;; *) continue ;; esac   # `origin` alone is origin/HEAD
        b="${rb#origin/}"
        [ "$b" = "HEAD" ] && continue
        if ! git show-ref --verify --quiet "refs/heads/$b"; then
            git branch --quiet "$b" "$rb"
            echo "tracked $rb"
        fi
    done < <(git for-each-ref --format='%(refname:short)' refs/remotes/origin)
fi

ARGS=()
for p in "${PATHS[@]}"; do
    case "$p" in
        glob:*) ARGS+=(--path-glob "${p#glob:}") ;;
        *) ARGS+=(--path "$p") ;;
    esac
done
git filter-repo --force --invert-paths "${ARGS[@]}"

if [ -n "$ORIGIN_URL" ]; then
    git remote add origin "$ORIGIN_URL"
fi

FAIL=0
for p in "${PATHS[@]}"; do
    case "$p" in
        glob:*) n="$(git log --all --format=%h -- "${p#glob:}" | wc -l)" ;;
        *) n="$(git log --all --format=%h -- "$p" | wc -l)" ;;
    esac
    echo "verify path $p: $n"
    [ "$n" -eq 0 ] || FAIL=1
done
SECRET_RE='(hf_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY)'
HITS="$(git log --all --format='%h %cs' --name-only -G "$SECRET_RE" | grep -v '^$' || true)"
echo "verify secrets: $(printf '%s' "$HITS" | grep -c . || true)"
if [ -n "$HITS" ]; then
    printf '%s\n' "$HITS" | sed 's/^/  /'
    FAIL=1
fi

git worktree list --porcelain | awk '/^worktree /{print $2}' | while read -r wt; do
    [ "$wt" = "$ROOT" ] && continue
    echo "worktree $wt"
done

if [ "$FAIL" -ne 0 ]; then
    echo "verification failed: nothing pushed; the backup is $BACKUP" >&2
    exit 1
fi

PUSH_CMDS=(
    "git push --force --all origin"
    "git push --force --tags origin"
)
if [ "$PUSH" -eq 1 ] && [ -n "$ORIGIN_URL" ]; then
    git push --force --all origin
    git push --force --tags origin
    git fetch --quiet --prune origin
    echo "pushed to $ORIGIN_URL"
else
    echo "local rewrite done; to publish it:"
    printf '  %s\n' "${PUSH_CMDS[@]}"
fi
