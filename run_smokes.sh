#!/usr/bin/env bash
# Fixture smokes -> perf ledger, one command:
#
#   bash run_smokes.sh ["note for the ledger rows"]
#
# 1. Ensures vLLM on :8000 is serving the MODEL named in .env — if it already
#    is, the healthy container is left alone (restarting it would also upgrade
#    it and change the measurement); otherwise launches `docker_vllm.sh
#    --no-pull --y` (no :latest pull, so the server stays the constant across
#    ledger rows) and waits for readiness.
# 2. Runs `run_microcrate.sh` then `run_hardcrate.sh --complexity=7`, both
#    FULL (from scratch): the seeded ledger baselines are from-scratch runs,
#    so only a from-scratch run compares against them.
# 3. Appends one row per run to logs/perf_ledger.jsonl via
#    tools/perf_ledger.py, capturing each run's id from the events the run
#    itself emitted — run BEFORE touching .env, the row records it.
#
# A hardcrate REGRESSION exit does not skip the ledger append (the numbers of
# a failed run are exactly what the ledger is for); the script's own exit
# code still reports the failure.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

NOTE="${1:-}"
PY=.venv/bin/python
EVENTS=logs/events.jsonl

MODEL_LINE="$(grep -E '^MODEL=' .env | head -1 | cut -d= -f2-)"
if [ -z "$MODEL_LINE" ]; then
    echo "run_smokes: no MODEL= line in .env" >&2
    exit 2
fi

probe() { curl -s --max-time 5 localhost:8000/v1/models 2>/dev/null || true; }

if probe | grep -qF "$MODEL_LINE"; then
    echo "run_smokes: vLLM already serving $MODEL_LINE — leaving it alone."
else
    echo "run_smokes: starting vLLM for $MODEL_LINE (--no-pull: same server image)"
    bash docker_vllm.sh --no-pull --y
    echo "run_smokes: waiting for vLLM readiness (up to 30 min)..."
    ready=""
    for _ in $(seq 1 180); do
        if probe | grep -qF "$MODEL_LINE"; then ready=1; break; fi
        sleep 10
    done
    if [ -z "$ready" ]; then
        echo "run_smokes: vLLM did not become ready — aborting before any run." >&2
        exit 1
    fi
fi

# Launch conformance (pipeline_core/adapters/model_conformance.py): the served
# id, the reasoning dialect the chat template reads, and the parser split, all
# against the model's profile. A smoke row recorded under a mismatched model is
# a measurement of the wrong thing, so a failure stops here.
if ! $PY -m pipeline_core.adapters.model_conformance --env-file .env --deadline 1800 \
        --report logs/model_conformance.json; then
    echo "run_smokes: model conformance failed — aborting before any run (logs/model_conformance.json)." >&2
    exit 1
fi

# Newest run_id among events appended after line-mark $1 (the smokes run
# serially and nothing else emits, so the new region belongs to the smoke).
new_run_id() {
    $PY - "$1" <<'PYEOF'
import json
import sys

start = int(sys.argv[1])
rid = ""
try:
    with open("logs/events.jsonl", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            if i < start:
                continue
            try:
                rid = json.loads(line).get("run_id") or rid
            except ValueError:
                pass
except OSError:
    pass
print(rid)
PYEOF
}

overall_rc=0
run_one() {
    local crate="$1"; shift
    local mark rc rid
    mark=$( [ -f "$EVENTS" ] && wc -l < "$EVENTS" || echo 0 )
    echo "=== run_smokes: $crate: $*"
    rc=0
    "$@" || rc=$?
    rid="$(new_run_id "$mark")"
    if [ -z "$rid" ]; then
        echo "run_smokes: $crate: no run id found in $EVENTS after the run — NOT appended." >&2
        overall_rc=1
        return
    fi
    local note="${NOTE:-smoke via run_smokes.sh}"
    if [ "$rc" -ne 0 ]; then
        note="$note (script exit $rc)"
        overall_rc="$rc"
    fi
    echo "run_smokes: $crate run $rid finished rc=$rc — appending ledger row."
    $PY tools/perf_ledger.py append "$rid" --crate "$crate" --note "$note" \
        || { echo "run_smokes: ledger append failed for $rid" >&2; overall_rc=1; }
}

run_one microcrate bash run_microcrate.sh
run_one hardcrate bash run_hardcrate.sh --complexity=7

echo
echo "run_smokes: latest ledger rows:"
$PY tools/perf_ledger.py list | tail -4
exit "$overall_rc"
