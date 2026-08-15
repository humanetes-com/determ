#!/usr/bin/env python3
"""Refund layout-retrigger attempts for a component whose burn was mechanical
(splice/locate infra died before the model's output was ever evaluated — e.g.
the pre-locate-key "could not locate a frozen definition" failure mode, fixed
since). The cap exists to bound MODEL attempts; an attempt with no model
evaluation bought no evidence and must not count against it.

Usage:
  .venv/bin/python tools/reset_retrigger_attempts.py PROJECT_DIR CLUSTER KEY_SUBSTR [NEW_COUNT] [--write]

KEY_SUBSTR — any substring of the component key (sorted names joined by `|`).
NEW_COUNT  — attempts value to set (default 1: keep the one real attempt).
--write    — actually write. WITHOUT it this only prints what it would do.

The default is a dry run because this file is live pipeline state and a run
reads it every sweep: invoked on 2026-08-15 to inspect a key, it silently set
the 34-name construct/vtable component to the count passed as an example while
that very component's re-author was in flight (run `669347-cf33eae3`). A tool
whose reading mode and writing mode are the same invocation has no reading mode.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline_core.domain.layout_retrigger import (  # noqa: E402
    BOOKKEEPING_KEY_PREFIXES,
)


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    argv = [a for a in sys.argv[1:] if a != "--write"]
    write = "--write" in sys.argv
    project_dir, cluster, key_substr = argv[0], argv[1], argv[2]
    new_count = int(argv[3]) if len(argv) > 3 else 1
    path = Path(project_dir) / ".skeleton" / f"_layout_retrigger_attempts_{cluster}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    # The store keeps bookkeeping rows beside the component counters — the
    # mechanical streak and the build that earned it, the in-flight marker and
    # its pid, the rejection streak and its contract epoch, the payoff and
    # crash-signature rows. Matching one would make every substring ambiguous,
    # so the prefix list comes from the domain that writes them rather than
    # being restated here (it was restated here, as `mech:`/`inflight:` alone,
    # and went stale the moment a sixth prefix was added).
    hits = [k for k in data
            if key_substr in k
            and not k.startswith(BOOKKEEPING_KEY_PREFIXES)]
    if len(hits) != 1:
        print(f"need exactly one key matching {key_substr!r}, got {len(hits)}: "
              f"{[h[:80] for h in hits]}")
        return 1
    key = hits[0]
    old = data[key]
    if not write:
        print(f"DRY RUN — would set {key[:80]}…: attempts {old} -> "
              f"{new_count}. Re-run with --write to apply.")
        return 0
    data[key] = new_count
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"refunded {key[:80]}…: attempts {old} -> {new_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
