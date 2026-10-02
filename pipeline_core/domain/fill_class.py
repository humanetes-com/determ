"""Per-fill outcome class — the four causes the repo conventions name, plus cashed, compile and declined.

AttemptLoss answers a different question (whether the last-resort whole-file
prompt is justified). These seven values classify what a dispatched fill
BOUGHT, so a flat sweep is readable fill-by-fill instead of as a score.

Nothing is gated on the histogram in this code_id. The emitter and the
census detector exist so a later gate has a measured distribution to read
rather than a guessed threshold.
"""
from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
from enum import StrEnum

from pipeline_core.domain.oracle_ledger import bare_test_name


class FillOutcomeClass(StrEnum):
    """What one dispatched fill (file group / impl block / per-fn unit)
    terminated as."""

    CASHED = "cashed"
    """This keep's own reading moved verdicts: a net gain in the suite
    reading the route ledger grades on, or one of the unit's attributed
    tests turned green by it (:func:`keep_reading`). Attributed tests
    merely observed passing are not this: they may have been green before
    the keep."""

    WRONG_BODY = "wrong"
    """The right unit, a compiling (or peeled) answer that did not cash:
    U2 REJECT revert, peel that restored the snapshot. A keep whose
    reading moved verdicts is :attr:`CASHED`; a still-stubbed member
    after accept is :attr:`ANSWER_DROPPED` at the splice, not this."""

    MIS_TARGETED = "mis"
    """The record already said this unit cannot pay (refill-only, no
    container-qualified convicting test) — GPU skipped, or would have been.
    Also a keep whose attributed tests all ran green and none moved: no
    red test was aimed when the body was judged."""

    UNREACHABLE_VERDICT = "unreachable"
    """Body kept, attributed tests cannot run (ACCEPT_EXTERNAL_ONLY /
    still-stubbed sibling / B3 keep while the crate is red / compiling
    accept with no test observed passing)."""

    ANSWER_DROPPED = "dropped"
    """The model answered and a route/parser/splicer lost it
    (AttemptLoss routing_drop / splice_refusal)."""

    COMPILE_FAIL = "compile"
    """The fill stopped on a red crate — no U2 verdict, body reverted or
    the loop exhausted at the compile tier."""

    DECLINED = "declined"
    """The model answered the unit with an EMPTY body — a `<FN>` tag with
    nothing in it. Not a reader loss (the reader had nothing to lose) and
    not a wrong body (nothing landed): the prompt did not give the model
    what it needed to write this unit, or the model decided not to. Run
    `959654-1e96b403` (2026-09-06): three vtable drop helpers declined in
    one file-first reply, logged as a parser defect for eight hours."""


_FIELD = {
    FillOutcomeClass.CASHED: "cashed",
    FillOutcomeClass.WRONG_BODY: "wrong",
    FillOutcomeClass.MIS_TARGETED: "mis",
    FillOutcomeClass.UNREACHABLE_VERDICT: "unreachable",
    FillOutcomeClass.ANSWER_DROPPED: "dropped",
    FillOutcomeClass.COMPILE_FAIL: "compile",
    FillOutcomeClass.DECLINED: "declined",
}


@dataclass(frozen=True, slots=True)
class CouldNotRun:
    """Why the attributed tests could not run, or nothing when they ran.

    A TYPE, not a bool, because `class_for_kept` reads this before it reads
    the measurement, so anything truthy passed in outranks what the sweep
    observed. While it was a bool, a site could hand it any expression: the
    file and pair sites folded a non-regression keep into it (run
    `3400-4d0618ba`, 2026-09-21, four keeps graded unreachable beside a GUARD
    line saying the tests ran), and the check that shipped against that was
    an AST walk asserting today's sites spell `tests_could_not_run(...)` —
    which a new site, or an old one edited back, simply does not appear in.
    The type is the check: `class_for_kept` refuses anything else, so a
    fabricated reason cannot be constructed, only a real one."""

    crash_keep: bool = False
    external_only: bool = False
    blocked_sibling: bool = False
    crate_red: bool = False

    def __bool__(self) -> bool:
        return (self.crash_keep or self.external_only
                or self.blocked_sibling or self.crate_red)

    @property
    def reason(self) -> str:
        """The reasons that hold, for a log line; `""` when the tests ran."""
        return ", ".join(
            name for name in ("crash_keep", "external_only",
                              "blocked_sibling", "crate_red")
            if getattr(self, name))


