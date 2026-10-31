"""One typed outcome per test, read off a capture — the evidence every plan
and every coverage claim rests on.

A test in a capture is one of: passed, failed at its own first panic, or
ignored; its first panic sits in the crate's source, in test source, in the
Rust runtime, or nowhere (a process that died silently). Two questions the
loop keeps asking are answered from exactly this and nothing else:

- **Has the ground moved under the plan?** A fill is planned from one sweep's
  reading: which tests fail, and where each one first fails. On run
  `3383938-c5d24499` (2026-09-19) the first keep of fill 1 moved the first
  failure of all 141 failing tests off `construct`'s stub (`src/error.rs:249`)
  onto `object_ref` (111) and `vtable` (13), and the rest of the fill kept
  executing a plan in which `vtable` was on no run. :func:`premise_of` is the
  plan's ground; :func:`premise_moved` says which tests it moved for.
- **How far did a run get?** A failing test that stopped inside the crate
  says nothing about what it would have executed next, so "no test executes
  this body" cannot be read from it. On the same run's entry sweep all 141
  failing runs stopped at `src/error.rs:249` and the coverage reading still
  called 58 bodies dead, `vtable`, `drop` and `downcast` among them.
  :func:`run_kind` is that distinction.

Identities are chosen so a body growing above a site does not read as a
move: a crate site is the FUNCTION it lies in (resolved by the caller's
locator, the extractor's records of the tree the capture ran on), a test or
runtime site its file and line (test sources are the immutable oracle; the
runtime does not move), and a doc test's name drops its `(line N)` suffix
(`test_results.stable_name_keys`).
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from pipeline_core.domain.abort_cause import AbortReading, read_abort
from pipeline_core.domain.generation import is_gen_tests_sibling_name
from pipeline_core.domain.models import FnLocator, FnSite
from pipeline_core.domain.reference_floor import doc_test_line
from pipeline_core.domain.test_report import parse_test_output, stub_abort_reading
from pipeline_core.domain.test_results import (
    failure_bodies_for,
    parse_binary_results,
    stable_name_keys,
)

__all__ = [
    "abort_readings",
    "test_leaf",
    "RunKind",
    "SiteKind",
    "HeldSites",
    "StubHold",
    "TestOutcome",
    "PremiseEntry",
    "outcomes_of",
    "premise_moved",
    "ground_left",
    "held_locators",
    "moved_in_scope",
    "names_of",
    "premise_of",
    "run_kind",
    "site_kind_of",
    "standing_in",
    "stub_hold_of",
    "truncated_failing",
]

# Where the Rust runtime's own sources print from (`/rustc/<sha>/library/…`
# or a local `library/…`): the same mark `test_report` keys runtime aborts on.
_RUNTIME_MARK = "library/"


class SiteKind(StrEnum):
    CRATE = "crate"
    TEST = "test"
    RUNTIME = "runtime"
    NONE = "none"


class RunKind(StrEnum):
    """How much of a test's path a capture measured."""

    COMPLETE = "complete"    # passed: every body it needs ran
    ASSERTED = "asserted"    # failed at a panic in test source: the crate
                             # calls before it completed
    TRUNCATED = "truncated"  # stopped inside the crate / runtime / silently


def site_kind_of(file: str | None) -> SiteKind:
    """Test source is anything under a `tests/` directory or a relocated
    inline test module (`<stem>.gen_tests_<N>.rs`); the runtime is a path
    under `library/`; everything else a panic can name is crate source."""
    if not file:
        return SiteKind.NONE
    path = file.replace("\\", "/")
    if _RUNTIME_MARK in path and not path.startswith(("src/", "tests/")):
        return SiteKind.RUNTIME
    base = path.rsplit("/", 1)[-1]
    if (path.startswith("tests/") or "/tests/" in path
            or is_gen_tests_sibling_name(base)):
        return SiteKind.TEST
    return SiteKind.CRATE


@dataclass(frozen=True, slots=True)
class TestOutcome:
    """One test's outcome in one capture."""

    __test__ = False  # domain VO, not a pytest class

    name: str
    passed: bool
    first_file: str | None = None
    first_line: int | None = None
    aborted: bool = False
    silent: bool = False
    #: Why this test's own process aborted (`abort_cause`), read from its
    #: own block; None when it did not abort.
    abort: AbortReading | None = None
    #: The same name's failures in other test binaries, first failure site
    #: each (`TestFailure.others`): one name, several failing tests.
    others: tuple[TestOutcome, ...] = ()

    @property
    def site_kind(self) -> SiteKind:
        return SiteKind.NONE if self.passed else site_kind_of(self.first_file)


