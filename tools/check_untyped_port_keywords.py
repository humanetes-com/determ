#!/usr/bin/env python3
"""Untyped-port keyword ratchet — no new keyword through a `Callable[..., X]` port.

WHY. A collaborator declared `Callable[..., X]` and bound through
`lambda *a, **k: f(*a, **k)` is invisible to mypy: a call may pass any
keyword, and one the bound implementation does not take is found only when the
call runs. Twice that was a live run's first fill — 2026-08-30 (`failing_tests=`
to the coverage-slice port) and 2026-10-10 (`due_rungs=` to the fill-pass port,
both clusters ended at pass 31 before any model call). Each time the keyword
was NEW at that call site.

WHAT. Every class attribute annotated `Callable[..., X]` (or `... | None`) in
pipeline.py and pipeline_core is a port; every call `<expr>.<port>(...)` with
keywords is read, and the keyword set per port name is compared with
`.untyped_port_keywords.json` (repo root, committed):

  - a keyword not in the baseline for that port  → FAIL: type the port first
    (a Protocol with the real signature, as `ports.fill_engine.FillPassPort`,
    and a binding mypy checks), then add the keyword
  - a port or keyword gone from the code          → baseline shrinks (commit it)
  - no baseline yet                               → initialized, passes

Matching is by attribute name (a call on an imported or injected stdlib
module, `subprocess.run(...)`, is not a port), so a typed method sharing a name with an untyped port is read
conservatively as the port. Positional-arity drift is
not read here.

USAGE.  .venv/bin/python tools/check_untyped_port_keywords.py   (run by check.sh)
"""
from __future__ import annotations

import ast
import json
import sys
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = ROOT / ".untyped_port_keywords.json"
SOURCES = ("pipeline.py", "pipeline_core")


def _is_untyped_callable(annotation: ast.expr) -> bool:
    """`Callable[..., X]`, alone or in a union."""
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return (_is_untyped_callable(annotation.left)
                or _is_untyped_callable(annotation.right))
    if not isinstance(annotation, ast.Subscript):
        return False
    head = annotation.value
    name = head.attr if isinstance(head, ast.Attribute) else getattr(head, "id", "")
    if name != "Callable" or not isinstance(annotation.slice, ast.Tuple):
        return False
    first = annotation.slice.elts[0] if annotation.slice.elts else None
    return isinstance(first, ast.Constant) and first.value is Ellipsis


def untyped_ports(trees: Iterable[ast.AST]) -> set[str]:
    """Names of class attributes annotated `Callable[..., X]`."""
    out: set[str] = set()
    for tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for stmt in node.body:
                if (isinstance(stmt, ast.AnnAssign)
                        and isinstance(stmt.target, ast.Name)
                        and _is_untyped_callable(stmt.annotation)):
                    out.add(stmt.target.id)
    return out


def _imported_names(tree: ast.AST) -> set[str]:
    """Names an import binds in this file: a call on one is a module or class
    function, not a port that shares its name."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            out.update(a.asname or a.name for a in node.names)
    return out


def port_keywords(sources: dict[str, str]) -> dict[str, set[str]]:
    """Per untyped port name, every keyword some call site passes it."""
    trees = [ast.parse(text, filename=name) for name, text in sources.items()]
    ports = untyped_ports(trees)
    out: dict[str, set[str]] = {}
    for tree in trees:
        imported = _imported_names(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ports
                    and not (isinstance(node.func.value, ast.Name)
                             and (node.func.value.id in imported
                                  or node.func.value.id in sys.stdlib_module_names))):
                kws = {k.arg for k in node.keywords if k.arg is not None}
                if kws:
                    out.setdefault(node.func.attr, set()).update(kws)
    return out


def compare(current: dict[str, set[str]],
            baseline: dict[str, list[str]]) -> tuple[list[str], dict[str, list[str]]]:
    """(failures, the shrunk baseline). A keyword is new when its port is
    absent from the baseline or the baseline does not list it."""
    failures: list[str] = []
    for port in sorted(current):
        new = current[port] - set(baseline.get(port, ()))
        if new:
            failures.append(
                f"  FAIL  `.{port}(...)` passes {sorted(new)}, new on a "
                "`Callable[..., X]` port: mypy cannot see whether the bound "
                "implementation takes it. Type the port (a Protocol with the "
                "real signature, as `ports.fill_engine.FillPassPort`) first.")
    shrunk = {p: sorted(set(kws) & current.get(p, set()))
              for p, kws in baseline.items()}
    return failures, {p: kws for p, kws in sorted(shrunk.items()) if kws}


def _read_sources() -> dict[str, str]:
    files: list[Path] = []
    for rel in SOURCES:
        path = ROOT / rel
        files.extend([path] if path.is_file() else sorted(path.rglob("*.py")))
    return {str(f.relative_to(ROOT)): f.read_text(encoding="utf-8") for f in files}


def main() -> int:
    current = port_keywords(_read_sources())
    if not BASELINE_PATH.exists():
        BASELINE_PATH.write_text(json.dumps(
            {p: sorted(k) for p, k in sorted(current.items())}, indent=2) + "\n")
        print(f"  OK    {len(current)} untyped port(s) called with keywords "
              "(ratchet initialized)")
        return 0
    baseline: dict[str, list[str]] = json.loads(BASELINE_PATH.read_text())
    failures, shrunk = compare(current, baseline)
    for line in failures:
        print(line)
    if not failures and shrunk != baseline:
        BASELINE_PATH.write_text(json.dumps(shrunk, indent=2) + "\n")
        print(f"  OK    ratchet lowered: {len(baseline)} -> {len(shrunk)} port(s)")
    elif not failures:
        print(f"  OK    {len(current)} untyped port(s) called with keywords, "
              "none with a new keyword")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
