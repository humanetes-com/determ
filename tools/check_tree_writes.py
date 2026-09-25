#!/usr/bin/env python3
"""Every write into a fill tree goes through `tree_txn` — enforced statically.

A `.skeleton-filled/<cluster>/src` file is written by two kinds of code. An
AUTHORED write (a model's body) may leave the tree compile-red, never
unparseable — the parse-gated splicers. A MECHANICAL write (a re-seed, a
revert, a propagated layout definition, a donor body, a rustc-suggested `use`)
must never leave the tree worse than it found it, and its undo must be a byte
restore of a captured pre-image, never a re-derivation over text it may have
corrupted. `pipeline_core/application/reconstruct/tree_txn.py` is the one
module that implements both contracts; this gate is what keeps a fifth ad-hoc
transaction, or a sixth bare `write_text`, from being added by someone who does
not know the rule exists. Run `2973-66e50ad9` (2026-09-04) is the instance: a
bare-write re-seed left `src/context.rs` unparseable and a name-keyed revert
could not undo it.

The rule, checkable: in the modules that touch a fill tree (listed below), a
`.write_text(` / `.write_bytes(` call is a breach unless it sits in the
transaction module or is named in ALLOWLIST with the reason it is not a
mechanical tree write. Adding a tree writer therefore means either routing it
through `TreeTxn` or adding an allowlist line that a reviewer can read.

Pure AST walk — no imports, no execution. Exit 1 on any breach. Run from
check.sh beside the other invariant gates.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "pipeline_core"

# Modules whose writes reach a fill tree (or sit beside code that does).
SCANNED: tuple[str, ...] = (
    "adapters/fn_reseed.py",
    "adapters/fn_revert.py",
    "adapters/layout_tree_splice.py",
    "application/retrigger_layout.py",
    "application/fix_loop.py",
    "application/generators/task2_generator.py",
)
SCANNED_DIRS: tuple[str, ...] = ("application/reconstruct",)

# The transaction module: the only place a bare write is the mechanism.
TRANSACTION_MODULES: frozenset[str] = frozenset({
    "application/reconstruct/tree_txn.py",
})

# (module, enclosing function) -> why this write is not a mechanical tree
# write. Two admissible reasons: it is not a tree file (a failures log, a
# JSON store, a staged scratch copy), or it is an authored write already
# parse-gated in place. A byte restore of a pre-image is NOT a reason any
# more: every such site (the fix loops' snapshots and salvage, the slice
# session, the peel, the task3 retry gate, the duplicate repair, the batch
# ratchet) captures through `tree_txn.capture` and restores through
# `tree_txn.restore`, so an inlined text round-trip — which folds `\r\n`
# and is not a byte restore — cannot be added back without a reviewable
# line here. Anything else routes through TreeTxn.
ALLOWLIST: dict[tuple[str, str], str] = {
    ("application/reconstruct/fill_one_fn.py", "_run"):
        "failures-dir text log, not a tree file",
    ("application/reconstruct/group_fill.py", "task2_fill_groups"):
        "legacy grouped route: skeleton seed copy (parseable by construction), "
        "failures-dir log, and an authored merge the tdd driver never runs",
    ("application/reconstruct/cleanup_residuals.py", "cleanup_residuals"):
        "legacy phase-d authored merge; skipped under the tdd driver",
    ("application/reconstruct/heal.py", "heal_file"):
        "last-resort restore of the baked skeleton file, parseable by construction",
    ("application/reconstruct/hollow_repair.py", "repair_hollow_bodies"):
        "the file did not parse before; the parse-gated candidate was refused "
        "above and an un-nested body cannot make an unparseable file worse",
    ("application/reconstruct/splice.py", "splice_fn_with_where"):
        "where-clause insert and its undo inside the parse-gated body splice",
    ("application/reconstruct/assert_probe_runner.py", "run_assert_probes"):
        "writes into a staged scratch copy, never the fill tree",
    ("application/reconstruct/source_view.py", "public_api_from_source"):
        "writes a scratch view crate, never the fill tree",
    ("application/reconstruct/test_grouping.py", "group_tests"):
        "JSON groups store, not a tree file",
    ("application/generators/task2_generator.py", "_save_cluster_evidence"):
        "JSON evidence store, not a tree file",
    ("application/generators/task2_generator.py", "_save_reinstall_streak"):
        "JSON streak store, not a tree file",
    ("application/generators/task2_generator.py", "_generate_one_cluster"):
        "skeleton seed copy of files that do not exist yet (parseable by "
        "construction) and the gen_tests oracle attach",
}


def _module_key(path: Path) -> str:
    return str(path.relative_to(PKG)).replace("\\", "/")


def _scan_files() -> list[Path]:
    files = [PKG / m for m in SCANNED]
    for d in SCANNED_DIRS:
        files.extend(sorted((PKG / d).glob("*.py")))
    return [f for f in files if f.exists()]


class _Visitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.stack: list[str] = []
        self.hits: list[tuple[int, str, str]] = []   # (line, func, attr)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Call(self, node: ast.Call) -> None:
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr in ("write_text", "write_bytes"):
            self.hits.append((node.lineno, self.stack[-1] if self.stack else "<module>", fn.attr))
        self.generic_visit(node)


def main() -> int:
    breaches: list[str] = []
    used: set[tuple[str, str]] = set()
    for path in _scan_files():
        key = _module_key(path)
        if key in TRANSACTION_MODULES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        v = _Visitor()
        v.visit(tree)
        for line, func, attr in v.hits:
            allow = ALLOWLIST.get((key, func))
            if allow is None:
                breaches.append(
                    f"{key}:{line}: `.{attr}(` in `{func}` is not routed through "
                    "tree_txn and has no allowlist reason")
            else:
                used.add((key, func))
    stale = sorted(k for k in ALLOWLIST if k not in used)
    if breaches:
        print("tree-write gate: BREACH")
        for b in breaches:
            print("  " + b)
        print("  fix: route the write through TreeTxn (application/reconstruct/"
              "tree_txn.py), or add a (module, function) allowlist entry with "
              "the reason it is not a mechanical tree write.")
        return 1
    if stale:
        print("tree-write gate: stale allowlist entries (no such write any more):")
        for k in stale:
            print(f"  {k[0]}::{k[1]}")
        return 1
    print(f"tree-write gate: ok ({len(used)} allowlisted site(s), "
          f"{len(_scan_files())} module(s) scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
