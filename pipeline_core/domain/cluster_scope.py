"""ClusterScope — the explicit run-state for "which compilation cluster is
active right now" plus the full cluster plan (Hard Rule 8).

Replaces the two mutable ``pipeline.py`` module globals (``_current_cluster`` /
``_all_clusters``) with one typed, injectable object. It is deliberately mutable:
the pipeline iterates clusters, activating each in turn, and engine code reads
the active one at call time (e.g. ``_cargo_env`` builds the cluster's env). The
old call-time getter lambdas (``lambda: _current_cluster``) become the bound
:meth:`get_active`.

Structurally satisfies ``ports.augment.ClusterScope`` (the ``set_active`` the
ClusterAugmentor drives), so it can be injected as the scope directly — no
composition-root adapter method needed. Stdlib only, no I/O."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .models import BuildCluster


@dataclass
class ClusterScope:
    """The active cluster + the full plan for the current run."""

    active: BuildCluster | None = None
    all: list[BuildCluster] = field(default_factory=list)

    def set_active(self, cluster: BuildCluster | None) -> None:
        """ports.augment.ClusterScope — activate a cluster (or clear with None)."""
        self.active = cluster

    def get_active(self) -> BuildCluster | None:
        """Call-time getter (the seam engine deps bind instead of reading a global)."""
        return self.active

    def reset(self, clusters: Iterable[BuildCluster]) -> None:
        """Install the full cluster plan from the cfg scan (clears the active one)."""
        self.all = list(clusters)
        self.active = None
