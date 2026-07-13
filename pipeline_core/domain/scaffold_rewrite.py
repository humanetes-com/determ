"""Scaffold rewrite (F11 t1.5) — splice the model's authored private layout
into the baked skeleton, replacing the frozen private definitions.

Per the F11(b) decision the layout IS the scaffold: t1.5 rewrites the skeleton
files pre-t2a, and the body-filler (splice) is untouched. This module performs
the replacement purely on text: find a frozen definition in a skeleton file
(whitespace-tolerant, since the enumerated source text and the emitted skeleton
may differ in spacing) and swap in the authored one.

No regex, no IO: whitespace-run-insensitive substring location over the raw
text (the adapter reads/writes the files).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RewriteResult:
    text: str
    replaced: bool


def _nonspace_index(text: str) -> tuple[str, list[int]]:
    """(compacted non-space chars, orig-index per compacted char)."""
    chars: list[str] = []
    idx: list[int] = []
    for i, c in enumerate(text):
        if not c.isspace():
            chars.append(c)
            idx.append(i)
    return "".join(chars), idx


def find_definition_span(text: str, definition: str) -> tuple[int, int] | None:
    """The [start, end) span in `text` matching `definition` ignoring whitespace
    runs, or None. Matches on the non-space projection so a struct enumerated as
    ``struct Own<T> { ptr: NonNull<T> }`` still locates its skeleton rendering
    across differing indentation/newlines."""
    hay, idx = _nonspace_index(text)
    needle, _ = _nonspace_index(definition)
    if not needle:
        return None
    pos = hay.find(needle)
    if pos == -1:
        return None
    start = idx[pos]
    end = idx[pos + len(needle) - 1] + 1
    return start, end


def replace_definition(
    text: str, old_definition: str, new_definition: str,
) -> RewriteResult:
    """Replace the first whitespace-tolerant occurrence of `old_definition` in
    `text` with `new_definition`. If not found, returns the text unchanged with
    ``replaced=False`` (the caller falls back to compile-validation / retry —
    never a silent partial rewrite)."""
    span = find_definition_span(text, old_definition)
    if span is None:
        return RewriteResult(text, False)
    start, end = span
    return RewriteResult(text[:start] + new_definition + text[end:], True)
