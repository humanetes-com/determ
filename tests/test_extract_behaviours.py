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


def test_suite_deterministic_and_deduped():
    a = harvest_behaviour_suite([("lib.rs", _SRC_WITH_DOCTEST)])
    b = harvest_behaviour_suite([("lib.rs", _SRC_WITH_DOCTEST)])
    assert [s.behaviour_id for s in a.specs] == [s.behaviour_id for s in b.specs]
    ids = [s.behaviour_id for s in a.specs]
    assert len(ids) == len(set(ids))


def test_no_test_sources_still_works():
    suite = harvest_behaviour_suite([("lib.rs", _SRC_WITH_DOCTEST)])
    assert suite.specs
