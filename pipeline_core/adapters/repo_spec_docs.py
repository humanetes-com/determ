"""Mtime-cached loader for the repository-specification documents
(genesis.md, functional_requirements_{module}.md, the api-surface artifact).

Relocated from pipeline.py: file IO + a process-lifetime cache —
an adapter concern. The prompt-side RENDERING stays in prompts/repo_spec.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pipeline_core.adapters.artifacts import api_surface_path
from pipeline_core.adapters.pipeline_db import genesis_rules_read
from pipeline_core.domain.model_notes import strip_model_notes_section
from pipeline_core.prompts.repo_spec import RepoSpecDocs

FR_PREFIX = "functional_requirements_"

_CACHE: dict[str, tuple[tuple[float, float, float], RepoSpecDocs]] = {}


def make_repo_spec_section(default_budget: int) -> Callable[..., str]:
    """Build the REPOSITORY_SPECIFICATIONS prompt provider bound to a default
    char budget. Derives project_dir and module from fr_path
    (functional_requirements_{module}.md). Fail-safe: any problem → ""
    (the prompt renders without the section)."""
    from pipeline_core.prompts.repo_spec import (
        identifiers_for_target,
        render_repo_spec,
    )

    def repo_spec_section(
        fr_path: Path | None = None, *, file_rel: str = "", fn_name: str = "",
        container: str = "", char_budget: int | None = None,
    ) -> str:
        try:
            if fr_path is None:
                return ""
            fr_path = Path(fr_path)
            if not fr_path.name.startswith(FR_PREFIX) or not fr_path.name.endswith(".md"):
                return ""
            module = fr_path.name[len(FR_PREFIX):-3]
            docs = load_repo_spec_docs(fr_path.parent, module)
            return render_repo_spec(
                docs, file_rel=file_rel,
                identifiers=identifiers_for_target(fn_name, container, file_rel),
                char_budget=char_budget or default_budget,
            )
        except Exception:
            return ""

    return repo_spec_section


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
