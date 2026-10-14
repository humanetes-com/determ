"""A format placeholder is read by the `std::fmt` grammar, inside string
literals only.

Run `1430425-193a8f28` (2026-10-14, cluster `stable`): two tests failed on a
type query (`assert!(e.is::<T>())`) whose binding was built from a literal
with escaped braces. `"{}" in text` matched the `{}` inside `{{}}`, so the
observable read Display, the tests were attributed to the Display formatter
and their producer escalation was withheld. The plan's sentences: `{{` and
`}}` are literal braces; braces in code and placeholders in comments name
nothing; the spec's type decides the trait.

Negative control: the substring reading the rule replaced, on the same text.
"""
from __future__ import annotations

from pipeline_core.domain.failing_observable import observable_source
from pipeline_core.domain.format_traits import format_traits_used, placeholder_traits

_SRC = '''use widget::make;

#[test]
fn query_after_escaped_literal() {
    let w = make("total: {{}} units");
    assert_eq!(w.label(), "total: {} units");
    assert!(w.is::<&str>());
}
'''


def test_escaped_braces_are_not_a_placeholder() -> None:
    assert format_traits_used('"total: {{}} units"') == frozenset()
    assert format_traits_used('make("a {{}} b {{}}")') == frozenset()
    # Negative control: the substring rule this replaced.
    assert "{}" in '"total: {{}} units"'


def test_the_type_query_observable_reads_no_trait() -> None:
    evidence = ("thread 'query_after_escaped_literal' panicked at "
                "tests/widget.rs:7:5:\nassertion failed: w.is::<&str>()")
    obs = observable_source(_SRC, "query_after_escaped_literal", evidence,
                            "tests/widget.rs")
    assert obs is not None and 'make("total: {{}} units")' in obs
    assert format_traits_used(obs) == frozenset()


def test_placeholders_name_their_trait() -> None:
    assert placeholder_traits("{}") == {"Display"}
    assert placeholder_traits("{0} and {name}") == {"Display"}
    assert placeholder_traits("{:>8.3}") == {"Display"}
    assert placeholder_traits("{:?}") == {"Debug"}
    assert placeholder_traits("{x:#?}") == {"Debug"}
    assert placeholder_traits("{:x?}") == {"Debug"}
    assert placeholder_traits("{:#x}") == frozenset()
    assert placeholder_traits("{{:?}}") == frozenset()
    assert placeholder_traits("{ a: 1 }") == frozenset()


def test_code_and_comments_are_not_format_strings() -> None:
    assert format_traits_used("fn f() {}") == frozenset()
    assert format_traits_used('// prints "{:?}" and x.to_string()') == frozenset()
    assert format_traits_used("v.to_string()") == {"Display"}
    assert format_traits_used('format!(r#"{:?}"#, v)') == {"Debug"}
