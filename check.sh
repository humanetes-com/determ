#!/usr/bin/env bash
# Static analysis + smoke tests — run before starting the pipeline to catch type errors,
# idiom regressions, extraction-wiring breaks, and gen_tests helper regressions.
# Tool config (ruff rule set, mypy strictness) lives in pyproject.toml; this script just
# runs the gates and FAILS on any finding. For the slower cargo coverage smoke, see test.sh.
#
# Parallel layout: the static gates run concurrently (outputs buffered to temp
# files so the log stays readable), then pytest runs on all cores via xdist
# (--dist loadfile: tests in one file share a worker — file-local fixtures stay safe).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PY_TARGETS=(pipeline.py monitor.py model_interact.py pipeline_core)

GATE_TMP="$(mktemp -d)"
trap 'rm -rf "$GATE_TMP"' EXIT

# Gating now — a type error fails the build. (A blind ruff autofix once renamed a loop var
# into a same-named Thread variable; only mypy caught the resulting clash. Hence: hard gate.)
"$SCRIPT_DIR/.venv/bin/mypy" "${PY_TARGETS[@]}" --no-error-summary \
    > "$GATE_TMP/mypy.out" 2>&1 &
PID_MYPY=$!
"$SCRIPT_DIR/.venv/bin/ruff" check "${PY_TARGETS[@]}" tools tests --no-cache --quiet \
    > "$GATE_TMP/ruff.out" 2>&1 &
PID_RUFF=$!
"$SCRIPT_DIR/.venv/bin/lint-imports" --config pyproject.toml \
    > "$GATE_TMP/layers.out" 2>&1 &
PID_LAYERS=$!
"$SCRIPT_DIR/.venv/bin/python" tools/check_extraction_invariants.py \
    > "$GATE_TMP/extraction.out" 2>&1 &
PID_EXTRACTION=$!
"$SCRIPT_DIR/.venv/bin/python" tools/check_ambiguous_fixtures.py \
    > "$GATE_TMP/fixtures.out" 2>&1 &
PID_FIXTURES=$!
"$SCRIPT_DIR/.venv/bin/python" tools/check_identity_contract.py \
    > "$GATE_TMP/identity.out" 2>&1 &
PID_IDENTITY=$!
"$SCRIPT_DIR/.venv/bin/python" tools/check_tree_writes.py \
    > "$GATE_TMP/tree_writes.out" 2>&1 &
PID_TREE_WRITES=$!
"$SCRIPT_DIR/.venv/bin/python" tools/loc_ratchet.py \
    > "$GATE_TMP/loc.out" 2>&1 &
PID_LOC=$!
"$SCRIPT_DIR/.venv/bin/python" tools/check_no_tmp_literals.py \
    > "$GATE_TMP/tmp_literals.out" 2>&1 &
PID_TMP_LITERALS=$!
# A keyword new at a `Callable[..., X]` port's call site is invisible to mypy;
# twice it was the first fill of a live run (2026-08-30, 2026-10-10).
"$SCRIPT_DIR/.venv/bin/python" tools/check_untyped_port_keywords.py \
    > "$GATE_TMP/port_keywords.out" 2>&1 &
PID_PORT_KEYWORDS=$!
# The extractor's own unit tests ran in NO gate before — a rendering regression
# there (e.g. definitions the t1.5 splice can never locate) only surfaced live.
#
# The other three gates mirror the `rust` job of .github/workflows/ci.yml exactly,
# because nothing local ran them and the push of 2026-08-15 failed on `cargo fmt
# --check` with 21 diff hunks across 5 files (src/lib.rs alone held 15) that had
# accumulated unseen. clippy/test/doc share one cargo build lock, so they run in
# sequence inside a single gate rather than fighting each other for it; fmt needs
# no build at all and gets its own, so a formatting failure reads on its own line.
#
# The TOOLCHAIN is pinned, and pinning it is what makes the gate meaningful: the
# same push then failed `cargo clippy -D warnings` on two `question_mark` hits in
# src/cfg.rs that this box could not see, because its default toolchain is 1.95.0
# (2026-04-14) and CI's `stable` had moved to 1.97.0 (2026-07-07), which added the
# lint. A gate on a different compiler than CI answers a different question. The
# other half of the pin is `dtolnay/rust-toolchain@1.97.0` in ci.yml — move both
# together, then run this gate and fix what the new lints find. `cargo +<version>`
# fails loudly when the toolchain is absent (rustup toolchain install 1.97.0
# --profile minimal --component clippy --component rustfmt), which is the intent:
# an unpinned fallback is how the drift got in.
#
# Only the GATE is pinned. The pipeline's own builds of the extractor keep using
# the default toolchain, so nothing here changes the compiler a live run is using.
RUST_GATE_TOOLCHAIN="${RUST_GATE_TOOLCHAIN:-1.97.0}"
(cd "$SCRIPT_DIR/tools/skeleton_extractor" && cargo "+$RUST_GATE_TOOLCHAIN" fmt --check) \
    > "$GATE_TMP/rustfmt.out" 2>&1 &
