"""Pins for composition-gap targeting + synthesis prompt (F10 M7)."""

from __future__ import annotations

from pipeline_core.domain.behaviour import (
    BehaviourSpec,
    BehaviourSubKind,
)
from pipeline_core.domain.composition_gap import (
    covered_units,
    synthesis_targets,
    uncovered_units,
)
from pipeline_core.prompts.behaviour_synthesis import build_behaviour_synthesis_prompt


def _spec(bid, exercises):
    return BehaviourSpec(
        behaviour_id=bid,
        given="",
        when="x",
        then=None,
        subkind=BehaviourSubKind.ASSERTION,
        exercises=frozenset(exercises),
    )


def test_covered_and_uncovered_units():
    specs = (_spec("a", {"context", "chain"}),)
    allu = frozenset({"context", "chain", "root_cause", "downcast"})
    assert covered_units(specs) == frozenset({"context", "chain"})
    assert uncovered_units(allu, specs) == frozenset({"root_cause", "downcast"})


def test_synthesis_targets_sorted_and_capped():
    specs = ()
    allu = frozenset({"c", "a", "b", "d"})
    assert synthesis_targets(allu, specs, max_targets=2) == ("a", "b")


def test_no_targets_when_fully_covered():
    specs = (_spec("a", {"x", "y"}),)
    assert synthesis_targets(frozenset({"x", "y"}), specs) == ()


def test_synthesis_prompt_names_targets_and_forbids_internals():
    p = build_behaviour_synthesis_prompt(
        module_name="anyhow",
        cfg_cluster="stable",
        target_units=("root_cause", "downcast"),
        public_surface="pub fn context();",
        symbol_inventory="Box, Chain",
    )
    assert "root_cause" in p and "downcast" in p
    assert "PUBLIC API ONLY" in p
    assert "<IN_SCOPE_SYMBOLS>" in p and "Box, Chain" in p
    # Empty inventory omits the tag: the live collaborator is a constant "",
    # and filling it from PUBLIC_API_SURFACE would dump that block twice.
    empty = build_behaviour_synthesis_prompt(
        module_name="anyhow",
        cfg_cluster="stable",
        target_units=("root_cause",),
        public_surface="pub fn context();",
        symbol_inventory="",
    )
    assert "<IN_SCOPE_SYMBOLS>" not in empty
    # The CONSTRAINED shape: setup, ONE expression, a render kind — and the
    # two rules measured 2026-08-27 to be what turns a capture from an
    # unusable multi-println blob into something a test can assert.
    assert "<BEHAVIOUR name=" in p
    assert "<WHEN>" in p and "<GIVEN>" in p and "<OBSERVE>" in p
    assert "RENDERED VALUE" in p  # no comparisons: a bool loses the value
    assert "Do NOT state a class" in p  # the selector supplies it
