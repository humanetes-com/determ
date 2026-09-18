"""TASK 0b — discovery pass producing coverage_strategy.md (LLM use case).

Extracted from pipeline.py. Inline regex (gen_tests filename match,
fence fallback) is replaced: the filename check goes through an injected predicate
(`is_gen_tests_file`, regex stays in pipeline.py) and the fence fallback uses the relocated
`strip_markdown_fences`. The source-condensing parser and other pipeline-coupled
collaborators are injected via :class:`Task0bDeps`. No import of pipeline.py.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...domain.llm_output import extract_files
from ...domain.text import strip_markdown_fences
from ...log import log
from ...retry import with_retry

# Pipeline scratch + build dirs that are NOT project source. rglob over project_dir
# otherwise pulls in `.skeleton`/`.skeleton-filled` (each has its own `src/` + nested
# `target/.../out/*.rs` build-script output) and bloats the discovery budget.
_NON_SOURCE_DIRS = frozenset(
    {".skeleton", ".skeleton-filled", ".best_tree", "target", ".git"})


def _iter_source_rs(project_dir: Path) -> Iterator[Path]:
    """Yield real source `*.rs` files under `project_dir`, pruning pipeline scratch
    and build directories (see `_NON_SOURCE_DIRS`). Used by both the hash loop and
    the content loop so neither sees phantom files."""
    for p in project_dir.rglob("*.rs"):
        try:
            parts = p.relative_to(project_dir).parts
        except ValueError:
            continue
        if any(part in _NON_SOURCE_DIRS for part in parts):
            continue
        yield p


@dataclass(frozen=True, slots=True)
class Task0bDeps:
    """Collaborators TASK 0b needs, injected by the composition root."""

    ask: Callable[..., str]
    write: Callable[[Any, str], None]
    is_test_file: Callable[[Path, Path], bool]
    is_gen_tests_file: Callable[[str], bool]      # wraps the gen_tests filename regex
    strip_gen_tests_decls: Callable[[str], str]
    condense_rust_to_signatures: Callable[..., str]
    manifest_name: str
    sibling_cap: int
    single_call_char_limit: int
    task0_model: str


def task0b_discovery(strategy_path: Path, project_dir: Path, test_files: Any,
                     readme_text: str, profile_content: str,
                     deps: Task0bDeps,
                     clusters: list[Any] | None = None,
                     force: bool = False) -> str:
    """
    BL-001: Discovery pass — produce coverage_strategy.md before TASK 0c.

    Feeds the LLM the entire project source tree (src/**/*.rs, tests/**/*.rs, Cargo.toml)
    so it can document:
      - Correct `use` import paths for this crate's own types
      - How each public type is constructed (fields, variants, builders)
      - cfg/feature flags active in the project
      - Test style conventions already present in the test suite

    Cached with a hash of all source content. Returns strategy text (empty on failure).
    """
    cargo_toml_path = project_dir / "Cargo.toml"
    cargo_text = ""
    if cargo_toml_path.exists():
        with contextlib.suppress(Exception):
            cargo_text = cargo_toml_path.read_text(encoding="utf-8")

    # Load manifest-tracked generated files so they can be excluded from the hash.
    # On restart mid-run the pipeline may have stripped or rewritten source files;
    # including them would produce a different hash and force an unnecessary re-run.
    _generated: set[str] = set()
    for _mf in [project_dir / deps.manifest_name, *project_dir.glob(".generated_files_*.txt")]:
        try:
            if _mf.exists():
                _generated.update(ln.strip() for ln in _mf.read_text().splitlines() if ln.strip())
        except Exception:
            pass

    src_texts: list[str] = []
    for src_file in sorted(_iter_source_rs(project_dir)):
        if deps.is_test_file(src_file, project_dir):
            continue
        # Skip gen_tests sibling files entirely — they are pipeline-generated content
        # and including them would invalidate the hash on every pass.
        if deps.is_gen_tests_file(src_file.name):
            continue
        try:
            rel = str(src_file.relative_to(project_dir))
            if rel in _generated:
                continue
            text = src_file.read_text(encoding="utf-8")
            # Strip path-mod declarations (which point at sibling files) so the hash
            # remains stable across pipeline passes.
            src_texts.append(deps.strip_gen_tests_decls(text))
        except Exception:
            pass

    # Skip is governed by pipeline_state.md ("task0b" phase) — the outer caller
    # already checks _phase_valid before invoking task0b_discovery. Source/tests
    # changes (e.g. task0c augmentation) do not invalidate the strategy doc.

    log("\nTASK 0b (discovery): Analyzing project for coverage strategy...")

    # BL-010: condense source files to signatures only — function bodies are irrelevant
    # for TASK 0b which only needs import patterns, type shapes, and cfg flags.
    #
    # Budget: measure fixed overhead first (boilerplate instruction text + Cargo.toml +
    # README + clusters), then divide the remaining budget 55/45 between src and tests.
    # Old approach (fixed 45%/40% of SINGLE_CALL) didn't account for large README/Cargo.toml
    # and triggered the context guard which drops chars from the middle incoherently.
    _BOILERPLATE_ESTIMATE = 2_500   # static instruction text + FILE markers + headings

    # Cap cargo.toml and README so they don't eat the whole budget.
    _cargo_cap   = min(len(cargo_text), 3_000)
    _readme_cap  = min(len(readme_text), 2_000)

    clusters_section = ""
    if clusters:
        _cluster_lines = ["\nCompiler clusters (each is a separate test pass with its own env/cfg):"]
        for c in clusters:
            env_desc = ", ".join(f"{k}={v}" for k, v in c.env_extra.items()) if c.env_extra else "default"
            active   = ", ".join(c.active_cfg)   if c.active_cfg   else "none"
            excluded = ", ".join(c.excluded_cfg) if c.excluded_cfg else "none"
            _cluster_lines.append(f"  {c.name}: env={env_desc}  active_cfg=[{active}]  excluded_cfg=[{excluded}]")
        clusters_section = "\n".join(_cluster_lines) + "\n"

    _fixed_overhead = _BOILERPLATE_ESTIMATE + _cargo_cap + _readme_cap + len(clusters_section)
    _content_budget = max(2_000, deps.single_call_char_limit - _fixed_overhead)
    src_budget  = int(_content_budget * 0.55)
    test_budget_alloc = int(_content_budget * 0.45)

    src_section_parts: list[str] = []
    src_dropped: list[str] = []

    # Collect and condense all source files first, then sort by DESCENDING complexity
    # (unsafe/cfg count) so the hardest-to-test files are included first. Alphabetical
    # order was dropping error.rs, ptr.rs, wrapper.rs — the most important files.
    # _iter_source_rs prunes target/ + pipeline scratch dirs (.skeleton, .skeleton-filled)
    # which otherwise contribute build-script output + skeleton copies.
    src_candidates: list[tuple[int, Path, str, str]] = []  # (priority, path, rel, condensed)
    for src_file in _iter_source_rs(project_dir):
        if deps.is_test_file(src_file, project_dir):
            continue
        try:
            raw = src_file.read_text(encoding="utf-8")
            rel = str(src_file.relative_to(project_dir))
            condensed = deps.condense_rust_to_signatures(raw, filename=src_file.name)
            # Cap per-file contribution — macro-heavy files (ensure.rs, error.rs) produce
            # 35-50k condensed despite stripping, consuming the entire budget alone.
            if len(condensed) > deps.sibling_cap:
                _cut = condensed.rfind("\n", 0, deps.sibling_cap)
                condensed = condensed[:_cut if _cut != -1 else deps.sibling_cap] + "\n// ... (signatures truncated)"
            # Priority = count of complexity markers (unsafe, #[cfg(, #[macro_export)
            priority = raw.count("unsafe") + raw.count("#[cfg(") + raw.count("#[macro_export")
            src_candidates.append((priority, src_file, rel, condensed))
        except Exception:
            pass
    src_candidates.sort(key=lambda x: x[0], reverse=True)  # most complex first

    for _, _sf, rel, condensed in src_candidates:
        part = f'\n<FILE path="{rel}">\n{condensed}\n</FILE>'
        if len(part) <= src_budget:
            src_section_parts.append(part)
            src_budget -= len(part)
        else:
            src_dropped.append(rel)  # BL-017: track dropped files

    # BL-017: warn if any source files were dropped from the discovery pass.
    if src_dropped:
        log(f"  [task0b] WARNING: {len(src_dropped)} source file(s) dropped (budget exhausted): "
            f"{', '.join(src_dropped)}")

    test_section_parts: list[str] = []
    _tb = test_budget_alloc
    for rel, content in test_files:
        part = f'\n<TESTFILE path="{rel}">\n{content}\n</TESTFILE>'
        if len(part) <= _tb:
            test_section_parts.append(part)
            _tb -= len(part)

    cargo_section  = f"\nCargo.toml:\n```toml\n{cargo_text[:_cargo_cap]}\n```\n" if cargo_text else ""
    readme_section = f"\nREADME.md:\n{readme_text[:_readme_cap]}\n" if readme_text.strip() else ""
    dropped_note   = (
        f"\nNOTE: The following source files were excluded due to context budget limits "
        f"— the strategy below may be incomplete for: {', '.join(src_dropped)}\n"
    ) if src_dropped else ""
    src_section  = "\nSource files (signatures and type definitions only):\n" + "".join(src_section_parts) if src_section_parts else ""
    test_section = "\nTest files:\n" + "".join(test_section_parts) if test_section_parts else ""

    prompt = f"""You are analyzing a software project to produce a compact coverage strategy reference.
