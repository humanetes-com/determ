"""Fill-replay characterisation tests.

The per-impl fill engine (`_phase_c_fill_impl_blocks` / `_process_one_stub`) is closure-heavy and
interleaves an LLM call, a cargo check, and a retrospective loop — not unit-testable end-to-end
without reconstructing its whole enclosing scope. But its *deterministic core* IS testable: given
a canned model response in the `<FN name="..." block="...">body</FN>` protocol, the engine parses
it with `extract_fn_bodies` and applies each body with the container-aware splicer
`_splice_fn_with_where`. These tests pin that parse→splice transform with real inputs, so the
engine can be refactored against a net without a live model or cargo.

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
    ok, _reason, _delta = pipeline._APP.impl_fill_deps.splice_fn_with_where(
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


# The reply shape that cost a whole file-first attempt: nightly/src/error.rs,
# 2026-08-03 10:37, 17 <FN> blocks and every one of them ending `\n    }`. Each
# body is short one `{`, so each splice closed its fn early, the top-level item
# fingerprint refused all 17, and the file landed nothing and fell back to
# per-block fills. Verbatim from that reply's shape, at two members.
CANNED_SELF_CLOSING = (
    '<FN name="a" block="impl Pair">\n        1\n    }\n</FN>\n'
    '<FN name="b" block="impl Pair">\n        2\n    }\n</FN>'
)


def test_fill_replay_reply_that_closes_each_body_itself() -> None:
    """A reply that restates each fn's closing brace still fills every member.

    The bodies are correct; only the protocol's shape is wrong. Before the
    splice learnt to drop that brace this response filled NOTHING — 0 of 17 on
    the live tree — and the fill route reported `nothing spliced`."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "lib.rs"
        p.write_text(
            "impl Pair {\n"
            "    fn a(&self) -> u32 {\n        unimplemented!()\n    }\n"
            "    fn b(&self) -> u32 {\n        unimplemented!()\n    }\n"
            "}\n",
            encoding="utf-8",
        )
        recs = pipeline.extract_fn_bodies(CANNED_SELF_CLOSING)
        assert [r["fn_name"] for r in recs] == ["a", "b"]
        for rec in recs:
            assert _splice(p, rec), rec["fn_name"]
        out = p.read_text(encoding="utf-8")
        assert "unimplemented!()" not in out
        assert out.count("}") == 3          # two fns, the impl, and nothing spare
        assert "fn a(&self) -> u32 {\n        1" in out
        assert "fn b(&self) -> u32 {\n        2" in out
