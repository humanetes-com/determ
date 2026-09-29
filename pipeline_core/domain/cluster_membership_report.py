"""The outcome of applying a cluster's cfg-set as a membership filter to a
population of stub fns: how many were examined, and which ones were dropped.

Exists because a skip COUNT alone is diagnostically inverted. "skipped 8" is a
true sentence about the survivors of a filter and reads identically whether the
filter dropped 8 of 30 or 8 of 8 — so a filter that had wrongly emptied the
frontier reported the same healthy-looking line for 9.5h while 20 further stubs
were discarded, and no reader could tell. A count without its denominator and
without the identity of what it dropped is not evidence.

Renderer and parser live together on purpose: two scan routes emit this line
(the tuple-form and record-form cluster scans) and a health detector reads it
back, so three components share one sentence. Keeping the format in one place
is what stops the emitters and the reader from drifting apart — the previous
shape of this code had the format written out twice and matched by substring in
a third file, and only one of the two copies ever got corrected.

Token scans only, no regex (repo rule).
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ClusterMembershipReport"]

_TAG = "  [phase-c/per-fn] cluster "
_MARK_SKIPPED = ": skipped "
_MARK_TAIL = " stub(s) not active in this cluster's cfg-set"
_OF = " of "
# The frontier-admission form: what one TDD iteration's frontier, pairs,
# session hops and measured-owner admits proposed at sites the cluster never
# compiles. A different population from the stub scan, so a different tag
# and noun: the health detector's "filter emptied" reading is about the stub
# scan, and a frontier line must never be read as one.
_FRONTIER_TAG = "  [phase-c/tdd] cluster "
_FRONTIER_TAIL = " frontier unit(s) not active in this cluster's cfg-set"


@dataclass(frozen=True, slots=True)
class ClusterMembershipReport:
    """`total` stubs were examined for `cluster_name`; `skipped` names the ones
    the cfg-set excluded. `kept` is what actually reaches the fill routes."""

    cluster_name: str
    total: int
    skipped: tuple[str, ...] = ()
    # True for the TDD iteration's frontier-admission report; False for the
    # stub scan's.
    frontier: bool = False

    @property
    def kept(self) -> int:
        return self.total - len(self.skipped)

    @property
    def emptied(self) -> bool:
        """True when the filter dropped its entire input. Not a state to
        interpret — a filter with input and no output is a contradiction to
        locate, since every stub on disk was put there by the same bake that
        the cluster was derived from."""
        return self.total > 0 and self.kept == 0

    def log_line(self) -> str:
        tag, tail = ((_FRONTIER_TAG, _FRONTIER_TAIL) if self.frontier
                     else (_TAG, _MARK_TAIL))
        return (f"{tag}{self.cluster_name}{_MARK_SKIPPED}"
                f"{len(self.skipped)}{_OF}{self.total}{tail}: "
                f"{', '.join(sorted(self.skipped))}")

    @classmethod
    def parse(cls, line: str) -> ClusterMembershipReport | None:
        """Recover a report from its own `log_line`, else None.

        Rejects the legacy bare-count form (`skipped 8 stub(s) …`) rather than
        guessing a denominator for it: a reader that invents `total` would
        resurrect the exact ambiguity this type exists to remove. A stale-format
        segment therefore yields no reading at all, which is the honest answer.
        """
        frontier = _FRONTIER_TAIL in line
        tag, tail = ((_FRONTIER_TAG, _FRONTIER_TAIL) if frontier
                     else (_TAG, _MARK_TAIL))
        i = line.find(_MARK_SKIPPED)
        j = line.find(tail)
        if i < 0 or j <= i:
            return None
        c = line.rfind(tag, 0, i)
        if c < 0:
            return None
        counts = line[i + len(_MARK_SKIPPED):j].split(_OF)
        if len(counts) != 2 or not all(p.strip().isdigit() for p in counts):
            return None
        n_skipped, total = int(counts[0].strip()), int(counts[1].strip())
        names = tuple(
            n for n in (s.strip() for s in
                        line[j + len(tail):].lstrip(":").split(","))
            if n
        )
        if len(names) != n_skipped:
            return None
        return cls(line[c + len(tag):i], total, names, frontier=frontier)
