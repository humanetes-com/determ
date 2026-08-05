"""Which cluster's answers are better believed (pure domain).

Clusters run serially and seed each other forward: a later cluster inherits the
bodies earlier ones accepted, and nothing flows backward. Once a cluster is
checkpointed it is skipped on every later run, permanently — so a cluster that
plateaued early keeps its bodies forever, and the merge, which gates a divergent
item by the cluster it came from, keeps shipping them.

Measured on `anyhow`: `stable` was last swept 2026-07-30 16:20 with
`test_downcast` and `test_large_alignment` in its `failed[]` and checkpointed
there; `nightly` won both on 2026-08-04 22:43 and finished at 100/104. `stable`
ran FIRST, so it never saw nightly's answers and will never run again. The
merged tree's default build takes stable's copy of every off-axis divergence,
which is why it cored on `tests/test_downcast.rs` while both cluster trees were
the best they had ever been.

The rule here is deliberately narrow, because pass COUNTS across clusters are
not comparable — the clusters run different oracles (nightly's cfg excludes 20
tests from the build, so 100/104 and 112/121 measure different things). What IS
comparable is the failure SETS restricted to the tests both clusters ran: if
one cluster fails a strict subset of what another fails, it is better on every
shared test and worse on none. That is a fact about the same tests, and it is
the only cross-cluster comparison this module will make.
"""

from __future__ import annotations

from collections.abc import Mapping, Set


def dominant_cluster(
    failed_by_cluster: Mapping[str, Set[str]],
    ran_by_cluster: Mapping[str, Set[str]] | None = None,
) -> str | None:
    """The cluster whose failures are a strict subset of every other's, or None.

    `failed_by_cluster` maps a cluster to the tests it last failed;
    `ran_by_cluster` maps it to the tests it ran at all. Comparison is
    restricted to the tests each PAIR shares, because a test one cluster's cfg
    excludes from the build says nothing about the other's answers. With
    `ran_by_cluster` omitted, the union of the failure sets is used as the
    shared scope — enough to order two clusters whose failures nest, and never
    enough to order two that merely differ.

    None when fewer than two clusters are known, when any two clusters' failures
    are incomparable, or when two clusters fail exactly the same tests: an
    arbitrary winner is what this is meant to replace.
    """
    names = sorted(failed_by_cluster)
    if len(names) < 2:
        return None

    def shared(a: str, b: str) -> Set[str]:
        if ran_by_cluster is None:
            return set(failed_by_cluster[a]) | set(failed_by_cluster[b])
        return set(ran_by_cluster.get(a, ())) & set(ran_by_cluster.get(b, ()))

    best: str | None = None
    for cand in names:
        if all(
            (set(failed_by_cluster[cand]) & shared(cand, other))
            < (set(failed_by_cluster[other]) & shared(cand, other))
            for other in names
            if other != cand
        ):
            # A strict subset of every other cluster's failures on the tests
            # they share — better on at least one, worse on none.
            best = cand
            break
    return best
