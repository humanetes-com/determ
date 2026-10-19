"""The reference-floor ruling on a sweep that judged every build world.

The one-tree loop's sweep is one capture of the same test command run under
each baked world (`test_results.join_world_captures`). The original crate was
measured per world, so each world's part is judged against that world's
reference, and the sweep meets the floor only when every world does: a
failure the original shares in `nightly` (`ui`, the trybuild harness, on run
`1680589-7a6824f8`) is excused in `nightly` and nowhere else.
"""
from __future__ import annotations

from pipeline_core.domain.reference_floor import (
    FloorVerdict,
    ReferenceMeasurement,
    judge_cluster_binaries,
    pairs_of,
)
from pipeline_core.domain.test_results import parse_binary_results, world_sections


def judge_every_world(
    reference: ReferenceMeasurement | None, capture: str, home: str,
) -> FloorVerdict | None:
    """The floor verdict of `capture`, or None when some world it holds has
    no usable reference. A capture with no world header is `home`'s alone and
    is judged exactly as `judge_cluster_binaries` judges it."""
    sections = world_sections(capture)
    if "" in sections:
        return judge_cluster_binaries(reference, home,
                                      parse_binary_results(capture))
    verdicts: list[FloorVerdict] = []
    for world, text in sections.items():
        v = judge_cluster_binaries(reference, world, parse_binary_results(text))
        if v is None:
            return None
        verdicts.append(v)
    return FloorVerdict(
        cluster="+".join(v.cluster for v in verdicts),
        unmet=frozenset().union(*(v.unmet for v in verdicts)),
        unexcused_failures=frozenset().union(
            *(v.unexcused_failures for v in verdicts)),
        advisory_unmet=frozenset().union(*(v.advisory_unmet for v in verdicts)),
        exceeded=sum(v.exceeded for v in verdicts),
    )


def failed_pairs(capture: str) -> frozenset[tuple[str, str]]:
    """Every (binary label, test) that failed in any world of `capture`."""
    out: set[tuple[str, str]] = set()
    for text in world_sections(capture).values():
        out |= set(pairs_of(parse_binary_results(text))[1])
    return frozenset(out)
