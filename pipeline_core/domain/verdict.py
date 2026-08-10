"""The typed verdict every judging gate answers with, and the rule that
decides when a NEGATIVE one may be charged.

The inversion this module carries: a negative judgment must present
IMPLICATING evidence, not merely the absence of improvement. Measured across
the 14 days of retained logs (2026-07-27 → 2026-08-10): 251 + 215
reason-carrying unit reverts, of which 30–45 % carried a crash-contaminated
or sibling-caused reason; 268 of 268 per-fn "kept" lines in the first era
were `blocked by still-stubbed sibling(s)` — no per-fn fill was ever judged
on its own tests; and the keep-side guards shipped against individual
signatures (stub-marker panic, ASAN-localized blame, crash-dispatch
evidence) fired zero times on the same corpus, because each next instance
arrived wearing a different signature. The exoneration list is enumerable;
the class is not. So the default flips: the gate that cannot show its
evidence does not judge.

`grade_negative_evidence` is the whole rule. NEGATIVE requires a newly-red
verdict from a test attributed to the unit, observed in a COMPLETED binary,
where that test is not blocked by a producer outside the unit's reach and
any localized blame does not lie wholly outside the unit. Everything short
of that is UNMEASURABLE — keep on non-regression, defer judgment to the
next sweep in the joint tree, charge no budget, write no retrospective. The
outer machinery (batch ratchet, re-sweep confirmation, durable best tree)
remains the executioner for a wrongly KEPT body; a wrongly DESTROYED one
costs a re-climb measured at ~2.5 GPU-hours per draw on the 2026-08-09
`pass=13` orbit, and nothing recovers it.

`VerdictRecord` is the ledger row: one JSONL line per judgment, whatever
the disposition, so the audit that produced the numbers above becomes a
query instead of a session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class GateOutcome(StrEnum):
    """The three answers a judging gate can give about one attempt."""

    POSITIVE = "positive"
    """The unit's own evidence improved — accepted on its merits."""

    NEGATIVE = "negative"
    """Implicating evidence exists: an attributed test went newly red in a
    completed binary, unblocked, with no localized blame lying wholly
    outside the unit. The only outcome that may destroy work or charge a
    budget."""

    UNMEASURABLE = "unmeasurable"
    """No implicating evidence — the binary died, the failures are a
    sibling's, the test never ran, or the blame sits outside the unit.
    Keep on non-regression, defer to the joint tree, charge nothing."""


class Disposition(StrEnum):
    """What the caller actually did with the attempt — the ledger's verb.

    Wider than :class:`GateOutcome` because the machinery has more ways to
    end an attempt than a gate has answers: a cap refusal or an exhaustion
    marking is a judgment about budgets, not about the body."""

    ACCEPT = "accept"
    KEEP_DEFERRED = "keep_deferred"
    REJECT = "reject"
    REVERT = "revert"
    SALVAGE_REFUSAL = "salvage_refusal"
    CONVICTION = "conviction"
    EXHAUSTION = "exhaustion"
    CAP_REFUSAL = "cap_refusal"


@dataclass(frozen=True, slots=True)
class NegativeEvidence:
    """What a gate knows when it is about to judge an attempt negatively.

    `own_newly_red` — attributed tests of THIS unit that were passing at
    baseline and fail now. Still-red-at-baseline failures are deliberately
    not here: they implicate the baseline, not the attempt.
    `binary_completed` — the judging run produced a verdict for the unit's
    tests (nothing attributed to the unit vanished with a crashed binary).
    `blocked_tests` — charged tests whose attribution crosses a producer
    the unit cannot reach (still-stubbed, or a fn no route attempts); their
    red is the producer's, on the same evidence the dispatch partition
    already uses to defer consumers.
    `blame_wholly_outside` — when a crash localizer blamed anything, whether
    every blamed locator lies outside the unit; ``None`` when no
    localization exists."""

    own_newly_red: frozenset[str] = frozenset()
    binary_completed: bool = True
    blocked_tests: frozenset[str] = frozenset()
    blame_wholly_outside: bool | None = None


def grade_negative_evidence(evidence: NegativeEvidence) -> GateOutcome:
    """NEGATIVE iff the evidence implicates the unit; UNMEASURABLE otherwise.

    Never answers POSITIVE: improvement is measured by the accepting branch
    of each gate, before this rule is consulted. This function only decides
    whether the failure the gate is holding is CHARGEABLE."""
    if not evidence.binary_completed:
        return GateOutcome.UNMEASURABLE
    if evidence.blame_wholly_outside is True:
        return GateOutcome.UNMEASURABLE
    if evidence.own_newly_red - evidence.blocked_tests:
        return GateOutcome.NEGATIVE
    return GateOutcome.UNMEASURABLE


@dataclass(frozen=True, slots=True)
class ValidityVector:
    """The judgment's measurement conditions, recorded beside its verdict.

    ``None`` means the dimension was not evaluated at this site — a real
    fact about the site, worth distinguishing from a measured True/False:
    the 2026-08-10 audit's hardest question ("was the binary even complete
    when this unit was convicted?") was unanswerable precisely because no
    site recorded it."""

    binary_completed: bool | None = None
    oracle_intact: bool | None = None
    tests_blocked: bool | None = None
    blame_outside_unit: bool | None = None
    baseline_epoch: int | None = None

    def to_dict(self) -> dict[str, bool | int | None]:
        return {
            "binary_completed": self.binary_completed,
            "oracle_intact": self.oracle_intact,
            "tests_blocked": self.tests_blocked,
            "blame_outside_unit": self.blame_outside_unit,
            "baseline_epoch": self.baseline_epoch,
        }


@dataclass(frozen=True, slots=True)
class VerdictRecord:
    """One ledger row: what was judged, on what evidence, at what cost.

    Total by construction — every field is a JSON-safe primitive or a tuple
    of strings, so :meth:`to_json_dict` cannot fail on any instance. The
    row is append-only evidence, not state: nothing reads it back into a
    decision (cross-run resumption reads it deliberately, through its own
    loader, when that ships)."""

    unit: str
    route: str
    attempt: int
    disposition: Disposition
    reason: str
    outcome: GateOutcome | None = None
    validity: ValidityVector = field(default_factory=ValidityVector)
    evidence_keys: tuple[str, ...] = ()
    model_calls: int = 0
    prompt_chars: int = 0

    def to_json_dict(self) -> dict[str, object]:
        return {
            "unit": self.unit,
            "route": self.route,
            "attempt": self.attempt,
            "disposition": self.disposition.value,
            "outcome": None if self.outcome is None else self.outcome.value,
            "reason": self.reason,
            "validity": self.validity.to_dict(),
            "evidence_keys": list(self.evidence_keys),
            "model_calls": self.model_calls,
            "prompt_chars": self.prompt_chars,
        }
