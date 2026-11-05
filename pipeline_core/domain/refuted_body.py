"""What the next writer of a unit is told about a body a crash settlement
took off it (pure domain).

THE FACT. A crash settlement that convicts a body puts it back (to a stub when
the settlement STUBs it, to the text it had before when it NARROWs a write)
and writes a `BodyState.REFUTED` ledger row carrying the body the stack ran
and the crash as the localizer read it (`_settle_record` in the TDD driver,
the only writer of that row shape). `latest_crash_refutation` is the
predicate for such a row.

WHY THIS MODULE EXISTS. The row's one prompt reader used to be the held-stub
branch of the sweep, which appended the note to the stub's `CRASH_EVIDENCE`
text. Two things were wrong with that, and both were measured.

- A filled unit was never told. Run `630350-93c5a6ee` (2026-11-05, cluster
  `stable`, fill 2): the owed `src/error.rs::context_drop_rest` got three
  file-route attempts, each one line from right; the second compiled and
  crashed (`heap-use-after-free`), and the settlement NARROWed it back to
  its previous text with a refuted row. Fill 3's per-fn re-fill on the same
  unit opened with no word of it and failed on `E0609`, the same error the
  first attempt had already hit.
- The note rode in the crash-evidence dict, which every writer reads through
  `CrashSites.evidence_for`. That returns the dict's text only while no
  frame of the unit's name resolved; once one did, the note was dropped.
  The same dict is the rank's `sited` set and the file route's aimed set,
  so a previous body's crash would also have graded the current body as a
  fault site.

So the note is its own channel: a writer appends it after rendering its own
crash text, for the units it can edit, under a lead that says the quoted body
is NOT the one on the tree (`prompts.sections.refuted_body_block`). Every
writer reads it through :func:`refuted_text_for` on the live ledger, so a
refutation written mid-fill reaches the next writer of the same fill,
including the retry of a conversation the settlement opened under. The ask
and landing fingerprints read the same notes through
:func:`with_refuted_notes`, so a unit asked before the refutation counts as
asked under new evidence, and both stamps compare like with like.

The lead is true only when the quoted body is not the body on the tree.
:func:`refuted_notes` takes that body (`on_tree`) and says nothing for a
unit whose current text is the refuted one: a later keep of those bytes is
the reseed gate's case, and quoting it as taken off would name the body the
writer is looking at.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from pipeline_core.domain.body_ledger import (
    LedgerEntry,
    canon_body,
    is_crash_refutation,
)
from pipeline_core.domain.crash_settlement import refuted_crash_note
from pipeline_core.domain.impl_routing import norm_src_rel
from pipeline_core.domain.models import FnLocator

__all__ = [
    "on_tree_bodies",
    "refuted_notes",
    "refuted_text_for",
    "with_refuted_notes",
]


def _key(file: str, fn_name: str) -> tuple[str, str]:
    return norm_src_rel(file), fn_name


def _body_of(rec: object) -> tuple[str, str, str] | None:
    """`(file, fn_name, body_text)` from an extractor dict or a record.
    None when the record does not carry a body, so a name-only row is not
    a claim about what is on the tree."""
    if isinstance(rec, Mapping):
        if "body_text" not in rec:
            return None
        return (str(rec.get("file") or ""), str(rec.get("fn_name") or ""),
                str(rec.get("body_text") or ""))
    if not hasattr(rec, "body_text"):
        return None
    return (str(getattr(rec, "file", "") or ""),
            str(getattr(rec, "fn_name", "") or ""),
            str(getattr(rec, "body_text", "") or ""))


def on_tree_bodies(records: Iterable[object],
                   ) -> dict[tuple[str, str], tuple[str, ...]]:
    """Current bodies keyed by `(file, fn)`, one tuple per unit, in the
    order `records` names the occurrences. The map :func:`refuted_notes`
    compares with the refuted row."""
    acc: dict[tuple[str, str], list[str]] = {}
    for rec in records:
        got = _body_of(rec)
        if got is None:
            continue
        file, fn, body = got
        if not file or not fn:
            continue
        acc.setdefault(_key(file, fn), []).append(body)
    return {k: tuple(v) for k, v in acc.items()}


def _quoted_on_tree(refuted: tuple[str, ...],
                    current: tuple[str, ...]) -> bool:
    """The body the row quotes is the body on the tree. Sets of folded
    occurrence texts, so whitespace and order do not count as a difference.
    One body the writer is editing matching any refuted occurrence is the
    same fact."""
    ref = {canon_body(b) for b in refuted if b.strip()}
    cur = {canon_body(b) for b in current if b.strip()}
    if not ref or not cur:
        return False
    return ref == cur or (len(cur) == 1 and cur <= ref)


def refuted_notes(entries: Iterable[LedgerEntry],
                  oracle_fp: str, *,
                  on_tree: Mapping[tuple[str, str], tuple[str, ...]] | None = None,
                  ) -> dict[FnLocator, str]:
    """Per unit, the note for its LATEST crash refutation under `oracle_fp`
    (the same row `latest_crash_refutation` selects): the body that crashed
    and the crash. A row graded under another oracle describes other tests
    and is not told. A unit whose `on_tree` body is that refuted body is
    not told either: the lead would say the quoted body is off the tree
    while the writer is looking at it. A unit absent from `on_tree` is
    told — the caller did not claim what is on the tree. `on_tree` None
    skips the comparison (a reservation that must not under-count). Keyed
    by the row's own locator."""
    current = ({_key(f, fn): bodies for (f, fn), bodies in on_tree.items()}
               if on_tree is not None else None)
    latest: dict[tuple[str, str], LedgerEntry] = {}
    for e in entries:
        if is_crash_refutation(e, oracle_fp):
            latest[_key(e.key.file, e.key.fn_name)] = e
    out: dict[FnLocator, str] = {}
    for e in latest.values():
        k = _key(e.key.file, e.key.fn_name)
        if (current is not None and k in current
                and _quoted_on_tree(e.bodies, current[k])):
            continue
        out[FnLocator(e.key.file, e.key.fn_name)] = refuted_crash_note(
            "\n\n".join(e.bodies), e.crash_evidence, e.iteration,
            unit=f"{e.key.file}::{e.key.fn_name}")
    return out


