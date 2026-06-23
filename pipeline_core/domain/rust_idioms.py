"""Structured detectors for the four code "idioms" the sibling-hint builder scans
for in generated fn bodies (P7 — replaces the `_IDIOM_*_RE` source regexes).

Each detector is byte-equivalent to the regex it replaces — verified by large
random fuzz against the original pattern — and returns matches in `finditer`
order (the consumer counts occurrences and grabs example snippets):

  - `self_constructor_calls`  ← ``\\bSelf::(name)\\s*\\((args≤200, non-greedy)\\)``
  - `fn_pointer_field_calls`  ← ``\\(\\s*(path, non-greedy)\\s*\\)\\s*\\(``
  - `unsafe_block_chains`     ← ``unsafe\\s*\\{\\s*([^\\n{}]{1,160})\\s*\\}``
  - `struct_literals`         ← ``\\b([A-Z]\\w{2,40})\\s*\\{\\s*([^{}]{1,400})\\s*\\}``

Pure: stdlib only, no I/O, no regex. Naive of string/char literals, exactly as the
negated-class regexes were.
"""

from __future__ import annotations

_IDENT_START = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_ASCII_WORD = _IDENT_START | frozenset("0123456789")  # [A-Za-z0-9_]


def _is_word_char(c: str) -> bool:
    """Python ``\\w`` (unicode-aware) — what a ``\\b`` boundary is measured against."""
    return c.isalnum() or c == "_"


def _match_ws_capture_ws_brace(
    text: str, bp1: int, excluded: frozenset[str], cap: int, n: int,
) -> tuple[str, int] | None:
    """Match ``\\s*([^excluded]{1,cap})\\s*\\}`` starting at `bp1` (just past the
    opening ``{``). Returns ``(captured, end)`` (`end` just past the ``}``) or None.

    Faithful nested-greedy backtracking: the leading ``\\s*`` is greedy (longest
    first) but yields whitespace chars back to the capture so its ``{1,…}`` minimum is
    met — e.g. ``{  \\n}`` for ``[^\\n{}]`` captures a single space (the leading ``\\s*``
    shrinks, the capture takes a space, the trailing ``\\s*`` eats the newline)."""
    lead_max = bp1
    while lead_max < n and text[lead_max].isspace():
        lead_max += 1
    for a in range(lead_max, bp1 - 1, -1):  # leading \s* length: longest first
        g_end = a
        while g_end < n and (g_end - a) < cap and text[g_end] not in excluded:
            g_end += 1
        for gl in range(g_end - a, 0, -1):  # capture {1,cap}: greedy longest first
            p = a + gl
            while p < n and text[p].isspace():  # trailing \s*
                p += 1
            if p < n and text[p] == "}":
                return text[a:a + gl], p + 1
    return None


def self_constructor_calls(text: str) -> list[tuple[str, str]]:
    """``(full_match, method_name)`` for every ``Self::name(args)`` — byte-equivalent
    to ``re.finditer(r'\\bSelf::([A-Za-z_][A-Za-z_0-9]*)\\s*\\(([^()]{0,200}?)\\)',
    re.DOTALL)`` projected onto ``(m.group(0), m.group(1))``."""
    out: list[tuple[str, str]] = []
    n = len(text)
    needle = "Self::"
    i = 0
    while (k := text.find(needle, i)) >= 0:
        i = k + 1
        if k > 0 and _is_word_char(text[k - 1]):  # \bSelf
            continue
        q = k + 6
        if q >= n or text[q] not in _IDENT_START:  # group1 [A-Za-z_]…
            continue
        e = q + 1
        while e < n and text[e] in _ASCII_WORD:
            e += 1
        p = e
        while p < n and text[p].isspace():  # \s*
            p += 1
        if p >= n or text[p] != "(":
            continue
        # ([^()]{0,200}?)\) — non-greedy: first ')' reachable via non-paren ≤200
        gs = p + 1
        r = gs
        while r < n and text[r] not in "()" and (r - gs) < 200:
            r += 1
        if r < n and text[r] == ")" and (r - gs) <= 200:
            out.append((text[k:r + 1], text[q:e]))
            i = r + 1
    return out


def fn_pointer_field_calls(text: str) -> list[str]:
    """The path of every ``(path)(`` fn-pointer-field dispatch — byte-equivalent to
    the group(1) of ``re.finditer(r'\\(\\s*([A-Za-z_][\\w.]+?)\\s*\\)\\s*\\(')`` (the
    consumer filters paths without a ``.``)."""
    out: list[str] = []
    n = len(text)
    i = 0
    while (k := text.find("(", i)) >= 0:
        i = k + 1
        p = k + 1
        while p < n and text[p].isspace():  # \s*
            p += 1
        if p >= n or text[p] not in _IDENT_START:  # [A-Za-z_]
            continue
        s = p
        e = s + 1
        while e < n and (text[e] in _ASCII_WORD or text[e] == "."):  # [\w.]
            e += 1
        # [\w.]+? — non-greedy ≥1 after the start char: shortest L≥2 with \s*\)\s*\(
        for length in range(2, (e - s) + 1):
            g_end = s + length
            r = g_end
            while r < n and text[r].isspace():
                r += 1
            if r >= n or text[r] != ")":
                continue
            r2 = r + 1
            while r2 < n and text[r2].isspace():
                r2 += 1
            if r2 < n and text[r2] == "(":
                out.append(text[s:g_end])
                i = r2 + 1
                break
    return out


def unsafe_block_chains(text: str) -> list[str]:
    """Inner text of every ``unsafe { … }`` single-block chain — byte-equivalent to
    the group(1) of ``re.finditer(r'unsafe\\s*\\{\\s*([^\\n{}]{1,160})\\s*\\}')``
    (``unsafe`` is NOT word-anchored, matching the original)."""
    out: list[str] = []
    n = len(text)
    i = 0
    while (k := text.find("unsafe", i)) >= 0:
        i = k + 6
        p = k + 6
        while p < n and text[p].isspace():  # \s*
            p += 1
        if p >= n or text[p] != "{":
            continue
        res = _match_ws_capture_ws_brace(text, p + 1, frozenset("\n{}"), 160, n)
        if res is not None:
            out.append(res[0])
            i = res[1]
    return out


def struct_literals(text: str) -> list[tuple[str, str]]:
    """``(type_name, inner)`` for every ``Name { … }`` struct literal — byte-equivalent
    to ``re.finditer(r'\\b([A-Z][A-Za-z0-9_]{2,40})\\s*\\{\\s*([^{}]{1,400})\\s*\\}',
    re.DOTALL)``. Inner cannot contain braces, so no nesting/overlap is possible — the
    matches equal a per-word-span scan."""
    out: list[tuple[str, str]] = []
    n = len(text)
    i = 0
    while i < n:
        # \b[A-Z] : ASCII uppercase at a (unicode-aware) word boundary
        if "A" <= text[i] <= "Z" and (i == 0 or not _is_word_char(text[i - 1])):
            e = i + 1
            while e < n and text[e] in _ASCII_WORD:  # [A-Za-z0-9_]{…}
                e += 1
            matched_end: int | None = None
            if 3 <= e - i <= 41:  # [A-Z] + {2,40}
                p = e
                while p < n and text[p].isspace():  # \s*
                    p += 1
                if p < n and text[p] == "{":
                    res = _match_ws_capture_ws_brace(text, p + 1, frozenset("{}"), 400, n)
                    if res is not None:
                        out.append((text[i:e], res[0]))
                        matched_end = res[1]
            i = matched_end if matched_end is not None else e
        else:
            i += 1
    return out
