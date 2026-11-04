"""The cluster-active stubs on the tree at the moment a judge reads them.

Two judges ask "which bodies are still hollow": the per-fn sibling-blocked
verdict (`dispatch.sibling_blocked_u2_failures`, which refuses a unit the fix
loop) and the regression gate's `stub_free` (which reverts a crashing keep at
once when no hollow sibling is left to blame). Both used to be handed a value
scanned once at the start of a pass or of a route call. A fill now runs for
hours and takes blockers into the running per-fn run, so that value described
a tree tens of keeps old. On run `994565-a5b000bb` (2026-11-04, cluster
`stable`, fill 1) `src/error.rs::msg` was kept at about minute 10, and about
40 minutes later `src/error.rs::construct_from_adhoc`, whose own body the
crash settle had just blamed, was refused the fix loop and stopped as
unpayable because "every failing attributed test needs still-stubbed sibling
src/error.rs::msg". Over the recorded event log, 46 of 69 such verdicts in 12
runs named a sibling already kept earlier in the same fill
(`scripts/stale_sibling_block_census.py`).

So the judges are handed a READER, not a set: `LiveStubs` holds the scan and
runs it when asked. There is no field that stores a scan's result, so a new
judge cannot be built on a stale one by forgetting to refresh it.

A site the cluster never compiles is not a stub here, whatever its body: it
cannot panic in this cluster's binaries (`cluster_membership.cluster_dead_sites`).
An unavailable scan is unknown, never "no stubs": `keys()` returns None and
`free()` is False, the direction in which neither judge refuses anything it
would not have refused before.
"""
from __future__ import annotations

from collections.abc import Callable, Container, Iterable
from dataclasses import dataclass, field

from pipeline_core.domain.models import FnLocator


def _nothing_dead() -> frozenset[FnLocator]:
    return frozenset()


@dataclass(frozen=True, slots=True)
class LiveStubs:
    """A reader of the cluster-active stub set of the tree as it is now."""

    # Every stub the cluster's scan finds on the tree right now, or None when
    # the scan cannot run (no extractor, no cluster).
    scan: Callable[[], Iterable[FnLocator] | None]
    # The sites this cluster's build never compiles.
    cfg_dead: Container[FnLocator] = field(default_factory=_nothing_dead)

    def keys(self) -> frozenset[FnLocator] | None:
        """The cluster-active stubs now; None when the scan is unavailable."""
        found = self.scan()
        if found is None:
            return None
        return frozenset(k for k in found if k not in self.cfg_dead)

    def free(self) -> bool:
        """True only when a scan ran now and found no cluster-active stub."""
        keys = self.keys()
        return keys is not None and not keys


#: The reader of a caller that has no tree to scan: unknown, never empty.
UNKNOWN_STUBS = LiveStubs(scan=lambda: None)
