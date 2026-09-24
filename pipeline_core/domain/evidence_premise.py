"""What a unit was TOLD, as a comparable fingerprint.

WHAT WAS MISSING. `body_ledger.flat_standing_unchanged` scopes a FLAT grade
("this body, tried under these convicting tests and this oracle, moved
nothing") to three clauses: the tree body is still the graded one, the oracle
is still the one that graded it, and no NEW test convicts the unit. All three
describe the WORLD AROUND the unit, and on a stalled tree none of them can
move — so a unit that lands a compiling, non-regressing, non-paying body is
retired from dispatch by its own success, permanently. Run
`837207-ce5b1647` (2026-09-15, cluster `stable`) is the measured cost: six
ledger rows, three of them flat on bodies the loop KEPT, and from iteration 3
the sweep read `dispatching 0 of 0 (0/0 this pass)` against `frontier=3
fn(s)` for three more hours until an external TERM ended the run. The
excluded set grew by exactly the units that succeeded in landing a body:
`src/fmt.rs::debug` (excluded from iter 2), `src/error.rs::fmt` (from iter 3),
`src/error.rs::msg` (from iter 1, on a row run `200802-b835274e` wrote).
`src/error.rs::context` landed and graded `unmeasured`, never flat, and was
never excluded — the grade is what does it, not the landing.

WHAT THIS ADDS. Re-asking a unit with the SAME evidence is genuinely the same
attempt and should be refused — that is the guard's correct half. Re-asking it
with evidence the previous attempt did not have is a different question, and
nothing in the ledger could tell the two apart. This module is that
distinction: a fingerprint of the evidence channels a unit's next prompt draws
from, stamped on the row beside `oracle_fp` / `convicting` and compared at the
next dispatch. The exclusion stands while the fingerprint is unchanged and
lapses once for a unit whose next prompt would differ.

ONE FUNCTION, TWO MOMENTS. `evidence_fingerprints` is the only computation of
the identity. Dispatch calls it to COMPARE; the landing calls it to STAMP,
over the tree and the retrospectives store as the landing left them. A
builder-side computation would be a second reading of the same channels, and
two computations of one identity is how `retro_gate`'s prose reader and its
renderer drifted until every compiling crash reached the prompt dressed as a
rustc rejection (run `681204-b695ab3f`).

The stamp was first taken at dispatch too, and that made the claim
unreachable for any body the loop kept: the `context` slot digests the
unit's own body, the landing replaces that body, so the next dispatch always
read the premise as moved. Run `143676-7dadc500` (2026-09-24, cluster
`stable`) never excluded a unit, ranked every flat re-buy `served`, and
graded `src/context.rs::ext_context` flat 11 times on 8 distinct bodies. The
premise is what the attempt was told PLUS what it wrote; only what arrives
after it is a new question. A row whose body did not move (an escalated
reply identical to the tree) is still stamped at dispatch: nothing landed.

THE THREE CHANNELS, and why these.

* `context` — the unit's FILE, as its bodies. Every route quotes it: the
  per-fn route annotates the whole file view, the per-impl route renders the
  target block plus its siblings and related blocks, file-first sends the
  file. It is also the channel that actually moves on a stalled tree, because
  the loop keeps landing bodies in it — on the run above, `src/error.rs`
  gained `context` (`78f67290fd15864e`) and `fmt` (`834f31f22906674e`)
  between the dispatch that graded `fmt` flat and the iteration that excluded
  it.
* `retro` — the retrospectives store's records for the unit. This is the
  `RETROSPECTIVE_GUIDANCE` channel AND the cross-fill `LAST_ERROR` channel:
  the pair route's next `LAST_ERROR` is built from `last_compile_error` /
  `last_error_preview` / `last_failed_body` on the same record
  (`impl_fill`'s `pair_last_error` call). The screened guidance is a pure
  function of the record, the file text and the conviction evidence, and the
  first two are digested here, so digesting the record covers what the prompt
  would carry without re-running the screen. On the run above the pair
  record's `retrospective_count` walked 49 -> 61 with four different
  categories, and `src/fmt.rs::debug::ErrorImpl` went from `GENUINELY_HARD`
  round 1 to `WRONG_DEREF_PATTERN` round 2, between the grade and the
  exclusion.
* `crash` — the localized crash text naming the unit this sweep. A fault the
  tree produced is the strongest evidence a prompt can carry and the one that
  arrives without any test convicting the unit, so it must be able to move a
  premise on its own.

DELIBERATELY OVER-INCLUSIVE. The `retro` slot digests the whole record,
bookkeeping fields included. A field that moves for a reason the prompt never
shows costs one re-dispatch of one unit; a field left out that the prompt DOES
show costs a permanent lock, which is the defect this module exists to close.
The guard's failure direction has to be the re-buy — and a re-buy whose prompt
really is byte-identical is refused one layer down, by the routes' own
`last_prompt_sha` check.

NOT A STEER. The inputs are the extractor's records, the retro store and the
crash localizer's output. No test name, unit name, trait, language construct
or crate symbol appears here or decides anything: the same function with the
same arguments fires for any crate, any remaining-failure set and any unit
whose file the extractor can read, including a crate that does not exist yet.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from pipeline_core.domain.models import FnLocator

# Digest width, matching `body_ledger.oracle_fingerprint` and
# `authored_layout_fp`: these values sit beside each other on a ledger row and
# are read by eye in logs, so they render the same length.
_WIDTH = 12


def _fold(text: str) -> str:
    """Whitespace runs folded — the same identity `body_ledger.canon_body`
    takes over a body, restated over arbitrary evidence text so a reformatted
    diagnostic is the same evidence and a changed word is not."""
    return " ".join(str(text).split())


def retro_evidence_text(records: Mapping[str, Mapping[str, Any]]) -> str:
    """The retrospectives store's rows for ONE unit, as comparable text.

    `records` is `{store key: row}` — a unit can carry several, one per
    container (`src/error.rs::fmt` has `DebugforError`, `DisplayforError`,
    `DebugforErrorImpl<E>` and `DisplayforErrorImpl<E>` on the live store)
    plus any joint record that names it. Keys are sorted so the store's
    iteration order cannot move the digest, and each row is dumped with
    sorted field names for the same reason. `default=str` keeps a row a
    corrupt value cannot make unreadable: this feeds a fingerprint, and the
    cost of rendering an odd value as its repr is a fingerprint that moves
    (one re-dispatch), never an exception on the dispatch path.
    """
    if not records:
        return ""
    parts = []
    for key in sorted(records):
        row = records[key]
        body = row if isinstance(row, Mapping) else {"value": row}
        parts.append(key + "\t" + json.dumps(
            dict(body), sort_keys=True, default=str, ensure_ascii=False))
    return "\n".join(parts)


def file_context_text(
    records: Iterable[Mapping[str, Any]],
) -> dict[str, str]:
    """Per file, the bodies the extractor sees there, as comparable text.

    Keyed on the record's own `file` — repo-relative (`src/error.rs`) when the
    caller enumerated from the cluster ROOT, which is what every `FnLocator`
    in the loop carries. A walk rooted at `src/` yields bare `error.rs` and
    joins to nothing; that is the rooting bug, not a no-op, and it shows up
    here as a unit with no context slot (an unrecorded premise, which
    refuses).

    Every occurrence is kept, sorted by `(fn_name, folded body)`: two impls of
    one name are two bodies the prompt renders, and a unit's own body is part
    of its file's text exactly as the prompt shows it.
    """
    by_file: dict[str, list[tuple[str, str]]] = {}
    for rec in records:
        file = str(rec.get("file") or "")
        if not file:
            continue
        by_file.setdefault(file, []).append(
            (str(rec.get("fn_name") or ""), _fold(rec.get("body_text") or "")))
    return {file: "\n".join(f"{name}\t{body}" for name, body in sorted(pairs))
            for file, pairs in by_file.items()}


def evidence_fingerprint(*, context: str = "", retro: str = "",
                         crash: str = "") -> str:
    """The identity of one unit's evidence, or `""` when nothing was recorded.

    The empty return is load-bearing and is the same convention
    `body_ledger.oracle_fingerprint` refuses on: a caller that could read none
    of the three channels has not observed that the evidence is unchanged, it
    has observed nothing, and a gate keyed on nothing is a lock. Slot names
    are part of the digest so the same text in two channels is two different
    premises.
    """
    if not (context or retro or crash):
        return ""
    h = hashlib.sha256()
    for name, text in (("context", context), ("crash", crash),
                       ("retro", retro)):
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        h.update(_fold(text).encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:_WIDTH]


def evidence_fingerprints(
    units: Iterable[FnLocator],
    records: Iterable[Mapping[str, Any]],
    *,
    retro: Mapping[FnLocator, str] | None = None,
    crash: Mapping[FnLocator, str] | None = None,
) -> dict[FnLocator, str]:
    """Per unit, the fingerprint of the evidence its next prompt would carry.

    `records` are the extractor's per-fn dicts for the tree the fill would
    write (`file`, `fn_name`, `body_text`). `retro` and `crash` are the
    per-unit texts of the other two channels — `retro_evidence_text`'s output
    from the store, and the crash localizer's excerpt keyed by locator.

    A unit whose file the records do not carry and which has neither a retro
    row nor a crash excerpt gets `""`: nothing about its evidence was
    observed. Omitted entirely rather than mapped to `""` would read the same
    at the gate, but a present empty value says "asked, nothing recorded",
    which is what a reader of the event needs.
    """
    context = file_context_text(records)
    _retro = retro or {}
    _crash = crash or {}
    return {
        unit: evidence_fingerprint(
            context=context.get(unit.file, ""),
            retro=_retro.get(unit, ""),
            crash=_crash.get(unit, ""))
        for unit in units
    }
