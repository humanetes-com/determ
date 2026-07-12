"""Transactional, parse-gated fill-tree writes (F1).

Every splice into a `.skeleton-filled` tree MUST leave the file parseable. A
locator bug once wrote fn bodies INTO a `///` doc-comment example, persisting an
unparseable `error.rs` that poisoned the whole cluster for ~30h (the write went
through unchecked). This is the single write primitive that enforces the
invariant: write the new bytes, ask the syn-backed validator whether the file
still parses, and if not, restore the pre-image and report failure — the bytes
on disk never persist an unparseable state.

`validate_parse` is injected (the composition root binds
`StructureReader.parse_ok`, backed by the extractor's `--parse-check`); a `None`
validator means "no gate" (pure unit tests that don't need syn).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

# A `(path) -> parses?` predicate. Production binding is the syn extractor via
# the StructureReader port; never brace-counting (syn is the standard parser).
ParseValidator = Callable[[Path], bool]


def parse_guarded_write(
    path: Path, new_bytes: bytes, pre_bytes: bytes,
    validate_parse: ParseValidator | None,
) -> bool:
    """Atomically commit `new_bytes` to `path` IFF the result parses.

    Writes `new_bytes`; if `validate_parse` is given and reports the file no
    longer parses, restores `pre_bytes` and returns False (the caller maps this
    to `SpliceReason.parse_broken`). Returns True when committed. With no
    validator the write always commits (backward-compatible unit-test path)."""
    path.write_bytes(new_bytes)
    if validate_parse is not None and not validate_parse(path):
        path.write_bytes(pre_bytes)
        return False
    return True