def tests_could_not_run(
    *, crash_keep: bool = False, external_only: bool = False,
    blocked_sibling: bool = False, crate_red: bool = False,
) -> CouldNotRun:
    """The ``unreachable=`` every keep site passes to :func:`class_for_kept`.

    The signature is the docstring: the attributed tests could not run
    because the keep was on crash evidence, U2 found every failing test
    blocked outside the unit (ACCEPT_EXTERNAL_ONLY), a still-stubbed
    sibling blocks them, or the crate was red so no test ran. Nothing else.

    A non-regression keep is deliberately not a parameter. Its GUARD line
    says the attributed tests RAN and were already failing at baseline, so
    the body is WRONG, not unmeasured. Run `3400-4d0618ba` (2026-09-21):
    the file and pair sites folded the non-regression flag into
    ``unreachable=``, and all four file keeps of that run were graded
    `unreachable` beside a GUARD line saying the tests ran. Every consumer
    calls this function, so no site can pass the non-regression flag in.
    """
    return CouldNotRun(crash_keep=crash_keep, external_only=external_only,
                       blocked_sibling=blocked_sibling, crate_red=crate_red)


@dataclass(frozen=True, slots=True)
class KeepReading:
    """What ONE keep moved: the suite reading's net gain at the keep, and
    the unit's own attributed tests that keep turned green. Built only by
    :func:`keep_reading`; truthy when the keep moved a verdict."""

    net_delta: int = 0
    newly_passing: frozenset[str] = frozenset()

    def __bool__(self) -> bool:
        return self.net_delta > 0 or bool(self.newly_passing)


def keep_reading(
    *, net_delta: int = 0, newly_green: Collection[str] = (),
    attributed: Collection[str] = (),
) -> KeepReading:
    """The ``reading=`` every keep site passes to :func:`class_for_kept`.

    ``net_delta`` and ``newly_green`` are the comparison the keep site
    measured (`fill_reading.last_comparison` at the keep, a session's final
    outcome): the reading the route row is graded on. ``net_delta`` is the
    suite reading, so a joint keep with an empty attributed set and a moved
    reading still cashes (run `3014175-d98ed09e`, 2026-09-08). A newly
    green test counts only inside ``attributed``: a sibling's test going
    green in the same comparison is not this unit's payment.

    A site with no comparison passes nothing and gets a reading that moved
    nothing. There is no argument for "tests observed passing": a test that
    was green before the keep is observed passing after it too."""
    own = {bare_test_name(t) for t in attributed}
    return KeepReading(
        net_delta=int(net_delta),
        newly_passing=frozenset(
            n for n in (bare_test_name(t) for t in newly_green) if n in own))


def class_for_kept(
    *, reading: KeepReading, passed: Collection[str],
    unreachable: CouldNotRun,
    attributed: Collection[str] | None = None,
    measured: bool | None = None,
) -> FillOutcomeClass:
    """CASHED when THIS keep's reading moved a verdict; else UNREACHABLE,
    WRONG_BODY or MIS_TARGETED by what its attributed tests did.

    The three fill routes used to assign CASHED in the else of a U2
    decision chain, ten lines before they computed ``_passed_f`` /
    ``_passed_i`` / ``_u2_passed``. That is the fabricated-win class
    priced at run ``1064759-0f23fb68`` (2026-08-15/16): 50 successes
    against a byte-identical verdict vector. ``unreachable`` is always
    :func:`tests_could_not_run` (ACCEPT_EXTERNAL_ONLY, still-stubbed
    sibling, crash-keep, B3-while-red), never a site's own flag.

    ``reading`` is always :func:`keep_reading`, and it is the ONLY source
    of CASHED. ``passed`` — the unit's attributed tests observed passing
    after the splice — used to be a second one, and it cannot tell a test
    this keep turned green from one that was green before the call
    returned. Run `3040-51eab48c` (2026-10-02, fill 5): one file keep
    turned the last red test green (`pass 183 -> 184`), and the four keeps
    after it in the same fill — one of them 19 bodies with no byte changed
    on disk — were each graded `cashed` beside a `pass 184 -> 184 (net
    +0)` reading, because that test was attributed to them and passing.

    A keep whose attributed tests RAN and still fail is WRONG_BODY, not
    UNREACHABLE. Run `3500465-67c40e78` (2026-09-08) labelled twelve
    such per-fn keeps unreachable because ``not passed`` collapsed "none
    passed" with "could not run". Callers that pass ``attributed`` and
    ``measured`` split those.

    A keep whose attributed tests ran, all pass, and none moved is
    MIS_TARGETED: nothing red was aimed when the body was judged, so no
    body written there could have moved a verdict.
    """
    if not isinstance(unreachable, CouldNotRun):
        # The one state this grading must not be able to reach: a site
        # asserting "the tests could not run" from a flag of its own.
        raise TypeError(
            "class_for_kept(unreachable=) takes tests_could_not_run(...), "
            f"not {type(unreachable).__name__} — a keep whose attributed "
            "tests RAN is WRONG_BODY, whatever the site believes")
    if not isinstance(reading, KeepReading):
        raise TypeError(
            "class_for_kept(reading=) takes keep_reading(...), not "
            f"{type(reading).__name__} — CASHED is the keep's own "
            "comparison, never a set of tests observed passing")
    if reading:
        return FillOutcomeClass.CASHED
    if unreachable:
        return FillOutcomeClass.UNREACHABLE_VERDICT
    green = {bare_test_name(t) for t in passed}
    if attributed is not None and measured is not None:
        if not attributed or not measured:
            return FillOutcomeClass.UNREACHABLE_VERDICT
        if {bare_test_name(t) for t in attributed} - green:
            return FillOutcomeClass.WRONG_BODY
        return FillOutcomeClass.MIS_TARGETED
    if not green:
        return FillOutcomeClass.UNREACHABLE_VERDICT
    return FillOutcomeClass.MIS_TARGETED


