"""Byte-equivalence pins for `pipeline_core.domain.doc_render` (P7)."""
from __future__ import annotations

import re

from pipeline_core.domain.doc_render import (
    doc_eq_value_offset,
    is_link_ref_def,
    strip_inline_md_links,
)

_DOC_EQ = re.compile(r'doc\s*=\s*')
_LINK_REF = re.compile(r'\[[^\]]+\]:')
_MD_LINK = re.compile(r'\[([^\]]+)\]\([^)]*\)')


def test_doc_eq_value_offset_matches_regex():
    for s in ["doc = \"x\"", "doc=\"y\"", "doc  =  z", "documentation", "doc(hidden)",
              "doc", "doc =", "notdoc = x", "", "doc\t=\tq"]:
        m = _DOC_EQ.match(s)
        assert doc_eq_value_offset(s) == (m.end() if m else None), s


def test_is_link_ref_def_matches_regex():
    for s in ["[foo]: http://x", "[a]:", "[]:", "[no colon]", "plain", "[x](y)",
              "[a][b]: c", "", "[", "[]"]:
        assert is_link_ref_def(s) == bool(_LINK_REF.match(s)), s


def test_strip_inline_md_links_matches_regex():
    for s in ["[label](url)", "see [a](x) and [b](y)", "[](u)", "[x](", "[x]()",
              "no links", "[a(b)](u)c", "", "[t](http://e.com/p?q=1)", "[[x]](y)"]:
        assert strip_inline_md_links(s) == _MD_LINK.sub(r'\1', s), s


# --- ratchet sweep 2026-07-04: string-literal-aware span walking + doc-run
# --- condensing, previously reached only through e2e prompt flows ---------- #
from pipeline_core.domain.doc_render import (  # noqa: E402
    attr_span_end,
    condense_doc_attrs,
    condense_doc_run,
    doc_attr_string,
)


def test_attr_span_end_string_aware() -> None:
    plain = '#[doc = "has ] bracket"] fn f() {}'
    end = attr_span_end(plain, plain.index("["))
    assert plain[:end] == '#[doc = "has ] bracket"]'
    esc = '#[doc = "a \\" ]b"] rest'
    end = attr_span_end(esc, esc.index("["))
    assert esc[:end] == '#[doc = "a \\" ]b"]'
    raw = '#[doc = r#"raw ] "and" text"#] rest'
    end = attr_span_end(raw, raw.index("["))
    assert raw[:end] == '#[doc = r#"raw ] "and" text"#]'


def test_attr_span_end_unbalanced_and_unterminated() -> None:
    assert attr_span_end('#[doc = "x"', 1) == -1
    assert attr_span_end('#[doc = r#"never closes', 1) == -1


def test_doc_attr_string_forms() -> None:
    assert doc_attr_string('doc = "hi \\"q\\""') == 'hi "q"'
    assert doc_attr_string("doc = hidden") == ""    # doc = non-string → drop, no text
    assert doc_attr_string("doc(hidden)") is None   # no `doc =` prefix
    assert doc_attr_string("allow(dead_code)") is None


def test_condense_doc_run_shapes() -> None:
    assert condense_doc_run(
        ["[![badge]](url)", "", "First sentence. And more prose"]) == "First sentence."
    assert condense_doc_run(["[![only]](badge)"]) == ""
    assert condense_doc_run(["```", "fn example() {}", "```"]) == ""
    assert condense_doc_run(["Intro without period", "", "next paragraph"]) == \
        "Intro without period"
    assert condense_doc_run(["See [docs](http://x) now."]) == "See docs now."


def test_condense_doc_attrs_runs_and_crate_attr_drop() -> None:
    text = ('#![allow(dead_code)]\n'
            '#![doc = "Crate intent. Details follow."]\n'
            'use std::fmt;\n'
            '#[doc = "Fn intent here. Extra."]\n'
            'fn f() {}\n')
    out = condense_doc_attrs(text)
    assert "allow(dead_code)" not in out
    assert "//! Crate intent." in out
    assert "// Fn intent here." in out
    assert "use std::fmt;" in out and "fn f() {}" in out
