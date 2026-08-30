#!/usr/bin/env bash
# Commit with the message the assistant last wrote to last_commit_message.txt.
#
# Why this exists. The suggested commit message used to arrive as the last
# lines of a chat summary, and the terminal hard-wraps that text at whatever
# width the pane happens to be. Copy-pasting it put those wraps into the
# object: `git log` then shows a subject running into the body, ragged
# indentation and 100+ column lines (see the four-point message on abc2179).
# The message is now written to a FILE instead, and `git commit -F` takes it
# verbatim — nothing passes through a terminal on the way in.
#
# A later rule (hard-wrap the body at 72 columns) produced the same symptom
# from the other direction: an EOL landing mid-sentence, not at a paragraph
# boundary (2026-08-21). So an EOL in the file is only ever structural now —
# the subject/body separator or a blank line between points — and this script
# normalizes before committing: every run of non-blank lines (one paragraph)
# is rejoined into a single line, blank lines are left as the separators they
# are. That runs unconditionally, so a stray mid-paragraph wrap in the file
# costs nothing even if one slips in.
#
# USAGE
#   ./commit.sh                # commit what is staged, message from the file
#   ./commit.sh -a             # also stage tracked modifications (no new files)
#   ./commit.sh --amend        # reword/extend the previous commit
#   ./commit.sh <any git commit flag> ...   # all args pass through
#
# Preconditions: last_commit_message.txt exists, is non-empty, and describes
# the changes actually staged — it is written fresh for every batch, so it
# holds the CURRENT batch's message and nothing older. Nothing is staged for
# you unless you ask (`-a`, or `git add` first); the script refuses rather than
# guess what belongs in the commit.
#
# THE MESSAGE FILE IS CONSUMED. On a commit that succeeds, last_commit_message.txt
# is deleted. A message describes one batch and stops being true the moment that
# batch is in the object store, but the file used to survive the commit and stay
# readable — so a later `./commit.sh` on an unrelated batch, run before the next
# message was written, would silently commit under the PREVIOUS batch's subject
# and body. Overwriting-every-batch made that window small; deleting on success
# closes it, and the refusal above turns the mistake into an exit 2 that names
# what to do. The consequence to know: a second `./commit.sh --amend` needs the
# message written again, because the first one consumed it. `git commit --amend`
# with no `-F` still reuses what is already on HEAD if all that is wanted is to
# restage.
#
# HOW TO READ THE OUTPUT
#   "refusing: ..."      nothing was committed and the tree is untouched —
#                        the message file is missing/empty, nothing is staged,
#                        the subject carries a tracker id (`debt 91.2`,
#                        `FR-018`, `#82`), which is banned in commit messages
#                        because git log travels with the repo and the .md
#                        trackers do not, or the message carries assistant
#                        attribution (`Co-Authored-By:`, "Generated with",
#                        Claude/Anthropic/Cursor/Grok/Fable, 🤖). The commits are the user's,
#                        authored under the user's name; a trailer naming a
#                        model adds nothing a reader can act on and stays in
#                        the object forever. The assistant tooling appends
#                        such a trailer by default, so this check is what
#                        makes the override mechanical rather than something
#                        each batch has to remember.
#   "warning: ..."       the commit happened; the subject is over 72 columns
#                        and will be truncated in `git log --oneline`.
#   "committed <sha> <subject>"   done; last_commit_message.txt has been
#                        removed — write a new one for the next batch.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

MSG_FILE="last_commit_message.txt"

if [[ ! -s "$MSG_FILE" ]]; then
    echo "refusing: $MSG_FILE is missing or empty — write the commit message there first" >&2
    exit 2
fi

# Rejoin any mid-paragraph wrap: consecutive non-blank lines become one line,
# blank lines (the subject/body separator, the gaps between points) pass
# through untouched. A no-op on an already-correct file.
NORMALIZED="$(mktemp)"
trap 'rm -f "$NORMALIZED"' EXIT
awk '
    /^[[:space:]]*$/ { if (para != "") { print para; para = "" }; print ""; next }
    { para = (para == "") ? $0 : para " " $0 }
    END { if (para != "") print para }
' "$MSG_FILE" > "$NORMALIZED"

SUBJECT="$(head -n 1 "$NORMALIZED")"

if [[ -z "${SUBJECT// /}" ]]; then
    echo "refusing: $MSG_FILE starts with a blank line — line 1 is the subject" >&2
    exit 2
fi

if [[ "$SUBJECT" =~ ^[[:space:]]*(debt|FR-|plan[[:space:]]+#|#[0-9]) ]]; then
    echo "refusing: the subject opens with a tracker id — '$SUBJECT'" >&2
    echo "          state the fact the entry stood for instead (run ids, dates, symbols, numbers are fine)" >&2
    exit 4
fi

# Assistant attribution never reaches the object: the commits are the user's,
# and the drafting tool's default convention appends a Co-Authored-By trailer
# naming the model. Checked over the whole message, not just the subject —
# the trailer lands at the bottom. The name list is the editors and models
# that draft here (Claude, Cursor, Grok, Fable); substring match, same as
# `claude` already matching the conventions filename.
if ATTRIB="$(grep -n -i -E 'co-authored-by|generated with|claude|anthropic|cursor|grok|fable|🤖' "$NORMALIZED")"; then
    echo "refusing: the message carries assistant attribution —" >&2
    echo "$ATTRIB" | sed 's/^/          /' >&2
    echo "          rewrite $MSG_FILE without it; the commit is yours, not a model's" >&2
    exit 5
fi

# Anything staged? --amend and -a/--all/--include supply their own content.
WANTS_OWN_CONTENT=0
for arg in "$@"; do
    case "$arg" in
        -a|--all|--amend|-i|--include|--allow-empty|-a*) WANTS_OWN_CONTENT=1 ;;
    esac
done

if [[ $WANTS_OWN_CONTENT -eq 0 ]] && git diff --cached --quiet; then
    echo "refusing: nothing staged — run 'git add <paths>' first, or './commit.sh -a' for tracked modifications" >&2
    exit 3
fi

if [[ ${#SUBJECT} -gt 72 ]]; then
    echo "warning: subject is ${#SUBJECT} chars; git log --oneline truncates past 72" >&2
fi

git commit -F "$NORMALIZED" "$@"

# Only past `set -e`, i.e. only when git actually made the commit. The message
# has been consumed: leaving it on disk is what lets an unrelated later batch
# commit under this batch's subject.
rm -f "$MSG_FILE"

echo "committed $(git rev-parse --short HEAD) $SUBJECT"
echo "consumed $MSG_FILE — write a new message for the next batch"
