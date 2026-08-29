"""Per-fill outcome class — CLAUDE.md's four causes, plus cashed/compile.

AttemptLoss answers a different question (whether the last-resort whole-file
prompt is justified). These six values classify what a dispatched fill
BOUGHT, so a flat sweep is readable fill-by-fill instead of as a score.

Nothing is gated on the histogram in this code_id. The emitter and the
census detector exist so a later gate has a measured distribution to read
rather than a guessed threshold.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class FillOutcomeClass(StrEnum):
    """What one dispatched fill (file group / impl block / per-fn unit)
    terminated as."""

    CASHED = "cashed"
    """U2 ACCEPT — attributed tests were observed passing, or a banked
    joint improvement kept the body on positive evidence."""

    WRONG_BODY = "wrong"
    """The right unit, a compiling (or peeled) answer that did not cash:
    U2 REJECT revert, POST-FILL MISMATCH, peel that restored the snapshot."""

    MIS_TARGETED = "mis"
    """The record already said this unit cannot pay (refill-only, no
    container-qualified convicting test) — GPU skipped, or would have been."""

    UNREACHABLE_VERDICT = "unreachable"
    """Body kept, attributed tests cannot run (ACCEPT_EXTERNAL_ONLY /
    still-stubbed sibling)."""

    ANSWER_DROPPED = "dropped"
    """The model answered and a route/parser/splicer lost it
    (AttemptLoss routing_drop / splice_refusal)."""

    COMPILE_FAIL = "compile"
    """The fill stopped on a red crate — no U2 verdict, body reverted or
    the loop exhausted at the compile tier."""


_FIELD = {
    FillOutcomeClass.CASHED: "cashed",
    FillOutcomeClass.WRONG_BODY: "wrong",
    FillOutcomeClass.MIS_TARGETED: "mis",
    FillOutcomeClass.UNREACHABLE_VERDICT: "unreachable",
    FillOutcomeClass.ANSWER_DROPPED: "dropped",
    FillOutcomeClass.COMPILE_FAIL: "compile",
}


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

    def rendered(self) -> str:
        return (
            f"classes: cashed={self.cashed} wrong={self.wrong} "
            f"mis={self.mis} unreachable={self.unreachable} "
            f"dropped={self.dropped} compile={self.compile}"
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "cashed": self.cashed, "wrong": self.wrong, "mis": self.mis,
            "unreachable": self.unreachable, "dropped": self.dropped,
            "compile": self.compile,
        }

    @property
    def total(self) -> int:
        return (self.cashed + self.wrong + self.mis
                + self.unreachable + self.dropped + self.compile)

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

    def add(self, cls: FillOutcomeClass) -> None:
        name = _FIELD[cls]
        setattr(self, name, int(getattr(self, name)) + 1)

    def histogram(self) -> FillClassHistogram:
        return FillClassHistogram(
            cashed=self.cashed, wrong=self.wrong, mis=self.mis,
            unreachable=self.unreachable, dropped=self.dropped,
            compile=self.compile,
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
