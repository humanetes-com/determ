"""Fn-body elision pins.

The pre-strip API-surface artefact promises "signatures only" and is read by
TASK 1 AFTER strip. When its elider was a line/brace scanner it only recognised
a body whose `{` shared the declaration's line, so every `where`-clause /
multi-line-generic fn leaked verbatim — selecting exactly the hard fns the
reconstruction is supposed to derive from tests. These pin both layers: the
span-based domain elider, and the text condenser other callers still use.
"""
from __future__ import annotations

from pipeline_core.application.reconstruct.source_view import (
    condense_rust_to_signatures,
)
from pipeline_core.domain.models import FnRecord
from pipeline_core.domain.rust_elide import elide_fn_bodies, outermost_spans

_SRC = """\
impl Thing {
    unsafe fn construct<E>(
        error: E,
    ) -> Self
    where
        E: Send + Sync + 'static,
    {
        let inner = Own::new(error).cast::<Impl>();
        Thing { inner }
    }
}
"""


def _rec(src: str, needle: str, close: str) -> FnRecord:
    start = src.index(needle) + len(needle)
    return FnRecord(fn_name="construct", body_byte_start=start,
                    body_byte_end=src.index(close, start))


def test_span_elision_removes_a_where_clause_body() -> None:
    rec = _rec(_SRC, "'static,\n    {", "\n    }")
    out = elide_fn_bodies(_SRC, [rec])
    assert "Own::new" not in out and "let inner" not in out
    assert "/* ... */" in out
    # The signature itself must survive intact — it IS the artefact's payload.
    assert "unsafe fn construct<E>(" in out and "E: Send + Sync + 'static," in out


def test_nested_fn_span_does_not_corrupt_the_outer_replacement() -> None:
    """A fn declared inside another fn's body has a contained span. Replacing
    the inner one first would leave the outer span pointing at shifted text."""
    src = "fn outer() {\n    fn inner() {\n        secret();\n    }\n    also();\n}\n"
    outer = FnRecord(fn_name="outer", body_byte_start=src.index("{") + 1,
                     body_byte_end=src.rindex("}"))
    inner_open = src.index("{", src.index("fn inner")) + 1
    inner = FnRecord(fn_name="inner", body_byte_start=inner_open,
                     body_byte_end=src.index("}", inner_open))
    assert len(outermost_spans([outer, inner])) == 1
    out = elide_fn_bodies(src, [outer, inner])
    assert "secret()" not in out and "also()" not in out
    assert out.count("/* ... */") == 1


def test_stale_span_is_skipped_never_truncates() -> None:
    src = "fn f() { body(); }\n"
    bogus = FnRecord(fn_name="f", body_byte_start=8, body_byte_end=10_000)
    assert elide_fn_bodies(src, [bogus]) == src


def test_condenser_elides_multiline_signature_bodies() -> None:
    """The regression that leaked anyhow's `construct` into the artefact."""
    out = condense_rust_to_signatures(_SRC, filename="t.rs")
    assert "Own::new" not in out, "multi-line signature must not leak its body"
    assert "/* ... */" in out
    assert "unsafe fn construct<E>(" in out


def test_condenser_still_elides_single_line_signature_bodies() -> None:
    src = "impl T {\n    pub fn f(&self, n: u32) -> u32 {\n        n * 2\n    }\n}\n"
    out = condense_rust_to_signatures(src, filename="t.rs")
    assert "n * 2" not in out and "/* ... */" in out


def test_condenser_keeps_bodyless_declarations() -> None:
    """A trait method or extern fn ends at `;` — there is no body to elide and
    the declaration must not be swallowed by the multi-line-signature path."""
    src = "trait T {\n    fn method(\n        &self,\n    ) -> u32;\n}\n"
    out = condense_rust_to_signatures(src, filename="t.rs")
    assert "fn method(" in out and "-> u32;" in out
