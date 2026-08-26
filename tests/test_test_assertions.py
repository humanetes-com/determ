"""Pins for the test-assertion harvester (F10 tier 2), on the anyhow test
files the 98-tree failures pin — copied verbatim to tests/fixtures/behaviours/.

The claim under test is the plan's: the three defect observables the suite
lacked (`Error::msg` payload identity through `downcast::<&str>`, the
`EXPECTED_ALTDEBUG_*` `{:#?}` strings, the `Condition failed:` ensure message
through the `assert_err` helper) come out as OUTCOME specs with a RUNTIME
then. The negative control runs the OLD tiers (doc-fence + layout pins) over
the same files and gets 0 runtime then."""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline_core.domain.behaviour import (
    BehaviourClass,
    BehaviourProvenance,
    BehaviourSubKind,
    FenceDirective,
    OutcomeKind,
)
from pipeline_core.domain.doctest import (
    harvest_doctest_behaviours,
    harvest_static_assertions,
)
from pipeline_core.domain.layout_pins import harvest_layout_pin_behaviours
from pipeline_core.domain.test_assertions import (
    GIVEN_CAP,
    harvest_test_assertion_behaviours,
    outcome_kind_for,
    parse_string_literal,
    string_consts,
)

FIXTURES = Path(__file__).parent / "fixtures" / "behaviours"

EXPECTED_ALTDEBUG_F = (
    "Custom {\n"
    "    kind: PermissionDenied,\n"
    '    error: "oh no!",\n'
    "}"
)
EXPECTED_ALTDEBUG_H = (
    "Error {\n"
    '    context: "g failed",\n'
    "    source: Error {\n"
    '        context: "f failed",\n'
    "        source: Custom {\n"
    "            kind: PermissionDenied,\n"
    '            error: "oh no!",\n'
    "        },\n"
    "    },\n"
    "}"
)
TRAILER_STRINGS = (
    "Condition failed: `(|| 1)() == 2` (1 vs 2)",
    "Condition failed: `b\"hmm\"[1] == b'c'` (109 vs 99)",
    "Condition failed: `result? == 2` (1 vs 2)",
)


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _harvest(name: str):
    return harvest_test_assertion_behaviours(
        _fixture(name), source_file=f"tests/{name}", cfg_cluster="stable")


# --- literal reading ----------------------------------------------------------

def test_string_literal_escapes_and_continuation():
    assert parse_string_literal(r'"a\"b\\c\n\t"', 0) == ('a"b\\c\n\t', 13)
    cont = '"\\\nfoo\n\nbar\\\n    "'
    assert parse_string_literal(cont, 0)[0] == "foo\n\nbar"
    assert parse_string_literal('r#"raw \\n "quoted""#', 0)[0] == 'raw \\n "quoted"'
    assert parse_string_literal('"\\u{1F600}\\x41"', 0)[0] == "\U0001F600A"
    assert parse_string_literal("not a literal", 0) is None


def test_multiline_consts_of_test_fmt_evaluate_exactly():
    consts = string_consts(_fixture("test_fmt.rs"))
    assert consts["EXPECTED_ALTDEBUG_F"] == EXPECTED_ALTDEBUG_F
    assert consts["EXPECTED_ALTDEBUG_H"] == EXPECTED_ALTDEBUG_H
    assert consts["EXPECTED_DEBUG_G"] == "f failed\n\nCaused by:\n    oh no!"
    assert consts["EXPECTED_ALTDISPLAY_G"] == "f failed: oh no!"


def test_outcome_kind_from_expression_text():
    assert outcome_kind_for('format!("{:#?}", e)') is OutcomeKind.ALT_DEBUG_STRING
    assert outcome_kind_for('format!("{e:#?}")') is OutcomeKind.ALT_DEBUG_STRING
    assert outcome_kind_for('format!("{:?}", e)') is OutcomeKind.DEBUG_STRING
    assert outcome_kind_for("e.to_string()") is OutcomeKind.DISPLAY_STRING
    assert outcome_kind_for('format!("{}", e)') is OutcomeKind.DISPLAY_STRING
    assert outcome_kind_for('format!("{e}")') is OutcomeKind.DISPLAY_STRING
    assert outcome_kind_for("e.downcast::<&str>().unwrap()") is OutcomeKind.RETURN_RENDERED


# --- the plan's fixture: the three defect observables ---------------------------

def test_altdebug_consts_resolve_to_alt_debug_specs():
    specs = _harvest("test_fmt.rs")
    alt = [s for s in specs if s.then.kind is OutcomeKind.ALT_DEBUG_STRING]
    assert [s.when for s in alt] == [
        'format!("{:#?}", f().unwrap_err())',
        'format!("{:#?}", g().unwrap_err())',
        'format!("{:#?}", h().unwrap_err())',
    ]
    assert alt[0].then.value == EXPECTED_ALTDEBUG_F
    assert alt[2].then.value == EXPECTED_ALTDEBUG_H
    assert alt[0].source_ref == "tests/test_fmt.rs::test_altdebug"
    assert alt[0].behaviour_id == "testassert::tests/test_fmt.rs::test_altdebug::0"
    assert alt[0].klass is BehaviourClass.EXACT_FORMAT
    assert alt[0].subkind is BehaviourSubKind.OUTCOME
    assert alt[0].provenance is BehaviourProvenance.HARVESTED
    assert alt[0].fence is FenceDirective.RUN
    assert alt[0].is_positive_oracle
    # the sibling kinds in the same file
    kinds = {s.then.kind for s in specs}
    assert kinds == {OutcomeKind.ALT_DEBUG_STRING, OutcomeKind.DEBUG_STRING,
                     OutcomeKind.DISPLAY_STRING}
    assert next(s for s in specs if s.source_ref.endswith("::test_display")).then.value \
        == "g failed"


