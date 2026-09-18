"""Impure collaborators the prompt builders call.

The builders in this package are pure text→text. Three collaborators are
pipeline-bound and stay in pipeline.py — the file-view annotator, the
promoted-error rules (mutable run-state counters + FR-file I/O), and the
repo-spec section. Instead of three independently-rebindable module globals
set via `configure()`, the composition root binds ONE FROZEN, typed
:class:`PromptRuntimeDeps` once via :func:`bind`; the inert default keeps prompt
rendering working in unit tests without the pipeline. The thin module-level
facade is retained deliberately — the prompt builders are a stateless free-fn
assembly layer, so a single composition-root-bound port is the right seam."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


def _annotate_file_view_default(file_text: str, fn_name: str) -> str:
    return file_text


def _promoted_error_rules_default(fr_path: Path | None = None) -> str:
    return ""


def _repo_spec_section_default(
    fr_path: Path | None = None, *, file_rel: str = "", fn_name: str = "",
    container: str = "", char_budget: int | None = None,
) -> str:
    return ""


@dataclass(frozen=True, slots=True)
class PromptRuntimeDeps:
    """The pipeline-bound collaborators the prompt builders call. Frozen + typed;
    bound once at the composition root (replaces three mutable globals +
    configure())."""
    annotate_file_view: Callable[[str, str], str] = _annotate_file_view_default
    promoted_error_rules: Callable[[Path | None], str] = _promoted_error_rules_default
    repo_spec_section: Callable[..., str] = _repo_spec_section_default


_DEPS: PromptRuntimeDeps = PromptRuntimeDeps()


def bind(deps: PromptRuntimeDeps) -> None:
    """Bind the real collaborators once at the composition root."""
    global _DEPS
    _DEPS = deps


def annotate_file_view(file_text: str, fn_name: str) -> str:
    """File view with stub/TARGET annotations (pipeline's `_annotate_file_view`)."""
    return _DEPS.annotate_file_view(file_text, fn_name)


def promoted_error_rules(fr_path: Path | None = None) -> str:
    """RECURRING_ERRORS block for promoted error codes (pipeline's `_promoted_error_rules`)."""
    return _DEPS.promoted_error_rules(fr_path)


def repo_spec_section(
    fr_path: Path | None = None, *, file_rel: str = "", fn_name: str = "",
    container: str = "", char_budget: int | None = None,
) -> str:
    """REPOSITORY_SPECIFICATIONS body for a fill/fix target (pipeline's
    `_repo_spec_section`: FR + genesis + API, relevance-selected, budgeted).
    Inert ("") until the composition root binds the provider."""
    return _DEPS.repo_spec_section(fr_path, file_rel=file_rel, fn_name=fn_name,
                                   container=container, char_budget=char_budget)
