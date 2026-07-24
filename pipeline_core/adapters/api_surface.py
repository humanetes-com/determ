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

from pipeline_core.domain.models import FnRecord
from pipeline_core.domain.private_surface import pub_fields_authorable
from pipeline_core.domain.pub_items import pub_items_from_records
from pipeline_core.domain.rust_elide import elide_fn_bodies, elide_private_struct_fields


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
    # Freeze-scope switch, same value the bake classifies under: when the model
    # designs public structs' private fields, this artefact must not carry the
    # original's — it is read long after strip, when nothing can correct it.
    author_private_fields: bool = False

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

        # Body elision runs off the extractor's byte spans, not a text scan: a
        # scanner only recognises a body whose `{` shares the declaration's
        # line, so every `where`-clause / multi-line-generic fn leaked its body
        # verbatim into an artefact that TASK 1 reads AFTER strip. Records are
        # keyed by the extractor's own relative path.
        # Keyed by RESOLVED path, not by the extractor's relative string: the
        # two only agree if this walk and the extractor's input root happen to
        # render paths identically, and a silent key miss would degrade back to
        # leaking bodies with no signal.
        _fn_dicts = self.enumerate_fns(walk_root) or []
        _fn_records = [FnRecord.from_dict(d) for d in _fn_dicts]
        # Enumerated once, up front: the same records drive the type manifest
        # below AND (when field authoring is on) the names whose field blocks
        # this artefact must not disclose.
        type_records = self.enumerate_types(walk_root) or []
        _elide_fields = frozenset(
            t.name for t in type_records
            if self.author_private_fields and pub_fields_authorable(t)
            and t.is_pub and t.is_exactly_pub)
        by_file: dict[Path, list[FnRecord]] = {}
        for _r in _fn_records:
            if _r.file:
                by_file.setdefault((walk_root / _r.file).resolve(), []).append(_r)
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
            # Elide FIRST: the spans index the file exactly as the extractor
            # read it, so any rewrite must be applied to the result. The text
            # condenser still runs afterwards for type-level shaping, but it is
            # no longer what keeps bodies out.
            _recs = by_file.get(src_file.resolve(), [])
            if _recs:
                text = elide_fn_bodies(text, _recs)
            # Drop any gen_tests declarations the pipeline added.
            text = self.strip_gen_tests_decls(text)
            # Text-positional, so it runs AFTER the span-based body elision.
            text = elide_private_struct_fields(text, _elide_fields)
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
        if _elide_fields:
            self.log(f"  API surface: private fields elided for "
                     f"{len(_elide_fields)} public struct(s) (model-authored): "
                     f"{', '.join(sorted(_elide_fields))}.")
        self.log(f"  Captured API surface: {file_count} file(s) → "
                 f"{out_path.relative_to(project_dir)} ({out_path.stat().st_size:,} chars).")

        # Deterministic type-name manifest (syn-extracted, same walk_root) so TASK 1
        # knows "what types exist" up front instead of guessing it.
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
            # Same walk_root, same extraction the elision pass already did.
            pub_items = sorted(pub_items_from_records(type_records, _fn_dicts))
            if pub_items:
                items_path = self.pub_items_manifest_path(project_dir, module_name)
                items_path.write_text(json.dumps(pub_items, indent=2), encoding="utf-8")
                self.log(f"  Captured pub-items manifest: {len(pub_items)} item(s) → "
                         f"{items_path.relative_to(project_dir)}.")
        return out_path
