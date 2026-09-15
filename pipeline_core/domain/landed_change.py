"""Did a dispatched unit actually TAKE a semantic change? (pure domain)

The pass cursor (`domain/tdd_floor.py`, `domain/dispatch_batch.py`) marks a
unit ATTEMPTED at dispatch, and every traversal-denominated reader — the
stall budget, the flat-score guard, and above all the "completed pass, still
flat ⇒ genuine plateau" verdict — silently upgrades that to TRIED: a new
body, measured on the tree. The pass that closed the 2026-08-19 escalation
crawl shows the two are not the same fact, batch by batch:

* byte-identical re-authors — `src/error.rs::source` (a traversal step:
  dispatched, zero change), `construct_from_adhoc`, `construct_from_context`,
  `construct_from_boxed` (`construct_from_std` changed whitespace only);
* no landing at all — `src/context.rs::source` (the severed `None` the whole
  crawl exists to reach) and `::ext_context`, all four bodies peeled round 1
  as compiler-blamed, `FILE FILLED 0`, verdict `accept/unmeasurable`;
* immutable test-oracle producers, skipped by rule EVERY pass — so "43/43"
  can never mean 43 tries.

A flat sweep after such a pass refutes the escalation's theory ONLY for the
units where a semantic change landed; pronounced over the dispatch count, the
"genuine plateau" reading covers a denominator roughly half of which was
never re-tried — the aggregate misread one level up from the sweep-level one.

The measurement is a body-text comparison per dispatched unit, taken from the
extractor records the driver already has (`enumerate_fns`: `file`,
`fn_name`, `body_text`). A unit key spans every same-named occurrence in its
file on purpose — `src/context.rs::source` is two impls, and the escalation's
unit is the pair — so the bodies are compared as the ordered tuple of all
occurrences. Whitespace-insensitive: a re-author that moved only indentation
re-landed the same body ("changed whitespace only" is the recorded
non-change, not a change).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from pipeline_core.domain.models import FnLocator


def _canon_body(text: str) -> str:
    """A body's semantic identity: its text with runs of whitespace folded.

    Not a parse — folding is enough to equate the recorded
    "changed whitespace only" re-author with its predecessor, and anything
    finer would need the syn reader for a question this cheap."""
    return " ".join(text.split())


def unit_bodies(
    fn_records: Iterable[Mapping[str, object]],
    keys: Iterable[FnLocator],
) -> dict[FnLocator, tuple[str, ...]]:
    """Per unit key, the canonical bodies of EVERY matching occurrence, in
    record order. A key with no matching record maps to `()` — a unit that
    does not exist yet (or whose file was replaced) still compares.
    """
    wanted = set(keys)
    out: dict[FnLocator, tuple[str, ...]] = dict.fromkeys(wanted, ())
    for rec in fn_records:
        key = FnLocator(str(rec.get("file") or ""),
                        str(rec.get("fn_name") or ""))
        if key in wanted:
            out[key] = (*out[key], _canon_body(str(rec.get("body_text") or "")))
    return out


def locator_has_convicting(
    convicting: Mapping[Any, set[str]], key: FnLocator,
) -> bool:
    """True when any site (or locator-lifted row) with this file+name convicts.

    Conviction after site-at-write is keyed by ``FnSite``. ``dict.get`` with
    an ``FnLocator`` on that map is unconditionally empty — commit ``59a314c``
    (2026-08-29) left two reads that way, so every unchanged non-escalated
    unit was filed declined and ``cursor_carry`` carried it instead of
    releasing it. Index 0/1 are file/name on both 2-tuples and 3-tuples.
    An empty test set is not a conviction.
    """
    return any(
        k[0] == key.file and k[1] == key.fn_name and tests
        for k, tests in convicting.items())


def declined_unchanged(
    unchanged: frozenset[FnLocator] | set[FnLocator],
    *,
    convicting: Mapping[Any, set[str]],
    escalated: frozenset[FnLocator] | set[FnLocator],
    already_declined: frozenset[FnLocator] | set[FnLocator] = frozenset(),
    crash_evidence: frozenset[FnLocator] | set[FnLocator] = frozenset(),
) -> frozenset[FnLocator]:
    """Unchanged units whose 'return it unchanged' was the instruction.

    Escalated units are never declined: their instruction was rewrite.
    ``already_declined`` is the pass cursor's record from earlier batches
    of the same traversal (the pass-close fallback).

    NEITHER ARE UNITS UNDER LIVE CRASH EVIDENCE, and this is the one clause
    that has to be checked before ``already_declined`` rather than beside it.
    The test below is "no failing test convicts this unit" — and a crashed
    binary reports no verdict for anything inside it, so a fault's own blamed
    frame passes that test BECAUSE it crashed. The louder the failure, the more
    confidently the unit was filed as correctly standing pat.

    What that cost, run `1519852-5d607329` (2026-08-30, cluster stable):
    `src/error.rs::drop` and `src/ptr.rs::as_ptr` were both written to the
    persisted floor's `declined` while ASAN was blaming `drop`'s frame for a
    heap-use-after-free that kept 13 tests unmeasurable. `declined` is the one
    population :func:`domain.tdd_floor.cursor_carry` carries across a `code_id`
    move UNCONDITIONALLY — on the premise that landing nothing was the
    instructed outcome and a fill-path fix gives no reason to re-offer it — so
    every relaunch inherited the verdict that the crashing body was fine.
    Fixing it here rather than in the carry is deliberate: the carry's premise
    is correct for what `declined` is supposed to mean, and every other reader
    of the field (the pass-close audit, the re-buy detector, the resume) then
    stays correct without knowing what a crash is.
    """
    return frozenset(
        k for k in unchanged
        if k not in escalated
        and k not in crash_evidence
        and (k in already_declined or not locator_has_convicting(convicting, k)))


def split_reverted(
    unchanged: frozenset[FnLocator] | set[FnLocator],
    reverted: Iterable[FnLocator] | Iterable[tuple[str, str]],
) -> tuple[frozenset[FnLocator], frozenset[FnLocator]]:
    """Split dispatched-unchanged into (identical-reply, reverted-to-snapshot).

    The body comparison cannot see a peel or a regression revert: both leave
    the pre-fill text on the tree, so they sit in `frontier - landed` next
    to a byte-identical re-author. Writing FLAT on that union overclaimed
    the try — "the body on the tree is the one the model returned" — which
    was false for the 2026-08-19 crawl's peeled `src/context.rs::source`.
    The fill result's `reverted_fns` is the set that distinguishes them."""
    rev = frozenset(FnLocator(f, n) for f, n in reverted)
    un = frozenset(unchanged)
    return un - rev, un & rev


