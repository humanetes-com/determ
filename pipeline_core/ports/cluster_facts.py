"""ClusterFactReader port (P5) — the source facts the cluster planner needs.

Narrow, ISP-respecting view over the syn extractor: just the three reads
``application.tasks.cfg_scan`` consumes to derive compiler clusters. The real
:class:`pipeline_core.adapters.extractor.CliExtractor` satisfies it structurally;
tests inject a fake. No I/O, no ``BuildCluster`` knowledge here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..domain.models import CfgOccurrence, ModRecord


class ClusterFactReader(Protocol):
    """The `#[cfg]` facts (occurrences, gated mod decls, nightly-feature gates)
    a cluster scan reads from a live pre-strip source tree."""

    def enumerate_cfgs(self, src_dir: Path, *, cfg_true: tuple[str, ...] = ...,
                       cfg_false: tuple[str, ...] = ...) -> list[CfgOccurrence] | None: ...

    def enumerate_mods(self, src_dir: Path, *, cfg_true: tuple[str, ...] = ...,
                       cfg_false: tuple[str, ...] = ...) -> list[ModRecord] | None: ...

    def enumerate_feature_gate_cfgs(self, src_dir: Path, *, cfg_true: tuple[str, ...] = ...,
                                    cfg_false: tuple[str, ...] = ...) -> list[str] | None: ...
