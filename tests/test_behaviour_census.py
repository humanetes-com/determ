"""Pins for the shared census definitions: the three defect-observable
patterns give 3/3 on the test-assertion harvest of the anyhow fixtures and
0/3 with a runtime then on the OLD tiers (negative control), and the script
uses the domain's patterns rather than a copy."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.extract_behaviours import harvest_behaviour_suite
from pipeline_core.domain.behaviour_census import (
    DEFECT_OBSERVABLES,
    defect_observable_flags,
    defect_observable_presence,
    klass_histogram,
    render_counts,
    runtime_then_counts,
)
from pipeline_core.domain.doctest import harvest_doctest_behaviours

FIXTURES = Path(__file__).parent / "fixtures" / "behaviours"


def _pairs():
    return [(f"tests/{n}", (FIXTURES / n).read_text(encoding="utf-8"))
            for n in ("test_fmt.rs", "test_ensure.rs", "test_downcast.rs")]


def test_harvested_suite_has_all_three_observables_with_runtime_then():
    suite = harvest_behaviour_suite([], cfg_cluster="stable", test_sources=_pairs())
    flags = defect_observable_flags(suite.specs)
    assert flags == {"identity": True, "alt-debug": True, "ensure-message": True}
    presence = defect_observable_presence(suite.specs)
    assert all(with_rt > 0 for _, with_rt in presence.values())
    hist = klass_histogram(suite.specs)
    assert hist["IDENTITY"] > 0 and hist["EXACT_FORMAT"] > 0
    assert set(runtime_then_counts(suite.specs)) >= {
        "ALT_DEBUG_STRING", "TYPE_IDENTITY", "DISPLAY_STRING"}
    assert render_counts(runtime_then_counts(suite.specs)).startswith("ALT_DEBUG_STRING ")


def test_negative_control_doc_fence_text_is_present_but_uncheckable():
    # the bake's own reading: the `downcast_ref::<io::Error>` doc-fence of
    # error.rs mentions no defect literal, and an ASSERTION spec has no then.
    src = ('/// ```\n/// let e = anyhow!("x");\n'
           '/// assert!(e.downcast_ref::<&str>().is_some());\n/// ```\npub fn f() {}\n')
    specs = harvest_doctest_behaviours(src, source_file="lib.rs")
    presence = defect_observable_presence(specs)
    label = next(iter(DEFECT_OBSERVABLES))
    assert presence[label] == (1, 0), "present in text, no runtime then"
    assert defect_observable_flags(specs)["identity"] is False


def test_script_imports_the_domain_patterns():
    script = (Path(__file__).parent.parent / "scripts" / "behaviour_suite_census.py").read_text()
    assert "from pipeline_core.domain.behaviour_census import" in script
    assert "re.compile(" not in script, "the patterns live in the domain, not in the script"
    assert "klass_histogram" in script