def refuted_text_for(entries: Iterable[LedgerEntry], oracle_fp: str,
                     units: Iterable[tuple[str, str]], *,
                     on_tree: Mapping[tuple[str, str], tuple[str, ...]] | None = None,
                     ) -> str:
    """The notes of the `(file, fn_name)` units one writer can edit, each
    once, in the order given. "" when none of them was refuted on a crash,
    or the body on the tree is the refuted one (`on_tree`)."""
    notes = {_key(loc.file, loc.fn_name): text
             for loc, text in refuted_notes(
                 entries, oracle_fp, on_tree=on_tree).items()}
    seen: dict[tuple[str, str], None] = {}
    for f, fn in units:
        k = _key(f, fn)
        if k in notes:
            seen.setdefault(k)
    return "\n\n".join(notes[k] for k in seen)


def with_refuted_notes(crash: Mapping[FnLocator, str],
                       notes: Mapping[FnLocator, str]
                       ) -> dict[FnLocator, str]:
    """The crash excerpt an ask or landing fingerprint reads: the sweep's
    crash text per unit with the unit's refuted note after it. Used ONLY for
    fingerprints, never as the crash-evidence dict a writer, the rank or the
    aimed set reads."""
    out = dict(crash)
    by_key = {_key(loc.file, loc.fn_name): loc for loc in out}
    for loc, text in notes.items():
        k = by_key.get(_key(loc.file, loc.fn_name), loc)
        out[k] = (out[k] + "\n\n" + text) if k in out else text
    return out
