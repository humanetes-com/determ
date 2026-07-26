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

Pure: stdlib only, no I/O, no regex. Comment/string-aware via `code_mask`: a
``fn fmt`` sitting inside a ``///`` doc-comment usage example or a string
literal is TEXT, never a header — splicing a body into one corrupts the file
(observed crate-wide `unexpected closing delimiter` from a doc-comment splice).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

from .rust_lex import code_mask
from .rust_scan import starts_with_pub

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


def _body_brace(text: str, mask: list[bool], e: int) -> int | None:
    """Index of the ``{`` opening the body of the fn whose name ends at `e`, or
    None when the declaration is BODYLESS.

    A trait method declaration (``fn not(self) -> bool;``) and an ``extern``
    block entry have no body. Taking the next textual ``{`` instead adopts the
    FOLLOWING item's brace, so the "body" swallows whole items:

        WRONG  trait Bool { fn not(self) -> bool; }
               impl Bool for bool { fn not(self) -> bool { !self } }
               -> the declaration's "body" spans `} impl Bool for bool {`,
                  and reverting `not` splices `unimplemented!()` over the
                  trait's closing brace and the impl header -> the file no
                  longer parses and the whole crate stops compiling.
        RIGHT  the declaration has no body and is not a locatable fn at all.

    A ``;`` at bracket depth 0 terminates the item; one inside ``[u8; 4]`` or a
    comment does not, so depth is tracked and masked spans are skipped."""
    n = len(text)
    depth = 0
    i = e
    while i < n:
        if mask[i]:
            i += 1
            continue
        ch = text[i]
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif depth == 0:
            if ch == "{":
                return i
            if ch == ";":
                return None
        i += 1
    return None


def _match_fn_clause(text: str, c: int, mask: list[bool]) -> tuple[str, int] | None:
    """Match ``(pub…)?(async )?(unsafe )?(const )?fn NAME[^{;]*{`` at `c`; return
    ``(name, brace_open)`` or None (None also for a bodyless declaration)."""
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
    brace = _body_brace(text, mask, e)
    if brace is None:
        return None
    return text[q:e], brace


def _match_macro_clause(text: str, c: int, mask: list[bool]) -> tuple[str, int] | None:
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
    clause: Callable[[str, int, list[bool]], tuple[str, int] | None],
    lead_chars: frozenset[str],
    *,
    line_anchored: bool,
) -> list[HeaderMatch]:
    out: list[HeaderMatch] = []
    n = len(text)
    mask = code_mask(text)
    i = 0
    while i < n:
        # Perf gate: a match can only begin at the first char of its leading keyword
        # (`pub`/`async`/`unsafe`/`const`/`fn`, or `pub`/`macro_rules`). Skipping other
        # positions is exact — `clause` would return None there anyway.
        if text[i] not in lead_chars:
            i += 1
            continue
        # Never match inside a comment or string/char literal.
        if mask[i]:
            i += 1
            continue
        # (?<![A-Za-z_0-9]) lookbehind for the non-line-anchored variant.
        if not line_anchored and i > 0 and text[i - 1] in _IDENT_CHARS:
            i += 1
            continue
        m = clause(text, i, mask)
        if m is None:
            i += 1
            continue
        name, brace = m
        if mask[brace]:
            # The first textual `{` after the header sits in a comment/string
            # — refusing the match is safe; accepting it would splice into
            # non-code.
            i += 1
            continue
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
    return _scan(text, _match_fn_clause, _FN_LEAD_CHARS, line_anchored=line_anchored)


_FN_LEAD_CHARS = frozenset("paucf")     # pub / async / unsafe / const / fn
_MACRO_LEAD_CHARS = frozenset("pm")     # pub / macro_rules


def macro_headers(text: str) -> list[HeaderMatch]:
    """All ``macro_rules!`` header matches (line-anchored), mirroring the macro_re of
    `_condense_rust_to_signatures`."""
    return _scan(text, _match_macro_clause, _MACRO_LEAD_CHARS, line_anchored=True)


def locate_fn(file_text: str, fn_name: str) -> tuple[str, int, int, int] | None:
    """Return ``(sig_text, sig_start, body_start, body_end)`` for the first fn
    whose name matches ``fn_name``. ``body_start`` is just after the opening
    ``{``; ``body_end`` is the position of the matching ``}`` (exclusive end of
    body). ``None`` if not found or braces are malformed."""
    all_locs = locate_all_fns(file_text, fn_name)
    return all_locs[0] if all_locs else None


def locate_all_fns(file_text: str, fn_name: str) -> list[tuple[str, int, int, int]]:
    """Every ``(sig_text, sig_start, body_start, body_end)`` whose name matches
    ``fn_name``, in source order. Same-name fns are the norm in Rust (`fmt` in
    each Debug/Display impl) — callers that revert or inspect "the" fn by bare
    name must handle every occurrence, not just the first. Brace counting
    skips comment/string content so a ``}`` in a string never ends a body."""
    mask = code_mask(file_text)
    out: list[tuple[str, int, int, int]] = []
    for h in fn_headers(file_text, line_anchored=False):
        if h.name != fn_name:
            continue
        sig_start = h.start
        body_start = h.brace_open + 1
        depth = 1
        i = body_start
        while i < len(file_text) and depth > 0:
            if not mask[i]:
                ch = file_text[i]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
            i += 1
        if depth != 0:
            continue
        body_end = i - 1
        sig_text = file_text[sig_start:body_start].rstrip("{ \t\n").strip()
        out.append((sig_text, sig_start, body_start, body_end))
    return out


def unwrap_own_fn_definition(body_text: str, fn_name: str) -> str | None:
    """Inner body of ``body_text`` when the WHOLE text is one fn definition
    declaring ``fn_name``; ``None`` for anything else.

    A model reply sometimes wraps the requested body in the full definition
    (``fn target(...) -> T { <real body> }``). Spliced verbatim, that nests a
    second ``fn target`` inside the stub's braces — legal syntax, so the parse
    guard passes, but the outer fn now ends in an item, has no tail expression,
    and returns ``()``: a guaranteed E0308. Worse, the duplicate header makes
    every later bare-name operation on that fn refuse ``ambiguous_name``, so
    the broken body cannot even be reverted and poisons every sibling compile.

    Strict on purpose: the definition must span the entire text (only
    whitespace, ``//`` comments, or ``#[...]`` attribute lines before the
    header; only whitespace after the closing brace) and declare exactly
    ``fn_name``. A real body that merely STARTS with a nested helper keeps
    its trailing expression and is returned untouched (None)."""
    text = body_text.strip()
    locs = locate_all_fns(text, fn_name)
    if not locs:
        return None
    _sig, sig_start, body_start, body_end = locs[0]
    for line in text[:sig_start].splitlines():
        s = line.strip()
        if s and not (s.startswith("//") or s.startswith("#[")):
            return None
    if text[body_end + 1:].strip():
        return None
    inner = text[body_start:body_end].strip("\n")
    lines = inner.splitlines()
    indents = [len(ln) - len(ln.lstrip()) for ln in lines if ln.strip()]
    cut = min(indents) if indents else 0
    return "\n".join(ln[cut:] if ln.strip() else "" for ln in lines).strip()


def is_pub_fn(file_text: str, fn_name: str) -> bool:
    """True if the fn declaration includes ``pub`` (any visibility variant)."""
    loc = locate_fn(file_text, fn_name)
    if loc is None:
        return False
    return starts_with_pub(loc[0])
