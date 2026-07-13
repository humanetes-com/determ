"""Pins for the behaviour few-shot selector (F10 Use-B / M5)."""
from __future__ import annotations

from pipeline_core.domain.behaviour import (
    BehaviourSpec,
    BehaviourSubKind,
    FenceDirective,
)
from pipeline_core.domain.behaviour_select import (
    is_relevant,
    select_usage_examples,
)


def _spec(bid, when, *, exercises=frozenset(), fence=FenceDirective.RUN):
    return BehaviourSpec(
        behaviour_id=bid, given="", when=when, then=None,
        subkind=BehaviourSubKind.ASSERTION, fence=fence,
        exercises=exercises, source_ref=bid)


def test_relevant_by_fn_name_token():
    s = _spec("d1", 'let e = context(x); assert_eq!(e.to_string(), "y");')
    assert is_relevant(s, fn_name="context", container="")
    assert not is_relevant(s, fn_name="downcast", container="")


def test_relevant_by_container_type():
    s = _spec("d2", "let c = Chain::new(); c.next();")
    assert is_relevant(s, fn_name="next", container="impl Iterator for Chain")


def test_relevant_by_exercises_id():
    s = _spec("d3", "opaque();", exercises=frozenset({"root_cause"}))
    assert is_relevant(s, fn_name="root_cause", container="")


def test_compile_fail_never_used_as_example():
    s = _spec("d4", "bad_usage();", fence=FenceDirective.COMPILE_FAIL)
    assert not is_relevant(s, fn_name="bad_usage", container="")


def test_select_renders_matching_specs_and_respects_budget():
    specs = (
        _spec("a", 'let e = anyhow!("x"); e.chain();'),
        _spec("b", "unrelated();"),
        _spec("c", "let e2 = anyhow!(\"y\"); e2.chain();"),
    )
    out = select_usage_examples(tuple(specs), fn_name="chain", container="",
                                char_budget=10_000)
    assert "e.chain()" in out and "e2.chain()" in out
    assert "unrelated" not in out
    # whole-spec budget: a tiny budget keeps only the first, never a fragment.
    small = select_usage_examples(tuple(specs), fn_name="chain", container="",
                                  char_budget=1)
    assert small.count("chain()") <= 1


def test_select_empty_when_nothing_matches():
    specs = (_spec("a", "foo();"),)
    assert select_usage_examples(specs, fn_name="bar", container="",
                                 char_budget=1000) == ""
