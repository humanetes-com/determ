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
crash text, for the units it can edit, under its own lead
(`prompts.sections.refuted_body_block`). Every
writer reads it through :func:`refuted_text_for` on the live ledger, so a
refutation written mid-fill reaches the next writer of the same fill,
including the retry of a conversation the settlement opened under. The ask
and landing fingerprints read the same notes through
:func:`with_refuted_notes`, so a unit asked before the refutation counts as
asked under new evidence, and both stamps compare like with like.

The note states only history: that body was taken off the tree at
iteration N because a test process crashed on it, and if the body the writer
is shown now is that same text, it is the one that crashed. That is true in
every tree state, so no reader has to know what is on the tree. An earlier
version omitted the note when the current body equalled the refuted one, to
keep a lead that said "the body on the tree is NOT the quoted one" true; the
file, per-impl and pair routes compared against `FnRecord.body_text` read
when the fill planned, so a body STUBbed by a settlement later in the same
fill still compared equal and its note was hidden, and the equality hid the
evidence exactly when the crashing bytes were back on the tree.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from pipeline_core.domain.body_ledger import LedgerEntry, is_crash_refutation
from pipeline_core.domain.crash_settlement import refuted_crash_note
from pipeline_core.domain.impl_routing import norm_src_rel
from pipeline_core.domain.models import FnLocator

__all__ = [
    "refuted_notes",
    "refuted_text_for",
    "with_refuted_notes",
]


def _key(file: str, fn_name: str) -> tuple[str, str]:
    return norm_src_rel(file), fn_name


def refuted_notes(entries: Iterable[LedgerEntry],
                  oracle_fp: str) -> dict[FnLocator, str]:
    """Per unit, the note for its LATEST crash refutation under `oracle_fp`
    (the same row `latest_crash_refutation` selects): the body that crashed
    and the crash. A row graded under another oracle describes other tests
    and is not told. Keyed by the row's own locator. Told whatever body is
    on the tree now: the note claims nothing about it."""
    latest: dict[tuple[str, str], LedgerEntry] = {}
    for e in entries:
        if is_crash_refutation(e, oracle_fp):
            latest[_key(e.key.file, e.key.fn_name)] = e
    return {
        FnLocator(e.key.file, e.key.fn_name): refuted_crash_note(
            "\n\n".join(e.bodies), e.crash_evidence, e.iteration,
            unit=f"{e.key.file}::{e.key.fn_name}")
        for e in latest.values()}


def refuted_text_for(entries: Iterable[LedgerEntry], oracle_fp: str,
                     units: Iterable[tuple[str, str]]) -> str:
    """The notes of the `(file, fn_name)` units one writer can edit, each
    once, in the order given. "" when none of them was refuted on a crash."""
    notes = {_key(loc.file, loc.fn_name): text
             for loc, text in refuted_notes(entries, oracle_fp).items()}
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
