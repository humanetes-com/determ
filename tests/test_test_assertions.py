"""Pins for the test-assertion harvester, on the anyhow test
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
    asserting_helper_names,
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
    assert parse_string_literal('b"hi"', 0) == ("hi", 5)
    assert parse_string_literal("rfoo", 0) is None
    assert parse_string_literal('r#"no close', 0) is None
    assert parse_string_literal('"\\0\\r\\q"', 0) == ("\0\rq", 8)
    assert parse_string_literal('"\\u{no"', 0) is None
    assert parse_string_literal('"trail\\', 0) is None
    assert parse_string_literal('"no close', 0) is None
    cr_cont = '"a\\\r\n    b"'
    assert parse_string_literal(cr_cont, 0)[0] == "ab"


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
    assert outcome_kind_for('format!("{e}")') is OutcomeKind.DISPLAY_STRING
    assert outcome_kind_for('format!("{e:?}")') is OutcomeKind.DEBUG_STRING
    assert outcome_kind_for("{:#}") is OutcomeKind.DISPLAY_STRING


# --- parser edges the live fixtures never open --------------------------------

def test_string_consts_skip_opaque_non_str_and_nameless() -> None:
    src = (
        'let s = "const INSIDE: &str = \\"nope\\"";\n'
        "const;\n"
        "const FOO: i32 = 1;\n"
        'xconst SHADOW: &str = "no";\n'
        'const REAL: &\'static str = "yes";\n'
        'static ALSO: &str = "stat";\n'
        "const NOEQ: &str;\n"
    )
    consts = string_consts(src)
    assert consts == {"REAL": "yes", "ALSO": "stat"}
    assert "INSIDE" not in consts and "FOO" not in consts and "SHADOW" not in consts


def test_comments_chars_and_unterminated_string_do_not_crash_harvest() -> None:
    src = (
        "#[test]\nfn t() {\n"
        "    // assert_eq!(x, \"no\");\n"
        "    /* assert_eq!(y, \"no\"); */\n"
        "    let c = 'x';\n"
        "    let d = '\\n';\n"
        '    assert_eq!(e.to_string(), "ok value");\n'
        "}\n"
        "// eof comment with no newline"
    )
    specs = harvest_test_assertion_behaviours(src, source_file="tests/t.rs")
    assert [(s.when, s.then.value) for s in specs] == [("e.to_string()", "ok value")]
    unclosed = '#[test]\nfn t() { assert_eq!(x, "no close\n'
    assert harvest_test_assertion_behaviours(unclosed, source_file="tests/t.rs") == []
    unclosed_cmt = "#[test]\nfn t() { let x = 1; /* never"
    assert harvest_test_assertion_behaviours(unclosed_cmt, source_file="tests/t.rs") == []
    lifetime = "#[test]\nfn t() { let x: &'static str = \"a\"; }"
    assert harvest_test_assertion_behaviours(lifetime, source_file="tests/t.rs") == []


def test_assert_eq_unclosed_one_arg_and_ident_prefix_are_skipped() -> None:
    src = (
        "#[test]\nfn t() {\n"
        "    assert_eq!(x);\n"
        "    assert_eq!(x, ;\n"
        "    myassert_eq!(x, \"nope\");\n"
        '    assert_eq!(r#"raw value"#, e.to_string());\n'
        "}\n"
    )
    specs = harvest_test_assertion_behaviours(src, source_file="tests/t.rs")
    assert [(s.when, s.then.value) for s in specs] == [("e.to_string()", "raw value")]


def test_identity_receiver_walks_macros_qmark_and_paths_and_skips_ensure() -> None:
    src = (
        "#[test]\nfn t() {\n"
        '    assert!(anyhow!("x").is::<&str>());\n'
        "    assert!(e?.is::<String>());\n"
        "    assert!(crate::Error::msg(x).is::<io::Error>());\n"
        "    assert!(inner.payload.is::<u8>());\n"
        "    assert!(e.is::<  >());\n"
        "    assert!(e.is::<T);\n"
        "    assert!(e.is::<T>);\n"
        "    let _ = ensure!(err.is::<&str>());\n"
        "}\n"
    )
    specs = harvest_test_assertion_behaviours(src, source_file="tests/t.rs")
    got = {(s.when, s.then.value, s.then.ok) for s in specs}
    assert ("anyhow!(\"x\").is::<&str>()", "&str", True) in got
    assert ("e?.is::<String>()", "String", True) in got
    assert ("crate::Error::msg(x).is::<io::Error>()", "io::Error", True) in got
    assert ("inner.payload.is::<u8>()", "u8", True) in got
    src2 = (
        "#[test]\nfn t() {\n"
        "    assert!(foo::<T>().is::<U>());\n"
        "    assert!(e.is::<T>  ());\n"
        "}\n"
    )
    specs2 = harvest_test_assertion_behaviours(src2, source_file="tests/t.rs")
    got2 = {(s.when, s.then.value) for s in specs2}
    assert ("foo::<T>().is::<U>()", "U") in got2
    assert ("e.is::<T> ()", "T") in got2

    assert not any(s.then.value.strip() == "" for s in specs)
    assert not any("ensure" in s.when for s in specs)


def test_helper_calls_skip_paths_methods_and_need_two_args() -> None:
    src = (
        "fn assert_err(test: impl Fn(), msg: &str) { assert_eq!(msg, msg); }\n"
        "fn check(test: impl Fn(), msg: &str) { assert!(true); assert_eq!(1, 1); }\n"
        "#[test]\nfn t() {\n"
        '    assert_err(test, "boom boom");\n'
        '    check(test, "via helper");\n'
        '    x.assert_err(test, "method");\n'
        '    foo::assert_err(test, "path");\n'
        "    assert_err(test);\n"
        '    Some(test, "kw");\n'
        "}\n"
    )
    assert asserting_helper_names(src) == {"assert_err", "check"}
    specs = harvest_test_assertion_behaviours(src, source_file="tests/t.rs")
    values = {s.then.value for s in specs}
    assert "boom boom" in values and "via helper" in values
    assert "method" not in values and "path" not in values and "kw" not in values


def test_mismatched_closers_and_empty_asserting_helpers() -> None:
    src = '#[test]\nfn t() { assert_eq!(e.to_string(), "ok value"); ) }'
    specs = harvest_test_assertion_behaviours(src, source_file="tests/t.rs")
    assert [(s.when, s.then.value) for s in specs] == [("e.to_string()", "ok value")]
    assert asserting_helper_names("") == frozenset()
    assert asserting_helper_names("fn helper() { let x = 1; }") == frozenset()


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


def test_setup_is_the_slice_the_assertion_needs_and_ids_are_per_item():
    """The plan's claim: an example's setup is the bindings its `when` needs,
    not the test item. `test_trailer` holds eight independent `let test = …;
    assert_err(test, …)` cases; the one about `err.is::<&str>()` needs the
    `let err` binding and ITS `let test`, and none of the other seven."""
    specs = _harvest("test_ensure.rs")
    by_test = {}
    for s in specs:
        by_test.setdefault(s.source_ref, []).append(s.behaviour_id)
    for ref, ids in by_test.items():
        name = ref.rsplit("::", 1)[1]
        assert ids == [f"testassert::tests/test_ensure.rs::{name}::{i}" for i in range(len(ids))]
    trailer = [s for s in specs if s.source_ref.endswith("::test_trailer")]
    is_str = next(s for s in trailer if "err.is::<&str>() == false" in s.then.value)
    assert is_str.setup == ('let err = anyhow!("");\n'
                            "let test = || Ok(ensure!(err.is::<&str>() == false));")
    assert is_str.when == "test" and is_str.via == "assert_err"
    assert all("#[test]" not in s.setup for s in specs)
    # every assertion of the item used to carry the whole item: 8 copies
    assert sum(len(s.setup) for s in trailer) < len(_fixture("test_ensure.rs")) // 20
    # the result? case needs `result` as well as its own `test`
    q = next(s for s in trailer if "result? == 2" in s.then.value)
    assert q.setup == ("let result = Ok::<_, Error>(1);\n"
                       "let test = || Ok(ensure!(result? == 2));")
    # the asserting helper is a stored helper, not repeated per example
    assert "tests/test_ensure.rs::assert_err" in is_str.helper_keys



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
