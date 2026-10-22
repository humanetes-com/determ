"""The tree a fill's gates judge from: the bytes the last verdict left.

A fill is sweep -> runs -> sweep. Every route measures a baseline, writes,
measures again and keeps or reverts; the keep is judged against the
baseline. That judgment is only worth something when the baseline was read
on the outcome of the previous verdicts. Run `4012759-5c5177c6`
(2026-10-21/22, cluster `stable`, fill 9) broke it: the joint ask spliced a
reverted candidate onto the shared tree before the fill and judged it only
after the fill, so every baseline of that fill read 337 against the
sweep's 343. A pair body that broke the same three tests read as clean, a
file run kept five bodies on "all were failing at baseline", the next sweep
read 339 and the batch ratchet reverted four files, taking the fill's one
cashed session with them.

The fact is a property of the tree when a baseline is read, not of any one
writer, so it is held here as data: the accepted bytes of each file.
`accept` replaces the stored bytes of the files a verdict left changed and
leaves every other file alone, so a keep in one file cannot store another
worker's in-flight write in a second file. `diff` names the files whose
bytes on the tree are not the accepted ones. No I/O: the caller reads the
tree and writes the restore.

`MeasureRole` is the other half. One measurement function serves two uses:
the PREMISE a run is judged against (taken before the run writes) and the
JUDGMENT of a write the run has made. A check that refuses a tree that
differs from the accepted bytes is right for the first and fires on the
very write the second is measuring, so every caller names its use and
there is no default.
"""
from __future__ import annotations

import enum
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field

__all__ = [
    "FillGround",
    "GroundBreak",
    "GroundRoleError",
    "MeasureRole",
    "UnjudgedWrite",
    "tree_digest",
]


class MeasureRole(enum.Enum):
    """What a suite measurement is for. A premise is read on the accepted
    tree before the run writes; a judgment is read on a write the run has
    made."""

    PREMISE = "premise"
    JUDGMENT = "judgment"


class UnjudgedWrite(Exception):  # noqa: N818 — named for the fact, like the log line
    """Bytes no verdict produced were found on the tree and could not be put
    back. The run that met them is ended reverted; the fill continues to its
    next sweep, which reads whatever the restore could not remove."""

    def __init__(self, files: tuple[str, ...], why: str) -> None:
        super().__init__(f"unjudged write in {', '.join(files) or '?'}: {why}")
        self.files = files
        self.why = why


class GroundRoleError(RuntimeError):
    """A measurement asked outside the hold of the tree's splice lock while
    a fill's ground is bound: its baseline would include whatever another
    worker has spliced and not yet judged. A programming error at the call
    site, never a reading."""


def _sha(data: bytes | None) -> str:
    return "-" if data is None else hashlib.sha256(data).hexdigest()[:16]


def tree_digest(files: Mapping[str, bytes | None]) -> str:
    """One identity for a set of file images, path order independent."""
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(_sha(files[rel]).encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class GroundBreak:
    """Files whose bytes differ from the accepted ones, with both shas so a
    log line can say what was found where."""

    files: tuple[str, ...]
    accepted: Mapping[str, str] = field(default_factory=dict)
    found: Mapping[str, str] = field(default_factory=dict)

    def restricted(self, files: frozenset[str]) -> GroundBreak | None:
        """This break limited to ``files``; None when none of them differ."""
        keep = tuple(f for f in self.files if f in files)
        if not keep:
            return None
        return GroundBreak(
            keep, accepted={f: self.accepted[f] for f in keep if f in self.accepted},
            found={f: self.found[f] for f in keep if f in self.found})

    def payload(self) -> dict[str, object]:
        return {"files": list(self.files),
                "accepted": dict(self.accepted), "found": dict(self.found)}


@dataclass(slots=True)
class FillGround:
    """The accepted bytes per file (crate-relative, `src/...`). A file absent
    from the tree is stored as None; a file the ground has never seen is
    absent from it, and a new file on the tree is a difference."""

    _bytes: dict[str, bytes | None] = field(default_factory=dict)

    @classmethod
    def open(cls, files: Mapping[str, bytes | None]) -> FillGround:
        return cls(dict(files))

    def stored(self, rel: str) -> bytes | None:
        return self._bytes.get(rel)

    def knows(self, rel: str) -> bool:
        return rel in self._bytes

    @property
    def identity(self) -> str:
        return tree_digest(self._bytes)

    def diff(self, current: Mapping[str, bytes | None],
             ignore: frozenset[str] = frozenset()) -> GroundBreak | None:
        """The files whose bytes on `current` are not the accepted ones; None
        when the tree is the accepted tree. A file in `ignore` is not
        compared: the reader could not read it, so its bytes are unknown,
        not absent."""
        names = sorted((set(self._bytes) | set(current)) - ignore)
        bad = tuple(rel for rel in names
                    if self._bytes.get(rel) != current.get(rel))
        if not bad:
            return None
        return GroundBreak(
            bad,
            accepted={rel: _sha(self._bytes.get(rel)) for rel in bad},
            found={rel: _sha(current.get(rel)) for rel in bad})

    def accept(self, files: Mapping[str, bytes | None]) -> None:
        """Store `files` as accepted; every other file keeps its bytes."""
        for rel, data in files.items():
            self._bytes[rel] = data
