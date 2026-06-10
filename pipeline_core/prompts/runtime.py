"""Impure collaborators the prompt builders call (slice 8).

The builders in this package are pure text→text. Two collaborators are
pipeline-bound and stay in pipeline.py — the file-view annotator (regex-based;
no regex in moved code per the project rule) and the promoted-error rules
(mutable run-state counters + FR-file I/O). The composition root wires them
here via :func:`configure` (same settable-facade pattern as
``pipeline_core.log``); the defaults are inert so prompt rendering works in
unit tests without the pipeline.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


def _annotate_file_view_default(file_text: str, fn_name: str) -> str:
    return file_text


def _promoted_error_rules_default(fr_path: Path | None = None) -> str:
    return ""


_annotate_file_view: Callable[[str, str], str] = _annotate_file_view_default
_promoted_error_rules: Callable[[Path | None], str] = _promoted_error_rules_default


def configure(
    *,
    annotate_file_view: Callable[[str, str], str],
    promoted_error_rules: Callable[[Path | None], str],
) -> None:
    """Wire the facade to the pipeline's real collaborators. Called once at
    the composition root."""
    global _annotate_file_view, _promoted_error_rules
    _annotate_file_view = annotate_file_view
    _promoted_error_rules = promoted_error_rules


def annotate_file_view(file_text: str, fn_name: str) -> str:
    """File view with stub/TARGET annotations (pipeline's `_annotate_file_view`)."""
    return _annotate_file_view(file_text, fn_name)


def promoted_error_rules(fr_path: Path | None = None) -> str:
    """RECURRING_ERRORS block for promoted error codes (pipeline's `_promoted_error_rules`)."""
    return _promoted_error_rules(fr_path)
