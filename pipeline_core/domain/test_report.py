"""Structured test-run report + blame partition (pure domain).

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
from dataclasses import dataclass, field, replace
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from pipeline_core.domain.assertion_position import (
    KeyedVerdicts,
    PositionMove,
    SuiteDelta,
    TestKey,
    TestOutcomes,
    TestSource,
    assertion_positions,
    leaf,
    outcomes_of,
)
from pipeline_core.domain.crash_site import CrashSite
from pipeline_core.domain.models import FnSite
from pipeline_core.domain.rust_idents import canon_container
from pipeline_core.domain.test_results import (
    WORLD_KEY_SEP,
    TestStateReading,
    binary_output_sections,
    failure_bodies_for,
    parse_binary_results,
    parse_failed_tests,
    split_test_stdout_sections_in_worlds,
    strip_libtest_qualifier,
    world_failure_line,
)

if TYPE_CHECKING:
    from pipeline_core.domain.attribution import WriteGround


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
    # The build world whose part of a joined capture (`test_results
    # .join_world_captures`) holds `body`; "" for a capture of one world.
    # Required, so a producer cannot drop it by omission: the sweep of the
    # one-tree loop judges every world, and a failure read without its
    # world was quoted to the fill model as the home world's (run
    # `1700492-f6c4d66c`, 2026-10-24).
    world: str
    # The same name's failures in OTHER test binaries, each with its own
    # panic and section, in capture order. A name is not a test: two
    # integration binaries may each hold a `test_downcast_ref`, and with the
    # first section winning the second binary's panic reached no reader —
    # run `3893442-cb547388` (2026-09-19): `tests/test_context.rs`'s copy
    # asserted a call the session could not edit, `tests/test_downcast.rs`'s
    # copy was the one measured. The primary fields stay the first copy's,
    # so every reader that asks "where did this name fail" answers as before;
    # the readers that must see every copy (the evidence a prompt quotes,
    # the premise, the truncation reading, the keep's blame partition)
    # read these too. The partition quotes a copy that is this write's
    # own failure when one exists, rather than the first section.
    others: tuple[TestFailure, ...] = ()


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


@dataclass(frozen=True, slots=True)
class HeldTests:
    """Tests a write is judged on, as the frontier holds them, matched
    against names as libtest printed them.

    The frontier keys a test by its leaf (`bare_test_name` at the sweep);
    libtest prints a test that lives in a module with its path
    (`counter::gen_tests_1::counter_bumps`). A printed name is a held test
    when the two leaves are equal, the identity `TestKey` already uses. On
    run `3669371-9d5a7361` (hardcrate, 2026-11-06) the keep gate compared
    the two forms as strings: six fill-1 keeps whose own inline-module test
    FAILED were logged `reported no verdict (binary crashed)` and kept
    unjudged.

    `names` stays readable for the scoped command (libtest's filter matches
    the leaf as a substring) and for display. A comparison between a
    printed name and held names goes through `held_as` / `held_of` /
    `missing_from` / `failures_in`, and the names they return are the held
    ones, the form every frontier-keyed reader downstream expects.
    `tests/test_held_tests.py` refuses a raw membership or set operation
    between the two forms inside the keep gate."""

    names: frozenset[str]

    def held_as(self, printed: str) -> str | None:
        """The held name `printed` is a report of, or None. Two held names
        with one leaf (a qualified and a bare spelling of one test) resolve
        to the first in sorted order."""
        want = leaf(printed)
        hits = sorted(n for n in self.names if leaf(n) == want)
        return hits[0] if hits else None

    def held_of(self, printed: Iterable[str]) -> frozenset[str]:
        """The held names some printed name reports on: every spelling of
        each reported test, because a test is its leaf.

        The ground can hold one test twice, bare and module-qualified
        (`counter_bumps` and `counter::gen_tests_1::counter_bumps`). Until
        2026-11-06 this resolved each printed name to `held_as`'s one pick,
        so the other spelling was never reported and `missing_from` called
        it a test with no verdict. On run `79280-8bd7cb71` (hardcrate, fill
        1) all seven `u2_missing_verdict … (binary crashed)` guards were
        that: every judged lib binary completed, and each keep was charged
        as an abort and kept on crash-evidence non-regression instead of
        judged on its own failure."""
        leaves = {leaf(p) for p in printed}
        return frozenset(n for n in self.names if leaf(n) in leaves)

    def missing_from(self, printed: Iterable[str]) -> frozenset[str]:
        """The held names no printed name reports on (no printed name has
        their leaf)."""
        return self.names - self.held_of(printed)

    def failures_in(self, report: TestRunReport) -> tuple[TestFailure, ...]:
        """The report's failures of held tests, as printed (their bodies
        are the evidence)."""
        return tuple(f for f in report.failures
                     if self.held_as(f.test_name) is not None)


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

    In a capture that joined build worlds every copy names the world it
    failed in (`world_failure_line` above the first copy, the world in each
    other copy's line). The text named none, and the fill prompt said the
    tests run in the home world: run `1700492-f6c4d66c` (2026-10-24), the
    one red test failed only in `nightly::tests/doc_error_gen_6.rs`. The
    same binary failing in two worlds is not "a different test binary";
    that line now says which. A one-world capture reads as before.
    """
    out: dict[str, str] = {}
    for f in parse_test_output(output).failures:
        text = _evidence_of(f.body, per_test_cap)
        if text and f.world:
            text = f"{world_failure_line(f.world)}\n{text}"
        # Every binary's failure of the name: the model must see the panic
        # of the copy whose asserted call it is asked to fix.
        extra = [(o, e) for o, e in ((o, _evidence_of(o.body, per_test_cap))
                                     for o in f.others) if e]
        if extra:
            text = "\n".join(
                [text or "(no captured output)"]
                + [f"{OTHER_COPY_MARK} {f.test_name}, {_copy_place(f, o)}, "
                   f"also failed:)\n{e}" for o, e in extra])
        if text:
            out[f.test_name] = text
    return out


