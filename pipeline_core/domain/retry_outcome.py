"""The task3 inline-retry decision logic, as explicit types (P3.D).

The retry ran in one of two implicit modes and ended on a tangle of inline
comparisons deciding whether to keep the edits, revert them, recurse, or
escalate. This module names both:

* :class:`RetryMode` — BUILD (no test reported FAILED because the tree did not
  compile → attribute via cargo diagnostics) vs TEST (≥1 test FAILED → attribute
  via the reverse-map/classifier).
* :class:`RetryVerdict` + the two pure ``*_verdict`` functions — given the
  before/after counts, what to do. Pure: no I/O, so the revert-or-keep policy is
  unit-testable on numbers alone (it was the subtlest part — R9.2: a tree that
  no longer compiles emits zero FAILED lines and must NOT read as "near success").
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# A tree that produces no test verdicts at all is WORSE than any number of
# failing tests, and the improvement loop's metric is lower-is-better. Scoring a
# non-compiling tree as 0 read as the best result possible: on 2026-08-05 a
# 14-body batch broke the build and `loop.iter` recorded
# `metric_prev: 2, metric_new: 0, improved: true, metric_best: 0`, poisoning the
# ratchet with a floor no real tree can beat. The sentinel puts it above every
# real failure count instead.
NO_VERDICTS_METRIC = 1_000_000


def failure_metric(*, ran: bool, failures: int) -> int:
    """The improvement loop's lower-is-better reading of one sweep."""
    return failures if ran else NO_VERDICTS_METRIC


@dataclass(frozen=True, slots=True)
class ClusterReading:
    """One cluster's verdict on one tree state. `ran` is the compile/run state —
    a cluster whose binaries never reported has no meaningful `failures`."""

    cluster: str
    ran: bool
    failures: int


@dataclass(frozen=True, slots=True)
class TreeReading:
    """What EVERY cluster says about one tree state.

    task3 runs the merged tree under each cluster's own toolchain and cfg
    because it is one artifact serving several cfg worlds. A reading taken
    through a single cluster cannot see a body that fixes that cluster and
    breaks another, which is the class of regression the retry's floor guard
    exists to refuse.
    """

    per_cluster: tuple[ClusterReading, ...] = ()

    @property
    def ran(self) -> bool:
        """True when every cluster produced test verdicts."""
        return bool(self.per_cluster) and all(c.ran for c in self.per_cluster)

    @property
    def failures(self) -> int:
        return sum(c.failures for c in self.per_cluster)

    def cluster(self, name: str) -> ClusterReading | None:
        return next((c for c in self.per_cluster if c.cluster == name), None)


class BodyVerdict(StrEnum):
    """What to do with ONE body the moment it lands."""

    KEEP = "keep"
    REVERT_BROKE_BUILD = "broke_build"   # a cluster stopped reporting verdicts
    REVERT_REGRESSED = "regressed"       # a cluster gained failing tests


def body_verdict(*, before: TreeReading, after: TreeReading) -> BodyVerdict:
    """Accept a body only if it regresses NO cluster that builds the file.

    Judged per cluster rather than on the total: a body that fixes one failing
    test in `nightly` and breaks one in `stable` nets zero and must still be
    refused.

    Both comparisons are gated on the cluster having RUN before. A cluster that
    was not producing verdicts has no failure count worth comparing — a body
    that gets it compiling again reports its first real failures, and reading
    those as "regressed from 0" would revert exactly the body that fixed the
    build.
    """
    for aft in after.per_cluster:
        bef = before.cluster(aft.cluster)
        if bef is None or not bef.ran:
            continue
        if not aft.ran:
            return BodyVerdict.REVERT_BROKE_BUILD
        if aft.failures > bef.failures:
            return BodyVerdict.REVERT_REGRESSED
    return BodyVerdict.KEEP


class RetryMode(StrEnum):
    """How the retry attributes failures and re-checks progress."""

    BUILD = "build"   # tree didn't compile — cargo diagnostics drive the fix
    TEST = "test"     # ≥1 test FAILED — reverse-map/classifier drives the fix


class RetryVerdict(StrEnum):
    """What to do with the retry's result."""

    PASS = "pass"               # tests pass now — keep, report success
    REVERT = "revert"           # strictly worse — restore the snapshot
    RECURSE = "recurse"         # strict progress — re-run TASK 3 on the new tree
    ESCALATE = "escalate"       # no progress — hand to TASK 4
    KEEP_PARTIAL = "keep"       # not worse, not passing — keep partial progress


def build_verdict(*, initial_errors: int, final_errors: int,
                  has_snapshot: bool) -> RetryVerdict:
    """BUILD mode: fewer compile errors ⇒ recurse; more ⇒ revert (if we can);
    same ⇒ escalate to TASK 4."""
    if has_snapshot and final_errors > initial_errors:
        return RetryVerdict.REVERT
    if final_errors < initial_errors:
        return RetryVerdict.RECURSE
    return RetryVerdict.ESCALATE


def failing_test_verdict(*, passed: bool, initial_failures: int,
                         final_failures: int, initially_ran: bool,
                         finally_ran: bool, has_snapshot: bool) -> RetryVerdict:
    """TEST mode: pass ⇒ keep. Else revert when the retry broke the build (ran
    before, doesn't now — strictly worse than N failing tests) or increased the
    failure count; otherwise keep the partial progress."""
    if passed:
        return RetryVerdict.PASS
    broke_build = initially_ran and not finally_ran
    if has_snapshot and (broke_build or final_failures > initial_failures):
        return RetryVerdict.REVERT
    return RetryVerdict.KEEP_PARTIAL
