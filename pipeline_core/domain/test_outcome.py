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

from pipeline_core.domain.generation import is_gen_tests_sibling_name
from pipeline_core.domain.models import FnSite
from pipeline_core.domain.test_report import parse_test_output, stub_abort_reading
from pipeline_core.domain.test_results import (
    parse_binary_results,
    stable_name_keys,
)

__all__ = [
    "RunKind",
    "SiteKind",
    "StubHold",
    "TestOutcome",
    "PremiseEntry",
    "outcomes_of",
    "premise_moved",
    "names_of",
    "premise_of",
    "run_kind",
    "site_kind_of",
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

    @property
    def site_kind(self) -> SiteKind:
        return SiteKind.NONE if self.passed else site_kind_of(self.first_file)


def outcomes_of(capture: str) -> dict[str, TestOutcome]:
    """Every passed and failed test of a capture. Ignored tests have no
    outcome. A name reported both passed and failed (two binaries sharing a
    test name) is failed: a failure anywhere is the evidence."""
    report = parse_test_output(capture)
    aborted: set[str] = set()
    silent: set[str] = set()
    for b in parse_binary_results(capture):
        aborted |= b.aborted
        silent |= b.aborted_silent
    out: dict[str, TestOutcome] = {
        n: TestOutcome(n, True) for n in report.passed}
    for f in report.failures:
        out[f.test_name] = TestOutcome(
            f.test_name, False, f.panic_file, f.panic_line,
            aborted=f.test_name in aborted, silent=f.test_name in silent)
    return out


def run_kind(o: TestOutcome) -> RunKind:
    """COMPLETE for a pass; ASSERTED for a failure whose first panic is in
    test source and whose process did not die; TRUNCATED otherwise. The
    statements after an ASSERTED test's failing assertion are not measured
    either — a named limit: the crate call the assertion judged completed,
    which is what a coverage refusal about that test needs."""
    if o.passed:
        return RunKind.COMPLETE
    if o.site_kind is SiteKind.TEST and not o.aborted:
        return RunKind.ASSERTED
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
    return {keys[n]: (o.passed, _site_identity(o, locate), o.aborted)
            for n, o in outcomes.items()}


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
    for o in outcomes_of(capture).values():
        if (o.passed or o.site_kind is not SiteKind.CRATE
                or o.first_file is None or o.first_line is None):
            continue
        site = locate(o.first_file, int(o.first_line))
        if site is not None and is_stub(site):
            held.setdefault(o.name, set()).add(site)
    vanished = list(vanished)
    if vanished:
        fallback = stub_abort_reading(capture, locate)
        for t, sites in fallback.hollow(vanished, test_files or {}).items():
            held.setdefault(t, set()).update(sites)
    return StubHold({t: frozenset(ss) for t, ss in held.items()})


def names_of(entries: Iterable[str]) -> str:
    """A short rendering of a moved set for a log line."""
    items = list(entries)
    head = ", ".join(items[:4])
    return head + (f" (+{len(items) - 4} more)" if len(items) > 4 else "")
