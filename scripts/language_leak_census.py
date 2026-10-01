#!/usr/bin/env python3
"""Where the target language (Rust) and its toolchain are written into each layer.

WHY
    The pipeline has only ever reconstructed Rust crates, and the question
    "is Rust an infrastructure detail?" (2026-10-01) could not be answered from
    a grep: `grep -rl cargo pipeline_core/domain` counts docstrings and comments,
    which explain a decision and execute nothing. A first count that way read
    56 of 174 domain files naming cargo and 123 naming impl/cfg/repr/.rs — an
    upper bound with no way to say how much of it is code. This census reads
    only what RUNS: string and bytes literals that are not docstrings (regex
    sources, substring tests, prompt text, command lines), and imports of the
    `domain/rust_*` text scanners. It is the baseline of the language-seam plan
    and the number its ratchet holds.

USAGE
    python scripts/language_leak_census.py              # layer table + top files
    python scripts/language_leak_census.py --top 40     # longer per-family lists
    python scripts/language_leak_census.py --file pipeline_core/domain/verdict.py
                                                        # every matching literal of one file
    python scripts/language_leak_census.py --json       # machine-readable, for a ratchet

    No GPU, no vLLM, no cargo; reads the working tree only (about a second).
    Run from the repo root.

HOW TO INTERPRET THE OUTPUT
    Two marker families, counted per non-docstring literal:

    toolchain  the literal names a tool or the shape of its output: cargo,
               rustc, rustup, RUSTFLAGS, an `error[E0308]` code, libtest's
               `test result:` / `running N tests` / `panicked at`, trybuild,
               llvm-cov, Cargo.toml, a toolchain channel. This is the part
               that is infrastructure by nature: it belongs in adapters and
               reaches the inner layers only as typed values.
    syntax     the literal names a construct of the language's surface:
               `fn `, `impl `, `#[cfg`, `unimplemented!`, `&self`, `dyn `,
               `Box<`, `::`, `.rs`, `pub(crate)`. In `prompts` this is the
               output contract and the evidence shown to the model (expected
               there); in `domain`/`application` it is the pipeline reading or
               writing source text itself rather than through the extractor.

    Per layer the table prints:
      files          python files in the layer
      tool-files     files holding >= 1 toolchain literal
      tool-lits      toolchain literals in the layer
      syn-files      files holding >= 1 syntax literal
      syn-lits       syntax literals in the layer
      scan-importers files importing a `pipeline_core.domain.rust_*` module
                     (the text scanners), excluding those modules themselves

    Decision rule. `domain` + `ports` + `application` tool-lits is the number a
    seam must drive to zero: every one is a decision reading tool text where a
    typed fact should arrive. `domain` syn-lits outside the `rust_*` modules is
    the size of the language model that has no owner yet (the candidates for
    the language profile). `prompts` syn-lits is the prompt contract's
    language-specific text and moves only when prompts are parameterised.
    A census that goes UP after a change is the leak growing; the per-file
    lists say where.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter
from pathlib import Path

LAYERS = ("domain", "ports", "prompts", "application", "adapters", "llm", "monitor")

TOOLCHAIN = re.compile(
    r"\bcargo\b|\brustc\b|\brustup\b|RUSTFLAGS|RUSTDOCFLAGS|RUSTC_BOOTSTRAP"
    r"|error\[E|\bE0\d{3}\b|test result:|running \\?d?\+? ?tests?|panicked at"
    r"|\btrybuild\b|llvm-cov|llvm_profile|LLVM_PROFILE|Cargo\.toml|Cargo\.lock"
    r"|\+nightly|\bnightly\b|--no-fail-fast|could not compile|libtest"
    r"|AddressSanitizer|\bASAN\b|-Zsanitizer|Doc-tests",
)
SYNTAX = re.compile(
    r"\bfn\s|\bfn\b\\|\bimpl\b|#\[|#!\[|unimplemented!|todo!\(|\bunsafe\b|&self|&mut\b"
    r"|\bdyn\s|Box<|::|\.rs\b|\bpub\b|\bmod\s|\bstruct\s|\benum\s|\btrait\s"
    r"|\buse\s+\w+::|cfg\(|\brepr\(|\bderive\(|'static|\bmacro_rules\b|\bcrate\b",
)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def literals(path: Path) -> tuple[list[tuple[int, str]], list[str]]:
    """(line, text) of every non-docstring str/bytes literal, and imported module names."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    doc = _docstring_nodes(tree)
    lits: list[tuple[int, str]] = []
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and id(node) not in doc:
            if isinstance(node.value, str):
                lits.append((node.lineno, node.value))
            elif isinstance(node.value, bytes):
                lits.append((node.lineno, node.value.decode("utf-8", "replace")))
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
            imports.extend(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            imports.extend(a.name for a in node.names)
    return lits, imports


def census(root: Path) -> dict[str, dict[str, object]]:
    core = root / "pipeline_core"
    rows: dict[str, dict[str, object]] = {}
    groups: list[tuple[str, list[Path]]] = [
        (layer, sorted(p for p in (core / layer).rglob("*.py") if "__pycache__" not in p.parts))
        for layer in LAYERS if (core / layer).is_dir()
    ]
    groups.append(("core-root", sorted(core.glob("*.py"))))
    groups.append(("pipeline.py", [root / "pipeline.py"] if (root / "pipeline.py").exists() else []))
    for layer, files in groups:
        tool: Counter[str] = Counter()
        syn: Counter[str] = Counter()
        scan: list[str] = []
        for f in files:
            rel = str(f.relative_to(root))
            try:
                lits, imports = literals(f)
            except SyntaxError as exc:  # a file that does not parse is a finding, not a skip
                print(f"UNPARSED {rel}: {exc}", file=sys.stderr)
                continue
            t = sum(1 for _, s in lits if TOOLCHAIN.search(s))
            s_ = sum(1 for _, s in lits if SYNTAX.search(s))
            if t:
                tool[rel] = t
            if s_:
                syn[rel] = s_
            if not f.name.startswith("rust_") and any(
                    re.search(r"pipeline_core\.domain\.rust_\w+", m) for m in imports):
                scan.append(rel)
        rows[layer] = {
            "files": len(files), "tool": dict(tool), "syn": dict(syn), "scan_importers": scan,
        }
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--file")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    root = Path(__file__).resolve().parent.parent

    if args.file:
        lits, _ = literals(root / args.file)
        for line, text in sorted(lits):
            fam = "".join(c for c, rx in (("T", TOOLCHAIN), ("S", SYNTAX)) if rx.search(text))
            if fam:
                print(f"{args.file}:{line} [{fam}] {text[:140]!r}")
        return 0

    rows = census(root)
    if args.json:
        json.dump(rows, sys.stdout, indent=1, sort_keys=True)
        return 0

    print(f"{'layer':<12}{'files':>6}{'tool-files':>11}{'tool-lits':>10}"
          f"{'syn-files':>10}{'syn-lits':>9}{'scan-importers':>15}")
    for layer, r in rows.items():
        tool, syn = r["tool"], r["syn"]
        assert isinstance(tool, dict) and isinstance(syn, dict)
        scan = r["scan_importers"]
        assert isinstance(scan, list)
        print(f"{layer:<12}{r['files']:>6}{len(tool):>11}{sum(tool.values()):>10}"
              f"{len(syn):>10}{sum(syn.values()):>9}{len(scan):>15}")
    for fam in ("tool", "syn"):
        for layer in ("domain", "ports", "application"):
            counts = rows[layer][fam]
            assert isinstance(counts, dict)
            print(f"\n{fam} literals, {layer}, top {args.top}:")
            for rel, n in sorted(counts.items(), key=lambda kv: -kv[1])[:args.top]:
                print(f"  {n:>5}  {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
