"""The assertion probe: a failing `assert!(s.contains(x))` prints
`assertion failed: s.contains(x)` and nothing else — the value of `s` is
observed by nobody. Run `3003-93e9a9bf` (2026-09-02) carried that panic line
to six prompts while the string under test was `"backtrace test"` in every
one of them. The probe rewrites the test's own assertions into prints of
their operands; these tests pin the rewrite on the recorded assertion
shapes and keep the pre-probe evidence as the negative control.
"""
from __future__ import annotations

from pipeline_core.domain.assert_probe import (
    Observation,
    operands_of,
    parse_probe_output,
    render_observations,
    rewrite_tests_for_probe,
    without_probe_points,
)

# The recorded shape: env vars, a constructor, `format!("{:?}")`, then
# three one-argument asserts on the string.
SIMPLE = '''#[test]
fn probe_simple() {
    std::env::set_var("RUST_BACKTRACE", "1");

    let err = Error::msg("probe text");
    let s = format!("{:?}", err);

    assert!(s.starts_with("probe text"));
    assert!(s.contains("\\n\\n"));
    assert!(s.contains("Stack backtrace") || s.contains("stack backtrace"));
}

#[test]
fn untouched() {
    assert_eq!(1 + 1, 2);
}
'''


def test_one_argument_asserts_print_the_value_under_test() -> None:
    text, points = rewrite_tests_for_probe(SIMPLE, ["probe_simple"])
    assert [p.operands for p in points] == [("s",), ("s",), ("s",)]
    assert [p.line for p in points] == [8, 9, 10]
    assert "assert!(s.starts_with" not in text
    assert '<<PROBE {}:{}>> {} = {:?}' in text
    assert 'r#"probe_simple"#, 8, r#"s"#, &(s)' in text
    # A test not named is byte-identical.
    assert "fn untouched() {\n    assert_eq!(1 + 1, 2);\n}" in text


def test_the_rewrite_keeps_every_line_number() -> None:
    multi = '''#[test]
fn spans() {
    let v = vec![1, 2];
    assert_eq!(
        v.len(),
        2,
    );
    let after = 1;
    assert!(after == 1);
}
'''
    text, points = rewrite_tests_for_probe(multi, ["spans"])
    assert [p.line for p in points] == [4, 9]
    assert text.count("\n") == multi.count("\n")
    assert text.splitlines()[8].lstrip().startswith("{ eprintln!")


def test_a_blamed_multiline_assert_is_restored_span_for_span() -> None:
    """The blamed-line retry must put the original assertion back, not
    just its first line: restoring only `assert_eq!(` leaves blanks and a
    leftover semicolon, which does not compile."""
    multi = '''#[test]
fn spans() {
    let v = vec![1, 2];
    assert_eq!(
        v.len(),
        2,
    );
    let after = 1;
    assert!(after == 1);
}
'''
    rewritten, points = rewrite_tests_for_probe(multi, ["spans"])
    assert points[0].span_lines == 4
    # Negative control: first-line-only restore is not the original span.
    src_lines = multi.splitlines()
    new_lines = rewritten.splitlines()
    first_only = list(new_lines)
    first_only[points[0].line - 1] = src_lines[points[0].line - 1]
    assert "assert_eq!(" in "\n".join(first_only)
    assert first_only[points[0].line] != src_lines[points[0].line]
    restored, kept = without_probe_points(
        multi, ["spans"], {(points[0].test, points[0].line)})
    out_lines = restored.splitlines()
    start = points[0].line - 1
    assert src_lines[start:start + 4] == out_lines[start:start + 4]
    assert "assert!(after" not in restored
    assert kept == (points[1],)


def test_operand_rules_on_the_recorded_and_common_shapes() -> None:
    assert operands_of("assert!", 's.contains("Stack backtrace") || s.contains("stack backtrace")') == ("s",)
    assert operands_of("assert!", 'a == b && !v.is_empty()') == ("a", "b", "v")
    assert operands_of("assert_eq!", 'format!("{:?}", err), "hello"') == ('format!("{:?}", err)',)
    assert operands_of("assert_eq!", "left, right, \"message {}\", 1") == ("left", "right")
    assert operands_of("assert!", "(x > 1)") == ("x",)
    assert operands_of("assert!", "matches!(x, Some(_))") == ("matches!(x, Some(_))",)
    assert operands_of("assert!", "err.downcast_ref::<E>().is_some()") == ("err.downcast_ref::<E>()",)
    assert operands_of("assert!", "") == ()


def test_literals_inside_strings_do_not_confuse_the_scanner() -> None:
    src = '''#[test]
fn tricky() {
    let s = String::from("a ) b , c");
    assert!(s.contains(") , "), "msg with (parens)");
    let c = ')';
    assert_eq!(c, ')');
}
'''
    text, points = rewrite_tests_for_probe(src, ["tricky"])
    assert [p.operands for p in points] == [("s",), ("c",)]
    assert "msg with (parens)" not in text.split("<<PROBE")[1].split("}")[0]


def test_parse_and_render_observations() -> None:
    out = (
        "running 1 test\n"
        '<<PROBE probe_simple:8>> s = "probe text"\n'
        '<<PROBE probe_simple:9>> s = "probe text"\n'
        "noise line\n"
        "<<PROBE other:3>> v = [1, 2]\n"
        "test probe_simple ... ok\n"
    )
    obs = parse_probe_output(out)
    assert obs["probe_simple"] == [
        Observation("probe_simple", 8, "s", '"probe text"'),
        Observation("probe_simple", 9, "s", '"probe text"'),
    ]
    assert obs["other"][0].value == "[1, 2]"
    assert render_observations(obs["probe_simple"]).splitlines() == [
        'observed at line 8: s = "probe text"',
        'observed at line 9: s = "probe text"',
    ]


