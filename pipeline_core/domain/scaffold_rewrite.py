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

from pipeline_core.domain.rust_fn_headers import locate_all_fns


@dataclass(frozen=True, slots=True)
class RewriteResult:
    text: str
    replaced: bool


@dataclass(frozen=True, slots=True)
class DefReplacement:
    """One frozen-definition swap. `old` is the enumerated definition text used
    to LOCATE the target (whitespace-tolerant). For fn items the enumerated
    text is the bare signature — no visibility/`unsafe` prefix and no stub
    body — so replacing just the matched span leaves `pub(crate) pub(crate)
    fn …` and an orphaned `{ … }` behind. A non-empty `fn_name` therefore
    expands the matched signature to the ENTIRE enclosing fn item (qualifiers
    through closing brace) before splicing `new` over it."""

    old: str
    new: str
    fn_name: str = ""


def _nonspace_index(text: str) -> tuple[str, list[int]]:
    """(compacted non-space chars, orig-index per compacted char)."""
    chars: list[str] = []
    idx: list[int] = []
    for i, c in enumerate(text):
        if not c.isspace():
            chars.append(c)
            idx.append(i)
    return "".join(chars), idx


def find_definition_span(
    text: str, definition: str, *, start_at: int = 0,
) -> tuple[int, int] | None:
    """The [start, end) span in `text` matching `definition` ignoring whitespace
    runs, or None. Matches on the non-space projection so a struct enumerated as
    ``struct Own<T> { ptr: NonNull<T> }`` still locates its skeleton rendering
    across differing indentation/newlines. `start_at` restricts the search to
    spans beginning at or after that original-text offset (occurrence stepping
    for duplicate definitions)."""
    hay, idx = _nonspace_index(text)
    needle, _ = _nonspace_index(definition)
    if not needle:
        return None
    pos = 0
    while True:
        pos = hay.find(needle, pos)
        if pos == -1:
            return None
        start = idx[pos]
        if start >= start_at:
            end = idx[pos + len(needle) - 1] + 1
            return start, end
        pos += 1


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


def replace_definitions(
    text: str, replacements: list[tuple[str, str]],
) -> RewriteResult:
    """Text-match-only batch replace (see `replace_surface_definitions`)."""
    return replace_surface_definitions(
        text, [DefReplacement(o, n) for o, n in replacements])


def replace_surface_definitions(
    text: str, replacements: list[DefReplacement],
) -> RewriteResult:
    """Replace several frozen definitions in one file atomically. Occurrences
    are assigned in list order: when two items share an IDENTICAL frozen
    definition (e.g. the same private trait method on two impls), the first
    pair binds the first occurrence, the second pair the next one — never the
    text a previous replacement just spliced in (sequential single replaces
    re-match inside the fresh splice and nest garbage). Spans are located on
    the pristine text and applied bottom-up; any miss rejects the whole batch
    (``replaced=False``, text unchanged).

    A replacement carrying `fn_name` swaps the WHOLE enclosing fn item: the
    signature match must fall inside the header of a brace-counted fn of that
    name (comment/string occurrences of the signature text fail this test and
    are skipped), and the splice span becomes qualifiers-through-closing-brace.
    Same-name fns NOT targeted by any pair (a frozen `pub fn` twin) are left
    untouched because their header never contains the enumerated signature."""
    fn_locs: dict[str, list[tuple[str, int, int, int]]] = {}
    used_fn_spans: set[tuple[int, int]] = set()
    cursor_by_def: dict[str, int] = {}
    spans: list[tuple[int, int, str]] = []
    for rep in replacements:
        pos = cursor_by_def.get(rep.old, 0)
        while True:
            span = find_definition_span(text, rep.old, start_at=pos)
            if span is None:
                return RewriteResult(text, False)
            start, end = span
            if not rep.fn_name:
                cursor_by_def[rep.old] = end
                spans.append((start, end, rep.new))
                break
            if rep.fn_name not in fn_locs:
                fn_locs[rep.fn_name] = locate_all_fns(text, rep.fn_name)
            enclosing = next(
                ((s, be + 1) for _, s, bs, be in fn_locs[rep.fn_name]
                 if s <= start and end < bs and (s, be + 1) not in used_fn_spans),
                None)
            if enclosing is not None:
                used_fn_spans.add(enclosing)
                cursor_by_def[rep.old] = end
                spans.append((*enclosing, rep.new))
                break
            pos = start + 1  # sig text hit outside any fn header — keep looking
    ordered = sorted(spans, reverse=True)
    for (start, _, _), (_, prev_end, _) in zip(ordered, ordered[1:], strict=False):
        if start < prev_end:  # overlapping targets — ambiguous, refuse
            return RewriteResult(text, False)
    for start, end, new_def in ordered:
        text = text[:start] + new_def + text[end:]
    return RewriteResult(text, True)
