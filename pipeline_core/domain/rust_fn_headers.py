"""Structured fn / `macro_rules!` HEADER location over Rust-ish source (P7).

Collapses the three parallel header regexes the pipeline used to drive its body
elision and body-splice logic into one domain concept — a *header match*:

  - `_condense_rust_to_signatures` fn_re + macro_re  (line-anchored, MULTILINE)
  - `_merge_fn_blocks_into_disk` fn_re                (non-ident lookbehind)
  - `_FN_RE_GLOBAL` / `_locate_fn`                    (non-ident lookbehind + name)

All three share the clause ``(pub(...)? )?(async )?(unsafe )?(const )?fn NAME …{``
(and a `macro_rules!` variant); they differ only in the BOUNDARY before the clause:
line-start + optional ``[ \\t]`` indent (``(?:^|\\n)[ \\t]*``, captured INTO group 1)
vs. a non-identifier lookbehind (``(?<![A-Za-z_0-9])``, zero-width). A `HeaderMatch`
exposes `start` (= the old ``m.start(1)`` / ``m.start()``), the fn/macro `name`, and
`brace_open` (index of the opening ``{``; ``body_start`` = ``brace_open + 1`` = the old
``m.end()``). The caller does its own brace-depth body scan from `body_start`, exactly
as before.

Pure: stdlib only, no I/O, no regex. Naive of string/char literals — byte-for-byte
what the negated-class regexes (``[^{]``) did; the first textual ``{`` ends the header.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

_IDENT_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
)
_IDENT_START = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")


class HeaderMatch(NamedTuple):
    start: int        # group-1 start: the `\n`/line-start (line-anchored) or clause start
    name: str         # the fn / macro name
    brace_open: int   # index of the opening `{` (body_start = brace_open + 1)


def _opt_pub(text: str, p: int) -> int:
    """Past an optional ``pub(?:\\([^)]*\\))?\\s+`` visibility clause, else `p`
    unchanged (the clause REQUIRES trailing whitespace; a non-ws-terminated `pub`
    backtracks to empty)."""
    n = len(text)
    if not text.startswith("pub", p):
        return p
    q = p + 3
    if q < n and text[q] == "(":  # optional \([^)]*\)
        close = text.find(")", q + 1)
        if close != -1:
            q = close + 1
    if q < n and text[q].isspace():  # required \s+
        while q < n and text[q].isspace():
            q += 1
        return q
    return p


def _opt_kw_ws(text: str, p: int, kw: str) -> int:
    """Past an optional ``<kw>\\s+`` modifier, else `p` unchanged."""
    n = len(text)
    if text.startswith(kw, p):
        q = p + len(kw)
        if q < n and text[q].isspace():
            while q < n and text[q].isspace():
                q += 1
            return q
    return p


def _ident_end(text: str, q: int) -> int:
    """End of an ``[A-Za-z_]\\w*`` identifier starting at `q`, or `q` if none."""
    n = len(text)
    if q >= n or text[q] not in _IDENT_START:
        return q
    e = q + 1
    while e < n and text[e] in _IDENT_CHARS:
        e += 1
    return e


def _match_fn_clause(text: str, c: int) -> tuple[str, int] | None:
    """Match ``(pub…)?(async )?(unsafe )?(const )?fn NAME[^{]*{`` at `c`; return
    ``(name, brace_open)`` or None."""
    n = len(text)
    p = _opt_pub(text, c)
    p = _opt_kw_ws(text, p, "async")
    p = _opt_kw_ws(text, p, "unsafe")
    p = _opt_kw_ws(text, p, "const")
    if not text.startswith("fn", p):
        return None
    q = p + 2
    if q >= n or not text[q].isspace():  # fn\s+
        return None
    while q < n and text[q].isspace():
        q += 1
    e = _ident_end(text, q)
    if e == q:  # name [A-Za-z_]\w* required
        return None
    brace = text.find("{", e)  # [^{]*\{
    if brace == -1:
        return None
    return text[q:e], brace


def _match_macro_clause(text: str, c: int) -> tuple[str, int] | None:
    """Match ``(pub…)?macro_rules!\\s+NAME\\s*{`` at `c`; return
    ``(name, brace_open)`` or None."""
    n = len(text)
    p = _opt_pub(text, c)
    if not text.startswith("macro_rules", p):
        return None
    q = p + 11
    if q >= n or text[q] != "!":
        return None
    q += 1
    if q >= n or not text[q].isspace():  # \s+
        return None
    while q < n and text[q].isspace():
        q += 1
    e = _ident_end(text, q)
    if e == q:
        return None
    b = e  # \s*
    while b < n and text[b].isspace():
        b += 1
    if b >= n or text[b] != "{":
        return None
    return text[q:e], b


def _scan(
    text: str,
    clause: Callable[[str, int], tuple[str, int] | None],
    *,
    line_anchored: bool,
) -> list[HeaderMatch]:
    out: list[HeaderMatch] = []
    n = len(text)
    i = 0
    while i < n:
        # (?<![A-Za-z_0-9]) lookbehind for the non-line-anchored variant.
        if not line_anchored and i > 0 and text[i - 1] in _IDENT_CHARS:
            i += 1
            continue
        m = clause(text, i)
        if m is None:
            i += 1
            continue
        name, brace = m
        if line_anchored:
            # (?:^|\n)[ \t]*: the clause must be the first non-blank content of its
            # line; group 1 starts at the preceding `\n` (or at string start).
            s = i
            while s > 0 and text[s - 1] in " \t":
                s -= 1
            if not (s == 0 or text[s - 1] == "\n"):
                i += 1
                continue
            start = s if s == 0 else s - 1
        else:
            start = i
        out.append(HeaderMatch(start, name, brace))
        i = brace + 1  # resume past the `{` (= the old m.end())
    return out


def fn_headers(text: str, *, line_anchored: bool) -> list[HeaderMatch]:
    """All fn-signature header matches, in source order (finditer order).

    `line_anchored=True` mirrors the MULTILINE ``(?:^|\\n)[ \\t]*…fn…\\{`` used by
    `_condense_rust_to_signatures` (group 1 includes the leading newline+indent);
    `False` mirrors the ``(?<![A-Za-z_0-9])…fn…\\{`` used by `_merge_fn_blocks_into_disk`
    and `_FN_RE_GLOBAL` (`start` = clause start)."""
    return _scan(text, _match_fn_clause, line_anchored=line_anchored)


def macro_headers(text: str) -> list[HeaderMatch]:
    """All ``macro_rules!`` header matches (line-anchored), mirroring the macro_re of
    `_condense_rust_to_signatures`."""
    return _scan(text, _match_macro_clause, line_anchored=True)
