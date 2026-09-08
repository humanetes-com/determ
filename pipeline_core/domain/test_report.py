"""Structured test-run report + blame partition (pure domain, F3).

`parse_test_output` turns raw `cargo test` text into a typed
:class:`TestRunReport`; :func:`partition_blame` splits a unit's failing tests
into the ones it OWNS versus the ones that merely stub-panic inside a DIFFERENT
still-unfilled unit. The U2 acceptance gate consumes the partition so an
innocent unit is never charged (retro'd / exhausted) for a test that panics in
another unit's `unimplemented!()` stub — the anyhow "everything routes through
still-stubbed error.rs" ceiling.

No regex, no IO: `parse_test_output` is a structured line scanner over cargo's
own output layout; `locate` is injected by the caller (the syn-backed
structure reader), never approximated here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Protocol

from pipeline_core.domain.models import FnSite
from pipeline_core.domain.rust_idents import canon_container
from pipeline_core.domain.test_results import (
    binary_output_sections,
    parse_failed_tests,
    split_test_stdout_sections,
    strip_libtest_qualifier,
)


class FailureKind(Enum):
    """Why one test failed — the axis the blame partition keys on. A
    STUB_PANIC that resolves to another unit's stub is not this unit's fault;
    an ASSERTION/OTHER failure is a real behavioral defect that stays self."""

    STUB_PANIC = auto()   # panicked in an `unimplemented!()`/`todo!()` stub
    ASSERTION = auto()    # `assert!`/`assert_eq!` mismatch — real wrong behavior
    OTHER = auto()        # any other failure (returned Err, should_panic, …)


@dataclass(frozen=True, slots=True)
class TestFailure:
    """One failing test, with the panic location libtest printed (when any)."""

    __test__ = False  # domain VO, not a pytest class (silences collection warning)

    test_name: str
    kind: FailureKind
    panic_file: str | None      # path as printed by libtest, e.g. "src/error.rs"
    panic_line: int | None
    panic_col: int | None
    body: str                   # the test's `---- name stdout ----` section (uncapped)


@dataclass(frozen=True, slots=True)
class TestRunReport:
    """The parsed result of one `cargo test` run: which tests passed, and the
    structured failures. `passed`/`failures` are disjoint by test name."""

    __test__ = False  # domain VO, not a pytest class (silences collection warning)

    passed: frozenset[str]
    failures: tuple[TestFailure, ...]

    @property
    def failed_names(self) -> frozenset[str]:
        return frozenset(f.test_name for f in self.failures)


# `unimplemented!()` panics with "not implemented"; `todo!()` with "not yet
# implemented". Skeleton stubs use `unimplemented!()`, but accept both forms.
_STUB_MARKERS = ("not yet implemented", "not implemented")

# The Rust runtime's own announcement of a double panic. When a `Drop` impl
# panics while a first panic is already unwinding, `core::panicking::
# panic_in_cleanup` prints ONE MORE `panicked at` line — located in the
# runtime's sources (`/rustc/<sha>/library/core/src/panicking.rs:233:5` on
# rustc 1.95.0, the `/rustc/<sha>/` prefix absent when the sysroot source
# is a local path) carrying this message — and then `thread caused
# non-unwinding panic. aborting.`, which is not a `panicked at` line and is
# never yielded by the locator. The locator parsed the runtime line as one
# more located panic with a non-stub message, and the all-or-nothing
# stub-only rule read it as a veto: that is how a correct `Own::new` was
# reverted on every route across three iterations of run
# `2585062-494e630e` (2026-09-07/08). `Own::new` real means every `Error` a
# test builds is dropped through the still-stubbed `vtable()`; the stub
# panics during unwinding; the runtime aborts; the capture holds `not
# implemented` at the stub's line PLUS this line. The line is POSITIVE
# evidence the abort is a panic and not undefined behaviour, and it names
# no body — a runtime-located panic with any OTHER message (`capacity
# overflow` in `library/alloc`) is still a crate panic and still vetoes.
_RUNTIME_ABORT_MESSAGES: tuple[str, ...] = (
    "panic in a destructor during cleanup",
)
_RUNTIME_SOURCE_MARK = "library/"


def parse_panic_location(body: str) -> tuple[str | None, int | None, int | None, str]:
    """First panic in a test's stdout section → (file, line, col, message).

    Handles both libtest forms:
      2021+  : ``thread '<t>' panicked at <path>:<line>:<col>:`` then the
               message on the following line(s).
      pre-2021: ``thread '<t>' panicked at '<msg>', <path>:<line>:<col>``.
    Returns (None, None, None, "") when no panic header is present (e.g. a test
    that returned ``Err`` or tripped ``should_panic``)."""
    lines = body.splitlines()
    for i, ln in enumerate(lines):
        idx = ln.find("panicked at ")
        if idx < 0:
            continue
        after = ln[idx + len("panicked at "):]
        if after.startswith("'"):
            # pre-2021: 'MESSAGE', PATH:LINE:COL  (MESSAGE may contain commas)
            j = after.rfind("', ")
            if j <= 0:
                continue
            msg = after[1:j]
            loc = after[j + 3:].strip()
        else:
            # 2021+: PATH:LINE:COL:  — message is the next non-empty line.
            loc = after.rstrip()
            if loc.endswith(":"):
                loc = loc[:-1]
            msg = ""
            for nxt in lines[i + 1:]:
                if nxt.strip():
                    msg = nxt.strip()
                    break
        parsed = _split_location(loc)
        if parsed is None:
            continue
        pf, pl, pc = parsed
        return pf, pl, pc, msg
    return None, None, None, ""


def _split_location(loc: str) -> tuple[str, int, int] | None:
    """``path:line:col`` → (path, line, col); None if it isn't that shape.
    `path` itself carries no colon in practice, so rpartition twice from the
    right isolates col then line."""
    rest, _, col = loc.rpartition(":")
    path, _, line = rest.rpartition(":")
    if not (path and line.isdigit() and col.isdigit()):
        return None
    return path, int(line), int(col)


def _classify(msg: str, body: str) -> FailureKind:
    text = f"{msg}\n{body}"
    if any(m in text for m in _STUB_MARKERS):
        return FailureKind.STUB_PANIC
    if "assertion" in text:      # `assertion failed`, `assertion `left == right``
        return FailureKind.ASSERTION
    return FailureKind.OTHER


def _passed_names(output: str) -> set[str]:
    """Names of tests libtest reported as ``... ok`` (regex-free; the mirror of
    :func:`failing_test_names` for the passing verdict).

    Split on the ``" ... "`` separator, never on whitespace tokens. A
    ``#[should_panic]`` test passes as ``test a::b::test_x - should panic ...
    ok``; a token walk expecting ``toks[2] == "..."`` dropped it from `passed`
    while the binary's trailer still counted it, and `reconcile_sweep` reads
    THIS set against the expected names. That is how a PASSING test was
    reported crash-vanished on every sweep of runs `626784-4b7c38ba` and
    `902902-daee577d` (2026-08-20) — `test_no_backtrace_fallback`, 37/0 in its
    binary — after the qualifier strip had been applied to the three parsers in
    `test_results` but not to this one: each sweep ran the gdb/ASAN localizer,
    deliberated the layout retrigger, escalated 25 producers for a test that
    was green, and refused to replace the durable tree ("1 test unmeasured").
    """
    passed: set[str] = set()
    for raw in output.splitlines():
        line = raw.strip()
        if not (line.startswith("test ") and " ... " in line):
            continue
        if line.startswith("test result:"):
            continue
        name, _, status = line[len("test "):].partition(" ... ")
        if status.split(",")[0].split(" ")[0] == "ok":
            passed.add(strip_libtest_qualifier(name.strip()))
    return passed


def failure_evidence_by_test(output: str, *, per_test_cap: int = 480) -> dict[str, str]:
    """Per failing test, the lines a body author needs: the ``panicked at``
    location and message, then the assertion's ``left``/``right`` (or the first
    lines of the panic body), capped at `per_test_cap` characters. Keyed by the
    test name exactly as libtest printed it; a consumer holding a bare name
    matches on the last ``::`` segment (`FailureFrontier.evidence_for`).

    The per-impl prompt's [REFILL] section told the model "its attributed test
    (see Tests below) still FAILS" and carried neither the test nor what it
    said — `test_len`'s ``left: 4 / right: 1`` and `test_rev`'s ``"0" vs "3"``
    (run `902902-daee577d`, 2026-08-20) never reached a prompt, so the model
    reasoned about WHICH test could fail instead of about why. A test that
    failed with no captured section (an aborted binary) yields no entry: a
    verdict-less crash is not evidence about a body.
    """
    out: dict[str, str] = {}
    for f in parse_test_output(output).failures:
        if not f.body.strip():
            continue
        keep: list[str] = []
        started = False
        for ln in f.body.splitlines():
            s = ln.strip()
            if not started:
                if "panicked at " in s:
                    started = True
                    keep.append(s)
                continue
            if not s:
                continue
            if s.startswith("note: run with `RUST_BACKTRACE"):
                break
            keep.append(s)
            if len(keep) >= 8:
                break
        if not keep:
            keep = [s for s in (ln.strip() for ln in f.body.splitlines()) if s][:6]
        text = "\n".join(keep)
        if len(text) > per_test_cap:
            text = text[:per_test_cap].rstrip() + " …"
        out[f.test_name] = text
    return out


def merge_observations(
    evidence: Mapping[str, str], observed: Mapping[str, str],
) -> dict[str, str]:
    """`evidence` (test -> its own words) with the probe's `observed:` lines
    appended per test, matched on the bare name; tests with an observation
    but no evidence entry gain one holding the observation alone."""
    out = dict(evidence)
    by_leaf = {k.rsplit("::", 1)[-1]: k for k in evidence}
    for name, lines in observed.items():
        if not lines:
            continue
        key = by_leaf.get(name.rsplit("::", 1)[-1], name)
        out[key] = (out[key] + "\n" + lines) if out.get(key) else lines
    return out


def regressed_test_outputs(
    output: str, names: Iterable[str], *, per_test_cap: int = 480,
) -> dict[str, str]:
    """What each of `names` PRINTED in `output` — the failing assertion with
    its `left` / `right` values — keyed by the BARE test name.

    The regression gate measures the whole suite after every splice, and its
    detail named only the tests that went red. Run `3003-93e9a9bf`
    (2026-09-02, cluster `stable`): a Debug formatter rewrite reddened ten
    passing tests; the capture held `left: "hello<disabled>"` against
    `right: "hello"` for `debug_for_anyhow_literal`, the detail said
    `REGRESSED previously-passing test(s): ['debug_for_anyhow_literal', …]`,
    and both the next fill prompt and the retrospective prompt written from
    that detail carried 0 `left:` lines. The model was told WHICH tests it
    broke and never WHAT they printed, and the next reply broke the same ten.

    Same extraction as :func:`failure_evidence_by_test`; matched on the last
    `::` segment because the comparison keys on bare names while libtest
    prints the module path. A name with no captured section yields no entry.
    """
    wanted = {n.rsplit("::", 1)[-1] for n in names if n}
    if not wanted:
        return {}
    out: dict[str, str] = {}
    for printed, text in failure_evidence_by_test(
            output, per_test_cap=per_test_cap).items():
        leaf = printed.rsplit("::", 1)[-1]
        if leaf in wanted and leaf not in out:
            out[leaf] = text
    return out


def parse_test_output(text: str) -> TestRunReport:
    """Parse raw `cargo test` output into a :class:`TestRunReport`. Failures
    carry the first panic location libtest printed so the caller can attribute
    a stub-panic to the fn it actually fired in. A failed test with no stdout
    section still appears (kind OTHER, no location)."""
    if not text:
        return TestRunReport(frozenset(), ())
    bodies: dict[str, str] = {}
    for name, body in split_test_stdout_sections(text):
        bodies.setdefault(name, body)   # first section wins
    # Some cargo versions emit no `---- name stdout ----` header for a
    # single-test run — the panic sits in the bare output. Fall back to the
    # whole text as the body so the panic still resolves. Gated on the text
    # actually holding a panic: an aborted process also prints no sections,
    # and handing back its bare `test NAME ... FAILED` lines as a "body" would
    # dress a verdict-less crash up as diagnosed evidence.
    no_sections = not bodies and "panicked at " in text
    failures: list[TestFailure] = []
    seen: set[str] = set()
    for name in sorted(parse_failed_tests(text)):
        if name in seen:
            continue
        seen.add(name)
        body = text if no_sections else bodies.get(name, "")
        pf, pl, pc, msg = parse_panic_location(body)
        failures.append(TestFailure(
            test_name=name, kind=_classify(msg, body),
            panic_file=pf, panic_line=pl, panic_col=pc, body=body))
    passed = frozenset(_passed_names(text) - seen)
    return TestRunReport(passed, tuple(failures))


@dataclass(frozen=True, slots=True)
class BlameVerdict:
    """Partition of a unit's failing tests. `self_failures` count against the
    unit; `external` are stub-panics that fired in a DIFFERENT unit's stub —
    each paired with the fn locator they resolved to (the blocking producer);
    `no_verdict` are failures libtest named but never printed output for, which
    can neither convict nor exonerate (see :func:`partition_blame`)."""

    self_failures: tuple[TestFailure, ...] = ()
    external: tuple[tuple[TestFailure, FnSite], ...] = ()
    no_verdict: tuple[TestFailure, ...] = ()

    @property
    def blocking_fns(self) -> frozenset[FnSite]:
        return frozenset(loc for _f, loc in self.external)


def self_aborted_tests(missing: frozenset[str],
                       judged_at_baseline: frozenset[str]) -> frozenset[str]:
    """The unit's OWN tests whose verdict this body destroyed: judged in a
    completed binary at baseline, no verdict at all now.

    :func:`partition_blame` files an output-less failure as NO_VERDICT because,
    read in isolation, silence can neither convict nor exonerate. Held against
    a baseline the same silence inverts into the strongest signal there is: the
    binary ran these tests to a verdict before the splice and died before
    reaching them after it, so the body under test aborted the process. Nothing
    else the unit can be scored on outranks that — an abort takes every later
    test in the binary down with it, so a gate that stays silent here reverts
    on two unrelated assertions while the body it is scoring quietly removes
    dozens of verdicts from the run."""
    return frozenset(missing & judged_at_baseline)


def _site_key(s: FnSite) -> tuple[str, str, str]:
    """Collision-safe identity: file, name, and CANONICAL container. Canonical
    because the extractor renders a header's spacing (`impl Bool for & bool`)
    differently from the impl-manifest a unit was built from (`&bool`); an
    exact-string container compare would miss the match and misclassify a unit's
    OWN failure as external."""
    return (s.file, s.fn_name, canon_container(s.container))


def partition_blame(
    report: TestRunReport,
    unit_sites: frozenset[FnSite],
    locate: Callable[[str, int], FnSite | None],
) -> BlameVerdict:
    """Split `report.failures` into self vs external. A failure is EXTERNAL iff
    it is a STUB_PANIC whose panic location resolves (via `locate`) to a fn NOT
    in `unit_sites` — matched on (file, name, CANONICAL container), so a panic
    in a same-named SIBLING (the free `fn not(cond)` vs a `Bool` impl's `fn
    not`; one `From` impl's `fn from` vs another) is correctly external, not
    charged to this unit. ASSERTION/OTHER failures always stay self — an
    assertion can be another unit's wrong body, but that is behavioral
    attribution, out of scope here (do not over-subtract).

    A failure with an EMPTY body is NO_VERDICT, never self. libtest always
    prints a `---- name stdout ----` section for a test that fails normally, so
    a bare `test NAME ... FAILED` with nothing captured means the process died
    before the summary (a panic while unwinding out of a stubbed `Drop` aborts,
    taking every section with it). Which tests printed before the abort varies
    run to run, so such a failure carries no information about the unit under
    test: charging it convicts the unit of a crash somewhere else, with no
    diagnostic to act on. Same rule the regression gate applies to already-
    crashed binaries — evidence-free means it can neither convict nor
    exonerate."""
    own = {_site_key(s) for s in unit_sites}
    self_f: list[TestFailure] = []
    external: list[tuple[TestFailure, FnSite]] = []
    no_verdict: list[TestFailure] = []
    for f in report.failures:
        if not f.body:
            no_verdict.append(f)
            continue
        if (f.kind is FailureKind.STUB_PANIC
                and f.panic_file and f.panic_line is not None):
            loc = locate(f.panic_file, f.panic_line)
            if loc is not None and _site_key(loc) not in own:
                external.append((f, loc))
                continue
        self_f.append(f)
    return BlameVerdict(tuple(self_f), tuple(external), tuple(no_verdict))


@dataclass(frozen=True, slots=True)
class LocatedPanic:
    """One ``panicked at`` occurrence a capture printed, with the location
    libtest gave it and the message that followed."""

    file: str
    line: int
    message: str

    @property
    def site(self) -> tuple[str, int]:
        return (self.file, self.line)

    @property
    def is_stub(self) -> bool:
        """An `unimplemented!()` / `todo!()` stub speaking — the panic names
        the hollow fn's own line."""
        return any(m in self.message for m in _STUB_MARKERS)

    @property
    def is_runtime_abort(self) -> bool:
        """The runtime's double-panic announcement (see
        `_RUNTIME_ABORT_MESSAGES`): located in the runtime's own sources AND
        carrying one of the announced messages. Neither half alone: a crate
        panic can be located in `library/` (a `capacity overflow` inside
        `alloc`), and the message alone on a crate-located line would be a
        test that printed it."""
        return (_RUNTIME_SOURCE_MARK in self.file
                and any(m in self.message for m in _RUNTIME_ABORT_MESSAGES))


def _iter_located_panics(
    text: str,
) -> Iterator[LocatedPanic | None]:
    """Every ``panicked at`` occurrence in a raw capture →
    :class:`LocatedPanic`, or None for one whose location does not parse
    (unknown evidence). Same two libtest forms as
    :func:`parse_panic_location`, but ALL occurrences: a crash capture can
    hold several — the drop panic that aborted the process plus the panic it
    interrupted, plus the runtime's announcement of the pair — and
    attribution must see every one before it may exonerate."""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        idx = ln.find("panicked at ")
        if idx < 0:
            continue
        after = ln[idx + len("panicked at "):]
        if after.startswith("'"):
            j = after.rfind("', ")
            if j <= 0:
                yield None
                continue
            msg = after[1:j]
            loc = after[j + 3:].strip()
        else:
            loc = after.rstrip()
            if loc.endswith(":"):
                loc = loc[:-1]
            msg = ""
            for nxt in lines[i + 1:]:
                if nxt.strip():
                    msg = nxt.strip()
                    break
        parsed = _split_location(loc)
        if parsed is None:
            yield None
            continue
        pf, pl, _pc = parsed
        yield LocatedPanic(pf, pl, msg)


@dataclass(frozen=True, slots=True)
class CrashPanics:
    """What the CRASHED binaries of a capture printed about why they died —
    every located panic in those binaries' own sections, sorted into the
    three kinds attribution keys on, plus the two kinds of missing evidence.

    ``stub_sites`` — `(file, line)` of `not implemented` panics: a hollow
    sibling speaking. ``crate_panics`` — every other located crate panic
    (an assertion, an `unwrap` on `None`, an index out of range): the
    fill's own doing, or at least not the stubs'. ``runtime_aborts`` — the
    runtime's double-panic announcements (`LocatedPanic.is_runtime_abort`):
    positive evidence the abort was a panic in `Drop` and not undefined
    behaviour, naming no body, counted so a renderer can say so and never
    a veto. ``unparseable`` — `panicked at` lines with no location:
    unknown evidence. ``missing`` — sections the caller asked to judge that
    the capture does not hold: no evidence at all."""

    stub_sites: frozenset[tuple[str, int]]
    crate_panics: tuple[LocatedPanic, ...]
    runtime_aborts: int
    unparseable: int
    sections: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def stub_only(self) -> bool:
        """The crash is exonerating evidence: at least one stub panic, and
        nothing else spoke — no crate panic, no unparseable line, no section
        the capture could not show. A segfault (no panic at all) is not
        stub-only; neither is the runtime's announcement on its own."""
        return (bool(self.stub_sites) and not self.crate_panics
                and not self.unparseable and not self.missing)

    @property
    def rendered_stub_sites(self) -> str:
        return ", ".join(f"{f}:{ln}" for f, ln in sorted(self.stub_sites))


def crash_panics(
    raw_output: str, crashed: Iterable[str] | None = None,
) -> CrashPanics:
    """Read the panics of the binaries named by ``crashed`` — labels as
    :func:`test_results.parse_binary_results` gives them — out of their own
    sections of ``raw_output`` (`binary_output_sections`: the stdout under
    the binary's header plus the stderr under its ``Running`` line).
    ``None`` judges every section that opened and never printed its
    trailer, the aborted binaries of a scoped run.

    Per SECTION, never over the whole capture. `compare_readings` already
    splits a crash into ``newly_crashed`` (complete at baseline with a
    pass — the cost) and ``crashed_all_red`` (complete at baseline with no
    pass — the failure mode moved, not charged), and a completed binary's
    failures are verdicts the comparison reads as ``newly_red``. A rule
    over the whole capture let an assertion panic in an all-red or
    completed binary veto the stub-only evidence of the binary that
    actually cost something: on the live `2585062-494e630e` fills
    (`newly_red=[]`, three to eight `newly_crashed`, every one paired with
    a `crash_all_red` guard on `error_gen_3` and friends), the whole-text
    read was `frozenset()` on every fill, and each of the seven `ptr.rs`
    route-runs reverted the textbook `Own::new`."""
    sections = binary_output_sections(raw_output)
    by_label: dict[str, list[str]] = {}
    for s in sections:
        by_label.setdefault(s.label, []).append(s.text)
    wanted = (tuple(crashed) if crashed is not None
              else tuple(s.label for s in sections if not s.completed))
    stub_sites: set[tuple[str, int]] = set()
    crate: list[LocatedPanic] = []
    runtime = unparseable = 0
    judged: list[str] = []
    missing: list[str] = []
    for label in wanted:
        texts = by_label.get(label)
        if not texts:
            missing.append(label)
            continue
        judged.append(label)
        for p in _iter_located_panics("\n".join(texts)):
            if p is None:
                unparseable += 1
            elif p.is_runtime_abort:
                runtime += 1
            elif p.is_stub:
                stub_sites.add(p.site)
            else:
                crate.append(p)
    return CrashPanics(
        stub_sites=frozenset(stub_sites), crate_panics=tuple(crate),
        runtime_aborts=runtime, unparseable=unparseable,
        sections=tuple(judged), missing=tuple(missing))


def stub_only_crash(
    raw_output: str, crashed: Iterable[str] | None = None,
) -> frozenset[tuple[str, int]]:
    """The (file, line) stub-panic sites of the ``crashed`` binaries, iff
    EVERY crate panic those binaries printed is a stub panic
    (``unimplemented!()``/``todo!()`` markers) — else empty. See
    :func:`crash_panics` for the section rule and ``crashed``'s default.

    This is the evidence that separates "the fill is bad unsafe code" from
    "the fill correctly reached a still-hollow sibling": a stub panic names
    the hollow fn's own line, while a segfault prints no panic at all and an
    assertion panic carries a real message. ALL-or-nothing WITHIN the
    crashed sections on purpose — an assertion panic followed by a stub
    panic in the unwind's Drop is real self-evidence, and partial
    attribution must not exonerate. An unparseable ``panicked at`` line
    likewise vetoes: unknown evidence is not exculpatory evidence. The
    runtime's own double-panic announcement is neither: it is the
    announcement OF the stub panic in `Drop`, and it is ignored here (never
    a veto, never a site)."""
    panics = crash_panics(raw_output, crashed)
    return panics.stub_sites if panics.stub_only else frozenset()


def stub_sites_outside(
    sites: Iterable[tuple[str, int]],
    unit_sites: frozenset[FnSite],
    locate: Callable[[str, int], FnSite | None],
) -> frozenset[tuple[str, int]]:
    """The subset of stub-panic ``sites`` that resolve to a fn OUTSIDE
    `unit_sites` — the hollow siblings, as `(file, line)`. A site that does
    not locate or that resolves into the unit is dropped."""
    own = {_site_key(s) for s in unit_sites}
    out: set[tuple[str, int]] = set()
    for pf, pl in sites:
        loc = locate(pf, pl)
        if loc is not None and _site_key(loc) not in own:
            out.add((pf, pl))
    return frozenset(out)


def external_stub_sites_of(
    panics: CrashPanics,
    unit_sites: frozenset[FnSite],
    locate: Callable[[str, int], FnSite | None],
) -> frozenset[FnSite]:
    """The still-stubbed fns OUTSIDE `unit_sites` whose stub panics are the
    ONLY crate panics in the judged crashed sections — the blockers an
    abort is attributable to. Empty when any panic is not a stub panic,
    fails to locate, or resolves inside the unit itself: in each of those
    cases the abort may be the unit's own doing and must stay charged."""
    if not panics.stub_only:
        return frozenset()
    own = {_site_key(s) for s in unit_sites}
    out: set[FnSite] = set()
    for pf, pl in panics.stub_sites:
        loc = locate(pf, pl)
        if loc is None or _site_key(loc) in own:
            return frozenset()
        out.add(loc)
    return frozenset(out)


def external_stub_crash_sites(
    raw_output: str,
    unit_sites: frozenset[FnSite],
    locate: Callable[[str, int], FnSite | None],
    crashed: Iterable[str] | None = None,
) -> frozenset[FnSite]:
    """:func:`external_stub_sites_of` over :func:`crash_panics` of
    ``raw_output``'s ``crashed`` sections (default: the aborted ones)."""
    return external_stub_sites_of(
        crash_panics(raw_output, crashed), unit_sites, locate)


class U2Decision(Enum):
    ACCEPT = auto()               # tests pass / gate off / inconclusive
    ACCEPT_EXTERNAL_ONLY = auto()  # only-external failures — keep fill, don't charge
    REJECT = auto()               # ≥1 real self failure — revert + re-prompt


@dataclass(frozen=True, slots=True)
class U2Verdict:
    """Typed U2 acceptance decision (replaces the ``tuple[frozenset, str] |
    None`` protocol). ACCEPT/ACCEPT_EXTERNAL_ONLY keep the compiling fill and
    write no retro; REJECT reverts with `detail` as the next-prompt feedback
    and `self_failures` as the stall signature. `no_verdict` names failures
    that were excluded from blame for want of any captured output — carried so
    a caller renders WHY it accepted instead of logging "0 attributed"."""

    decision: U2Decision
    self_failures: frozenset[str] = frozenset()
    # The same failures as RECORDS, so a caller can compare where they panicked
    # and not merely that they panicked. `external` has carried its records
    # since it existed; `self_failures` was narrowed to names, and that is the
    # narrowing that left the non-regression gate blind. On the pass-92 defect
    # every attributed test is red before AND after every candidate body, so
    # `new_failures` is empty for the correct body and for the delegation
    # alike — which is how the 2026-08-20 regression was KEPT ("no self-failure
    # is newly red"). The panic SITE separates them: a test that panics later
    # in the same file passed strictly more assertions than before.
    self_failure_sites: tuple[TestFailure, ...] = ()
    external: tuple[tuple[TestFailure, FnSite], ...] = ()
    detail: str = ""
    no_verdict: frozenset[str] = frozenset()
    # Attributed tests observed PASSING in the same scoped run — the positive
    # evidence comparative banking requires (a test absent from the output is
    # never "resolved", it is unmeasured).
    self_passed: frozenset[str] = frozenset()
    # A REJECT for an abort: the attributed tests whose verdict the scoped
    # binary destroyed (`self_aborted_tests`), what the aborted sections
    # printed (`crash_panics`), and the stub-panic sites among them that
    # resolve OUTSIDE the unit. The caller's retro writer stamps the record
    # it earns from this verdict with those sites (`crash_stub_sites`), so a
    # conviction reached on an abort the tree's holes decided is never an
    # honoured exhaustion, and the failed-attempt comment names the stubs
    # rather than calling the body wrong. Empty / None on every other
    # decision.
    aborted: frozenset[str] = frozenset()
    crash: CrashPanics | None = None
    external_stub_sites: frozenset[tuple[str, int]] = frozenset()

    @classmethod
    def accept(cls, self_passed: frozenset[str] = frozenset()) -> U2Verdict:
        """ACCEPT covers three different states — the unit's tests PASSED, the
        gate was off or had no tests to run, and the run was inconclusive
        (timeout, exception, fail-safe). They are one decision (keep the
        compiling body) and they are NOT one measurement, so the caller that
        records a verdict has to be able to tell them apart: `self_passed`
        non-empty is the only one that saw an attributed test pass.

        Run `1064759-0f23fb68` (2026-08-15/16) is the cost of conflating them:
        50 fills recorded `accept`/POSITIVE while the per-binary verdict vector
        stayed byte-identical across the whole run. Every ledger consumer —
        the retro round, the payoff detector, the calibration this project owes
        its shadow detector — was reading "50 successes" off a night that
        changed nothing."""
        return cls(U2Decision.ACCEPT, self_passed=self_passed)

    @classmethod
    def accept_external_only(
        cls, external: tuple[tuple[TestFailure, FnSite], ...],
        no_verdict: frozenset[str] = frozenset(),
    ) -> U2Verdict:
        return cls(U2Decision.ACCEPT_EXTERNAL_ONLY, external=external,
                   no_verdict=no_verdict)

    @classmethod
    def reject(cls, self_failures: frozenset[str], detail: str,
               no_verdict: frozenset[str] = frozenset(),
               self_passed: frozenset[str] = frozenset(),
               sites: tuple[TestFailure, ...] = (),
               aborted: frozenset[str] = frozenset(),
               crash: CrashPanics | None = None,
               external_stub_sites: frozenset[tuple[str, int]] = frozenset(),
               ) -> U2Verdict:
        return cls(U2Decision.REJECT, self_failures=self_failures,
                   detail=detail, no_verdict=no_verdict,
                   self_passed=self_passed, self_failure_sites=sites,
                   aborted=aborted, crash=crash,
                   external_stub_sites=external_stub_sites)

    @property
    def panic_sites(self) -> dict[str, tuple[str, int]]:
        """test name -> (panic file, panic line) for the self-failures that
        printed one. A failure with no location is ABSENT rather than defaulted:
        the comparison this feeds treats a missing site as unmeasured, and a
        sentinel line number would read as movement."""
        return {f.test_name: (f.panic_file, f.panic_line)
                for f in self.self_failure_sites
                if f.panic_file and f.panic_line is not None}

    @property
    def blocking_fns(self) -> frozenset[FnSite]:
        return frozenset(loc for _f, loc in self.external)


@dataclass(frozen=True)
class JointImprovement:
    """Comparative banking assessment for JOINT fill routes only (file-first,
    impl-block). Per-fn acceptance stays absolute — a single fn that reddens
    its own test is simply wrong — but where one prompt rewrites a coupled
    family, the only REACHABLE step may be FEWER self-failures, and an
    absolute gate discards exactly the improvements the iter-level ratchet
    exists to bank. Vanished/aborted tests count by PRIOR state: a test that
    was failing and now aborts is still failing, not newly broken; a
    previously-passing test lost to an abort is caught by the global
    regression gate, which still runs unchanged.

    ``baseline_failing`` — the unit's attributed tests not observed passing
    (and not cfg-ignored) at the fresh in-lock baseline. ``resolved`` — the
    subset observed PASSING in the fill's own scoped run: banking demands
    POSITIVE evidence (a test absent from both readings is unmeasured, not
    fixed — counting absence as progress fabricated improvement). ``new_failures``
    — self-failures outside the baseline-failing set. Banks iff something
    resolved AND nothing new broke."""

    baseline_failing: frozenset[str]
    resolved: frozenset[str]
    new_failures: frozenset[str]
    #: (test, file, line before, line after) for attributed tests whose panic
    #: moved WITHIN one file. `regressed` is an EARLIER line — strictly fewer
    #: assertions passed than before the fill; `advanced` is a later one.
    regressed_sites: tuple[tuple[str, str, int, int], ...] = ()
    advanced_sites: tuple[tuple[str, str, int, int], ...] = ()

    @property
    def unresolved(self) -> frozenset[str]:
        return self.baseline_failing - self.resolved

    @property
    def banks(self) -> bool:
        return bool(self.resolved) and not self.new_failures

    @property
    def site_movement(self) -> str:
        """One line for the log, or "" when nothing moved. A sweep that reports
        a byte-identical verdict vector while panic lines moved later is not
        flat, and the vector cannot see it."""
        def _fmt(rows: tuple[tuple[str, str, int, int], ...]) -> str:
            return ", ".join(f"{t} {f}:{a}->{b}" for t, f, a, b in rows[:3])
        parts = []
        if self.advanced_sites:
            parts.append(f"{len(self.advanced_sites)} test(s) panic LATER "
                         f"(more assertions passed): {_fmt(self.advanced_sites)}")
        if self.regressed_sites:
            parts.append(f"{len(self.regressed_sites)} test(s) panic EARLIER: "
                         f"{_fmt(self.regressed_sites)}")
        return "; ".join(parts)

    @property
    def summary(self) -> str:
        shown = ", ".join(sorted(self.resolved)[:5]) + (
            "…" if len(self.resolved) > 5 else "")
        return (f"self-failures {len(self.baseline_failing)}→"
                f"{len(self.unresolved)} (resolved: {shown})")


def compare_panic_sites(
    before: Mapping[str, tuple[str, int]],
    after: Mapping[str, tuple[str, int]],
) -> tuple[tuple[tuple[str, str, int, int], ...],
           tuple[tuple[str, str, int, int], ...]]:
    """(regressed, advanced) — attributed tests whose panic moved to an
    EARLIER / LATER line of the SAME file.

    Why order within one file and not "a site the pre-fill tree did not have".
    Every panic site on this crate's 13 failures is an ASSERTION LINE in a test
    file, never a site in `src/` (measured 2026-08-21 in a scratch copy:
    `tests/test_downcast.rs:17:56`, `tests/test_context.rs:103:5`,
    `tests/test_boxed.rs:26:14`, …). A body that makes
    `err.is::<HighLevel>()` at `test_context.rs:103` pass and then fails
    `downcast_ref` at `:105` has moved the panic to a line the pre-fill tree
    did NOT have — so the "new site" formulation would refuse the one thing
    that is unambiguous progress. Order is the gradient: later is strictly more
    assertions passed, earlier is strictly fewer.

    A test missing from either reading, or one whose panic moved to a DIFFERENT
    file, is unmeasured and appears in neither list. Two files cannot be
    ordered against each other, and calling that a regression would charge a
    body for a comparison nobody made."""
    regressed: list[tuple[str, str, int, int]] = []
    advanced: list[tuple[str, str, int, int]] = []
    for test, (bf, bl) in sorted(before.items()):
        site = after.get(test)
        if site is None:
            continue
        af, al = site
        if af != bf or al == bl:
            continue
        (regressed if al < bl else advanced).append((test, bf, bl, al))
    return tuple(regressed), tuple(advanced)


def assess_joint_improvement(
    attributed: frozenset[str],
    baseline_passed: frozenset[str],
    baseline_ignored: frozenset[str],
    verdict: U2Verdict,
    baseline_sites: Mapping[str, tuple[str, int]] | None = None,
) -> JointImprovement:
    """Build the :class:`JointImprovement` for a joint fill judged REJECT by
    the absolute U2 gate. ``attributed − passed − ignored`` (rather than the
    baseline's failed names) is the honest "was failing" set: a test whose
    baseline binary crashed printed no verdict at all, yet it was certainly
    not passing. Resolution comes only from ``verdict.self_passed`` — the
    same scoped run that produced the failures."""
    baseline_failing = attributed - baseline_passed - baseline_ignored
    # Scoped to the attributed tests: a panic that moved in a test this unit
    # does not answer for is somebody else's measurement.
    _before = {t: site for t, site in (baseline_sites or {}).items()
               if t in attributed}
    regressed, advanced = compare_panic_sites(_before, verdict.panic_sites)
    return JointImprovement(
        baseline_failing=frozenset(baseline_failing),
        resolved=frozenset(verdict.self_passed & baseline_failing),
        new_failures=frozenset(verdict.self_failures - baseline_failing),
        regressed_sites=regressed, advanced_sites=advanced)


def cap_at_line_boundary(text: str, cap: int) -> str:
    """Trim `text` to at most `cap` chars, cutting at a LINE boundary so an
    excerpt never ends mid-diagnostic (`- th…`). Keeps the TAIL (the freshest
    output); if the last line alone exceeds `cap`, hard-slice the tail."""
    if len(text) <= cap:
        return text
    tail = text[-cap:]
    nl = tail.find("\n")
    return tail[nl + 1:] if 0 <= nl < len(tail) - 1 else tail


@dataclass(frozen=True, slots=True)
class FailureExcerpt:
    """One place the excerpt rule lives (F8): the failing-test NAME list is
    NEVER truncated (names are short and identify the target); only the panic
    BODIES are capped, and only at line boundaries."""

    names: tuple[str, ...]
    bodies: tuple[tuple[str, str], ...]   # (test_name, body) in order

    def render(self, cap: int) -> str:
        head = (f"This compiled, but these tests still FAIL: {list(self.names)}"
                "\n\nFix the BEHAVIOR so they pass.\n")
        joined = "\n\n".join(f"---- {n} stdout ----\n{b}"
                             for n, b in self.bodies if b)
        return head + (cap_at_line_boundary(joined, cap) if joined else "")


class U2TestGate(Protocol):
    """The composition-bound U2 acceptance gate a fill loop calls once its unit
    compiles: run the unit's own tests, partition blame, return a typed verdict.
    `unit_sites` is what the unit owns as container-qualified :class:`FnSite`
    identities — used to subtract same-file/same-name SIBLING stub-panics that a
    name-only key would wrongly charge to this unit."""

    def __call__(
        self, fn_tests: set[str], unit_sites: frozenset[FnSite],
        test_cmd: str | None, target_src_dir: Path,
        cluster_env: dict[str, str] | None,
    ) -> U2Verdict: ...
