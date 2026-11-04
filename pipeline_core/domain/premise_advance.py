"""A body whose own keep moved a test's failure past it is not that test's
owner at the new failure (pure).

The fact is written once, by the body ledger's grade (`body_ledger.classify`):
a landing whose convicting test, still red at the next sweep, stops at a
LATER assertion position of its own body carries `(test, "advanced")` in its
`verdict_delta`. Until 2026-10-19 the entry was written only when no
convicting test turned green, so a keep that both paid some tests and moved
the rest was recorded as `proven` with the moves dropped.

The instance, run `1680589-7a6824f8` (cluster `stable`, iters 11 to 13):
seven tests rendering with `{:#?}` needed two bodies in series, the error's
debug formatter and the context error's `Debug` impl. Fill 11's rewrite of
the formatter turned three green and moved the other four to a later
assertion. At the next sweep behaviour attribution still owned those four by
the formatter it had just fixed, the measured coverage owner that was wrong
(`src/context.rs::fmt`) sat outside the batch, and fill 12 spent 69 minutes
rewriting the fixed body twice, both reverted. Fill 13 reached the context
impl as a leftover and cashed the four in 6 minutes.

The rule: for a test still failing, a unit whose latest graded row (under
the oracle scoring the tree now) says it advanced that test does not own the
test. The test is owned by whatever else attribution and its coverage name.
The rule keys on a recorded position move; it names no trait, file or
construct. It never strands a test: the caller restores the advanced-past
units for a test nothing else can own (`stranded`).
"""
from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping

from pipeline_core.domain.attribution import AttributionResult
from pipeline_core.domain.body_ledger import OBSERVED as OBSERVED_KIND
from pipeline_core.domain.body_ledger import (
    LedgerEntry,
    LedgerKey,
    latest_by_unit,
    standing_on_tree,
)
from pipeline_core.domain.models import FnLocator, FnSite

__all__ = ["ADVANCED", "OBSERVED", "advanced_past", "edges_on_tree",
           "observed_by", "without_advanced", "stranded", "owning_view"]

ADVANCED = "advanced"
OBSERVED = OBSERVED_KIND


def _bare(name: str) -> str:
    return name.rsplit("::", 1)[-1].strip()


def edges_on_tree(entries: Iterable[LedgerEntry], *, oracle_fp: str,
                  failing: Collection[str],
                  body_now: Mapping[LedgerKey, str] | None = None,
                  ) -> dict[str, dict[FnLocator, frozenset[str]]]:
    """`bare test -> unit -> kinds`: every per-test edge the standing row of
    each unit records for a still-failing test (`verdict_delta` kinds:
    `advanced`, `observed`, …). The one reader of those edges; each meaning
    is a view of it (:func:`advanced_past`, :func:`observed_by`), so two
    readers cannot read the same row by two rules. Rows graded under another
    oracle say nothing about this one (an empty `oracle_fp` matches nothing).

    With `body_now` (the body on the tree per unit) the standing row is the
    latest one about THAT body (`standing_on_tree`, the view dispatch reads):
    an edge graded on a body since rewritten is not a claim about the tree,
    and a body re-seeded at cluster entry brings its edge with it. Without
    it, `latest_by_unit`, whose proven row outranks a later row about
    another body. A unit absent from `body_now` has no standing row, so the
    caller must put every unit that has an edge row in it."""
    want = {_bare(t) for t in failing}
    rows = list(entries)
    standing = (standing_on_tree(rows, body_now) if body_now is not None
                else latest_by_unit(rows))
    out: dict[str, dict[FnLocator, set[str]]] = {}
    for key, row in standing.items():
        if not oracle_fp or row.oracle_fp != oracle_fp:
            continue
        for test, kind in row.verdict_delta:
            if _bare(test) in want:
                out.setdefault(_bare(test), {}).setdefault(
                    FnLocator(key.file, key.fn_name), set()).add(kind)
    return {t: {u: frozenset(ks) for u, ks in us.items()}
            for t, us in out.items()}


def _of_kind(edges: Mapping[str, Mapping[FnLocator, frozenset[str]]],
             kind: str) -> dict[str, frozenset[FnLocator]]:
    out = {t: frozenset(u for u, ks in us.items() if kind in ks)
           for t, us in edges.items()}
    return {t: us for t, us in out.items() if us}


