"""The run-identity banner logged at startup.

The `PIPELINE STARTED …  pipeline.py=<md5_8>  git=<sha>  run_id=…` line is
LOAD-BEARING: the microcrate watcher, the autopilot's which-code-is-running
check (CLAUDE.md), and monitor tooling all grep it and compare the md5 to the
on-disk `pipeline.py`. This adapter owns the derivation (md5 + git short-sha
reads) and the exact line layout so the format is pinned by unit test instead
of living inline in `init_log`.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from pipeline_core.domain.measurement import code_identity


def banner_lines(pipeline_file: Path, run_id: str, argv: list[str],
                 parallelism: int) -> list[str]:
    """The startup banner, one entry per log call — byte-identical to the
    historical inline rendering (the md5 is of `pipeline_file` as it is ON
    DISK at startup; the git sha is decorative and absent outside a repo)."""
    py_hash = hashlib.md5(pipeline_file.read_bytes()).hexdigest()[:8]
    # `pipeline.py=` covers the composition root only, and the logic left it
    # long ago; `code=` covers everything that actually runs. Both are printed
    # because existing tooling greps the first — see `code_identity`.
    code_id = code_identity(pipeline_file.parent)
    git_hash = ""
    with suppress(Exception):
        git_hash = " git=" + subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=pipeline_file.parent, stderr=subprocess.DEVNULL, text=True,
        ).strip()
    bar = "=" * 60
    started = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    return [
        f"\n{bar}",
        bar,
        f"  PIPELINE STARTED — {started} UTC  "
        f"pipeline.py={py_hash}  code={code_id}{git_hash}  run_id={run_id}",
        f"  CMD: {sys.executable} {' '.join(argv)}",
        f"  PIPELINE_PARALLELISM={parallelism}",
        bar,
        bar,
    ]
