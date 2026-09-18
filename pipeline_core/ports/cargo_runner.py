"""The application-facing cargo boundary.

Distinct from :class:`ports.Cargo` (the raw one-command subprocess port the
ADAPTERS own): a `CargoRunner` is the pipeline's SEMANTIC view — combined
legacy text for prompts/logs, and the json-diagnostic composition — the level
every engine dep and every scripted test actually works at. Making it a port
turns the implicit "the two module fns everyone monkeypatches" seam into an
explicit, injectable concept on `App`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from pipeline_core.domain.models import Diagnostic

__all__ = ["CargoRunner"]


@runtime_checkable
class CargoRunner(Protocol):
    def run(self, cmd: str, cwd: Path, env: dict[str, str],
            timeout: int = 180) -> tuple[int, str]: ...

    def check_diag(self, cmd: str, cwd: Path, env: dict[str, str],
                   timeout: int = 180) -> tuple[int, str, list[Diagnostic]]: ...
