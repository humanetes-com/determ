"""Fill-replay characterisation tests (CODE_ARCHITECTURE.md §8 R4.3).

The per-impl fill engine (`_phase_c_fill_impl_blocks` / `_process_one_stub`) is closure-heavy and
interleaves an LLM call, a cargo check, and a retrospective loop — not unit-testable end-to-end
without reconstructing its whole enclosing scope. But its *deterministic core* IS testable: given
a canned model response in the `<FN name="..." block="...">body</FN>` protocol, the engine parses
it with `extract_fn_bodies` and applies each body with the container-aware splicer
`_splice_fn_with_where`. These tests pin that parse→splice transform with real inputs, so the
engine can be refactored (R3) against a net without a live model or cargo.

If one of these breaks during an engine refactor, the model-response → filled-file behaviour
changed — which is exactly what must NOT change silently. (Free-fn fills use a different `sig=`
protocol + `_splice_fn_body`, covered by `test_splice_fn_body_allow_nonstub` in the smoke suite.)
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pipeline

CANNED_IMPL_METHOD = '<FN name="kind" block="impl Foo">self.tag</FN>'
CANNED_WITH_WHERE = '<FN name="read" block="impl Mut">where T: Sized\n    self.inner</FN>'
CANNED_TWO_MEMBERS = (
    '<FN name="a" block="impl Pair">1</FN>\n'
    '<FN name="b" block="impl Pair">2</FN>'
)


def _splice(p: Path, rec: dict) -> bool:
    ok, _reason, _delta = pipeline._splice_fn_with_where(
        p, rec["block"], rec["fn_name"], rec["body"], rec["where_clause"]
    )
    return ok


def test_parse_canned_response() -> None:
    """The parser yields one record per <FN> block, in order, with the optional leading
    `where` clause surfaced separately from the body."""
    recs = pipeline.extract_fn_bodies(CANNED_TWO_MEMBERS)
    assert [r["fn_name"] for r in recs] == ["a", "b"]
    assert [r["body"] for r in recs] == ["1", "2"]

    (w,) = pipeline.extract_fn_bodies(CANNED_WITH_WHERE)
    assert w["fn_name"] == "read"
    assert w["where_clause"] == "where T: Sized"
    assert w["body"] == "self.inner"   # where-clause stripped off the body


def test_fill_replay_impl_method() -> None:
    """Canned response → parse → container-aware splice fills the `kind` stub inside `impl Foo`;
    the unimplemented marker is gone and the signature/structure are preserved."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "lib.rs"
        p.write_text(
            "impl Foo {\n    fn kind(&self) -> Tag {\n        unimplemented!()\n    }\n}\n",
            encoding="utf-8",
        )
        (rec,) = pipeline.extract_fn_bodies(CANNED_IMPL_METHOD)
        assert _splice(p, rec)
        out = p.read_text(encoding="utf-8")
        assert "self.tag" in out and "unimplemented!()" not in out
        assert "impl Foo" in out and "fn kind(&self) -> Tag" in out


def test_fill_replay_with_where_clause() -> None:
    """A fill that tightens bounds: the leading `where T: Sized` is applied to the signature and
    the remaining text becomes the body."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "lib.rs"
        p.write_text(
            "impl Mut {\n    fn read(self) -> T {\n        unimplemented!()\n    }\n}\n",
            encoding="utf-8",
        )
        (rec,) = pipeline.extract_fn_bodies(CANNED_WITH_WHERE)
        assert _splice(p, rec)
        out = p.read_text(encoding="utf-8")
        assert "self.inner" in out and "unimplemented!()" not in out
        assert "where T: Sized" in out   # bound tightened onto the signature


def test_fill_replay_two_members_one_response() -> None:
    """The per-impl path fills MULTIPLE members from a single response — replay both <FN> blocks
    against the same impl block and confirm each stub is filled."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "lib.rs"
        p.write_text(
            "impl Pair {\n"
            "    fn a(&self) -> u32 {\n        unimplemented!()\n    }\n"
            "    fn b(&self) -> u32 {\n        unimplemented!()\n    }\n"
            "}\n",
            encoding="utf-8",
        )
        for rec in pipeline.extract_fn_bodies(CANNED_TWO_MEMBERS):
            assert _splice(p, rec), rec["fn_name"]
        out = p.read_text(encoding="utf-8")
        assert "unimplemented!()" not in out
        assert "fn a(&self) -> u32 {\n        1" in out
        assert "fn b(&self) -> u32 {\n        2" in out
