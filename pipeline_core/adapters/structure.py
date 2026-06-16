"""CliStructureReader (P1) — the real StructureReader over the Extractor seam.

Holds the `.to_dict()`-wrapping that used to live in pipeline.py's `_enumerate_*_via_extractor`
shims (anti-pattern A: logic in the god-file behind a monkeypatch seam). The composition root
constructs it once from the `Extractor` adapter and injects it; tests inject a fake.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.domain.models import ConstRecord, DispatchFamily, ModRecord, TypeRecord
from pipeline_core.ports import Extractor


@dataclass(frozen=True, slots=True)
class CliStructureReader:
    """Structure reads via the syn extractor. Satisfies ports.structure.StructureReader."""

    extractor: Extractor

    def enumerate_fns(self, src_dir: Path) -> list[dict[str, Any]] | None:
        """Fn records as wire dicts (the per-fn/per-impl engine contract); None if the
        extractor binary is unavailable. The seam returns typed FnRecords (slice 6); we
        serialise back via ``to_dict`` so the engine stays byte-identical. Each dict:
        file, fn_name, sig_text, body_text, sig_byte_start, body_byte_start,
        body_byte_end, cfg_predicates (list[str]), is_pub (bool), container
        ("" free fn | "impl ..." | "trait ..."), has_unimplemented_body (bool)."""
        recs = self.extractor.enumerate_fns(src_dir)
        return None if recs is None else [r.to_dict() for r in recs]

    def enumerate_types(self, src_dir: Path) -> list[TypeRecord] | None:
        return self.extractor.enumerate_types(src_dir)

    def enumerate_mods(self, src_dir: Path) -> list[ModRecord] | None:
        return self.extractor.enumerate_mods(src_dir)

    def enumerate_consts(self, src_dir: Path) -> list[ConstRecord] | None:
        return self.extractor.enumerate_consts(src_dir)

    def enumerate_dispatch_families(self, src_dir: Path) -> list[DispatchFamily] | None:
        return self.extractor.enumerate_dispatch_families(src_dir)
