#!/usr/bin/env python3
"""Refund layout-retrigger attempts for a component whose burn was mechanical
(splice/locate infra died before the model's output was ever evaluated — e.g.
the pre-locate-key "could not locate a frozen definition" failure mode, fixed
since). The cap exists to bound MODEL attempts; an attempt with no model
evaluation bought no evidence and must not count against it.

Usage:
  .venv/bin/python tools/reset_retrigger_attempts.py PROJECT_DIR CLUSTER KEY_SUBSTR [NEW_COUNT]

KEY_SUBSTR — any substring of the component key (sorted names joined by `|`).
NEW_COUNT  — attempts value to set (default 1: keep the one real attempt).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    project_dir, cluster, key_substr = sys.argv[1], sys.argv[2], sys.argv[3]
    new_count = int(sys.argv[4]) if len(sys.argv) > 4 else 1
    path = Path(project_dir) / ".skeleton" / f"_layout_retrigger_attempts_{cluster}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    # `mech:` / `inflight:` rows are bookkeeping the store keeps beside the
    # component counters; matching them would make every substring ambiguous.
    hits = [k for k in data
            if key_substr in k and not k.startswith(("mech:", "inflight:"))]
    if len(hits) != 1:
        print(f"need exactly one key matching {key_substr!r}, got {len(hits)}: "
              f"{[h[:80] for h in hits]}")
        return 1
    key = hits[0]
    old = data[key]
    data[key] = new_count
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"refunded {key[:80]}…: attempts {old} -> {new_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
