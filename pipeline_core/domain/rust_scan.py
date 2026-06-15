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


def _has_word(text: str, word: str) -> bool:
    """True when `word` occurs in `text` as a whole identifier token (regex `\\bword\\b`):
    neither neighbour is an identifier char."""
    start = 0
    while (k := text.find(word, start)) >= 0:
        before = text[k - 1] if k > 0 else ""
        after = text[k + len(word)] if k + len(word) < len(text) else ""
        if before not in _IDENT_CHARS and after not in _IDENT_CHARS:
            return True
        start = k + 1
    return False


def use_imports_symbol(block: str, prefixes: Sequence[str], symbol: str) -> bool:
    """True when `block` already has a `use <prefix>…<symbol>…` import — the
    presence check the gen-prelude injector uses to avoid double-importing
    (regex-free, R7; replaces `use (?:std|alloc)::[^;]*\\bSymbol\\b`-style patterns).
    Scans each `use ` occurrence, requires the text right after `use ` to start
    with one of `prefixes`, and `symbol` to appear as a whole identifier token
    within that statement (up to its terminating `;`)."""
    needle = "use "
    pos = 0
    while (u := block.find(needle, pos)) >= 0:
        semi = block.find(";", u)
        stmt = block[u: semi if semi >= 0 else len(block)]
        body = stmt[len(needle):]
        if any(body.startswith(p) for p in prefixes) and _has_word(stmt, symbol):
            return True
        pos = u + len(needle)
    return False


def find_mod_opener(text: str, *, gen_only: bool) -> tuple[int, int] | None:
    """Locate the FIRST `[pub ]mod <name> {` opener (regex-free, R7; mirrors
    `(?:pub\\s+)?mod\\s+(?:gen_)?\\w+\\s*\\{`). Returns `(start, brace)` where
    `start` is the insert-before index (the `pub` if present, else `mod`) and
    `brace` is the index of `{`; None when absent. `gen_only` requires the module
    name to start with `gen_` (and have ≥1 char after). Arbitrary inter-token
    whitespace is allowed."""
    i = 0
    n = len(text)
    while (m := text.find("mod", i)) >= 0:
        i = m + 3
        after = m + 3
        if m > 0 and text[m - 1] in _IDENT_CHARS:      # tail of a longer ident
            continue
        if after >= n or not text[after].isspace():     # need \s+ after `mod`
            continue
        j = after
        while j < n and text[j].isspace():
            j += 1
        k = j
        while k < n and text[k] in _IDENT_CHARS:
            k += 1
        name = text[j:k]
        if not name or (gen_only and not (name.startswith("gen_") and len(name) > 4)):
            continue
        b = k
        while b < n and text[b].isspace():
            b += 1
        if b >= n or text[b] != "{":
            continue
        start = m
        head = text[:m].rstrip()                        # `pub` + \s+ before `mod`?
        if len(head) < m and head.endswith("pub") and (
                len(head) == 3 or head[-4] not in _IDENT_CHARS):
            start = len(head) - 3
        return start, b
    return None


def after_brace_newline(text: str, brace_idx: int) -> int | None:
    """Index just past the last newline in the whitespace run following the `{`
    at `brace_idx` (mirrors greedy `\\{\\s*\\n`), or None when no newline follows
    through whitespace only."""
    n = len(text)
    j = brace_idx + 1
    last_nl = -1
    while j < n and text[j].isspace():
        if text[j] == "\n":
            last_nl = j
        j += 1
    return last_nl + 1 if last_nl >= 0 else None


def count_test_attrs(block: str) -> int:
    """Number of `#[test]` attributes, incl. path-qualified `#[tokio::test]`
    (regex-free, R7; mirrors `#\\[(?:\\w+::)*test\\]`)."""
    count = 0
    i = 0
    while (k := block.find("#[", i)) >= 0:
        i = k + 2
        j = k + 2
        while True:                       # consume zero or more `<ident>::` segments
            seg = j
            while j < len(block) and block[j] in _IDENT_CHARS:
                j += 1
            if j > seg and block[j:j + 2] == "::":
                j += 2
                continue
            j = seg
            break
        if block[j:j + 5] == "test]":
            count += 1
    return count


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
