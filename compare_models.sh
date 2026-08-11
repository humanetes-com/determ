#!/usr/bin/env bash
# A/B two MODELS over the same reconstruction, from the same starting tree.
#
# What is being compared is how far the tree gets per SWEEP and per hour, not
# tokens/sec. Those come apart: a model whose iterations are slower but whose
# fills land reaches a pass count in fewer iterations, and a throughput
# comparison ranks it last. `tools/compare_trajectories.py` reports it as a
# trajectory for that reason — a single endpoint is noise, the same crate went
# 81 on one run and 21 on another on 2026-08-07.
#
#   bash compare_models.sh snapshot <name>          # freeze today's tree as the seed
#   bash compare_models.sh arm <name> <qwen|muse>   # restore seed, point .env, restart vLLM
#   bash compare_models.sh report <A=run> <B=run>   # side-by-side trajectories
#   bash compare_models.sh list                     # seeds on disk + run ids in the sink
#
# Between the two arms nothing else may change: same crate, same cluster, same
# seed tree. The one confound that is NOT free is context — see FAIR below.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ENV_FILE="$SCRIPT_DIR/.env"
SEED_ROOT="${COMPARE_SEED_ROOT:-$SCRIPT_DIR/.compare-seeds}"
PROJECT="${COMPARE_PROJECT:-anyhow}"

