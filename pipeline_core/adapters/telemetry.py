"""LogTelemetry — the concrete `ports.reconstruct.Telemetry`.

Routes the reconstruction use-cases' log/emit/set_current through the ambient
:mod:`pipeline_core.log` facade (wired to the real sinks once by the composition
root's ``set_sinks``) and the state adapter. Replaces the `_ReconstructAdapter`
telemetry pass-through methods so the use-cases receive a real adapter (DI)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..log import emit, log
from .state import set_current


class LogTelemetry:
    def log(self, msg: str) -> None:
        log(msg)

    def emit(self, event: str, **fields: Any) -> None:
        emit(event, **fields)

    def set_current(self, project_dir: Path, label: str) -> None:
        set_current(project_dir, label)
