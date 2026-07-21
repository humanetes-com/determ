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

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Protocol

from pipeline_core.domain.models import FnSite
from pipeline_core.domain.rust_idents import canon_container
from pipeline_core.domain.test_results import (
    parse_failed_tests,
    split_test_stdout_sections,
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


def _parse_panic_location(body: str) -> tuple[str | None, int | None, int | None, str]:
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
    """Names of tests libtest reported as ``... ok`` (regex-free; mirrors the
    ``test NAME ... FAILED`` scan in :func:`parse_failed_tests`)."""
    passed: set[str] = set()
    for ln in output.splitlines():
        toks = ln.split()
        if (ln[:4] == "test" and len(toks) >= 4
                and toks[0] == "test" and toks[2] == "..." and toks[3] == "ok"):
            passed.add(toks[1])
    return passed


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
        pf, pl, pc, msg = _parse_panic_location(body)
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
    external: tuple[tuple[TestFailure, FnSite], ...] = ()
    detail: str = ""
    no_verdict: frozenset[str] = frozenset()

    @classmethod
    def accept(cls) -> U2Verdict:
        return cls(U2Decision.ACCEPT)

    @classmethod
    def accept_external_only(
        cls, external: tuple[tuple[TestFailure, FnSite], ...],
        no_verdict: frozenset[str] = frozenset(),
    ) -> U2Verdict:
        return cls(U2Decision.ACCEPT_EXTERNAL_ONLY, external=external,
                   no_verdict=no_verdict)

    @classmethod
    def reject(cls, self_failures: frozenset[str], detail: str,
               no_verdict: frozenset[str] = frozenset()) -> U2Verdict:
        return cls(U2Decision.REJECT, self_failures=self_failures,
                   detail=detail, no_verdict=no_verdict)

    @property
    def blocking_fns(self) -> frozenset[FnSite]:
        return frozenset(loc for _f, loc in self.external)


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
