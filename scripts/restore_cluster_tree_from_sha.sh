#!/usr/bin/env bash
#
# WHY THIS EXISTS
# ---------------
# `.best_tree/<cluster>` records the best tree a cluster ever reached, and for
# months it was write-only as a TREE: `load_cluster_best_tree_mark` reads the
# score and hash to decide whether a sweep may overwrite the slot, heal's
# `BEST_TREE_COPY` tier reads ONE file when that file is unparseable, and the
# whole-directory `restore_best_tree` is module scope over `ctx.project_dir`
# inside a task3/task4 repair iteration. So when a deliberate re-stub did not
# climb back, the loop could not return to the tree it left — the high-water is
# re-based to the post-destruction score and every later sweep scores as an
# improvement against it.
#
# Measured on `anyhow`: run `1082156-4da4faf7` iter 1 (2026-08-14T10:07Z) sat at
# 79/101 with one crashed binary, fired the crash retrigger, re-authored
# `['StdError']` and reverted 25 bodies. Eight of the twelve scored sweeps in
# the 30 hours after it read exactly 39, across four runs, and the entire
# 79→39 deficit is two binaries that stopped executing (`unittests src/lib.rs`
# 37/0 and `tests/test_convert.rs` 4/0 leave the `iter N binaries:` vector;
# every surviving binary is byte-identical).
#
# WHY IT STILL EXISTS NOW THAT THE LOOP HAS ITS OWN RESTORE.
# `application/workspace_lifecycle.restore_cluster_best_tree` puts both halves
# of the slot back, and the TDD driver's flat-score guard calls it before it
# stops the run. It is guarded by `cluster_best_tree_has_layout`, and it
# DECLINES when the slot holds bodies without the `_layout/` half — which is
# every slot written before that half was stored, i.e. every slot on disk when
# this line was written. This script covers exactly that gap, and it covers a
# second one the in-loop restore does not reach at all: a tree from an EARLIER
# run, which the slot never held because the slot only ever keeps the best
# tree of the lineage the mark describes.
#
# The pipeline commits its own restore point before it destroys anything —
# `pre-layout-retrigger checkpoint — <cluster>/[...] attempt N` — and those
# commits carry `.skeleton/<cluster>` and `.skeleton-filled/<cluster>`
# together. This script takes that PAIR back, out of the object store. It is an
# operator lever, deliberately: the in-loop mechanism restores what THIS
# lineage recorded, and reaching past that is a decision with no measurement
# behind it, so it belongs to a human.
#
# USAGE
# -----
#   bash scripts/restore_cluster_tree_from_sha.sh <sha> [cluster] [project_dir]
#       sha          workspace-repo commit holding the pair (e.g. f318d6bc)
#       cluster      default: stable
#       project_dir  default: tmp/anyhow
#
#   DRY RUN unless RESTORE_WRITE=1 is set.
#
# PRECONDITIONS the script enforces, because each one has cost a measurement:
#   * no pipeline may be running — a live loop writes into the tree being
#     replaced. Stop it with the graceful sentinel, never a SIGTERM
#     mid-authoring (that charges the component a mechanical failure).
#   * both halves must exist at <sha>. t1.5 re-authors the layout every run, so
#     bodies from one run do not compile against another's skeleton; taking one
#     directory without the other produces a tree that cannot build.
#
# It NEVER touches `.skeleton/*.json` at the top level — `_layout_registry`,
# `_layout_outcomes_*` and above all `_layout_retrigger_attempts_*` live there,
# and the attempt counters are what stop the restored tree being destroyed
# again by the same component the same way.
#
# HOW TO INTERPRET THE OUTPUT
# ---------------------------
#   layout delta / filled delta   files differing between <sha> and the live
#       tree, printed BEFORE anything moves. A large layout delta is the signal
#       to stop and think: the bodies answer to a layout, and a re-authored one
#       means the restore is a bigger claim than it looks.
#   SNAPSHOT <path>   where the tree being replaced went. Nothing is deleted —
#       the current pair is MOVED here, so the restore is reversible by moving
#       it back. Keep it until the next scored sweep says the restore held.
#   RESTORED          the pair is in place. The next `continue.sh` resumes
#       task2a on it; the first sweep is the verdict, and it should land near
#       the score the checkpoint was taken at.
#
set -u

