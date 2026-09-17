"""Doc tests — the tests a `#[test]`-record walk cannot see.

`cargo test` runs rustdoc over every ```` ``` ```` block in a doc comment as its
own test binary. Those tests have no `#[test]` attribute and no fn record, so
the static inventory that counts `#[test]` fns misses all of them: on the
reconstructed anyhow tree task3 announced 149 tests against `expected=124` for
`stable` and 129 against `expected=104` for `nightly` — undershooting by exactly
25 in both, and `cargo test --doc` on that tree reports `running 25 tests`.
Identical for both clusters because the doc comments carrying them are not
cfg-split.

Pure text analysis, no regex, matching rustdoc's own rule for what is a test.
"""
from __future__ import annotations

from collections.abc import Iterable

# rustdoc attributes that may appear in a fence's info string. Anything else is
# a LANGUAGE tag, and a block in another language is documentation, not a test:
# this crate's `toml` (2) and `console` (3) blocks are why a naive fence count
# reads 28 where cargo runs 25.
_RUSTDOC_ATTRS = frozenset({
    "rust", "ignore", "should_panic", "no_run", "compile_fail",
    "allow_fail", "standalone", "standalone_crate",
})


def _is_rust_fence(info: str) -> bool:
    """Whether a fence's info string opens a RUST block (i.e. a doc test)."""
    tags = {t.strip().lower() for t in info.replace(",", " ").split() if t.strip()}
    if not tags:
        return True          # bare ``` is Rust
    return all(t in _RUSTDOC_ATTRS or t.startswith("edition") for t in tags)


def _read_string_literal(text: str, start: int) -> tuple[str, int] | None:
    """The value of the Rust string literal opening at `start` (a `"`), and the
    index just past its closing quote. Handles `\\"` and `\\\\`; returns None if
    unterminated."""
    if start >= len(text) or text[start] != '"':
        return None
    out: list[str] = []
    i = start + 1
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            out.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
            i += 2
            continue
        if ch == '"':
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    return None


def doc_lines(text: str) -> list[str]:
    """Every documentation line in source order, in BOTH spellings.

    `///` and `//!` comments, and the `#[doc = "…"]` attribute form. The second
    is not exotic here: this pipeline's own emission renders doc comments as
    `#[doc = …]` attributes when it round-trips a file through syn, and the
    `ensure!` macro in the reconstructed anyhow tree carries two real doc tests
    that way — a scanner that reads only `///` silently loses them.
    """
    found: list[tuple[int, str]] = []
    pos = 0
    for raw in text.splitlines(keepends=True):
        s = raw.strip()
        if s.startswith("///") or s.startswith("//!"):
            found.append((pos, s[3:]))
        pos += len(raw)
    i = text.find("#[doc")
    while i >= 0:
        j = i + len("#[doc")
        while j < len(text) and text[j].isspace():
            j += 1
        if j < len(text) and text[j] == "=":
            j += 1
            while j < len(text) and text[j].isspace():
                j += 1
            lit = _read_string_literal(text, j)
            if lit is not None:
                found.append((i, lit[0]))
        i = text.find("#[doc", i + 1)
    found.sort(key=lambda p: p[0])
    return [v for _, v in found]


def count_doc_tests(text: str) -> int:
    """How many doc tests one Rust source file contributes to `cargo test`.

    The open/close toggle matters: a non-Rust block still has a CLOSING fence,
    and treating that close as an open desynchronises the rest of the file —
    which is how a first cut both over-counted `lib.rs` and lost two real doc
    tests in `macros.rs`.
    """
    return _count_rust_fences(doc_lines(text))


def count_doc_tests_in_doc(doc: str) -> int:
    """How many doc tests one item's joined doc text contributes. rustdoc reads
    each item's doc on its own, so a fence left open in one item never spills
    into the next."""
    return _count_rust_fences(doc.splitlines())


def _count_rust_fences(lines: Iterable[str]) -> int:
    n = 0
    open_fence = False
    for line in lines:
        body = line.strip()
        if not body.startswith("```"):
            continue
        if open_fence:
            open_fence = False
            continue
        open_fence = True
        if _is_rust_fence(body[3:]):
            n += 1
    return n
