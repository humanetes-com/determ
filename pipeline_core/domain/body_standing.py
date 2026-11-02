"""Which filled bodies on the tree have evidence that they are right (pure
domain, plus the per-fill binding every prompt reader goes through).

After strip every filled body is a model's answer, and most were kept while
their tests could not run (they stopped in a still-stubbed function). Run
`1057870-b427a3a2` (2026-10-15): on that augmentation 71 bodies were graded
`unmeasured` at their landing, 0 of them were ever graded again, and the
prompt's crate-idioms section showed one of them — a constructor that calls
an accessor on `NonNull::dangling()` — to every later prompt of the file as
"patterns this file already uses, MATCH these". The next constructor copied
it.

A body is PROVEN ON THE TREE when the body on the tree is the one a proven
ledger row graded, or when, in the latest intact sweep, at least one test
attributed to the unit passed with this body on the tree and no attributed
test died in it or died unlocated (:func:`death_places`). That second clause is not written back to the ledger: the schedule
reads a landing's later grade as that landing's payoff
(`fill_schedule.unit_evidence_from`), and a test a sibling turned green is
not what the landing bought. It is a reading of the tree, recomputed at every
sweep.

The driver binds the set once per sweep (:func:`bind`); readers that show a
filled body to the model as something to follow call :func:`is_proven_text`
themselves, so none of them can be wired without it. Unbound (None) means no
reading was taken: readers then make no claim either way.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from pipeline_core.domain.assertion_position import leaf
from pipeline_core.domain.body_ledger import canon_body, strip_comments
from pipeline_core.domain.crash_site import CrashSite
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.rust_fn_headers import fn_headers, locate_all_fns
from pipeline_core.domain.rust_scan import is_hollow_body

# unit -> the identities of its proven bodies, as the sweep read them; and
# every unit with a body the sweep read, proven or not.
_proven: Mapping[FnLocator, frozenset[str]] | None = None
_known: frozenset[FnLocator] = frozenset()


def _identity(body_text: str) -> str:
    # Comments are not compared: a prompt's whole-file view may elide them.
    return canon_body(strip_comments(body_text))


def bind(proven: Mapping[FnLocator, Iterable[str]] | None,
         known: Iterable[FnLocator] = ()) -> None:
    """Install the sweep's reading: ``proven`` maps a unit to its body texts
    on the tree when the sweep was read; ``known`` is every unit the reading
    covered. None clears the reading."""
    global _proven, _known
    _proven = (None if proven is None else
               {u: frozenset(_identity(t) for t in texts)
                for u, texts in proven.items()})
    _known = frozenset(known) | frozenset(_proven or ())


def is_proven_text(unit: FnLocator, body_text: str) -> bool | None:
    """Whether ``body_text`` is a proven body of ``unit`` on this tree. A body
    rewritten since the sweep is not the one the sweep measured. None when no
    reading is bound, or the reading never saw the unit (a test file, a path
    outside the crate)."""
    if _proven is None or unit not in _known:
        return None
    return _identity(body_text) in _proven.get(unit, frozenset())


#: The line a prompt's file view puts above a filled body that is not proven.
UNVERIFIED_MARK = ("// UNVERIFIED — an earlier fill's answer; no test has "
                   "passed with it on the tree yet. Read it for what it "
                   "calls, not as how this crate does things.")


def mark_unverified(path: str, text: str) -> str:
    """``text`` (a rendering of the crate file ``path``) with
    :data:`UNVERIFIED_MARK` above every filled body the bound reading knows
    and does not find proven. Stubs, unknown units and an unbound reading
    are left as they are."""
    if _proven is None:
        return text
    inserts: list[tuple[int, str]] = []
    for name in dict.fromkeys(h.name for h in fn_headers(text, line_anchored=False)):
        for _sig, sig_start, body_start, body_end in locate_all_fns(text, name):
            body = text[body_start:body_end]
            if is_hollow_body(body):
                continue
            if is_proven_text(FnLocator(path, name), body) is not False:
                continue
            line_start = text.rfind("\n", 0, sig_start) + 1
            indent = text[line_start:sig_start]
            indent = indent[:len(indent) - len(indent.lstrip(" \t"))]
            inserts.append((line_start, f"{indent}{UNVERIFIED_MARK}\n"))
    for at, line in sorted(set(inserts), reverse=True):
        text = text[:at] + line + text[at:]
    return text


#: Where a test's own process died: the crate units the death is placed in.
#: Empty means the death is placed nowhere (unlocated), which refutes every
#: unit the test is attributed to.
DeathPlace = frozenset[FnLocator]


def death_places(
    aborted: Iterable[str],
    *,
    sites: Sequence[CrashSite],
) -> dict[str, DeathPlace]:
    """Every dead test (bare name) and the units its death is placed in.

    A death is placed by its crash sites: the culprit of a site that names
    a producer (:meth:`CrashSite.producer_refusal`, the settlement's own
    reading). A site that names none, or no site at all, leaves the death
    unlocated. The debugger's reader, a runtime abort and NOBODY say
    nothing about who made the bad memory, and one unlocated site of a
    test unlocates the test. An abort that printed a panic is not a native
    crash (`test_results.native_crashes`), so the localizer never sites it
    and the death is unlocated. Run `941823-6d7acea5` (2026-11-02, iter 2)
    placed that death at the panic's function and proved 17 units beside
    it. Not `abort_verdict.crash_units_by_test`: that is every crate frame
    of the stack ("this crash names a body the loop can rewrite"), and on
    a destructor crash the stack is the test's own call chain, so it would
    refute every attributed unit again."""
    by_test: dict[str, list[CrashSite]] = {}
    for site in sites:
        if site.test_name:
            by_test.setdefault(leaf(site.test_name), []).append(site)
    out: dict[str, DeathPlace] = {}
    for t in {leaf(n) for n in aborted}:
        mine = by_test.get(t, [])
        out[t] = (frozenset().union(*(s.culprit_locators for s in mine))
                  if mine and all(s.names_producer for s in mine)
                  else frozenset())
    return out


def _refutes(unit: FnLocator, place: DeathPlace) -> bool:
    """A death refutes ``unit`` when it is placed nowhere, or in ``unit``. A
    death placed in other units only says nothing about this one."""
    return not place or unit in place


def proven_units(
    ledger_proven: Iterable[FnLocator],
    attributed: Mapping[str, Iterable[FnLocator]],
    *,
    passed: Iterable[str],
    deaths: Mapping[str, DeathPlace],
    intact: bool,
) -> frozenset[FnLocator]:
    """The units whose on-tree bodies :func:`bind` records.
    ``ledger_proven`` are the units whose on-tree body a proven ledger row
    graded; ``attributed`` maps a test (bare name) to the units it is
    attributed to; ``passed`` are the sweep's passing tests and ``deaths``
    every test whose own process died, with where (:func:`death_places`). A
    unit is proven when an attributed test passed and no attributed test's
    death refutes it (:func:`_refutes`). A sweep that could not be
    reconciled (``intact`` False) proves nothing new.

    Until 2026-11-02 any attributed death removed the unit, wherever it
    died: the standing line read 33 of 394 with 18 more left out over one
    aborted test (run `3178980-c42f7944`, iter 2), and 39 of 367 with 17
    left out over two (`4112620-8a89f05c`, iter 2), where the one located
    death was a segfault blamed on `src/error.rs::drop`. A unit left out is
    marked unverified in every prompt's file view and may be stubbed by a
    settlement."""
    out = set(ledger_proven)
    if not intact:
        return frozenset(out)
    passing = frozenset(passed)
    for unit, tests in _tests_by_unit(attributed).items():
        if tests & passing and not any(
                _refutes(unit, deaths[t]) for t in tests if t in deaths):
            out.add(unit)
    return frozenset(out)


def _tests_by_unit(
    attributed: Mapping[str, Iterable[FnLocator]],
) -> dict[FnLocator, set[str]]:
    by_unit: dict[FnLocator, set[str]] = {}
    for test, units in attributed.items():
        for u in units:
            by_unit.setdefault(u, set()).add(test)
    return by_unit


@dataclass(frozen=True, slots=True)
class DeathVetoes:
    """The units with a passing attributed test and an attributed death,
    split by what the deaths decide: ``kept`` (every death placed in other
    units) and ``refused`` (a death unlocated or placed in the unit), each
    unit with its dead tests and their places. The standing line prints
    both, so a reading can check every veto against its location."""

    kept: Mapping[FnLocator, tuple[tuple[str, DeathPlace], ...]]
    refused: Mapping[FnLocator, tuple[tuple[str, DeathPlace], ...]]


def death_vetoes(
    attributed: Mapping[str, Iterable[FnLocator]],
    *,
    passed: Iterable[str],
    deaths: Mapping[str, DeathPlace],
) -> DeathVetoes:
    """:class:`DeathVetoes` of one sweep, by the rule :func:`proven_units`
    applies."""
    passing = frozenset(passed)
    kept: dict[FnLocator, tuple[tuple[str, DeathPlace], ...]] = {}
    refused: dict[FnLocator, tuple[tuple[str, DeathPlace], ...]] = {}
    for unit, tests in _tests_by_unit(attributed).items():
        dead = tuple(sorted((t, deaths[t]) for t in tests if t in deaths))
        if not dead or not tests & passing:
            continue
        if any(_refutes(unit, place) for _t, place in dead):
            refused[unit] = dead
        else:
            kept[unit] = dead
    return DeathVetoes(kept=kept, refused=refused)


def render_place(place: DeathPlace) -> str:
    """A death's place for a log line."""
    if not place:
        return "unlocated"
    return ", ".join(sorted(f"{u.file}::{u.fn_name}" for u in place))
