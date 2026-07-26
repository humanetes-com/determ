#!/usr/bin/env python3
"""Find every fn on disk whose body is only a re-declaration of itself.

The shape a whole-item LLM answer leaves behind when it is spliced between the
braces of the existing fn: the outer body is empty, the real code sits in a
nested item that is never called. It parses, it type-checks, it holds no
`unimplemented!()` — so it reads as FILLED to every scan. On a `#[test]` fn the
same shape is worse than dead code: the test passes while asserting nothing, so
the oracle counts a pass that proves nothing, and the test-oracle immutability
rule means nothing will ever repair it.

    tools/probe_hollow_bodies.py [PATH ...]      # default: every cluster tree
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline_core.domain.rust_fn_headers import fn_headers, locate_all_fns  # noqa: E402
from pipeline_core.domain.rust_scan import body_is_only_redeclaration  # noqa: E402

_DEFAULT_ROOTS = ("tmp/anyhow/.skeleton-filled", "tmp/anyhow/src", "tmp/anyhow/tests")


def _is_test_fn(text: str, sig_start: int) -> bool:
    """`#[test]` anywhere in the 200 chars of attributes above the signature."""
    return "#[test]" in text[max(0, sig_start - 200):sig_start]


def scan_file(path: Path) -> list[tuple[int, str, bool]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    hits: list[tuple[int, str, bool]] = []
    for name in dict.fromkeys(h.name for h in fn_headers(text, line_anchored=False)):
        for _sig, ss, bs, be in locate_all_fns(text, name):
            if body_is_only_redeclaration(text[bs:be], name):
                hits.append((text.count("\n", 0, ss) + 1, name, _is_test_fn(text, ss)))
    return sorted(hits)


def main() -> int:
    roots = [Path(a) for a in sys.argv[1:]] or [Path(r) for r in _DEFAULT_ROOTS]
    files = sorted({f for r in roots for f in ([r] if r.is_file() else r.rglob("*.rs"))})
    total = tests = 0
    for f in files:
        for line, name, is_test in scan_file(f):
            total += 1
            tests += is_test
            print(f"{'TEST ' if is_test else 'FN   '} {f}:{line} :: {name}")
    print(f"\n{len(files)} file(s) scanned — {total} hollow body/bodies, "
          f"{tests} of them #[test] (unrepairable: test targets are immutable)")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
