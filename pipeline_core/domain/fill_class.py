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


class FillOutcomeClass(StrEnum):
    """What one dispatched fill (file group / impl block / per-fn unit)
    terminated as."""

    CASHED = "cashed"
    """Attributed tests were observed passing, or a banked joint
    improvement kept the body on that positive evidence. A compiling
    accept with an empty passing set is :attr:`UNREACHABLE_VERDICT` —
    not this."""

    WRONG_BODY = "wrong"
    """The right unit, a compiling (or peeled) answer that did not cash:
    U2 REJECT revert, POST-FILL MISMATCH, peel that restored the snapshot."""

    MIS_TARGETED = "mis"
    """The record already said this unit cannot pay (refill-only, no
    container-qualified convicting test) — GPU skipped, or would have been."""

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


def class_for_kept(*, passed: Collection[str], unreachable: bool) -> FillOutcomeClass:
    """CASHED only when attributed tests were observed passing.

    The three fill routes used to assign CASHED in the else of a U2
    decision chain, ten lines before they computed ``_passed_f`` /
    ``_passed_i`` / ``_u2_passed``. That is the fabricated-win class
    priced at run ``1064759-0f23fb68`` (2026-08-15/16): 50 successes
    against a byte-identical verdict vector. ``unreachable=True`` is
    ACCEPT_EXTERNAL_ONLY, still-stubbed sibling, crash-keep, B3-while-red.
    """
    if unreachable or not passed:
        return FillOutcomeClass.UNREACHABLE_VERDICT
    return FillOutcomeClass.CASHED


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