def outcomes_of(capture: str) -> dict[str, TestOutcome]:
    """Every passed and failed test of a capture. Ignored tests have no
    outcome. A name reported both passed and failed (two binaries sharing a
    test name) is failed: a failure anywhere is the evidence. A name that
    failed in several binaries carries each further copy in `others`."""
    report = parse_test_output(capture)
    silent: set[str] = set()
    for b in parse_binary_results(capture):
        silent |= b.aborted_silent
    causes = abort_readings(capture)
    out: dict[str, TestOutcome] = {
        n: TestOutcome(n, True) for n in report.passed}
    for f in report.failures:
        out[f.test_name] = TestOutcome(
            f.test_name, False, f.panic_file, f.panic_line,
            aborted=f.test_name in causes, silent=f.test_name in silent,
            abort=causes.get(f.test_name),
            others=tuple(TestOutcome(o.test_name, False, o.panic_file,
                                     o.panic_line) for o in f.others))
    return out


def abort_readings(capture: str) -> dict[str, AbortReading]:
    """Every test whose own process aborted, with the cause its block
    states — the one reading the outcome and the crash localizer share."""
    aborted: set[str] = set()
    for b in parse_binary_results(capture):
        aborted |= b.aborted
    bodies = failure_bodies_for(capture, aborted) if aborted else {}
    return {n: read_abort(bodies.get(n, "")) for n in aborted}


def run_kind(o: TestOutcome) -> RunKind:
    """COMPLETE for a pass; ASSERTED for a failure whose first panic is in
    test source and whose process did not die; TRUNCATED otherwise. The
    statements after an ASSERTED test's failing assertion are not measured
    either — a named limit: the crate call the assertion judged completed,
    which is what a coverage refusal about that test needs."""
    if o.passed:
        return RunKind.COMPLETE
    if (o.site_kind is SiteKind.TEST and not o.aborted
            and all(c.site_kind is SiteKind.TEST for c in o.others)):
        return RunKind.ASSERTED
    # Any copy of the name that stopped inside the crate (or died) is a run
    # that did not reach the end of its path.
    return RunKind.TRUNCATED


def truncated_failing(outcomes: Mapping[str, TestOutcome]) -> frozenset[str]:
    """The failing tests whose run stopped before its crate path finished."""
    return frozenset(n for n, o in outcomes.items()
                     if run_kind(o) is RunKind.TRUNCATED)


Locate = Callable[[str, int], FnSite | None]
#: One test's premise entry: (passed, site identity, aborted).
PremiseEntry = tuple[bool, tuple[str, ...], bool]


def _site_identity(o: TestOutcome, locate: Locate | None) -> tuple[str, ...]:
    kind = o.site_kind
    if kind is SiteKind.NONE:
        return ("none",)
    file = str(o.first_file)
    if kind is SiteKind.CRATE:
        site = (locate(file, int(o.first_line))
                if locate is not None and o.first_line is not None else None)
        if site is not None:
            return ("fn", site.file, site.fn_name, site.container)
        # Unresolved crate site: the file alone. A line would move with any
        # body above it.
        return ("crate", file)
    return ("at", file, str(o.first_line))


def premise_of(
    outcomes: Mapping[str, TestOutcome], locate: Locate | None = None,
) -> dict[str, PremiseEntry]:
    """The ground a plan stands on: per test (stable name), whether it
    passes, where it first fails, and whether its process died."""
    keys = stable_name_keys(outcomes)
    return {keys[n]: (o.passed, _sites_identity(o, locate), o.aborted)
            for n, o in outcomes.items()}


def _sites_identity(o: TestOutcome, locate: Locate | None) -> tuple[str, ...]:
    """The name's site identity; with copies in other binaries, every
    copy's, so a move of any of them is a move of the name."""
    own = _site_identity(o, locate)
    if not o.others:
        return own
    rest = sorted("|".join(_site_identity(c, locate)) for c in o.others)
    return (*own, "+", *rest)


def premise_moved(
    plan: Mapping[str, PremiseEntry], now: Mapping[str, PremiseEntry],
) -> tuple[str, ...]:
    """The tests present in both readings whose entry changed, sorted. A
    test present in only one reading (a binary that did not run, a test
    filtered out) is not a move: absence is not a reading."""
    return tuple(sorted(k for k in plan.keys() & now.keys()
                        if plan[k] != now[k]))


