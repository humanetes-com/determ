#!/usr/bin/env bash
# Shared "restart from <stage>" mechanism — sourced by restart_from.sh (anyhow,
# launches via continue.sh) and run_microcrate.sh (microcrate, launches pipeline
# directly). Keeps the stage→phase-key mapping in ONE place so the two callers
# can never drift. Pure state manipulation; does NOT stop or launch the pipeline
# (each caller owns its process lifecycle).

# restart_from_keys <stage> — print the space-separated phase-entry keys to clear
# (the stage + every stage after it). Returns 2 on an unknown stage.
#   task1   → re-run FR synthesis + the whole reconstruct loop
#   task2/task2a → re-run the loop (per-cluster fill → merge → merged-fill → test → genesis)
#   task2b/task2c → skip the expensive per-cluster fill (kept in .skeleton-filled/)
#   task3/task4   → keep all fills done; straight to test/fix
#   task0c  → re-run the test augmentation itself and everything after it. The
#             state keys alone do not bring the original source back: the
#             caller must ALSO launch with --restart-from-pre-augment, which
#             resets the workspace to the task0c-pre-<module> tag (restart_from.sh
#             does; run_hardcrate.sh/run_microcrate.sh do not and refuse it).
#             task0d and bake are listed for the phase keys those stages will
#             write; a key that never appears in the state file clears nothing.
# task0a/task0b and earlier still need redo.sh or a re-clone.
restart_from_keys() {
    # Accept the monitor's short labels too (t1, t2, t2a, t2b, t2c, t3, t4): a
    # leading `t<digit>` normalises to `task<digit>` so callers (run_hardcrate
    # `--from t2c`) share one vocabulary with the docs.
    local s="$1"
    [[ "$s" =~ ^t[0-9] ]] && s="task${s#t}"
    case "$s" in
        task0c)        echo "task0c task0d bake strip task1 task2a task2b task2c task2-4" ;;
        task1)         echo "task1 task2a task2b task2c task2-4" ;;
        task2|task2a)  echo "task2a task2b task2c task2-4" ;;
        task2b|task2c) echo "task2b task2c task2-4" ;;
        task3|task4)   echo "task2-4" ;;
        *)             return 2 ;;
    esac
}

# restart_from_clear <project_dir> <key>... — delete passing-* lock tags (when the
# reconstruct loop is re-entered) and remove the chosen `[<key>...]` lines from
# pipeline_state.md so generate() no longer treats those phases as done.
restart_from_clear() {
    local project_dir="$1"; shift
    local keys=("$@")

    if printf '%s\n' "${keys[@]}" | grep -q 'task2-4'; then
        if [[ -d "$project_dir/.git" ]]; then
            local tags
            tags=$(git -C "$project_dir" tag -l "passing-*" 2>/dev/null || true)
            if [[ -n "$tags" ]]; then
                echo "[restart-from] deleting locked tags: $(echo "$tags" | tr '\n' ' ')"
                echo "$tags" | xargs -r -I{} git -C "$project_dir" tag -d {} >/dev/null 2>&1 || true
            fi
        fi
    fi

    local state="$project_dir/pipeline_state.md"
    if [[ -f "$state" ]]; then
        echo "[restart-from] clearing ${keys[*]} entries from $state"
        python3 - "$state" "${keys[@]}" << 'PYEOF'
import re, sys
path = sys.argv[1]; keys = sys.argv[2:]
with open(path) as f:
    lines = f.read().splitlines()
alt = "|".join(re.escape(k) for k in keys)
pat = re.compile(rf"^\[(?:{alt})(?:/[^\]]*)?\]")
kept, removed = [], 0
for ln in lines:
    if pat.match(ln):
        removed += 1
        continue
    if ln.startswith("current:"):
        ln = "current: "
    kept.append(ln)
with open(path, "w") as f:
    f.write("\n".join(kept) + "\n")
print(f"  removed {removed} phase line(s)")
PYEOF
    fi
}
