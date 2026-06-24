"""Downstream-API scanner adapter (P6 relocation).

Scans the workspace's `.rs` source for `use <module>::…` imports (and PascalCase
types named in `functional_requirements.md` code fences) to infer the public API a
leaf module must export — context for crates with no upstream locked deps to learn
from. Filesystem-reading ⇒ an adapter; all Rust parsing goes through the structured
`domain.rust_*` scanners (no regex on source). The `use`-path / code-fence parses
read workspace text, not the crate's own source-of-truth, and stay structured.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.adapters.discovery import SKIP_DIR_NAMES
from pipeline_core.domain.rust_idents import camelcase_type_names
from pipeline_core.domain.rust_scan import module_use_imports, rust_code_fences


def scan_downstream_api(project_dir: Path, module_name: str) -> str:
    """
    Scan all .rs source files in the workspace for 'use <module_name>::...' imports.
    Returns a formatted hint telling the model what the module must publicly export.
    Used to give argh_shared (and similar leaf crates) context about their API contract
    even though they have no upstream locked deps to learn from.
    """
    direct_imports: set[str] = set()
    submodule_imports: dict[str, set[str]] = {}

    for p in sorted(project_dir.rglob("*.rs")):
        rel = p.relative_to(project_dir)
        if any(part in SKIP_DIR_NAMES for part in rel.parts):
            continue
        try:
            text = p.read_text(errors='replace')
        except Exception:
            continue

        brace_groups, submod_brace, submod_single = module_use_imports(text, module_name)

        # use module::{A, B, C}
        for inner in brace_groups:
            for item in inner.split(','):
                item = item.strip()
                if item:
                    direct_imports.add(item)

        # use module::submod::{A, B}
        for submod, inner in submod_brace:
            for item in inner.split(','):
                item = item.strip()
                if item:
                    submodule_imports.setdefault(submod, set()).add(item)

        # use module::submod::Item  (single item, no braces)
        for submod, item in submod_single:
            if submod[0].islower() and item[0].isupper():
                submodule_imports.setdefault(submod, set()).add(item)

    # Also scan functional_requirements.md for type names used in code blocks that look like
    # internal types (not imported from outside). These must be defined in the module itself.
    internal_types: set[str] = set()
    func_req_path = project_dir / "functional_requirements.md"
    if func_req_path.exists():
        try:
            func_text = func_req_path.read_text(errors='replace')
            # Find type names in code blocks that are not imported from elsewhere
            # Look for PascalCase identifiers in Rust signatures inside ```rust blocks
            for code in rust_code_fences(func_text):
                # Find PascalCase identifiers
                for name in camelcase_type_names(code):
                    # Skip if it's a stdlib/known type or already in our direct/submodule imports
                    if name in ('Result', 'Option', 'String', 'Vec', 'Ok', 'Err', 'Some', 'None',
                                'Box', 'Arc', 'Rc', 'Fn', 'FnMut', 'FnOnce', 'Send', 'Sync',
                                'Self', 'Args', 'bool', 'usize', 'str'):
                        continue
                    if name in direct_imports:
                        continue
                    if any(name in v for v in submodule_imports.values()):
                        continue
                    internal_types.add(name)
        except Exception:
            pass

    if not direct_imports and not submodule_imports and not internal_types:
        return ""

    lines = [
        f"\n⚠️  REQUIRED PUBLIC API for `{module_name}` (inferred from workspace source files):",
        f"Downstream crates import from `{module_name}` as follows — you MUST match these paths exactly:",
    ]

    if submodule_imports:
        lines.append("")
        lines.append("Sub-module paths (must be separate .rs files declared with `pub mod` in lib.rs):")
        for submod in sorted(submodule_imports.keys()):
            exports = ', '.join(sorted(submodule_imports[submod]))
            lines.append(f"  {module_name}/src/{submod}.rs  →  pub mod {submod};  // exports: {exports}")
        lines.append("")
        lines.append("lib.rs MUST contain these exact pub mod declarations:")
        for submod in sorted(submodule_imports.keys()):
            lines.append(f"  pub mod {submod};")

    if direct_imports:
        lines.append("")
        lines.append(f"Types imported directly from the crate root (`use {module_name}::TypeName`):")
        for item in sorted(direct_imports):
            lines.append(f"  pub use ...::{item};  // or define directly in lib.rs")

    if internal_types:
        lines.append("")
        lines.append("Internal types referenced in functional requirements — MUST be defined in lib.rs at the crate root:")
        lines.append("⚠️  DO NOT put these in submodules. Define them directly in lib.rs so all submodules can use `crate::TypeName`.")
        for item in sorted(internal_types):
            lines.append(f"  pub struct {item}; // define in lib.rs, NOT in submodules")

    return '\n'.join(lines) + '\n'