@dataclass(frozen=True, slots=True)
class StubHold:
    """Which still-stubbed functions hold which failing tests.

    A failing test whose first failure is inside a still-stubbed function
    cannot move until that function has a body: whatever else is filled, the
    test stops at the same `unimplemented!()`. That is the strongest "need"
    a sweep can state about a unit, and it is read off each test's own
    outcome — whether its process died is beside the point.

    It used to be read off aborted BINARIES only (`test_report.
    StubAbortReading`: a section that never printed its trailer, whose only
    crate panics were stub panics, feeding the vanished tests). Isolating
    aborted binaries one test per process (`cargo_runner.
    isolate_aborted_binaries`) gives every test a verdict and closes every
    section, so that reading went silent while the stubs still held the
    tests. Measured on run `3453127-045b05b1` (2026-09-19, cluster `stable`),
    the iter-3 checkpoint re-run through the pipeline's runner: isolation on
    — 0 unfinished sections, the abort reading empty, 82 of 141 failing tests
    first failing inside the `vtable` stub; isolation off — 24 unfinished
    sections and the abort reading naming that stub. The loop dispatched
    it two iterations later, after the service order had sunk it behind
    never-served units.

    The shared-process reading survives only as what it now is: the fallback
    for a binary isolation could not rebuild, whose tests have no outcome.
    """

    by_test: Mapping[str, frozenset[FnSite]]

    @property
    def blockers(self) -> frozenset[FnSite]:
        out: set[FnSite] = set()
        for sites in self.by_test.values():
            out |= sites
        return frozenset(out)

    def held_by(self, site: FnSite) -> frozenset[str]:
        return frozenset(t for t, ss in self.by_test.items() if site in ss)


#: The held view a fill reads after its own keeps: per failing test (bare
#: name), the still-stubbed BODIES its first failure is inside, container
#: included (`StubHold.by_test`). Until 2026-10-28 the fill carried this as
#: locators, so a run opened on a measured blocker could not say which body
#: of a name its tests stop in, and the frontier it is judged with could
#: only have named the empty-container site, which every homonym inherits
#: through `FailureFrontier.convicting_for` (run `2129106-ead996ab`: two
#: bodies each of `ext_context`, `from`, `as_ref` and Debug `fmt` among the
#: opened units).
HeldSites = Mapping[str, frozenset[FnSite]]


def held_locators(held: HeldSites) -> dict[str, frozenset[FnLocator]]:
    """`held` keyed by the dispatch locator: the view of readers that order
    or write whole units (the yield, the requeue, the opener's targets)."""
    return {t: frozenset(FnLocator(s.file, s.fn_name) for s in ss)
            for t, ss in held.items()}


def stub_hold_of(
    capture: str,
    locate: Locate,
    is_stub: Callable[[FnSite], bool],
    vanished: Iterable[str] = (),
    test_files: Mapping[str, str] | None = None,
) -> StubHold:
    """:class:`StubHold` of one sweep capture.

    Each failing test whose first failure site is crate source that
    ``locate`` resolves to a function ``is_stub`` accepts is held by that
    function. Then, for the tests the capture has no outcome for
    (``vanished``: a binary isolation could not rebuild), the shared-process
    stub-abort reading adds the stubs its unfinished sections name.
    """
    held: dict[str, set[FnSite]] = {}
    for top in outcomes_of(capture).values():
        for o in (top, *top.others):
            if (o.passed or o.site_kind is not SiteKind.CRATE
                    or o.first_file is None or o.first_line is None):
                continue
            site = locate(o.first_file, int(o.first_line))
            if site is not None and is_stub(site):
                held.setdefault(top.name, set()).add(site)
    vanished = list(vanished)
    if vanished:
        fallback = stub_abort_reading(capture, locate)
        for t, sites in fallback.hollow(vanished, test_files or {}).items():
            held.setdefault(t, set()).update(sites)
    return StubHold({t: frozenset(ss) for t, ss in held.items()})


def test_leaf(name: str) -> str:
    """One spelling for a test name across the readers that carry it: the
    premise's stable key (a doc test's `#k`), libtest's raw name (a doc
    test's `(line N)`), and the leaf a route keeps (`rsplit("::")`). Two
    tests sharing a leaf in different binaries read as one here, which can
    only refuse a claim more often, never admit one whose tests moved."""
    n = name.strip()
    if doc_test_line(n) is not None:
        n = n.rpartition(" (line ")[0].rstrip()
    head, sep, tail = n.rpartition("#")
    if sep and tail.isdigit():
        n = head
    return n.rsplit("::", 1)[-1]


def moved_in_scope(
    moved: Iterable[str], scope: Iterable[str] | None,
) -> tuple[str, ...]:
    """The moved tests a claim's plan read, sorted.

    A plan is a set of claims and each one was made from part of the
    sweep's ground: the tests whose failures put that unit or run on the
    plan. A keep that moves some tests invalidates the claims made from
    those tests and no other. The first gate refused every claim of the
    fill on any move, and on a hollow tree every keep of a stub moves the
    tests that stub held, so each fill ended at its first keep with only
    the in-flight units landed: run `497151-a6fa205b` (2026-09-22, cluster
    `stable`, iter 1) kept `src/error.rs::new`, its 13 held tests moved to
    `construct_from_std`, and the 8 unclaimed units of the 12-unit batch
    were refused while none of their 55 held tests had moved; three
    iterations in a row landed only the four bodies in flight.

    A claim with no scope (None, or an empty set: a run or unit planned on
    no named test) stands on the whole ground, so any move refuses it."""
    moved_t = tuple(sorted(set(moved)))
    if not scope:
        return moved_t
    leaves = {test_leaf(s) for s in scope}
    return tuple(m for m in moved_t if test_leaf(m) in leaves)


