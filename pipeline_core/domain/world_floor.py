"""The reference-floor ruling on a test capture, per build world.

The one-tree loop's sweep is one capture of the same test command run under
each baked world (`test_results.join_world_captures`). The original crate was
measured per world, so each world's part is judged against that world's
reference, and the sweep meets the floor only when every world does: a
failure the original shares in `nightly` (`ui`, the trybuild harness, on run
`1680589-7a6824f8`) is excused in `nightly` and nowhere else.

A failure the original shares is excused only when it is the same failure
(`reference_floor.judge_against_reference`), so judging a capture reads how
each such test failed here: the caller's `SignatureReader` runs it alone in
this tree, and the capture supplies what the reader needs (the libtest name,
the executable cargo named, the failure block). Every gate that asks "is this
cluster done" reads through :func:`judge_cluster_capture`: task 2a's close and
withdrawn set, task 3's per-cluster verdict, and the module's out-of-reach
stop.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping

from pipeline_core.domain.oracle_ledger import bare_test_name
from pipeline_core.domain.reference_floor import (
    ClusterReference,
    FloorVerdict,
    ReferenceMeasurement,
    binary_pairs,
    judge_against_reference,
    pairs_of,
)
from pipeline_core.domain.same_failure import (
    FailedReading,
    FailureToRead,
    SignatureReader,
)
from pipeline_core.domain.test_results import (
    parse_binary_results,
    split_test_stdout_sections,
    world_sections,
)

#: The reader for one world's part of a capture (`world` is "" for a
#: capture that names no world).
ReaderForWorld = Callable[[str], SignatureReader]


def failures_to_read(reference: ClusterReference | None, capture: str,
                     world: str = "") -> tuple[FailureToRead, ...]:
    """The failing tests of `capture` whose failure the floor must compare:
    those the reference failed (all failing tests when `reference` is None,
    which is how the reference reads its own). One entry per pair."""
    bodies: dict[str, str] = {}
    for name, body in split_test_stdout_sections(capture):
        bodies.setdefault(name, body)
    out: dict[tuple[str, str], FailureToRead] = {}
    for b in parse_binary_results(capture):
        for verdict, raw, pair in binary_pairs(b):
            if verdict != "failed" or pair in out:
                continue
            if reference is not None and pair not in reference.failed:
                continue
            out[pair] = FailureToRead(pair=pair, label=b.label, name=raw,
                                      exe_path=b.exe_path, world=world,
                                      body=bodies.get(raw, ""))
    return tuple(out[p] for p in sorted(out))


def judge_cluster_capture(
    reference: ReferenceMeasurement | None,
    cluster: str,
    capture: str,
    read: SignatureReader,
) -> FloorVerdict | None:
    """The reference-floor ruling on one cluster's test capture, or None
    when there is no usable reference for that cluster."""
    if reference is None or not reference.usable:
        return None
    cref = reference.for_cluster(cluster)
    if cref is None:
        return None
    ours_passed, ours_failed, _, _ = pairs_of(parse_binary_results(capture))
    todo = failures_to_read(cref, capture, cluster)
    signatures = dict(read(todo)) if todo else {}
    return judge_against_reference(
        cref, ours_passed, FailedReading(pairs=ours_failed, signatures=signatures))


def judge_every_world(
    reference: ReferenceMeasurement | None, capture: str, home: str,
    read_for_world: ReaderForWorld,
) -> FloorVerdict | None:
    """The floor verdict of `capture`, or None when some world it holds has
    no usable reference. A capture with no world header is `home`'s alone and
    is judged exactly as `judge_cluster_capture` judges it."""
    sections = world_sections(capture)
    if "" in sections:
        return judge_cluster_capture(reference, home, capture, read_for_world(""))
    verdicts: list[FloorVerdict] = []
    for world, text in sections.items():
        v = judge_cluster_capture(reference, world, text, read_for_world(world))
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
        diverged=frozenset().union(*(v.diverged for v in verdicts)),
        divergence={k: d for v in verdicts for k, d in v.divergence.items()},
        excused=frozenset().union(*(v.excused for v in verdicts)),
        excused_by_name=frozenset().union(*(v.excused_by_name for v in verdicts)),
        # A doc test met through its stand-in in one world and advisory in
        # another is advisory: the union is a statement about every world.
        doc_stand_ins=(frozenset().union(*(v.doc_stand_ins for v in verdicts))
                       - frozenset().union(*(v.advisory_unmet for v in verdicts))),
        advisory_gaps=_world_gaps(verdicts),
    )


def _world_gaps(verdicts: list[FloorVerdict]) -> dict[tuple[str, str], str]:
    """Each advisory doc test's reasons, named by the world that gave them:
    a doc test can stand in through its example in one world and not in
    another (an example the original passes only under a world's cfg)."""
    out: dict[tuple[str, str], list[str]] = {}
    for v in verdicts:
        for pair, gap in sorted(v.advisory_gaps.items()):
            out.setdefault(pair, []).append(f"{v.cluster}: {gap}")
    return {pair: "; ".join(gaps) for pair, gaps in out.items()}


def excused_everywhere(
    judged: Mapping[str, tuple[str, FloorVerdict | None]],
) -> frozenset[str]:
    """Bare names of the failing tests the floor excuses in EVERY cluster
    they failed in, from `cluster -> (that cluster's capture, its verdict)`.
    A test failing in a cluster whose verdict did not excuse it (or that had
    no verdict) is not excused, whatever another cluster said: the
    original's failure under one cfg says nothing about this tree under
    another."""
    excused: set[str] = set()
    not_excused: set[str] = set()
    for capture, verdict in judged.values():
        failing = {bare_test_name(t) for _l, t in failed_pairs(capture)}
        here = ({bare_test_name(t) for _l, t in verdict.excused}
                if verdict is not None else set())
        excused |= here & failing
        not_excused |= failing - here
    return frozenset(excused - not_excused)


def failed_pairs(capture: str) -> frozenset[tuple[str, str]]:
    """Every (binary label, test) that failed in any world of `capture`."""
    out: set[tuple[str, str]] = set()
    for text in world_sections(capture).values():
        out |= set(pairs_of(parse_binary_results(text))[1])
    return frozenset(out)
