"""Behaviour extraction use-case (F10 t0d) — harvest a cfg-cluster's behaviour
suite from the original library in the pre-strip window.

Three harvest tiers share one assembler: doc-tests (assertion-behaviours and
doc-fence layout pins) from ``src/``, the layout pins that live in
``tests/*.rs`` bodies, and — since 2026-08-26 — the OUTCOME behaviours the
crate's own test assertions pin (`domain/test_assertions.py`: `assert_eq!`
against a string literal or a same-file `const`, `is::<T>()`/`downcast::<T>()`
identity, and literals routed through an asserting helper). Before the third
tier the anyhow suite held 0 runtime `then` over 30 specs, so nothing in it
could pin the observables the stuck tree fails on. Pure orchestration: the
caller supplies the already-read ``(rel_path, source)`` pairs (one pre-strip
read, Hard Rule 11); this module classifies them into a :class:`BehaviourSuite`.
Dedup is by ``behaviour_id`` so re-baking is idempotent.
"""
from __future__ import annotations

from collections.abc import Iterable

from pipeline_core.domain.behaviour import BehaviourSpec, BehaviourSuite, classified
from pipeline_core.domain.doctest import (
    harvest_doctest_behaviours,
    harvest_static_assertions,
)
from pipeline_core.domain.layout_pins import harvest_layout_pin_behaviours
from pipeline_core.domain.test_assertions import harvest_test_assertion_behaviours


def harvest_behaviour_suite(
    sources: Iterable[tuple[str, str]],
    *,
    cfg_cluster: str = "default",
    test_sources: Iterable[tuple[str, str]] = (),
    default_cluster: str | None = None,
) -> BehaviourSuite:
    """Assemble one cfg-cluster's :class:`BehaviourSuite`.

    ``sources`` = crate ``src/*.rs`` ``(rel, text)`` pairs → doc-test
    assertion-behaviours + doc-test STATIC_ASSERTION pins. ``test_sources`` =
    integration/unit ``tests/*.rs`` pairs → the layout pins (`size_of`, NPO,
    autotraits) that live in test bodies (M12 — the source M2's selector reads
    from) AND the test-assertion OUTCOME behaviours (runtime `then`).
    ``default_cluster`` names the crate's default cluster for the CFG_WORLD
    class (None = this cluster). Deterministic: specs sorted + deduped by id."""
    by_id: dict[str, BehaviourSpec] = {}
    for rel, text in sources:
        for spec in harvest_doctest_behaviours(
                text, source_file=rel, cfg_cluster=cfg_cluster):
            by_id.setdefault(spec.behaviour_id, spec)
        for spec in harvest_static_assertions(
                text, source_file=rel, cfg_cluster=cfg_cluster):
            by_id.setdefault(spec.behaviour_id, spec)
    for rel, text in test_sources:
        for spec in harvest_layout_pin_behaviours(
                text, source_file=rel, cfg_cluster=cfg_cluster):
            by_id.setdefault(spec.behaviour_id, spec)
        for spec in harvest_test_assertion_behaviours(
                text, source_file=rel, cfg_cluster=cfg_cluster,
                default_cluster=default_cluster):
            by_id.setdefault(spec.behaviour_id, spec)
    # Every tier's spec gets its class here, once: the doc-fence and layout-pin
    # harvesters predate `klass` and construct specs with the default OTHER,
    # which on the live anyhow harvest (2026-08-26) left all 6 STATIC_ASSERTION
    # pins histogrammed as OTHER instead of LAYOUT_PIN.
    home = default_cluster or cfg_cluster
    specs = tuple(classified(by_id[k], default_cluster=home) for k in sorted(by_id))
    return BehaviourSuite(cfg_cluster=cfg_cluster, specs=specs)
