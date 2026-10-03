#!/usr/bin/env python3
"""No `/tmp` path outside `pipeline_core/domain/runtime_paths.py`, enforced statically.

A file under `/tmp` is shared with every process on the machine, the test suite
included. The pipeline's code held 30 such literals (HEAD `f3fe484`, by this
gate's own scan). Some were control channels that are machine-wide by design (a stop sentinel, the
auto-operator lock). Others were per-run artefacts that belong to one workspace
(coverage tool outputs, the monitor's per-file coverage cache, saved stream
logs). The suite reached both kinds three times: a test deleted a live run's
pending stop request, which then ran 1.5 h past it; a live stop killed 15
tests; and on 2026-10-03 a `check.sh` emptied the live run's coverage overview
and left a test's `microcrate` row in its place. Each fix redirected one module
constant in `tests/conftest.py`. The coverage paths were literals built inside
function bodies, which no redirect reaches, and nothing stopped the next one
from being written.

The rule, checkable: a string literal (an f-string's literal parts included)
that is `/tmp` or starts with `/tmp/` is a breach anywhere in `pipeline.py`,
`monitor.py`, `model_interact.py` or `pipeline_core/`, except in
`runtime_paths`. A new runtime file is named there, as a control channel under
`control_dir()` (redirected per test) or as a workspace scratch path
(`workspace_layout`). A real temp file goes through `tempfile`, which honours
`TMPDIR`.

Pure AST walk — no imports, no execution. Exit 1 on any breach. Run from
check.sh beside the other invariant gates.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOP_LEVEL: tuple[str, ...] = ("pipeline.py", "monitor.py", "model_interact.py")
PKG = "pipeline_core"
OWNER = "pipeline_core/domain/runtime_paths.py"


def _is_tmp_path(value: str) -> bool:
    return value == "/tmp" or value.startswith("/tmp/")


def scan_source(text: str, key: str) -> list[str]:
    """Breaches in one module's source, as `key:line: ...` lines."""
    if key == OWNER:
        return []
    hits = sorted((node.lineno, node.value)
                  for node in ast.walk(ast.parse(text, filename=key))
                  if isinstance(node, ast.Constant) and isinstance(node.value, str)
                  and _is_tmp_path(node.value))
    return [f"{key}:{line}: `/tmp` path literal {value[:60]!r}" for line, value in hits]


def scanned_files(root: Path = ROOT) -> list[Path]:
    files = [root / name for name in TOP_LEVEL]
    files.extend(sorted((root / PKG).rglob("*.py")))
    return [f for f in files if f.exists()]


def main() -> int:
    breaches: list[str] = []
    files = scanned_files()
    for path in files:
        key = str(path.relative_to(ROOT)).replace("\\", "/")
        breaches.extend(scan_source(path.read_text(encoding="utf-8"), key))
    if breaches:
        print("tmp-literal gate: BREACH")
        for b in breaches:
            print("  " + b)
        print("  fix: name the file in pipeline_core/domain/runtime_paths.py — a "
              "control channel under control_dir(), or a workspace scratch path "
              "(domain.workspace_layout) for anything one run writes.")
        return 1
    print(f"tmp-literal gate: ok ({len(files)} module(s) scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
