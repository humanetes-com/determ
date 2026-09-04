"""One door to the filled tree for every write that is not a model's answer.

Every write into `.skeleton-filled/<cluster>/src` is one of two kinds. An
AUTHORED write is a model's body going in: it is allowed to leave the tree
compile-red, because the fix loop that owns it converses with the compiler
afterwards — but it must never leave a file unparseable (`splice.py`'s
`parse_guarded_write` gate). A MECHANICAL write carries no new information —
a re-seed of stored bodies, a revert to `unimplemented!()`, a layout
definition propagated from the skeleton, a donor body carried across
clusters, a `use` line rustc suggested — and must never leave the tree worse
than it found it, on EITHER axis: parse and, when the caller says the tree
built before, build.

Before this module those two kinds were enforced by four ad-hoc shapes: the
single-file parse gate, a diag-count whole-file gate with byte rollback
(`write_gate.py`), the layout-sync's compile gate with a hand-kept `_before`
dict, and a revert that parse-checked and escalated to heal — and three
mechanical writers had none of them. Run `2973-66e50ad9` (2026-09-04): the
cluster-entry re-seed wrote the same body span twice into `src/context.rs`,
the bisect's rollback re-derived spans by name over the wrecked text, and
the tree stayed unparseable through a cluster abort. The rollback here is a
BYTE restore of a pre-image captured before the first touch, never a
re-derivation; it cannot depend on the text being sane.

`TreeTxn` is the transaction: `write()` captures a pre-image on first touch,
`verify()` parse-checks every touched file through the live structure reader
and then runs the optional build check, `rollback()` puts every pre-image
back, `commit()` forgets them. `parse_guarded_write` is the same contract for
one file and one write, kept because the authored splicers need exactly that.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType

# A `(path) -> parses?` predicate. Production binding is the syn extractor via
# the StructureReader port; never brace-counting (syn is the standard parser).
ParseValidator = Callable[[Path], bool]
# `() -> (ok, error excerpt)`; the caller binds the cluster and the command.
BuildCheck = Callable[[], tuple[bool, str]]


@dataclass(frozen=True, slots=True)
class PreImage:
    """A file's bytes before a transaction touched it. `data is None` when
    the file did not exist; `parsed is None` when no validator was wired."""

    path: Path
    data: bytes | None
    parsed: bool | None


def capture(path: Path, validate_parse: ParseValidator | None) -> PreImage:
    if not path.exists():
        return PreImage(path, None, None)
    data = path.read_bytes()
    parsed = None if validate_parse is None else bool(validate_parse(path))
    return PreImage(path, data, parsed)


def restore(pre: PreImage) -> None:
    """Put the pre-image back, byte for byte. A file that did not exist is
    removed again."""
    if pre.data is None:
        pre.path.unlink(missing_ok=True)
        return
    pre.path.parent.mkdir(parents=True, exist_ok=True)
    pre.path.write_bytes(pre.data)


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


def unparseable_files(src_dir: Path, validate_parse: ParseValidator,
                      ) -> tuple[str, ...]:
    """Every `.rs` under `src_dir` the structure reader cannot parse, as
    paths relative to `src_dir`, sorted. The boundary invariant's reading:
    a file in this list has no enumerable unit, so no fill route can open
    it and no sweep can attribute its failure to a body."""
    out: list[str] = []
    for p in sorted(src_dir.rglob("*.rs")):
        if not validate_parse(p):
            out.append(str(p.relative_to(src_dir)))
    return tuple(out)


@dataclass(frozen=True, slots=True)
class TxnVerdict:
    """What `verify()` found. `broken` names the touched files that parsed
    before the transaction and do not parse now; `build_error` is the build
    check's excerpt when it refused."""

    ok: bool
    broken: tuple[str, ...] = ()
    build_error: str = ""

    @property
    def reason(self) -> str:
        if self.ok:
            return ""
        if self.broken:
            return "unparseable after write: " + ", ".join(self.broken)
        first = self.build_error.strip().splitlines()
        return "does not build: " + (first[0] if first else "?")


@dataclass(slots=True)
class TreeTxn:
    """A set of file edits under `src_dir` that can be verified and undone
    as one. Paths are relative to `src_dir`, the way the extractor and cargo
    spell them when `src_dir` is the crate root.

    Not re-entrant on the same file: a second transaction opened on a file
    this one has touched captures the FIRST transaction's write as its
    pre-image, which is what a nested per-unit bisect wants (it restores to
    the state its parent left, not to the run's start)."""

    src_dir: Path
    validate_parse: ParseValidator | None = None
    build_check: BuildCheck | None = None
    _pre: dict[str, PreImage] = field(default_factory=dict)
    _order: list[str] = field(default_factory=list)
    _closed: bool = False

    # --- reading and writing --------------------------------------------

    def path(self, rel: str) -> Path:
        return Path(self.src_dir) / rel

    def read(self, rel: str) -> str:
        return self.path(rel).read_text(encoding="utf-8")

    def write(self, rel: str, text: str) -> None:
        self.write_bytes(rel, text.encode("utf-8"))

    def write_bytes(self, rel: str, data: bytes) -> None:
        if self._closed:
            raise RuntimeError("write on a closed transaction")
        if rel not in self._pre:
            self._pre[rel] = capture(self.path(rel), self.validate_parse)
            self._order.append(rel)
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    @property
    def touched(self) -> tuple[str, ...]:
        return tuple(self._order)

    def pre_image(self, rel: str) -> PreImage | None:
        return self._pre.get(rel)

    # --- the transaction ---------------------------------------------------

    def verify(self) -> TxnVerdict:
        """Parse every touched file, then run the build check if one is wired.

        A file that did not parse BEFORE the transaction is not held against
        it — the transaction did not make it worse — but a file that parsed
        and no longer does is a refusal regardless of what the build says."""
        broken: list[str] = []
        if self.validate_parse is not None:
            for rel in self._order:
                pre = self._pre[rel]
                if pre.parsed is False:
                    continue
                p = self.path(rel)
                if p.exists() and not self.validate_parse(p):
                    broken.append(rel)
        if broken:
            return TxnVerdict(False, tuple(broken))
        if self.build_check is not None:
            ok, err = self.build_check()
            if not ok:
                return TxnVerdict(False, (), err)
        return TxnVerdict(True)

    def rollback(self) -> int:
        """Restore every touched file's pre-image, last touch first. Returns
        the number of files restored. The pre-images are KEPT, so a second
        rollback after further edits restores the same state again."""
        for rel in reversed(self._order):
            restore(self._pre[rel])
        return len(self._order)

    def commit(self) -> None:
        self._pre.clear()
        self._order.clear()
        self._closed = True

    def close(self) -> TxnVerdict:
        """Verify; commit when clean, roll back otherwise. The verdict says
        which, and its `reason` is ready for a log line."""
        verdict = self.verify()
        if verdict.ok:
            self.commit()
        else:
            self.rollback()
            self._closed = True
        return verdict

    def __enter__(self) -> TreeTxn:
        return self

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc: BaseException | None, tb: TracebackType | None) -> None:
        # An exception mid-transaction leaves the tree exactly as found; a
        # normal exit leaves the decision to `close()`/`commit()`, because
        # the verify may be the caller's own (a cargo check it already runs).
        if exc_type is not None and not self._closed:
            self.rollback()
            self._closed = True
