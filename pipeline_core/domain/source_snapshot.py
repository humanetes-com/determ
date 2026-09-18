"""A revertible snapshot of the reconstructed source tree.

The task3 inline retry mutates `src/**/*.rs` in place and must be able to UNDO
those edits when a retry leaves the tree worse than it found it (more failing
tests, or a tree that no longer compiles). That capture-and-restore was an
inline `dict[Path, bytes]` + two raw loops. :class:`SourceSnapshot` names it: an
immutable capture you can `restore()`, testable without the rest of the retry.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """The bytes of every `*.rs` file under a root at capture time."""

    files: Mapping[Path, bytes]

    @classmethod
    def capture(cls, src_root: Path) -> SourceSnapshot:
        """Snapshot every `*.rs` under `src_root` (empty when it's absent). Only
        the files the retry might touch — `tests/` is excluded by the caller's
        choice of root, keeping the snapshot cheap."""
        files: dict[Path, bytes] = {}
        if src_root.is_dir():
            for p in src_root.rglob("*.rs"):
                with suppress(OSError):
                    files[p] = p.read_bytes()
        return cls(MappingProxyType(files))

    def __bool__(self) -> bool:
        return bool(self.files)

    def restore(self) -> None:
        """Rewrite every captured file back to its snapshot bytes."""
        for path, data in self.files.items():
            with suppress(OSError):
                path.write_bytes(data)