def _copy_place(primary: TestFailure, copy: TestFailure) -> str:
    """Where another copy of a failing name failed, relative to the first:
    another world (the same binary may be the one that failed there), or
    another binary of the same world."""
    if copy.world and copy.world != primary.world:
        return f"in cfg world `{copy.world}`"
    if copy.world:
        return f"in a different test binary, in cfg world `{copy.world}`"
    return "in a different test binary"


def _evidence_of(body: str, per_test_cap: int) -> str:
    """The panic line and what follows it, capped; "" for an empty body."""
    if not body.strip():
        return ""
    keep: list[str] = []
    started = False
    for ln in body.splitlines():
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
        keep = [s for s in (ln.strip() for ln in body.splitlines()) if s][:6]
    text = "\n".join(keep)
    if len(text) > per_test_cap:
        text = text[:per_test_cap].rstrip() + " …"
    return text


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


#: Opens the part of a name's evidence that is another binary's failure of
#: that name (`failure_evidence_by_test`); readers that act per panic split
#: on it (`refill_evidence.quote_panic_site`).
OTHER_COPY_MARK = "(another test named"


def parse_test_output(text: str) -> TestRunReport:
    """Parse raw `cargo test` output into a :class:`TestRunReport`. Failures
    carry the first panic location libtest printed so the caller can attribute
    a stub-panic to the fn it actually fired in. A failed test with no stdout
    section still appears (kind OTHER, no location)."""
    if not text:
        return TestRunReport(frozenset(), ())
    sections: dict[str, list[tuple[str, str]]] = {}
    for world, name, body in split_test_stdout_sections_in_worlds(text):
        sections.setdefault(name, []).append((world, body))
    # The first section is the name's primary failure; a further section
    # under the same name is another binary's test of that name, or the
    # same binary's in another world.
    bodies = {name: bs[0] for name, bs in sections.items()}
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
        # A name with no section has no world to read: "" (the one-world
        # reading), as for a capture that joined no worlds.
        world, body = (("", text) if no_sections
                       else bodies.get(name, ("", "")))
        pf, pl, pc, msg = parse_panic_location(body)
        others: list[TestFailure] = []
        for o_world, extra in ([] if no_sections
                               else sections.get(name, [])[1:]):
            opf, opl, opc, omsg = parse_panic_location(extra)
            others.append(TestFailure(
                test_name=name, kind=_classify(omsg, extra),
                panic_file=opf, panic_line=opl, panic_col=opc, body=extra,
                world=o_world))
        failures.append(TestFailure(
            test_name=name, kind=_classify(msg, body),
            panic_file=pf, panic_line=pl, panic_col=pc, body=body,
            world=world, others=tuple(others)))
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
    dozens of verdicts from the run.

    `missing` are held names (the gate's ground, leaves); the baseline's
    names are as libtest printed them. They meet through `HeldTests`: a
    string intersection never charged a module test (run
    `3669371-9d5a7361`, 2026-11-06)."""
    return HeldTests(frozenset(missing)).held_of(judged_at_baseline)


def _site_key(s: FnSite) -> tuple[str, str, str]:
    """Collision-safe identity: file, name, and CANONICAL container. Canonical
    because the extractor renders a header's spacing (`impl Bool for & bool`)
    differently from the impl-manifest a unit was built from (`&bool`); an
    exact-string container compare would miss the match and misclassify a unit's
    OWN failure as external."""
    return (s.file, s.fn_name, canon_container(s.container))


def _no_untouched_arm(_file: str, _line: int) -> bool:
    return False


@dataclass(frozen=True, slots=True)
class UnitScope:
    """What a write owns, for every reader that splits a crate panic into
    the write's own and another unit's.

    A site is `(file, name, canonical container)`, and two definitions of
    one name under complementary `#[cfg]`s are one site. A write fills one
    of them (per-fn locates the admitted stub by its `cfg_predicates`), so
    a stub panic in the OTHER definition, which still holds the skeleton's
    whole-body stub, is a still-stubbed sibling body, not this write's
    failure. `untouched_arm(file, line)` answers that from the extractor's
    records (`attribution.make_untouched_arm_probe`). On run
    `3669371-9d5a7361` (2026-11-06) `src/lib.rs::cap_limit`'s stable arm
    stayed a stub after fill 1 wrote the `extra_cap` arm; once the gate
    matched module test names, that stub would have convicted the written
    arm on every attempt the splice could not aim at the other one.

    Built by the keep gate; `partition_blame`, `stub_sites_outside` and
    `external_stub_sites_of` take it (a bare site set means no arm is
    untouched)."""

    sites: frozenset[FnSite]
    untouched_arm: Callable[[str, int], bool] = _no_untouched_arm

    def outside(self, loc: FnSite, file: str, line: int) -> bool:
        """`loc`, located from `file:line`, is not a body this write owns."""
        own = {_site_key(s) for s in self.sites}
        return _site_key(loc) not in own or self.untouched_arm(file, line)


def _scope_of(unit: frozenset[FnSite] | UnitScope) -> UnitScope:
    return unit if isinstance(unit, UnitScope) else UnitScope(frozenset(unit))


def _copy_site_outside(
    copy: TestFailure, scope: UnitScope,
    locate: Callable[[str, int], FnSite | None],
) -> FnSite | None:
    """The site a stub panic in `copy` fired in, when that site is not a
    body this write owns. None when the copy is not such a panic: an
    empty section, an assertion, or a stub that resolves into the write
    (or to no fn). Those are decided by the caller."""
    if (copy.body and copy.kind is FailureKind.STUB_PANIC
            and copy.panic_file and copy.panic_line is not None):
        loc = locate(copy.panic_file, copy.panic_line)
        if loc is not None and scope.outside(
                loc, copy.panic_file, copy.panic_line):
            return loc
    return None


def partition_blame(
    report: TestRunReport,
    unit_sites: frozenset[FnSite] | UnitScope,
    locate: Callable[[str, int], FnSite | None],
) -> BlameVerdict:
    """Split `report.failures` into self vs external.

    A name that failed in more than one binary is one failure: the first
    section is the primary, and every later section of that name is
    `TestFailure.others`. Each copy is classified on its own. A copy is
    external only when it is a STUB_PANIC whose panic resolves (via
    `locate`) to a fn NOT in `unit_sites` — matched on (file, name,
    CANONICAL container), so a panic in a same-named SIBLING (the free
    `fn not(cond)` vs a `Bool` impl's `fn not`; one `From` impl's `fn
    from` vs another) and a stub panic in the untouched arm of a cfg
    pair are not this write's failure. An ASSERTION or any other
    non-stub stays self — an assertion can be another unit's wrong body,
    but that is behavioral attribution, out of scope here (do not
    over-subtract).

    The name is this write's failure when any copy is. The verdict quotes
    that copy, not whichever section the capture stored first. A stub
    panic in the home world used to be the whole decision: the assertion
    of the same name in another world sat on `others`, the name was read
    as external, and the keep never judged the assertion. The name is
    external only when every copy that has a body is a stub panic outside
    the write.

    A copy with an EMPTY body neither convicts nor exonerates. libtest
    always prints a `---- name stdout ----` section for a test that fails
    normally, so a bare `test NAME ... FAILED` with nothing captured means
    the process died before the summary (a panic while unwinding out of a
    stubbed `Drop` aborts, taking every section with it). Which tests
    printed before the abort varies run to run, so such a copy carries no
    information about the unit under test. Same rule the regression gate
    applies to already-crashed binaries."""
    scope = _scope_of(unit_sites)
    self_f: list[TestFailure] = []
    external: list[tuple[TestFailure, FnSite]] = []
    no_verdict: list[TestFailure] = []
    for f in report.failures:
        # One level, the same walk as the stub hold: the parse attaches
        # later sections on `others` and does not nest them.
        copies = (replace(f, others=()), *f.others)
        outside = [_copy_site_outside(c, scope, locate) for c in copies]
        selves = [c for c, loc in zip(copies, outside, strict=True)
                  if c.body and loc is None]
        if selves:
            lead = selves[0]
            self_f.append(replace(
                lead, others=tuple(c for c in copies if c is not lead)))
            continue
        seen: set[FnSite] = set()
        for c, loc in zip(copies, outside, strict=True):
            if loc is not None and loc not in seen:
                seen.add(loc)
                external.append((c, loc))
        if seen:
            continue
        no_verdict.append(f)
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


def located_panics(text: str) -> tuple[LocatedPanic, ...]:
    """Every ``panicked at`` occurrence of ``text`` whose location parses,
    in the order printed (:func:`_iter_located_panics` without the
    unknown-evidence entries). The order is the reading: in one test's own
    block the first is where the test first failed, and every later one was
    raised while it unwound."""
    return tuple(p for p in _iter_located_panics(text) if p is not None)


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
    aborted_tests: Iterable[str] = (),
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
    route-runs reverted the textbook `Own::new`.

    ``aborted_tests`` — tests whose OWN process aborted (the binary was
    re-run one test per process, `BinaryResult.aborted`). Each one's failure
    block is that process's whole evidence and is read by the same rule as a
    crashed section; a name with no block is ``missing``. An isolated abort
    through a still-stubbed drop path is the crashed-binary case one test
    wide, and reading only sections left it with no reader at all."""
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
    _tests = tuple(dict.fromkeys(aborted_tests))
    blocks = failure_bodies_for(raw_output, set(_tests)) if _tests else {}
    evidence: list[tuple[str, str | None]] = [
        (label, "\n".join(by_label[label]) if by_label.get(label) else None)
        for label in wanted]
    evidence += [(t, blocks.get(t)) for t in _tests]
    for label, text in evidence:
        if not text:
            missing.append(label)
            continue
        judged.append(label)
        for p in _iter_located_panics(text):
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
    aborted_tests: Iterable[str] = (),
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
    panics = crash_panics(raw_output, crashed, aborted_tests)
    return panics.stub_sites if panics.stub_only else frozenset()


def stub_sites_outside(
    sites: Iterable[tuple[str, int]],
    unit_sites: frozenset[FnSite] | UnitScope,
    locate: Callable[[str, int], FnSite | None],
) -> frozenset[tuple[str, int]]:
    """The subset of stub-panic ``sites`` that resolve to a fn OUTSIDE
    `unit_sites` — the hollow siblings, as `(file, line)`. A site that does
    not locate or that resolves into the unit is dropped."""
    scope = _scope_of(unit_sites)
    out: set[tuple[str, int]] = set()
    for pf, pl in sites:
        loc = locate(pf, pl)
        if loc is not None and scope.outside(loc, pf, pl):
            out.add((pf, pl))
    return frozenset(out)


def external_stub_sites_of(
    panics: CrashPanics,
    unit_sites: frozenset[FnSite] | UnitScope,
    locate: Callable[[str, int], FnSite | None],
) -> frozenset[FnSite]:
    """The still-stubbed fns OUTSIDE `unit_sites` whose stub panics are the
    ONLY crate panics in the judged crashed sections — the blockers an
    abort is attributable to. Empty when any panic is not a stub panic,
    fails to locate, or resolves inside the unit itself: in each of those
    cases the abort may be the unit's own doing and must stay charged."""
    if not panics.stub_only:
        return frozenset()
    scope = _scope_of(unit_sites)
    out: set[FnSite] = set()
    for pf, pl in panics.stub_sites:
        loc = locate(pf, pl)
        if loc is None or not scope.outside(loc, pf, pl):
            return frozenset()
        out.add(loc)
    return frozenset(out)


def external_stub_crash_sites(
    raw_output: str,
    unit_sites: frozenset[FnSite] | UnitScope,
    locate: Callable[[str, int], FnSite | None],
    crashed: Iterable[str] | None = None,
) -> frozenset[FnSite]:
    """:func:`external_stub_sites_of` over :func:`crash_panics` of
    ``raw_output``'s ``crashed`` sections (default: the aborted ones)."""
    return external_stub_sites_of(
        crash_panics(raw_output, crashed), unit_sites, locate)


@dataclass(frozen=True, slots=True)
class StubAbortReading:
    """What a sweep's aborted binaries say about the tree's holes.

    ``by_binary`` maps each aborted binary whose only crate panics are stub
    panics (:attr:`CrashPanics.stub_only`, judged on that binary's own
    section) to the still-stubbed fns those panics resolve to. Those fns are
    the whole reason the binary produced no verdicts: until they have bodies,
    every test in that binary is unmeasurable, whatever else is filled.

    The sweep already reads this evidence to FORGIVE — the batch ratchet's
    closure exemption and the fill gates' crash-only keep both say "the charge
    waits until the stubs fill". Nothing read it to FILL them. Dispatch took
    crash blame only from the native localizer, which finds no fault in a panic
    during unwind, and the schedule's hollow relation reads coverage, which an
    aborted binary never writes. Run `119837-09e3ac57`, cluster `nightly`,
    2026-09-15: ten binaries aborted on stub panics in five fns (23 at one
    vtable drop fn, 18 at a nightly request helper, 15 at a vtable ref fn); all
    five were attributed and on the frontier, logged "parked under the dispatch
    cap, not served", while two fills spent their whole budget on a pair whose
    tests sat in those binaries.

    Since test isolation re-runs an aborted binary one test per process, this
    reading only sees a binary isolation could not rebuild. The sweep's
    reader is `test_outcome.stub_hold_of`, which holds each failing test by
    the stub it first fails in and calls this for the vanished remainder."""

    by_binary: Mapping[str, frozenset[FnSite]]

    @property
    def blockers(self) -> frozenset[FnSite]:
        out: set[FnSite] = set()
        for sites in self.by_binary.values():
            out |= sites
        return frozenset(out)

    def hollow(
        self, tests: Iterable[str], test_files: Mapping[str, str],
    ) -> dict[str, frozenset[FnSite]]:
        """``{test: stubs}`` for each of ``tests`` declared in a binary this
        reading holds — the abort proves the binary reached those stubs, which
        is what "hollow" means to the schedule. A test declared under
        ``tests/`` is its own binary (cargo labels it by that path); any other
        declaring file compiles into the library's unit-test binary, labelled
        ``unittests <lib path>``. A test the index does not place, or a binary
        this reading does not hold, contributes nothing."""
        # A joined capture keys each binary by its world (`binary_key`); a test
        # file names the binary in every world, so a test is held by the union
        # of its binary's sites over the worlds that aborted it.
        by_bare: dict[str, set[FnSite]] = {}
        for key, sites in self.by_binary.items():
            by_bare.setdefault(key.rsplit(WORLD_KEY_SEP, 1)[-1], set()).update(sites)
        lib_labels = [lab for lab in by_bare if lab.startswith("unittests ")]
        out: dict[str, frozenset[FnSite]] = {}
        for t in tests:
            rel = test_files.get(t)
            if rel is None:
                continue
            if rel.startswith("tests/"):
                got = by_bare.get(rel)
            else:
                got = by_bare[lib_labels[0]] if len(lib_labels) == 1 else None
            if got:
                out[t] = frozenset(got)
        return out


def stub_abort_reading(
    raw_output: str,
    locate: Callable[[str, int], FnSite | None],
) -> StubAbortReading:
    """:class:`StubAbortReading` of a sweep's combined capture. Each aborted
    section is judged alone, with the same rule the fill gates keep on
    (:func:`crash_panics` over that one label): a section is admitted only when
    every crate panic it printed is a stub panic and every stub site locates.
    An assertion beside the stub panic, an unparseable panic line, or a site
    that resolves to nothing leaves that binary out — the abort may be a body's
    own doing, and a reading that names the wrong fn is worse than none."""
    by_binary: dict[str, frozenset[FnSite]] = {}
    for label in sorted({s.label for s in binary_output_sections(raw_output)
                         if not s.completed}):
        panics = crash_panics(raw_output, (label,))
        if not panics.stub_only:
            continue
        located = [locate(f, ln) for f, ln in sorted(panics.stub_sites)]
        if any(loc is None for loc in located):
            continue
        by_binary[label] = frozenset(loc for loc in located if loc is not None)
    return StubAbortReading(by_binary)


class U2Decision(Enum):
    ACCEPT = auto()               # tests pass / gate off / inconclusive
    ACCEPT_EXTERNAL_ONLY = auto()  # only-external failures — keep fill, don't charge
    REJECT = auto()               # ≥1 real self failure — revert + re-prompt
    # The gate was handed no test: the body is kept on compile alone and
    # nothing about it was measured. Its own decision, never an ACCEPT, so
    # no verdict row can read "none were failing" for a body no test ran
    # against (run `2129106-ead996ab`, 2026-10-28: 28 opened-run bodies).
    NO_TESTS = auto()


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
    # Whether the abort is evidence about THIS unit: a crate panic the
    # aborted sections printed resolves into one of its bodies, or the crash
    # settlement named one of them as the culprit. False for an abort no
    # reading attributed — the process died and nothing says the body under
    # test made it die. A lesson written from such a reject ("what this
    # attempt tried did not work") records an outcome another body decided:
    # run `79929-5a16b1d7` stored "I tried reading self.inner with
    # core::ptr::read and calling the vtable object_drop…" as exhausted for
    # the destructor whose file-route body then read `pass 100 -> 132`.
    abort_attributed: bool = False
    # The scoped `cargo test` capture this verdict was read from. Empty on
    # verdicts built without a run (tests, the accepts that never asked).
    # The same-crash keep reads native crashes from it; a summary string
    # is not that capture.
    capture: str = ""
    # The assertion positions of that capture (`assertion_positions`), keyed
    # by binary and test: what a keep compares with the baseline reading's.
    # A failure with no position (a panic in `src/`, a helper, a closure)
    # is absent, so no keep can order it.
    positions: Mapping[TestKey, int] = field(default_factory=dict)
    # The sites the gate's crash settlement localized under this attempt
    # (`crash_settler.Settlement.sites`): what `judge_refill` hands
    # `outcomes_of`, so an already-red test that now dies in a crate body is
    # judged by the abort partition (`abort_verdict`). Empty when nothing
    # settled: every red abort then reads unlocated, as before.
    crash_sites: tuple[CrashSite, ...] = ()
    # Those sites as the settlement rendered them (`crash_text_of`), for a
    # caller that stores the reject for a later prompt.
    crash_reading: str = ""
    # Whether a scoped test run produced this verdict: a handed test
    # reported one. False for the accepts that never asked (gate off, no
    # test command, a runner exception, a build that did not finish), for an
    # accept whose handed tests reported nothing (the scoped build failed,
    # or the binary died with nothing charged), and for NO_TESTS.
    ran: bool = True
    # Judged tests that are not convicting (`WriteGround`), ran into this
    # write and stayed red: an ACCEPT's reading of what the keep left red.
    # Empty on every other verdict.
    judged_red: frozenset[str] = frozenset()

    @property
    def reported(self) -> frozenset[str]:
        """Every test of the gate's ground the scoped run reported on: passed,
        failed (convicting or only judged), or killed by this write. What a
        keep is graded against (`fill_class.class_for_kept`'s
        ``attributed``); the file, impl and pair routes read it here, so a
        judged test that stayed red cannot drop out of one of them."""
        return (self.self_passed | self.self_failures | self.aborted
                | self.judged_red)

    @property
    def measured(self) -> bool:
        """A scoped run of this body's tests produced the verdict."""
        return self.ran and self.decision is not U2Decision.NO_TESTS

    @classmethod
    def no_tests(cls) -> U2Verdict:
        """The gate was handed no test for this body (:attr:`U2Decision.
        NO_TESTS`)."""
        return cls(U2Decision.NO_TESTS, ran=False)

    @classmethod
    def accept(cls, self_passed: frozenset[str] = frozenset(), *,
               ran: bool = False,
               judged_red: frozenset[str] = frozenset()) -> U2Verdict:
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
        changed nothing.

        ``ran`` is True only for the accept read off a completed scoped
        run; the three that never asked leave it False (:attr:`measured`)."""
        return cls(U2Decision.ACCEPT, self_passed=self_passed, ran=ran,
                   judged_red=judged_red)

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
               capture: str = "",
               positions: Mapping[TestKey, int] | None = None,
               abort_attributed: bool = False,
               crash_sites: tuple[CrashSite, ...] = (),
               crash_reading: str = "",
               ) -> U2Verdict:
        return cls(U2Decision.REJECT, self_failures=self_failures,
                   detail=detail, no_verdict=no_verdict,
                   self_passed=self_passed, self_failure_sites=sites,
                   aborted=aborted, crash=crash,
                   external_stub_sites=external_stub_sites,
                   capture=capture, positions=dict(positions or {}),
                   abort_attributed=abort_attributed,
                   crash_sites=crash_sites, crash_reading=crash_reading)

    @property
    def abort_unattributed(self) -> bool:
        """The reject lost verdicts to an abort that no reading put on this
        unit (`abort_attributed`)."""
        return bool(self.aborted) and not self.abort_attributed

    @property
    def blocking_fns(self) -> frozenset[FnSite]:
        return frozenset(loc for _f, loc in self.external)


def unmeasured_keep_reason(verdict: U2Verdict | None) -> str:
    """The verdict-row reason of a body kept with no attributed test
    observed passing, by what the gate actually did. One sentence used to
    cover all of these ("none were failing, or the scoped run was
    inconclusive"), and it was printed for 28 bodies the gate had been
    handed no test for (run `2129106-ead996ab`, 2026-10-28)."""
    if verdict is None:
        return ("kept — compiles; the test gate was not asked, so no "
                "scoped test ran")
    if verdict.decision is U2Decision.NO_TESTS:
        return ("kept — compiles; no test is attributed to this body, so "
                "no scoped test ran")
    if not verdict.ran:
        return ("kept — compiles; no attributed test reported a verdict "
                "(gate off, no test command, a run that did not finish, a "
                "scoped build that failed, or a binary that died)")
    return ("kept — compiles; its attributed tests ran and none was "
            "observed passing")


@dataclass(frozen=True)
class JointImprovement:
    """Comparative banking assessment the file-first, impl-block and pair
    routes' rejects read. Per-fn reads it only on a crash-graded or
    aborted attempt (`per_fn_reject_kept`); its plain reject stays
    absolute, because a single fn that reddens its own test is wrong and
    the fix loop's next attempt carries that assertion. Where one prompt
    rewrites a coupled family, the only REACHABLE step may be FEWER
    self-failures, and an absolute gate discards exactly the improvements
    the iter-level ratchet exists to bank. A test whose baseline process ran to its own verdict and
    whose process the attempt killed without a panic is `aborted`, and
    refuses (`TestOutcomes.refuses`) whatever its prior state: until
    2026-10-11 this read "a test that was failing and now aborts is still
    failing, not newly broken", and run `2157238-210637e6` kept five
    self-recursive `construct_from_*` bodies on it, after which 26 binaries
    died of stack exhaustion every sweep.

    ``outcomes`` is the attempt in the one outcome model every keep reads,
    judged per (binary, test) (`assertion_position.outcomes_of`): until
    2026-10-15 this class built "newly red" itself as the self-failures whose
    NAME any baseline binary passed, and run `14495-a26ad4ea` reverted a
    `pass 120 -> 127`, no-regression `src/error.rs` rewrite three times
    because `tests/test_repr.rs::test_drop` passed while
    `tests/test_downcast.rs::test_drop` was red before and after.

    ``baseline_failing`` — the attributed names with a test not observed
    passing (and not cfg-ignored) at the in-lock baseline, a name the
    baseline never reported included (its binary crashed: certainly not
    passing). ``resolved`` — the names with a test that turned green in the
    fill's own scoped run: banking demands POSITIVE evidence (a test absent
    from both readings is unmeasured, not fixed — counting absence as
    progress fabricated improvement). Banks iff something resolved and the
    outcome does not refuse."""

    baseline_failing: frozenset[str]
    outcomes: TestOutcomes

    @property
    def resolved(self) -> frozenset[str]:
        return self.outcomes.gained

    @property
    def new_failures(self) -> frozenset[str]:
        """Names with a test that passed at baseline and fails now."""
        return self.outcomes.lost

    @property
    def regressed_sites(self) -> tuple[PositionMove, ...]:
        """Attributed tests red before and after that stopped at an earlier
        line of their own body (`assertion_position.compare_positions`)."""
        return self.outcomes.earlier

    @property
    def advanced_sites(self) -> tuple[PositionMove, ...]:
        return self.outcomes.later

    @property
    def aborted(self) -> frozenset[str]:
        return self.outcomes.aborted

    @property
    def unresolved(self) -> frozenset[str]:
        return self.baseline_failing - self.resolved

    @property
    def banks(self) -> bool:
        """A joint rewrite that shrank its own failing set and is still a
        non-regression; the file and impl routes accept when this is true."""
        return bool(self.resolved) and self.non_regression

    def banks_on(self, suite: SuiteDelta) -> bool:
        """Banks on the write's own suite reading (:class:`assertion_position
        .SuiteDelta`): a strict suite gain and nothing the rule refuses on
        it. The file, impl and pair keeps ask this once their suite
        comparison exists; the aimed-set :attr:`banks` stays the first
        question."""
        return suite.gained and not self.outcomes.refuses_on(suite)

    @property
    def may_bank_on_suite(self) -> bool:
        """Whether a suite reading could still bank this write: the scoped
        run alone refuses nothing a strict suite gain with nothing lost
        would forgive. False keeps a route from measuring the suite for a
        write the scoped run has already refused."""
        return not self.outcomes.refuses_on(SuiteDelta(net=1, lost=False))

    @property
    def non_regression(self) -> bool:
        """The decided rule (`TestOutcomes.refuses`): nothing newly red,
        nothing aborted, and no attributed test stopped at an earlier
        assertion position unless another attributed test went green. The
        one definition every refill keep reads (`judge_refill`); a keep on
        names alone is how run `24951-a72c4642` (2026-09-29) kept a pair
        write that turned `test_altdebug`'s passing first assertion red
        while the test, red before and after, left the name set unchanged."""
        return not self.outcomes.refuses

    @property
    def refused_on_position(self) -> bool:
        """Refused by the earlier-position clause alone: nothing newly red,
        nothing resolved, nothing aborted, and an attributed test stopped
        earlier."""
        return (not self.new_failures and not self.resolved
                and not self.aborted and bool(self.regressed_sites))

    @property
    def refusal(self) -> str:
        """Which clause refuses, with its tests, or "" when none does: the
        log and the verdict row name it, so a refused keep is a lookup."""
        o = self.outcomes
        parts = []
        if o.lost:
            parts.append("newly red: " + ", ".join(sorted(o.lost)))
        if o.aborted and o.abort.refuses(strict_gain=bool(o.gained),
                                         lost=bool(o.lost)):
            parts.append("aborted: " + ", ".join(sorted(o.aborted)))
        if o.earlier and not o.gained:
            parts.append("panics earlier: " + ", ".join(o.earlier_tests))
        return "; ".join(parts)

    @property
    def site_movement(self) -> str:
        """One line for the log, or "" when nothing moved. A sweep that reports
        a byte-identical verdict vector while panic lines moved later is not
        flat, and the vector cannot see it."""
        def _fmt(rows: tuple[PositionMove, ...]) -> str:
            return ", ".join(m.render() for m in rows[:3])
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


def keyed_verdict(verdict: U2Verdict, baseline: KeyedVerdicts) -> KeyedVerdicts:
    """The scoped run behind `verdict`, per (binary, test).

    Read off the verdict's own capture, so a name two binaries hold is two
    tests. A failing test counts only when its name is one of the verdict's
    self-failures (the gate's blame already excluded a failure inside
    another unit's stub). A name the gate says lost its verdict
    (`verdict.aborted`) and that the capture does not report is a death of
    the baseline's tests of that name that the capture does not hold.

    A verdict with no parsed capture (one built without a run) is read
    against the baseline's keys: a self-failure is every baseline test of
    that name, and a pass counts only where the baseline holds that name
    once — it never invents a gain and never hides a loss."""
    reading = TestStateReading(parse_binary_results(verdict.capture or ""))
    own_failed = {leaf(n) for n in verdict.self_failures}
    gone = {leaf(n) for n in verdict.aborted}
    if reading.binaries:
        k = reading.keyed()
        reported = k.keys
        silent = set(k.aborted_silent) | {
            b for b in baseline.keys if b.test in gone and b not in reported}
        return KeyedVerdicts(
            passed=k.passed,
            failed=frozenset(x for x in k.failed if x.test in own_failed),
            ignored=k.ignored, aborted=k.aborted,
            aborted_silent=frozenset(silent),
            positions=dict(verdict.positions))

    def _of(name: str) -> set[TestKey]:
        return {b for b in baseline.keys if b.test == name}

    failed: set[TestKey] = set()
    for n in own_failed:
        failed |= _of(n) or {TestKey("", n)}
    passed: set[TestKey] = set()
    for n in {leaf(x) for x in verdict.self_passed}:
        held = _of(n)
        if len(held) <= 1:
            passed |= held or {TestKey("", n)}
    silent_k: set[TestKey] = set()
    for n in gone:
        silent_k |= _of(n)
    return KeyedVerdicts(passed=frozenset(passed), failed=frozenset(failed),
                         aborted_silent=frozenset(silent_k),
                         positions=dict(verdict.positions))


def judge_refill(
    attributed: Iterable[str],
    baseline: TestStateReading,
    verdict: U2Verdict,
) -> JointImprovement:
    """How a refill attempt's scoped run compares to the in-lock baseline —
    the ONE judgment every keep site (file, impl, pair, per-fn) reads, via
    `.non_regression` and `.banks`.

    It takes the baseline READING rather than its parts so that no caller
    can judge without the positions or the binaries: both readings go to
    `outcomes_of` per (binary, test), never as name sets. Before this
    function the four keep sites each built "non-regression" from their own
    flags, and three of them (impl, pair, per-fn) omitted the site clause
    the file route had carried since 2026-08-20."""
    names = frozenset(leaf(t) for t in attributed)
    before = baseline.keyed()
    after = keyed_verdict(verdict, before)
    # A self-failure judges the attempt whether or not it is attributed: a
    # test the scoped run named red that passed at baseline is lost. A
    # position is evidence only for a test the unit answers for: one that
    # moved in another test is somebody else's measurement.
    judged = names | {leaf(t) for t in verdict.self_failures | verdict.aborted}

    def _own(pos: Mapping[TestKey, int]) -> dict[TestKey, int]:
        return {k: v for k, v in pos.items() if k.test in names}

    outcomes = outcomes_of(
        judged, before=replace(before, positions=_own(before.positions)),
        after=replace(after, positions=_own(after.positions)),
        crash_sites=verdict.crash_sites)
    clear = before.passed | before.ignored
    failing = frozenset(
        n for n in names
        if not (held := {k for k in before.keys if k.test == n})
        or not held <= clear)
    return JointImprovement(baseline_failing=failing, outcomes=outcomes)


def per_fn_reject_kept(*, crash_graded: bool, aborted: bool,
                       judged: JointImprovement | None) -> bool:
    """Whether the per-fn route keeps an attempt its keep gate rejected.

    Two shapes only. An attempt dispatched on localized crash evidence is
    kept when nothing regressed (`non_regression`): a correct fix of a
    latent fault moves no test at baseline. The keep site still asks
    `SameCrashJudge` before it keeps; this only says which rejects reach
    that question. An attempt whose aimed test died without a panic is kept
    when it banks (something attributed turned green, nothing refuses).

    A plain reject is not kept, even when it banks. `banks` is also every
    multi-test unit whose body turned one attributed test green and fails
    another on an assertion inside it, and keeping that ends the unit's
    in-fill attempts that would carry the assertion and its observed value,
    while the exhaustion salvage (`CompilingSalvage`, fewer failing tests
    wins) already keeps the best compiling attempt. Measured 2026-11-06 on
    one `unittests src/lib.rs` capture, held `{a_case, b_case}`, both red
    before, `a_case ok` and `b_case` failing `left: 7` after: the gate
    rejects on `b_case`, `banks` is True, and a rule keyed on `banks` alone
    kept it.

    Left open: a held leaf matches every binary's copy of a test, so a copy
    in another binary that was red before and after convicts a per-fn body
    that turned its own copy green. Telling that copy from the unit's own
    needs the binary in the ground's test identity, not a keep rule."""
    if judged is None:
        return False
    return ((crash_graded and judged.non_regression)
            or (aborted and judged.banks))


def turned_green(
    baseline: TestStateReading | None, verdict: U2Verdict | None,
) -> frozenset[str]:
    """The tests a keep's scoped run saw passing that the in-lock baseline
    did not (neither passing nor ignored): what the keep turned green. A test
    green before the keep is observed passing after any body, so it is not
    in this set. Either side missing is no observation: empty.

    `self_passed` holds the gate's ground names, the baseline what libtest
    printed; they meet through `HeldTests`."""
    if baseline is None or verdict is None:
        return frozenset()
    return HeldTests(verdict.self_passed).missing_from(
        baseline.passed_names | baseline.ignored_names)


def reading_of_capture(
    out: str, *, sources: TestSource, timed_out: bool = False,
) -> TestStateReading:
    """A :class:`TestStateReading` of one raw `cargo test` capture, with the
    per-binary verdicts AND the assertion position of every failure that has
    one. The constructor every regression baseline goes through, so the
    positions a keep compares against come from the same capture as the
    names. `sources` reads the oracle test files; it is required so that no
    reading can be built with positions nobody placed."""
    return TestStateReading(parse_binary_results(out or "", timed_out=timed_out),
                            positions=assertion_positions(out or "", sources),
                            fingerprints=failure_fingerprints(out or ""))


def failure_fingerprints(capture: str) -> dict[TestKey, str]:
    """Per (binary, test) of `capture`, the identity of that failure's own
    text: the panic line and what follows it (`_evidence_of`), with
    libtest's per-run noise masked (`candidate_tiers.ask_fingerprint`).

    Keyed per binary section, the way `TestStateReading.keyed` keys the
    verdicts, so one name failing in two binaries is two observations. A
    failure with no captured body has no fingerprint. Two readings of one
    test at one assertion with different fingerprints said different
    things (`observation_move`)."""
    from pipeline_core.domain.candidate_tiers import ask_fingerprint

    out: dict[TestKey, str] = {}
    for sec in binary_output_sections(capture or ""):
        for f in parse_test_output(sec.text).failures:
            text = _evidence_of(f.body, 480)
            if text:
                out[TestKey(sec.label, leaf(f.test_name))] = (
                    ask_fingerprint(text))
    return out


def cap_at_line_boundary(text: str, cap: int) -> str:
    """Trim `text` to at most `cap` chars, cutting at a LINE boundary so an
    excerpt never ends mid-diagnostic (`- th…`). Keeps the TAIL (the freshest
    output); if the last line alone exceeds `cap`, hard-slice the tail."""
    if len(text) <= cap:
        return text
    tail = text[-cap:]
    nl = tail.find("\n")
    return tail[nl + 1:] if 0 <= nl < len(tail) - 1 else tail


HEAD_CUT_MARK = " [… cut]"


def head_with_cut_mark(text: str, cap: int) -> str:
    """Trim `text` to at most `cap` chars, keeping the HEAD. A cut inside a
    line keeps that line up to the bound and ends it with `HEAD_CUT_MARK`,
    so the reader sees the line went on; a cut on a line end needs no mark.

    The line that crosses the bound is kept, not dropped back to the last
    line end. In a failure body the panic message is the line after
    `thread '…' panicked at <file>:<line>:<col>:`, and a long one (an
    `assert_eq!` of two Debug values) is the observed value the next writer
    needs: a boundary cut kept the header alone and lost the whole message."""
    if len(text) <= cap:
        return text
    if text[cap] == "\n":
        return text[:cap]
    keep = max(cap - len(HEAD_CUT_MARK), 0)
    return text[:keep] + HEAD_CUT_MARK


@dataclass(frozen=True, slots=True)
class FailureExcerpt:
    """One place the excerpt rule lives: the failing-test NAME list is
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
    compiles: run the tests the write is judged on (`WriteGround.judged`),
    partition blame, revert only on a convicting one, return a typed verdict.
    `unit_sites` is what the unit owns as container-qualified :class:`FnSite`
    identities — used to subtract same-file/same-name SIBLING stub-panics that a
    name-only key would wrongly charge to this unit."""

    def __call__(
        self, ground: WriteGround, unit_sites: frozenset[FnSite],
        test_cmd: str | None, target_src_dir: Path,
        cluster_env: dict[str, str] | None,
    ) -> U2Verdict: ...
