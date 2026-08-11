"""The keep/refuse rule for a repair write.

The fix loop's whole-file rewrite path had no such rule, and the cost is
measured. Run `3274139-76ff8d08` opened at 21:25:50Z on 2026-08-10 with
`Build check PASSED` and a fix-loop overview of `12/12 clean` /
`fix attempt 0 — 197/227 tests passing`. Its next overview, 21:52:05Z, reads
9/12 clean and 15 diagnostics, and the merged tree did not compile again for
13h50m across that run and its successor — no scored sweep, no verdict row.
The diagnostic totals over those two runs, in order: 0, 15, 15, 14, 7, 32, 33,
34, 32, 32, 36, 46, 39, 39, 38, 19, 25, 20, 21, 8, 7, 7, 27, 12, 22, 23, 21,
23, 6, 25, 3, 1. Every increase in that series was a KEPT write.

Two decisions, in this order:

- a write that introduces a cross-file duplicate item is refused outright, on
  the evidence rather than on the count: it is unreachable by every repair
  route the loop owns, so keeping it converts a bad attempt into a dead run;
- otherwise a write that RAISES the diagnostic count is refused, and an equal
  count is neutral (kept). Equal-stays-neutral is the same asymmetry the
  cluster-side reading gate uses: a repair often trades one diagnostic for
  another on its way out of a broken state, and refusing that would leave the
  loop no legal move at all.

What refusing costs is one retry. What keeping cost, once, was 13 GPU-hours —
and because a refused write leaves the floor metric where it was, the loop's
existing stall budget now ENDS a pass that has nothing to offer instead of
grinding on it.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pipeline_core.domain.associated_items import DuplicateIntroduction


class WriteOutcome(Enum):
    KEEP = "keep"
    REFUSE_DUPLICATE = "refuse_duplicate"
    REFUSE_DIAG_INCREASE = "refuse_diag_increase"


@dataclass(frozen=True, slots=True)
class WriteVerdict:
    outcome: WriteOutcome
    before: int
    after: int
    duplicates: tuple[DuplicateIntroduction, ...] = ()

    @property
    def kept(self) -> bool:
        return self.outcome is WriteOutcome.KEEP

    def render(self) -> str:
        if self.outcome is WriteOutcome.REFUSE_DUPLICATE:
            what = "; ".join(d.render() for d in self.duplicates)
            return (f"duplicate definition introduced ({what}) — no body "
                    f"rewrite can reach it; restored")
        if self.outcome is WriteOutcome.REFUSE_DIAG_INCREASE:
            return (f"diagnostics {self.before} → {self.after} — a repair that "
                    f"breaks more than it fixes; restored")
        return f"diagnostics {self.before} → {self.after}"


def judge_write(
    *, before: int, after: int,
    duplicates: tuple[DuplicateIntroduction, ...] = (),
) -> WriteVerdict:
    """Keep or refuse, per the module docstring."""
    if duplicates:
        return WriteVerdict(WriteOutcome.REFUSE_DUPLICATE, before, after, duplicates)
    if after > before:
        return WriteVerdict(WriteOutcome.REFUSE_DIAG_INCREASE, before, after)
    return WriteVerdict(WriteOutcome.KEEP, before, after)
