#!/usr/bin/env python3
"""AST map: function/method name -> source of its return annotation.

Used to mechanically type `Callable[..., Any]` Deps fields by mirroring the
real bound function's declared return type. No regex; pure `ast`.
Usage: return_type_map.py NAME [NAME ...]   (prints name -> return per def found)
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def unparse(node: ast.AST | None) -> str:
    return "None-annotated" if node is None else ast.unparse(node)


def collect() -> dict[str, list[tuple[str, str]]]:
    out: dict[str, list[tuple[str, str]]] = {}
    for p in sorted(ROOT.rglob("*.py")):
        if ".venv" in p.parts or "tmp" in p.parts or ".claude" in p.parts:
            continue
        try:
            tree = ast.parse(p.read_text(), filename=str(p))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                rel = p.relative_to(ROOT)
                out.setdefault(node.name, []).append(
                    (f"{rel}:{node.lineno}", unparse(node.returns))
                )
    return out


def main() -> None:
    m = collect()
    names = sys.argv[1:] or sorted(m)
    for n in names:
        for loc, ret in m.get(n, [("NOT-FOUND", "")]):
            print(f"{n:42s} -> {ret:32s} @ {loc}")


if __name__ == "__main__":
    main()
