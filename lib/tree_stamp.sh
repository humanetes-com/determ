#!/usr/bin/env bash
# The content hash of the code that decides how a run behaves.
#
# `pre_run.sh` records this hash after its gates pass; `continue.sh` recomputes it
# and refuses to launch when the two disagree. That pairing is the whole point:
# the stamp is not a timestamp, so a gate run that passed against a DIFFERENT tree
# cannot vouch for this one.
#
# Git cannot serve this role here. Runs are launched from an uncommitted working
# tree as a matter of course, so `git rev-parse HEAD` is identical across the edit
# that introduced a deadlock and the edit that fixed it. The bytes are the only
# honest identity.
#
# What is inside the hash is "anything that changes what the loop decides":
# pipeline.py, every module of pipeline_core, the tools/ scripts the gates run,
# and the skeleton extractor's Rust sources — a rendering regression there is
# invisible to every Python gate and surfaces only when a live t1.5 splice cannot
# locate a definition. What is deliberately OUTSIDE it: tests (a new test cannot
# make a passing gate stale), the .md notes, and cargo's target/ directory.
#
# The hash covers file CONTENTS and repo-relative paths only. `sha256sum` prints
# the path it was given, so hashing its output verbatim would make the stamp
# depend on how the caller spelled the root — `pipeline_tree_stamp .` and
# `pipeline_tree_stamp /home/user/determ` would disagree about an identical
# tree, and moving the checkout would invalidate a gate that is still perfectly
# valid. The subshell `cd` makes every printed path relative to the root.
pipeline_tree_stamp() {
    local root="$1"
    (
        cd "$root" || return 1
        {
            sha256sum ./pipeline.py
            find ./pipeline_core ./tools \
                \( -path '*/target' -o -path '*/__pycache__' \) -prune -o \
                -type f \( -name '*.py' -o -name '*.rs' -o -name '*.toml' \) -print0 \
                | sort -z | xargs -0 -r sha256sum
        } | sha256sum | cut -d' ' -f1
    )
}

# Where the stamp lives. Untracked and machine-local: it is a claim about THIS
# checkout's bytes, and copying it between machines would assert something the
# copy never verified.
pipeline_stamp_file() {
    echo "$1/.pre_run_stamp"
}