def changed_units(
    before: Mapping[FnLocator, tuple[str, ...]],
    after: Mapping[FnLocator, tuple[str, ...]],
) -> frozenset[FnLocator]:
    """The units whose body tuple moved between the two readings.

    Asymmetric keys count as changed — a unit that appeared or vanished is a
    tree change wherever it came from."""
    keys = set(before) | set(after)
    return frozenset(k for k in keys if before.get(k, ()) != after.get(k, ()))


def unattempted_units(
    frontier: Iterable[FnLocator],
    attempted: Iterable[FnLocator],
    landed: Iterable[FnLocator],
) -> frozenset[FnLocator]:
    """Frontier units no route opened and whose body did not move.

    A whole-file answer can rewrite a frontier unit the route never
    targeted (a passenger of the write list). Subtracting `landed` keeps
    that unit out of the "no route attempted" set: its body moved, and
    naming it unattempted in the same audit contradicts the landed list.
    A passenger still earns no served-at stamp — that update keys on
    `attempted` only, which this function does not touch.
    """
    return frozenset(frontier) - frozenset(attempted) - frozenset(landed)


def pass_unopened(
    units_at_close: Iterable[FnLocator],
    *,
    resumed: Iterable[FnLocator],
    opened: Iterable[FnLocator],
    landed: Iterable[FnLocator],
) -> frozenset[FnLocator]:
    """The closing pass's dispatched units that no route opened.

    The cursor marks a unit attempted when a batch holds it; a route opens
    it only if a run holding it was not deferred or refused. The units in
    neither `opened` nor `landed` were dispatched and never written — not
    a try. `resumed` (the cursor a previous process left) is excluded: this
    process cannot know whether that process's routes opened them, and an
    audit that claimed so would be inventing the record.
    """
    return (frozenset(units_at_close) - frozenset(resumed)
            - frozenset(opened) - frozenset(landed))


