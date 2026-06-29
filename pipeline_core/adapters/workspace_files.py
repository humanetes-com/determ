"""Source-file lifecycle on the project workspace (adapter).

The model-generated source files' write / delete / lock lifecycle plus the
manifest + locked.txt ledgers that track them. The ONLY layer that mutates the
project tree for generated code. Pure-policy bits (path normalisation,
protected-file rules) stay inline here because they're inseparable from the
write; the well-known names/extensions come from `domain.workspace_layout`.

Logging goes through the ambient `pipeline_core.log` facade (wired to the real
sink at the composition root), so these functions take no logger argument.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.adapters.artifacts import load_manifest
from pipeline_core.adapters.discovery import TEST_DIR_NAMES
from pipeline_core.domain.rust_scan import replace_elision_placeholders
from pipeline_core.domain.workspace_layout import (
    LOCKED_MODULES_DIR,
    SOURCE_EXTENSIONS,
)
from pipeline_core.io import write
from pipeline_core.log import log as _log

# Files that must never be overwritten regardless of what the model generates.
_PROTECTED = {"README.md", "readme.md", "README.rst", "LICENSE", "LICENSE.md",
              "LICENSE.txt", "Cargo.lock"}
# Crate sub-directories a bare `crate/file.rs` is NOT missing a `src/` for.
_RUST_SOURCE_DIRS = {"src", "tests", "benches", "examples", "build"}


def delete(path: Path | str) -> None:
    p = Path(path)
    if p.exists():
        p.unlink()


def save_manifest(manifest_path: Path, files: list[str]) -> None:
    write(manifest_path, "\n".join(files))


def load_locked_modules(project_dir: Path) -> set[str]:
    """The set of module names that have been locked (tests pass, files frozen)."""
    p = project_dir / LOCKED_MODULES_DIR / "locked.txt"
    if not p.exists():
        return set()
    return {line.strip() for line in p.read_text().splitlines() if line.strip()}


def lock_module(project_dir: Path, module_name: str, module_dir: Path,
                manifest_path: Path) -> int:
    """Lock a module: copy its generated files to ``.locked_modules/<name>/`` as
    a permanent backup, then record the module name in ``locked.txt``. The live
    files remain on disk so the compiler can use them as dependencies for later
    modules. Returns the number of files locked."""
    lock_dir = project_dir / LOCKED_MODULES_DIR / module_name
    lock_dir.mkdir(parents=True, exist_ok=True)

    module_rel = module_dir.relative_to(project_dir)
    files = load_manifest(manifest_path)
    locked_count = 0

    for rel_path in files:
        full = project_dir / rel_path
        if not full.exists():
            continue
        try:
            Path(rel_path).relative_to(module_rel)  # only files under this module's dir
        except ValueError:
            continue
        dest = lock_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(full.read_text())
        locked_count += 1

    locked = load_locked_modules(project_dir)
    locked.add(module_name)
    lock_txt = project_dir / LOCKED_MODULES_DIR / "locked.txt"
    lock_txt.parent.mkdir(parents=True, exist_ok=True)
    lock_txt.write_text("\n".join(sorted(locked)) + "\n")

    _log(f"  LOCKED '{module_name}' ({locked_count} file(s) → {LOCKED_MODULES_DIR}/{module_name}/)")
    return locked_count


def write_source_files(files_dict: dict[str, str], project_dir: Path,
                       manifest_path: Path, existing_test_files: set[str]) -> int:
    """Write model-generated source files to disk and record them in the
    manifest. Skips test files and protected/non-source project files
    (README/LICENSE/Cargo.toml/…) that must never be overwritten. Returns the
    number of files actually written."""
    written = []
    skipped = []
    for rel_path, content in files_dict.items():
        # Strip leading slashes — the model sometimes emits absolute-looking
        # paths; Path's `/` discards the left operand when the right is absolute.
        rel_path = rel_path.lstrip("/")
        # Normalise Rust source paths: insert src/ for a file sitting directly
        # under a crate dir; treat */src/main.rs as a mislabelled lib.rs.
        if Path(rel_path).suffix == '.rs':
            parts = Path(rel_path).parts
            if len(parts) == 2 and parts[0] not in _RUST_SOURCE_DIRS:
                fixed = str(Path(parts[0]) / 'src' / parts[1])
                _log(f"  PATH NORMALISED: {rel_path} → {fixed}")
                rel_path = fixed
            if rel_path.endswith('/src/main.rs') or rel_path == 'src/main.rs':
                lib_path = rel_path[:-len('main.rs')] + 'lib.rs'
                if (project_dir / lib_path).exists():
                    _log(f"  PATH NORMALISED: {rel_path} → {lib_path} (lib crate stub present)")
                    rel_path = lib_path
        # `{ ... }` placeholder bodies → `{ todo!() }` so the build check compiles.
        if Path(rel_path).suffix == '.rs' and '...' in content:
            content = replace_elision_placeholders(content)
        if not rel_path:
            continue
        if rel_path in existing_test_files:
            skipped.append(rel_path)
            continue
        # The model must never create test files.
        if any(part in TEST_DIR_NAMES for part in Path(rel_path).parts):
            skipped.append(rel_path)
            continue
        if Path(rel_path).name in _PROTECTED and (project_dir / rel_path).exists():
            skipped.append(rel_path)
            continue
        # Protect any existing non-source file (Cargo.toml, *.toml, *.json, …).
        p = Path(rel_path)
        if p.suffix not in SOURCE_EXTENSIONS and (project_dir / p).exists():
            skipped.append(rel_path)
            _log(f"  PROTECTED (non-source file): {rel_path}")
            continue
        write(project_dir / rel_path, content)
        written.append(rel_path)
        _log(f"  wrote {rel_path}")
    if skipped:
        _log(f"  SKIPPED (would overwrite test/config files): {', '.join(skipped)}")
    # Merge with files written in previous inner-loop attempts so the manifest
    # always tracks the full accumulated set on disk.
    existing = load_manifest(manifest_path)
    merged = list(dict.fromkeys(existing + written))  # dedup, preserve order
    save_manifest(manifest_path, merged)
    return len(written)


def delete_source_files(project_dir: Path, manifest_path: Path,
                        module_dir: Path | None = None,
                        existing_test_files: set[str] | None = None) -> None:
    """Delete model-generated files (tracked in the manifest). When
    ``module_dir`` is given, also sweep it for untracked NON-test ghost source
    files (written by the fix loop but never added to the manifest)."""
    files = load_manifest(manifest_path)
    deleted = 0
    deleted_paths = set()
    for rel_path in files:
        full = project_dir / rel_path
        if full.exists():
            full.unlink()
            deleted += 1
            deleted_paths.add(str(full))
    delete(manifest_path)

    if module_dir is not None:
        src_dir = module_dir / "src"
        if src_dir.exists():
            for candidate in src_dir.rglob("*"):
                if not candidate.is_file():
                    continue
                if candidate.suffix not in SOURCE_EXTENSIONS:
                    continue
                if str(candidate) in deleted_paths:
                    continue
                rel = str(candidate.relative_to(project_dir))
                if existing_test_files and rel in existing_test_files:
                    continue
                # Only delete a file with no test blocks — a ghost impl file.
                try:
                    content = candidate.read_text(encoding="utf-8")
                except OSError:
                    continue
                if "#[cfg(test)]" not in content and "#[test]" not in content:
                    candidate.unlink()
                    deleted += 1
                    _log(f"  Deleted ghost file (untracked by manifest): {rel}")

    _log(f"  Deleted {deleted} generated source file(s).")
