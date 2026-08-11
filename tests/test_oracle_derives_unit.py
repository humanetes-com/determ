"""`oracle_derives` — the traits the frozen tests require of a layout type.

The defect these guard, measured 2026-08-11: the layout-authoring prompt does
not contain the tests, so a trait the tests call has to be inferred, and across
eight authorings (two crates, two models, two context sizes, two reasoning
strengths) the inference was wrong 5 times. Each miss cost the whole cluster —
`error[E0599] ... 'default' found for struct 'Counter'` means no test binary is
produced, so every test scores as failing.
"""
from __future__ import annotations

from pipeline_core.domain.oracle_derives import (
    derive_requirements_text,
    scan_required_derives,
)

# The exact call that broke five of eight runs.
_INTEGRATION = """
#[test]
fn counter_from_default() {
    let mut c = Counter::default();
    assert_eq!(c.bump(), 1);
}
"""


def test_associated_call_names_its_own_type() -> None:
    reqs = scan_required_derives({"tests/integration.rs": _INTEGRATION}, {"Counter"})
    assert [(r.type_name, r.trait) for r in reqs] == [("Counter", "Default")]
    assert reqs[0].evidence == "Counter::default()"
    assert reqs[0].origin == "tests/integration.rs:4"


def test_only_requested_types_are_reported() -> None:
    """A component is authored in isolation; a requirement naming a type this
    component does not declare would ask for a derive on an item the reply is
    not allowed to emit."""
    assert scan_required_derives({"t.rs": _INTEGRATION}, {"Unrelated"}) == ()
    assert scan_required_derives({"t.rs": _INTEGRATION}, set()) == ()


def test_equality_assertion_requires_partialeq_and_debug() -> None:
    src = "assert_eq!(Wire::default(), Wire::default());"
    got = {(r.type_name, r.trait) for r in scan_required_derives({"a.rs": src}, {"Wire"})}
    assert got == {("Wire", "Default"), ("Wire", "PartialEq"), ("Wire", "Debug")}


def test_inherent_constructor_is_not_a_derive() -> None:
    """`new` is written by the layout author. Requiring a derive for it would
    demand a trait that does not exist."""
    assert scan_required_derives({"a.rs": "let c = Counter::new(3);"}, {"Counter"}) == ()


def test_method_call_on_a_value_is_not_attributed() -> None:
    """`x.clone()` does not name its type. Guessing would push `Clone` onto a
    type that may be unable to derive it — a failure identical to the one this
    module exists to prevent, blaming the wrong item."""
    assert scan_required_derives({"a.rs": "let d = c.clone();"}, {"Counter"}) == ()


def test_first_citation_is_kept_and_pairs_dedupe() -> None:
    src = "Counter::default(); Counter::default(); Counter::default();"
    reqs = scan_required_derives({"a.rs": src}, {"Counter"})
    assert len(reqs) == 1
    assert reqs[0].origin == "a.rs:1"


def test_requirements_text_states_the_consequence_and_groups_per_type() -> None:
    text = derive_requirements_text(
        scan_required_derives({"tests/integration.rs": _INTEGRATION}, {"Counter"}))
    assert "`Counter` must derive: Default" in text
    assert "tests/integration.rs:4" in text
    # The cost of omission is the whole build, not one test — the prompt has to
    # say so or the requirement reads as a nicety.
    assert "EVERY test scores as failing" in text


def test_requirements_text_is_empty_without_requirements() -> None:
    """An empty note must add NOTHING to the prompt: a heading with no rows
    below it reads as 'no derives are required', which is a different claim."""
    assert derive_requirements_text(()) == ""


def test_negative_control_pre_fix_prompt_carried_no_requirement() -> None:
    """The pre-fix behaviour, asserted so this cannot silently regress to it:
    with no scan, the authoring prompt said nothing about `Default` and the
    model had to guess. The scan is what makes attempt 1 able to satisfy it."""
    assert derive_requirements_text([]) == ""
    assert "Default" in derive_requirements_text(
        scan_required_derives({"t.rs": _INTEGRATION}, {"Counter"}))
