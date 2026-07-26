#!/usr/bin/env bash
#
# operator_run.sh — one unattended operator-mode pass, then commit.
#
# Loads Claude Code non-interactively with the message "load the handover then
# start operator mode", lets it run a single operator iteration (analyse the handover,
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
#   OPERATOR_EFFORT    reasoning effort for the pass (default: max) — matches an
#                      interactive operator session, where the deep-analysis
#                      work this pass does is what the setting buys
#
# No spend ceiling is set. `--max-budget-usd` caps a figure the CLI DERIVES from
# token counts at list API rates; with no ANTHROPIC_API_KEY in the environment
# these runs authenticate against the subscription, so that figure is never
# money — it can only cut a pass off mid-work. The pass is bounded by the flock,
# the cooldown, and the one-iteration instruction below instead.
#   OPERATOR_PERMS     permission flag (default: --dangerously-skip-permissions;
#                      set to "--permission-mode bypassPermissions" to prefer
#                      the softer mode)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"

# Single pass at a time, machine-wide. A pass kills and restarts the pipeline;
# the restarted process has no memory of the pass that spawned it, so without a
# lock that outlives both it would launch a second one that edits and commits
# the same working tree concurrently. Held for the whole run via fd 9.
exec 9>/tmp/pipeline_operator_pass.lock
flock -n 9 || {
  echo "operator_run: another pass already holds the lock — exiting." >&2
  exit 0
}

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
- Perform EXACTLY ONE full operator-mode iteration (load the handover -> deep
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
#    --session-id: fixes the transcript filename up front so the exact model id
#      (the alias below resolves to one) can be read back for the report header.
mkdir -p logs/auto
LOG="logs/auto/operator_run.log"
PART="logs/auto/operator_run.part"     # live, tailable; survives a hard kill
MODEL_REQ="${OPERATOR_MODEL:-opus}"
SESSION_ID="$(python3 -c 'import uuid;print(uuid.uuid4())')"
PASS_START="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "operator_run: starting pass -> $LOG (live: tail -f $PART)"

: > "$PART"
if ! claude -p "load the handover then start operator mode" \
      --model "$MODEL_REQ" \
      --effort "${OPERATOR_EFFORT:-max}" \
      --session-id "$SESSION_ID" \
      ${OPERATOR_PERMS:---dangerously-skip-permissions} \
      --settings '{"includeCoAuthoredBy": false}' \
      --append-system-prompt "$APPEND_SP" \
      --output-format text 2>&1 | tee "$PART"; then
  echo "operator_run: WARNING — claude exited non-zero; committing whatever changed." >&2
fi

# Header is written AFTER the pass, ahead of the body, because the exact model id
# only exists once the session transcript does — the report must name the model
# that wrote it, not the alias that was asked for.
MODEL_USED="$(.venv/bin/python tools/session_model.py "$SESSION_ID" 2>/dev/null || true)"
if [ -n "$MODEL_USED" ]; then
  MODEL_LINE="model: $MODEL_USED (requested: $MODEL_REQ)"
else
  MODEL_LINE="model: $MODEL_REQ (requested alias; exact id unresolved)"
fi
{
  echo "===== operator_run pass — $PASS_START ====="
  echo "date:  $PASS_START"
  echo "$MODEL_LINE"
  echo "session: $SESSION_ID"
  echo
  cat "$PART"
  echo "===== operator_run pass end — $(date -u +%Y-%m-%dT%H:%M:%SZ) ====="
} >> "$LOG"
rm -f "$PART"

# 4. Safety net: guarantee a commit even if the pass did not make one. The
#    message must read exactly like a message a developer wrote by hand: no
#    "operator pass" / "fallback" / "automated" wording, no timestamp, no
#    mention of Claude or of how the message was produced — git already
#    records the real commit time in its own metadata. A bash string built
#    from filenames cannot describe WHAT changed, so a model call (same
#    OPERATOR_MODEL as the pass itself) reads the actual staged diff and
#    drafts the message; the commit-msg hook still strips any attribution
#    line as a deterministic backstop either way.
if [ -n "$(git status --porcelain)" ]; then
  echo "operator_run: uncommitted changes remain — committing as a fallback."
  git add -A
  CHANGED_FILES="$(git diff --cached --name-only)"
  FIRST_FILES="$(printf '%s\n' "$CHANGED_FILES" | head -3 | paste -sd, -)"

  COMMIT_MSG="$(claude -p "$(cat <<EOF
Write a git commit message for the staged diff below. Output ONLY the raw
message text (short subject line, blank line, then a body of 1-3 sentences
if useful) — no markdown code fences, no preamble, no mention of automation,
tooling, dates, or authorship. Describe only what the diff changes.

$(git diff --cached | head -c 20000)
EOF
)" --model "${OPERATOR_MODEL:-opus}" \
      --output-format text 2>/dev/null | sed -e '/^```/d' -e '/./,$!d')" || true

  if [ -z "$COMMIT_MSG" ]; then
    COMMIT_MSG="update $FIRST_FILES"
  fi

  git commit -m "$COMMIT_MSG"
else
  echo "operator_run: working tree clean (pass committed its own changes)."
fi

echo "operator_run: done — log: $LOG"