@dataclass(frozen=True, slots=True)
class FillClassHistogram:
    """Counts of :class:`FillOutcomeClass` for one fill pass.

    ``rendered`` is the token the TDD iter line and the fill-returned
    line print, so a fold and a log grep see the same six integers.
    """

    cashed: int = 0
    wrong: int = 0
    mis: int = 0
    unreachable: int = 0
    dropped: int = 0
    compile: int = 0
    declined: int = 0

    def rendered(self) -> str:
        return (
            f"classes: cashed={self.cashed} wrong={self.wrong} "
            f"mis={self.mis} unreachable={self.unreachable} "
            f"dropped={self.dropped} compile={self.compile} "
            f"declined={self.declined}"
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "cashed": self.cashed, "wrong": self.wrong, "mis": self.mis,
            "unreachable": self.unreachable, "dropped": self.dropped,
            "compile": self.compile, "declined": self.declined,
        }

    @property
    def total(self) -> int:
        return (self.cashed + self.wrong + self.mis
                + self.unreachable + self.dropped + self.compile
                + self.declined)

    def __bool__(self) -> bool:
        return self.total > 0

    def combined_with(self, other: FillClassHistogram | None) -> FillClassHistogram:
        if other is None:
            return self
        return FillClassHistogram(
            cashed=self.cashed + other.cashed,
            wrong=self.wrong + other.wrong,
            mis=self.mis + other.mis,
            unreachable=self.unreachable + other.unreachable,
            dropped=self.dropped + other.dropped,
            compile=self.compile + other.compile,
            declined=self.declined + other.declined,
        )


@dataclass(slots=True)
class FillClassCounter:
    """Mutable accumulator; freeze with :meth:`histogram` at the pass end."""

    cashed: int = 0
    wrong: int = 0
    mis: int = 0
    unreachable: int = 0
    dropped: int = 0
    compile: int = 0
    declined: int = 0

    def add(self, cls: FillOutcomeClass) -> None:
        name = _FIELD[cls]
        setattr(self, name, int(getattr(self, name)) + 1)

    def histogram(self) -> FillClassHistogram:
        return FillClassHistogram(
            cashed=self.cashed, wrong=self.wrong, mis=self.mis,
            unreachable=self.unreachable, dropped=self.dropped,
            compile=self.compile, declined=self.declined,
        )


def note_fill_class(
    emit: Callable[..., None],
    counter: FillClassCounter,
    cls: FillOutcomeClass,
    *,
    cluster: str,
    unit: str,
    route: str,
) -> None:
    """Record one terminal fill outcome. Event per fill; counts ride the
    histogram that the next iter line prints."""
    counter.add(cls)
    emit("phase_c.fill.class", data={
        "cluster": cluster, "unit": unit, "route": route,
        "class": cls.value,
    })