def test_trailer_yields_the_condition_failed_display_strings_via_helper():
    specs = _harvest("test_ensure.rs")
    trailer = [s for s in specs if s.source_ref == "tests/test_ensure.rs::test_trailer"]
    values = [s.then.value for s in trailer]
    for expected in TRAILER_STRINGS:
        assert expected in values
    assert all(s.then.kind is OutcomeKind.DISPLAY_STRING for s in trailer)
    assert all(s.when == "test" for s in trailer), "when = the helper call's first argument"
    # the `err.is::<&str>() == false` inside `ensure!` is the ensure's CONDITION
    # (the test asserts the ensure FAILS): it must never surface as an identity pin
    assert not any(s.then.kind is OutcomeKind.TYPE_IDENTITY for s in specs)


def test_downcast_identity_ok_and_refused():
    specs = _harvest("test_downcast.rs")
    idents = [s for s in specs if s.then.kind is OutcomeKind.TYPE_IDENTITY]
    ok_str = [s for s in idents if s.then.value == "&str" and s.then.ok]
    assert any(s.when == "bail_literal().unwrap_err().downcast::<&str>()" for s in ok_str)
    assert all(s.klass is BehaviourClass.IDENTITY for s in idents)
    refused = [s for s in idents
               if s.source_ref.endswith("::test_unsuccessful_downcast")]
    assert {(s.then.value, s.then.ok) for s in refused} == {("&str", False)}
    assert {s.when for s in refused} == {
        "error.downcast_ref::<&str>()", "error.downcast_mut::<&str>()",
        "error.downcast::<&str>()"}
    # the rendered payload beside the identity pin
    rendered = next(s for s in specs
                    if s.when == "bail_literal().unwrap_err().downcast::<&str>().unwrap()")
    assert rendered.then.kind is OutcomeKind.RETURN_RENDERED and rendered.then.value == "oh no!"
    # a multi-line chain re-joins
    assert any(s.when == "bail_error().unwrap_err().downcast::<io::Error>().unwrap().to_string()"
               for s in specs)


def test_given_is_the_test_item_capped_and_ids_are_per_item():
    specs = _harvest("test_ensure.rs")
    assert all(len(s.given) <= GIVEN_CAP for s in specs)
    by_test = {}
    for s in specs:
        by_test.setdefault(s.source_ref, []).append(s.behaviour_id)
    for ref, ids in by_test.items():
        name = ref.rsplit("::", 1)[1]
        assert ids == [f"testassert::tests/test_ensure.rs::{name}::{i}" for i in range(len(ids))]
    trailer = next(s for s in specs if s.source_ref.endswith("::test_trailer"))
    assert trailer.given.startswith("#[test]\nfn test_trailer()")
    assert "#[test]" not in trailer.given[10:], "the next item's attribute is not part of this one"


def test_generated_test_siblings_are_refused_by_name():
    src = _fixture("test_fmt.rs")
    assert harvest_test_assertion_behaviours(
        src, source_file="src/fmt.gen_tests_3.rs", cfg_cluster="stable") == []


def test_is_inside_assert_with_negation():
    src = (
        "#[test]\nfn t() {\n"
        "    let err = anyhow!(\"x\");\n"
        "    assert!(err.is::<&str>());\n"
        "    assert!(!err.is::<String>());\n"
        "    assert!(err.downcast_ref::<&str>().is_some());\n"
        "    assert!(err.downcast_ref::<io::Error>().is_none());\n"
        "}\n")
    specs = harvest_test_assertion_behaviours(src, source_file="tests/t.rs")
    got = {(s.when, s.then.value, s.then.ok) for s in specs}
    assert got == {
        ("err.is::<&str>()", "&str", True),
        ("err.is::<String>()", "String", False),
        ("err.downcast_ref::<&str>()", "&str", True),
        ("err.downcast_ref::<io::Error>()", "io::Error", False),
    }


def test_assert_eq_needs_exactly_one_literal_side():
    src = (
        "#[test]\nfn t() {\n"
        "    assert_eq!(\"a\", \"a\");\n"
        "    assert_eq!(x.len(), y.len());\n"
        "    assert_eq!(x.to_string(), \"boom\", \"message {}\", 1);\n"
        "}\n")
    specs = harvest_test_assertion_behaviours(src, source_file="tests/t.rs")
    assert [(s.when, s.then.value) for s in specs] == [("x.to_string()", "boom")]


# --- negative control: the old tiers on the same files -----------------------------

@pytest.mark.parametrize("name", ["test_fmt.rs", "test_ensure.rs", "test_downcast.rs"])
def test_old_tiers_yield_no_runtime_then_on_these_files(name: str):
    src = _fixture(name)
    old = (harvest_doctest_behaviours(src, source_file=f"tests/{name}")
           + harvest_static_assertions(src, source_file=f"tests/{name}")
           + harvest_layout_pin_behaviours(src, source_file=f"tests/{name}"))
    assert sum(1 for s in old if s.has_runtime_then) == 0
    new = harvest_test_assertion_behaviours(src, source_file=f"tests/{name}")
    assert sum(1 for s in new if s.has_runtime_then) == len(new) > 0