def advanced_past(entries: Iterable[LedgerEntry], *, oracle_fp: str,
                  failing: Collection[str],
                  body_now: Mapping[LedgerKey, str] | None = None,
                  ) -> dict[str, frozenset[FnLocator]]:
    """`bare test -> units` that the standing row of each unit says advanced
    that still-failing test (:func:`edges_on_tree`, kind `advanced` only:
    an `observed` edge is evidence FOR the unit, and reading it here would
    strip the producing body from the test it produces)."""
    return _of_kind(edges_on_tree(entries, oracle_fp=oracle_fp,
                                  failing=failing, body_now=body_now),
                    ADVANCED)


def observed_by(entries: Iterable[LedgerEntry], *, oracle_fp: str,
                failing: Collection[str],
                body_now: Mapping[LedgerKey, str] | None = None,
                ) -> dict[str, frozenset[FnLocator]]:
    """`bare test -> units` whose standing body's own keep moved that
    still-failing test at the statement that judges it (kind `observed`).
    The owed walk owes those units first (`candidate_tiers.due_reading`)."""
    return _of_kind(edges_on_tree(entries, oracle_fp=oracle_fp,
                                  failing=failing, body_now=body_now),
                    OBSERVED)


def _strip[K: (FnLocator, FnSite)](
        fr: Mapping[K, set[str]], advanced: Mapping[str, frozenset[FnLocator]]
) -> dict[K, set[str]]:
    out: dict[K, set[str]] = {}
    for k, tests in fr.items():
        loc = FnLocator(k.file, k.fn_name)
        kept = {t for t in tests if loc not in advanced.get(_bare(t), frozenset())}
        if kept:
            out[k] = kept
    return out


def without_advanced(attr: AttributionResult,
                     advanced: Mapping[str, frozenset[FnLocator]]) -> AttributionResult:
    """The attribution with each test taken off the units that advanced past
    it: the dispatch map, the convicting map and the site frontier, and any
    formatter pair none of whose members still owns a test."""
    if not advanced:
        return attr
    frontier = _strip(attr.frontier, advanced)
    owning = set(frontier)
    pairs = frozenset(p for p in attr.pairs
                      if any(m in owning for m in p.member_locators()))
    return attr._replace(frontier=frontier,
                         convicting=_strip(attr.convicting, advanced),
                         site_frontier=_strip(attr.site_frontier, advanced),
                         pairs=pairs)


def stranded(advanced: Mapping[str, frozenset[FnLocator]],
             owned: Mapping[str, Collection[FnLocator]]) -> dict[str, frozenset[FnLocator]]:
    """The advanced tests nothing else owns after the strip (`owned`: every
    owner the sweep found for each test, attribution and coverage, with the
    advanced-past units already removed). Their advanced-past units go back:
    a test with no owner is a test no sweep can move."""
    return {t: us for t, us in advanced.items() if not owned.get(t)}


def owning_view(owners: Mapping[str, Collection[FnSite]],
                advanced: Mapping[str, frozenset[FnLocator]], *,
                owned_elsewhere: Collection[str] = ()) -> dict[str, frozenset[FnSite]]:
    """A test -> sites map of who OWNS each test, from a map of who ran in it
    (coverage owners, recorded entry sets): each test without the units that
    advanced past it. A test keeps every key it had, so "measured, owned by
    nobody here" never reads as "unmeasured" (absent) to a stand-in reader.

    Never strands a test: when the strip would leave a test with no site and
    attribution (`owned_elsewhere`, bare names) gives it no owner either, the
    test keeps its sites as measured.

    Only readers that turn execution into ownership take this view (owner
    admission and ranking, the session slices). What a test ENTERED is still
    a measurement and stays on the coverage reading itself: the red-dead
    demotion and the prompt's "no failing test enters this body" sentence
    read the reading, and a unit a moved test still enters is entered."""
    if not advanced:
        return {t: frozenset(ks) for t, ks in owners.items()}
    elsewhere = {_bare(t) for t in owned_elsewhere}
    out: dict[str, frozenset[FnSite]] = {}
    for t, ks in owners.items():
        adv = advanced.get(_bare(t), frozenset())
        kept = frozenset(k for k in ks if FnLocator(k.file, k.fn_name) not in adv)
        if not kept and ks and _bare(t) not in elsewhere:
            kept = frozenset(ks)
        out[t] = kept
    return out