SHA="${1:-}"
CLUSTER="${2:-stable}"
PROJECT="${3:-tmp/anyhow}"

if [ -z "$SHA" ]; then
    echo "usage: $0 <sha> [cluster] [project_dir]   (RESTORE_WRITE=1 to apply)" >&2
    exit 2
fi

cd "$(dirname "$0")/.." || exit 1

if pgrep -f "pipeline.py --project" >/dev/null 2>&1; then
    echo "REFUSING: a pipeline is running. Stop it at a checkpoint first" >&2
    echo "  (touch /tmp/pipeline_graceful_stop, or lib/graceful_stop.sh)." >&2
    exit 3
fi

cd "$PROJECT" || exit 1

# The `src` halves only. `.skeleton-filled/<cluster>/target` is TRACKED in this
# workspace (its .gitignore is `/target/` and `/Cargo.lock` at the repo root
# only), so a whole-directory restore drags thousands of stale `.o` files and a
# stale `dep-graph.bin` back with it — on the 2026-08-14 pair that was 3710
# files of build cache around 10 files of source. Verified before narrowing:
# outside `src/` and `target/`, the pair is byte-identical (Cargo.toml,
# Cargo.lock and tests/ all unchanged), so `src` IS the whole delta. Re-check
# that with
#   git diff --name-only <sha> -- .skeleton/<c> .skeleton-filled/<c> \
#     | grep -v /target | grep -v /src/
# before trusting this narrowing on a different pair.
LAYOUT=".skeleton/$CLUSTER/src"
FILLED=".skeleton-filled/$CLUSTER/src"

for p in "$LAYOUT" "$FILLED"; do
    n=$(git ls-tree -r --name-only "$SHA" -- "$p" | wc -l)
    if [ "$n" -eq 0 ]; then
        echo "REFUSING: $SHA holds no $p — the pair is incomplete." >&2
        exit 4
    fi
    echo "  $SHA carries $p ($n file(s))"
done

echo
echo "layout delta ($SHA -> live):"
git diff --stat "$SHA" -- "$LAYOUT" | tail -20
echo "filled delta ($SHA -> live):"
git diff --stat "$SHA" -- "$FILLED" | tail -20
echo

if [ "${RESTORE_WRITE:-0}" != "1" ]; then
    echo "DRY RUN — set RESTORE_WRITE=1 to snapshot and restore."
    exit 0
fi

STAMP=$(git log -1 --format=%cd --date=format:%Y%m%dT%H%M%SZ "$SHA")
SNAP="../anyhow.pre_restart/${STAMP}_restore_${CLUSTER}"
mkdir -p "$SNAP/skeleton" "$SNAP/skeleton-filled" || exit 5
mv "$LAYOUT" "$SNAP/skeleton/src" || exit 5
mv "$FILLED" "$SNAP/skeleton-filled/src" || exit 5
echo "SNAPSHOT $(cd "$SNAP" && pwd)"

# `git archive | tar -x` rather than `git checkout <sha> -- <path>`: this
# working tree is permanently dirty and holds artefacts newer than any commit,
# and a checkout would write into it wholesale.
# `-m` because git archive stamps entries with the COMMIT date and tar restores
# it: extracted files would be older than cargo's fingerprints from the last
# build, cargo rebuilds only on newer-than-fingerprint, and the next `cargo
# test` would run the PRE-restore binaries against the restored sources — the
# same staleness measured in-pipeline on run `2620196-de4a7ca2` (2026-08-17),
# where a 57-file restore swept 0.4s later with a byte-identical vector.
git archive "$SHA" "$LAYOUT" "$FILLED" | tar -xm || exit 6
echo "RESTORED $LAYOUT + $FILLED from $SHA"
echo "  reverse it with:  rm -rf $LAYOUT $FILLED"
echo "                    mv $SNAP/skeleton/src $LAYOUT"
echo "                    mv $SNAP/skeleton-filled/src $FILLED"
