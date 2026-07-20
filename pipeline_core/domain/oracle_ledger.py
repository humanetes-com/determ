"""Sweep-level test-oracle reconciliation — pure domain.

A cargo test sweep only reports tests whose binary built AND ran to the
summary line; a binary that fails to compile or crashes (segfault, abort)
takes its whole test set out of the output silently. Comparing pass/fail
counts across sweeps with a shrinking denominator understates failures and
can manufacture a fraudulent "all tests pass" (FR-018/FR-022) — the same
fraud class FR-024 guards at TASK 3, applied here to every TDD iteration.

`reconcile_sweep` folds the statically-expected oracle (cfg-aware test-fn
inventory) into one sweep's observed results: expected tests that reported no
verdict are VANISHED and must be accounted as failing; a reported total below
the expected count is a DEFICIT that blocks "cluster done".

Names compare on their last `::` segment: cargo may print `module::test_x`
while the static inventory carries the bare fn name.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


def bare_test_name(name: str) -> str:
    return name.rsplit("::", 1)[-1]


@dataclass(frozen=True, slots=True)
class SweepOracle:
    """One sweep's oracle verdict. `vanished` holds expected test names (bare
    form) that reported no verdict; `deficit` is how many expected tests are
    missing from the reported total (covers tests the static NAME inventory
    could not enumerate). `intact` gates ratchet snapshots / "cluster done"."""
    vanished: frozenset[str]
    deficit: int

    @property
    def intact(self) -> bool:
        return not self.vanished and self.deficit <= 0

    @property
    def missing_count(self) -> int:
        """How many expected tests reported no verdict. `vanished` and
        `deficit` overlap (a vanished name IS part of the count deficit), so
        the honest fail-count adjustment is the max, never the sum."""
        return max(len(self.vanished), self.deficit)

    @property
    def unnamed_shortfall(self) -> int:
        """Missing tests that no expected NAME accounts for.

        A shrink has two causes with two different remedies, and only one of
        them is a crash. Named `vanished` tests were enumerated and then
        reported nothing → their binary died, so a stack is worth hunting. An
        unnamed shortfall means every expected name DID report and the total is
        still short → the static inventory under-enumerated, and no debugger
        will ever find a fault for it. Folding both into one count sends the
        crash localizer (gdb + an ASAN rebuild) after a fault that cannot
        exist, every iteration, and logs it as tests vanishing when none did."""
        return max(0, self.deficit - len(self.vanished))


def reconcile_sweep(
    expected_count: int,
    expected_names: frozenset[str],
    passed: Iterable[str],
    failed: Iterable[str],
    ignored_count: int = 0,
    *,
    ignored_names: Iterable[str] = (),
    executed_count: int | None = None,
) -> SweepOracle:
    """Fold one sweep's observations into the expected oracle.

    `ignored_names` are tests libtest reported as `ignored` — cfg-gated off for
    THIS cluster (`ignored, requires nightly`). They report no pass/fail
    verdict, so without them a cluster-ignored test looks identical to a test
    whose binary crashed: it gets counted as vanished and charged as a failure,
    which manufactures a deterministic-crash signal out of a test that was
    never meant to run here (Hard Rule 8 — an ignored test belongs to the
    cluster whose cfg enables it).

    `executed_count` is the AUTHORITATIVE reported total, summed from the
    libtest `test result:` trailers. It must be preferred over `len(observed)`:
    `observed` is a SET of BARE names, so two binaries that each define a test
    with the same name collapse to one entry and every duplicate silently
    inflates the deficit by one. Comparing a deduped set size against an
    un-deduped `expected_count` is a category error; the fallback only stands
    in when trailer counts are unavailable.
    """
    observed = {bare_test_name(n) for n in passed}
    observed |= {bare_test_name(n) for n in failed}
    skip = {bare_test_name(n) for n in ignored_names}
    vanished = frozenset(
        n for n in (bare_test_name(e) for e in expected_names)
        if n not in observed and n not in skip)
    executed = executed_count if executed_count is not None else len(observed)
    reported = executed + ignored_count
    return SweepOracle(
        vanished=vanished,
        deficit=max(0, expected_count - reported),
    )
