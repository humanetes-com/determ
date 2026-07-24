#!/usr/bin/env bash
#
# operator_run.sh — one unattended operator-mode pass, then commit.
#
# Loads Claude Code non-interactively with the message "load handover and start
# operator mode", lets it run a single operator iteration (analyse the handover,
# apply fixes via stop-fix-validate, start the pipeline if that makes sense),
# and commits the result.
#
# A git commit-msg hook (tools/git-hooks/commit-msg) GUARANTEES that no commit
# ever attributes authorship to Claude — required for legal reasons. The hook is
# enforced by git on every commit, so the guarantee does not rely on the model.
#
# Usage:  ./operator_run.sh
# Env:
#   OPERATOR_MODEL     model alias (default: opus)
#   OPERATOR_MAX_USD   API spend cap for the pass in dollars (default: 10)
#   OPERATOR_PERMS     permission flag (default: --dangerously-skip-permissions;
#                      set to "--permission-mode bypassPermissions" to prefer
#                      the softer mode)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"

command -v claude >/dev/null 2>&1 || {
  echo "operator_run: 'claude' CLI not found on PATH" >&2
  exit 127
}

# 1. Install the attribution-stripping commit-msg hook (the deterministic legal
#    guarantee — git runs it on every commit made below).
install -m 0755 "$REPO/tools/git-hooks/commit-msg" "$REPO/.git/hooks/commit-msg"

# 2. Instructions layered on top of the project's CLAUDE.md for THIS run only.
APPEND_SP="$(cat <<'EOF'
AUTOMATION CONTEXT — overrides the project's "do not commit / ask before
committing" rule for THIS non-interactive run only:
- Perform EXACTLY ONE full operator-mode iteration (load handover -> deep
  analysis -> fixes via stop-fix-validate -> save handover), then STOP. Do not
  begin a second iteration and do not start a background watcher.
- If the pipeline is not running and resuming is the right call, start it before
  finishing, without blocking on a log tail (so this run can exit).
- Then commit ALL changes from this pass with a concise, descriptive message.
  Commit permission is PRE-GRANTED for this automated run; do not ask.
- The commit message must NOT contain a "Co-Authored-By: Claude" trailer, any
  "Co-authored-by" line naming Claude or Anthropic, or a "Generated with Claude
  Code" line. Commits must not attribute authorship to Claude (legal
  requirement).
EOF
)"

# 3. Run Claude headless.
#    --dangerously-skip-permissions: let the pass edit files, run bash and commit
#      without interactive approval (this repo is trusted; runs as a normal user).
#    --settings '{"includeCoAuthoredBy": false}': disable Claude's own commit
#      attribution as a first line of defence (the hook is the guarantee).
mkdir -p logs
LOG="logs/operator_run_$(date -u +%Y%m%d-%H%M%S).log"
echo "operator_run: starting pass -> $LOG"

if ! claude -p "load handover and start operator mode" \
      --model "${OPERATOR_MODEL:-opus}" \
      ${OPERATOR_PERMS:---dangerously-skip-permissions} \
      --settings '{"includeCoAuthoredBy": false}' \
      --append-system-prompt "$APPEND_SP" \
      --max-budget-usd "${OPERATOR_MAX_USD:-10}" \
      --output-format text 2>&1 | tee "$LOG"; then
  echo "operator_run: WARNING — claude exited non-zero; committing whatever changed." >&2
fi

# 4. Safety net: guarantee a commit even if the pass did not make one. The
#    commit-msg hook still strips any attribution from this message too.
if [ -n "$(git status --porcelain)" ]; then
  echo "operator_run: uncommitted changes remain — committing as a fallback."
  git add -A
  git commit -m "operator pass — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
else
  echo "operator_run: working tree clean (pass committed its own changes)."
fi

echo "operator_run: done — log: $LOG"
