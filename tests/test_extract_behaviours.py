"""Pins for the behaviour-suite assembler (F10 harvest + M12 layout pins)."""
from __future__ import annotations

from pipeline_core.application.extract_behaviours import harvest_behaviour_suite
from pipeline_core.domain.behaviour import OutcomeKind

_SRC_WITH_DOCTEST = '''
/// ```
/// let e = anyhow!("boom");
/// assert_eq!(e.to_string(), "boom");
/// ```
pub fn f() {}
'''

_REPR_TEST = """
#[test]
fn test_error_size() {
    assert_eq!(size_of::<Error>(), size_of::<usize>());
}
"""


def test_suite_harvests_doctest_and_test_layout_pins():
    suite = harvest_behaviour_suite(
        [("lib.rs", _SRC_WITH_DOCTEST)],
        cfg_cluster="stable",
        test_sources=[("tests/repr.rs", _REPR_TEST)],
    )
    assert suite.cfg_cluster == "stable"
    # doc-test assertion-behaviour present
    assert any(s.when.strip().startswith("let e") for s in suite.specs)
    # layout pin from tests/ present as a STATIC_ASSERTION
    statics = [s for s in suite.specs
               if s.then is not None and s.then.kind is OutcomeKind.STATIC_ASSERTION]
    assert statics, "layout pin from tests/*.rs must reach the suite (M12)"
    from pipeline_core.domain.behaviour import BehaviourClass
    assert all(s.klass is BehaviourClass.LAYOUT_PIN for s in statics), (
        "the assembler classifies every tier's specs, not only the test-assertion tier")


def test_suite_deterministic_and_deduped():
    a = harvest_behaviour_suite([("lib.rs", _SRC_WITH_DOCTEST)])
    b = harvest_behaviour_suite([("lib.rs", _SRC_WITH_DOCTEST)])
    assert [s.behaviour_id for s in a.specs] == [s.behaviour_id for s in b.specs]
    ids = [s.behaviour_id for s in a.specs]
    assert len(ids) == len(set(ids))


def test_no_test_sources_still_works():
    suite = harvest_behaviour_suite([("lib.rs", _SRC_WITH_DOCTEST)])
    assert suite.specs


# --- tier 2 (test assertions) reaches the suite -------------------------------------

_FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures" / "behaviours"


def test_suite_carries_runtime_then_from_test_assertions_and_old_tiers_do_not():
    from pipeline_core.domain.doctest import (
        harvest_doctest_behaviours,
        harvest_static_assertions,
    )
    from pipeline_core.domain.layout_pins import harvest_layout_pin_behaviours

    pairs = [(f"tests/{n}", (_FIXTURES / n).read_text(encoding="utf-8"))
             for n in ("test_fmt.rs", "test_ensure.rs", "test_downcast.rs")]
    suite = harvest_behaviour_suite([], cfg_cluster="stable", test_sources=pairs)
    runtime = [s for s in suite.specs if s.has_runtime_then]
    assert runtime, "the assembler must run the test-assertion tier over test_sources"
    assert {s.then.kind for s in runtime} >= {
        OutcomeKind.ALT_DEBUG_STRING, OutcomeKind.TYPE_IDENTITY, OutcomeKind.DISPLAY_STRING}
    ids = [s.behaviour_id for s in suite.specs]
    assert ids == sorted(ids) and len(ids) == len(set(ids))
    # negative control: the OLD tiers alone, called directly, over the same pairs
    old = []
    for rel, text in pairs:
        old += harvest_doctest_behaviours(text, source_file=rel)
        old += harvest_static_assertions(text, source_file=rel)
        old += harvest_layout_pin_behaviours(text, source_file=rel)
    assert sum(1 for s in old if s.has_runtime_then) == 0


def test_default_cluster_reaches_the_classifier():
    from pipeline_core.domain.behaviour import BehaviourClass
    gated = ("#[test]\n#[cfg(error_generic_member_access)]\nfn t() {\n"
             "    assert_eq!(f().unwrap_err().to_string(), \"x\");\n}\n")
    world = harvest_behaviour_suite(
        [], cfg_cluster="nightly", test_sources=[("tests/t.rs", gated)],
        default_cluster="stable")
    home = harvest_behaviour_suite(
        [], cfg_cluster="nightly", test_sources=[("tests/t.rs", gated)])
    # a Display string is EXACT_FORMAT before CFG_WORLD; use a rendered value
    assert world.specs and home.specs
    assert {s.klass for s in world.specs} == {BehaviourClass.EXACT_FORMAT}
