"""The probe-runner boundary: build and run one program as an example of the
original crate (`adapters.behaviour_probe.run_probe`).

Typed so a keyword added at a call site is checked against the bound
implementation: `build=` (which build of the original runs the program) was
added on 2026-10-31, and a `Callable[..., ProbeResult]` port would have let a
caller pass it to a runner that does not take it.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from pipeline_core.domain.behaviour_probe import ProbeBuild, ProbeResult
from pipeline_core.domain.models import BuildCluster

__all__ = ["RunProbe"]


class RunProbe(Protocol):
    """Build and run `program` as the example `name` of the crate at
    `project_dir` under `cluster`'s env, plus `extra_env`, as `build` asks."""

    def __call__(
        self, project_dir: Path, cluster: BuildCluster | None, program: str, name: str,
        *, timeout: int = ..., extra_env: Mapping[str, str] | None = ...,
        build: ProbeBuild = ...,
    ) -> ProbeResult: ...
