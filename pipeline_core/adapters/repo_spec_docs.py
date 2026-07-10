"""Mtime-cached loader for the repository-specification documents
(genesis.md, functional_requirements_{module}.md, the api-surface artifact).

Relocated from pipeline.py (P3 step 51): file IO + a process-lifetime cache —
an adapter concern. The prompt-side RENDERING stays in prompts/repo_spec.
"""

from __future__ import annotations

from pathlib import Path

from pipeline_core.adapters.artifacts import api_surface_path
from pipeline_core.adapters.pipeline_db import genesis_rules_read
from pipeline_core.domain.model_notes import strip_model_notes_section
from pipeline_core.prompts.repo_spec import RepoSpecDocs

FR_PREFIX = "functional_requirements_"

_CACHE: dict[str, tuple[tuple[float, float, float], RepoSpecDocs]] = {}


def load_repo_spec_docs(project_dir: Path, module_name: str) -> RepoSpecDocs:
    genesis_p = project_dir / "genesis.md"
    fr_p = project_dir / f"{FR_PREFIX}{module_name}.md"
    api_p = api_surface_path(project_dir, module_name)
    mtimes = tuple(p.stat().st_mtime if p.exists() else 0.0
                   for p in (genesis_p, fr_p, api_p))
    key = str(project_dir / module_name)
    cached = _CACHE.get(key)
    if cached and cached[0] == mtimes:
        return cached[1]

    def _read(p: Path) -> str:
        try:
            return p.read_text(encoding="utf-8") if p.exists() else ""
        except OSError:
            return ""

    docs = RepoSpecDocs(
        genesis_rules=genesis_rules_read(project_dir, module_scope=module_name or "*"),
        genesis=_read(genesis_p),
        functional_req=strip_model_notes_section(_read(fr_p)),
        api_surface=_read(api_p),
    )
    _CACHE[key] = (mtimes, docs)  # type: ignore[assignment]
    return docs
