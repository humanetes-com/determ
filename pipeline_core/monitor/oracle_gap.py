"""What the DECLARED oracle holds that a sweep's `pass + fail` does not.

The score bar is drawn against `oracle_size` (the tests the sweep counted)
while `oracle_static` is what the cluster's test inventory declares. The panel
used to render the difference as "N unreachable (crashed binaries)", which is
wrong twice over — and wrong in the direction that stops an operator looking:

* the tests a crashed binary LOST are not in the gap at all. They are counted
  as FAILING, by `oracle_ledger.reconcile_sweep` + the loop's
  `fail_n += oracle.missing_count`, and they print as
  `ORACLE SHRUNK — counting 13 as failing`;
* what IS in the gap is (a) the verdicts a crashed binary managed to PRINT
  before it aborted, which `pass_count`/`fail_count` cannot see because those
  counters are summed from the libtest `test result:` trailers and a crashed
  binary never prints one, and (b) tests that did not run at all — libtest
  `ignored` (cfg-gated to another cluster) plus any static over-count.

Both are recoverable from the `phase_c.tdd.iter` event alone: `incomplete`
names the binaries that produced no trailer, and `passed_pairs`/`failed_pairs`
are `(binary, test)` from the SAME parse the counters come from, so the
verdicts a crashed binary printed are exactly the pairs whose binary is in
`incomplete`.

Computed on the live records of `logs/events.jsonl`, cluster `stable`
(2026-08-29), every distinct iter reading in the window:

    ts                   static oracle gap  crashed printed never-ran
    2026-08-19T06:36:48     107    106    1       0       0    1
    2026-08-20T19:23:14     107    105    2       0       0    2
    2026-08-21T05:44:51*    113    113    0       0       0    0   (*nightly)
    2026-08-28T07:14:50     139    137    2       0       0    2
    2026-08-28T20:08:20     139    130    9      22       7    2
    2026-08-28T20:49:10     139    127   12       5      10    2

`never-ran` is the falsifiable half: it stays 2 while the crash count moves
0 → 5 → 22 (the two `requires nightly` tests, `test_backtrace` and
`compiletest`, which the nightly cluster runs — hence 0 there), and the whole
swing of the gap is the printed-then-lost verdicts. Under the old label the
2026-08-28T20:49 reading read "12 unreachable (crashed binaries)" when five of
those twelve were tests that PASSED and went uncredited.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

__all__ = ["PAIR_CAP", "OracleGap", "oracle_gap"]

# `phase_c.tdd.iter` truncates its name and pair lists at 200 entries
# (`sorted(...)[:200]` in phase_c_tdd.py). A list AT the cap may be short of
# verdicts, so the split below cannot be trusted and the gap is reported
# whole instead of decomposed into a number that is quietly too small.
PAIR_CAP = 200


@dataclass(frozen=True, slots=True)
class OracleGap:
    """The declared-minus-counted gap of one sweep, split by cause.

    `total` is `oracle_static - oracle_size`. `lost_with_crash` are verdicts
    printed by binaries that never reached a trailer (`crashed_binaries` of
    them); `never_ran` is the remainder — cfg-ignored tests and any static
    over-count. `decomposed` is False when the event's pair lists were
    truncated, in which case only `total` is meaningful.
    """

    total: int
    lost_with_crash: int
    crashed_binaries: int
    never_ran: int
    decomposed: bool

    @property
    def rendered(self) -> str:
        """One panel segment. Never says "unreachable": the word invited the
        reading that these tests are lost to the crash, when the crash's own
        casualties are inside the failing count and half of THIS number is
        verdicts that were observed and dropped."""
        if not self.decomposed:
            return f"{self.total} outside the count"
        bins = (f"{self.crashed_binaries} aborted "
                f"{'binary' if self.crashed_binaries == 1 else 'binaries'}")
        if self.lost_with_crash and self.never_ran:
            return (f"{self.total} outside the count "
                    f"({self.lost_with_crash} lost with {bins}, "
                    f"{self.never_ran} never run)")
        if self.lost_with_crash:
            return (f"{self.total} outside the count "
                    f"(lost with {bins})")
        return f"{self.total} outside the count (never run)"


def _pairs_of(raw: Any) -> list[Sequence[Any]]:
    """The `[binary, test]` pairs of an iter-event field, ignoring any entry
    that is not a 2-sequence (an older event shape, or a truncated write)."""
    if not isinstance(raw, list):
        return []
    return [p for p in raw
            if isinstance(p, (list, tuple)) and len(p) >= 1]


def oracle_gap(state: dict[str, Any]) -> OracleGap | None:
    """Split this sweep's declared-minus-counted gap, or None when there is
    none (the sweep counted everything the inventory declares).

    Reads the monitor state keys the `phase_c.tdd.iter` fold carries:
    `tdd_oracle`, `tdd_oracle_static`, `tdd_incomplete_binaries`,
    `tdd_passed_pairs`, `tdd_failed_pairs`.
    """
    oracle = state.get("tdd_oracle")
    static = state.get("tdd_oracle_static")
    if not isinstance(oracle, int) or not isinstance(static, int):
        return None
    total = static - oracle
    if total <= 0:
        return None
    incomplete = {str(b) for b in (state.get("tdd_incomplete_binaries") or [])}
    passed = _pairs_of(state.get("tdd_passed_pairs"))
    failed = _pairs_of(state.get("tdd_failed_pairs"))
    # No pair vector at all (an older run, or an event window that has not
    # carried one yet) is not the same as "no crashed verdicts" — say so by
    # declining to decompose rather than reporting 0 lost.
    if not passed and not failed:
        return OracleGap(total, 0, len(incomplete), 0, decomposed=False)
    if len(passed) >= PAIR_CAP or len(failed) >= PAIR_CAP:
        return OracleGap(total, 0, len(incomplete), 0, decomposed=False)
    lost = sum(1 for p in passed + failed if str(p[0]) in incomplete)
    # Clamp rather than trust the subtraction: the two numbers come from one
    # event, but a future emitter change could make them disagree, and a
    # negative "never run" on the panel would be a puzzle with no answer.
    lost = min(lost, total)
    return OracleGap(total, lost, len(incomplete), total - lost,
                     decomposed=True)
