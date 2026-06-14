"""TASK-2 source generators (P4) — Clean-Architecture port of the pipeline.py
`Task2Generator` family.

Each generator is a use-case that turns functional requirements + tests into
source files, depending only on narrow role ports (`ports.generator`): the LLM,
the workspace, the per-module context docs, telemetry. `pipeline.py` is the
composition root (`_Task2Adapter` + `_task2_generator_factory`); tests inject
fakes. Prompt-section text and file extraction stay pure (built here / imported
from `domain`); the model call, file writes, and context reads cross ports.

Grown strangler-fig: generators move in here one slice at a time as their helper
surface is bound behind ports.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...domain.generation import (
    Task2Config,
    strip_test_prefix,
    testfile_paths,
    topo_sort_files,
)
from ...domain.llm_output import extract_files
from ...domain.rust_scan import item_names
from ...domain.text import (
    extract_code_content,
    smart_truncate,
    strip_list_marker,
    trim_to_line,
)
from ...ports.generator import LLM, ContextDocs, Telemetry, Workspace
from ...retry import with_retry

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
    def generate(
        self,
        project_dir: Path,
        genesis_path: Path,
        functional_req_path: Path,
        manifest_path: Path,
        existing_test_files: Any,
        project_profile: str = "",
        workspace_structure: str = "",
        build_error: str = "",
        locked_dep_context: str = "",
        current_module: str = "",
        module_dir_rel: str = "",
        whole_project_context: str = "",
        coverage_strategy: str = "",
        api_surface: str = "",
        prev_source_context: str = "",
    ) -> bool:
        """Write source files to disk. Returns True on success.

        api_surface (when provided) is the pre-strip public-API skeleton. TASK 1
        already received it; passing it here too prevents loss-in-translation if
        TASK 1's requirements doc dropped any signature — TASK 2 sees the ground
        truth directly.
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

    def generate(
        self, project_dir: Path, genesis_path: Path, functional_req_path: Path,
        manifest_path: Path, existing_test_files: Any, project_profile: str = "",
        workspace_structure: str = "", build_error: str = "", locked_dep_context: str = "",
        current_module: str = "", module_dir_rel: str = "", whole_project_context: str = "",
        coverage_strategy: str = "", api_surface: str = "", prev_source_context: str = "",
    ) -> bool:
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

    def generate(
        self, project_dir: Path, genesis_path: Path, functional_req_path: Path,
        manifest_path: Path, existing_test_files: Any, project_profile: str = "",
        workspace_structure: str = "", build_error: str = "", locked_dep_context: str = "",
        current_module: str = "", module_dir_rel: str = "", whole_project_context: str = "",
        coverage_strategy: str = "", api_surface: str = "", prev_source_context: str = "",
    ) -> bool:
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

    def generate(
        self, project_dir: Path, genesis_path: Path, functional_req_path: Path,
        manifest_path: Path, existing_test_files: Any, project_profile: str = "",
        workspace_structure: str = "", build_error: str = "", locked_dep_context: str = "",
        current_module: str = "", module_dir_rel: str = "", whole_project_context: str = "",
        coverage_strategy: str = "", api_surface: str = "", prev_source_context: str = "",
    ) -> bool:
        self.tel.log("\nTASK 2: Implementing source code (static file list)")
        genesis = self.ws.read(genesis_path)
        functional_req = self.ws.read(functional_req_path)

        # Get file list from pre-strip snapshot — no LLM needed.
        file_paths = self.ws.load_original_structure(project_dir, current_module) if current_module else []

        if not file_paths:
            self.tel.log("  No saved file structure — falling back to LLM file-list generator.")
            return LLMTask2Generator(
                tel=self.tel, llm=self.llm, ws=self.ws, docs=self.docs, cfg=self.cfg,
            ).generate(
                project_dir, genesis_path, functional_req_path, manifest_path,
                existing_test_files, project_profile, workspace_structure, build_error,
                locked_dep_context, current_module, module_dir_rel, whole_project_context,
                coverage_strategy=coverage_strategy,
            )

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