def test_long_values_are_cut() -> None:
    out = "<<PROBE t:1>> s = " + "x" * 700
    obs = parse_probe_output(out, value_cap=100)
    assert obs["t"][0].value.endswith(" …") and len(obs["t"][0].value) < 110


def test_negative_control_the_panic_line_alone_carries_no_value() -> None:
    """What the pipeline had before the probe: libtest's own message for a
    one-argument assert names the expression and never its value."""
    panic = "assertion failed: s.contains(\"\\n\\n\")"
    assert "probe text" not in panic and " = " not in panic


# --- lexis and edge cases the live file did not exercise ----------------------

def test_comments_raw_byte_and_char_literals_are_skipped_not_scanned() -> None:
    src = '''#[test]
fn lexis() {
    // assert!(never(seen)) in a line comment
    /* assert_eq!(a, b) in a block comment */
    let raw = r#"a ) " b"#;
    let bytes = b"x)y";
    let ch = '\\n';
    let other = ')';
    let _life: &'static str = "s";
    assert!(raw.len() > 1 && bytes.len() > 1 && ch != other);
}
'''
    text, points = rewrite_tests_for_probe(src, ["lexis"])
    assert len(points) == 1
    assert points[0].operands == ("raw.len()", "bytes.len()", "ch", "other")
    assert "never(seen)" in text and "in a block comment" in text


def test_bare_identifier_and_spaced_macro_call() -> None:
    src = '''#[test]
fn odd() {
    let assert_ready = 1;
    assert! (assert_ready == 1);
}
'''
    text, points = rewrite_tests_for_probe(src, ["odd"])
    # `assert_ready` is an identifier, `assert! (` with a space is a call.
    assert [p.operands for p in points] == [("assert_ready",)]
    assert "let assert_ready = 1;" in text


def test_an_unterminated_assertion_leaves_the_test_untouched() -> None:
    src = '''#[test]
fn broken() {
    assert!(unbalanced == 1;
}
'''
    text, points = rewrite_tests_for_probe(src, ["broken"])
    # The fn body cannot be delimited, so nothing is rewritten — a test that
    # does not parse cannot be probed and must not be mangled.
    assert points == () and text == src


def test_a_missing_or_prefixed_test_name_locates_nothing() -> None:
    src = '''#[test]
fn xprobe_simple() {
    assert!(false);
}
'''
    text, points = rewrite_tests_for_probe(src, ["probe_simple", "absent"])
    assert points == () and text == src


def test_operand_rules_edge_cases() -> None:
    assert operands_of("assert!", "true") == ()
    assert operands_of("assert!", "-1.5 == x") == ("x",)
    assert operands_of("assert!", "helper(v)") == ("helper(v)",)
    assert operands_of("assert!", ".bad(v)") == (".bad(v)",)
    assert operands_of("assert!", "(a, b)") == ("a, b",)
    # A comparison side is printed as written: the length is the value.
    assert operands_of("assert!", "v.len() > 1 || (w.len() < 2)") == ("v.len()", "w.len()")


def test_raw_string_wrapper_escapes_embedded_hashes() -> None:
    src = '''#[test]
fn hashy() {
    let s = String::new();
    assert!(s.contains(r#"q"#));
}
'''
    text, points = rewrite_tests_for_probe(src, ["hashy"])
    assert points[0].operands == ("s",)
    assert 'r##"' in text or 'r#"s"#' in text


def test_probe_output_parser_ignores_malformed_lines() -> None:
    out = (
        "<<PROBE nohead\n"
        "<<PROBE t:notanumber>> s = 1\n"
        "<<PROBE t:3>> no separator here\n"
        "<<PROBE t:4>> s = 4\n"
    )
    obs = parse_probe_output(out)
    assert list(obs) == ["t"] and obs["t"][0].line == 4


def test_remaining_lexis_and_locator_branches() -> None:
    from pipeline_core.domain.assert_probe import _fn_body_span, _skip_literal
    # An unterminated string runs to the end; a bare `r` is not a raw string.
    assert _skip_literal('"open', 0) == 5
    assert _skip_literal("r + 1", 0) == 0
    # A raw-string wrapper picks enough hashes when the text holds `"#`.
    src = '''#[test]
fn hashes() {
    let s = String::new();
    assert_eq!(s, concat!("\\"", "#"));
}
'''
    text, points = rewrite_tests_for_probe(src, ["hashes"])
    assert points and points[0].operands[1].startswith("concat!") and 'r##"' in text
    # A same-named prefix fn before the real one, a fn with no body brace,
    # and a fn whose parameter list never closes.
    assert _fn_body_span("fn xodd() { 1 }\nfn odd() { 2 }", "odd") == (26, 29)
    assert _fn_body_span("fn odd();", "odd") is None
    assert _fn_body_span("fn odd( { }", "odd") is None
    # `assert!` not followed by `(` is copied; a literal-only condition keeps
    # the original assertion; a negative number is a literal.
    src2 = '''#[test]
fn shapes() {
    let n = 1;
    assert!;
    assert!(true);
    assert!(n == -1);
}
'''
    text2, points2 = rewrite_tests_for_probe(src2, ["shapes"])
    assert "assert!;" in text2 and "assert!(true);" in text2
    assert [p.operands for p in points2] == [("n",)]
    # A receiver-less call and a trailing group that is not the last token.
    assert operands_of("assert!", "(a)(b)") == ("(a)(b)",)
    assert operands_of("assert!", "f(x) + 1") == ("f(x) + 1",)
