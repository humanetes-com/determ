#!/usr/bin/env bash
# The `#[cold]` invariant, counted the way it should always have been.
#
# The standing invariant used to read "`#[cold]` in
# .skeleton-filled/stable/src/error.rs = 30, must not grow". That counted 30
# `#[cold]` LINES against 16 annotated FNS, because attribute runs held repeats
# (several x3) — fossil from a splice that re-prepended attributes and was
# fixed 2026-07-27. A grep count therefore read GREEN on a tree where real
# `#[cold]` fns were lost and RED on one that was merely cleaned. The residue
# was removed on 2026-08-01 (`tools/dedupe_attribute_runs.py`), so lines and
# fns now agree at 16 — but they agree only as long as nothing re-prepends,
# which is why the duplicate count stays in the output below.
#
# The real invariant is the count of ANNOTATED FNS, and it must not SHRINK
# (losing a `#[cold]` is losing a cold path; gaining one is not a defect).
# Both cluster trees independently hold 16, which is the cross-check.
set -u
cd "$(dirname "$0")/.." || exit 2
EXPECT="${1:-16}"
.venv/bin/python - "$EXPECT" <<'PY'
import pathlib, sys
sys.path.insert(0, ".")
from pipeline_core.domain.rust_scan import (
    attributed_fn_names, count_attrs, duplicate_attribute_runs)

expect = int(sys.argv[1])
rc = 0
for tree in ("stable", "nightly"):
    p = pathlib.Path(f"tmp/anyhow/.skeleton-filled/{tree}/src/error.rs")
    if not p.exists():
        print(f"  {tree}: MISSING {p}")
        continue
    s = p.read_text(encoding="utf-8", errors="replace")
    fns = attributed_fn_names(s, "cold")
    dups = duplicate_attribute_runs(s)
    status = "OK " if len(fns) >= expect else "RED"
    if len(fns) < expect:
        rc = 1
    print(f"  {status} {tree}: {len(fns)} #[cold] fn(s) "
          f"({len(set(fns))} distinct) >= {expect}?  "
          f"[{count_attrs(s, 'cold')} raw attribute lines, "
          f"{len(dups)} duplicated attribute(s) — must stay 0; a non-zero "
          f"count means something re-prepended attributes again]")
sys.exit(rc)
PY
