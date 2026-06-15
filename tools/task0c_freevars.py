"""Free-variable analysis for extracting a line-range of `task0c_augment_module`
into its own function (W5 thinning). Given a line range inside the function,
report the names the range USES that are bound by the enclosing function (params,
assignments, for-targets, with-as, pre-range nested defs) but NOT bound inside the
range itself — i.e. exactly the values that must be passed into an extracted
helper. Module globals/builtins are excluded (they resolve without passing).

Usage: python tools/task0c_freevars.py <start_line> <end_line>
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "pipeline_core/application/tasks/task0c.py"
FUNC = "task0c_augment_module"


def _bound_names(node: ast.AST) -> set[str]:
    """Names this node binds in its own scope (targets, params, for/with/except,
    nested def/class names, comprehension vars, walrus)."""
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            out.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
    return out


def _used_loads(stmts: list[ast.stmt]) -> set[str]:
    out: set[str] = set()
    for s in stmts:
        for n in ast.walk(s):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                out.add(n.id)
    return out


def main() -> None:
    start, end = int(sys.argv[1]), int(sys.argv[2])
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    func = next(f for f in ast.walk(tree)
                if isinstance(f, ast.FunctionDef) and f.name == FUNC)

    func_bound = _bound_names(func) | {a.arg for a in func.args.args}
    module_bound = {n.name for n in tree.body
                    if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    module_bound |= {t.id for n in tree.body if isinstance(n, ast.Assign)
                     for t in n.targets if isinstance(t, ast.Name)}
    module_bound |= {a.asname or a.name.split(".")[0]
                     for n in tree.body if isinstance(n, ast.Import) for a in n.names}
    module_bound |= {a.asname or a.name
                     for n in tree.body if isinstance(n, ast.ImportFrom) for a in n.names}

    in_range = [s for s in ast.walk(func)
                if isinstance(s, ast.stmt) and start <= getattr(s, "lineno", -1) <= end
                and getattr(s, "end_lineno", -1) <= end]
    # Top-level statements of the range only (avoid double counting nested).
    top = [s for s in in_range
           if not any(s is not o and start <= getattr(o, "lineno", -1)
                      and getattr(o, "end_lineno", 1 << 30) >= getattr(s, "end_lineno", -1)
                      and o is not s and _contains(o, s) for o in in_range)]

    used = _used_loads(top)
    bound_in_range: set[str] = set()
    for s in top:
        bound_in_range |= _bound_names(s)

    free = sorted(n for n in used
                  if n in func_bound and n not in bound_in_range and n not in module_bound)
    print(f"range {start}-{end}: {len(free)} free names bound by the function:")
    for n in free:
        print(" ", n)


def _contains(outer: ast.AST, inner: ast.AST) -> bool:
    return any(c is inner for c in ast.walk(outer)) and outer is not inner


if __name__ == "__main__":
    main()