def ground_left(
    moved: Iterable[str], scope: Iterable[str] | None, read: Iterable[str],
) -> tuple[str, ...]:
    """The planned tests of a claim that the plan READ (``read``: the
    premise's keys) and no keep moved, sorted: the part of its ground the
    plan's reading still describes. A planned test the premise holds no
    entry for is no ground at all — absence is not a reading
    (:func:`premise_moved`), so it cannot keep a claim standing.

    A claim is made from several tests' readings, and a move invalidates the
    reading of the tests it moved and no other. Refusing a claim because ONE
    of its tests moved made the rule fill-wide again for any claim that is
    wide: run `116341-24c76a97` (2026-09-25, cluster `stable`) fill 6, the
    keeps of a per-fn run moved `test_downcast` and `test_downcast_mut` (a
    binary whose abort site shifts with every drop body), and every run
    ahead with more than one planned test intersected the fill's moved set
    (counted at the fill's end: the error.rs file run 2 of 54, its impl twin
    3 of 55, both formatter pairs 2 of 54, of which 1 had moved when the
    pairs were deferred), so each was deferred while nearly all of its
    readings still held; the pairs had opened in none of the run's six
    fills.

    A claim with no named test (None or empty) has no part of the ground of
    its own: nothing is left once anything moved, which keeps the unscoped
    rule of :func:`moved_in_scope`."""
    if not scope:
        return ()
    gone = {test_leaf(m) for m in moved}
    seen = {test_leaf(r) for r in read}
    return tuple(sorted({s for s in scope
                         if test_leaf(s) in seen
                         and test_leaf(s) not in gone}))


def _first_fail_fns(identity: tuple[str, ...]) -> frozenset[tuple[str, str]]:
    """The (file, fn) of every crate function a premise site identity names:
    the name's own first failure and each other binary's copy
    (:func:`_sites_identity` is the writer of this shape)."""
    own: list[str] = []
    rest: list[str] = []
    if "+" in identity:
        cut = identity.index("+")
        own, rest = list(identity[:cut]), list(identity[cut + 1:])
    else:
        own = list(identity)
    out: set[tuple[str, str]] = set()
    for parts in (own, *(r.split("|") for r in rest)):
        if len(parts) >= 3 and parts[0] == "fn":
            out.add((parts[1], parts[2]))
    return frozenset(out)


def standing_in(
    now: Mapping[str, PremiseEntry], units: Iterable[tuple[str, str]],
) -> tuple[str, ...]:
    """The failing tests whose first failure, on the ground as it stands
    NOW, lies inside one of ``units`` ((file, fn) pairs), sorted.

    This is the claim's standing on the current ground, independent of the
    ground it was planned on: a unit a failing test now stops inside is on
    that test's path whatever else moved, which is the reading a fresh plan
    dispatches it on (the sweep's stub holds rank such a unit as a blocker,
    whichever test put it there). A move is evidence FOR such a claim. Run
    `790176-f0df7402` (2026-09-23, cluster `stable`): the keep of
    `src/ptr.rs::new` moved all 141 failing tests, and the gate refused
    `src/error.rs::downcast_ref` and `is`, which the next sweep read holding
    6 and 4 tests at their first failure.

    Any failing test counts, not only the tests the claim was planned on.
    Run `14235-bf24f931` (2026-10-16) fill 1: the per-fn claim of
    `src/lib.rs::must_use` was refused `premise` because the tests it was
    planned on had moved elsewhere, while the keep that moved them put
    `test_boxed_str` (attributed at that sweep to a formatter) at its first
    failure inside `must_use`; the next sweep's stub holds name it, and the
    unit waited a fill. The same refusal recurs on `14495-a26ad4ea` and
    `327239-f8e7fff0`.

    Units are matched by file and function name: two homonyms in one file
    read as one, which can only admit a claim the fresh measurement then
    judges, never refuse one."""
    want = frozenset(units)
    if not want:
        return ()
    return tuple(sorted(
        k for k, (passed, identity, _aborted) in now.items()
        if not passed and _first_fail_fns(identity) & want))


def names_of(entries: Iterable[str]) -> str:
    """A short rendering of a moved set for a log line."""
    items = list(entries)
    head = ", ".join(items[:4])
    return head + (f" (+{len(items) - 4} more)" if len(items) > 4 else "")
