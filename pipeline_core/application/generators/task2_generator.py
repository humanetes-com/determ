"""TASK-2 source generators (P4) — Clean-Architecture port of the pipeline.py
`Task2Generator` family.

Each generator is a use-case that turns functional requirements + tests into
source files, depending only on narrow role ports (`ports.generator`): the LLM,
the workspace, the per-module context docs, telemetry. `pipeline.py` is the
composition root (the focused `adapters.task2_roles` adapters +
`_task2_generator_factory`); tests inject fakes. Prompt-section text and file extraction stay pure (built here / imported
from `domain`); the model call, file writes, and context reads cross ports.

Grown strangler-fig: generators move in here one slice at a time as their helper
surface is bound behind ports.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...application.author_layout import (
    LayoutComponentRecord,
    LayoutOutcome,
)
from ...application.reconcile_layout import (
    adopt_registry_definitions,
    plan_layout_reconciliation,
)
from ...application.reconstruct.fill_merge import backfill_stub_bodies
from ...application.reconstruct.fill_rules import is_unimplemented_body
from ...application.reconstruct.heal import HealDeps, heal_file
from ...application.reconstruct.hollow_repair import repair_hollow_bodies
from ...application.reconstruct.import_repair import repair_missing_imports
from ...domain.attribution import FailureFrontier
from ...domain.cfg import CfgSet
from ...domain.cluster_plan import fill_order
from ...domain.generation import (
    GenerationRequest,
    SkeletonConfig,
    Task2Config,
    parse_gen_tests_filename,
    strip_test_prefix,
    testfile_paths,
    topo_sort_files,
)
from ...domain.heal import RestorePoint
from ...domain.llm_output import extract_files
from ...domain.models import (
    BuildCluster,
    ClusterFillResult,
    FnLocator,
)
from ...domain.rust_fn_headers import locate_all_fns
from ...domain.rust_scan import item_names
from ...domain.test_results import parse_test_counts
from ...domain.text import (
    extract_code_content,
    smart_truncate,
    strip_list_marker,
    trim_to_line,
)
from ...ports.generator import (
    LLM,
    Cargo,
    Clusters,
    ContextDocs,
    FillEngine,
    GenTests,
    SourceScan,
    Telemetry,
    Workspace,
)
from ...retry import with_retry

# Bound on the pre-fill heal loop. One round repairs the one file rustc could
# name before its parse aborted, so N corrupted files need N rounds; the loop
# also exits as soon as a round leaves the culprit unchanged. The cap only stops
# a pathological restore-point cycle from spinning before the fill pass.
_PREPASS_HEAL_MAX_ROUNDS = 8

# Prompt fragment for proc-macro crates (syn 2.0, default features) — pure text,
# rendered into the StaticTask2Generator prompt when the workspace is a
# proc-macro target. Kept as a module constant (data, not logic).
PROC_MACRO_NOTE = (
    "\n⚠️  PROC-MACRO CRATE — mandatory rules (syn = \"2.0\", default features only):\n"
    "\n  IMPORTS — lib.rs must have these at the top:\n"
    "    use proc_macro::TokenStream;\n"
    "    use proc_macro2::{TokenStream as TokenStream2, Span};\n"
    "    use syn::{parse_macro_input, DeriveInput, Data, Fields, FieldsNamed, FieldsUnnamed,\n"
    "              Field, Ident, Type, Attribute, Meta, Expr, Lit, LitStr, LitInt,\n"
    "              Path, PathSegment, Generics, Error as SynError};\n"
    "    use quote::{quote, quote_spanned, ToTokens};\n"
    "    use syn::spanned::Spanned;\n"
    "\n  AVAILABLE syn 2.0 types (default features, no 'full' needed):\n"
    "    DeriveInput, Data, DataStruct, DataEnum, DataUnion\n"
    "    Fields, FieldsNamed, FieldsUnnamed, Field\n"
    "    Attribute, Meta, MetaList, MetaNameValue\n"
    "    Ident, Type, TypePath, Generics, GenericParam\n"
    "    Path, PathSegment, PathArguments, AngleBracketedGenericArguments\n"
    "    Expr, ExprLit, Lit, LitStr, LitInt, LitBool\n"
    "    syn::Error (for compile-time errors via .to_compile_error())\n"
    "    Span comes from proc_macro2::Span, NOT syn::Span\n"
    "\n  NOT AVAILABLE without features = [\"full\"]:\n"
    "    ItemEnum, ItemFn, ItemStruct, ItemImpl — DO NOT USE\n"
    "    PatIdent, Stmt, Block, Arm — DO NOT USE\n"
    "\n  STRUCTURE rules:\n"
    "  - Entry-point fn MUST be tagged `#[proc_macro_derive(TraitName)]` or `#[proc_macro_attribute]`.\n"
    "  - proc-macro crates can ONLY export #[proc_macro_derive]/#[proc_macro_attribute]/#[proc_macro] fns.\n"
    "  - ALL other functions and types MUST be private (no `pub`). Example:\n"
    "      WRONG: `pub fn helper(...)` or `pub struct Foo` — these cause 'cannot export' compile error\n"
    "      RIGHT: `fn helper(...)` or `struct Foo` — private, no pub keyword\n"
    "  - Use `std::env` not bare `env::` for environment variables.\n"
    "  - A proc-macro crate does NOT define runtime types from the crates it serves —\n"
    "    those types live in the runtime crate(s) and are referenced by full path\n"
    "    inside `quote! { ... }` blocks (e.g. `runtime_crate::SomeType`).\n"
    "  - Parse input as `DeriveInput` (not `ItemStruct`/`ItemEnum`/`ItemFn` — those require 'full' feature):\n"
    "      let input = parse_macro_input!(input as DeriveInput);\n"
    "      match input.data { Data::Struct(s) => ..., Data::Enum(e) => ..., _ => ... }\n"
    "  - `syn::AttributeArgs` does not exist in syn 2.x — parse attributes via `Attribute::parse_nested_meta`.\n"
    "  - Iterating FieldsNamed: use `fields.named.iter()` not `fields.iter()`.\n"
    "    FieldsNamed has a `.named` field (Punctuated<Field, Comma>), not a direct iterator.\n"
    "  - Do NOT declare `extern crate proc_macro;` — it is implicit in proc-macro crates.\n"
    "  - Structs are data only — NO function signatures inside struct bodies.\n"
    "  - References in struct fields MUST have lifetimes: `struct Foo<'a> { x: &'a Bar }`\n"
    "  - Do NOT use inline `mod xyz { }` blocks — put helpers directly in lib.rs or in a separate file.\n"
    "  - If a submodule file `foo.rs` is generated, declare it in lib.rs as `mod foo;` (no braces).\n"
    "  - Every `mod foo;` declaration needs a corresponding foo.rs file.\n"
)


def _read_text(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


class Task2Generator(ABC):
    """Generate source files from functional requirements (TASK 2)."""

    @staticmethod
    def _common_sections(
        project_profile: str,
        workspace_structure: str,
        build_error: str,
        locked_dep_context: str,
    ) -> tuple[str, str, str, str]:
        """Build the four prompt sections shared by all Task2 generators."""
        profile = (
            f"\nProject profile (language, build system, workspace layout):\n{project_profile}\n"
        ) if project_profile.strip() else ""
        workspace = (
            f"\nWorkspace structure (MUST follow exactly — "
            f"every required entry-point file listed below must be generated):\n{workspace_structure}\n"
        ) if workspace_structure.strip() else ""
        build_err = (
            f"\n⚠️  PREVIOUS COMPILE ATTEMPT FAILED. Fix ALL errors below before regenerating.\n"
            f"Do not reuse the previous code. Generate corrected files that address every error.\n\n"
            f"Compiler output:\n{build_error}\n"
        ) if build_error.strip() else ""
        locked_dep = f"\n{locked_dep_context}\n" if locked_dep_context.strip() else ""
        return profile, workspace, build_err, locked_dep

    @abstractmethod
    def generate(self, req: GenerationRequest) -> bool:
        """Write source files to disk. Returns True on success.

        `req` bundles every generation input (see `domain.generation.GenerationRequest`).
        `req.api_surface` (when provided) is the pre-strip public-API skeleton: TASK 1
        already received it; passing it here too prevents loss-in-translation if TASK 1's
        requirements doc dropped any signature — TASK 2 sees the ground truth directly.
        """


@dataclass(frozen=True, slots=True)
class StaticTask2Generator(Task2Generator):
    """One LLM call per attempt that produces ALL source files in a single
    response using `<FILE path="...">` markers."""
    tel: Telemetry
    llm: LLM
    ws: Workspace
    docs: ContextDocs
    cfg: Task2Config

    def generate(self, req: GenerationRequest) -> bool:
        (project_dir, genesis_path, functional_req_path, manifest_path,
         existing_test_files) = (req.project_dir, req.genesis_path,
                                 req.functional_req_path, req.manifest_path,
                                 req.existing_test_files)
        (project_profile, workspace_structure, build_error, locked_dep_context) = (
            req.project_profile, req.workspace_structure, req.build_error,
            req.locked_dep_context)
        (current_module, module_dir_rel, whole_project_context, api_surface,
         prev_source_context) = (
            req.current_module, req.module_dir_rel, req.whole_project_context,
            req.api_surface, req.prev_source_context)
        self.tel.log("\nTASK 2: Implementing source code from functional requirements")
        genesis = self.ws.read(genesis_path)
        functional_req = self.ws.read(functional_req_path)

        profile_section, workspace_section, build_error_section, locked_dep_section = \
            self._common_sections(project_profile, workspace_structure, build_error, locked_dep_context)

        downstream_api_static = (
            self.docs.scan_downstream_api(project_dir, current_module) if current_module else "")

        proc_macro_note = ""
        if "proc-macro target" in workspace_structure and current_module:
            proc_macro_note = PROC_MACRO_NOTE

        module_scope_section = (
            f"\n⚠️  MODULE SCOPE: Generate source files ONLY for the `{current_module}` module.\n"
            f"Only output files whose path starts with `{module_dir_rel}`.\n"
            f"Do NOT generate files for any other module in the workspace.\n"
            f"{downstream_api_static}"
            f"{proc_macro_note}"
        ) if current_module else ""

        # BL-025: read known compile pitfalls for this module.
        pitfalls_section = ""
        if current_module:
            pitfalls = self.docs.compile_patterns_read(project_dir, current_module)
            if pitfalls:
                pitfalls_section = f"\n{pitfalls}\n"

        # BL-023: inject test assertions as ground truth.
        assertions_section = ""
        if existing_test_files:
            test_file_list = [(rel, (project_dir / rel).read_text(encoding="utf-8"))
                              for rel in existing_test_files
                              if (project_dir / rel).exists()]
            assertions = self.docs.extract_test_assertions(test_file_list, module_dir_rel)
            if assertions:
                assertions_section = f"\n{assertions}\n"

        # BL-022/BL-024: read structured rules relevant to this module.
        structured_rules = self.docs.genesis_rules_read(project_dir, module_scope=current_module or "*")
        rules_section = f"\n{structured_rules}\n" if structured_rules else ""

        api_section = self.docs.format_api_surface_section(api_surface)
        prev_source_section = (
            f"\nPrevious attempt's generated code (failed the tests — improve on this, do not repeat its mistakes):\n"
            f"{prev_source_context}\n"
        ) if prev_source_context.strip() else ""

        def attempt() -> bool:
            # Cap genesis and whole_project_context to keep the prompt within the
            # input budget. Without these caps a large genesis or huge whole-
            # project context could push the prompt past the limit and silently
            # truncate other sections.
            _genesis_capped = trim_to_line(genesis, self.cfg.genesis_cap, "\n... (genesis truncated)")
            # Render scaffold with sized sections empty for exact overhead.
            _mono_scaffold = f"""You are implementing a software project from functional requirements.
{self.cfg.thinking_directive}
Process (genesis.md):

{profile_section}{workspace_section}{module_scope_section}{locked_dep_section}{build_error_section}{pitfalls_section}{rules_section}{api_section}{prev_source_section}
Functional requirements:

{assertions_section}
Produce ALL source files needed to satisfy these requirements.
Every module/crate/package in the workspace structure above MUST have its required entry-point
file (e.g. each Rust crate declaring a lib target needs src/lib.rs; a bin target needs src/main.rs).
The TESTFILE blocks above are READ-ONLY spec; do NOT regenerate any test files.

{self.cfg.file_format_instruction}
{self.cfg.verbosity_guard}Output ALL files one after another using only those markers.
"""
            _mono_overhead = len(_mono_scaffold) + len(_genesis_capped) + len(functional_req)
            _wpc_budget = max(0, self.cfg.single_call_char_limit - _mono_overhead)
            _whole_project_capped = smart_truncate(whole_project_context, total_limit=_wpc_budget,
                                                   head=_wpc_budget // 4, tail=3 * _wpc_budget // 4) \
                                    if whole_project_context else ""
            prompt = f"""You are implementing a software project from functional requirements.
{self.cfg.thinking_directive}
Process (genesis.md):
{_genesis_capped}
{profile_section}{workspace_section}{module_scope_section}{locked_dep_section}{build_error_section}{pitfalls_section}{rules_section}{api_section}{prev_source_section}
Functional requirements:
{functional_req}
{assertions_section}{_whole_project_capped}
Produce ALL source files needed to satisfy these requirements.
Every module/crate/package in the workspace structure above MUST have its required entry-point
file (e.g. each Rust crate declaring a lib target needs src/lib.rs; a bin target needs src/main.rs).
The TESTFILE blocks above are READ-ONLY spec; do NOT regenerate any test files.

