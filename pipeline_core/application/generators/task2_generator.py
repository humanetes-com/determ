"""TASK-2 source generators — Clean-Architecture port of the pipeline.py
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
import traceback
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from ...application.author_layout import (
    LayoutComponentRecord,
    LayoutOutcome,
)
from ...application.reconcile_layout import (
    adopt_registry_definitions,
    carried_locate_keys,
    carry_registry,
    plan_layout_reconciliation,
)
from ...application.reconstruct.fill_rules import is_unimplemented_body
from ...application.reconstruct.heal import HealDeps, heal_file
from ...application.reconstruct.hollow_repair import repair_hollow_bodies
from ...application.reconstruct.import_repair import repair_missing_imports
from ...application.reconstruct.publish import publish_tree
from ...application.reconstruct.tree_txn import TreeTxn
from ...domain.cluster_plan import fill_order, one_tree_cluster
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
from ...domain.layout_sync_staleness import DEFAULT_STALE_AFTER, ReinstallStreak
from ...domain.llm_output import extract_files
from ...domain.models import (
    BuildCluster,
    ClusterFillResult,
    FnLocator,
)
from ...domain.pipeline_crash import ClusterScore, PipelineCodeCrash, PipelineCrash
from ...domain.reference_floor import (
    ReferenceMeasurement,
)
from ...domain.rust_fn_headers import locate_all_fns
from ...domain.rust_scan import item_names
from ...domain.text import (
    extract_code_content,
    smart_truncate,
    strip_list_marker,
    trim_to_line,
)
from ...phases import phase_scope, phase_skipped
from ...phases import step as phase_step
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
            # Strip list markers: "- ", "* ", "1. ", "2) ", etc. (structured).
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

        # Put the entry point first; remaining order updated dynamically after each generation.
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
                norm_test = strip_test_prefix(test_stem)  # structured prefix strip
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
            # cover the same TESTFILE blocks (structured marker scan).
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
            # Per-file retry — qwen3 thinking mode sometimes returns empty output.
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

            # Re-sort remaining files using mod declarations from the just-generated file.
            if pending and file_path in all_files:
                with suppress(Exception):  # cycle or error — keep current order
                    pending = topo_sort_files(pending, all_files)

        if not all_files:
            self.tel.log("  ERROR: no files generated.")
            return False

        written = self.ws.write_source_files(all_files, project_dir, manifest_path, existing_test_files)
        self.tel.log(f"  {written} source file(s) written.")
        return True


class PostMergeCrash(NamedTuple):
    """One merged sweep's crash reading. `culprits` is what the rank holds
    ahead of the cap. `sites` is what `scope_convicting` reads, so the
    judgment and the rank cannot come from two localizer calls."""

    culprits: Mapping[FnLocator, frozenset[str]]
    sites: tuple[Any, ...]




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
    # Blueprint phase deps (BlueprintDeps, constructed by the composition
    # root; None → blueprint generation is skipped).
    blueprint_deps: Any = None
    # Heal: the syn StructureReader (parse-check gate for the heal hierarchy).
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
    # `project_dir, registry -> path`. Writes the carried registry back.
    # None: the in-memory carry still installs, and the file stays as stored.
    save_layout_registry: Any = None
    # `project_dir, cluster_name, records -> path`. Writes locate keys back
    # once the skeleton holds the carried definition. None: keys stay stored.
    save_layout_records: Any = None
    # `project_dir -> ReferenceMeasurement | None`: the original crate's verdict
    # vector, the same floor task2a closes a cluster on and task3 passes one
    # on. Omitted, the post-merge per-cluster sweep keeps the absolute rule.
    reference_measurement: Callable[[Path], ReferenceMeasurement | None] | None = None
    # The held-out agreement reading on a cluster tree whose fill just
    # closed (`application/behaviour_agreement.measure_agreement`, bound in
    # composition). Counts only; its return is not read here. None: no
    # reading.
    agreement: Callable[..., object] | None = None
    # `(sweep_output, test_cmd, crate_root, env) -> crash sites`, the TDD
    # driver's localizer (`crash_localizer.localize_crashes`, bound in
    # composition). The post-merge loop reads it when a merged sweep has
    # binaries that died, so a culprit is held in its batch the way task2a
    # holds it. None: the loop ranks without crash facts, as before.
    localize_crashes: Callable[[str, str, Path, dict[str, str]], tuple[Any, ...]] | None = None

    def _sync_layout_into_tree(self, tag: str, project_dir: Path, cluster: Any,
                               filled_dir: Path,
                               donor_clusters: Sequence[str] = (),
                               skeleton_dir: Path | None = None) -> None:
        """Reinstall every AUTHORED layout definition the live tree is missing.
        Silent no-op on a healthy tree (nothing is superseded), so this reads a
        handful of files per cluster start and never rewrites a filled body.

        The definition matched and installed is the stored text with frozen
        repr and visibility carried. When the filled tree keeps that text, it
        is written back into the registry, the locate keys and the skeleton.
        ``setdefault`` does not replace an entry already stored, so a registry
        written before the carry existed would otherwise be reinstalled, and
        the next seed would copy it out of the skeleton, for the life of the
        tree. A reinstall the compile gate rolls back is not written back.

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
        _streak = self._load_reinstall_streak(project_dir, cluster)
        if not plan:
            # Nothing superseded: the registry and this cluster's tree agree.
            # A standing staleness streak is over — the divergence it measured
            # has re-converged (a re-author refreshed the registry, or the
            # tree adopted it), so the next divergence starts from zero.
            if _streak.green_reverts:
                self._save_reinstall_streak(project_dir, cluster,
                                            _streak.cleared())
            # The filled tree already holds the carried text. The stores and
            # the skeleton may still hold the model's — that is what a later
            # seed and a raw compare would put back.
            self._rewrite_stored_layout(
                tag, project_dir, cluster, surface, own_records, skeleton_dir)
            return
        _stale_after = int(os.getenv("LAYOUT_SYNC_STALE_AFTER",
                                     str(DEFAULT_STALE_AFTER)))
        if _streak.stale(_stale_after):
            # The registry is the stale side, measured: its reinstall has been
            # rolled back on a COMPILING tree this many consecutive entries,
            # so the registry holds a layout the live tree cannot build (the
            # 2026-08-19 entry: `ErrorImpl { vtable, backtrace, value }`
            # field-set mismatch, E0308 at src/error.rs:220, plus an E0277 —
            # repeating every entry). Not re-adopting protects the RED path:
            # kept-by-rule on a red-at-entry tree, the reinstall would hand
            # `_restub_after_reinstall` the live fills to re-stub toward that
            # unbuildable layout — a whole-run cost for a crash mid-splice.
            self.tel.log(
                f"  {tag} [layout-sync] NOT re-adopting the registry: its "
                f"reinstall was rolled back on a compiling tree "
                f"{_streak.green_reverts} consecutive entr(y/ies) — the "
                "registry is stale against this cluster's live tree. The "
                "streak clears when a reinstall lands or the plans converge.")
            self.tel.emit("phase_c.layout_sync.registry_stale", data={
                "cluster": getattr(cluster, "name", ""),
                "green_reverts": _streak.green_reverts,
                "threshold": _stale_after,
                "definitions_withheld": sum(len(v) for v in plan.values()),
            })
            return
        # The registry is CRATE-WIDE and permanent, and this step reinstalls it
        # over the live tree on every later start. A retrigger that fired on a
        # false cause therefore keeps costing: on 2026-08-01 two fired with
        # `cause=DESIGN_CONFLICT` on rustc errors that came from rewritten test
        # fixtures, their definitions survived the run, were reinstalled on the
        # next start, broke the build, and `pre-pass-heal` reverted 41 fns —
        # 93 passing to 31. The tree can be restored from a snapshot; the
        # registry cannot.
        #
        # So the reinstall gets a build, and is judged the way a body is: kept
        # only if it does not make a compiling tree stop compiling. A tree that
        # was ALREADY broken is not held against it — that is the case this step
        # exists to repair.
        _check = self._cluster_check_cmd(cluster)
        _env = self._cluster_env(cluster)
        _rc_before, _, _ = self.cargo.run_check_diag(_check, filled_dir, _env, 180)
        # A MECHANICAL write: pre-imaged, parse-verified, and undone by byte
        # restore — the hand-kept `_before` dict this replaces was the
        # same idea written once for this caller only.
        _structure = getattr(self, "structure", None)
        _txn = TreeTxn(src, validate_parse=(
            _structure.parse_ok if _structure is not None else None))
        written, err = apply_to_tree(plan, _txn)
        _pv = _txn.verify()
        if not _pv.ok:
            _txn.rollback()
            self.tel.log(f"  {tag} [layout-sync] REVERTED: the reinstall left "
                         f"a file unparseable ({_pv.reason}) — every "
                         "pre-image restored.")
            self.tel.emit("phase_c.entry.step_reverted", data={
                "cluster": getattr(cluster, "name", ""),
                "step": "layout-sync", "files": list(_txn.touched),
                "reason": _pv.reason})
            return
        self.tel.log(
            f"  {tag} [layout-sync] reinstalled "
            f"{sum(len(v) for v in plan.values())} authored definition(s) into "
            f"{written} live file(s)" + (f"; ERROR: {err}" if err else "")
            + " — the tree was building a layout the store had superseded.")
        _rc_after, _out_after, _diags_after = self.cargo.run_check_diag(
            _check, filled_dir, _env, 180)
        if _rc_after == 0:
            _txn.commit()
            # The reinstall LANDED — whatever divergence the streak was
            # counting is resolved.
            if _streak.green_reverts:
                self._save_reinstall_streak(project_dir, cluster,
                                            _streak.cleared())
            self._rewrite_stored_layout(
                tag, project_dir, cluster, surface, own_records, skeleton_dir)
            return
        if _rc_before != 0:
            # The rollback rule below cannot judge a reinstall on a tree that
            # was already red — and stopping here left the destruction
            # unpaired: the adopted definitions stood while every body
            # written against the superseded ones was left in place for the
            # compiler to find. The wreckage then presents as file-level
            # errors that resolve to no fn (a cast in a macro definition, a
            # top-level item), pre-pass-heal's fn-scoped reader reverts
            # nothing, and the cluster pays with its whole run: `nightly` in
            # run `3579062-476abc3b` (2026-08-19) took `E0605` at
            # src/backtrace.rs:21 plus unresolved-import cascades in every
            # test file, three heal rounds reverted 0 fns, and the TDD loop
            # closed the cluster with ZERO scored sweeps. Destroy-then-restub
            # is a pair (the crash/layout retrigger's own rule): re-stub what
            # the compiler implicates, and give file-level damage file-level
            # reversion. Scoped to src/ — errors in tests/ and in generated
            # test files are the oracle (never rewritten) and, for a lib that
            # does not build, cascades that repairing the lib removes.
            _txn.commit()
            self._rewrite_stored_layout(
                tag, project_dir, cluster, surface, own_records, skeleton_dir)
            self._restub_after_reinstall(
                tag, filled_dir, skeleton_dir, project_dir, cluster,
                _check, _env, _rc_after, _out_after, _diags_after)
            return
        _txn.rollback()
        self.tel.emit("phase_c.entry.step_reverted", data={
            "cluster": getattr(cluster, "name", ""),
            "step": "layout-sync", "files": list(_txn.touched),
            "reason": "a compiling tree stopped compiling"})
        # A green-tree rollback is a measurement of the REGISTRY, and it
        # accumulates: enough consecutive ones and the sync stops re-adopting
        # (the staleness branch above), so a later red-at-entry tree is not
        # re-stubbed toward a layout these rollbacks proved unbuildable.
        _streak = _streak.reverted_on_green()
        self._save_reinstall_streak(project_dir, cluster, _streak)
        self.tel.log(
            f"  {tag} [layout-sync] REVERTED: the reinstalled definition(s) "
            "stopped a compiling tree from compiling. The registry holds a "
            "layout this cluster's tree cannot build "
            f"({_streak.green_reverts} consecutive green-tree rollback(s)) "
            "— first errors: "
            + "; ".join(line for line in _out_after.splitlines()
                        if "error" in line)[:300])
        self.tel.emit("phase_c.layout_sync.reverted", data={
            "cluster": getattr(cluster, "name", ""),
            "files": sorted(Path(p).name for p in _txn.touched),
            "definitions": sum(len(v) for v in plan.values()),
            "green_reverts": _streak.green_reverts,
        })

    def _rewrite_stored_layout(
        self, tag: str, project_dir: Path, cluster: Any, surface: Any,
        records: Sequence[Any], skeleton_dir: Path | None,
    ) -> None:
        """Persist the carried definition into the skeleton, the locate keys
        and the registry. The filled tree already holds it (the reinstall
        committed, or the file already matched). A green-tree rollback does
        not call this: the carried text did not compile, and storing it would
        make the registry the side the staleness streak then refuses.

        The skeleton is updated first, and a locate key moves only once that
        file contains the carried definition. The registry is the crate-wide
        copy ``setdefault`` will not replace, so it is rewritten whenever the
        carry changes an entry, including one the skeleton does not hold —
        the next cluster start installs from it."""
        save_reg = getattr(self, "save_layout_registry", None)
        save_recs = getattr(self, "save_layout_records", None)
        if (skeleton_dir is None and save_reg is None and save_recs is None
                and self.layout_registry is None):
            return
        skel_files = 0
        read_skel: Callable[[str], str | None] | None = None
        if skeleton_dir is not None:
            skel_src = skeleton_dir / "src"
            if skel_src.is_dir():
                read_skel = lambda rel: _read_text(skel_src / rel)  # noqa: E731
                skel_plan, skel_notes = plan_layout_reconciliation(
                    surface, records, read_skel)
                for n in skel_notes:
                    self.tel.log(f"  {tag} [layout-sync] skeleton: {n}")
                if skel_plan:
                    _structure = getattr(self, "structure", None)
                    _txn = TreeTxn(skel_src, validate_parse=(
                        _structure.parse_ok if _structure is not None else None))
                    apply_to_tree = self.layout_sync[2]
                    written, err = apply_to_tree(skel_plan, _txn)
                    _pv = _txn.verify()
                    if err or not _pv.ok:
                        _txn.rollback()
                        self.tel.log(
                            f"  {tag} [layout-sync] skeleton carry NOT kept"
                            + (f" ({err})" if err else "")
                            + (f" ({_pv.reason})" if not _pv.ok else ""))
                    else:
                        _txn.commit()
                        skel_files = written
        n_reg = 0
        if self.layout_registry is not None and save_reg is not None:
            reg = self.layout_registry(project_dir)
            carried = carry_registry(surface, reg)
            n_reg = sum(
                1 for uid, item in carried.items()
                if uid not in reg or item.definition != reg[uid].definition)
            if n_reg:
                save_reg(project_dir, carried)
        n_keys = 0
        if save_recs is not None and read_skel is not None:
            updated, changed = carried_locate_keys(
                surface, records, read_skel)
            if changed:
                save_recs(project_dir, getattr(cluster, "name", ""), updated)
                n_keys = sum(
                    1 for old, new in zip(records, updated, strict=True)
                    for (_ou, od), (_nu, nd) in zip(
                        old.defs, new.defs, strict=True)
                    if od != nd)
        if n_reg or skel_files or n_keys:
            self.tel.log(
                f"  {tag} [layout-sync] carried frozen facts into the stored "
                f"layout — registry {n_reg}, skeleton file(s) {skel_files}, "
                f"locate key(s) {n_keys}")

    @staticmethod
    def _reinstall_streak_path(project_dir: Path, cluster: Any) -> Path:
        return (project_dir / ".skeleton"
                / f"_layout_sync_streak_{getattr(cluster, 'name', '')}.json")

    def _load_reinstall_streak(self, project_dir: Path,
                               cluster: Any) -> ReinstallStreak:
        """Total: a missing or corrupt state file costs a fresh streak, which
        fails SAFE (the sync re-adopts — the pre-guard behaviour)."""
        try:
            return ReinstallStreak.from_dict(json.loads(
                self._reinstall_streak_path(project_dir, cluster)
                .read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return ReinstallStreak()

    def _save_reinstall_streak(self, project_dir: Path, cluster: Any,
                               streak: ReinstallStreak) -> None:
        p = self._reinstall_streak_path(project_dir, cluster)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(streak.to_dict()), encoding="utf-8")
        except OSError as e:
            self.tel.log(f"  [layout-sync] could not persist the reinstall "
                         f"streak: {e}")

    def _restub_after_reinstall(self, tag: str, filled_dir: Path,
                                skeleton_dir: Path | None, project_dir: Path,
                                cluster: Any, check_cmd: str,
                                env: dict[str, str], rc: int, out: str,
                                diags: list[Any]) -> None:
        """The second half of a red-path reinstall: re-stub the bodies the
        compiler implicates against the adopted definitions, giving file-level
        damage (errors that resolve to no fn) file-level reversion. Bounded
        rounds; stops when the tree compiles, when a round reverts nothing, or
        when every remaining error is outside src/ (oracle content, or test
        cascades a lib repair removes)."""
        _skel = skeleton_dir or (project_dir / ".skeleton"
                                 / getattr(cluster, "name", ""))
        for _round in range(1, 4):
            _impl = self.cargo.implicated_fns_from_check(
                diags, out or "", filled_dir)
            _fn_targets = [
                t for t in _impl
                if t.file.startswith("src/")
                and not self.gentests.is_gen_tests_file(Path(t.file).name)]
            _covered = {t.file for t in _fn_targets}
            _nofn = sorted(
                {d.file for d in diags
                 if getattr(d, "file", "")
                 and d.file.startswith("src/")
                 and not self.gentests.is_gen_tests_file(Path(d.file).name)}
                - _covered)
            _targets = _fn_targets or [FnLocator(f, "") for f in _nofn]
            if not _targets:
                self.tel.log(
                    f"  {tag} [layout-sync] tree still red after the "
                    "reinstall, but no error lands on repairable src/ "
                    "content — leaving it to pre-pass-heal (test-file "
                    "errors are the oracle, or cascades of the lib).")
                break
            _n = self._revert_implicated(tag, _targets, filled_dir, _skel,
                                         project_dir,
                                         getattr(cluster, "name", ""))
            self.tel.log(
                f"  {tag} [layout-sync] round {_round}: re-stubbed/healed "
                f"{_n} target(s) written against the superseded "
                "definition(s) — a reinstall is a deliberate destruction "
                "and travels with its re-stub, like a re-author.")
            self.tel.emit("phase_c.layout_sync.restubbed", data={
                "cluster": getattr(cluster, "name", ""), "round": _round,
                "reverted": _n,
                "targets": [f"{f}::{fn}" for f, fn in _targets[:12]],
            })
            if _n == 0:
                break
            rc, out, diags = self.cargo.run_check_diag(
                check_cmd, filled_dir, env, 180)
            if rc == 0:
                self.tel.log(f"  {tag} [layout-sync] tree compiles after "
                             f"{_round} re-stub round(s).")
                return
        if rc != 0:
            self.tel.log(
                f"  {tag} [layout-sync] tree STILL red after the reinstall's "
                "re-stub rounds — first errors: "
                + "; ".join(line for line in (out or "").splitlines()
                            if "error" in line)[:300])

    def _ensure_blueprint(self, project_dir: Path, module: str,
                          default_cluster: Any, all_test_pairs: list[tuple[str, str]],
                          functional_req: str, functional_req_path: Path) -> None:
        """The model authors its own design doc from skeleton sigs +
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

        # Load clusters from the bake artefact written before strip (no original
        # source access post-strip). If absent, abort — there is no live source
        # to discover from at this point.
        clusters = self.clusters.load_baked(project_dir)
        if not clusters:
            self.tel.log("  [skeleton] FATAL: no baked clusters at "
                         f"{project_dir}/.skeleton/_clusters.json. The skeleton bake "
                         "step (pre-strip) must run before TASK 2.")
            return False
        # Default cluster first — later clusters seed shared-fn
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

        # Crate blueprint — the top-down design the per-block fills
        # consume slices of. Idempotent; never blocks the run on failure.
        if (self.blueprint_deps is not None
                and os.getenv("BLUEPRINT_PHASE", "1") != "0"):
            try:
                with phase_scope("t2a.blueprint", module=module):
                    self._ensure_blueprint(project_dir, module, clusters[0],
                                           all_test_pairs, functional_req,
                                           functional_req_path)
            except Exception as e:
                self.tel.log(f"  [blueprint] generation failed ({e}) — "
                             "continuing without a blueprint.")

        # === task2a — ONE loop over ONE tree, judged in every world
        # (RESUMABLE). Task 2a used to run one loop per cluster over one tree
        # each, seed later clusters from earlier ones, merge the trees in
        # task2b and run a second, default-world fill over the merged tree in
        # task2c. On run `1680589-7a6824f8` (2026-10-19) the two filled trees
        # were byte-identical, so the second tree bought no body, while a
        # module re-entry skipped both clusters and the merge overwrote what
        # later writers put in `src/`. The loop's identity
        # (`cluster_plan.one_tree_cluster`) keeps the default world's tree and
        # stores and makes every test run cover each world. ===
        loop_cluster = one_tree_cluster(clusters)
        assert loop_cluster is not None
        _tree = project_dir / ".skeleton-filled" / loop_cluster.name
        _worlds_label = "+".join(sorted(c.name for c in clusters))
        cluster_summaries: list[dict[str, Any]] = []
        if self.tel.phase_valid(project_dir, f"task2a/{module}") and _tree.exists():
            self.tel.set_current(project_dir,
                                 f"task2a/{module} / skipped (checkpoint valid)")
            self.tel.log("  [skeleton] task2a (the one tree) already done — "
                         "skipping (fills persist in .skeleton-filled/).")
            cluster_summaries = [{"cluster": loop_cluster.name, "ok": True}]
            phase_skipped("t2a", "checkpoint valid", module=module)
        else:
            if self.tel.phase_valid(project_dir, f"task2a/{module}"):
                self.tel.log(f"  [skeleton] task2a marked done but no tree at "
                             f"{_tree} — stale checkpoint; re-running it.")
            self.tel.set_current(
                project_dir,
                f"task2a/{module} / one tree, worlds {_worlds_label}")
            tag = f"[{loop_cluster.name}]"
            self.tel.log(f"\n  {tag} === tree start === one tree, judged in "
                         f"every world: {_worlds_label}")
            with phase_scope("t2a", cluster=loop_cluster.name,
                             module=module) as _ph:
                try:
                    res = self._generate_one_cluster(
                        project_dir=project_dir,
                        cluster=loop_cluster,
                        module=module,
                        all_test_pairs=all_test_pairs,
                        genesis=genesis,
                        functional_req=functional_req,
                        worlds=clusters,
                    )
                except Exception as _ce:
                    # The traceback is the only record of WHICH call raised:
                    # run `39786-fd38e72f` (2026-10-09) kept the message alone
                    # and the raising decode could not be named.
                    _tb = traceback.format_exc()
                    self.tel.log(f"  {tag} tree crashed: {type(_ce).__name__}: {_ce}\n{_tb}")
                    self.tel.emit("task2a.cluster_crashed", data={
                        "cluster": loop_cluster.name,
                        "exception": f"{type(_ce).__name__}: {_ce}",
                        "traceback": _tb})
                    res = {"ok": False, "reason": f"exception: {type(_ce).__name__}: {_ce}",
                           "crash": PipelineCrash(
                               cluster=loop_cluster.name,
                               exception=f"{type(_ce).__name__}: {_ce}",
                               traceback=_tb)}
                if not res.get("ok"):
                    self.tel.log(f"  {tag} tree aborted: {res.get('reason', 'unknown')}")
                    _ph.close("error")
            _summary = {"cluster": loop_cluster.name, **res}
            cluster_summaries.append(_summary)
            _pc = _summary.get("phase_c")
            _crash = str(getattr(_pc, "crashed", None) or "")
            _scores = [ClusterScore(
                cluster=loop_cluster.name,
                passed=len(getattr(_pc, "ran_tests", ()) or ())
                - len(getattr(_pc, "failed_tests", ()) or ()),
                failed=tuple(sorted(getattr(_pc, "failed_tests", ()) or ())),
                how="crashed" if _crash or _summary.get("crash") else "filled")]
            _pipeline_crash: PipelineCrash | None = None
            _escaped = _summary.get("crash")
            if isinstance(_escaped, PipelineCrash):
                _pipeline_crash = _escaped
            elif _crash:
                _pipeline_crash = PipelineCrash(
                    cluster=loop_cluster.name, exception=_crash,
                    traceback=str(getattr(_pc, "crash_traceback", "")
                                  or "(the driver logged it)"))
            if _pipeline_crash is not None:
                # task2b and task3 would build on a tree a crash left
                # unfilled. The orchestrator turns this into the HEALTH line
                # and a failed exit.
                self.tel.log(f"  [skeleton] task2a/{module}: NOT marking done; "
                             "the publish and task3 are skipped.")
                self.tel.emit("task2a.pipeline_crash", data={
                    "cluster": _pipeline_crash.cluster,
                    "exception": _pipeline_crash.exception,
                    "traceback": _pipeline_crash.traceback,
                    "scores": [{"cluster": c.cluster, "passed": c.passed,
                                "failed": list(c.failed), "how": c.how}
                               for c in _scores]})
                raise PipelineCodeCrash(_pipeline_crash, tuple(_scores))
            if (_pc is not None and getattr(_pc, "ran_tests", None)
                    and getattr(_pc, "exhausted", False)):
                self.tel.log(
                    f"  [skeleton] the tree is EXHAUSTED, not done: "
                    f"{len(_pc.failed_tests)} test(s) still failing after its "
                    f"budgets were spent "
                    f"({', '.join(sorted(_pc.failed_tests)[:4])}).")
            if getattr(_pc, "baseline_broken", False):
                self.tel.log(f"  [skeleton] task2a/{module}: NOT marking done — "
                             "the tree stopped on a build that does not compile. "
                             "The next run re-enters it.")
            elif _summary.get("ok"):
                self.tel.mark_phase(
                    project_dir, f"task2a/{module}", "done",
                    detail=f"one tree, worlds {_worlds_label}")
            else:
                self.tel.log(f"  [skeleton] task2a/{module}: the tree did not "
                             "complete — NOT marking done, so a retry "
                             "re-attempts it.")

        # === task2b — publish the one tree into project_dir/src/
        # (RESUMABLE). A byte copy: no body is chosen, gated or written here,
        # so `src/` is the tree the loop judged. Skipped only if marked done
        # AND every file of the saved manifest is still on disk. ===
        _published_intact = False
        if self.tel.phase_valid(project_dir, f"task2b/{module}"):
            _mf = self.ws.load_manifest(manifest_path) if manifest_path else []
            _published_intact = bool(_mf) and all(
                (project_dir / rel).exists() for rel in _mf)
        if _published_intact:
            self.tel.set_current(project_dir,
                                 f"task2b/{module} / skipped (published tree intact)")
            self.tel.log("  [skeleton-publish] task2b (publish) already done — skipping.")
            phase_skipped("t2b", "publish marker valid, tree intact", module=module)
        elif cluster_summaries and cluster_summaries[0].get("ok") and _tree.exists():
            with phase_scope("t2b", module=module):
                _n = publish_tree(_tree, project_dir)
                self.tel.log(f"  [skeleton-publish] {_n} file(s) of {_tree} "
                             f"published into {project_dir}/src/.")
                self.tel.mark_phase(project_dir, f"task2b/{module}", "done",
                                    detail=f"{_n} file(s) published")

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
        worlds: Sequence[BuildCluster] = (),
    ) -> dict[str, Any]:
        """Run Phase A + B + C + D for a single cluster, returning a stats dict.

        ``worlds``: every build world the tree is judged in. The tests of the
        loop are those active in ANY world, and the loop's env (from
        ``cluster``, `cluster_plan.one_tree_cluster`) runs each test command
        under every world. Empty is the one world ``cluster``.

        All artifacts land under `.skeleton/{cluster}/` (skeleton + groups +
        failure dumps) and `.skeleton-filled/{cluster}/src/` (filled output).
        Cargo invocations honour `cluster.env_extra` end-to-end.
        """
        tag = f"[{cluster.name}]"
        skeleton_dir = project_dir / ".skeleton" / cluster.name
        filled_dir = project_dir / ".skeleton-filled" / cluster.name

        # Phase A — load the pre-baked skeleton from disk. The
        # original source is gone after strip, so the skeleton MUST have been
        # produced earlier by `_bake_skeleton_artifacts`. If absent here, that
        # step did not run — fail loud.
        if not (skeleton_dir / "src").exists() or not any(skeleton_dir.rglob("*.rs")):
            self.tel.log(f"  {tag} [skeleton-A] FATAL: no baked skeleton at "
                         f"{skeleton_dir}. The pre-strip bake step did not run "
                         "for this cluster.")
            return {"ok": False, "reason": "no_baked_skeleton"}
        phase_step("t2a.check", cluster=cluster.name)
        self.tel.log(f"  {tag} [skeleton-A] using baked skeleton at {skeleton_dir}.")

        # cargo check on the skeleton with cluster env. Fix-loop on failure.
        _cargo_toml = project_dir / "Cargo.toml"
        if _cargo_toml.exists() and not (skeleton_dir / "Cargo.toml").exists():
            shutil.copy(_cargo_toml, skeleton_dir / "Cargo.toml")
        check_cmd = self._cluster_check_cmd(cluster)
        # Diagnostic: log toolchain + final check_cmd so we can verify the
        # nightly cluster is actually using +nightly.
        self.tel.log(f"  {tag} [skeleton-A] cluster.toolchain={cluster.toolchain!r} "
                     f"requires_nightly={getattr(cluster, 'requires_nightly', False)}")
        self.tel.log(f"  {tag} [skeleton-A] cargo check ({check_cmd})...")
        rc, _out, _err = self.cargo.run_raw(
            shlex.split(check_cmd), skeleton_dir, self._cluster_env(cluster), 180)
        if rc == -1:
            self.tel.log(f"  {tag} [skeleton-A] cargo check timed out; proceeding anyway.")
        # Detect E0554 (nightly feature on stable channel) and retry with
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
                # about ONE hard fn, not the whole tree: only a tree marked ok
                # is published, so bailing here discards every OTHER file the
                # fix loop already got compiling clean along with the one it
                # couldn't. Re-check compilability
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
        phase_step("t2a.tests", cluster=cluster.name)
        # Every world's tests, once each: a test file compiled in any world is
        # one the sweep runs (it builds empty in a world that gates it out).
        _seen_tests: set[str] = set()
        cluster_tests = []
        for _w in (tuple(worlds) or (cluster,)):
            for _pair in self.clusters.filter_tests_for_cluster(all_test_pairs, _w):
                if _pair[0] not in _seen_tests:
                    _seen_tests.add(_pair[0])
                    cluster_tests.append(_pair)
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

        # The tree is a crate before anything builds it. Every check below (the
        # layout sync, the ownership backfill's compile gate, the pre-pass heal)
        # runs cargo with this directory as its working directory, and cargo
        # with no manifest here climbs to the workspace's and builds the
        # stripped shell instead.
        self._establish_crate_root(tag, project_dir, cluster, filled_dir,
                                   _cargo_toml)

        # The seed above copies only files that do not exist yet, which is right
        # for BODIES (the fill loop owns them) and wrong for the private LAYOUT:
        # a mid-run re-author edits the skeleton, so its new type/impl blocks
        # would never reach the tree cargo builds. Reinstall them here, where
        # the seed invariant is established.
        # The other worlds' authored layout records count as donors here: the
        # per-world authoring of t1.5 still writes one record set per baked
        # cluster, and the one tree must declare what any of them authored.
        self._sync_layout_into_tree(
            tag, project_dir, cluster, filled_dir,
            [w.name for w in worlds if w.name != cluster.name],
            skeleton_dir=skeleton_dir)

        # Pre-pass heal: a resume admits bodies that were
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
                    self.tel.log, f"{tag} [pre-pass-heal]",
                    validate_parse=_parse_ok)
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
            # RED and named as a budget matter, not just a state report: the
            # nightly cluster of run `2219473` (2026-08-10) entered its TDD
            # phase in exactly this state and spent three iterations at
            # 0/113 under an unchanged pair of rustc errors before exiting.
            # The TDD loop now refuses after 3 identical-digest red sweeps
            # (its red-build budget); this line is the entry-side warning
            # that the phase is starting inside that budget.
            self.tel.log(f"  {tag} [pre-pass-heal] STILL BROKEN after {_round} "
                         f"round(s) — cluster cannot compile: {_last_msg[:160]} "
                         "— RED: the fill phase starts on a tree where no "
                         "test can report a verdict; the TDD loop's red-build "
                         "budget (3 identical-digest sweeps) bounds it.")
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

        _filled_src = filled_dir / "src"

        phase_step("t2a.fill", cluster=cluster.name)
        c_result: ClusterFillResult | dict[str, Any]
        if per_fn_mode:
            self.tel.log(f"  {tag} [skeleton-C] per-fn mode (Flavour B): scanning stubs...")
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
            # PHASE_C_DRIVER=tdd switches to the TDD outer loop (cargo
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
                    heal_file=lambda rel: self._heal_rel(
                        project_dir, filled_dir, skeleton_dir, cluster.name, rel),
                    worlds=tuple(worlds),
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
                    heal_file=lambda rel: self._heal_rel(
                        project_dir, filled_dir, skeleton_dir, cluster.name, rel),
                )
            self.tel.log(f"  {tag} [skeleton-C] {c_result.fns_filled} fn(s) filled, "
                         f"{c_result.bytes_filled:,} bytes net, "
                         f"{c_result.attempts} LLM call(s), "
                         f"{c_result.remaining} stub(s) remaining, "
                         f"{len(c_result.errors)} error(s).")
            if self.agreement is not None:
                self.agreement(project_dir=project_dir, tree_dir=filled_dir, module=module,
                               cluster=cluster, where="task2a", stubs=c_result.remaining)
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
        phase_step("t2a.residual", cluster=cluster.name)
        # Under driver=tdd, the outer loop fills exactly the fns
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
        # End-of-cluster validation with a fix-loop. The per-fn check
        # catches errors as they happen, but bodies that pass at splice-time can
        # still break integration when all fills are in place (cross-fn semantic
        # issues, stale fills from prior runs). When this cargo check fails,
        # identify the implicated fns from the error output, revert their bodies
        # to unimplemented!() so they re-appear as stubs, then re-enter the per-fn
        # fill loop targeting only those fns. Loop up to PHASE_C_VALIDATE_ITERS.
        phase_step("t2a.validate", cluster=cluster.name)
        max_validate_iters = self.cfg.validate_iters
        validate_ok = False
        _vr_out = ""
        # The verdict is computed over EVERY target, not just the lib. A cluster
        # whose test binaries do not compile is not a validated cluster, and
        # `cargo check` alone certified one on 2026-08-01. Measured before
        # switching, per the note this fix was filed with: both cluster trees and
        # the merged tree pass `cargo check --all-targets` today exactly as they
        # pass `cargo check`, so nothing that currently validates stops doing so.
        _verdict_cmd = self._cluster_check_cmd(cluster, all_targets=True)
        for v_iter in range(1, max_validate_iters + 1):
            self.tel.check_stop(f"skeleton-validate iter {v_iter}")
            self.tel.log(f"  {tag} [skeleton-validate] cargo check on filled output "
                         f"(iter {v_iter}/{max_validate_iters})...")
            rc, _vr_out, _vdiags = self.cargo.run_check_diag(
                _verdict_cmd, filled_dir, self._cluster_env(cluster), 180)
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
            # The verdict now compiles the test binaries, so a diagnostic can
            # land in one — and the repair below reverts what it is handed to
            # `unimplemented!()`. The test oracle is immutable: an error in a
            # test file is evidence about the production API, never a licence to
            # rewrite the spec. Drop those targets before anything is reverted.
            _oracle = [(f, fn) for f, fn in implicated
                       if f.startswith("tests/")
                       or self.gentests.is_gen_tests_file(Path(f).name)]
            if _oracle:
                implicated = [t for t in implicated if t not in _oracle]
                self.tel.log(
                    f"  {tag} [skeleton-validate] {len(_oracle)} implicated "
                    "target(s) are test-oracle content and were NOT reverted: "
                    + ", ".join(f"{f}::{fn}" for f, fn in _oracle[:4]))
            if not implicated:
                # Two different dead ends, and they call for different reading.
                # Nothing parsed means the diagnostics carried no location this
                # walk could resolve; everything dropped means the errors ARE
                # located and they are in the immutable oracle, so the cluster
                # is genuinely broken and no body rewrite is the answer.
                self.tel.log(
                    f"  {tag} [skeleton-validate] "
                    + (f"every implicated target ({len(_oracle)}) is test-oracle "
                       "content — the errors are in the spec, not in a body this "
                       "loop may rewrite; giving up."
                       if _oracle else
                       "no implicated fns parsed — cannot retry, giving up."))
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
                heal_file=lambda rel: self._heal_rel(
                    project_dir, filled_dir, skeleton_dir, cluster.name, rel),
            )

        # Tree health: the tree still does not compile/parse after every heal attempt.
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

        # A cluster whose fill loop stopped on an unbuildable tree is not a
        # finished cluster, and `validate_ok` cannot see the difference: it
        # comes from `cargo check`, which builds the LIB, while the targets
        # that failed were the TEST binaries. Reporting ok here checkpoints the
        # cluster `done`, and the checkpoint is read on every later run as
        # "already reconstructed — skipping", so one broken sweep retires the
        # cluster permanently. Observed 2026-08-01: `nightly` stopped at
        # pass=0 with two rustc errors in the test oracle and was recorded
        # `done — validated`; the next run skipped straight past it to the
        # merge, leaving a cluster that had scored 93/103 an iteration earlier.
        _baseline_broken = bool(getattr(c_result, "baseline_broken", False))
        if _baseline_broken:
            # The stop reason is a claim about a MOMENT: the validate loop
            # above runs AFTER the fill loop stopped, and it reverts, heals
            # and refills. On 2026-08-18 (`nightly`, run `3579062-476abc3b`)
            # validate had just PASSED when this line asserted the tree "does
            # not build" — the refusal itself was right (zero scored sweeps
            # is not a finished cluster), but its premise must be re-read
            # from the tree, not from the fill loop's exit reason.
            if validate_ok:
                self.tel.log(
                    f"  {tag} [skeleton-C] cluster NOT checkpointed — the "
                    "fill loop stopped RED with zero scored sweeps; the "
                    "validate loop has since repaired the tree, but an "
                    "unscored cluster is unfinished, and the next run "
                    "re-enters it to score what validate rebuilt.")
            else:
                self.tel.log(
                    f"  {tag} [skeleton-C] cluster NOT checkpointed — the "
                    "fill loop stopped on a tree that does not build, so "
                    "this is an unfinished cluster, not a done one; the "
                    "next run re-enters it against the same tree.")
        return {"ok": not _baseline_broken, "phase_c": c_result,
                "phase_d": d_result,
                "validates": validate_ok and not _baseline_broken}

    def _git_ancestor_blobs(self, repo: Path, repo_rel: str) -> list[bytes]:
        """Committed versions of `repo_rel` in the workspace git repo, newest
        first — the fill tree is checkpoint-committed, so a parseable ancestor
        is exactly what would have saved the pre-corruption 22-fill state."""
        import subprocess
        try:
            r = subprocess.run(
                ["git", "-C", str(repo), "log", "--format=%H", "--", repo_rel],
                capture_output=True, text=True, errors="replace", timeout=30)
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
        """Build the heal collaborators, or None when no parse gate is wired
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
                text = fp.read_text(encoding="utf-8")
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
            # A revert that leaves the file unparseable is worse than the broken
            # body it replaced: it kills the whole cluster's compile, so every
            # unit of the coming pass fails for a reason none of them caused and
            # the run reads 0 passing with no localizable culprit. The
            # transaction restores the pre-image and the file goes to the
            # heal hierarchy instead.
            _rt = TreeTxn(filled_dir, validate_parse=(
                heal_deps.parse_ok if heal_deps is not None else None))
            _rt.write(rel_f, text)
            if not _rt.close().ok:
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



    def _heal_rel(self, project_dir: Path, filled_dir: Path,
                  skeleton_dir: Path, cluster_name: str, rel: str) -> bool:
        """The heal hierarchy on one file of the filled tree, for the TDD
        driver's boundary invariant. True when some restore point held."""
        deps = self._heal_deps(project_dir, filled_dir, skeleton_dir,
                               cluster_name)
        if deps is None:
            return False
        return heal_file(filled_dir, rel, cluster_name,
                         deps).point is not RestorePoint.NONE


    def _establish_crate_root(self, tag: str, project_dir: Path,
                              cluster: BuildCluster, filled_dir: Path,
                              cargo_toml: Path) -> None:
        """Make ``filled_dir`` the crate cargo builds: its own manifest, the
        workspace ``tests/`` behind a symlink, and the gen_tests siblings with
        their declarations. Idempotent.

        Called where the seed establishes the tree, before the first cargo
        command on it. It used to run at the start of the fill, after the
        ownership backfill's compile gate and the pre-pass heal, so on a fresh
        tree both built the workspace crate above it: eight `reverted 0` heal
        rounds and `STILL BROKEN` on every fresh cluster entry, and a backfill
        gate that read the tree as already broken and so could never roll
        back (replayed 2026-10-17 on checkpoint `18fb6a03d6`)."""
        if cargo_toml.exists() and not (filled_dir / "Cargo.toml").exists():
            shutil.copy(cargo_toml, filled_dir / "Cargo.toml")
        _proj_tests = project_dir / "tests"
        _filled_tests = filled_dir / "tests"
        if _proj_tests.exists() and not _filled_tests.exists():
            try:
                os.symlink(_proj_tests, _filled_tests)
            except OSError as e:
                self.tel.log(f"  {tag} [skeleton-C] tests symlink failed: {e}")

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
                _parsed = parse_gen_tests_filename(gt.name)  # structured parse
                if not _parsed:
                    continue
                _parent_stem, _pass_n = _parsed
                # Skip + quarantine undeclared orphans (on disk but absent
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









    @staticmethod
    def _cluster_check_cmd(cluster: BuildCluster, *,
                           all_targets: bool = False) -> str:
        """Build the cargo check command. Cluster-specific flags are passed via
        env_extra (RUSTFLAGS etc.), not via --features — that distinction depends
        on which cfg names are real Cargo features vs --cfg flags, and
        task0_cfg_scan has already encoded the right form in env_extra. Toolchain
        is applied via `cargo +nightly` when required.

        `all_targets` builds the TEST binaries too. That is what a health
        VERDICT needs and what the per-fn inner loop must not pay for on every
        splice: on 2026-08-01 `nightly` stopped with two rustc errors in its test
        binaries and was still recorded `done — validated`, because a plain
        `cargo check` builds only the lib and cannot observe the failing
        targets.
        """
        # Default message format (not --message-format=short) so the model sees
        # rustc's `note:` and `help:` hints alongside the bare error, which often
        # points directly at the fix (e.g. "consider importing one of these
        # traits: ...").
        parts = ["cargo", "check"]
        if cluster.toolchain:
            parts = ["cargo", f"+{cluster.toolchain}"] + parts[1:]
        if all_targets:
            parts.append("--all-targets")
        return shlex.join(parts)

    def _cluster_env(self, cluster: BuildCluster) -> dict[str, str]:
        """The full cargo env for `cluster` — the mapping handed to the TDD
        loop, the fill engine, the assert probe and the coverage instrument.

        This used to be `self.cargo.env()` (the ACTIVE scope's env, which
        task2 never activates, so base only) with `env_extra` merged by
        `dict.update`: RUSTFLAGS was REPLACED by the cluster's `--cfg`
        (dropping `-A warnings`) and RUSTUP_TOOLCHAIN was never set. The
        sweep did not notice because its command line carries
        `cargo +nightly`; every cargo the loop composes from the env alone
        ran on stable with the nightly cfg on and failed E0554 — the assert
        probe printed `produced no observation (rc=101)` on every nightly
        sweep of run 216855-03b2a21b (2026-09-02), and the coverage
        instrument would have failed the same way once it inherited the
        env. The port's `cluster_env` appends and sets the toolchain.
        """
        return self.cargo.cluster_env(cluster)
