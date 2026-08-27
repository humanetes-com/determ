"""Which harvested behaviours a generated test block pins — pure domain.

The defect these pin: on the 2026-08 anyhow trees, augmentation admitted
`assert!(format!("{:?}", err_impl).contains("impl debug test"))` while the
crate's own `tests/test_fmt.rs` pins the whole `{:#?}` rendering
(`EXPECTED_ALTDEBUG_F`). A `contains` on a fragment freezes nothing an
alternative implementation could violate; the exact rendering does. The
fixture below is COMPUTED from the live `tests/test_fmt.rs` literal, never
copied: the spec's `then` is what that file declares, read at test time.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline_core.domain.behaviour import (
    BehaviourSpec,
    BehaviourSubKind,
    ObservableOutcome,
    OutcomeKind,
)
from pipeline_core.domain.behaviour_pinning import (
    _char_literal_end,
    _literals_in,
    _read_string_literal,
    _string_opener_at,
    behaviours_pinned_by,
    is_pinnable,
    render_behaviour_list,
    render_behaviours_for_file,
    select_behaviours_for_file,
    weak_assertion_reason,
)

_TEST_FMT = Path("tmp/anyhow/tests/test_fmt.rs")


def _display(bid: str, when: str, value: str, exercises: frozenset[str] = frozenset()) -> BehaviourSpec:
    return BehaviourSpec(bid, "", when, then=ObservableOutcome.display(value),
                         exercises=exercises, subkind=BehaviourSubKind.OUTCOME)


def _identity(bid: str, type_text: str, ok: bool) -> SimpleNamespace:
    """A TYPE_IDENTITY spec by kind NAME — the module compares names, so it
    runs whether or not `OutcomeKind.TYPE_IDENTITY` has landed."""
    then = SimpleNamespace(kind=SimpleNamespace(name="TYPE_IDENTITY"), value=type_text, ok=ok)
    return SimpleNamespace(behaviour_id=bid, given="", when=f"e.is::<{type_text}>()",
                           then=then, exercises=frozenset())


def _fmt_literal(name: str) -> str:
    text = _TEST_FMT.read_text(encoding="utf-8")
    at = text.find(f"const {name}")
    assert at != -1, name
    lit = _read_string_literal(text, text.find('"', at))
    assert lit is not None
    return lit[0]


@pytest.mark.skipif(not _TEST_FMT.exists(), reason="live anyhow tests/test_fmt.rs not present")
def test_live_altdebug_literal_pins_exactly_and_contains_does_not() -> None:
    """The plan's fixture, computed: the `{:#?}` value `tests/test_fmt.rs`
    declares as EXPECTED_ALTDEBUG_F. A block asserting it through the same
    `const` + `assert_eq!` shape pins the behaviour; a `contains` on a
    fragment of it (the shape the live gen_tests use) does not."""
    value = _fmt_literal("EXPECTED_ALTDEBUG_F")
    assert value.startswith("Custom {") and "PermissionDenied" in value and "\n" in value
    spec = BehaviourSpec("altdebug_f", "", 'format!("{:#?}", f().unwrap_err())',
                         then=ObservableOutcome.debug(value), subkind=BehaviourSubKind.OUTCOME)
    text = _TEST_FMT.read_text(encoding="utf-8")
    const_decl = text[text.find("const EXPECTED_ALTDEBUG_F"):]
    const_decl = const_decl[:const_decl.find('";') + 2]
    exact = (const_decl + "\n#[test]\nfn test_altdebug() {\n"
             '    assert_eq!(EXPECTED_ALTDEBUG_F, format!("{:#?}", f().unwrap_err()));\n}\n')
    contains = ("#[test]\nfn test_error_impl_debug_fmt() {\n"
                '    assert!(format!("{:#?}", f().unwrap_err()).contains("PermissionDenied"));\n}\n')
    assert behaviours_pinned_by(exact, [spec]) == {"altdebug_f"}
    assert behaviours_pinned_by(contains, [spec]) == frozenset()
    assert weak_assertion_reason(exact) is None
    assert "contains(" in (weak_assertion_reason(contains) or "")


def test_inline_literal_and_bound_let_pin_the_display_string() -> None:
    spec = _display("d1", 'MathError::DivByZero.to_string()', "division by zero")
    inline = '#[test]\nfn t() { assert_eq!(MathError::DivByZero.to_string(), "division by zero"); }'
    bound = ('#[test]\nfn t() {\n    let expected = "division by zero";\n'
             '    assert_eq!(expected, checked_div(1, 0).unwrap_err().to_string());\n}')
    swapped = '#[test]\nfn t() { assert!(checked_div(1, 0).unwrap_err().to_string() == "division by zero"); }'
    assert behaviours_pinned_by(inline, [spec]) == {"d1"}
    assert behaviours_pinned_by(bound, [spec]) == {"d1"}
    assert behaviours_pinned_by(swapped, [spec]) == {"d1"}


def test_a_literal_used_as_an_input_is_not_a_pin() -> None:
    """`Error::msg("outer context")` displays as its input; a test that only
    passes the literal in and asserts `.is_ok()` has pinned nothing."""
    spec = _display("d2", 'Error::msg("outer context").to_string()', "outer context")
    block = '#[test]\nfn t() { assert!(Error::msg("outer context").is::<&str>()); }'
    assert behaviours_pinned_by(block, [spec]) == frozenset()


def test_short_values_and_assertion_specs_are_not_pinnable() -> None:
    short = _display("s", "x.to_string()", "3")
    assertion = BehaviourSpec("a", "", "assert!(x)")            # then=None
    assert not is_pinnable(short) and not is_pinnable(assertion)
    assert behaviours_pinned_by('#[test]\nfn t() { assert_eq!(x.to_string(), "3"); }', [short]) == frozenset()


def test_identity_pin_needs_the_same_type_and_polarity() -> None:
    pos = _identity("id_pos", "&str", True)
    neg = _identity("id_neg", "io::Error", False)
    assert behaviours_pinned_by('#[test]\nfn t() { assert!(Error::msg("x").is::<&str>()); }', [pos, neg]) == {"id_pos"}
    assert behaviours_pinned_by('#[test]\nfn t() { assert!(e.downcast_ref::< &str >().is_some()); }', [pos]) == {"id_pos"}
    assert behaviours_pinned_by('#[test]\nfn t() { assert!(!e.is::<&str>()); }', [pos]) == frozenset()
    assert behaviours_pinned_by('#[test]\nfn t() { assert!(e.downcast_ref::<std::io::Error>().is_none()); }', [neg]) == {"id_neg"}
    assert behaviours_pinned_by('#[test]\nfn t() { assert!(e.downcast::<io::Error>().is_err()); }', [neg]) == {"id_neg"}
    assert behaviours_pinned_by('#[test]\nfn t() { assert_eq!(e.is::<io::Error>(), false); }', [neg]) == {"id_neg"}
    assert behaviours_pinned_by('#[test]\nfn t() { assert!(e.is::<io::Error>()); }', [neg]) == frozenset()
    assert behaviours_pinned_by('#[test]\nfn t() { assert!(e.is::<fmt::Error>()); }', [neg]) == frozenset()


def test_weak_assertion_reason_names_the_shapes() -> None:
    assert weak_assertion_reason('#[test]\nfn t() { let v = f().unwrap(); }') == (
        "no exact-value assertion — only t: .unwrap() without an assert")
    reason = weak_assertion_reason(
        '#[test]\nfn a() { assert!(f().is_ok()); }\n#[test]\nfn b() { assert_eq!(g().is_some(), true); }')
    assert reason is not None and ".is_ok()" in reason and "assert_eq!(…, true)" in reason
    assert weak_assertion_reason('#[test]\nfn t() { assert_eq!(decode_tag("t4"), Some(4)); }') is None
    assert weak_assertion_reason('#[test]\nfn t() { assert!(matches!(e, MathError::DivByZero)); }') is None
    assert weak_assertion_reason('#[test]\nfn t() { assert!(matches!(e, Some(_))); }') is not None
    assert weak_assertion_reason('#[test]\n#[should_panic(expected = "boom")]\nfn t() { f(); }') is None
    assert weak_assertion_reason('#[test]\nfn t() { assert_display(&e, "hello world"); }') is None
    assert weak_assertion_reason("fn helper() {}") == "no #[test] item"


def test_select_by_exercises_segment_or_pub_item_word_and_skip_pinned() -> None:
    by_fn = _display("f", "x", "value one here", exercises=frozenset({"src/lib.rs::checked_div"}))
    by_word = _display("w", "MathError::DivByZero.to_string()", "division by zero")
    other = _display("o", "Counter::new(1).get()", "unrelated value")
    pinned_already = _display("p", "checked_div(2, 1)", "already pinned!")
    chosen = select_behaviours_for_file(
        [by_fn, by_word, other, pinned_already],
        file_fn_names={"checked_div", "add"}, file_pub_items={"MathError", "checked_div", "add"},
        already_pinned={"p"})
    assert [s.behaviour_id for s in chosen] == ["f", "w"]


def test_render_lists_exact_then_and_caps_with_a_count() -> None:
    multi = BehaviourSpec("m", "let e = f();", 'format!("{:#?}", e)',
                          then=ObservableOutcome.debug("Custom {\n    kind: X,\n}"),
                          subkind=BehaviourSubKind.OUTCOME)
    single = _display("s", "e.to_string()", 'say "hi" now')
    ident = _identity("i", "&str", True)
    text = render_behaviour_list([multi, single, ident], rel_path="src/x.rs")
    assert "Harvested behaviours of `src/x.rs`" in text
    assert "  ```\n  Custom {\n      kind: X,\n  }\n  ```" in text
    assert 'then: "say \\"hi\\" now"' in text
    assert "`.is::<&str>()` is true" in text
    capped = render_behaviour_list([single, multi, ident], cap_chars=len(text) // 2)
    assert "more behaviour(s) not shown" in capped
    assert render_behaviour_list([]) == ""
    assert "division" in render_behaviours_for_file(
        [_display("d", "MathError::DivByZero.to_string()", "division by zero")],
        file_fn_names=set(), file_pub_items={"MathError"}, already_pinned=set())


def test_kind_names_are_read_not_identities() -> None:
    """A spec whose kind is `ALT_DEBUG_STRING` by NAME (the vocabulary another
    change adds) pins like any string kind."""
    then = SimpleNamespace(kind=SimpleNamespace(name="ALT_DEBUG_STRING"), value="Custom {\n    a: 1,\n}", ok=False)
    spec = SimpleNamespace(behaviour_id="alt", given="", when="", then=then, exercises=frozenset())
    block = '#[test]\nfn t() { assert_eq!(format!("{:#?}", v), "Custom {\\n    a: 1,\\n}"); }'
    assert behaviours_pinned_by(block, [spec]) == {"alt"}
    assert OutcomeKind.DEBUG_STRING.name == "DEBUG_STRING"


# --- lexer / binder / weak-shape edges the happy-path tests never open ----------

def test_string_literal_raw_byte_escapes_and_unterminated() -> None:
    """Every opener and escape `_read_string_literal` claims, plus the
    refusals: an unclosed raw, a `\\` at EOF, a `b`/`r` that is not a
    string, and a hash-raw that never sees its close."""
    assert _read_string_literal('b"hi"', 0) == ("hi", 5)
    assert _read_string_literal('r"raw \\n"', 0) == ("raw \\n", 9)
    assert _read_string_literal('r#"a "quote""#', 0) == ('a "quote"', 14)
    assert _read_string_literal('br#"byte"#', 0) == ("byte", 10)
    assert _read_string_literal('r#"no close', 0) is None
    assert _read_string_literal("rfoo", 0) is None
    assert _read_string_literal("bx", 0) is None
    assert _read_string_literal('"\\x41\\u{41}\\0\\r"', 0) == ("AA\0\r", 16)
    # bad hex / unclosed / bad unicode fall through to the single-char map
    assert _read_string_literal('"\\xGG"', 0)[0] == "xGG"
    assert _read_string_literal('"\\u{zz}"', 0)[0] == "u{zz}"
    assert _read_string_literal('"\\u{no"', 0)[0] == "u{no"
    assert _read_string_literal('"\\q"', 0) == ("q", 4)
    assert _read_string_literal('"trail\\', 0) is None
    assert _read_string_literal('"no close', 0) is None
    cr_cont = '"a\\\r\n    b"'
    assert _read_string_literal(cr_cont, 0) == ("ab", len(cr_cont))
    # a `b"` after an identifier is not a string opener
    assert not _string_opener_at('xb"no"', 1)
    assert _string_opener_at('b"yes"', 0)
    assert _string_opener_at('r#"x"#', 0)


def test_char_literal_end_skips_escapes_and_refuses_lifetimes() -> None:
    assert _char_literal_end("'a'", 0) == 3
    assert _char_literal_end("'\\n'", 0) == 4
    assert _char_literal_end("'\\n", 0) is None          # unclosed escape
    assert _char_literal_end("'", 0) is None
    assert _char_literal_end("'static", 0) is None      # lifetime, not a char


def test_comments_and_char_literals_do_not_invent_pins() -> None:
    spec = _display("d", "e.to_string()", "division by zero")
    commented = (
        '#[test]\nfn t() {\n'
        '    // assert_eq!(e.to_string(), "division by zero");\n'
        '    /* assert_eq!(e.to_string(), "division by zero"); */\n'
        "    let c = 'x';\n"
        '    let _ = "not asserted";\n'
        '}\n')
    assert behaviours_pinned_by(commented, [spec]) == frozenset()
    # `//` with no newline, and an unclosed `/*`, still parse
    eof_comment = '#[test]\nfn t() { let x = 1; }// no newline'
    assert behaviours_pinned_by(eof_comment, [spec]) == frozenset()
    unclosed = '#[test]\nfn t() { let x = 1; /* never closes'
    assert behaviours_pinned_by(unclosed, [spec]) == frozenset()


def test_bound_let_mut_wrappers_and_static_const_pin() -> None:
    spec = _display("d", "e.to_string()", "division by zero")
    mut_let = ('#[test]\nfn t() {\n    let mut expected = "division by zero";\n'
               '    assert_eq!(expected, e.to_string());\n}')
    wrapped = ('#[test]\nfn t() {\n'
               '    assert_eq!(e.to_string(), String::from("division by zero"));\n'
               '    assert_eq!(format!("division by zero"), e.to_string());\n'
               '    assert_eq!(("division by zero").as_str(), e.to_string());\n}')
    st = ('static EXPECTED: &str = "division by zero";\n'
          '#[test]\nfn t() { assert_eq!(e.to_string(), EXPECTED.to_owned()); }')
    assert behaviours_pinned_by(mut_let, [spec]) == {"d"}
    assert behaviours_pinned_by(wrapped, [spec]) == {"d"}
    assert behaviours_pinned_by(st, [spec]) == {"d"}
    # a `let` whose rhs is not a literal binds nothing; a name with no rhs
    # does not pick up a sibling's literal
    not_lit = '#[test]\nfn t() { let expected = e.to_string(); assert_eq!(expected, x); }'
    no_eq = '#[test]\nfn t() { let expected; assert_eq!(expected, e.to_string()); }'
    assert behaviours_pinned_by(not_lit, [spec]) == frozenset()
    assert behaviours_pinned_by(no_eq, [spec]) == frozenset()


def test_panic_message_pins_via_should_panic_expected() -> None:
    spec = BehaviourSpec("p", "", "f()", then=ObservableOutcome.panic("boom boom"),
                         subkind=BehaviourSubKind.OUTCOME)
    pinned = '#[test]\n#[should_panic(expected = "boom boom")]\nfn t() { f(); }'
    bare = '#[test]\n#[should_panic]\nfn t() { f(); }'
    no_quote = '#[test]\n#[should_panic(expected = boom)]\nfn t() { f(); }'
    assert is_pinnable(spec)
    assert behaviours_pinned_by(pinned, [spec]) == {"p"}
    assert behaviours_pinned_by(bare, [spec]) == frozenset()
    assert behaviours_pinned_by(no_quote, [spec]) == frozenset()
    assert weak_assertion_reason(pinned) is None
    assert weak_assertion_reason(bare) is not None


def test_identity_unclosed_turbofish_and_matches_none_are_not_silent() -> None:
    pos = _identity("id_pos", "&str", True)
    neg = _identity("id_neg", "&str", False)
    unclosed = '#[test]\nfn t() { assert!(e.is::<&str); }'
    assert behaviours_pinned_by(unclosed, [pos]) == frozenset()
    none_pat = '#[test]\nfn t() { assert!(matches!(e.downcast_ref::<&str>(), None)); }'
    err_pat = '#[test]\nfn t() { assert!(matches!(e.downcast::<&str>(), Err(_))); }'
    assert behaviours_pinned_by(none_pat, [neg]) == {"id_neg"}
    assert behaviours_pinned_by(err_pat, [neg]) == {"id_neg"}
    # `None`/`Err` as a suffix of an identifier is not a negative pattern
    ident = '#[test]\nfn t() { assert!(e.downcast_ref::<&str>().is_none_or(|_| false)); }'
    assert behaviours_pinned_by(ident, [neg]) == frozenset()


def test_empty_and_unmatched_blocks_pin_nothing() -> None:
    spec = _display("d", "e.to_string()", "division by zero")
    assert behaviours_pinned_by("", [spec]) == frozenset()
    assert behaviours_pinned_by("   \n", [spec]) == frozenset()
    assert behaviours_pinned_by("#[test]\nfn t() {", [spec]) == frozenset()
    # `#[test]` then a comment then the fn — the item is still found
    spaced = '#[test]\n// setup\nfn t() { assert_eq!(e.to_string(), "division by zero"); }'
    assert behaviours_pinned_by(spaced, [spec]) == {"d"}


def test_weak_reason_covers_identity_numeric_char_and_helper_without_literal() -> None:
    assert weak_assertion_reason(
        '#[test]\nfn t() { assert!(e.is::<&str>()); }') is None
    assert weak_assertion_reason(
        '#[test]\nfn t() { assert_eq!(n, 42); }') is None
    assert weak_assertion_reason(
        "#[test]\nfn t() { assert_eq!(c, 'x'); }") is None
    assert weak_assertion_reason(
        '#[test]\nfn t() { assert_eq!(n, -3); }') is None
    # one-arg assert_eq, a helper with no literal, unmatched matches!(
    one = weak_assertion_reason('#[test]\nfn t() { assert_eq!(x); }')
    assert one is not None and "assert_eq!(…)" in one
    helper = weak_assertion_reason('#[test]\nfn t() { assert_display(&e, x); }')
    assert helper is not None and "assert_display(…)" in helper
    unclosed = weak_assertion_reason('#[test]\nfn t() { assert!(matches!(e, Some(_)); }')
    assert unclosed is not None
    empty_file = weak_assertion_reason("fn helper() {}")
    assert empty_file == "no #[test] item"


def test_select_without_pub_items_needs_an_exercises_hit() -> None:
    by_fn = _display("f", "x", "value one here", exercises=frozenset({"src/lib.rs::add"}))
    by_word = _display("w", "MathError::DivByZero.to_string()", "division by zero")
    chosen = select_behaviours_for_file(
        [by_fn, by_word], file_fn_names={"add"}, file_pub_items=set(),
        already_pinned=set())
    assert [s.behaviour_id for s in chosen] == ["f"]
    missed = select_behaviours_for_file(
        [by_word], file_fn_names=set(), file_pub_items=set(), already_pinned=set())
    assert missed == ()


def test_identity_empty_value_is_not_pinnable_and_false_polarity_renders() -> None:
    empty = SimpleNamespace(
        behaviour_id="e", given="", when="e.is::<T>()",
        then=SimpleNamespace(kind=SimpleNamespace(name="TYPE_IDENTITY"), value="  ", ok=True),
        exercises=frozenset())
    assert not is_pinnable(empty)
    ident = _identity("i", "io::Error", False)
    text = render_behaviour_list([ident])
    assert "`.is::<io::Error>()` is false" in text
    assert "is None" in text
    assert "Harvested behaviours NOT yet pinned" in text   # no rel_path
    assert render_behaviour_list([]) == ""


def test_literals_in_walks_raw_char_and_unclosed() -> None:
    """The token walk `_idents_in` shares: strings are collected, a char is
    skipped, an unclosed raw does not hang."""
    assert _literals_in('before "x" r#"y"# \'z\' r#"no') == ["x", "y"]
    assert _literals_in("") == []


def test_helper_literal_missing_test_item_and_exact_shapes_weak_reason_missed() -> None:
    spec = _display("d", "e.to_string()", "division by zero")
    helper = '#[test]\nfn t() { assert_display(&e, "division by zero"); }'
    assert behaviours_pinned_by(helper, [spec]) == {"d"}
    assert behaviours_pinned_by("fn helper() { let x = 1; }", [spec]) == frozenset()
    assert weak_assertion_reason('#[test]\nfn t() { assert_eq!(42, true); }') is None
    assert weak_assertion_reason("#[test]\nfn t() { assert!(a == b); }") is None
    assert weak_assertion_reason("#[test]\nfn t() { assert; }") is not None
    pos = _identity("id_pos", "&str", True)
    none_ident = '#[test]\nfn t() { assert!(matches!(e.downcast_ref::<&str>(), NoneX)); }'
    assert behaviours_pinned_by(none_ident, [pos]) == {"id_pos"}
    assert behaviours_pinned_by(none_ident, [_identity("id_neg2", "&str", False)]) == frozenset()
    assert weak_assertion_reason('#[test]\nfn t() { assert!(matches!(e); }') is not None
    assert weak_assertion_reason('#[test]\nfn t() { assert!(matches!(e)); }') is not None
    quoted = _display("q", 'MathError::from("x").to_string()', "division by zero")
    chosen = select_behaviours_for_file(
        [quoted], file_fn_names=set(), file_pub_items={"MathError"}, already_pinned=set())
    assert [s.behaviour_id for s in chosen] == ["q"]
    empty_name = '#[test]\nfn t() { let mut = "division by zero"; assert_eq!(x, y); }'
    assert behaviours_pinned_by(empty_name, [spec]) == frozenset()
