"""WorkspaceArtifacts — the concrete `ports.reconstruct.Artifacts`.

The read-side view of the project workspace the reconstruction phases consult:
the per-module API surface, the generated-file manifest, locked-dependency
source, and context-block formatting. Relocated from pipeline.py's free
functions (`load_manifest`/`load_api_surface`/`load_locked_source_context`/
`_format_whole_project_context`/`_api_surface_path`) + the `_ReconstructAdapter`
binding so the use-cases receive a real adapter directly (DI).

Adapter layer: file I/O only; imports stdlib + domain/strip layout vocabulary,
never pipeline.py."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..domain.workspace_layout import LOCKED_MODULES_DIR
from ..io import read
from .strip import ORIGINAL_STRUCTURE_DIR


def api_surface_path(project_dir: Path, module_name: str) -> Path:
    """Path where the pre-strip API surface for a module is saved."""
    return project_dir / ORIGINAL_STRUCTURE_DIR / f"{module_name}.api.md"


def load_manifest(manifest_path: Path) -> list[str]:
    """The generated-source manifest as a list of relative paths (blank-skipped)."""
    content = read(manifest_path)
    if not content:
        return []
    return [line for line in content.splitlines() if line.strip()]


def load_api_surface(project_dir: Path, module_name: str) -> str:
    """The saved pre-strip API surface for a module, or '' if absent."""
    p = api_surface_path(project_dir, module_name)
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def load_locked_source_context(project_dir: Path, dep_names: set[str]) -> str:
    """The actual source of every locked dependency module, as a prompt context
    block so the model uses exact types/signatures instead of inventing them.
    '' when there are no locked deps."""
    if not dep_names:
        return ""
    parts: list[str] = []
    for dep_name in sorted(dep_names):
        lock_dir = project_dir / LOCKED_MODULES_DIR / dep_name
        if not lock_dir.exists():
            continue
        for f in sorted(lock_dir.rglob("*")):
            if not f.is_file() or f.name == "locked.txt":
                continue
            rel = f.relative_to(lock_dir)
            try:
                content = f.read_text()
                parts.append(f"=== {dep_name}/{rel} ===\n{content}")
            except OSError:
                continue
    if not parts:
        return ""
    return (
        "--- Locked dependency source (already generated and tests passing — "
        "use these exact types, signatures, and module paths in your implementation) ---\n\n"
        + "\n\n".join(parts)
    )


def format_whole_project_context(test_files: Any, reason: str = "") -> str:
    """Format all test files as a TASK 2 context block (used when isolated-module
    context is insufficient: 0-test modules, or stalled improvement)."""
    if not test_files:
        return ""
    header = reason or "Full project test suite provided for additional context."
    parts = [f"\n--- WHOLE-PROJECT CONTEXT: {header} ---"]
    for rel, content in test_files:
        parts.append(f'\n<TESTFILE path="{rel}">\n{content}\n</TESTFILE>')
    parts.append("\n--- END WHOLE-PROJECT CONTEXT ---\n")
    return "\n".join(parts)


class WorkspaceArtifacts:
    """Read-side workspace artefacts for the reconstruction phases."""

    def read(self, path: Path) -> str:
        return read(path)

    def load_api_surface(self, project_dir: Path, module_name: str) -> str:
        return load_api_surface(project_dir, module_name)

    def load_manifest(self, manifest_path: Path) -> list[str]:
        return load_manifest(manifest_path)

    def load_locked_source_context(self, project_dir: Path, dep_names: set[str]) -> str:
        return load_locked_source_context(project_dir, dep_names)

    def format_whole_project_context(self, test_files: Any, reason: str = "") -> str:
        return format_whole_project_context(test_files, reason)

    def collect_source_context(self, project_dir: Path, manifest_path: Path,
                               exclude: set[str]) -> str:
        out = ""
        for rel in load_manifest(manifest_path):
            if rel in exclude:
                continue
            sp = project_dir / rel
            if sp.exists():
                out += f'\n<FILE path="{rel}">\n{sp.read_text(encoding="utf-8")}\n</FILE>\n'
        return out
