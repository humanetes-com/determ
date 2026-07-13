"""Behaviour extraction use-case (F10 t0d) — harvest a cfg-cluster's behaviour
suite from the original library in the pre-strip window.

Tier 1 (doc-tests) is implemented here; tiers 2 (examples/ + tests/) and 3
(synthesis) attach through the same assembler as their adapters land. Pure
orchestration: the caller supplies the already-read ``(rel_path, source)``
pairs (one pre-strip read, Hard Rule 11); this module classifies them into a
:class:`BehaviourSuite`. Dedup is by ``behaviour_id`` so re-baking is idempotent.
"""
from __future__ import annotations

from collections.abc import Iterable

from pipeline_core.domain.behaviour import BehaviourSpec, BehaviourSuite
from pipeline_core.domain.doctest import (
    harvest_doctest_behaviours,
    harvest_static_assertions,
)


def harvest_behaviour_suite(
    sources: Iterable[tuple[str, str]], *, cfg_cluster: str = "default",
) -> BehaviourSuite:
    """Assemble one cfg-cluster's :class:`BehaviourSuite` from ``(rel_path,
    source_text)`` pairs. Harvests runnable doc-test assertion-behaviours plus
    layout-pinning STATIC_ASSERTION behaviours (F11's selector). Deterministic:
    specs sorted by id, deduped by id."""
    by_id: dict[str, BehaviourSpec] = {}
    for rel, text in sources:
        for spec in harvest_doctest_behaviours(
                text, source_file=rel, cfg_cluster=cfg_cluster):
            by_id.setdefault(spec.behaviour_id, spec)
        for spec in harvest_static_assertions(
                text, source_file=rel, cfg_cluster=cfg_cluster):
            by_id.setdefault(spec.behaviour_id, spec)
    specs = tuple(by_id[k] for k in sorted(by_id))
    return BehaviourSuite(cfg_cluster=cfg_cluster, specs=specs)
