#!/usr/bin/env python3
"""No executed pipeline string names a symbol of a crate under reconstruction.

Hard Rule 11: after strip, nothing the fill model sees is the original crate,
and the pipeline does not know the crate's names. A string literal that runs
(prompt text, a regex source, a name set, a substring test) and names a symbol
the crate declares is the crate's answer living in the pipeline. On 2026-10-17
an AST scan found 16 such literals in 6 files: a catalog driver whose recipes
named the wrapper type, the vtable builders and the exact constructor call,
one environment flip from a prompt, plus one live literal in the test
locator's generic-name set and one measured incident quoted into the derive
instruction of the layout prompt. The catalog was deleted and the two live
literals removed the same day; this gate keeps a new one out.

The rule, checkable: in `pipeline.py` and `pipeline_core`, every `str`
constant that is not a docstring or a bare string statement (both explain,
neither executes) is matched word-wise against every name in
`tools/crate_symbols/*.txt` (written per crate by
`scripts/crate_symbol_list.py` from the crate's pre-strip source). A hit is a
breach unless (module, name) is in ALLOWLIST with the reason it is not the
crate's answer. A stale allowlist line fails too.

Pure AST walk — no imports, no execution. Exit 1 on any breach. Run from
check.sh beside the other invariant gates.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SYMBOL_DIR = ROOT / "tools" / "crate_symbols"
SCANNED: tuple[Path, ...] = (ROOT / "pipeline.py", ROOT / "pipeline_core")

# (repo-relative module, name) -> why this literal is not the crate's answer.
ALLOWLIST: dict[tuple[str, str], str] = {
    ("pipeline_core/domain/std_api_sigs.py", "MyError"):
        "placeholder type in a std API signature example, not the crate's type",
    ("pipeline_core/prompts/retrospective.py", "MyError"):
        "placeholder type in a generic example of the retrospective prompt",
}


def _symbols() -> dict[str, str]:
    """name -> the list file it came from."""
    out: dict[str, str] = {}
    for path in sorted(SYMBOL_DIR.glob("*.txt")):
        for line in path.read_text(encoding="utf-8").splitlines():
            name = line.strip()
            if name and not name.startswith("#"):
                out.setdefault(name, path.name)
    return out


def _files() -> list[Path]:
    files: list[Path] = []
    for root in SCANNED:
        files.extend([root] if root.is_file() else sorted(root.rglob("*.py")))
    return files


def _explanatory(tree: ast.AST) -> set[int]:
    """ids of string constants that only explain: docstrings and any bare
    string statement (attribute docstrings included)."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            out.add(id(node.value))
    return out


def scan(symbols: dict[str, str]) -> list[tuple[str, int, str]]:
    if not symbols:
        return []
    pat = re.compile(
        r"(?<![A-Za-z0-9_])("
        + "|".join(map(re.escape, sorted(symbols, key=len, reverse=True)))
        + r")(?![A-Za-z0-9_])")
    hits: list[tuple[str, int, str]] = []
    for path in _files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        skip = _explanatory(tree)
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in skip):
                for m in pat.finditer(node.value):
                    hits.append((rel, node.lineno, m.group(1)))
    return hits


def main() -> int:
    symbols = _symbols()
    if not symbols:
        print(f"crate-symbol gate: no symbol list under {SYMBOL_DIR} — "
              "write one with scripts/crate_symbol_list.py")
        return 1
    breaches: list[str] = []
    used: set[tuple[str, str]] = set()
    for rel, line, name in scan(symbols):
        if (rel, name) in ALLOWLIST:
            used.add((rel, name))
            continue
        breaches.append(f"{rel}:{line}: `{name}` ({symbols[name]}) in an "
                        "executed string")
    stale = sorted(k for k in ALLOWLIST if k not in used)
    if breaches:
        print("crate-symbol gate: BREACH")
        for b in breaches:
            print("  " + b)
        print("  fix: state the rule without the crate's name (a measured "
              "incident goes in a comment), or add a (module, name) allowlist "
              "entry with the reason the string is not the crate's answer.")
        return 1
    if stale:
        print("crate-symbol gate: stale allowlist entries (no such literal any more):")
        for k in stale:
            print(f"  {k[0]}::{k[1]}")
        return 1
    print(f"crate-symbol gate: ok ({len(symbols)} name(s), "
          f"{len(used)} allowlisted literal(s), {len(_files())} file(s) scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
