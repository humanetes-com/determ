#!/usr/bin/env python3
"""Void the `fully_exhausted` verdicts a cluster earned while its tree could not
COMPILE — attempts that never got a fair reading of the fn they convicted.

An exhaustion verdict says "this fn has had its chances and nothing new is
left to try". That claim needs the attempt to have been about the fn. When the
cluster's baseline is broken — a bad bake, or (the case this was written for) a
cross-cluster seed that put donor bodies in the wrong impl blocks — every fill is
graded against a crate that cannot build, and the retro classifier convicts on
errors the fn never caused. `is_fn_fully_exhausted` already reinterprets a
`downstream_only` record, so those heal themselves; a record whose errors landed
in the target file reads `signal="ok"` and does NOT, even when the error is a
seeding artefact (observed: `DisplayforError` exhausted GENUINELY_HARD on
`NonNull<ErrorImpl> cannot be sent between threads safely`, which came from a
mis-seeded vtable body two impls away).

Lessons are KEPT — category, guidance, history and counters all stay, exactly as
`reset_exhaustion_flags` does at an iteration boundary. Only the skip is lifted.

Usage:
  .venv/bin/python tools/void_cluster_exhaustion.py PROJECT_DIR CLUSTER [--apply]

Without `--apply` it prints what it would do and changes nothing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    project_dir, cluster = sys.argv[1], sys.argv[2]
    apply = "--apply" in sys.argv[3:]
    path = Path(project_dir) / ".phase_c_logs" / "retrospectives.json"
    if not path.exists():
        print(f"no retrospectives store at {path}")
        return 1
    data = json.loads(path.read_text(encoding="utf-8"))

    hits = [
        key for key, rec in data.items()
        if isinstance(rec, dict)
        and rec.get("cluster") == cluster
        and rec.get("fully_exhausted")
    ]
    if not hits:
        print(f"no exhausted records for cluster {cluster!r} — nothing to void")
        return 0

    for key in sorted(hits):
        rec = data[key]
        print(f"  {key}  [{rec.get('category')}] signal={rec.get('signal')}")
        if apply:
            rec["fully_exhausted"] = False
            # The per-fn re-admission cap counts resets, and this correction is
            # not one of the fn's own chances — leaving it charged would let a
            # single boundary reset retire the fn again immediately.
            rec["reset_attempts"] = 0

    verb = "voided" if apply else "would void"
    print(f"{verb} {len(hits)} exhaustion verdict(s) for cluster {cluster!r}")
    if apply:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    else:
        print("(dry run — pass --apply to write)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
