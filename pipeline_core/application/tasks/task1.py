"""TASK 1 — functional-requirements synthesis dispatcher (+ proc-macro path).

Extracted from pipeline.py (slice 5b, Option 1). `task1` is a thin dispatcher over a
Task1Synthesizer (built by an injected factory) with a special proc-macro path. The
Rust-source regex (derive/attribute prefilter + static fallback) stays in pipeline.py
behind injected helpers; only the incidental fence fallback is replaced with
`strip_markdown_fences`. No import of pipeline.py.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...domain.generation import Task1Request, coalesce_gen_tests_siblings
from ...domain.llm_output import extract_files
from ...domain.rust_idents import use_path_requirement_gaps
from ...domain.text import smart_truncate, strip_markdown_fences
from ...io import read as _io_read
from ...io import write
from ...log import log
from ...prompts.genesis_seed import GENESIS_SEED


@dataclass(frozen=True, slots=True)
class Task1Deps:
    """Collaborators TASK 1 needs, injected by the composition root."""

    load_api_surface: Callable[..., str]
    load_type_names: Callable[[Path, str], list[str]]
    read: Callable[[Any], str]
    ask: Callable[..., str]
    write_fr_preserving_notes: Callable[[Any, str], None]
    synthesizer_factory: Callable[[], Any]
    filter_proc_macro_test_files: Callable[[list[Any], str], list[Any]]  # keeps the derive/attr regex
    proc_macro_fallback_requirements: Callable[[list[Any], str], str]  # keeps the fallback regex
    format_api_surface_section: Callable[[str], str]
    file_format_instruction: str
    single_call_char_limit: int
    profile_cap: int
    task1_model: str


def _task1_proc_macro(project_dir: Path, genesis_path: Path, functional_req_path: Path,
                      test_files: Any, readme_text: str, project_profile: str, deps: Task1Deps, crate_name: str = "",
                      project_context: str = "", api_surface: str = "") -> bool:
    """
    Specialised TASK 1 for proc-macro crates.
    Instead of extracting runtime API contracts, extracts the proc-macro interface:
    - Which #[derive(...)] macros are provided
    - Which helper attributes (#[module(...)]) are used on fields/variants
    - What trait implementations the derives must generate
    """
    log("  [proc-macro mode] Extracting derive macro interface from test usage.")

    # Prefilter test files to those that use #[derive(...)] / the crate's own attribute.
    # The derive/attribute regex lives in pipeline.py (Option 1).
    relevant = deps.filter_proc_macro_test_files(test_files, crate_name)
    log(f"  [proc-macro mode] Using {len(relevant)} file(s) with derive/attribute usage.")

    existing_genesis = deps.read(genesis_path)
    genesis_section = (
        f"\n--- genesis.md ---\n{existing_genesis}\n"
        if existing_genesis.strip() else ""
    )
    effective_context = project_context or readme_text

    # Budget: render the prompt scaffold with sized sections empty for exact
    # overhead; remainder goes to tests.
    _effective_context_capped = effective_context[:min(len(effective_context), 2_000)]
    _pm_scaffold = f"""You are analysing test files to extract the interface of a Rust proc-macro crate.

A proc-macro crate provides #[derive(...)] macros and/or #[attribute] macros used in the
test files below. Your task is to produce functional_requirements.md describing ONLY what
this proc-macro crate must implement — not the runtime API of dependent crates.

{deps.file_format_instruction}
Your output MUST be the file `functional_requirements.md` wrapped in the FILE markers above.

Project context:
{_effective_context_capped}

Test files:


Produce functional_requirements.md with:
"""
    _fixed = len(_pm_scaffold) + len(genesis_section)
    _tests_budget = max(2_000, deps.single_call_char_limit - _fixed)
    _raw_tests = "\n\n".join(
        f'<TESTFILE path="{rel}">\n{content}\n</TESTFILE>'
        for rel, content in relevant
    )
    tests_section = smart_truncate(_raw_tests, total_limit=_tests_budget,
                                   head=_tests_budget // 3, tail=2 * _tests_budget // 3)
    effective_context = effective_context[:deps.profile_cap]

    api_section = deps.format_api_surface_section(api_surface)
    prompt = f"""You are analysing test files to extract the interface of a Rust proc-macro crate.

A proc-macro crate provides #[derive(...)] macros and/or #[attribute] macros used in the
test files below. Your task is to produce functional_requirements.md describing ONLY what
this proc-macro crate must implement — not the runtime API of dependent crates.

{deps.file_format_instruction}
Your output MUST be the file `functional_requirements.md` wrapped in the FILE markers above.
{genesis_section}{api_section}
Project context:
{effective_context}

Test files:
{tests_section}

Produce functional_requirements.md with:

1. **Derive macros provided** — list every `#[derive(MacroName)]` used in the tests
   that comes from this crate (not std derives like Clone, Debug, PartialEq).
   For each:
   - The derive name (e.g. `MyDerive`)
   - Which trait(s) it must implement on the annotated type
   - The signature of trait methods that tests call

2. **Helper attributes** — list every `#[crate_name(key = "value")]` attribute used
   on struct fields, enum variants, or items. For each:
   - The attribute path (e.g. `#[my_crate(option)]`, `#[my_crate(description = "...")]`)
   - Which field/item types it applies to
   - What it controls in the generated code

3. **proc-macro entry points** — for each derive/attribute macro:
   - The `#[proc_macro_derive(Name, attributes(...))]` registration
   - Input type: `TokenStream` (proc_macro::TokenStream)
   - Output type: `TokenStream`

4. **Generated trait implementations** — what `impl TraitName for T` blocks the macro
   must emit, and what methods those impls must contain, inferred from test assertions.

Do NOT include: runtime functions (from_env, from_file), stdlib types, or any API
that is NOT a proc-macro derive or attribute provided by this crate.
"""
    response = deps.ask(prompt, model=deps.task1_model)
    files = extract_files(response)
    req_content = files.get("functional_requirements.md", "").strip()
    if not req_content:
        # Model may have returned content without FILE markers — use raw response,
        # stripping any markdown fences.
        raw = strip_markdown_fences(response)
        if len(raw) > 200:
            req_content = raw
            log(f"  [proc-macro TASK 1] Using raw response as requirements ({len(raw)} chars).")
    if not req_content:
        log("  [proc-macro TASK 1] No output — writing static fallback requirements.")
        # Static fallback scans tests for derive/attribute usage (regex in pipeline.py).
        req_content = deps.proc_macro_fallback_requirements(relevant, crate_name)
    deps.write_fr_preserving_notes(functional_req_path, req_content)
    log(f"  [proc-macro TASK 1] functional_requirements written ({len(req_content)} chars).")
    return True


def task1(req: Task1Request, deps: Task1Deps) -> bool:
    project_dir = req.project_dir
    genesis_path = req.genesis_path
    functional_req_path = req.functional_req_path
    test_files = req.test_files
    readme_text = req.readme_text
    project_profile = req.project_profile
    crate_type = req.crate_type
    module_name = req.module_name
    project_context = req.project_context
    coverage_strategy = req.coverage_strategy
    log("\nTASK 1: Analyzing tests + README, building functional_requirements.md")

    # Pre-strip API surface snapshot (signatures + type defs of the original
    # source). Used as structural ground truth — TASK 1 derives behaviour from
    # the tests but must respect the shapes captured here.
    api_surface = deps.load_api_surface(project_dir, module_name) if module_name else ""
    if api_surface:
        log(f"  Loaded API surface: {len(api_surface):,} chars (pre-strip skeleton).")
    known_types = deps.load_type_names(project_dir, module_name) if module_name else []
    if known_types:
        log(f"  Loaded type manifest: {len(known_types)} type(s) (pre-strip, syn-extracted).")

    # Collapse task0c gen_tests_<N> augmentation siblings of each module into one
    # logical test unit — requirements are per-module, so one LLM call per physical
    # fragment is pure fan-out (anyhow: 140 fragments → ~33 units). On-disk siblings
    # are untouched; attribution still resolves them by their real paths.
    test_files = coalesce_gen_tests_siblings(test_files)

    existing_genesis = deps.read(genesis_path)
    genesis_exists = bool(existing_genesis.strip())
    if genesis_exists:
        log("  genesis.md exists — will use as-is; only producing functional_requirements.md.")
    else:
        # Seed the reconstruction methodology now (deterministic, debt #38) so
        # every downstream prompt carries it even when TASK 4 never runs.
        write(genesis_path, GENESIS_SEED)
        log("  No genesis.md yet — seeded it with the reconstruction methodology.")

    # For proc-macro crates with no own tests, extract proc-macro interface
    # (derive macros, helper attributes) from how they're used in the test suite.
    if crate_type == "proc-macro":
        return _task1_proc_macro(project_dir, genesis_path, functional_req_path,
                                 test_files, readme_text, project_profile, deps,
                                 crate_name=module_name, project_context=project_context,
                                 api_surface=api_surface)
    total_chars = sum(len(c) for _, c in test_files)
    log(f"  Total test content: {total_chars:,} chars across {len(test_files)} file(s).")

    synthesizer = deps.synthesizer_factory()
    log(f"  Using {synthesizer.__class__.__name__}.")
    ok = synthesizer.synthesize(
        project_dir, genesis_path, functional_req_path, test_files, readme_text,
        project_profile, project_context=project_context, coverage_strategy=coverage_strategy,
        api_surface=api_surface, known_types=known_types,
    )

    if not ok:
        return False
    if genesis_exists:
        log("  functional_requirements.md written (genesis.md unchanged).")
    else:
        log("  functional_requirements.md written; genesis.md seeded with the "
            "reconstruction methodology.")
    return True


def task1_static_audit(functional_req_path: Path, test_files: list[Any]) -> list[str]:
    """Parse every test file for `use` statements and check that each imported
    symbol path appears in functional_requirements.md. Returns the symbol
    paths absent from the requirements (callers log a warning and optionally
    re-run TASK 1 on the missing symbols). No LLM call — structured
    `use`-path scan, runs in milliseconds."""
    reqs = _io_read(functional_req_path)
    if not reqs.strip():
        return []

    found_count, gaps = use_path_requirement_gaps(reqs, test_files)

    log(f"\nTASK 1.5: {found_count} use-paths in tests, {len(gaps)} missing from requirements.")
    if gaps:
        for g in gaps[:10]:
            log(f"  missing: {g}")
        if len(gaps) > 10:
            log(f"  ... and {len(gaps) - 10} more")
    return gaps
