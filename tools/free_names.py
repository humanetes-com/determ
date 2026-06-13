#!/usr/bin/env python3
"""Report module-global names referenced inside a set of top-level defs/classes.

Usage: python tools/free_names.py <file.py> <Name1> [Name2 ...]

For each requested top-level def/class, computes the set of names it references
that are NOT bound locally (params, assignments, comprehension vars, nested defs)
— i.e. names it expects to resolve as module globals or builtins. Builtins are
filtered out. This is the dependency surface a verbatim relocation must satisfy.
"""
from __future__ import annotations

import ast
import builtins
import sys

BUILTINS = set(dir(builtins)) | {"__name__", "__file__", "self", "cls", "__class__"}


class ScopeCollector(ast.NodeVisitor):
    """Collect free names within a function/class scope (best-effort, ignores
    proper nested-scope rules but good enough to surface module deps)."""

    def __init__(self) -> None:
        self.bound: set[str] = set()
        self.used: set[str] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._handle_func(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._handle_func(node)

    def _handle_func(self, node) -> None:
        for a in node.args.args + node.args.kwonlyargs + node.args.posonlyargs:
            self.bound.add(a.arg)
        if node.args.vararg:
            self.bound.add(node.args.vararg.arg)
        if node.args.kwarg:
            self.bound.add(node.args.kwarg.arg)
        self.bound.add(node.name)
        for d in node.decorator_list:
            self.visit(d)
        for stmt in node.body:
            self.visit(stmt)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.bound.add(node.name)
        for b in node.bases:
            self.visit(b)
        for d in node.decorator_list:
            self.visit(d)
        for stmt in node.body:
            self.visit(stmt)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            self.bound.add(node.id)
        else:
            self.used.add(node.id)

    def visit_arg(self, node: ast.arg) -> None:
        self.bound.add(node.arg)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        self.visit(node.target)
        self.visit(node.iter)
        for c in node.ifs:
            self.visit(c)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for a in node.args.args:
            self.bound.add(a.arg)
        self.visit(node.body)


def free_names(node) -> set[str]:
    c = ScopeCollector()
    c.generic_visit(node) if isinstance(node, ast.Module) else c.visit(node)
    return (c.used - c.bound) - BUILTINS


def main() -> None:
    path = sys.argv[1]
    wanted = set(sys.argv[2:])
    with open(path) as f:
        tree = ast.parse(f.read())
    all_top = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    agg: set[str] = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name in wanted:
            fn = free_names(n)
            agg |= fn
    # split into: defined-elsewhere-in-this-module vs external
    internal = sorted(agg & all_top)
    external = sorted(agg - all_top)
    print("=== references to OTHER top-level names in this module ===")
    print("\n".join(internal))
    print("\n=== references to imported/other names (imports, consts) ===")
    print("\n".join(external))


if __name__ == "__main__":
    main()
