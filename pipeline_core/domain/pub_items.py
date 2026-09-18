"""Pure computation of a Rust crate's public-item surface from already-
enumerated type/fn records.

Shared by the pre-strip bake capture (`adapters/api_surface.py`, called on the
ORIGINAL source before strip — Hard Rule 11's one-time read) and the
post-generation conformance check (`application/validate.py`, called on the
REGENERATED source) so both sides compare the output of the SAME structured
extractor instead of one side re-deriving its set via regex over a condensed
text listing (which cannot distinguish a real top-level `pub` item from the
same syntax appearing inside a `///` doc-comment usage example).
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
    dict shape: `{"file": str, "fn_name": str, "is_exactly_pub": bool, ...}`.

    Gated on `is_pub AND is_exactly_pub` — this is the crate's EXTERNAL API
    surface, so a `pub(crate)` item (is_pub folds it in) is never part of it.
    The conjunction, not `is_exactly_pub` alone: `is_exactly_pub` defaults
    True when ABSENT (records from a pre-field artifact or a stale extractor
    binary), and alone it would then promote fully-PRIVATE items to pub —
    `is_pub` is the backstop that can only ever narrow."""
    out: set[tuple[str, str]] = set()
    for t in type_records:
        if t.is_pub and t.is_exactly_pub and t.kind in {"struct", "enum", "trait"} \
                and not _excluded(t.file):
            out.add((t.kind, t.name))
    for f in fn_records:
        fn_file = str(f.get("file", ""))
        fn_name = str(f.get("fn_name", ""))
        # Missing key defaults True — same reading as the record classes'
        # from_dict, so an absent field degrades to pre-field behaviour
        # instead of silently dropping every pub fn from a stale artifact.
        if f.get("is_pub") and f.get("is_exactly_pub", True) and not _excluded(fn_file):
            out.add(("fn", fn_name))
    return out
