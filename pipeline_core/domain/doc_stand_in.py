"""Which reference doc test a re-hosted doc example stands for (pure domain).

The crate's doc examples are re-hosted before strip as ordinary oracle tests,
`tests/doc_<stem>_gen_<N>.rs` (`domain.doc_example_test`), because the
reconstruction loop cannot work on a doc comment. The reference floor measures
the ORIGINAL crate, where those examples run as rustdoc doc tests
(`src/lib.rs - Context (line 543)`). Nothing recorded that the two are the same
example, so the floor reported every reference doc test absent from the
reconstruction as a gap while its re-hosted copy passed in the oracle: on run
`391506-3403a6a7` (2026-10-22) task 3 ended green with "26 doc test(s) the
reference passes are absent here" and all 26 re-hosted examples passing.

The record is written where the example is known, by the emitter
(:class:`DocOrigin`, one per fenced block, kept or not), and joined to the
reference by the line rustdoc names the doc test with. rustdoc numbers a doc
test from the first doc line of the item it documents plus the opening fence's
line within the joined doc text. That is the extractor's record line plus the
fence offset for a doc written on an item, and `tests/integration/
test_doc_test_count_cfg_copies.py` pins the rule against real rustdoc. A doc
written inside a macro's token stream documents the item the expansion
produces, whose line no syntactic walk knows (measured 2026-11-02 on the
pristine anyhow tree: 24 of 26 doc tests at record line plus offset, the two
written in a `macro_rules!` body at its call site plus offset). For such a
record the base is unknown, and its examples join only at the one shift that
places every one of them on a distinct reference line of the same file that
nothing else took; no shift, or more than one, and the record does not join.

A reference doc test that joins nothing, or joins an example the emitter did
not keep, stays reported, with the reason the record holds.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

#: The emitter's store of origins, at the project root. Outside `tests/`: the
#: lines it holds are the original's, and test files reach prompts.
ORIGINS_STORE = ".doc_example_origins.json"

_ORIGINS_SCHEMA = 1

Pair = tuple[str, str]


@dataclass(frozen=True, slots=True)
class DocOrigin:
    """One fenced doc block of the original, and what the emitter did with it.

    `index` counts every fence of the item's doc that rustdoc treats as a doc
    test, runnable or not. `kept_as` / `test_fn` name the oracle file and its
    test when the example was kept; `sha` is the hash of the part of that file
    the renderer wrote (`doc_example_test.rendered_part`), so a file changed
    after emission is not taken for the example. `reason` says why an example
    was not kept."""

    file: str
    item_line: int
    index: int
    offset: int
    in_macro_body: bool = False
    kept_as: str = ""
    test_fn: str = ""
    sha: str = ""
    reason: str = ""

    @property
    def line(self) -> int | None:
        """The line rustdoc names this doc test by, or None when the record
        cannot know it (a doc written in a macro body)."""
        return None if self.in_macro_body else self.item_line + self.offset

    @property
    def stand_in(self) -> Pair | None:
        """The oracle pair cargo reports for the kept example."""
        if not self.kept_as:
            return None
        return (f"tests/{self.kept_as}", self.test_fn)


@dataclass(frozen=True, slots=True)
class DocCover:
    """What stands for one reference doc test here: its re-hosted example's
    pair, or the reason there is none."""

    stand_in: Pair | None = None
    gap: str = ""


def origins_to_json(origins: Iterable[DocOrigin]) -> dict[str, object]:
    return {"schema": _ORIGINS_SCHEMA, "origins": [
        {"file": o.file, "item_line": o.item_line, "index": o.index,
         "offset": o.offset, "in_macro_body": o.in_macro_body,
         "kept_as": o.kept_as, "test_fn": o.test_fn, "sha": o.sha,
         "reason": o.reason}
        for o in origins]}


def origins_from_json(raw: object) -> tuple[DocOrigin, ...] | None:
    """The stored origins, or None for a store this code did not write."""
    if not isinstance(raw, dict) or raw.get("schema") != _ORIGINS_SCHEMA:
        return None
    out: list[DocOrigin] = []
    for d in raw.get("origins") or []:
        if not isinstance(d, dict):
            return None
        out.append(DocOrigin(
            file=str(d.get("file", "")), item_line=int(d.get("item_line", 0)),
            index=int(d.get("index", 0)), offset=int(d.get("offset", 0)),
            in_macro_body=bool(d.get("in_macro_body", False)),
            kept_as=str(d.get("kept_as", "")), test_fn=str(d.get("test_fn", "")),
            sha=str(d.get("sha", "")), reason=str(d.get("reason", ""))))
    return tuple(out)


def doc_test_file(name: str) -> str:
    """The source file of a doc test's name, raw or normalized:
    `src/lib.rs - Context (line 543)`, `src/lib.rs - Context#1` and the
    crate-level `src/lib.rs -#0` all give `src/lib.rs`."""
    return name.split(" -", 1)[0].strip()


def join_origins(doc_lines: Mapping[Pair, tuple[str, int]],
                 origins: Sequence[DocOrigin]) -> dict[Pair, DocOrigin]:
    """Each reference doc test (normalized pair → `(file, line)` rustdoc
    named it by) to the origin of the example at that place, where one is.

    A record whose base is known joins at its line and nowhere else. A record
    written in a macro body joins at its unique shift (module docstring); two
    such records whose unique shifts land on the same line both stay out."""
    by_file: dict[str, dict[int, Pair]] = {}
    for pair, (file, line) in doc_lines.items():
        by_file.setdefault(file, {})[line] = pair
    joined: dict[Pair, DocOrigin] = {}
    taken: set[tuple[str, int]] = set()
    for o in origins:
        if o.line is None:
            continue
        at = by_file.get(o.file, {}).get(o.line)
        if at is not None and (o.file, o.line) not in taken:
            joined[at] = o
            taken.add((o.file, o.line))
    records: dict[tuple[str, int], list[DocOrigin]] = {}
    for o in origins:
        if o.in_macro_body:
            records.setdefault((o.file, o.item_line), []).append(o)
    placed: dict[tuple[str, int], list[tuple[int, DocOrigin]]] = {}
    for (file, item_line), members in sorted(records.items()):
        free = {ln for ln in by_file.get(file, {}) if (file, ln) not in taken}
        first = item_line + members[0].offset
        shifts = [s for s in sorted({ln - first for ln in free})
                  if all(item_line + m.offset + s in free for m in members)]
        if len(shifts) == 1:
            placed[(file, item_line)] = [(item_line + m.offset + shifts[0], m)
                                         for m in members]
    claims: dict[tuple[str, int], int] = {}
    for (file, _item), lines in placed.items():
        for ln, _m in lines:
            claims[(file, ln)] = claims.get((file, ln), 0) + 1
    for (file, _item), lines in placed.items():
        if any(claims[(file, ln)] > 1 for ln, _m in lines):
            continue
        for ln, m in lines:
            joined[by_file[file][ln]] = m
    return joined


def doc_covers(doc_lines: Mapping[Pair, tuple[str, int]],
               origins: Sequence[DocOrigin],
               present: Mapping[str, str]) -> dict[Pair, DocCover]:
    """What stands for each reference doc test in `doc_lines`.

    `present` is the oracle's re-hosted files now, file name → hash of the
    rendered part. A joined, kept example whose file is gone or changed is
    not a stand-in."""
    joined = join_origins(doc_lines, origins)
    placed = {id(o) for o in joined.values()}
    files = {o.file for o in origins}
    unplaced: dict[str, int] = {}
    for left in origins:
        if left.in_macro_body and id(left) not in placed:
            unplaced[left.file] = unplaced.get(left.file, 0) + 1
    out: dict[Pair, DocCover] = {}
    for pair, (file, _line) in doc_lines.items():
        o = joined.get(pair)
        if o is None:
            out[pair] = DocCover(gap=_unjoined_gap(file, bool(origins), file in files,
                                                   unplaced.get(file, 0)))
        elif o.stand_in is None:
            out[pair] = DocCover(gap=o.reason or "its example was not kept")
        elif o.kept_as not in present:
            out[pair] = DocCover(gap=f"its re-hosted file {o.kept_as} is not in the oracle")
        elif present[o.kept_as] != o.sha:
            out[pair] = DocCover(gap=f"its re-hosted file {o.kept_as} changed after emission")
        else:
            out[pair] = DocCover(stand_in=o.stand_in)
    return out


def _unjoined_gap(file: str, any_origin: bool, file_has_origin: bool,
                  unplaced_macro: int) -> str:
    """Why a reference doc test matched no origin, most specific first."""
    if not any_origin:
        return "no doc-example origins recorded"
    if not file_has_origin:
        return f"no origin recorded for {file}"
    if unplaced_macro:
        return (f"no re-hosted example matches its line; {unplaced_macro} "
                f"fence(s) written in a macro body in {file} could not be "
                f"placed by one shift")
    return "no re-hosted example matches its line"
