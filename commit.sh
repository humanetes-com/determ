#!/usr/bin/env bash
# Commit with the message the assistant last wrote to last_commit_message.txt.
#
# Why this exists. The suggested commit message used to arrive as the last
# lines of a chat summary, and the terminal hard-wraps that text at whatever
# width the pane happens to be. Copy-pasting it put those wraps into the
# object: `git log` then shows a subject running into the body, ragged
# indentation and 100+ column lines (see the four-point message on abc2179).
# The message is now written to a FILE instead, wrapped by the writer at 72
# columns, and `git commit -F` takes it verbatim — nothing passes through a
# terminal on the way in.
#
# USAGE
#   ./commit.sh                # commit what is staged, message from the file
#   ./commit.sh -a             # also stage tracked modifications (no new files)
#   ./commit.sh --amend        # reword/extend the previous commit
#   ./commit.sh <any git commit flag> ...   # all args pass through
#
# Preconditions: last_commit_message.txt exists, is non-empty, and describes
# the changes actually staged — it is overwritten on every batch, so it holds
# the CURRENT batch's message and nothing older. Nothing is staged for you
# unless you ask (`-a`, or `git add` first); the script refuses rather than
# guess what belongs in the commit.
#
# HOW TO READ THE OUTPUT
#   "refusing: ..."      nothing was committed and the tree is untouched —
#                        the message file is missing/empty, nothing is staged,
#                        or the subject carries a tracker id (`debt 91.2`,
#                        `FR-018`, `#82`), which is banned in commit messages
#                        because git log travels with the repo and the .md
#                        trackers do not.
#   "warning: ..."       the commit happened; the subject is over 72 columns
#                        and will be truncated in `git log --oneline`.
#   "committed <sha> <subject>"   done; the file is left in place.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

MSG_FILE="last_commit_message.txt"

if [[ ! -s "$MSG_FILE" ]]; then
    echo "refusing: $MSG_FILE is missing or empty — write the commit message there first" >&2
    exit 2
fi

SUBJECT="$(head -n 1 "$MSG_FILE")"

if [[ -z "${SUBJECT// /}" ]]; then
    echo "refusing: $MSG_FILE starts with a blank line — line 1 is the subject" >&2
    exit 2
fi

if [[ "$SUBJECT" =~ ^[[:space:]]*(debt|FR-|plan[[:space:]]+#|#[0-9]) ]]; then
    echo "refusing: the subject opens with a tracker id — '$SUBJECT'" >&2
    echo "          state the fact the entry stood for instead (run ids, dates, symbols, numbers are fine)" >&2
    exit 4
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

git commit -F "$MSG_FILE" "$@"

echo "committed $(git rev-parse --short HEAD) $SUBJECT"
