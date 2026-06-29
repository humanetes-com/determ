"""Tests for pipeline_core.application.reconstruct.sibling_context."""
from __future__ import annotations

from pipeline_core.application.reconstruct.sibling_context import collect_sibling_filled_bodies

_SRC = """\
impl Foo {
    fn as_ref(&self) -> &Self {
        &self.inner
    }
}

impl Bar {
    fn as_ref(&self) -> &Self {
        &self.value
    }
}

impl Baz {
    fn as_ref(&self) -> &Self {
        unimplemented!()
    }
}
"""


def test_finds_sibling_excluding_target() -> None:
    result = collect_sibling_filled_bodies(_SRC, "as_ref", "impl Bar")
    # Foo's body is found; Bar (target) is excluded; Baz is a stub
    assert "&self.inner" in result
    assert "&self.value" not in result


def test_stub_bodies_excluded() -> None:
    result = collect_sibling_filled_bodies(_SRC, "as_ref", "impl Foo")
    # Baz's unimplemented!() stub must not appear
    assert "unimplemented!()" not in result


def test_no_siblings_returns_empty() -> None:
    src = "impl Lone { fn only(&self) {} }"
    result = collect_sibling_filled_bodies(src, "other_fn", "impl Lone")
    assert result == ""


def test_max_siblings_cap() -> None:
    blocks: list[str] = []
    for i in range(5):
        blocks.append(f"impl T{i} {{\n    fn shared(&self) -> i32 {{ {i} }}\n}}")
    src = "\n".join(blocks)
    result = collect_sibling_filled_bodies(src, "shared", "impl T99", max_siblings=2)
    assert result.count("fn shared") == 2


def test_body_truncated_at_max_chars() -> None:
    long_body = "x" * 900
    src = f"impl A {{\n    fn f(&self) -> &str {{ {long_body} }}\n}}"
    result = collect_sibling_filled_bodies(src, "f", "impl B", max_body_chars=100)
    assert "truncated" in result


def test_empty_source_returns_empty() -> None:
    assert collect_sibling_filled_bodies("", "f", "impl X") == ""
