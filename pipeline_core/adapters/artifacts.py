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

import json
from pathlib import Path
from typing import Any

from ..domain.workspace_layout import LOCKED_MODULES_DIR
from ..io import read
from .strip import ORIGINAL_STRUCTURE_DIR


def api_surface_path(project_dir: Path, module_name: str) -> Path:
    """Path where the pre-strip API surface for a module is saved."""
    return project_dir / ORIGINAL_STRUCTURE_DIR / f"{module_name}.api.md"


def pub_items_manifest_path(project_dir: Path, module_name: str) -> Path:
    """Path where the pre-strip STRUCTURED pub-item manifest is saved — the
    same structured extraction used on the regenerated side of the TASK 2
    conformance check (debt #64: this replaces a regex over the human-
    readable `.api.md` listing, which cannot tell a real top-level `pub`
    item from the same syntax inside a `///` doc-comment example)."""
    return project_dir / ORIGINAL_STRUCTURE_DIR / f"{module_name}.pub_items.json"


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


def load_pub_items_manifest(
        project_dir: Path, module_name: str) -> set[tuple[str, str]] | None:
    """The saved pre-strip structured pub-item set, or None if absent (a
    project baked before this manifest existed) — the caller should skip
    the conformance check on None rather than fabricate an empty result."""
    p = pub_items_manifest_path(project_dir, module_name)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {(str(kind), str(name)) for kind, name in data}
    except (OSError, ValueError, TypeError):
        return None


def type_manifest_path(project_dir: Path, module_name: str) -> Path:
    """Path where the pre-strip deterministic type-name manifest is saved."""
    return project_dir / ORIGINAL_STRUCTURE_DIR / f"{module_name}.types.json"


def load_type_names(project_dir: Path, module_name: str) -> list[str]:
    """The saved pre-strip type-name list for a module (struct/enum/type/trait
    names, syn-extracted), or [] if absent. Deterministic ground truth for any
    prompt that needs to know 'what types exist' without guessing from LLM text."""
    p = type_manifest_path(project_dir, module_name)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


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


def load_original_structure(project_dir: Path, module_name: str) -> list[str]:
    """Load the saved pre-strip file list for a module. Returns [] if not found.

    Matches by .txt filename stem (exact), not by content prefix. The stem-based
    match is unambiguous: 'argh' never collides with 'argh_shared', regardless of
    iteration order."""
    out_dir = project_dir / ORIGINAL_STRUCTURE_DIR
    target = module_name.replace("-", "_")
    for candidate in out_dir.glob("*.txt"):
        stem = candidate.stem.replace("-", "_")
        if stem == target:
            content = candidate.read_text()
            return [ln.strip() for ln in content.splitlines() if ln.strip()]
    return []


def load_skeleton_files(skeleton_dir: Path) -> list[tuple[str, str]]:
    """Return [(relative_path, content), ...] for every .rs file under skeleton_dir."""
    out: list[tuple[str, str]] = []
    for p in sorted(skeleton_dir.rglob("*.rs")):
        try:
            out.append((str(p.relative_to(skeleton_dir)), p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def load_baked_leaf_order(project_dir: Path, cluster_name: str) -> dict[str, int]:
    """Load the `{fn_name: ref_count}` map saved during the bake step.
    Returns {} if absent — caller falls back to alphabetical order."""
    path = project_dir / ".skeleton" / cluster_name / "_leaf_order.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {str(k): int(v) for k, v in data.items() if isinstance(v, (int, float))}