def reoffer_unopened(
    dispatched: Iterable[FnLocator],
    *,
    opened: Iterable[FnLocator],
    landed: Iterable[FnLocator],
    already: Iterable[FnLocator],
) -> frozenset[FnLocator]:
    """Dispatched units no route opened, that this pass has not re-offered.

    The cursor marks a unit attempted when a batch holds it, so a unit every
    run of which was deferred or refused had no turn and still counted as
    one: the pass moved past it and, if it was the last batch, closed over
    it. Run `11358-e5de094e` (2026-09-15) dispatched 27 units across three
    batches and closed traversal 1 with most of them never written, their
    runs deferred every fill. These units go back on the pass so the next
    batch offers them again.

    `already` bounds it: a unit is re-offered ONCE per pass. A unit the
    routes refuse again stays on the cursor and the pass closes as before —
    otherwise a unit whose runs are always deferred would hold the pass
    open forever, and every stop denominated in completed passes would go
    dead with it (`take_batch`'s traversal accounting names that lock).
    A passenger — landed without being opened — had its body written and is
    not re-offered.
    """
    return (frozenset(dispatched) - frozenset(opened) - frozenset(landed)
            - frozenset(already))


def _named(units: frozenset[FnLocator] | set[FnLocator], sample: int) -> str:
    named = ", ".join(sorted(f"{k.file}::{k.fn_name}" for k in units)[:sample])
    more = len(units) - min(len(units), sample)
    return named + (f" (+{more} more)" if more > 0 else "")


def render_pass_audit(*, cohort_size: int, attempted: int,
                      landed: frozenset[FnLocator] | set[FnLocator],
                      unchanged: frozenset[FnLocator] | set[FnLocator],
                      declined: frozenset[FnLocator] | set[FnLocator] = frozenset(),
                      unopened: frozenset[FnLocator] | set[FnLocator] = frozenset(),
                      sample: int = 12) -> str:
    """The completed pass's effective coverage, for the log line the
    post-pass verdict must be read against.

    Names the DISPATCHED-BUT-UNCHANGED units rather than the landed ones:
    they are the finding — a flat sweep after this pass proves nothing about
    them, and the reader deciding "genuine plateau" needs their names, not a
    count.

    `declined` is the subset of the unchanged that no failing test CONVICTS —
    units the frontier reached by inference (an escalation's producers, a
    traversal step), whose re-fill prompt says "rewrite ONLY if wrong, else
    return it unchanged". For them "unchanged" is the instructed outcome when
    the body is right, so the line keeps them out of the failed-try list
    (run `902902-daee577d`, 2026-08-20: `ContextError<C, E>::source`, correct
    and told it FAILS, was rewritten into a dead `TypeId` branch — the audit
    would have credited the walk downhill as a landing and, unchanged, would
    have listed the honest outcome as a failed try). The body comparison
    cannot tell an instructed "unchanged" from a peeled fill of the same
    unit; the split is by conviction, which is the distinction the record
    actually holds.

    `unopened` is the dispatched units NO route opened — every run holding
    them was deferred or refused. They are neither a landing nor a failed
    try, and the line names them separately so a flat sweep after the pass
    is not read as refuting units the fill never wrote (run
    `11358-e5de094e` iter 3, 2026-09-15: 26 of 27 filed as unchanged, most
    of them never opened)."""
    return (f"cohort {cohort_size} unit(s), {attempted} dispatched, "
            f"{len(landed)} landed a semantic change"
            + (f"; {len(unopened)} dispatched but NO route opened them "
               "(every run holding them deferred or refused — not a try): "
               f"{_named(unopened, sample)}"
               if unopened else "")
            + (f"; {len(unchanged)} dispatched with NO landed change "
               "(byte-identical re-author, reverted/peeled fill, or "
               f"immutable-by-rule): {_named(unchanged, sample)}"
               if unchanged else "")
            + (f"; {len(declined)} INFERRED re-fill(s) unchanged — no test "
               "convicts them and returning a right body unchanged was the "
               f"instruction, so not a failed try: {_named(declined, sample)}"
               if declined else "")
            + ". A flat sweep after this pass refutes the dispatch only for "
              "the landed subset.")