{self.cfg.file_format_instruction}
{self.cfg.verbosity_guard}Output ALL files one after another using only those markers.
"""
            response = self.llm.ask(prompt, model=self.cfg.task2_model)
            files = extract_files(response)
            if not files:
                self.tel.log(f"  ERROR: {self.cfg.model_label} returned no source files.")
                self.tel.log(f"  Raw response snippet: {response[:500]}")
                return False
            # Hard filter: discard any files outside the current module's directory.
            if module_dir_rel:
                outside = [p for p in files if not p.startswith(module_dir_rel)]
                if outside:
                    self.tel.log(f"  DISCARDED (outside module scope): {', '.join(outside)}")
                files = {p: c for p, c in files.items() if p.startswith(module_dir_rel)}
                if not files:
                    self.tel.log(f"  ERROR: no files in scope after filtering to `{module_dir_rel}`.")
                    return False
            written = self.ws.write_source_files(files, project_dir, manifest_path, existing_test_files)
            self.tel.log(f"  {written} source file(s) written.")
            return True

        return with_retry(attempt)


@dataclass(frozen=True, slots=True)
class LLMTask2Generator(Task2Generator):
    """File-by-file implementation: first asks the model which files to create,
    then generates each in a separate focused call (better for large workspaces
    where one call can't fit every file)."""
    tel: Telemetry
    llm: LLM
    ws: Workspace
    docs: ContextDocs
    cfg: Task2Config

    def generate(self, req: GenerationRequest) -> bool:
        (project_dir, genesis_path, functional_req_path, manifest_path,
         existing_test_files) = (req.project_dir, req.genesis_path,
                                 req.functional_req_path, req.manifest_path,
                                 req.existing_test_files)
        (project_profile, workspace_structure, build_error, locked_dep_context) = (
            req.project_profile, req.workspace_structure, req.build_error,
            req.locked_dep_context)
        (current_module, module_dir_rel, whole_project_context, api_surface) = (
            req.current_module, req.module_dir_rel, req.whole_project_context,
            req.api_surface)
        self.tel.log("\nTASK 2: Implementing source code from functional requirements (file-by-file)")
        genesis = self.ws.read(genesis_path)
        functional_req = self.ws.read(functional_req_path)

        profile_section, workspace_section, build_error_section, locked_dep_section = \
            self._common_sections(project_profile, workspace_structure, build_error, locked_dep_context)

        # Determine if current_module is a lib crate (has lib.rs entry point)
        lib_rs_first_note = ""
        if current_module and module_dir_rel:
            lib_rs_path = f"{module_dir_rel}src/lib.rs"
            lib_rs_first_note = (
                f"\n⚠️  FIRST FILE REQUIRED: The FIRST file you list MUST be `{lib_rs_path}`. "
                f"This is the crate root for `{current_module}`. All other modules are declared "
                f"from lib.rs with `pub mod` statements. Without lib.rs the crate cannot compile.\n"
            )

        downstream_api_section = ""
        if current_module:
            downstream_api_section = self.docs.scan_downstream_api(project_dir, current_module)

        rust_module_rules = ""
        if current_module and module_dir_rel:
            rust_module_rules = (
                "\n⚠️  RUST MODULE FILE RULES:\n"
                "1. A `foo.rs` file IS the module `foo`. Write its contents directly — do NOT wrap "
                "them in `pub mod foo { ... }`. That creates double nesting and won't compile.\n"
                "2. Types declared in lib.rs at the crate root are referenced from submodules as "
                "`use crate::TypeName;`. Submodules MUST NOT redefine root-level types.\n"
                "3. Submodule types declared at the root must have real field/variant definitions "
                "in lib.rs — not empty stubs.\n"
            )

        module_scope_section = (
            f"\n⚠️  MODULE SCOPE: Generate files ONLY for the `{current_module}` crate.\n"
            f"This is a Cargo workspace. Every file path MUST start with `{module_dir_rel}`.\n"
            f"CORRECT: `{module_dir_rel}src/lib.rs`, `{module_dir_rel}src/parse_attrs.rs`\n"
            f"WRONG:   `src/lib.rs`, `src/{current_module}/lib.rs`, `{current_module}/lib.rs`\n"
            f"Any file not starting with `{module_dir_rel}` will be discarded.\n"
            f"{lib_rs_first_note}"
            f"{downstream_api_section}"
            f"{rust_module_rules}"
        ) if current_module else ""

        # BL-025: read known compile pitfalls for this module.
        pitfalls_section_llm = ""
        if current_module:
            pitfalls_llm = self.docs.compile_patterns_read(project_dir, current_module)
            if pitfalls_llm:
                pitfalls_section_llm = f"\n{pitfalls_llm}\n"

        # BL-023: inject test assertions as ground truth.
        assertions_section_llm = ""
        if existing_test_files:
            test_file_list_llm = [(rel, (project_dir / rel).read_text(encoding="utf-8"))
                                  for rel in existing_test_files
                                  if (project_dir / rel).exists()]
            assertions_llm = self.docs.extract_test_assertions(test_file_list_llm, module_dir_rel)
            if assertions_llm:
                assertions_section_llm = f"\n{assertions_llm}\n"

        # BL-024: structured rules from knowledge store.
        structured_rules_llm = self.docs.genesis_rules_read(project_dir, module_scope=current_module or "*")
        rules_section_llm = f"\n{structured_rules_llm}\n" if structured_rules_llm else ""

        api_section = self.docs.format_api_surface_section(api_surface)
        # Fill-down: measure all fixed sections, give remainder to functional_req then whole_ctx.
        _genesis_truncated_plan = trim_to_line(genesis, self.cfg.genesis_cap, "\n... (genesis truncated)")
        # Render the plan prompt scaffold with measured sections empty to get an
        # exact overhead (replaces hardcoded estimate).
        _plan_scaffold = f"""You are planning the implementation of a software project.

Process (genesis.md):

{""}{""}{""}{""}{""}{""}{""}{""}{""}
Functional requirements:

{""}{""}
List ALL source files that must be created to implement these requirements.
Do NOT include: test files, config files (Cargo.toml, package.json, build.gradle, etc.), lock files, or any non-source file.
Only source code files (.rs, .py, .js, .ts, .go, .java, etc.).
Output one file path per line, relative to the project root. No other text.
"""
        _plan_fixed_size = (
            len(_plan_scaffold)
            + len(_genesis_truncated_plan) + len(profile_section) + len(workspace_section) +
            len(module_scope_section) + len(locked_dep_section) + len(build_error_section) +
            len(pitfalls_section_llm) + len(rules_section_llm) + len(api_section) +
            len(assertions_section_llm)
        )
        _plan_remaining = max(10_000, self.cfg.single_call_char_limit - _plan_fixed_size)
        _req_cap = _plan_remaining * 2 // 3
        _ctx_cap = _plan_remaining * 1 // 3
        _functional_req_capped = smart_truncate(functional_req, total_limit=_req_cap,
                                                head=_req_cap // 3, tail=2 * _req_cap // 3)
        _whole_ctx_capped = smart_truncate(whole_project_context, total_limit=_ctx_cap,
                                           head=_ctx_cap // 4, tail=3 * _ctx_cap // 4)

        # Step 1: ask model for the list of files to create.
        plan_prompt = f"""You are planning the implementation of a software project.

Process (genesis.md):
{_genesis_truncated_plan}
{profile_section}{workspace_section}{module_scope_section}{locked_dep_section}{build_error_section}{pitfalls_section_llm}{rules_section_llm}{api_section}
Functional requirements:
{_functional_req_capped}
{assertions_section_llm}{_whole_ctx_capped}
List ALL source files that must be created to implement these requirements.
Do NOT include: test files, config files (Cargo.toml, package.json, build.gradle, etc.), lock files, or any non-source file.
Only source code files (.rs, .py, .js, .ts, .go, .java, etc.).
Output one file path per line, relative to the project root. No other text.
"""
        file_list_response = self.llm.ask(plan_prompt, model=self.cfg.task2_model)
        file_paths: list[str] = []
        project_name = project_dir.name  # e.g. "argh-master"
        for raw_line in file_list_response.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            # Strip list markers: "- ", "* ", "1. ", "2) ", etc. (R7: structured).
            line = strip_list_marker(line)
            # Strip inline backticks and trailing punctuation (commas, semicolons, etc.)
            line = line.strip('`').strip(',;')
            # Strip trailing prose annotations (everything after first whitespace)
            line = line.split()[0] if line.split() else line
            # Final strip of any remaining backticks/punctuation after split.
            # Also strip trailing colons: model often uses `path`: description format.
            line = line.strip('`').strip(',;:').strip('`')
            # Normalise absolute paths to relative: strip everything up to project root
            if line.startswith('/'):
                try:
                    line = str(Path(line).relative_to(project_dir))
                except ValueError:
                    # Absolute path not under project_dir — skip it
                    continue
            # Strip leading project dir name if model included it (e.g. "argh-master/argh/src/lib.rs")
            if line.startswith(project_name + '/'):
                line = line[len(project_name) + 1:]
            # Final safety: strip any remaining leading slashes or dots
            line = line.lstrip('./')
            # Only keep lines that look like file paths (contain a dot and at least one slash)
            if line and '/' in line and '.' in line:
                file_paths.append(line)
        if not file_paths:
            self.tel.log("  ERROR: model returned no file list.")
            return False

        # Hard filter: discard files outside current module scope
        if module_dir_rel:
            outside = [p for p in file_paths if not p.startswith(module_dir_rel)]
            if outside:
                self.tel.log(f"  DISCARDED (outside module scope): {', '.join(outside)}")
            file_paths = [p for p in file_paths if p.startswith(module_dir_rel)]
            if not file_paths:
                self.tel.log(f"  ERROR: no files in scope after filtering to `{module_dir_rel}`.")
                return False

        self.tel.log(f"  Planning: {len(file_paths)} file(s) to generate.")

        # Step 2: generate each file individually, passing already-generated files as
        # context so each file can see what the others define (cross-file type refs).
        all_files: dict[str, str] = {}
        for file_path in file_paths:
            # Multi-file roadmap so the LLM doesn't cram the whole crate into one file.
            _file_list_parts = [
                f"\nCrate `{current_module}` consists of {len(file_paths)} "
                f"source files, each generated in a separate call. "
                f"This call generates ONE file; the others have been or will be "
                f"generated by other calls. Trust they will exist — reference "
                f"items from pending files by their declared module path."
                f"\nFile list:"
            ]
            for _fp in file_paths:
                if _fp == file_path:
                    _marker, _status = "→", "THIS CALL"
                elif _fp in all_files:
                    _marker, _status = "✓", "already generated"
                else:
                    _marker, _status = " ", "pending"
                _file_list_parts.append(f"  [{_marker}] {_fp}   ({_status})")
            file_roadmap_section = "\n".join(_file_list_parts) + "\n"

            prev_files_section = ""
            if all_files:
                parts = ["\nAlready-generated files for this module (for context — do NOT regenerate them):"]
                for prev_path, prev_content in all_files.items():
                    parts.append(f'\n<FILE path="{prev_path}">\n{prev_content}\n</FILE>')
                prev_files_section = "\n".join(parts) + "\n"

            file_prompt = f"""You are implementing a single source file for a software project.
{self.cfg.thinking_directive}
Process (genesis.md):
{trim_to_line(genesis, self.cfg.genesis_cap, "\n... (genesis truncated)")}
{file_roadmap_section}{profile_section}{module_scope_section}{locked_dep_section}{api_section}
Functional requirements:
{_functional_req_capped}
{_whole_ctx_capped}{prev_files_section}
Generate the file: `{file_path}` (this call writes ONLY that file).

{self.cfg.file_format_instruction}
{self.cfg.verbosity_guard}"""
            _per_file_attempts = 2
            for _attempt in range(_per_file_attempts):
                response = self.llm.ask(file_prompt, model=self.cfg.task2_model)
                files = extract_files(response)
                if file_path in files:
                    all_files[file_path] = files[file_path]
                    break
                matched = next((v for k, v in files.items()
                                if Path(k).name == Path(file_path).name), None)
                if matched:
                    all_files[file_path] = matched
                    self.tel.log(f"  Matched '{file_path}' by filename.")
                    break
                bare = extract_code_content(response)
                if bare:
                    all_files[file_path] = bare
                    self.tel.log(f"  Extracted '{file_path}' via content strip (model ignored FILE markers).")
                    break
                preview = response.strip()[:200].replace('\n', '↵') if response else "(empty)"
                if _attempt < _per_file_attempts - 1:
                    self.tel.log(f"  Empty response for `{file_path}`, retrying ({_attempt + 2}/{_per_file_attempts})...")
                else:
                    self.tel.log(f"  WARNING: model did not return `{file_path}`. Response preview: {preview}")

        if not all_files:
            self.tel.log("  ERROR: no files were successfully generated.")
            return False

        written = self.ws.write_source_files(all_files, project_dir, manifest_path, existing_test_files)
        self.tel.log(f"  {written} source file(s) written.")
        return True


# Proc-macro prompt fragment specific to the file-by-file generators (syn 2.0).
_FILELIST_PROC_MACRO_NOTE = """
⚠️  PROC-MACRO CRATE (syn = "2.0", default features only):

IMPORTS for lib.rs:
  use proc_macro::TokenStream;
  use proc_macro2::{TokenStream as TokenStream2, Span};
  use syn::{parse_macro_input, DeriveInput, Data, DataStruct, DataEnum, Fields,
            FieldsNamed, FieldsUnnamed, Field, Ident, Type, Attribute, Meta,
            Expr, Lit, LitStr, LitInt, Path, Generics, Error as SynError};
  use syn::spanned::Spanned;
  use quote::{quote, quote_spanned, ToTokens};

AVAILABLE syn 2.0 types (default features, no 'full' needed):
  DeriveInput, Data, DataStruct, DataEnum, Fields, FieldsNamed, FieldsUnnamed, Field
  Attribute, Meta, MetaList, MetaNameValue, Ident, Type, Generics, Path, LitStr, LitInt

NOT AVAILABLE (require 'full' feature — DO NOT USE):
  ItemEnum, ItemFn, ItemStruct, ItemImpl, PatIdent, Stmt, AttributeArgs

RULES:
  - Do NOT declare `extern crate proc_macro;` — it is implicit.
  - ALL helpers must be private (no `pub`) except #[proc_macro_derive]/[proc_macro_attribute] fns.
  - Do NOT define types that belong to dependent crates — reference them via their crate path
    inside quote! blocks (e.g. `my_crate::SomeType`).
  - Entry points must be at the TOP LEVEL of lib.rs with the correct registration attribute.
  - References in struct fields MUST have lifetimes: `struct Foo<'a> { x: &'a Bar }`
  - Iterate FieldsNamed: `fields.named.iter()` (NOT `fields.iter()`)
  - `syn::AttributeArgs` removed in syn 2.x — parse attrs via `Attribute::parse_nested_meta`
  - Do NOT use inline `mod helpers { }` blocks — helper functions go directly in the file.
  - Each `mod foo;` in lib.rs needs a corresponding foo.rs file (already in file list).
  - Derive/attribute macro names and helper attribute paths are in the functional requirements.
"""


@dataclass(frozen=True, slots=True)
class StaticFileListTask2Generator(Task2Generator):
    """File-by-file generator using the original source file list saved before
    stripping — the file list is deterministic, not LLM-guessed. Generates each
    file with focused context. Falls back to `LLMTask2Generator` when no saved
    file list exists."""
    tel: Telemetry
    llm: LLM
    ws: Workspace
    docs: ContextDocs
    cfg: Task2Config

    def generate(self, req: GenerationRequest) -> bool:
        (project_dir, genesis_path, functional_req_path, manifest_path,
         existing_test_files) = (req.project_dir, req.genesis_path,
                                 req.functional_req_path, req.manifest_path,
                                 req.existing_test_files)
        (project_profile, workspace_structure, build_error, locked_dep_context) = (
            req.project_profile, req.workspace_structure, req.build_error,
            req.locked_dep_context)
        (current_module, module_dir_rel, whole_project_context, coverage_strategy,
         api_surface) = (
            req.current_module, req.module_dir_rel, req.whole_project_context,
            req.coverage_strategy, req.api_surface)
        self.tel.log("\nTASK 2: Implementing source code (static file list)")
        genesis = self.ws.read(genesis_path)
        functional_req = self.ws.read(functional_req_path)

        # Get file list from pre-strip snapshot — no LLM needed.
        file_paths = self.ws.load_original_structure(project_dir, current_module) if current_module else []

        if not file_paths:
            self.tel.log("  No saved file structure — falling back to LLM file-list generator.")
            # Pass the full request through — the VO migration intentionally drops the
            # old positional-call's accidental omission of api_surface/prev_source_context
            # (those default-"" only because the fallback predates those fields).
            return LLMTask2Generator(
                tel=self.tel, llm=self.llm, ws=self.ws, docs=self.docs, cfg=self.cfg,
            ).generate(req)

        # Filter to module scope and exclude pure test files.
        if module_dir_rel:
            file_paths = [p for p in file_paths if p.startswith(module_dir_rel)]
        file_paths = [p for p in file_paths if p not in existing_test_files]

        # FR-010: put entry point first; remaining order updated dynamically after each generation.
        cargo_toml = (project_dir / module_dir_rel / "Cargo.toml") if module_dir_rel \
                     else (project_dir / "Cargo.toml")
        entry_point = self.ws.crate_entry_point(cargo_toml, module_dir_rel, set(file_paths))
        if entry_point:
            file_paths = [entry_point] + [p for p in file_paths if p != entry_point]

        self.tel.log(f"  File list from original structure: {len(file_paths)} file(s): "
                     f"{', '.join(file_paths)}")

        if not file_paths:
            self.tel.log("  No non-test files in original structure — nothing to generate.")
            return False

        profile_section, workspace_section, build_error_section, locked_dep_section = \
            self._common_sections(project_profile, workspace_structure, build_error, locked_dep_context)

        # Detect proc-macro crate once — affects lib.rs prompt.
        _is_proc_macro = False
        if module_dir_rel:
            _cargo_toml_path = project_dir / module_dir_rel / "Cargo.toml"
            if _cargo_toml_path.exists():
                _cargo_text = _cargo_toml_path.read_text(encoding="utf-8")
                _is_proc_macro = self.ws.is_proc_macro_crate(_cargo_text)

        _genesis_section = (
            f"Process (genesis.md):\n"
            f"{trim_to_line(genesis, self.cfg.genesis_cap, chr(10) + '... (genesis truncated)')}\n"
        ) if genesis.strip() else ""

        # Pre-parse API surface into per-file sections once.
        _api_file_sections = self.docs.split_api_surface_by_file(api_surface) if api_surface else []

        all_files: dict[str, str] = {}
        pending = list(file_paths)

        while pending:
            file_path = pending.pop(0)
            # Multi-file roadmap so the LLM doesn't try to cram the whole crate
            # into the current file.
            _file_list_parts = [
                f"\nCrate `{current_module}` consists of {len(file_paths)} "
                f"source files, each generated in a separate call. "
                f"This call generates ONE file; the others have been or will be "
                f"generated by other calls. Trust they will exist — reference "
                f"items from pending files by their declared module path."
                f"\nFile list:"
            ]
            for _fp in file_paths:
                if _fp == file_path:
                    _marker = "→"
                    _status = "THIS CALL"
                elif _fp in all_files:
                    _marker = "✓"
                    _status = "already generated"
                else:
                    _marker = " "
                    _status = "pending"
                _file_list_parts.append(f"  [{_marker}] {_fp}   ({_status})")
            file_roadmap_section = "\n".join(_file_list_parts) + "\n"

            # Prev files: most recent N in full content, older files as API
            # signatures only so the prompt does not balloon as file count grows.
            prev_files_section = ""
            if all_files:
                _FULL_PREV_COUNT = 3  # most recent N files keep full body
                _prev_items = list(all_files.items())
                _full_items = _prev_items[-_FULL_PREV_COUNT:]
                _digest_items = _prev_items[:-_FULL_PREV_COUNT]
                parts = ["\nAlready-generated files for this module (do NOT regenerate):"]
                _api_by_path = dict(_api_file_sections)
                for prev_path, _prev_content in _digest_items:
                    _digest = _api_by_path.get(prev_path, "")
                    if _digest:
                        parts.append(
                            f'\n<FILE path="{prev_path}" digest="api-signatures-only">\n'
                            f'{_digest}\n</FILE>'
                        )
                    else:
                        parts.append(
                            f'\n<FILE path="{prev_path}" digest="omitted-for-budget">\n'
                            f'// (content elided — see other already-generated files for cross-references)\n'
                            f'</FILE>'
                        )
                for prev_path, prev_content in _full_items:
                    parts.append(f'\n<FILE path="{prev_path}">\n{prev_content}\n</FILE>')
                prev_files_section = "\n".join(parts) + "\n"

            proc_macro_note = _FILELIST_PROC_MACRO_NOTE if _is_proc_macro else ""

            # ── Per-file content: priority-ordered fill-down ──────────────────
            _file_stem = Path(file_path).stem  # e.g. "error" for "src/error.rs"
            _is_entry = _file_stem in ("lib", "main")

            # Split API surface sections.
            _primary_api = ""
            _secondary_api_parts: list[str] = []
            for _api_path, _api_sec in _api_file_sections:
                if _api_path == file_path or Path(_api_path).stem == _file_stem:
                    _primary_api = _api_sec
                else:
                    _secondary_api_parts.append(_api_sec)

            # Split test files. Primary = tests relevant to the current file (loose match).
            def _is_primary_test(rel: str, *, _is_entry: bool = _is_entry,
                                 _file_stem: str = _file_stem) -> bool:  # B023
                if _is_entry and rel.startswith("tests/"):
                    return True
                test_stem = Path(rel).name.split('.')[0]
                if not _file_stem or not test_stem:
                    return False
                norm_test = strip_test_prefix(test_stem)  # R7: structured prefix strip
                norm_file = _file_stem
                return (
                    norm_test == norm_file
                    or norm_file in norm_test
                    or norm_test in norm_file
                )

            _primary_tests: list[tuple[str, str]] = []
            _secondary_tests: list[tuple[str, str]] = []
            for _rel in sorted(existing_test_files):
                if module_dir_rel and not _rel.startswith(module_dir_rel):
                    continue
                _tp = project_dir / _rel
                if not _tp.exists():
                    continue
                _content = _tp.read_text(encoding="utf-8")
                if _is_primary_test(_rel):
                    _primary_tests.append((_rel, _content))
                else:
                    _secondary_tests.append((_rel, _content))

            # Build primary test section (always included in full; READ-ONLY spec).
            _primary_test_parts = [
                "\nTest files — primary (READ-ONLY spec your implementation must satisfy; do NOT output these files):"
            ]
            for _rel, _content in _primary_tests:
                _primary_test_parts.append(f'\n<TESTFILE path="{_rel}">\n{_content}\n</TESTFILE>')
            _primary_test_section = (
                "\n".join(_primary_test_parts) + "\n" if len(_primary_test_parts) > 1 else ""
            )

            # Strategy section (actual size, always included).
            strategy_section = (
                f"\nCoverage strategy (import patterns, type construction, test conventions):\n"
                f"{trim_to_line(coverage_strategy, self.cfg.strategy_cap, chr(10) + '... (strategy truncated)')}\n"
            ) if coverage_strategy.strip() else ""

            # Functional requirements — always include in full; only hard-truncate
            # if the doc alone would exceed the entire budget (pathological case).
            _scl = self.cfg.single_call_char_limit
            _f_req = smart_truncate(functional_req, total_limit=_scl // 2,
                                    head=_scl // 6, tail=_scl // 3)

            # Whole-project context — suppress when primary/secondary tests already
            # cover the same TESTFILE blocks (R7: structured marker scan).
            _wp_paths = set(testfile_paths(whole_project_context or ""))
            _have_paths = {p for p, _ in _primary_tests} | {p for p, _ in _secondary_tests}
            if _wp_paths and _wp_paths.issubset(_have_paths):
                whole_project_context = ""
            _w_ctx = smart_truncate(whole_project_context, total_limit=_scl // 4,
                                    head=_scl // 16, tail=3 * _scl // 16)

            # Compute scaffolding overhead by rendering the prompt with all measured
            # sections empty (captures static text + directives at actual size).
            _scaffold = f"""You are implementing a single source file for a Rust project.
{self.cfg.thinking_directive}{self.cfg.file_format_instruction}
{self.cfg.verbosity_guard}Output ONLY the file `{file_path}` using the markers above. Nothing else.

⚠️  MODULE SCOPE: This file belongs to the `{current_module}` crate.
    Path: `{file_path}`
{proc_macro_note}{locked_dep_section}{build_error_section}
Functional requirements:



Generate the implementation for `{file_path}` (this call writes ONLY that file).
"""
            _primary_size = (
                len(_scaffold)
                + len(file_roadmap_section)
                + len(_genesis_section)
                + len(profile_section)
                + len(workspace_section)
                + len(strategy_section)
                + len(_primary_api)
                + len(_primary_test_section)
                + len(_f_req)
                + len(_w_ctx)
                + len(prev_files_section)
            )
            _fill_budget = max(0, _scl - _primary_size)

            # Fill secondary API sections then secondary test files with remaining budget.
            _secondary_api_section = ""
            _secondary_test_section = ""
            _fill_used = 0
            _sec_api_parts: list[str] = []
            for _sec in _secondary_api_parts:
                if _fill_used + len(_sec) > _fill_budget:
                    break
                _sec_api_parts.append(_sec)
                _fill_used += len(_sec)
            if _sec_api_parts:
                _secondary_api_section = "\n".join(_sec_api_parts) + "\n"

            # Sort secondary tests by relevance to current file before fill:
            # score = #times the file's API-surface item names appear in the test.
            _symbol_set = set(item_names(_primary_api or ""))
            def _relevance_score(item: tuple[str, str], *,
                                 _symbol_set: set[str] = _symbol_set) -> tuple[int, str]:  # B023
                _, content = item
                if not _symbol_set:
                    return (0, item[0])
                hits = sum(content.count(s) for s in _symbol_set)
                return (-hits, item[0])  # negative for descending sort on hits
            _secondary_tests_sorted = sorted(_secondary_tests, key=_relevance_score)

            _sec_test_parts: list[str] = []
            if _secondary_tests_sorted and _fill_used < _fill_budget:
                _sec_test_parts.append(
                    "\nTest files — additional context (READ-ONLY; do NOT output):"
                )
                for _rel, _content in _secondary_tests_sorted:
                    if _fill_used + len(_content) > _fill_budget:
                        break
                    _sec_test_parts.append(
                        f'\n<TESTFILE path="{_rel}">\n{_content}\n</TESTFILE>'
                    )
                    _fill_used += len(_content)
            if len(_sec_test_parts) > 1:
                _secondary_test_section = "\n".join(_sec_test_parts) + "\n"

            # Assemble final API and test sections.
            api_section = (
                "\nAPI surface — pre-strip structural skeleton (signatures and type definitions):\n"
                + _primary_api + _secondary_api_section
            ) if (_primary_api or _secondary_api_section) else ""
            test_files_section = _primary_test_section + _secondary_test_section
            prompt = f"""You are implementing a single source file for a Rust project.
{self.cfg.thinking_directive}{self.cfg.file_format_instruction}
{self.cfg.verbosity_guard}Output ONLY the file `{file_path}` using the markers above. Nothing else.
{file_roadmap_section}{_genesis_section}{profile_section}{workspace_section}{strategy_section}{api_section}
⚠️  MODULE SCOPE: This file belongs to the `{current_module}` crate.
    Path: `{file_path}`
{proc_macro_note}{locked_dep_section}{build_error_section}
Functional requirements:
{_f_req}
{test_files_section}{_w_ctx}{prev_files_section}
Generate the implementation for `{file_path}` (this call writes ONLY that file).
"""
            # FR-012: per-file retry — qwen3 thinking mode sometimes returns empty output.
            _per_file_attempts = 2
            for _attempt in range(_per_file_attempts):
                response = self.llm.ask(prompt, model=self.cfg.task2_model)
                files = extract_files(response)
                if file_path in files:
                    all_files[file_path] = files[file_path]
                    break
                matched = next((v for k, v in files.items()
                                if Path(k).name == Path(file_path).name), None)
                if matched:
                    all_files[file_path] = matched
                    self.tel.log(f"  Matched '{file_path}' by filename.")
                    break
                bare = extract_code_content(response)
                if bare:
                    all_files[file_path] = bare
                    self.tel.log(f"  Extracted '{file_path}' via content strip (model ignored FILE markers).")
                    break
                preview = response.strip()[:200].replace('\n', '↵') if response else "(empty)"
                if _attempt < _per_file_attempts - 1:
                    self.tel.log(f"  Empty response for `{file_path}`, retrying ({_attempt + 2}/{_per_file_attempts})...")
                else:
                    self.tel.log(f"  WARNING: no output for `{file_path}`. Preview: {preview}")

            # FR-010: re-sort remaining files using mod declarations from just-generated file.
            if pending and file_path in all_files:
                with suppress(Exception):  # cycle or error — keep current order
                    pending = topo_sort_files(pending, all_files)

        if not all_files:
            self.tel.log("  ERROR: no files generated.")
            return False

        written = self.ws.write_source_files(all_files, project_dir, manifest_path, existing_test_files)
        self.tel.log(f"  {written} source file(s) written.")
        return True


@dataclass(frozen=True, slots=True)
class StaticSkeletonTask2Generator(Task2Generator):
    """4-phase reconstruction: mechanical skeleton → LLM grouping → per-group
    fill → cleanup.

    Phase A:  Load the skeleton (signatures + types + macros + `unimplemented!()`
              bodies) baked from live source before strip (`_bake_skeleton_artifacts`).
              No LLM. cargo check verifies the cached skeleton still compiles under
              this cluster's env.
    Phase B:  One LLM call to group test files into cohesive units. Validated.
    Phase C:  Per-group LLM call that fills `unimplemented!()` bodies the group's
              tests transitively need. Leaf-first ordering. Merge preserves bodies
              already filled by prior groups.
    Phase D:  Single cleanup pass for test-exercised stubs left untouched.

    Phase A and B results cached on disk; Phase C and D re-run per iteration.
    """
    tel: Telemetry
    cargo: Cargo
    fill: FillEngine
    clusters: Clusters
    gentests: GenTests
    scan: SourceScan
    ws: Workspace
    cfg: SkeletonConfig
    # P3 blueprint phase deps (BlueprintDeps, constructed by the composition
    # root; None → blueprint generation is skipped).
    blueprint_deps: Any = None
    # F2 heal: the syn StructureReader (parse-check gate for the heal hierarchy).
    # None → heal falls back to whole-file skeleton restore (legacy behaviour).
    structure: Any = None
    # Layout propagation: (load_private_surface, load_layout_records,
    # apply_layout_to_tree). The filled tree is seeded from the skeleton once
    # and owned thereafter, so a layout re-authored mid-run reaches the skeleton
    # only — these let the seed step reinstall it. None → skipped.
    layout_sync: Any = None
    # Crate-wide authored-layout registry loader (`project_dir -> {uid: item}`).
    # The layout store is per cluster; the crate is not. Optional — omitted, a
    # cluster's own record stands, which is the pre-registry behaviour.
    layout_registry: Any = None

    def _sync_layout_into_tree(self, tag: str, project_dir: Path, cluster: Any,
                               filled_dir: Path,
                               donor_clusters: Sequence[str] = ()) -> None:
        """Reinstall every AUTHORED layout definition the live tree is missing.
        Silent no-op on a healthy tree (nothing is superseded), so this reads a
        handful of files per cluster start and never rewrites a filled body.

        DONOR clusters' authored definitions count too, and that is not a
        convenience — it is what makes the cross-cluster body seed sound. Bodies
        are shared ("fill once, propagate") while the layout store is per
        cluster, so a component re-authored in one cluster leaves every later
        cluster inheriting bodies written against a shape its own tree does not
        declare. Live: the retrigger re-authored `ErrorImpl` in `stable` to
        `{vtable, backtrace, _error}`; `nightly` kept the baked
        `{vtable, _data}`, adopted 121 stable bodies, and opened on 31
        `E0560`/`E0609` that NO per-fn fill can repair, because the fix is a
        struct declaration and the fill loop only writes bodies. A layout a body
        was authored against travels with the body, exactly as its imports do.

        This cluster's OWN records win: donor plans are merged in only for
        declarations this cluster has not itself authored, so a cluster that
        re-authored a component keeps its own shape."""
        if self.layout_sync is None:
            return
        load_surface, load_records, apply_to_tree = self.layout_sync
        src = filled_dir / "src"
        read_live = lambda rel: _read_text(src / rel)   # noqa: E731
        try:
            surface = load_surface(project_dir, cluster.name)
            own_records = load_records(project_dir, cluster.name)
            # The crate-wide registry outranks this cluster's record. A cluster
            # that authored an item the crate had already fixed holds a SECOND
            # definition of it, and `own_uids` below would otherwise defend that
            # second definition against the donor as if it were a choice.
            reg_notes: list[str] = []
            if self.layout_registry is not None:
                own_records, reg_notes = adopt_registry_definitions(
                    own_records, self.layout_registry(project_dir))
            plan, notes = plan_layout_reconciliation(
                surface, own_records, read_live)
            notes = reg_notes + notes
            # Precedence is decided from the RECORDS, never from the plans. A
            # plan is empty when the tree ALREADY holds this cluster's authored
            # shape, so an empty own-plan means "mine is installed", not "I have
            # no opinion" — and skipping only what the own-plan happens to
            # contain lets a donor overwrite a shape this cluster authored and
            # already had. The next cluster start would then plan the own shape
            # back and the two would flip-flop, breaking the seeded bodies on
            # every other iteration.
            own_uids = {uid for rec in own_records
                        if rec.outcome is LayoutOutcome.AUTHORED
                        for uid, _def in rec.defs}
            for _donor in donor_clusters:
                _drecs = [
                    LayoutComponentRecord(
                        names=rec.names, outcome=rec.outcome,
                        defs=tuple((uid, d) for uid, d in rec.defs
                                   if uid not in own_uids))
                    for rec in load_records(project_dir, _donor)]
                _dplan, _dnotes = plan_layout_reconciliation(
                    surface, [r for r in _drecs if r.defs], read_live)
                for _file, _reps in _dplan.items():
                    plan.setdefault(_file, []).extend(_reps)
                notes.extend(f"{n} (from donor cluster '{_donor}')"
                             for n in _dnotes)
        except Exception as e:                      # never block the fill loop
            self.tel.log(f"  {tag} [layout-sync] skipped: {e}")
            return
        # Notes first, and unconditionally: they carry the anomalies (an item
        # that could not be located, a file that could not be read), and those
        # are exactly the cases that produce an EMPTY plan — logging them only
        # alongside work done hides every one of them.
        for n in notes:
            self.tel.log(f"  {tag} [layout-sync] {n}")
        if not plan:
            return
        written, err = apply_to_tree(plan, src)
        self.tel.log(
            f"  {tag} [layout-sync] reinstalled "
            f"{sum(len(v) for v in plan.values())} authored definition(s) into "
            f"{written} live file(s)" + (f"; ERROR: {err}" if err else "")
            + " — the tree was building a layout the store had superseded.")

    def _ensure_blueprint(self, project_dir: Path, module: str,
                          default_cluster: Any, all_test_pairs: list[tuple[str, str]],
                          functional_req: str, functional_req_path: Path) -> None:
        """P3: the model authors its own design doc from skeleton sigs +
        tests + FR, once per crate, before any fill. Topics = source-file
        stems (one section each) + Overview; fills consume slices of it."""
        from pipeline_core.application.reconstruct.source_view import (
            condense_rust_to_signatures,
        )
        from pipeline_core.application.tasks.blueprint_phase import ensure_blueprint
        from pipeline_core.domain.blueprint import blueprint_path
        skel_dir = project_dir / ".skeleton" / default_cluster.name
        sig_parts: list[str] = []
        topics: list[str] = []
        for f in sorted(skel_dir.rglob("*.rs")):
            rel = f.relative_to(skel_dir)
            text = f.read_text(encoding="utf-8", errors="replace")
            sig_parts.append(f"// {rel}\n"
                             + condense_rust_to_signatures(text, str(rel)))
            if f.stem != "mod" and not f.stem.startswith("gen_tests"):
                topics.append(f.stem)
        if not sig_parts:
            self.tel.log("  [blueprint] no baked skeleton files — skipped.")
            return
        cap = int(os.getenv("BLUEPRINT_INPUT_CAP", "60000"))
        tests_block = "\n\n".join(
            f"// {rel}\n{txt}" for rel, txt in all_test_pairs)
        ensure_blueprint(
            blueprint_path(functional_req_path),
            module=module,
            skeleton_sigs="\n\n".join(sig_parts)[:cap],
            tests_block=tests_block[:cap],
            functional_req=functional_req[:cap // 2],
            topics=tuple(dict.fromkeys(topics)),
            deps=self.blueprint_deps, skeleton_dir=skel_dir)

    def generate(self, req: GenerationRequest) -> bool:
        (project_dir, genesis_path, functional_req_path, manifest_path,
         existing_test_files) = (req.project_dir, req.genesis_path,
                                 req.functional_req_path, req.manifest_path,
                                 req.existing_test_files)
        current_module = req.current_module
        self.tel.log("\nTASK 2: Implementing source code (skeleton backend, per-cluster)")
        module = current_module or project_dir.name

        # Load clusters from the bake artefact written before strip (Hard Rule 11:
        # no source access post-strip). If absent, abort — there is no live source
        # to discover from at this point.
        clusters = self.clusters.load_baked(project_dir)
        if not clusters:
            self.tel.log("  [skeleton] FATAL: no baked clusters at "
                         f"{project_dir}/.skeleton/_clusters.json. The skeleton bake "
                         "step (pre-strip) must run before TASK 2.")
            return False
        # Debt #36: default cluster first — later clusters seed shared-fn
        # bodies from the ones earlier clusters accepted (fill ONCE, propagate).
        clusters = fill_order(clusters)
        self.tel.log(f"  [skeleton] loaded {len(clusters)} baked cluster(s): "
                     f"{', '.join(c.name for c in clusters)}")

        genesis = self.ws.read(genesis_path) if genesis_path.exists() else ""
        functional_req = self.ws.read(functional_req_path) if functional_req_path.exists() else ""
        all_test_pairs: list[tuple[str, str]] = []
        for rel in sorted(existing_test_files):
            tp = project_dir / rel
            if tp.exists():
                try:
                    all_test_pairs.append((rel, tp.read_text(encoding="utf-8")))
                except Exception:
                    continue
        if not all_test_pairs:
            self.tel.log("  [skeleton] no test files; cannot proceed.")
            return False

        # P3: crate blueprint — the top-down design the per-block fills
        # consume slices of. Idempotent; never blocks the run on failure.
        if (self.blueprint_deps is not None
                and os.getenv("BLUEPRINT_PHASE", "1") != "0"):
            try:
                self._ensure_blueprint(project_dir, module, clusters[0],
                                       all_test_pairs, functional_req,
                                       functional_req_path)
            except Exception as e:
                self.tel.log(f"  [blueprint] generation failed ({e}) — "
                             "continuing without a blueprint.")

        # === task2a — per-cluster Phase A→D (RESUMABLE). The per-cluster
        # .skeleton-filled/ trees persist, so once this stage is marked done it
        # is skipped on resume / retry; restart_from.sh task2a re-runs it. ===
        cluster_summaries: list[dict[str, Any]] = []
        # Honor the task2a checkpoint only when every baked cluster's filled
        # tree is actually on disk (same artifact-intact rule task2b applies
        # to its merge manifest below). A bare done-flag is defeated by a
        # checkpoint written under a DIFFERENT cluster set: the skip branch
        # then fabricates ok=True for clusters that never ran, and the merge
        # silently drops their entire reconstruction.
        _task2a_done = False
        if self.tel.phase_valid(project_dir, f"task2a/{module}"):
            _missing = [c.name for c in clusters
                        if not (project_dir / ".skeleton-filled" / c.name).exists()]
            _task2a_done = not _missing
            if _missing:
                self.tel.log("  [skeleton] task2a marked done but no filled tree "
                             f"for cluster(s): {', '.join(_missing)} — stale "
                             "checkpoint; re-running per-cluster reconstruction.")
        if _task2a_done:
            self.tel.set_current(project_dir,
                                 f"task2a/{module} / skipped (checkpoint valid)")
            self.tel.log("  [skeleton] task2a (per-cluster reconstruction) already done — "
                         "skipping (fills persist in .skeleton-filled/).")
            cluster_summaries = [{"cluster": c.name, "ok": True} for c in clusters]
        else:
            self.tel.set_current(project_dir,
                                 f"task2a/{module} / per-cluster reconstruction (0/{len(clusters)})")

            def _run_one_cluster(cluster: BuildCluster,
                                 donor_dirs: list[Path]) -> dict[str, Any]:
                tag = f"[{cluster.name}]"
                self.tel.log(f"\n  {tag} === cluster start ===")
                try:
                    res = self._generate_one_cluster(
                        project_dir=project_dir,
                        cluster=cluster,
                        module=module,
                        all_test_pairs=all_test_pairs,
                        genesis=genesis,
                        functional_req=functional_req,
                        donor_dirs=donor_dirs,
                        donor_clusters={c.name: c for c in clusters},
                    )
                except Exception as _ce:
                    self.tel.log(f"  {tag} cluster crashed: {type(_ce).__name__}: {_ce}")
                    res = {"ok": False, "reason": f"exception: {type(_ce).__name__}: {_ce}"}
                if not res.get("ok"):
                    self.tel.log(f"  {tag} cluster aborted: {res.get('reason', 'unknown')}")
                return {"cluster": cluster.name, **res}

            # Debt #36: clusters run SERIALLY in fill_order. Each cluster seeds
            # its stubs from the bodies earlier clusters accepted, so an item
            # shared across clusters is filled ONCE; a later cluster rewrites a
            # seeded body only when its own compile/tests fail on it — which is
            # exactly the "proven divergence" that justifies a cfg-split at
            # merge. Cluster-level parallelism is intentionally gone: concurrent
            # per-cluster fills of the same shared fns were the double-fill /
            # cfg-chimera machine (2× LLM cost, wholesale cfg-gated forks).
            if self.cfg.parallelism > 1 and len(clusters) > 1:
                self.tel.log("  [skeleton] task2a runs clusters serially (debt "
                             "#36 shared-fn seeding); PIPELINE_PARALLELISM "
                             "applies within a cluster, not across clusters.")
            # The stage marker is all-or-nothing, but the work inside it is
            # per-cluster and serial — so without a per-cluster marker ANY
            # re-entry of task2a (a kill mid-cluster-2, a deleted
            # .skeleton-filled/<later cluster> to force a re-seed) replays
            # cluster 1's entire Phase A→D from iter 1 before the cluster that
            # actually needs work is reached. Measured on `anyhow`: `stable`
            # costs ~4.5 h and hundreds of model calls to re-derive the pass
            # count its intact tree already holds. A cluster that RAN TO
            # COMPLETION is checkpointed here, on the same rule task2b/task2c
            # use: marker valid AND its artifact on disk. Its tree still enters
            # `donor_dirs`, so a skipped cluster keeps seeding the ones after it
            # — the seed is read off the tree, never off the run that made it.
            donor_dirs: list[Path] = []
            for _ci, cluster in enumerate(clusters):
                _fd = project_dir / ".skeleton-filled" / cluster.name
                _ckey = f"task2a/{module}/{cluster.name}"
                _position = f"cluster {cluster.name} ({_ci + 1}/{len(clusters)})"
                if self.tel.phase_valid(project_dir, _ckey) and _fd.exists():
                    self.tel.set_current(
                        project_dir,
                        f"task2a/{module} / {_position} skipped (checkpoint valid)")
                    self.tel.log(f"  [{cluster.name}] cluster already reconstructed "
                                 "(checkpoint valid, filled tree on disk) — skipping "
                                 "its Phase A→D; its tree still seeds later clusters.")
                    cluster_summaries.append({"cluster": cluster.name, "ok": True,
                                              "skipped_checkpoint": True})
                    donor_dirs.append(_fd)
                    continue
                self.tel.set_current(project_dir, f"task2a/{module} / {_position}")
                _summary = _run_one_cluster(cluster, list(donor_dirs))
                cluster_summaries.append(_summary)
                if _summary.get("ok") and _fd.exists():
                    donor_dirs.append(_fd)
                    # Only a cluster whose Phase A→D returned ok is checkpointed:
                    # a crash or an abort leaves no marker, so the next run
                    # re-enters it and resumes against its partial tree exactly
                    # as it does today.
                    self.tel.mark_phase(
                        project_dir, _ckey, "done",
                        detail=("validated" if _summary.get("validates")
                                else "reconstructed"))
            # Only record task2a as done when at least one cluster actually
            # produced something. Marking it done unconditionally meant a
            # total wipeout (every cluster aborted) still got treated as a
            # valid checkpoint: the phase-valid check above reads it as
            # "already done" and short-circuits to `ok=True` for every
            # cluster without even looking at `.skeleton-filled/` — so a
            # retry within the same run inherited and merged straight from
            # whatever fragments survived the abort, silently losing every fn
            # that never got written. Not marking done here means a retry
            # re-attempts real reconstruction instead of trusting garbage.
            if any(s.get("ok") for s in cluster_summaries):
                self.tel.mark_phase(project_dir, f"task2a/{module}", "done",
                                    detail=f"{len(clusters)} cluster(s)")
            else:
                self.tel.log(f"  [skeleton] task2a/{module}: no cluster "
                             "succeeded — NOT marking done, so a retry "
                             "re-attempts reconstruction from the frozen "
                             "skeleton instead of reusing this attempt's "
                             "empty/partial .skeleton-filled/ state.")

        # === task2b — cross-cluster merge into project_dir/src/ (RESUMABLE).
        # Skipped only if marked done AND the merged tree is intact: every file
        # in the saved manifest still on disk. A bare `src/ exists` check is
        # defeated by the workspace stub (the startup wipe leaves a stub lib.rs),
        # which let phase C run against an empty tree. (task4's delete-and-retry
        # clears this marker, forcing a re-merge.) ===
        _merged_tree_intact = False
        if self.tel.phase_valid(project_dir, f"task2b/{module}"):
            _mf = self.ws.load_manifest(manifest_path) if manifest_path else []
            _merged_tree_intact = bool(_mf) and all(
                (project_dir / rel).exists() for rel in _mf)
        if _merged_tree_intact:
            self.tel.set_current(project_dir,
                                 f"task2b/{module} / skipped (merged tree intact)")
            self.tel.log("  [skeleton-merge] task2b (merge) already done — skipping.")
        else:
            self.tel.set_current(
                project_dir,
                f"task2b/{module} / merging {len(cluster_summaries)} cluster output(s)")
            # Debt #36: pre-merge stub backfill. A stub still left in one
            # cluster's tree (cfg-inactive there, or its fill exhausted) adopts
            # the body a sibling cluster accepted, so the AST merge never emits
            # a dead `unimplemented!()` cfg-variant alongside a real body —
            # only genuine rewrites remain divergent.
            _ok_trees = [project_dir / ".skeleton-filled" / s["cluster"]
                         for s in cluster_summaries if s.get("ok")]
            _ok_trees = [d for d in _ok_trees if d.exists()]
            _backfilled = 0
            _by_name = {c.name: c for c in clusters}
            for _tree in _ok_trees:
                _backfilled += self._backfill_tree(
                    _tree, [d for d in _ok_trees if d != _tree], _by_name)
            if _backfilled:
                self.tel.log(f"  [ownership] pre-merge backfill: {_backfilled} "
                             "stub body(ies) adopted from sibling clusters.")
            self.tel.log(f"\n  [skeleton-merge] composing {len(cluster_summaries)} cluster output(s) "
                         f"into {project_dir}/src/")
            merge_stats = self.fill.merge_cluster_outputs(
                project_dir=project_dir,
                cluster_specs={c.name: c for c in clusters},
                successes=[s for s in cluster_summaries if s.get("ok")],
            )
            self.tel.log(f"  [skeleton-merge] {merge_stats['files_written']} file(s) written, "
                         f"{merge_stats['cfg_gated_fns']} cfg-gated divergence(s).")
            self.tel.mark_phase(project_dir, f"task2b/{module}", "done",
                                detail=f"{merge_stats['files_written']} file(s)")

        # Post-merge fill pass: per-cluster Phase C drove fills against each
        # cluster's filtered cfg view of the code. After the AST merge, the
        # default-cfg view of `project_dir/src/` may expose fns that none of the
        # per-cluster Phase Cs reached (typically the stable variants of cfg-
        # divergent code paths). Without this step, TASK 3 panics at
        # `unimplemented!()` on fns Phase C never queued.
        #
        # Strategy: run cargo test --no-fail-fast on the merged tree, extract the
        # panic frontier with the same syn-based mapper Phase C uses, fill those
        # fns via the per-fn machinery, repeat until the frontier is empty or the
        # stall budget is exhausted. Always uses --no-fail-fast so panics from
        # every binary surface, not just the first one to crash.
        # === task2c — merged-tree fill (RESUMABLE). This is where U2 test-
        # acceptance does most of its work. restart_from.sh task2c re-runs ONLY
        # this stage (per-cluster + merge skipped if their markers stand). ===
        if self.tel.phase_valid(project_dir, f"task2c/{module}"):
            self.tel.set_current(project_dir,
                                 f"task2c/{module} / skipped (checkpoint valid)")
            self.tel.log("  task2c (merged-tree fill) already done — skipping.")
        else:
            self.tel.set_current(project_dir, f"task2c/{module} / merged-tree fill")
            self._post_merge_fill_pass(
                project_dir=project_dir,
                clusters=clusters,
                module=module,
                functional_req=functional_req,
            )
            self.tel.mark_phase(project_dir, f"task2c/{module}", "done")

        # Update the manifest so downstream task3/4 see the generated files.
        if manifest_path:
            generated = [str(p.relative_to(project_dir))
                         for p in (project_dir / "src").rglob("*.rs")
                         if str(p.relative_to(project_dir)) not in existing_test_files]
            if generated:
                self.ws.save_manifest(manifest_path, generated)
                self.tel.log(f"  [skeleton] manifest written: {len(generated)} source file(s).")

        return True

    def _generate_one_cluster(
        self,
        project_dir: Path,
        cluster: BuildCluster,
        module: str,
        all_test_pairs: list[tuple[str, str]],
        genesis: str,
        functional_req: str,
        donor_dirs: list[Path] | None = None,
        donor_clusters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run Phase A + B + C + D for a single cluster, returning a stats dict.

        All artifacts land under `.skeleton/{cluster}/` (skeleton + groups +
        failure dumps) and `.skeleton-filled/{cluster}/src/` (filled output).
        Cargo invocations honour `cluster.env_extra` end-to-end.
        """
        tag = f"[{cluster.name}]"
        skeleton_dir = project_dir / ".skeleton" / cluster.name
        filled_dir = project_dir / ".skeleton-filled" / cluster.name

        # Phase A — load the pre-baked skeleton from disk. Hard Rule 11: the
        # original source is gone after strip, so the skeleton MUST have been
        # produced earlier by `_bake_skeleton_artifacts`. If absent here, that
        # step did not run — fail loud.
        if not (skeleton_dir / "src").exists() or not any(skeleton_dir.rglob("*.rs")):
            self.tel.log(f"  {tag} [skeleton-A] FATAL: no baked skeleton at "
                         f"{skeleton_dir}. The pre-strip bake step did not run "
                         "for this cluster.")
            return {"ok": False, "reason": "no_baked_skeleton"}
        self.tel.log(f"  {tag} [skeleton-A] using baked skeleton at {skeleton_dir}.")

        # cargo check on the skeleton with cluster env. Fix-loop on failure.
        _cargo_toml = project_dir / "Cargo.toml"
        if _cargo_toml.exists() and not (skeleton_dir / "Cargo.toml").exists():
            shutil.copy(_cargo_toml, skeleton_dir / "Cargo.toml")
        check_cmd = self._cluster_check_cmd(cluster)
        # P3.8 diagnostic: log toolchain + final check_cmd so we can verify the
        # nightly cluster is actually using +nightly.
        self.tel.log(f"  {tag} [skeleton-A] cluster.toolchain={cluster.toolchain!r} "
                     f"requires_nightly={getattr(cluster, 'requires_nightly', False)}")
        self.tel.log(f"  {tag} [skeleton-A] cargo check ({check_cmd})...")
        rc, _out, _err = self.cargo.run_raw(
            shlex.split(check_cmd), skeleton_dir, self._cluster_env(cluster), 180)
        if rc == -1:
            self.tel.log(f"  {tag} [skeleton-A] cargo check timed out; proceeding anyway.")
        # P3.8: detect E0554 (nightly feature on stable channel) and retry with
        # nightly toolchain. The cluster's requires_nightly flag is set by an
        # earlier TASK 0 phase only when its cargo build hits E0554; the skeleton
        # backend's Phase A may run BEFORE that phase, so we need a local catch.
        if (rc not in (-1, 0)
                and "E0554" in (_err or "") + (_out or "")
                and not cluster.requires_nightly):
            self.tel.log(f"  {tag} [skeleton-A] detected E0554 — promoting cluster to nightly.")
            cluster.requires_nightly = True
            check_cmd = self._cluster_check_cmd(cluster)
            rc, _out, _err = self.cargo.run_raw(
                shlex.split(check_cmd), skeleton_dir, self._cluster_env(cluster), 180)
            if rc == -1:
                self.tel.log(f"  {tag} [skeleton-A] cargo +nightly check timed out.")
            else:
                self.tel.log(f"  {tag} [skeleton-A] retry with +nightly: rc={rc}")
        if rc not in (-1, 0):
            self.tel.log(f"  {tag} [skeleton-A] cargo check FAILED; invoking fix-loop.")
            _skel_sources = list((skeleton_dir / "src").rglob("*.rs"))
            ok, _ = self.fill.generate_fix_loop(
                project_dir=skeleton_dir,
                check_cmd=check_cmd,
                source_paths=_skel_sources,
                existing_test_files=set(),
                model=self.cfg.task2_model,
                max_fix_attempts=3,
                timeout=180,
            )
            if not ok:
                # The fix loop gave up (floor-stalled) — but that verdict is
                # about ONE hard fn, not the whole tree: `merge_cluster_outputs`
                # only ever sees clusters marked ok, so bailing here discards
                # every OTHER file the fix loop already got compiling clean
                # along with the one it couldn't. Re-check compilability
                # directly rather than trusting the fix loop's own exhausted
                # verdict: if the tree still doesn't compile, this genuinely
                # is a full failure (bail, unchanged from before). If it DOES
                # compile — the exhaustion was over test/behavioral
                # correctness on specific fn(s), not syntax — fall through to
                # Phase C below, whose per-fn TDD floor-stall/GENUINELY_HARD
                # machinery is exactly the right tool for "this one fn is
                # hard," instead of discarding the whole cluster over it.
                _rc2, _out2, _ = self.cargo.run_raw(
                    shlex.split(check_cmd), skeleton_dir, self._cluster_env(cluster), 180)
                if _rc2 not in (-1, 0):
                    return {"ok": False, "reason": "skeleton_fix_loop_exhausted"}
                self.tel.log(f"  {tag} [skeleton-A] fix-loop exhausted but the "
                             "tree still compiles clean — salvaging: falling "
                             "through to Phase C instead of discarding this "
                             "cluster's other files.")

        # Per-cluster test scoping.
        cluster_tests = self.clusters.filter_tests_for_cluster(all_test_pairs, cluster)
        self.tel.log(f"  {tag} [skeleton-B] tests for this cluster: "
                     f"{len(cluster_tests)}/{len(all_test_pairs)}")
        if not cluster_tests:
            return {"ok": True, "phase_c": None, "phase_d": None,
                    "note": "no tests apply to this cluster"}

        # Mode selection for Phase B+C: per-group (default) or per-fn TDD
        # (Flavour B, smaller prompts, one call per fn, no LLM grouping).
        per_fn_mode = self.cfg.phase_c_per_fn

        # Seed filled_dir from skeleton state (idempotent) regardless of mode.
        filled_dir.mkdir(parents=True, exist_ok=True)
        for src in skeleton_dir.rglob("*.rs"):
            rel = src.relative_to(skeleton_dir)
            dst = filled_dir / rel
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

        # The seed above copies only files that do not exist yet, which is right
        # for BODIES (the fill loop owns them) and wrong for the private LAYOUT:
        # a mid-run re-author edits the skeleton, so its new type/impl blocks
        # would never reach the tree cargo builds. Reinstall them here, where
        # the seed invariant is established.
        self._sync_layout_into_tree(
            tag, project_dir, cluster, filled_dir,
            [d.name for d in (donor_dirs or [])])

        # Debt #36: adopt the shared-fn bodies earlier clusters already accepted
        # (fill ONCE, propagate). Only `unimplemented!()` stubs are replaced, so
        # resume-time prior fills of THIS cluster are never overwritten; this
        # cluster's phase C then only fills its cluster-specific remainder and
        # rewrites a seeded body only when its own compile/tests reject it.
        if donor_dirs:
            _seeded = self._backfill_tree(filled_dir, donor_dirs, donor_clusters)
            if _seeded:
                self.tel.log(f"  {tag} [ownership] seeded {_seeded} shared fn "
                             f"body(ies) from {len(donor_dirs)} prior "
                             "cluster(s); only unfilled stubs remain for this "
                             "cluster's fill.")

        # Pre-pass heal: donor seeding and resume both admit bodies that were
        # never compile-gated against THIS tree (an exhausted fix loop leaves
        # its last broken body in the donor). One broken body fails every
        # unit of the coming pass as `downstream_only` — an entire pass of
        # LLM calls burned before end-of-pass validate reverts the blocker
        # (live case: a donor `context` body poisoned error.rs's 35-fn
        # refill). Check + revert the implicated fns NOW, before filling.
        # A syntax error ABORTS the parse, so rustc names exactly ONE unparseable
        # file per invocation — every other corrupted file stays invisible until
        # that one is repaired. A single heal round therefore fixes one file and
        # still hands the fill pass a tree that cannot build a single test binary
        # (observed: lib.rs healed, error.rs still broken, the cluster read
        # 0 passing for the whole run). Repeat until the tree compiles or a round
        # stops making progress.
        # Structural repair FIRST. A body that is only a re-declaration of its
        # own fn compiles and holds no `unimplemented!()`, so no compile
        # diagnostic and no stub scan can point at it — but the surplus brace
        # such a reply carries is a parse error the heal loop WOULD see, and it
        # would answer by reverting the fn to a stub, throwing away a body that
        # only needed un-nesting. Un-nest before anything gets a chance to
        # revert, and the hollow-but-compiling ones (a `#[test]` among them,
        # which no fill loop may ever touch) get repaired at all.
        _parse_ok = self.structure.parse_ok if self.structure is not None else None
        for _rep in repair_hollow_bodies(filled_dir / "src", _parse_ok):
            self.tel.log(f"  {tag} [hollow-repair] {_rep.file}::{_rep.fn_name}: "
                         "un-nested self-redeclaring body"
                         + (" + dropped surplus `}`" if _rep.dropped_stray_brace
                            else "")
                         + (" (TEST — was passing without asserting)"
                            if _rep.is_test else ""))
            self.tel.emit("phase_c.cluster.hollow_repair", data={
                "cluster": cluster.name, "file": _rep.file, "fn": _rep.fn_name,
                "is_test": _rep.is_test,
                "dropped_stray_brace": _rep.dropped_stray_brace,
            })
        # `--all-targets`, and this is the whole point of the heal. A bare
        # `cargo check` compiles the LIB; the sweep that judges the cluster runs
        # `cargo test`, which also builds every test binary and the `#[cfg(test)]`
        # gen_tests modules. Measured on `nightly` at the moment this was written:
        # `cargo +nightly check` -> 0 errors, `cargo +nightly check --all-targets`
        # -> 7, all of them in `test_autotrait` / `test_fmt`. So the heal was not
        # reporting a check it never ran -- it was truthfully reporting a NARROWER
        # compilation than the one it exists to protect, and a cluster whose test
        # binaries could not build sailed past it into a full fill pass. Validate
        # the compilation the sweep will do, not a subset of it.
        _heal_cmd = (check_cmd if "--all-targets" in check_cmd
                     else check_cmd + " --all-targets")
        _round = 0
        _clean = False
        _unchecked = False
        _prev_sig = ""
        _last_msg = ""
        _rc0 = 0
        _check_secs = 0.0
        while _round < _PREPASS_HEAL_MAX_ROUNDS:
            _t0 = time.monotonic()
            _rc0, _rendered0, _diags0 = self.cargo.run_check_diag(
                _heal_cmd, filled_dir, self._cluster_env(cluster), 180)
            _check_secs = time.monotonic() - _t0
            if _rc0 == 0:
                _clean = True
                break
            # Deterministic import repair BEFORE any revert. A seeded body is
            # authored against the DONOR's import block, and a body-only splice
            # cannot carry a `use` — so a body that is otherwise perfect fails
            # E0425 here. `_revert_implicated` would answer by throwing the body
            # away; rustc's own "consider importing" suggestion fixes it for
            # free. The seed propagates donor imports too, but only the ones the
            # donor FILE had: a body pulled through the signature fallback can
            # still name something neither file imports.
            for _imp_file in dict.fromkeys(
                    f.file for d in _diags0 for f in d.import_fixes):
                _imp = repair_missing_imports(
                    self.cargo.run_check_diag, _heal_cmd, filled_dir,
                    self._cluster_env(cluster), _imp_file, _diags0,
                    self.tel.log, f"{tag} [pre-pass-heal]")
                if _imp is not None:
                    _rc0, _rendered0, _diags0 = _imp
            if _rc0 == 0:
                _clean = True
                break
            if _rc0 == -1:
                # The check did not RUN — timeout, or cargo not found under this
                # env. It proved NOTHING, and the old code called that clean and
                # fell through in silence, which is indistinguishable in the log
                # from a tree that really did compile. A seeded cluster then
                # entered its fill pass unexamined and spent the pass on
                # `downstream_only` reverts. Say it, and let the TDD driver's
                # red-build guard aim the first iteration instead.
                _unchecked = True
                self.tel.log(f"  {tag} [pre-pass-heal] check did NOT run "
                             f"(rc=-1: {(_rendered0 or '').strip()[:120]}) — the "
                             "seeded tree is UNVERIFIED, not clean.")
                self.tel.emit("phase_c.cluster.prepass_heal_unchecked", data={
                    "cluster": cluster.name,
                    "detail": (_rendered0 or "").strip()[:400],
                })
                break
            _pre_impl = self.cargo.implicated_fns_from_check(
                _diags0, _rendered0 or "", filled_dir)
            _last_msg = next((d.message for d in _diags0 if d.message), "")
            # A syntax error (unbalanced braces left by an earlier splice) carries
            # NO fn attribution — rustc names only the file. Passing that file with
            # an empty fn name locates nothing at fn granularity, so the revert
            # escalates it to the heal hierarchy instead of silently doing nothing
            # and letting the cluster spend its whole pass on a tree that cannot
            # build a single test binary.
            _targets = _pre_impl or [FnLocator(d.file, "") for d in _diags0 if d.file]
            # An unchanged culprit means the heal hierarchy has no restore point
            # left for it; another round would just repeat the failed repair.
            _sig = repr(sorted({(f, fn) for f, fn in _targets})) + _last_msg[:200]
            if not _targets or _sig == _prev_sig:
                break
            _prev_sig = _sig
            _round += 1
            self.tel.log(f"  {tag} [pre-pass-heal] round {_round}: seeded/resumed "
                         "tree does not compile; implicated: "
                         + (", ".join(f"{f}::{fn}" for f, fn in _pre_impl[:6])
                            if _pre_impl
                            # A syntax error names no fn, so the FILE is the
                            # only handle on it — printing just the message
                            # leaves the culprit unidentifiable in the log.
                            else "no fn — file-level: "
                                 + ", ".join(dict.fromkeys(f for f, _ in _targets))
                                 + f": {_last_msg[:120]}"))
            _healed = self._revert_implicated(tag, _targets, filled_dir,
                                              skeleton_dir, project_dir,
                                              cluster.name)
            self.tel.log(f"  {tag} [pre-pass-heal] round {_round}: reverted "
                         f"{_healed} fn(s)/file(s).")
            self.tel.emit("phase_c.cluster.prepass_heal", data={
                "cluster": cluster.name, "round": _round, "reverted": _healed,
                "implicated": [{"file": f, "fn": fn} for f, fn in _targets],
            })
        # Verify the repair. A heal that did not restore the build leaves every
        # later signal meaningless — 0 passing with no localizable culprit — and
        # the run burns a full pass before anyone notices.
        if _round and not _clean:
            self.tel.log(f"  {tag} [pre-pass-heal] STILL BROKEN after {_round} "
                         f"round(s) — cluster cannot compile: {_last_msg[:160]}")
            self.tel.emit("phase_c.cluster.prepass_heal_failed", data={
                "cluster": cluster.name, "rounds": _round,
                "error": _last_msg[:400],
            })
        elif _round:
            self.tel.log(f"  {tag} [pre-pass-heal] tree compiles again after "
                         f"{_round} round(s).")
        elif _clean:
            # The no-op case, said out loud. A silent heal is why a cluster that
            # opened on 39 rustc errors looked like it had been checked: the only
            # evidence either way was the ABSENCE of log lines.
            self.tel.log(f"  {tag} [pre-pass-heal] seeded/resumed tree compiles "
                         f"as-is — no repair needed ({_heal_cmd}, "
                         f"{_check_secs:.1f}s).")
        # `rc` and `check_secs` are the falsifiers for the verdict itself. A
        # "clean" that came back in a fraction of a second on a tree whose src
        # was just rewritten did not compile anything, and the sweep six seconds
        # later has repeatedly disagreed with it (`nightly`: heal says clean,
        # `cargo test` finds 41 rustc errors). Until that is explained, the
        # verdict is a claim and these two fields are how the next run checks it.
        self.tel.emit("phase_c.cluster.prepass_heal_verdict", data={
            "cluster": cluster.name, "rounds": _round,
            "verdict": ("clean" if _clean else
                        "unchecked" if _unchecked else "still_broken"),
            "rc": _rc0, "check_secs": round(_check_secs, 2),
            "check_cmd": _heal_cmd,
        })

        # Propagate gen_tests siblings from project_dir/src/ into filled_dir/src/.
        # task0c commits these as cluster-specific unit-test specs; the bake step
        # emits only the structural skeleton. Without this propagation Phase C
        # runs cargo test against integration tests only and gen_tests are
        # invisible. `.broken` suffixes (left over from the legacy reattach
        # quarantine path) are treated as live — they were renamed because earlier
        # LLM-regenerated source didn't satisfy them, not because they are broken
        # in themselves.
        _proj_src = project_dir / "src"
        _filled_src = filled_dir / "src"
        if _proj_src.exists() and _filled_src.exists():
            _gt_allow_line, _ = self.gentests.derive_prelude(project_dir)
            # Gate each sibling by the cfg of the cluster that GENERATED it
            # (origin), not the current cluster — `cluster_cfg_attr(cluster)` is
            # always-active in its own build and gives no protection, so a
            # nightly-only test would compile (and E0658) in stable/no_core.
            # Origin cfg excludes it there and keeps it only where it belongs.
            # Fallback to current cluster if the manifest is missing (older runs).
            _gt_origin = self.gentests.load_origin(project_dir)
            _gt_cur_attr = self.clusters.cluster_cfg_attr(cluster)
            _gt_propagated = 0
            for gt in sorted(_proj_src.iterdir()):
                if not gt.is_file():
                    continue
                _parsed = parse_gen_tests_filename(gt.name)  # R7: structured parse
                if not _parsed:
                    continue
                _parent_stem, _pass_n = _parsed
                # R9.6: skip + quarantine undeclared orphans (on disk but absent
                # from a non-empty origin manifest) — they were never coverage-
                # validated and must not be resurrected under a guessed gate.
                if not self.gentests.propagate_or_quarantine_sibling(
                        gt, _gt_origin, _parent_stem, _pass_n, tag):
                    continue
                _dst_name = f"{_parent_stem}.gen_tests_{_pass_n}.rs"
                _dst = _filled_src / _dst_name
                _parent_path = _filled_src / f"{_parent_stem}.rs"
                if not _parent_path.exists():
                    continue
                try:
                    _body = gt.read_text(encoding="utf-8")
                except Exception as _e:
                    self.tel.log(f"  {tag} [skeleton-C] gen_tests read failed {gt.name}: {_e}")
                    continue
                if not self.scan.rust_delimiters_balanced(_body):
                    continue
                if not _dst.exists() or _dst.read_text(encoding="utf-8") != _body:
                    _dst.write_text(_body, encoding="utf-8")
                _ptext = _parent_path.read_text(encoding="utf-8")
                if not self.gentests.has_decl(_ptext, _pass_n):
                    _gt_attr = _gt_origin.get(
                        f"{_parent_stem}.gen_tests_{_pass_n}", _gt_cur_attr)
                    _decl = self.gentests.build_decl(
                        _dst_name, _pass_n, _gt_allow_line,
                        cluster_cfg_attr=_gt_attr,
                    )
                    _parent_path.write_text(_ptext.rstrip() + _decl, encoding="utf-8")
                    _gt_propagated += 1
            if _gt_propagated:
                self.tel.log(f"  {tag} [skeleton-C] propagated {_gt_propagated} "
                             f"gen_tests decl(s) into filled tree.")

        c_result: ClusterFillResult | dict[str, Any]
        if per_fn_mode:
            self.tel.log(f"  {tag} [skeleton-C] per-fn mode (Flavour B): scanning stubs...")
            # Make filled_dir a runnable crate root so cargo check/test work:
            # copy Cargo.toml + symlink tests/. Idempotent.
            if _cargo_toml.exists() and not (filled_dir / "Cargo.toml").exists():
                shutil.copy(_cargo_toml, filled_dir / "Cargo.toml")
            _proj_tests = project_dir / "tests"
            _filled_tests = filled_dir / "tests"
            if _proj_tests.exists() and not _filled_tests.exists():
                try:
                    os.symlink(_proj_tests, _filled_tests)
                except OSError as e:
                    self.tel.log(f"  {tag} [skeleton-C] tests symlink failed: {e}")

            # Deterministic cluster-leak guard. A gen_test that belongs to another
            # cluster (e.g. nightly's `request_ref`) must not be ACTIVE in this
            # cluster. With correct origin gating it is cfg-excluded and invisible
            # here; if any gen_tests file still produces an unstable-feature error
            # (E0658/E0554) under this cluster's stable toolchain, its origin gate
            # is missing/wrong — surface it loudly and quarantine the sibling so it
            # can't poison phase-c as undifferentiated reconstruction noise. The
            # skeleton's unimplemented!() bodies compile, so unrelated fill gaps
            # don't false-trigger (we act only on gen_tests-attributed diags).
            _has_gt = _filled_src.exists() and any(
                self.gentests.is_gen_tests_file(p.name)
                for p in _filled_src.iterdir() if p.is_file())
            if _has_gt:
                _norun = (f"cargo +{cluster.toolchain} test --no-run"
                          if cluster.toolchain else "cargo test --no-run")
                _lrc, _ltxt, _ldiags = self.cargo.run_check_diag(
                    _norun, filled_dir, self._cluster_env(cluster), 300)
                _leaks = sorted({
                    d.file for d in _ldiags
                    if ".gen_tests_" in d.file
                    and (d.code in ("E0658", "E0554") or "unstable" in d.message.lower())
                })
                if _leaks:
                    self.tel.log(f"  {tag} [skeleton-C] ⛔ CLUSTER LEAK in '{cluster.name}': "
                                 f"{len(_leaks)} gen_tests file(s) use nightly-only API but are "
                                 f"active under this cluster's stable toolchain "
                                 f"(origin-cfg gate missing/wrong): {', '.join(_leaks)}. "
                                 f"Quarantining so phase-c is not poisoned.")
                    for _bf in _leaks:
                        _name = Path(_bf).name
                        if _name.endswith(".rs") and ".gen_tests_" in _name:
                            _stem, _rest = _name.rsplit(".gen_tests_", 1)
                            _ns = _rest[:-3]  # strip ".rs"
                            if _ns.isdigit():
                                _pp = _filled_src / f"{_stem}.rs"
                                if _pp.exists():
                                    _pp.write_text(
                                        self.gentests.strip_decl_n(
                                            _pp.read_text(encoding="utf-8"), int(_ns)),
                                        encoding="utf-8")
                        _sp = filled_dir / _bf
                        if _sp.exists():
                            _sp.rename(_sp.with_name(_sp.name + ".broken"))
            tests_dir_files: list[Path] = []
            for trel, _ in cluster_tests:
                tp = project_dir / trel
                if tp.exists():
                    tests_dir_files.append(tp)
            # Leaf-first ordering counts come from the bake artefact (computed
            # against live source pre-strip). Falls back to alphabetical when absent.
            _leaf_counts = self.clusters.load_baked_leaf_order(project_dir, cluster.name)
            if not _leaf_counts:
                self.tel.log(f"  {tag} [skeleton-C] no leaf-order artefact "
                             f"({skeleton_dir}/_leaf_order.json); falling back to "
                             "alphabetical fn ordering.")
            # Per-crate FR is the home for model self-correction notes.
            _fr_for_notes = project_dir / f"functional_requirements_{module}.md"
            # Plan 4 — PHASE_C_DRIVER=tdd switches to the TDD outer loop (cargo
            # test → frontier → fill). Default `fn_walk` keeps the legacy per-fn-
            # walk driver.
            if self.cfg.phase_c_tdd:
                self.tel.log(f"  {tag} [skeleton-C] PHASE_C_DRIVER=tdd — using TDD outer loop")
                c_result = self.fill.run_phase_c_tdd(
                    project_dir=project_dir,
                    target_src_dir=filled_dir,
                    tests_dir_files=tests_dir_files,
                    cluster_tests=cluster_tests,
                    module=module,
                    cluster=cluster,
                    check_cmd=self._cluster_check_cmd(cluster),
                    cluster_env=self._cluster_env(cluster),
                    leaf_counts=_leaf_counts,
                    fr_path=_fr_for_notes,
                    test_cmd_builder=lambda stems, include_lib=False: (
                        self.cargo.build_test_cmd(stems, cluster, include_lib=include_lib)),
                    tag=tag,
                )
            else:
                c_result = self.fill.fill_fns_one_by_one(
                    project_dir=project_dir,
                    target_src_dir=filled_dir,
                    tests_dir_files=tests_dir_files,
                    module_name=module,
                    cluster_name=cluster.name,
                    cluster=cluster,
                    check_cmd=self._cluster_check_cmd(cluster),
                    cluster_env=self._cluster_env(cluster),
                    max_passes=self.cfg.phase_c_max_passes,
                    leaf_name_counts=_leaf_counts,
                    fr_path=_fr_for_notes,
                )
            self.tel.log(f"  {tag} [skeleton-C] {c_result.fns_filled} fn(s) filled, "
                         f"{c_result.bytes_filled:,} bytes net, "
                         f"{c_result.attempts} LLM call(s), "
                         f"{c_result.remaining} stub(s) remaining, "
                         f"{len(c_result.errors)} error(s).")
        else:
            # Phase B — grouping (cached per cluster).
            groups_path = skeleton_dir / "groups.json"
            groups: list[dict[str, Any]] | None = None
            if groups_path.exists():
                try:
                    groups = json.loads(groups_path.read_text(encoding="utf-8"))
                    self.tel.log(f"  {tag} [skeleton-B] cached manifest: {len(groups)} group(s).")
                except Exception:
                    groups = None
            if groups is None:
                self.tel.log(f"  {tag} [skeleton-B] grouping...")
                groups = self.fill.group_tests(skeleton_dir, cluster_tests,
                                                cluster_name=cluster.name, module_name=module)

            # Phase C — per-group fill into cluster-specific scratch dir.
            self.tel.log(f"  {tag} [skeleton-C] filling {len(groups)} group(s) into {filled_dir}/")
            c_result = self.fill.fill_groups(skeleton_dir, filled_dir, groups, project_dir,
                                             cluster_name=cluster.name, module_name=module,
                                             functional_req=functional_req, genesis=genesis,
                                             cluster=cluster)
            self.tel.log(f"  {tag} [skeleton-C] {c_result['groups_processed']} group(s), "
                         f"{c_result['bytes_filled']:,} bytes filled, "
                         f"{len(c_result['errors'])} error(s).")

        # Phase D — residual cleanup (per cluster).
        # Plan 4 — under driver=tdd, the outer loop fills exactly the fns
        # reachable from failing tests. Residuals are by-design unreachable from
        # any test → leaving them `unimplemented!()` is correct under tests-only
        # reconstruction. Skip Phase D.
        d_result: dict[str, Any]
        if self.cfg.phase_c_tdd:
            self.tel.log(f"  {tag} [skeleton-D] skipped (driver=tdd; residual stubs "
                         "are correctly left unimplemented).")
            d_result = {"stubs_total": 0, "stubs_exercised": 0,
                        "files_updated": 0, "stubs_remaining": 0}
        else:
            self.tel.log(f"  {tag} [skeleton-D] scanning for residual stubs...")
            d_result = self.fill.cleanup_residuals(filled_dir, project_dir, functional_req,
                                                   cluster_tests, module_name=module)
            self.tel.log(f"  {tag} [skeleton-D] {d_result['stubs_total']} stubs, "
                         f"{d_result['stubs_exercised']} test-exercised, "
                         f"{d_result['files_updated']} updated, "
                         f"{d_result['stubs_remaining']} remaining.")

        # Per-cluster intermediate validation: cargo check the filled output.
        if _cargo_toml.exists() and not (filled_dir / "Cargo.toml").exists():
            shutil.copy(_cargo_toml, filled_dir / "Cargo.toml")
        # P3.12: end-of-cluster validation with a fix-loop. The per-fn check
        # catches errors as they happen, but bodies that pass at splice-time can
        # still break integration when all fills are in place (cross-fn semantic
        # issues, stale fills from prior runs). When this cargo check fails,
        # identify the implicated fns from the error output, revert their bodies
        # to unimplemented!() so they re-appear as stubs, then re-enter the per-fn
        # fill loop targeting only those fns. Loop up to PHASE_C_VALIDATE_ITERS.
        max_validate_iters = self.cfg.validate_iters
        validate_ok = False
        _vr_out = ""
        for v_iter in range(1, max_validate_iters + 1):
            self.tel.check_stop(f"skeleton-validate iter {v_iter}")
            self.tel.log(f"  {tag} [skeleton-validate] cargo check on filled output "
                         f"(iter {v_iter}/{max_validate_iters})...")
            rc, _vr_out, _vdiags = self.cargo.run_check_diag(
                check_cmd, filled_dir, self._cluster_env(cluster), 180)
            if rc == -1:
                self.tel.log(f"  {tag} [skeleton-validate] cargo check timed out.")
                validate_ok = False
                break
            validate_ok = (rc == 0)

            if validate_ok:
                if v_iter > 1:
                    self.tel.log(f"  {tag} [skeleton-validate] PASSED after "
                                 f"{v_iter} iteration(s).")
                break

            # Failure path. Identify implicated fns.
            self.tel.log(f"  {tag} [skeleton-validate] cargo check FAILED "
                         f"(rc={rc}). First errors:")
            for line in _vr_out.splitlines()[:10]:
                self.tel.log(f"    {line}")
            implicated = self.cargo.implicated_fns_from_check(
                _vdiags, _vr_out, filled_dir)
            if not implicated:
                self.tel.log(f"  {tag} [skeleton-validate] no implicated fns parsed "
                             f"— cannot retry, giving up.")
                break
            self.tel.log(f"  {tag} [skeleton-validate] implicated fns: "
                         f"{', '.join(f'{f}::{fn}' for f, fn in implicated[:6])}"
                         + ("..." if len(implicated) > 6 else ""))
            reverted = self._revert_implicated(tag, implicated, filled_dir,
                                               skeleton_dir, project_dir,
                                               cluster.name)
            if reverted == 0:
                self.tel.log(f"  {tag} [skeleton-validate] nothing "
                             "revertible and no skeleton copy — giving up.")
                break
            self.tel.log(f"  {tag} [skeleton-validate] reverted {reverted} fn(s) "
                         f"to stubs; re-entering per-fn fill loop.")
            self.tel.emit("phase_c.cluster.validate_retry", data={
                "cluster": cluster.name, "iter": v_iter,
                "implicated": [{"file": f, "fn": fn} for f, fn in implicated],
                "reverted": reverted,
            })
            # Re-enter the per-fn fill loop for ONE pass over just the reverted
            # stubs. Use the cargo error excerpt as initial prompting context
            # (the fix loop will refresh per attempt).
            _ = self.fill.fill_fns_one_by_one(
                project_dir=project_dir,
                target_src_dir=filled_dir,
                tests_dir_files=[project_dir / trel
                                 for trel, _ in cluster_tests
                                 if (project_dir / trel).exists()],
                module_name=module,
                cluster_name=cluster.name,
                cluster=cluster,
                check_cmd=check_cmd,
                cluster_env=self._cluster_env(cluster),
                max_passes=1,  # one pass through the reverted set
                leaf_name_counts=self.clusters.load_baked_leaf_order(project_dir, cluster.name),
                fr_path=_fr_for_notes,
            )

        # F7: the tree still does not compile/parse after every heal attempt.
        # Emit a LOUD halt so the run never silently marches on mis-exhausting
        # fns from a broken state (the ~30h corruption freeze had no alarm).
        if not validate_ok:
            from pipeline_core.domain.tree_health import (
                TreeHealth,
                classify_tree_health,
            )
            # The verdict comes from THIS check's own rc and text: the loop's
            # last check predates its revert+refill, and the loop also exits on
            # a budget/attribution break with a tree the reverts already healed.
            # Hardcoding a failing rc reported "unrecoverable" for a tree that
            # compiles clean, and the stale excerpt named errors nobody could
            # still reproduce.
            _hrc, _htxt, _hdiags = self.cargo.run_check_diag(
                check_cmd, filled_dir, self._cluster_env(cluster), 180)
            _health = classify_tree_health(_hrc, _hdiags, 0, 0)
            _detail = ("validate budget exhausted, but the tree compiles"
                       if _health is TreeHealth.OK else "tree unrecoverable")
            self.tel.emit("phase_c.cluster.halted", data={
                "cluster": cluster.name, "health": _health.name,
                "first_errors": (_htxt or "").splitlines()[:5]})
            self.tel.log(f"  {tag} [skeleton-validate] cluster HALTED "
                         f"({_health.name}) — {_detail}; NOT marking "
                         "fns exhausted from this state.")

        return {"ok": True, "phase_c": c_result, "phase_d": d_result,
                "validates": validate_ok}

    def _git_ancestor_blobs(self, repo: Path, repo_rel: str) -> list[bytes]:
        """Committed versions of `repo_rel` in the workspace git repo, newest
        first — the fill tree is checkpoint-committed, so a parseable ancestor
        is exactly what would have saved the pre-corruption 22-fill state."""
        import subprocess
        try:
            r = subprocess.run(
                ["git", "-C", str(repo), "log", "--format=%H", "--", repo_rel],
                capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                return []
            blobs: list[bytes] = []
            for sha in r.stdout.split():
                b = subprocess.run(
                    ["git", "-C", str(repo), "show", f"{sha}:{repo_rel}"],
                    capture_output=True, timeout=30)
                if b.returncode == 0 and b.stdout:
                    blobs.append(b.stdout)
            return blobs
        except Exception:
            return []

    def _heal_deps(self, project_dir: Path, filled_dir: Path,
                   skeleton_dir: Path, cluster_name: str) -> HealDeps | None:
        """Build the F2 heal collaborators, or None when no parse gate is wired
        (→ caller keeps the legacy whole-file skeleton restore)."""
        if self.structure is None:
            return None

        def _skeleton_bytes(rel: str) -> bytes | None:
            sp = skeleton_dir / rel
            return sp.read_bytes() if sp.exists() else None

        def _best_tree_bytes(rel: str) -> bytes | None:
            bp = project_dir / ".best_tree" / cluster_name / rel
            return bp.read_bytes() if bp.exists() else None

        def _git_blobs(rel: str) -> list[bytes]:
            try:
                repo_rel = str((filled_dir / rel).relative_to(project_dir))
            except ValueError:
                return []
            return self._git_ancestor_blobs(project_dir, repo_rel)

        return HealDeps(
            parse_ok=self.structure.parse_ok,
            best_tree_bytes=_best_tree_bytes,
            git_ancestor_blobs=_git_blobs,
            skeleton_bytes=_skeleton_bytes,
            log=self.tel.log, emit=self.tel.emit)

    def _revert_implicated(self, tag: str, implicated: list[FnLocator],
                           filled_dir: Path, skeleton_dir: Path,
                           project_dir: Path | None = None,
                           cluster_name: str = "") -> int:
        """Revert every implicated fn to `unimplemented!()` so the per-fn loop
        refills it. EVERY same-name occurrence is reverted (fmt lives in each
        Debug/Display impl; the error output gives a bare name, so any of them
        may be the culprit — a stub is always safe, a kept-broken body never
        is). Occurrences are rewritten last-to-first so earlier offsets stay
        valid. When nothing is revertible at fn granularity (corruption
        OUTSIDE any locatable fn body — e.g. a past splice broke out of a
        doc-comment, leaving stray tokens between items), restore each
        implicated FILE wholesale from the baked skeleton: good fills in that
        file are lost, but the tree parses again and the per-fn loop refills
        them; a permanently broken file blocks the ENTIRE cluster forever.
        Returns the number of fns/files reverted (0 = nothing left to try)."""
        reverted = 0
        unparseable: set[str] = set()
        heal_deps = self._heal_deps(project_dir, filled_dir, skeleton_dir,
                                    cluster_name) if project_dir else None
        for rel_f, fn in implicated:
            fp = filled_dir / rel_f
            try:
                text = orig = fp.read_text(encoding="utf-8")
            except Exception:
                continue
            locs = locate_all_fns(text, fn)
            here = 0
            for _sig, _ss, body_start, body_end in reversed(locs):
                cur_body = text[body_start:body_end]
                if is_unimplemented_body(cur_body):
                    continue  # already a stub — nothing to revert
                # Re-indent unimplemented!() like a normal body.
                line_start = text.rfind("\n", 0, _ss) + 1
                indent = ""
                for ch in text[line_start:_ss]:
                    if ch in (" ", "\t"):
                        indent += ch
                    else:
                        break
                inner = indent + "    "
                new_body = "\n" + inner + "unimplemented!()\n" + indent
                text = text[:body_start] + new_body + text[body_end:]
                here += 1
            if not here:
                continue
            fp.write_text(text, encoding="utf-8")
            # A revert that leaves the file unparseable is worse than the broken
            # body it replaced: it kills the whole cluster's compile, so every
            # unit of the coming pass fails for a reason none of them caused and
            # the run reads 0 passing with no localizable culprit. Roll the file
            # back and hand it to the heal hierarchy instead.
            if heal_deps is not None and not heal_deps.parse_ok(fp):
                fp.write_text(orig, encoding="utf-8")
                unparseable.add(rel_f)
                self.tel.log(f"  {tag} [revert] {rel_f}: reverting '{fn}' left the "
                             "file unparseable — rolled back, escalating to heal.")
                self.tel.emit("phase_c.revert.unparseable",
                              data={"file": rel_f, "fn": fn,
                                    "cluster": cluster_name})
                continue
            reverted += here
        for rel_f in sorted(unparseable):
            if heal_deps is None:
                continue
            out = heal_file(filled_dir, rel_f, cluster_name, heal_deps)
            if out.point is not RestorePoint.NONE:
                reverted += 1
        if reverted == 0:
            # No fn was revertible at fn granularity — corruption sits OUTSIDE
            # any locatable fn body (the doc-comment intrusion class). Run the
            # heal hierarchy: surgical excision → best-tree → git-ancestor →
            # skeleton (loud loss), least-destructive first. The old code jumped
            # straight to a whole-file skeleton copy, discarding every fill.
            for rel_f in {rf for rf, _ in implicated}:
                if heal_deps is not None:
                    out = heal_file(filled_dir, rel_f, cluster_name, heal_deps)
                    if out.point is not RestorePoint.NONE:
                        reverted += 1
                    continue
                # Legacy fallback when no parse gate is wired.
                skel_src = skeleton_dir / rel_f
                if not skel_src.exists():
                    continue
                shutil.copy(skel_src, filled_dir / rel_f)
                reverted += 1
                self.tel.log(f"  {tag} [heal] {rel_f}: no fn was revertible — "
                             "restored whole file from skeleton (stubs will "
                             "be refilled).")
        return reverted

    @staticmethod
    def _cfg_set_of(cluster: Any) -> CfgSet | None:
        """A donor cluster's cfg evaluation context, or None when the cluster is
        unknown — None means 'no verdict', and the merge then trusts the donor
        exactly as it did before this gate existed."""
        if cluster is None:
            return None
        return CfgSet.of(set(cluster.cfg_true), set(cluster.cfg_false))

    def _backfill_tree(self, tree_dir: Path, donor_dirs: list[Path],
                       donor_clusters: dict[str, Any] | None = None) -> int:
        """Fill remaining `unimplemented!()` stubs in `tree_dir/src` with the
        bodies the same rel-file carries in `donor_dirs` (earlier donor wins).
        gen_tests siblings are skipped — they are cluster-owned test specs, not
        shared implementation. Returns the number of stub bodies adopted.

        Bodies are addressed by `FnSlot`, so a donor's `Display::fmt` can never
        land in the target's `Debug::fmt`; where a slot cannot be matched and the
        signature is ambiguous the merge refuses, and the refusals are logged
        because each one is a stub the model now has to fill for money.

        `donor_clusters` maps a donor dir NAME to its `BuildCluster`, which is
        what lets the merge withhold bodies the donor's own build never
        compiled. Absent (or missing a name) the donor is trusted whole, which
        is the pre-existing behaviour — the seed still runs when the cluster
        objects are not to hand rather than silently adopting nothing."""
        src_root = tree_dir / "src"
        if not src_root.exists():
            return 0
        adopted = 0
        refused = 0
        layout_skipped = 0
        cfg_skipped = 0
        imported = 0
        cfg_by_dir = {
            dd: self._cfg_set_of((donor_clusters or {}).get(dd.name))
            for dd in donor_dirs}
        for dst in sorted(src_root.rglob("*.rs")):
            if self.gentests.is_gen_tests_file(dst.name):
                continue
            rel = dst.relative_to(tree_dir)
            donors: list[str] = []
            # Built in the SAME pass as `donors` so the two stay positionally
            # paired: a donor dir without this file contributes neither a text
            # nor a cfg-set, and a list built per donor DIR would shift every
            # later donor's gate onto the wrong text.
            donor_cfgs: list[Any] = []
            for dd in donor_dirs:
                dp = dd / rel
                if dp.exists():
                    try:
                        donors.append(dp.read_text(encoding="utf-8"))
                    except OSError:
                        continue
                    donor_cfgs.append(cfg_by_dir.get(dd))
            if not donors:
                continue
            try:
                text = dst.read_text(encoding="utf-8")
            except OSError:
                continue
            _bf = backfill_stub_bodies(text, donors, donor_cfgs)
            refused += _bf.ambiguous_skipped
            layout_skipped += _bf.layout_skipped
            cfg_skipped += _bf.cfg_skipped
            if _bf.filled:
                dst.write_text(_bf.text, encoding="utf-8")
                adopted += _bf.filled
                imported += _bf.uses_added
        if imported:
            self.tel.log(f"  [ownership] carried {imported} donor import(s) in "
                         "with the adopted bodies — a body names what its own "
                         "file imports, and no per-fn fill can add a file-level "
                         "`use`.")
        if refused:
            self.tel.log(f"  [ownership] {refused} donor body(ies) NOT adopted — "
                         "signature ambiguous across sibling impl blocks and the "
                         "slot did not match; the fill loop owns those stubs.")
        if layout_skipped:
            self.tel.log(
                f"  [ownership] {layout_skipped} donor body(ies) NOT adopted — "
                "they read a type this cluster SHAPES differently (its own "
                "authored layout wins), so no per-fn fill could repair them "
                "here; this cluster fills them against its own declarations.")
        if cfg_skipped:
            self.tel.log(
                f"  [ownership] {cfg_skipped} donor body(ies) NOT adopted — the "
                "donor's own cfg-set never compiles them, so no compiler has "
                "ever checked them; a body cannot be inherited as verified by "
                "a cluster that does build it.")
        return adopted

    def _post_merge_fill_pass(
        self,
        project_dir: Path,
        clusters: list[BuildCluster],
        module: str,
        functional_req: str,
    ) -> None:
        """FR-024 improvement-driven loop on the merged tree.

        Runs cargo test on `project_dir`, attributes failures via the full chain
        (runtime/compile/classifier/recently_filled/reverse_map), fills, repeats.
        Exits when pass count stalls for `max_stalls` consecutive iters AND there
        is no actionable frontier — never on a fixed iter count alone. Closes the
        per-cluster / merged-view mismatch where Phase C declares done but the
        merged source still has unfilled stubs under the default cfg TASK 3 runs
        under.
        """
        max_stalls = self.cfg.post_merge_max_stalls
        # Hard cap is now a runaway-safety backstop only — floor-stall
        # (max_stalls) is the real terminator. Set to 0 to skip the pass entirely
        # (legacy behaviour preserved).
        hard_cap = self.cfg.post_merge_hard_cap
        if hard_cap <= 0:
            return
        tag = "[skeleton-merge]"
        tests_dir_files: list[Path] = list((project_dir / "tests").rglob("*.rs"))
        fr_path = project_dir / f"functional_requirements_{module}.md"
        leaf_counts: dict[str, int] = {}
        for c in clusters:
            for fn_name_k, v in (self.clusters.load_baked_leaf_order(project_dir, c.name) or {}).items():
                leaf_counts[fn_name_k] = max(leaf_counts.get(fn_name_k, 0), int(v))
        test_cmd = "cargo test --no-fail-fast"
        test_timeout = self.cfg.post_merge_test_timeout
        try:
            test_reverse_map = self.fill.build_test_reverse_map(tests_dir_files, project_dir)
        except Exception as _e:
            self.tel.log(f"  {tag} post-merge reverse-map build failed: {_e}")
            test_reverse_map = {}

        permanently_exhausted: set[FnLocator] = set()
        # The merge landed every cluster's fills — D3's "other fills landed"
        # criterion holds globally, so cross-iter exhaustion gets one reset.
        self.fill.reset_exhaustion_flags(project_dir, "post-merge: merged tree is new context")
        recently_filled: list[FnLocator] = []
        recently_filled_max = self.cfg.recently_filled_max
        stall = 0
        best_pass = -1
        best_fail = None
        iter_n = 0
        while iter_n < hard_cap:
            iter_n += 1
            self.tel.check_stop(f"post-merge fill iter {iter_n}")
            self.tel.log(f"\n  {tag} post-merge fill iter {iter_n}: "
                         f"running cargo test on merged tree...")
            t0 = time.monotonic()
            rc, output = self.cargo.run(test_cmd, project_dir, self.cargo.env(),
                                        timeout=test_timeout)
            dt = time.monotonic() - t0
            pass_count, fail_count, _ignored = parse_test_counts(output)
            if rc == 0:
                self.tel.log(f"  {tag} post-merge cargo test PASSED "
                             f"({pass_count} passed, {dt:.1f}s).")
                self.tel.emit("post_merge.fill.iter", data={
                    "iter": iter_n, "rc": 0, "frontier_size": 0, "kind": "pass",
                    "pass_count": pass_count, "fail_count": fail_count,
                })
                return
            if rc == -1:
                self.tel.log(f"  {tag} post-merge cargo test TIMED OUT. Stopping.")
                self.tel.emit("post_merge.fill.iter", data={
                    "iter": iter_n, "rc": -1, "frontier_size": 0, "kind": "timeout",
                    "pass_count": pass_count, "fail_count": fail_count,
                })
                return

            improved = pass_count > best_pass
            if improved:
                best_pass = pass_count
                best_fail = fail_count
                stall = 0
            else:
                stall += 1

            _attr = self.cargo.attribute_failures(
                output, project_dir, recently_filled, test_reverse_map, "merged",
            )
            frontier, kind = _attr.frontier, _attr.kind
            eligible = {k: v for k, v in frontier.items()
                        if k not in permanently_exhausted}
            # Stall on pass_count is the sole real terminator. A non-empty
            # frontier with a stuck fn must not override it — same fix as
            # _run_phase_c_tdd: pass_count plateau is the only "no real
            # improvement" signal that captures cluster-level progress.
            if stall >= max_stalls:
                self.tel.log(f"  {tag} post-merge iter {iter_n}: pass_count plateau "
                             f"{stall} iter(s) at best={best_pass}; stopping "
                             f"({len(eligible)} fn(s) still on frontier — marking "
                             "exhausted). best pass="
                             f"{best_pass} fail={best_fail}.")
                for _stuck_key in eligible:
                    permanently_exhausted.add(_stuck_key)
                self.tel.emit("post_merge.fill.iter", data={
                    "iter": iter_n, "rc": rc, "frontier_size": len(eligible),
                    "kind": "stall_exit",
                    "pass_count": pass_count, "fail_count": fail_count,
                    "improved": improved, "stall": stall,
                })
                return
            if not eligible:
                self.tel.log(f"  {tag} post-merge iter {iter_n}: no actionable "
                             f"frontier (stall={stall}/{max_stalls}); continuing.")
                self.tel.emit("post_merge.fill.iter", data={
                    "iter": iter_n, "rc": rc, "frontier_size": 0,
                    "kind": "no_frontier_continue",
                    "pass_count": pass_count, "fail_count": fail_count,
                    "improved": improved, "stall": stall,
                })
                continue

            self.tel.log(f"  {tag} post-merge iter {iter_n}: cargo test rc={rc} "
                         f"({dt:.1f}s); pass={pass_count} fail={fail_count} "
                         f"(improved={improved}, stall={stall}/{max_stalls}); "
                         f"frontier={len(eligible)} fn(s) ({kind}).")
            self.tel.emit("post_merge.fill.iter", data={
                "iter": iter_n, "rc": rc,
                "frontier_size": len(eligible), "kind": kind,
                "pass_count": pass_count, "fail_count": fail_count,
                "improved": improved, "stall": stall,
            })
            sub = self.fill.fill_fns_one_by_one(
                project_dir=project_dir,
                target_src_dir=project_dir,
                tests_dir_files=tests_dir_files,
                module_name=module,
                cluster_name="merged",
                cluster=None,
                check_cmd="cargo check",
                cluster_env=self.cargo.env(),
                max_passes=1,
                leaf_name_counts=leaf_counts,
                fr_path=fr_path,
                tdd_frontier_keys=set(eligible.keys()),
                tdd_frontier_tests=FailureFrontier.build(
                    eligible, _attr.convicting),
                tdd_test_cmd=test_cmd,         # Plan 10 U2
                skip_end_pass_test=True,
            )
            cur_stubs = set(self.fill.scan_unimplemented_functions(project_dir / "src"))
            newly_filled: list[FnLocator] = [
                k for k in eligible
                if k not in cur_stubs
                and (k[0].split("/")[-1], k[1]) not in cur_stubs
            ]
            for k in newly_filled:
                recently_filled.append(k)
            if len(recently_filled) > recently_filled_max:
                recently_filled = recently_filled[-recently_filled_max:]
            for key in eligible:
                if key in cur_stubs or (key[0].split("/")[-1], key[1]) in cur_stubs:
                    permanently_exhausted.add(key)
            self.tel.log(f"  {tag} post-merge iter {iter_n}: "
                         f"{sub.fns_filled} fn(s) filled, "
                         f"{sub.attempts} attempt(s).")
        self.tel.log(f"  {tag} post-merge hard cap {hard_cap} reached (safety net).")

    @staticmethod
    def _cluster_check_cmd(cluster: BuildCluster) -> str:
        """Build the cargo check command. Cluster-specific flags are passed via
        env_extra (RUSTFLAGS etc.), not via --features — that distinction depends
        on which cfg names are real Cargo features vs --cfg flags, and
        task0_cfg_scan has already encoded the right form in env_extra. Toolchain
        is applied via `cargo +nightly` when required."""
        # Default message format (not --message-format=short) so the model sees
        # rustc's `note:` and `help:` hints alongside the bare error, which often
        # points directly at the fix (e.g. "consider importing one of these
        # traits: ...").
        parts = ["cargo", "check"]
        if cluster.toolchain:
            parts = ["cargo", f"+{cluster.toolchain}"] + parts[1:]
        return shlex.join(parts)

    def _cluster_env(self, cluster: BuildCluster) -> dict[str, str]:
        """Cargo env with cluster's env_extra merged in."""
        env = self.cargo.env()
        env.update(cluster.env_extra or {})
        return env
