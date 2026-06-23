"""Impl-block byte-range scanning — pure domain (P6 engine-relocation series).

A hand-rolled, regex-free Rust lexer that finds the byte ranges of every
`impl` block in a source string. It is NOT a syn parse (syn runs on the Rust
side of the seam) but it is also NOT regex — it is a deterministic character
scanner that respects comments, string/raw-string/char literals, and lifetime
tokens so braces inside any of those never confuse the depth counter.

Pure (stdlib only, no I/O): given source text it returns the ranges. The
callers (`collect_impl_blocks`, `_render_impl_block`) use the ranges to assign
each fn record to the SPECIFIC impl block whose body contains its
`sig_byte_start`, disambiguating two impls that share a container string but
differ in their where-clauses.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ImplRange:
    """Byte offsets of one impl block within a source string."""

    header_start: int  # byte offset of the `impl` keyword
    body_open: int     # byte offset of the `{` that opens the body
    body_close: int    # byte offset of the matching `}`


def parse_impl_block_ranges(source: str) -> list[ImplRange]:
    """Source-aware scan of a Rust file for impl block byte ranges.

    The scanner respects comments (line and block, including nested),
    string literals (regular and raw), char literals, and lifetime
    tokens — so braces inside any of those don't confuse the depth counter.
    """
    n = len(source)
    out: list[ImplRange] = []

    def _skip_line_comment(i: int) -> int:
        while i < n and source[i] != "\n":
            i += 1
        return i

    def _skip_block_comment(i: int) -> int:
        # Rust permits nested `/* … */` blocks.
        depth = 1
        i += 2
        while i < n and depth > 0:
            if source[i] == "/" and i + 1 < n and source[i+1] == "*":
                depth += 1; i += 2
            elif source[i] == "*" and i + 1 < n and source[i+1] == "/":
                depth -= 1; i += 2
            else:
                i += 1
        return i

    def _skip_string(i: int) -> int:
        # Cursor is on the opening `"`.
        i += 1
        while i < n and source[i] != '"':
            if source[i] == "\\" and i + 1 < n:
                i += 2
            else:
                i += 1
        return i + 1 if i < n else i

    def _skip_raw_string(i: int) -> int:
        # Cursor is on `r`. Match `r#…#"…"#…#`.
        j = i + 1
        hashes = 0
        while j < n and source[j] == "#":
            hashes += 1; j += 1
        if j >= n or source[j] != '"':
            return i + 1  # not a raw string, just consume `r`
        end_pat = '"' + ("#" * hashes)
        pos = source.find(end_pat, j + 1)
        return pos + len(end_pat) if pos != -1 else n

    def _skip_char_or_lifetime(i: int) -> int:
        # Cursor is on `'`. Could be `'x'` (char) or `'a` (lifetime).
        j = i + 1
        if j < n and (source[j].isalpha() or source[j] == "_"):
            k = j
            while k < n and (source[k].isalnum() or source[k] == "_"):
                k += 1
            if k < n and source[k] == "'":
                return k + 1  # char literal `'A'`
            return k  # lifetime `'a` — already past
        # Escaped char or symbol literal.
        if j < n and source[j] == "\\" and j + 1 < n:
            j += 2
        elif j < n:
            j += 1
        if j < n and source[j] == "'":
            return j + 1
        return j

    def _is_word_boundary(i: int) -> bool:
        if i == 0:
            return True
        prev = source[i - 1]
        return not (prev.isalnum() or prev == "_")

    i = 0
    while i < n:
        ch = source[i]
        # Whitespace.
        if ch.isspace():
            i += 1; continue
        # Comments.
        if ch == "/" and i + 1 < n and source[i+1] == "/":
            i = _skip_line_comment(i); continue
        if ch == "/" and i + 1 < n and source[i+1] == "*":
            i = _skip_block_comment(i); continue
        # Strings.
        if ch == '"':
            i = _skip_string(i); continue
        if ch == "r" and i + 1 < n and source[i+1] in ('"', "#"):
            i = _skip_raw_string(i); continue
        # Char / lifetime.
        if ch == "'":
            i = _skip_char_or_lifetime(i); continue
        # Impl keyword.
        if (ch == "i" and source.startswith("impl", i)
                and _is_word_boundary(i)
                and (i + 4 >= n
                     or not (source[i+4].isalnum() or source[i+4] == "_"))):
            header_start = i
            j = i + 4
            # Walk to the `{` that opens the impl body — same scanner so
            # `<…>` and `where … { … }` clauses (in generics' const
            # expressions, theoretical) don't fool us.
            body_open = -1
            while j < n:
                cj = source[j]
                if cj.isspace(): j += 1; continue
                if cj == "/" and j + 1 < n and source[j+1] == "/":
                    j = _skip_line_comment(j); continue
                if cj == "/" and j + 1 < n and source[j+1] == "*":
                    j = _skip_block_comment(j); continue
                if cj == '"':
                    j = _skip_string(j); continue
                if cj == "r" and j + 1 < n and source[j+1] in ('"', "#"):
                    j = _skip_raw_string(j); continue
                if cj == "'":
                    j = _skip_char_or_lifetime(j); continue
                if cj == "{":
                    body_open = j; break
                if cj == ";":
                    # `impl X;` is not valid Rust; abort this candidate.
                    body_open = -1; break
                j += 1
            if body_open == -1:
                i = j; continue
            # Walk to matching `}`.
            depth = 1
            k = body_open + 1
            while k < n and depth > 0:
                ck = source[k]
                if ck == "/" and k + 1 < n and source[k+1] == "/":
                    k = _skip_line_comment(k); continue
                if ck == "/" and k + 1 < n and source[k+1] == "*":
                    k = _skip_block_comment(k); continue
                if ck == '"':
                    k = _skip_string(k); continue
                if ck == "r" and k + 1 < n and source[k+1] in ('"', "#"):
                    k = _skip_raw_string(k); continue
                if ck == "'":
                    k = _skip_char_or_lifetime(k); continue
                if ck == "{": depth += 1; k += 1; continue
                if ck == "}":
                    depth -= 1; k += 1
                    if depth == 0:
                        out.append(ImplRange(
                            header_start=header_start,
                            body_open=body_open,
                            body_close=k - 1,
                        ))
                        i = k; break
                    continue
                k += 1
            else:
                i = k
            continue
        i += 1
    return out


def _norm_ws(s: str) -> str:
    """Collapse every whitespace run to a single space and strip the ends —
    the structural equivalent of ``re.sub(r"\\s+", " ", s).strip()``."""
    return " ".join(s.split())


def _strip_leading_generics(s: str) -> str:
    """Drop a leading balanced ``<...>`` generic-parameter list (e.g. the impl's
    own ``<T, E>``), returning the remainder. BALANCED ``<>`` matching — unlike the
    old ``<[^>]*>`` regex it handles nested generics such as ``<T: Tr<X>>``."""
    if not s.startswith("<"):
        return s
    depth = 0
    for i, c in enumerate(s):
        if c == "<":
            depth += 1
        elif c == ">":
            depth -= 1
            if depth == 0:
                return s[i + 1:]
    return s  # unbalanced — leave untouched


def _where_suffix(text: str) -> str:
    """Text after the first whole-word ``where`` in `text`, stripped of
    surrounding whitespace and a trailing comma — i.e. the group(1) of
    ``\\bwhere\\b(.*)$`` after ``.strip().rstrip(",").strip()``. ``""`` if absent."""
    idx = 0
    n = len(text)
    while True:
        k = text.find("where", idx)
        if k == -1:
            return ""
        before_ok = k == 0 or not (text[k - 1].isalnum() or text[k - 1] == "_")
        after = k + len("where")
        after_ok = after >= n or not (text[after].isalnum() or text[after] == "_")
        if before_ok and after_ok:
            return text[after:].strip().rstrip(",").strip()
        idx = k + 1


def impl_where_clause(file_text: str, container: str) -> str:
    """The ``where`` clause of the impl block in `file_text` whose header matches
    `container` (the extractor's impl header with the impl's OWN generics stripped,
    e.g. ``impl Context<T, E> for Result<T, E>``); ``""`` if absent or unmatched.

    Structural rewrite of the old regex locator (P7): the impl blocks are found by
    the comment/string-aware brace scanner, so an ``impl …`` mention inside a
    comment or string can no longer be mistaken for a real impl, and the impl's
    own generics are stripped with BALANCED ``<>`` (the old ``<[^>]*>`` regex
    failed — returned ``""`` — for nested generics like ``<T: Tr<X>>``; this
    matches them).
    """
    if not container or not container.startswith("impl"):
        return ""
    target = _norm_ws(container[len("impl"):])
    if not target:
        return ""
    for r in parse_impl_block_ranges(file_text):
        header = _norm_ws(file_text[r.header_start:r.body_open])  # "impl <gen>? target where…"
        after_impl = header[len("impl"):].lstrip()
        after_gen = _strip_leading_generics(after_impl).lstrip()
        if after_gen.startswith(target):
            return _where_suffix(after_gen[len(target):])
    return ""
