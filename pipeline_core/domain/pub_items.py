"""Pure computation of a Rust crate's public-item surface from already-
enumerated type/fn records.

Shared by the pre-strip bake capture (`adapters/api_surface.py`, called on the
ORIGINAL source before strip — Hard Rule 11's one-time read) and the
post-generation conformance check (`application/validate.py`, called on the
REGENERATED source) so both sides compare the output of the SAME structured
extractor instead of one side re-deriving its set via regex over a condensed
text listing (which cannot distinguish a real top-level `pub` item from the
same syntax appearing inside a `///` doc-comment usage example — debt #64).
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.domain.models import TypeRecord
from pipeline_core.domain.workspace_layout import SKIP_DIR_NAMES

# Aux directories are consumers of the public API, not part of it.
_AUX_DIR_NAMES = frozenset({"examples", "benches", "tests"})


def _excluded(rel: str) -> bool:
    parts = set(Path(rel).parts)
    return bool(parts & _AUX_DIR_NAMES) or bool(parts & SKIP_DIR_NAMES)


def pub_items_from_records(
    type_records: list[TypeRecord],
    fn_records: list[dict[str, object]],
) -> set[tuple[str, str]]:
    """{(kind, name)} for every pub struct/enum/trait/fn, excluding
    examples/benches/tests/skip-dirs. `fn_records` entries are the extractor's
    dict shape: `{"file": str, "fn_name": str, "is_pub": bool, ...}`."""
    out: set[tuple[str, str]] = set()
    for t in type_records:
        if t.is_pub and t.kind in {"struct", "enum", "trait"} and not _excluded(t.file):
            out.add((t.kind, t.name))
    for f in fn_records:
        fn_file = str(f.get("file", ""))
        fn_name = str(f.get("fn_name", ""))
        if f.get("is_pub") and not _excluded(fn_file):
            out.add(("fn", fn_name))
    return out