This will be injected verbatim into every unit-test-generation prompt — token budget is scarce.
Output must be machine-readable compact notation, NOT prose paragraphs.
{cargo_section}{readme_section}{clusters_section}{dropped_note}{src_section}{test_section}
Produce coverage_strategy.md in this exact compact format (copy headings literally):

IMPORTS:
  # one use-statement per line, only what internal mod gen_tests {{ use super::*; }} actually needs
  # tests are INSIDE the crate — never use <crate_name>::... paths
  # example lines (replace with real observations):
  #   std_prelude: use std::{{format, string::String, vec::Vec, boxed::Box}};
  #   fmt: use std::fmt::{{self, Display, Debug}};
  NEVER: <list items that look importable but will fail, one per line with reason after #>

TYPES:
  # TypeName: construction_expression  # notes (no PartialEq / opaque / etc.)
  # enum variants on same line: EnumName: Variant1|Variant2(T)|Variant3{{field:T}}
  # builder pattern: TypeName::builder().field(x).build()

CFG:
  # feature=name: default=on|off  # which code it gates
  # cfg(target_os/arch/...): ... if relevant

CONVENTIONS:
  # fn_sig: #[test] fn name() or #[test] fn name() -> Result<(),E>
  # assert: assert_eq!, assert!(x), assert!(matches!(x, Pat))
  # error_helper: inline pattern if tests need a custom error type

