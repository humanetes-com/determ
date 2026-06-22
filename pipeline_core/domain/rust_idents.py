"""Rust identifier tokenisation — pure domain (P7 source-regex→structured).

Replaces the ``\\bfn\\s+(\\w+)`` / ``\\b<name>\\b`` regex scans that built the
leaf-order reference-count map in pipeline.py. A maximal-``\\w+``-run tokeniser
is byte-equivalent to Python's word-boundary regex for identifier words (a
``\\b<name>\\b`` match is exactly a maximal word-run equal to <name>), so the
produced counts — and thus the leaf-first ordering — are unchanged.

Pure: stdlib only, no I/O, no regex. Intentionally a token scan (not a syn AST
walk) because the original counted occurrences EVERYWHERE — including comments
and string literals — as an acknowledged approximation that is "robust enough
for leaf-first scheduling"; a token scan preserves that exactly.
"""

from __future__ import annotations

from collections import Counter


def _word_spans(text: str) -> list[tuple[int, int]]:
    """`(start, end)` of every maximal ``[A-Za-z0-9_]+`` run — the unit Python's
    ``\\b...\\b`` word boundaries delimit."""
    spans: list[tuple[int, int]] = []
    n = len(text)
    i = 0
    while i < n:
        if text[i].isalnum() or text[i] == "_":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            spans.append((i, j))
            i = j
        else:
            i += 1
    return spans


_TYPE_DEF_KEYWORDS = frozenset({"struct", "enum", "trait", "type", "impl"})


def _is_camelcase_type(word: str) -> bool:
    """True iff `word` matches ``[A-Z][A-Za-z0-9]+`` as a whole: ASCII
    uppercase-led, length ≥ 2, all ASCII alphanumerics (no underscore)."""
    return len(word) >= 2 and word.isascii() and word.isalnum() and word[0].isupper()


def camelcase_type_names(text: str) -> set[str]:
    """Set of CamelCase identifiers in `text` — byte-equivalent to
    ``set(re.findall(r'\\b([A-Z][A-Za-z0-9]+)\\b', text))`` (the `\\b` boundaries
    mean a CamelCase word inside `Foo_Bar` or `fooBar` never matches)."""
    return {w for s, e in _word_spans(text) if _is_camelcase_type(w := text[s:e])}


def defined_type_names(text: str) -> set[str]:
    """CamelCase names declared after a `struct`/`enum`/`trait`/`type`/`impl`
    keyword — byte-equivalent to
    ``re.finditer(r'\\b(?:struct|enum|trait|type|impl)\\s+([A-Z][A-Za-z0-9]+)\\b')``.
    The name must follow the keyword across one-or-more whitespace (so `impl<T>`
    or `type=` never capture)."""
    spans = _word_spans(text)
    words = [text[s:e] for s, e in spans]
    out: set[str] = set()
    for idx, w in enumerate(words):
        if w not in _TYPE_DEF_KEYWORDS or idx + 1 >= len(words):
            continue
        gap = text[spans[idx][1]:spans[idx + 1][0]]
        if not gap or not gap.isspace():
            continue
        name = words[idx + 1]
        if _is_camelcase_type(name):
            out.add(name)
    return out


def fn_reference_counts(text: str) -> dict[str, int]:
    """Map each `fn <name>` declaration's name to how many times that name word
    occurs anywhere in `text` — a usage-frequency map for leaf-first ordering.

    Byte-equivalent to the old ``re.finditer(r"\\bfn\\s+([A-Za-z_]\\w*)")`` +
    per-name ``re.findall(r"\\b<name>\\b")``: a name follows `fn` only across
    pure whitespace (so `fn(i32)` pointer types / `fn /*c*/ x` never match), and
    each name is counted across the whole text in first-declaration order."""
    spans = _word_spans(text)
    words = [text[s:e] for s, e in spans]
    freq = Counter(words)
    counts: dict[str, int] = {}
    for idx, w in enumerate(words):
        if w != "fn" or idx + 1 >= len(words):
            continue
        gap = text[spans[idx][1]:spans[idx + 1][0]]
        if not gap or not gap.isspace():
            continue  # `\s+`: name must follow `fn` across one-or-more whitespace
        name = words[idx + 1]
        # `fn <name>`: name is a letter/underscore-led identifier (`[A-Za-z_]\w*`).
        if (name[0].isalpha() or name[0] == "_") and name not in counts:
            counts[name] = freq[name]
    return counts
