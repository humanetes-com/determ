"""When the layout registry itself is the stale side (pure domain).

`layout-sync` reinstalls the crate-wide registry's authored definitions over
every cluster tree at entry, and judges the reinstall the way a body is
judged: on a GREEN tree it is rolled back if the tree stops compiling. That
rollback is evidence about the REGISTRY, not just about this entry: the
registry holds a layout the live tree cannot build, and since the registry is
permanent and the tree is not converging toward it on its own, the same
reinstall→rollback pair repeats at every cluster entry (measured 2026-08-19,
run entry 19:38:42: 7 authored definitions — `lib.rs::Error`,
`error.rs::ErrorImpl`, `error.rs::ErrorVTable`, `ptr.rs::Mut`, +3 — into 4
live files, rolled back on `E0308 src/error.rs:220` field-set mismatch plus
an E0277, tree preserved byte-identical).

The green path's cost is bounded — two cargo checks per entry. The RED path
is not: on a red-at-entry tree the reinstall is KEPT by rule (correctly for
the case where the tree's bodies answer to the registry — the nightly
`3579062-476abc3b` post-mortem), and `_restub_after_reinstall` then re-stubs
every body the compiler implicates against the adopted definitions. With the
registry divergent as measured, a single red entry converts into a re-stub
wave across the files holding the crawl's fills, toward a layout three
compile errors say those fills cannot build.

So the green path's repeated rollback is treated as a MEASUREMENT of the
registry: after `threshold` consecutive green-tree rollbacks, the registry is
stale-on-the-registry-side and the sync stops re-adopting it — on green AND
red entries — until the streak clears. It clears when a reinstall lands
(rc 0 after apply) or when the plans converge (nothing superseded: the
registry and the tree agree again, e.g. after a re-author refreshed the
registry). A red entry neither charges nor clears: a tree that does not
build cannot judge the registry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Consecutive green-tree rollbacks after which the registry is stale.
#: 2 rather than 1: one rollback can be a transient tree state (a body the
#: fill loop is about to rewrite); the second consecutive one is the pattern
#: repeating, which is the measured behaviour of a genuinely divergent
#: registry (it reverts at EVERY entry until re-convergence).
DEFAULT_STALE_AFTER = 2


@dataclass(frozen=True, slots=True)
class ReinstallStreak:
    """Consecutive green-tree reinstall rollbacks for one cluster."""

    green_reverts: int = 0

    def reverted_on_green(self) -> ReinstallStreak:
        return ReinstallStreak(self.green_reverts + 1)

    def cleared(self) -> ReinstallStreak:
        """A reinstall landed, or the plans converged — the registry and this
        cluster's tree agree again."""
        return ReinstallStreak(0)

    def stale(self, threshold: int = DEFAULT_STALE_AFTER) -> bool:
        """A non-positive threshold DISABLES the staleness verdict (same
        convention as the plateau budgets in `domain/plateau.py`)."""
        return threshold > 0 and self.green_reverts >= threshold

    def to_dict(self) -> dict[str, Any]:
        return {"green_reverts": self.green_reverts}

    @staticmethod
    def from_dict(raw: Any) -> ReinstallStreak:
        """Total: a corrupt state file costs a fresh streak, never the run —
        and a fresh streak fails SAFE here (the sync re-adopts, which is the
        pre-guard behaviour)."""
        try:
            return ReinstallStreak(max(0, int(raw["green_reverts"])))
        except (KeyError, TypeError, ValueError):
            return ReinstallStreak()
