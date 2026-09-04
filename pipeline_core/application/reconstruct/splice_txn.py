"""The single-file parse-gated write, re-exported from :mod:`tree_txn`.

Every splice into a `.skeleton-filled` tree MUST leave the file parseable. A
locator bug once wrote fn bodies INTO a `///` doc-comment example, persisting an
unparseable `error.rs` that poisoned the whole cluster for ~30h (the write went
through unchecked). `parse_guarded_write` is the primitive that enforces the
invariant for the authored splicers: write the new bytes, ask the syn-backed
validator whether the file still parses, and if not, restore the pre-image and
report failure — the bytes on disk never persist an unparseable state.

The primitive now lives in `tree_txn.py` beside the multi-file transaction
every MECHANICAL writer uses, so the two contracts share one implementation;
this module keeps the import path the splicers and their tests already use.

`validate_parse` is injected (the composition root binds
`StructureReader.parse_ok`, backed by the extractor's `--parse-check`); a `None`
validator means "no gate" (pure unit tests that don't need syn).
"""

from __future__ import annotations

from pipeline_core.application.reconstruct.tree_txn import (
    ParseValidator,
    parse_guarded_write,
)

__all__ = ["ParseValidator", "parse_guarded_write"]
