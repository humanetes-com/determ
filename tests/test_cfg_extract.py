"""Byte-equivalence pins for the `#[cfg(...)]` extraction helpers in
`pipeline_core.domain.cfg` (P7 step 2 — the cfg-attribute extraction regexes)."""
from __future__ import annotations

import re

from pipeline_core.domain.cfg import (
    cfg_inline_predicates,
    crate_cfg_predicates,
    match_cfg_attr_line,
)

_INLINE = re.compile(r'#\s*\[\s*cfg\s*\(([^]]+)\)\s*\]')
_ATTR = re.compile(r'^[ \t]*#\[cfg\(([^\]]+(?:\([^\]]*\)[^\]]*)*)\)\]\s*$')
_CRATE = re.compile(r'^\s*#!\[cfg\((.*?)\)\]\s*$', re.MULTILINE)

_INLINE_CASES = [
    '#[cfg(test)]', '# [ cfg ( unix ) ]', '#[cfg(feature = "std")] fn f',
    '#[cfg(all(unix, feature = "x"))]', '#[cfg()]', '#[cfg(a) ]',
    '#[cfg(not(windows))] mod m;', 'no cfg here', '#[cfg_attr(x, y)]',
    '#[cfg(a)] #[cfg(b)]', '#[cfgx(a)]', '', '#[cfg(a]',
]


def test_cfg_inline_predicates_matches_regex():
    for t in _INLINE_CASES:
        assert cfg_inline_predicates(t) == [m.group(1) for m in _INLINE.finditer(t)], t


_ATTR_CASES = [
    '#[cfg(test)]', '  #[cfg(unix)]', '#[cfg(all(a, b))]  ', '#[cfg(feature = "x")]',
    '#[cfg(a)] trailing', '\t#[cfg(not(x))]', '#[cfg()]', '#[ cfg(a)]',
    'fn f()', '#[cfg(a)b)]', '', '#[cfg(a)]x',
]


def test_match_cfg_attr_line_matches_regex():
    for t in _ATTR_CASES:
        m = _ATTR.match(t)
        assert match_cfg_attr_line(t) == (m.group(1) if m else None), t


_CRATE_CASES = [
    '#![cfg(test)]', '  #![cfg(feature = "std")]  ', 'code\n#![cfg(unix)]\nmore',
    '#![cfg(all(a, b))]', 'x #![cfg(a)]', '#![cfg(a)] y', '#![cfg()]',
    '\n\n#![cfg(nightly)]\n', 'no crate cfg', '', '#![cfg(a',
]


def test_crate_cfg_predicates_matches_regex():
    for t in _CRATE_CASES:
        assert crate_cfg_predicates(t) == [m.group(1) for m in _CRATE.finditer(t)], t
