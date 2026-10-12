#!/usr/bin/env bash
# Shared "restart from <stage>" mechanism — sourced by restart_from.sh (anyhow,
# launches via continue.sh) and run_microcrate.sh (microcrate, launches pipeline
# directly). Keeps the stage→phase-key mapping in ONE place so the two callers
# can never drift. Pure state manipulation; does NOT stop or launch the pipeline
# (each caller owns its process lifecycle).

# restart_from_keys <stage> — print the space-separated phase-entry keys to clear
# (the stage + every stage after it). Returns 2 on an unknown stage.
#   task1   → re-run FR synthesis + the whole reconstruct loop
#   task2/task2a → re-run the loop (per-cluster fill → merge → merged-fill → test → genesis).
#                  restart_from.sh (anyhow) also drops that phase's products unless
#                  --keep-fills is set; this function does not. run_hardcrate.sh --from
#                  and run_microcrate.sh call only restart_from_clear.
#   task2b/task2c → skip the expensive per-cluster fill (kept in .skeleton-filled/)
#   task3/task4   → keep all fills done; straight to test/fix
#   task0c  → re-run the test augmentation itself and everything after it. The
#             state keys alone do not bring the original source back: the
#             caller must ALSO launch with --restart-from-pre-augment, which
#             resets the workspace to the task0c-pre-<module> tag (restart_from.sh
#             does; run_hardcrate.sh/run_microcrate.sh do not and refuse it).
#             The task0d harvest is KEPT: the restore lands on the behaviour
#             restore point (task0d-done-<module>) and its commit carries the
#             task0d/<cluster> entries. bake is listed for the phase key that
#             stage writes; a key never in the state file clears nothing.
#   task0d  → task0c plus the behaviour harvest: the caller ALSO passes
#             --reharvest-behaviours, which resets to task0c-pre-<module>
#             (before task0d) and deletes the task0d-done-<module> tag.
# task0a/task0b and earlier still need redo.sh or a re-clone.
restart_from_keys() {
    # Accept the monitor's short labels too (t1, t2, t2a, t2b, t2c, t3, t4): a
    # leading `t<digit>` normalises to `task<digit>` so callers (run_hardcrate
    # `--from t2c`) share one vocabulary with the docs.
    local s="$1"
    [[ "$s" =~ ^t[0-9] ]] && s="task${s#t}"
    case "$s" in
        task0d)        echo "task0d task0c bake strip task1 task2a task2b task2c task2-4" ;;
        task0c)        echo "task0c bake strip task1 task2a task2b task2c task2-4" ;;
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

# restart_from_drops_t2a <stage> — true when <stage> re-enters the per-cluster
# fill (task2, task2a, and the t2 / t2a labels). task2b and later do not.
restart_from_drops_t2a() {
    local s="$1"
    [[ "$s" =~ ^t[0-9] ]] && s="task${s#t}"
    [[ "$s" == "task2" || "$s" == "task2a" ]]
}

# restart_from_drop_t2a_products <project_dir> — remove what the per-cluster
# fill wrote, and leave the layout, the generated tests and the FRs.
#
# The seed copies a skeleton file only when the filled destination is missing,
# and heal copies .best_tree back over a fresh tree, so both directories go.
# The floor is the same phase's dispatch memory, stored beside the layout:
# resume keeps the pass cursor, the stall and the futility evidence across a
# tree change (only the high-water drops), so a stub seed would skip units
# whose bodies were just deleted. Convictions stay fully_exhausted until a
# recorded blocker fills, which a re-stub does not do. The ask ledger would
# treat the old questions as already put to the model, and the red ledger is
# what task 3 prints about the tree task 2a just closed.
restart_from_drop_t2a_products() {
    local project_dir="$1"
    local _d _f _nullglob=0
    for _d in .skeleton-filled .best_tree; do
        if [[ -d "$project_dir/$_d" ]]; then
            echo "[restart-from] removing $project_dir/$_d ($(du -sh "$project_dir/$_d" | cut -f1))"
            rm -rf "$project_dir/$_d"
        fi
    done
    shopt -q nullglob && _nullglob=1
    shopt -s nullglob
    for _f in "$project_dir"/.skeleton/_tdd_floor_*.json \
              "$project_dir"/.phase_c_logs/ask_ledger_*.json \
              "$project_dir"/.phase_c_logs/red_ledger_*.json; do
        echo "[restart-from] removing $_f"
        rm -f "$_f"
    done
    if [[ $_nullglob -eq 0 ]]; then
        shopt -u nullglob
    fi
    if [[ -f "$project_dir/.phase_c_logs/retrospectives.json" ]]; then
        echo "[restart-from] removing $project_dir/.phase_c_logs/retrospectives.json"
        rm -f "$project_dir/.phase_c_logs/retrospectives.json"
    fi
    echo "[restart-from] kept: .skeleton (layout), generated tests, FRs, body and route ledgers"
}
