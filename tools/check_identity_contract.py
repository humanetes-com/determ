#!/usr/bin/env python3
"""Fail if a non-dispatch path keys conviction on FnLocator.

WHY. FnLocator is the dispatch key and may be ambiguous (six ``from`` in
``error.rs``). Conviction, reversion, exhaustion and error focusing take a
FnSite. After the write-path change, a ``convicting.setdefault(FnLocator(``
is the collapse that stamped every From coercion with a panic inside
``construct_from_boxed`` (run ``463713-b7c666c8``). This gate reads the AST
so the next such write is a check.sh failure, not a live 8-hour refill.

USAGE. ``python tools/check_identity_contract.py`` from the repo root.
Walks ``pipeline_core/**/*.py``. Tests live under tests/ and are not
walked (they construct locator-keyed maps that ``FailureFrontier.build``
lifts).

HOW TO INTERPRET. Each hit is ``path:lineno: <snippet>``. Zero hits is
clean. A planted dummy in tests/ is the negative control.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "pipeline_core"


def _is_fnlocator(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name) and func.id == "FnLocator":
        return True
    return isinstance(func, ast.Attribute) and func.attr == "FnLocator"


def _target_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def hits_in_tree(tree: ast.AST, *, filename: str = "") -> list[tuple[int, str]]:
    """Line numbers and a short label for conviction-shaped FnLocator writes."""
    found: list[tuple[int, str]] = []

    class V(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            if (isinstance(func, ast.Attribute) and func.attr == "setdefault"
                    and node.args and _is_fnlocator(node.args[0])):
                name = _target_name(func.value)
                if "convicting" in name.lower():
                    found.append((node.lineno,
                                  f"{name}.setdefault(FnLocator…)"))
            self.generic_visit(node)

        def visit_Assign(self, node: ast.Assign) -> None:
            for t in node.targets:
                if (isinstance(t, ast.Subscript)
                        and _is_fnlocator(t.slice)
                        and "convicting" in _target_name(t.value).lower()):
                    found.append((node.lineno,
                                  "convicting[FnLocator…] ="))
            self.generic_visit(node)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            t = node.target
            if (isinstance(t, ast.Subscript)
                    and _is_fnlocator(t.slice)
                    and "convicting" in _target_name(t.value).lower()):
                found.append((node.lineno, "convicting[FnLocator…] ="))
            self.generic_visit(node)

    V().visit(tree)
    return found


def scan_path(path: Path) -> list[tuple[Path, int, str]]:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    return [(path, ln, lab) for ln, lab in hits_in_tree(tree, filename=str(path))]


def scan_core(root: Path = CORE) -> list[tuple[Path, int, str]]:
    out: list[tuple[Path, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        if path.name.startswith("."):
            continue
        out.extend(scan_path(path))
    return out


def main() -> int:
    found = scan_core()
    if not found:
        print("  OK  identity contract (no FnLocator conviction writes)")
        return 0
    print("FAIL  conviction keyed on FnLocator (use FnSite):", file=sys.stderr)
    for path, ln, lab in found:
        rel = path.relative_to(ROOT)
        print(f"  {rel}:{ln}: {lab}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
