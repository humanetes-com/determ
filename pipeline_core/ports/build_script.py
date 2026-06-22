"""BuildScriptReader port (#23) — the build-script cfg facts the cluster planner
needs to tell reachable clusters from phantom source atoms.

Narrow, ISP-respecting: one read, parameterised by toolchain. The real
:class:`pipeline_core.adapters.build_script.CargoBuildScriptReader` runs
``cargo check`` and parses the emitted ``output``; tests inject a fake.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..domain.build_script import BuildScriptCfg


class BuildScriptReader(Protocol):
    """Captures what ``build.rs`` emits for ``project_dir`` under a given
    toolchain (``None`` = the default/stable toolchain). Returns ``None`` when the
    toolchain is unavailable or the crate has no build script."""

    def emitted_cfgs(self, project_dir: Path, toolchain: str | None) -> BuildScriptCfg | None: ...
