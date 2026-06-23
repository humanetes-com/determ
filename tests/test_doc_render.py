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
