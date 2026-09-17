"""StructureReader port (P1) — the syn-extractor structure reads the fill engine needs.

A thin, dict-shaped view over the `Extractor` seam: `enumerate_fns` returns the wire
dicts the per-fn / per-impl engine consumes (the `.to_dict()` wrapping that used to live in
pipeline.py shims); the rest return their typed records. The composition root injects the
real :class:`pipeline_core.adapters.structure.CliStructureReader`; tests inject a fake —
NO monkeypatching of module globals (CODE_ARCHITECTURE.md §0, §4 P1).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from pipeline_core.domain.models import (
    ConstRecord,
    DispatchFamily,
    DocItemRecord,
    ModRecord,
    TypeRecord,
    UseRecord,
)


class StructureReader(Protocol):
    """Structure facts read from a live source tree via the syn extractor."""

    def enumerate_fns(self, src_dir: Path) -> list[dict[str, Any]] | None:
        """Fn records as wire dicts (engine contract), or None if unavailable."""
        ...

    def enumerate_types(self, src_dir: Path) -> list[TypeRecord] | None: ...
    def enumerate_mods(self, src_dir: Path) -> list[ModRecord] | None: ...
    def enumerate_uses(self, src_dir: Path) -> list[UseRecord] | None: ...
    def enumerate_consts(self, src_dir: Path) -> list[ConstRecord] | None: ...
    def enumerate_dispatch_families(self, src_dir: Path) -> list[DispatchFamily] | None: ...
    def enumerate_doc_items(self, src_dir: Path) -> list[DocItemRecord] | None:
        """Per-item doc text with its cfg chain (the doc-test inventory), or None."""
        ...

    def parse_ok(self, path: Path) -> bool:
        """True iff `path` parses as valid Rust (syn). The transactional splice
        gate (F1) calls this after a write; False → the write is rolled back.
        Fail-open: inconclusive checks (no binary) return True."""
        ...
