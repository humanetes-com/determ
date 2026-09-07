"""The outer-attribute block of one item — walked back from the item's head.

Rust binds an outer attribute (`#[…]`) to the NEXT item, across any amount of
whitespace and any comment — so the block gating an item is exactly the run of
contiguous outer attributes whose last `]` is the last code character before
the item's head. The walk here is comment/string aware via `code_mask`, span by
span (a multi-line `#[cfg(\\n  all(a, b)\\n)]` is ONE attribute, not one per
line), and stops at the first thing that is not an outer attribute: the
previous item's `}` or `;`, or an inner `#![…]` (that one belongs to the
enclosing module).

Why the walk is anchored on the item and not on a window of text: an
enumerator that scanned a fixed 800-char lookback for the LAST `#[cfg(…)]`
line above each `macro_rules!` tagged all three `backtrace_if_absent!`
variants of the live `stable` tree's `src/backtrace.rs` with the FIRST one's
`error_generic_member_access` (2026-09-01) — its scanner could not match the
second variant's `all(not(error_generic_member_access), feature = "std")`,
so the last match in the window was the previous item's gate. A block that is
walked back from the item and ends at the previous item's code has no window
to fall through: a variant with no attributes gets an empty block, never its
neighbour's.

Pure: stdlib only, no I/O, no regex.
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = ["attribute_span_before", "outer_attributes_before", "prev_code_index"]


def outer_attributes_before(
    text: str, mask: Sequence[bool], head: int,
) -> tuple[int, list[str]]:
    """(span start, text of each outer attribute) for the item starting at `head`.

    The span start is where the item's FIRST attribute begins, so a caller can
    swap the whole `#[…] #[…] mod X { … }` run in one slice — replacing only the
    `mod` would strand its attributes on whatever item follows. Walks backwards
    span by span so a multi-line attribute is ONE span, not one per line. Stops
    at an inner attribute (`#![…]`): that belongs to the enclosing module.
    """
    attrs: list[str] = []
    at = head
    while True:
        span = attribute_span_before(text, mask, at)
        if span is None:
            return at, list(reversed(attrs))
        start, end = span
        body = text[start:end]
        if body.lstrip("#").lstrip().startswith("!"):
            return at, list(reversed(attrs))
        attrs.append(body)
        at = start


def attribute_span_before(
    text: str, mask: Sequence[bool], at: int,
) -> tuple[int, int] | None:
    """(start, end) of the attribute whose `]` is the last code char before `at`."""
    j = prev_code_index(text, mask, at)
    if j is None or text[j] != "]":
        return None
    close = j
    depth = 0
    while j >= 0:
        if not mask[j]:
            if text[j] == "]":
                depth += 1
            elif text[j] == "[":
                depth -= 1
                if depth == 0:
                    break
        j -= 1
    if j < 0:
        return None
    end = prev_code_index(text, mask, j)
    if end is None:
        return None
    if text[end] == "!":                        # `#![…]` — inner attribute
        end = prev_code_index(text, mask, end)
        if end is None:
            return None
    if text[end] != "#":
        return None
    return end, close + 1


def prev_code_index(text: str, mask: Sequence[bool], at: int) -> int | None:
    """Index of the last unmasked, non-whitespace char before `at`, else None."""
    j = at - 1
    while j >= 0 and (mask[j] or text[j].isspace()):
        j -= 1
    return j if j >= 0 else None
