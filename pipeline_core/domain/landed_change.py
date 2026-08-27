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


def _named(units: frozenset[FnLocator] | set[FnLocator], sample: int) -> str:
    named = ", ".join(sorted(f"{k.file}::{k.fn_name}" for k in units)[:sample])
    more = len(units) - min(len(units), sample)
    return named + (f" (+{more} more)" if more > 0 else "")


def render_pass_audit(*, cohort_size: int, attempted: int,
                      landed: frozenset[FnLocator] | set[FnLocator],
                      unchanged: frozenset[FnLocator] | set[FnLocator],
                      declined: frozenset[FnLocator] | set[FnLocator] = frozenset(),
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
    actually holds."""
    return (f"cohort {cohort_size} unit(s), {attempted} dispatched, "
            f"{len(landed)} landed a semantic change"
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