_env_get() { grep -m1 "^$1=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d ' ' || true; }

_env_set() {
    local key="$1" val="$2"
    if grep -q "^$key=" "$ENV_FILE" 2>/dev/null; then
        sed -i "s|^$key=.*|$key=$val|" "$ENV_FILE"
    else
        echo "$key=$val" >> "$ENV_FILE"
    fi
    echo "   [ok] .env  $key=$val"
}

# Refuse to touch anything while a run is live. Both halves of this script
# mutate state a running pipeline is actively reading — `arm` replaces the
# cluster's tree under it and restarts the server it is mid-call against.
_refuse_if_running() {
    local pids
    pids=$(pgrep -f "[p]ipeline.py" || true)
    if [[ -n "$pids" ]]; then
        echo "[error] a pipeline is running (PIDs: $(echo "$pids" | tr '\n' ' '))." >&2
        echo "        Stop it first — the arm below would replace the tree it is filling" >&2
        echo "        and restart the server it is mid-call against:" >&2
        echo "            bash continue.sh --force     # or: pkill -9 -f pipeline.py" >&2
        exit 1
    fi
}

# The tree the loop actually builds and scores is NOT src/. It is
# `.skeleton-filled/<cluster>/src`, answering to the authored layout in
# `.skeleton/<cluster>/src`. Take BOTH or neither: t1.5 re-authors the layout on
# every run, so bodies from one run do not compile against another's skeleton
# (between two 2026-08-07 trees, ErrorImpl lost a whole backtrace field). A seed
# that carries only the fills restores a tree that cannot build.
#
# The pair is not the whole seed. Two more workspace directories carry state the
# fill loop READS and the comparison is ABOUT, and neither lives under the two
# above:
#
#   .phase_c_logs/retrospectives.json — the conviction store. Its
#     `fully_exhausted` flags are what `is_fn_fully_exhausted` consults before
#     offering an fn to the model at all, and `readmission_warranted` releases
#     them only on a metric that beats the high-water. On 2026-08-11 this file
#     was 46,415 bytes for tmp/anyhow, holding convictions written by earlier
#     Qwen runs (`src/error.rs::new::Error`, GENUINELY_HARD, among them). Left
#     in place across a re-arm, the second model starts already told which fns
#     not to attempt — by the first model. That is the measurement, deleted.
#   .best_tree/<cluster>/src — heal's durable restore tier. Left in place, a
#     tree one arm produced can be restored into the other mid-run.
#
# Both are therefore snapshotted with the pair and wiped on restore. They are
# optional in the tar: a workspace that has not reached phase C yet has neither.
_seed_paths() {
    local ws="$1" p out=()
    for p in .skeleton .skeleton-filled .best_tree .phase_c_logs; do
        [[ -e "$ws/$p" ]] && out+=("$p")
    done
    printf '%s\n' "${out[@]}"
}

# Printed by snapshot and by restore, so the reset is VISIBLE rather than
# assumed: an arm that starts with a non-zero count it did not earn is the
# unfairness this seed set exists to close, and the number is the only cheap
# way to see it happened.
_conviction_count() {
    "$SCRIPT_DIR/.venv/bin/python" - "$1/.phase_c_logs/retrospectives.json" <<'PY' 2>/dev/null || echo "?"
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
if not p.exists():
    print(0); raise SystemExit
try:
    store = json.loads(p.read_text())
except Exception:
    print("?"); raise SystemExit
recs = store.values() if isinstance(store, dict) else store
print(sum(1 for r in recs if isinstance(r, dict) and r.get("fully_exhausted")))
PY
}

_workspace() {
    local ws
    ws=$(_env_get WORKSPACE_DIR)
    echo "${ws:-$SCRIPT_DIR/tmp/$PROJECT}"
}

cmd_snapshot() {
    local name="${1:?usage: compare_models.sh snapshot <name>}"
    # A seed taken while fills are landing is a torn tree: the tar would catch
    # some bodies mid-write and the two arms would not start from the same
    # place, which is the one thing this harness exists to guarantee.
    _refuse_if_running
    local ws; ws=$(_workspace)
    local dst="$SEED_ROOT/$name"
    [[ -d "$ws/.skeleton" ]] || { echo "[error] no .skeleton under $ws — nothing to seed from." >&2; exit 1; }
    mkdir -p "$dst"
    # tar, not `git checkout`/`git stash`: this workspace is permanently dirty
    # and holds fills newer than any commit, and a git-side restore would
    # discard them. Nothing here reads or writes the object store.
    local paths=(); mapfile -t paths < <(_seed_paths "$ws")
    tar -C "$ws" -cf "$dst/tree.tar" "${paths[@]}"
    {
        echo "seed:      $name"
        echo "workspace: $ws"
        echo "project:   $PROJECT"
        echo "taken:     $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "model_at_snapshot: $(_env_get MODEL)"
        echo "carried:   ${paths[*]}"
        echo "stubs_remaining:   $(grep -rl 'unimplemented!()' "$ws/.skeleton-filled" 2>/dev/null | wc -l) file(s)"
        echo "convictions:       $(_conviction_count "$ws") fully_exhausted flag(s)"
    } > "$dst/seed.txt"
    echo "Seed '$name' written to $dst"
    cat "$dst/seed.txt"
}

cmd_restore() {
    local name="${1:?usage: compare_models.sh restore <name>}"
    _refuse_if_running
    local ws; ws=$(_workspace)
    local src="$SEED_ROOT/$name/tree.tar"
    [[ -f "$src" ]] || { echo "[error] no seed '$name' at $src" >&2; exit 1; }
    # Wipe every path the seed can carry, not just the two it used to. A
    # directory left behind is not neutral: it is the previous arm's state,
    # surviving into the next arm's run under the seed's name.
    echo "Restoring seed '$name' into $ws (replaces the tree, the conviction store and .best_tree) ..."
    rm -rf "$ws/.skeleton" "$ws/.skeleton-filled" "$ws/.best_tree" "$ws/.phase_c_logs"
    tar -C "$ws" -xf "$src"
    echo "   [ok] tree restored from $(grep '^taken:' "$SEED_ROOT/$name/seed.txt")"
    echo "   [ok] convictions now $(_conviction_count "$ws") fully_exhausted flag(s)"
}

# FAIR: the arms must see the same context, or the result measures context.
# Muse Glimmer serves at 32768 on this box against Qwen's 131072, and the input
# budget does not degrade gracefully — safe_input_char_limit with the shipped
# VLLM_THINKING_BUDGET=16384 falls from 293,376 chars at 131072 to the 4,000
# char FLOOR at 32768, which is a model that cannot see the file it is filling.
# So both arms run at the smaller window with a thinking budget that leaves room
# (8192 -> 23,040 chars). Set COMPARE_CTX/COMPARE_THINK to explore other points;
# raising the Muse ceiling needs docker_vllm.sh's per-model ceiling raised too.
CTX="${COMPARE_CTX:-32768}"
THINK="${COMPARE_THINK:-8192}"
CONTENT="${COMPARE_CONTENT:-8192}"

cmd_arm() {
    local name="${1:?usage: compare_models.sh arm <seed> <qwen|muse>}"
    local which="${2:?usage: compare_models.sh arm <seed> <qwen|muse>}"
    _refuse_if_running
    local model
    case "$which" in
        qwen) model="Qwen/Qwen3.6-27B-FP8" ;;
        muse) model="RedHatAI/Muse-Glimmer-30B-FP8-block" ;;
        */*)  model="$which" ;;
        *)    echo "[error] arm wants qwen | muse | Org/Repo, got '$which'" >&2; exit 2 ;;
    esac

    cmd_restore "$name"
    echo "Pointing .env at $model ..."
    _env_set MODEL "$model"
    _env_set VLLM_MAX_MODEL_LEN "$CTX"
    _env_set VLLM_THINKING_BUDGET "$THINK"
    _env_set VLLM_CONTENT_BUDGET "$CONTENT"

    echo "Restarting vLLM on the new model (10-20 min load) ..."
    bash "$SCRIPT_DIR/docker_vllm.sh" --y

    echo
    echo "Arm ready: seed=$name model=$model ctx=$CTX think=$THINK"
    echo "Launch it with — pre_run.sh first, the tree stamp moved when the fix landed:"
    echo
    echo "    bash pre_run.sh && bash continue.sh"
    echo
    echo "Then note the run id from the launch banner; report needs it."
}

cmd_report() {
    [[ $# -ge 2 ]] || { echo "usage: compare_models.sh report <LABEL=RUN_ID> <LABEL=RUN_ID>" >&2; exit 2; }
    local args=()
    for spec in "$@"; do args+=(--arm "$spec"); done
    "$SCRIPT_DIR/.venv/bin/python" tools/compare_trajectories.py "${args[@]}"
}

cmd_list() {
    echo "── seeds"
    if [[ -d "$SEED_ROOT" ]]; then
        for d in "$SEED_ROOT"/*/; do
            [[ -f "$d/seed.txt" ]] && echo "   $(basename "$d")  $(grep '^taken:' "$d/seed.txt" | cut -d' ' -f2-)"
        done
    else
        echo "   (none — run: bash compare_models.sh snapshot <name>)"
    fi
    echo "── runs in the event sink"
    "$SCRIPT_DIR/.venv/bin/python" tools/compare_trajectories.py --list
}

case "${1:-}" in
    snapshot) shift; cmd_snapshot "$@" ;;
    restore)  shift; cmd_restore "$@" ;;
    arm)      shift; cmd_arm "$@" ;;
    report)   shift; cmd_report "$@" ;;
    list)     shift; cmd_list "$@" ;;
    *)        sed -n '2,18p' "$0"; exit 2 ;;
esac
