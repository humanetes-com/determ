"""Lexical comment/string masking for Rust-ish source (pure, regex-free).

`code_mask(text)` returns a per-char boolean list: True where the char is
inside a comment (`//…`, `///…`, nested `/*…*/`), a string literal
(`"…"`, `b"…"`, raw `r"…"` / `r#"…"#` / `br#"…"#`), or a char literal
(`'x'`, `'\n'`). Scanners that locate fn/macro headers or count braces must
skip masked positions — a `fn fmt(...) {` sitting inside a `///` doc-comment
usage example is TEXT, not a declaration, and splicing a body into it breaks
the file's syntax (observed: doc-comment `fn fmt` in error.rs got a real body
spliced in, producing `error: unexpected closing delimiter` crate-wide).

Lifetimes (`'a`) are NOT masked: a `'` only opens a char literal when it
closes within two chars or starts an escape.
"""

from __future__ import annotations

__all__ = ["code_mask"]


def code_mask(text: str) -> list[bool]:
    n = len(text)
    mask = [False] * n
    i = 0
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = text.find("\n", i)
            if j == -1:
                j = n
            for k in range(i, j):
                mask[k] = True
            i = j
            continue
        if c == "/" and nxt == "*":
            depth = 1
            j = i + 2
            while j < n and depth:
                if text.startswith("/*", j):
                    depth += 1
                    j += 2
                elif text.startswith("*/", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            for k in range(i, min(j, n)):
                mask[k] = True
            i = j
            continue
        # Raw / byte-string prefixes. Only when not mid-identifier (`for`,
        # `br0ken` must not trigger).
        if c in "rb" and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")):
            p = i
            if text[p] == "b" and p + 1 < n and text[p + 1] == "r":
                p += 1
            if text[p] == "r":
                q = p + 1
                hashes = 0
                while q < n and text[q] == "#":
                    hashes += 1
                    q += 1
                if q < n and text[q] == '"':
                    close = '"' + "#" * hashes
                    j = text.find(close, q + 1)
                    j = n if j == -1 else j + len(close)
                    for k in range(i, j):
                        mask[k] = True
                    i = j
                    continue
            if c == "b" and nxt == '"':
                i += 1  # let the `"` branch mask the string body
                continue
            i += 1
            continue
        if c == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                elif text[j] == '"':
                    j += 1
                    break
                else:
                    j += 1
            for k in range(i, min(j, n)):
                mask[k] = True
            i = j
            continue
        if c == "'":
            if nxt == "\\":
                j = text.find("'", i + 2)
                j = n if j == -1 else j + 1
                for k in range(i, min(j, n)):
                    mask[k] = True
                i = j
                continue
            if i + 2 < n and text[i + 2] == "'":
                for k in range(i, i + 3):
                    mask[k] = True
                i += 3
                continue
            i += 1  # lifetime — real code
            continue
        i += 1
    return mask