PID_RUSTFMT=$!
(cd "$SCRIPT_DIR/tools/skeleton_extractor" \
    && cargo "+$RUST_GATE_TOOLCHAIN" clippy --all-targets --quiet -- -D warnings \
    && cargo "+$RUST_GATE_TOOLCHAIN" test --quiet \
    && RUSTDOCFLAGS="-D warnings" cargo "+$RUST_GATE_TOOLCHAIN" doc --no-deps --quiet) \
    > "$GATE_TMP/cargo.out" 2>&1 &
PID_CARGO=$!

FAILED=0
gate() {  # gate <name> <pid> <outfile>
    local name="$1" pid="$2" out="$3"
    if wait "$pid"; then
        echo "  OK  $name clean"
    else
        echo "  FAIL  $name:"
        cat "$out"
        FAILED=1
    fi
}

echo "Running static gates in parallel (mypy, ruff, import-linter, extraction invariants, fixture ambiguity, identity contract, tree writes, LOC, /tmp literals, port keywords, rust)..."
gate "mypy" "$PID_MYPY" "$GATE_TMP/mypy.out"
gate "ruff" "$PID_RUFF" "$GATE_TMP/ruff.out"
gate "layer contract (adapters > application > ports > domain)" "$PID_LAYERS" "$GATE_TMP/layers.out"
gate "extraction invariants" "$PID_EXTRACTION" "$GATE_TMP/extraction.out"
gate "fixture ambiguity" "$PID_FIXTURES" "$GATE_TMP/fixtures.out"
gate "identity contract" "$PID_IDENTITY" "$GATE_TMP/identity.out"
gate "tree writes (every write into a fill tree goes through tree_txn)" "$PID_TREE_WRITES" "$GATE_TMP/tree_writes.out"
gate "LOC ratchet (god-files only shrink)" "$PID_LOC" "$GATE_TMP/loc.out"
gate "no /tmp path outside runtime_paths (tests share /tmp with live runs)" "$PID_TMP_LITERALS" "$GATE_TMP/tmp_literals.out"
gate "untyped-port keywords (no new keyword through a Callable[..., X] port)" "$PID_PORT_KEYWORDS" "$GATE_TMP/port_keywords.out"
gate "skeleton-extractor cargo fmt --check (rust $RUST_GATE_TOOLCHAIN)" "$PID_RUSTFMT" "$GATE_TMP/rustfmt.out"
gate "skeleton-extractor cargo clippy + test + doc (rust $RUST_GATE_TOOLCHAIN)" "$PID_CARGO" "$GATE_TMP/cargo.out"
[ "$FAILED" -eq 0 ] || exit 1

echo ""
echo "Running pytest (xdist, all cores) + coverage ratchet (cargo coverage tests skip unless SMOKE_FULL=1)..."
# Coverage is gated on EVERYTHING under refactor — pipeline.py (the engine
# being decomposed) and all of pipeline_core (where refactored code lands) — via a
# per-target ratchet (.coverage_ratchet.json): floors initialize from the first run,
# auto-raise when new tests land, and FAIL any change that loses coverage.
# domain/ keeps its absolute 95% minimum inside the ratchet (MIN_FLOORS). The
# `pipeline_all` target (pipeline.py + pipeline_core combined) is relocation-invariant:
# moving covered code out of the pipeline.py god-file can't drop it (LOC ratchet
# enforces the shrinking instead).
# --timeout: a single hung/slow test (e.g. an offline test that reaches a real
# backoff sleep) fails loudly in 60s instead of stalling the whole run for tens
# of minutes. thread method works under xdist workers (signal does not).
"$SCRIPT_DIR/.venv/bin/python" -m pytest -n auto --dist loadfile \
    --timeout=60 --timeout-method=thread \
    --cov=pipeline --cov=pipeline_core --cov-report=
"$SCRIPT_DIR/.venv/bin/python" tools/coverage_ratchet.py || { echo "coverage ratchet failed (see above)"; exit 1; }

echo ""
echo "Done."
