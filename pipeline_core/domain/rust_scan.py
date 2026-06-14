"""Regex-free scanners over rendered Rust-ish text (skeletons, merged source).

The pipeline's structured parser for *original* source is the syn-based extractor
(producing :mod:`pipeline_core.domain.models` records). These helpers operate on
ALREADY-rendered skeleton / marker text where re-running syn is neither available
nor reliable (signatures-only, elided bodies, FILE/TESTFILE framing). They use
deterministic token / substring scanning — never regex — mirroring the existing
``text.find``-based ``#[cfg(test)]`` block finder (Hard Rule: no regex).
"""

from __future__ import annotations

import string
from collections.abc import Sequence

_ITEM_KEYWORDS = frozenset({"fn", "struct", "enum", "trait", "type", "const"})
_IDENT_START = frozenset(string.ascii_letters + "_")
_IDENT_CHARS = _IDENT_START | frozenset(string.digits)


def _leading_ident(token: str) -> str:
    """The leading Rust identifier of ``token`` (``foo(`` → ``foo``, ``Bar<T>`` → ``Bar``)."""
    out: list[str] = []
    for ch in token:
        if ch in _IDENT_CHARS:
            out.append(ch)
        else:
            break
    name = "".join(out)
    return name if name and name[0] in _IDENT_START else ""


def item_names(text: str) -> list[str]:
    """Names declared after an item keyword (fn/struct/enum/trait/type/const).

    Token-scan replacement for ``\\b(?:fn|struct|...)\\s+([A-Za-z_]\\w*)``: the
    keyword must be a whitespace-delimited token, and the name is the leading
    identifier of the following token. Order-preserving; duplicates kept (callers
    that want a set can wrap in ``set()``).
    """
    tokens = text.split()
    names: list[str] = []
    for i, tok in enumerate(tokens[:-1]):
        if tok in _ITEM_KEYWORDS:
            name = _leading_ident(tokens[i + 1])
            if name:
                names.append(name)
    return names


def mod_decls(text: str) -> list[str]:
    """Names from ``mod NAME;`` declarations (NOT inline ``mod NAME { ... }``).

    Char-scan replacement for ``\\bmod\\s+(\\w+)\\s*;``: a ``mod`` token (word
    boundary before), then ``\\s+``, an identifier (``\\w+``), optional
    whitespace, then ``;``. An inline module (``mod m {``) lacks that ``;`` and
    is excluded, matching the regex exactly. Order-preserving.
    """
    n = len(text)
    names: list[str] = []
    i = 0
    while True:
        p = text.find("mod", i)
        if p == -1:
            return names
        i = p + 3
        if p > 0 and text[p - 1] in _IDENT_CHARS:  # \b: not a word boundary
            continue
        j = p + 3
        if j >= n or not text[j].isspace():         # \s+ (at least one)
            continue
        while j < n and text[j].isspace():
            j += 1
        k = j
        while k < n and text[k] in _IDENT_CHARS:     # \w+
            k += 1
        if k == j:
            continue
        m = k
        while m < n and text[m].isspace():           # \s*
            m += 1
        if m < n and text[m] == ";":
            names.append(text[j:k])


def count_cfg_attrs(text: str, needles: Sequence[str]) -> int:
    """Count ``#[cfg(...)]`` attributes whose predicate (up to the first ``)``)
    contains any of ``needles``.

    Substring-scan replacement for ``#\\[cfg\\([^)]*(needle)``; preserves the
    original first-``)`` truncation semantics so the count is unchanged.
    """
    marker = "#[cfg("
    count = 0
    i = 0
    while True:
        p = text.find(marker, i)
        if p == -1:
            return count
        close = text.find(")", p)
        segment = text[p:close if close != -1 else len(text)]
        if any(n in segment for n in needles):
            count += 1
        i = p + len(marker)
