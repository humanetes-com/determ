"""A tree's reading in every build world, and the one rule that keeps a body.

Task 3's inline retry introduced these types (a reading of one tree under
every cluster, and a per-body verdict that refuses a body regressing ANY
cluster); the retry was removed on 2026-10-20 because it wrote bodies into
the merged tree that the next merge overwrote. The reading stays: it is the
judge a keep site needs once one tree is measured in every world.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class ClusterReading:
    """One cluster's verdict on one tree state. `ran` is the compile/run state —
    a cluster whose binaries never reported has no meaningful `failures`.

    `diag_count` is the DISTINCT compile-diagnostic count observed when
    `ran=False` (0 whenever the cluster ran: a tree that produced verdicts
    compiled). It is what lets two broken readings of the same cluster be
    compared at all — deduplicated, because a cluster-wide break reprints the
    same lib error once per test binary, so a raw occurrence count moves with
    binary coverage while the distinct set is stable."""

    cluster: str
    ran: bool
    failures: int
    diag_count: int = 0
    # The failing tests counted in `failures`, and the cargo text the reading
    # was taken from. Neither takes part in a comparison; they are what a
    # revert hands the next ask of the same definition. The gate used to read
    # each cluster and drop its output, so a body reverted `broke_build` left
    # no diagnostic anywhere: on run `641699-ae684bdb` (2026-10-19) the
    # rejected `self.error.as_ref().provide(request)` and the one nightly
    # error it caused were in no later prompt.
    failing: frozenset[str] = field(default=frozenset(), compare=False)
    output: str = field(default="", compare=False, repr=False)


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
                                         # or new compile errors while broken


@dataclass(frozen=True, slots=True)
class BodyJudgement:
    """A body's verdict and the cluster readings that decided it — `before`
    and `after` are None exactly when the verdict is KEEP."""

    verdict: BodyVerdict
    before: ClusterReading | None = None
    after: ClusterReading | None = None


def body_verdict(*, before: TreeReading, after: TreeReading) -> BodyVerdict:
    """:func:`judge_body`'s verdict alone."""
    return judge_body(before=before, after=after).verdict


def judge_body(*, before: TreeReading, after: TreeReading) -> BodyJudgement:
    """Accept a body only if it regresses NO cluster that builds the file.

    Judged per cluster rather than on the total: a body that fixes one failing
    test in `nightly` and breaks one in `stable` nets zero and must still be
    refused.

    The failure-count comparisons are gated on the cluster having RUN before.
    A cluster that was not producing verdicts has no failure count worth
    comparing — a body that gets it compiling again reports its first real
    failures, and reading those as "regressed from 0" would revert exactly the
    body that fixed the build.

    A cluster broken on BOTH sides is compared on its distinct
    compile-diagnostic count instead of being skipped. Skipping it entirely
    meant a broken cluster could not get worse: for 4h15m on 2026-08-10 (run
    `2514529-5e77dafb`) every accepted splice was gated on "stable didn't
    regress, nightly still says NO VERDICTS" — nightly's 2 E0605s free to
    become N with no gate able to see it, during the repair of the other
    cluster. Equal counts stay neutral (the splice did not touch that
    cluster's break), and the broken→running transition still keeps.
    """
    for aft in after.per_cluster:
        bef = before.cluster(aft.cluster)
        if bef is None:
            continue
        if not bef.ran:
            if not aft.ran and aft.diag_count > bef.diag_count:
                return BodyJudgement(BodyVerdict.REVERT_REGRESSED, bef, aft)
            continue
        if not aft.ran:
            return BodyJudgement(BodyVerdict.REVERT_BROKE_BUILD, bef, aft)
        if aft.failures > bef.failures:
            return BodyJudgement(BodyVerdict.REVERT_REGRESSED, bef, aft)
    return BodyJudgement(BodyVerdict.KEEP)
