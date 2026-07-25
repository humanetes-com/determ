#!/usr/bin/env python3
"""Print the exact model id(s) a Claude Code session actually used.

Usage: session_model.py <session-uuid>

Resolves the session's transcript under ~/.claude/projects/*/<uuid>.jsonl and
prints the ids comma-separated (empty output + exit 1 when nothing is known
yet). Used by operator_run.sh so the pass report names the exact model rather
than the alias that was requested on the command line.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline_core.domain.session_transcript import models_from_transcript  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    sid = argv[1]
    matches = sorted(Path.home().glob(f".claude/projects/*/{sid}.jsonl"))
    if not matches:
        return 1
    models: list[str] = []
    for path in matches:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for model in models_from_transcript(fh):
                if model not in models:
                    models.append(model)
    if not models:
        return 1
    print(",".join(models))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