PITFALLS:
  # item → reason_it_fails → correct_alternative  (one per line)

RULES:
  # Hard constraints observed from source/Cargo.toml (one per line, terse):
  # Only include rules you can verify from the files above. Format examples (do NOT copy):
  #   NEVER: SomeType  # private / sealed — not accessible from test module
  #   NEVER: some_cfg_feature  # cfg-gated, nightly-only on this project
  #   NEVER: some_macro  # #[macro_export], not importable from inside crate

Only include lines you can directly observe in the files. Omit sections with nothing to say.
Each line must be self-contained — no cross-references between sections.
No prose. No markdown headers beyond the section names above.
Keep total output under 5,000 characters. Stop after RULES — do not add more sections.

<FILE path="coverage_strategy.md">
content here
</FILE>
"""

    def attempt() -> str | bool:
        response = deps.ask(prompt, model=deps.task0_model)
        files = extract_files(response)
        if "coverage_strategy.md" not in files:
            stripped = strip_markdown_fences(response)
            if len(stripped) > 100:
                files = {"coverage_strategy.md": stripped}
            else:
                log("  WARNING: could not produce coverage_strategy.md.")
                return False
        content = files["coverage_strategy.md"].strip()
        # Deduplicate lines — model sometimes repeats NEVER/ALWAYS entries.
        seen, deduped = set(), []
        for line in content.splitlines():
            key = line.strip()
            if key and key in seen:
                continue
            seen.add(key)
            deduped.append(line)
        content = "\n".join(deduped)
        deps.write(strategy_path, content)
        log(f"  coverage_strategy.md written ({len(content):,} chars).")
        return content

    result = with_retry(attempt)
    if result is False:
        log("  WARNING: discovery pass failed — proceeding without coverage strategy.")
        return ""
    return str(result)
