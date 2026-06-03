#!/usr/bin/env python3
"""Structural invariants for the strangler-fig task extractions (CODE_ARCHITECTURE.md §5).

mypy/ruff catch undefined names and dead code, but they cannot verify the project-specific
contract that every extracted task obeys: a task module exposes a frozen ``*Deps`` dataclass
+ a task fn that rebinds `deps.<field>` locals, and pipeline.py builds the matching
``_*_DEPS`` instance. A half-finished extraction (a field declared but never provided, a
prelude local read from a non-existent field, a body name that resolves to nothing) passes
mypy yet is wrong. This tool reconciles, per task module:

  declared fields  ==  fields provided at the pipeline.py construction site
  every declared field is consumed (read as deps.<field>) in the module — no dead fields
  no deps.<field> read that isn't declared (would AttributeError on a frozen+slots dataclass)
  the task fn body has zero unbound free names (every name is param/local/import/builtin)

Pure static analysis over the AST — no imports, no pipeline execution. Exit 1 on any breach.
Run from check.sh. Generalises across every current and future ``tasks/*.py`` extraction.
"""

from __future__ import annotations

import ast
import builtins
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASKS_DIR = ROOT / "pipeline_core" / "application" / "tasks"
PIPELINE = ROOT / "pipeline.py"


def _deps_classes(tree: ast.Module) -> dict[str, list[str]]:
    """Map every `*Deps` dataclass name → its declared field names (AnnAssign targets)."""
    out: dict[str, list[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name.endswith("Deps"):
            out[node.name] = [
                s.target.id for s in node.body
                if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)
            ]
    return out


def _deps_reads(fn: ast.AST) -> set[str]:
    """Every attribute read as `deps.<x>` anywhere in the function."""
    return {
        n.attr for n in ast.walk(fn)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "deps"
    }


def _free_unbound(fn: ast.FunctionDef, module_names: set[str]) -> list[str]:
    """Names loaded in the fn that are never bound (param/local/import/builtin)."""
    bound: set[str] = set(dir(builtins)) | module_names
    loaded: list[str] = []

    class V(ast.NodeVisitor):
        def visit_Name(self, n: ast.Name) -> None:
            if isinstance(n.ctx, ast.Store):
                bound.add(n.id)
            elif isinstance(n.ctx, ast.Load):
                loaded.append(n.id)

        def visit_arg(self, n: ast.arg) -> None:
            bound.add(n.arg)

        def visit_FunctionDef(self, n: ast.FunctionDef) -> None:
            bound.add(n.name)
            for a in n.args.posonlyargs + n.args.args + n.args.kwonlyargs:
                bound.add(a.arg)
            if n.args.vararg:
                bound.add(n.args.vararg.arg)
            if n.args.kwarg:
                bound.add(n.args.kwarg.arg)
            self.generic_visit(n)

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

        def visit_Lambda(self, n: ast.Lambda) -> None:
            for a in n.args.posonlyargs + n.args.args + n.args.kwonlyargs:
                bound.add(a.arg)
            self.generic_visit(n)

        def visit_ExceptHandler(self, n: ast.ExceptHandler) -> None:
            if n.name:
                bound.add(n.name)
            self.generic_visit(n)

    for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs:
        bound.add(a.arg)
    if fn.args.vararg:
        bound.add(fn.args.vararg.arg)
    if fn.args.kwarg:
        bound.add(fn.args.kwarg.arg)
    for stmt in fn.body:
        V().visit(stmt)
    return sorted({n for n in loaded if n not in bound})


def _module_bound_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
    return names


def _construction_kwargs(pipeline_tree: ast.Module, cls_name: str) -> set[str] | None:
    """Find `_X_DEPS = ClsName(...)` in pipeline.py and return the provided kwarg names."""
    for node in ast.walk(pipeline_tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == cls_name:
            return {kw.arg for kw in node.keywords if kw.arg is not None}
    return None


def main() -> int:
    failures: list[str] = []
    pipeline_tree = ast.parse(PIPELINE.read_text(encoding="utf-8"))

    task_files = sorted(p for p in TASKS_DIR.glob("*.py") if p.name != "__init__.py")
    checked = 0
    for path in task_files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        deps = _deps_classes(tree)
        if not deps:
            continue
        module_names = _module_bound_names(tree)
        for cls_name, fields in deps.items():
            checked += 1
            field_set = set(fields)

            # The task fn(s) that take a `deps` parameter.
            deps_fns = [
                n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef)
                and any(a.arg == "deps" for a in n.args.posonlyargs + n.args.args + n.args.kwonlyargs)
            ]
            consumed: set[str] = set()
            for fn in deps_fns:
                consumed |= _deps_reads(fn)

            dead = field_set - consumed
            undeclared = consumed - field_set
            if dead:
                failures.append(f"{path.name}:{cls_name}: declared-but-never-read fields: {sorted(dead)}")
            if undeclared:
                failures.append(f"{path.name}:{cls_name}: deps.<x> read but NOT declared (AttributeError risk): {sorted(undeclared)}")

            provided = _construction_kwargs(pipeline_tree, cls_name)
            if provided is None:
                failures.append(f"pipeline.py: no `{cls_name}(...)` construction site found")
            else:
                missing = field_set - provided
                extra = provided - field_set
                if missing:
                    failures.append(f"pipeline.py:{cls_name}(...): MISSING kwargs (field unset → TypeError): {sorted(missing)}")
                if extra:
                    failures.append(f"pipeline.py:{cls_name}(...): EXTRA kwargs not on the dataclass: {sorted(extra)}")

            # Free-name soundness of each deps-taking task fn body.
            for fn in deps_fns:
                unbound = _free_unbound(fn, module_names)
                if unbound:
                    failures.append(f"{path.name}:{fn.name}: unbound free names (NameError at runtime): {unbound}")

    if failures:
        print("EXTRACTION INVARIANT FAILURES:")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print(f"  OK  extraction invariants: {checked} *Deps dataclass(es) reconciled "
          f"(fields declared==provided==consumed, no unbound names)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
