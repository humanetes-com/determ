"""Pre-strip API-surface capture (P6 engine-relocation series).

Snapshots a module's public-API skeleton (signatures + type definitions, bodies
elided) to `.original_structure/<module>.api.md` and a deterministic type-name
manifest, BEFORE strip — the structural ground truth TASK 1 reads afterward
(Hard Rule 11: captured pre-strip, never re-derived from original source later).

Lifted verbatim out of the pipeline.py god-file; the composition root injects the
path helpers, the syn-backed condenser/extractor, and the log sink.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.domain.pub_items import pub_items_from_records


@dataclass(frozen=True, slots=True)
class ApiSurfaceCapture:
    api_surface_path: Callable[[Path, str], Path]
    type_manifest_path: Callable[[Path, str], Path]
    condense_to_signatures: Callable[..., str]
    strip_gen_tests_decls: Callable[[str], str]
    enumerate_types: Callable[[Path], list[Any] | None]
    is_test_file: Callable[[Path, Path], bool]
    skip_dir_names: frozenset[str]
    log: Callable[[str], None]
    enumerate_fns: Callable[[Path], list[dict[str, object]] | None] = lambda _: None
    pub_items_manifest_path: Callable[[Path, str], Path] | None = None

    def capture(self, project_dir: Path, module_dir: Path,
                module_name: str) -> Path | None:
        out_path = self.api_surface_path(project_dir, module_name)
        out_path.parent.mkdir(exist_ok=True)

        parts: list[str] = [
            f"# API surface — {module_name}",
            "",
            "Public-API skeleton captured before strip. Signatures and type definitions",
            "only (function bodies are elided as `{ /* ... */ }`). This is structural",
            "ground truth for TASK 1: any item NOT listed here did not exist in the",
            "original source. The behavioral semantics are still derived from tests —",
            "this document only defines the shape of the public API.",
            "",
        ]
        file_count = 0
        # Only walk the crate's src/ tree. examples/, benches/, tests/ are NOT
        # part of the public API — they are consumers of it.
        src_root = module_dir / "src"
        walk_root = src_root if src_root.is_dir() else module_dir
        for src_file in sorted(walk_root.rglob("*.rs")):
            rel = src_file.relative_to(project_dir)
            if any(part in self.skip_dir_names for part in rel.parts):
                continue
            if self.is_test_file(src_file, project_dir):
                continue
            # Defence in depth: skip example/bench/integration-test directories.
            if any(part in {"examples", "benches", "tests"} for part in rel.parts):
                continue
            try:
                text = src_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # Drop any gen_tests declarations the pipeline added.
            text = self.strip_gen_tests_decls(text)
            condensed = self.condense_to_signatures(text, filename=src_file.name).rstrip()
            if not condensed:
                continue
            parts.append(f'<FILE path="{rel}">')
            parts.append(condensed)
            parts.append("</FILE>")
            parts.append("")
            file_count += 1

        if file_count == 0:
            return None
        out_path.write_text("\n".join(parts), encoding="utf-8")
        self.log(f"  Captured API surface: {file_count} file(s) → "
                 f"{out_path.relative_to(project_dir)} ({out_path.stat().st_size:,} chars).")

        # Deterministic type-name manifest (syn-extracted, same walk_root) so TASK 1
        # knows "what types exist" up front instead of guessing it.
        type_records = self.enumerate_types(walk_root) or []
        type_names = sorted({t.name for t in type_records if t.name})
        if type_names:
            types_path = self.type_manifest_path(project_dir, module_name)
            types_path.write_text(json.dumps(type_names, indent=2), encoding="utf-8")
            self.log(f"  Captured type manifest: {len(type_names)} type(s) → "
                     f"{types_path.relative_to(project_dir)}.")

        # Structured pub-item manifest (same extraction as the type manifest
        # above, PLUS fns) — the TASK 2 conformance check compares against
        # THIS, not a regex over the human-readable listing above, which
        # cannot tell a real top-level `pub` item from the same syntax
        # inside a `///` doc-comment usage example.
        if self.pub_items_manifest_path is not None:
            fn_records = self.enumerate_fns(walk_root) or []
            pub_items = sorted(pub_items_from_records(type_records, fn_records))
            if pub_items:
                items_path = self.pub_items_manifest_path(project_dir, module_name)
                items_path.write_text(json.dumps(pub_items, indent=2), encoding="utf-8")
                self.log(f"  Captured pub-items manifest: {len(pub_items)} item(s) → "
                         f"{items_path.relative_to(project_dir)}.")
        return out_path
