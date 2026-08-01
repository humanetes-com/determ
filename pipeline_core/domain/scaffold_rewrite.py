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
from pipeline_core.domain.rust_lex import code_mask
from pipeline_core.domain.rust_scan import item_start, trait_impl_blocks


@dataclass(frozen=True, slots=True)
class RewriteResult:
    text: str
    replaced: bool
    # On a batch reject: index into the replacement list of the pair whose
    # `old` could not be located (None for the overlapping-spans reject and
    # for successes). Lets the caller name WHICH definition failed instead of
    # collapsing every cause into one opaque failure.
    miss_index: int | None = None


@dataclass(frozen=True, slots=True)
class DefReplacement:
    """One frozen-definition swap. `old` is the enumerated definition text used
    to LOCATE the target (whitespace-tolerant). For fn items the enumerated
    text is the bare signature — no visibility/`unsafe` prefix and no stub
    body — so replacing just the matched span leaves `pub(crate) pub(crate)
    fn …` and an orphaned `{ … }` behind. A non-empty `fn_name` therefore
    expands the matched signature to the ENTIRE enclosing fn item — outer
    attributes and qualifiers through closing brace — before splicing `new`
    over it. `new` is a whole item carrying its own `#[cfg]`/`#[cold]`, so a
    span that started at the qualifiers would leave the old attributes stacked
    above it: the two `#[cfg]`s AND together and one more copy accumulates per
    re-splice."""

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
    are skipped), and the splice span becomes outer-attributes-through-closing-
    brace — the item as Rust defines it, since `new` brings its own attributes.
    Same-name fns NOT targeted by any pair (a frozen `pub fn` twin) are left
    untouched because their header never contains the enumerated signature."""
    mask = code_mask(text)
    fn_locs: dict[str, list[tuple[str, int, int, int]]] = {}
    used_fn_spans: set[tuple[int, int]] = set()
    cursor_by_def: dict[str, int] = {}
    spans: list[tuple[int, int, str]] = []
    for rep_i, rep in enumerate(replacements):
        pos = cursor_by_def.get(rep.old, 0)
        while True:
            span = find_definition_span(text, rep.old, start_at=pos)
            if span is None:
                return RewriteResult(text, False, miss_index=rep_i)
            start, end = span
            if not rep.fn_name:
                cursor_by_def[rep.old] = end
                spans.append((start, end, rep.new))
                break
            if rep.fn_name not in fn_locs:
                fn_locs[rep.fn_name] = locate_all_fns(text, rep.fn_name)
            enclosing = next(
                ((item_start(text, mask, s), be + 1)
                 for _, s, bs, be in fn_locs[rep.fn_name]
                 if s <= start and end < bs
                 and (item_start(text, mask, s), be + 1) not in used_fn_spans),
                None)
            if enclosing is not None:
                used_fn_spans.add(enclosing)
                cursor_by_def[rep.old] = end
                spans.append((*enclosing, rep.new))
                break
            pos = start + 1  # sig text hit outside any fn header — keep looking
    spans.extend(_superseded_trait_impls(text, spans))
    ordered = sorted(spans, reverse=True)
    for (start, _, _), (_, prev_end, _) in zip(ordered, ordered[1:], strict=False):
        if start < prev_end:  # overlapping targets — ambiguous, refuse
            return RewriteResult(text, False)
    for start, end, new_def in ordered:
        text = text[:start] + new_def + text[end:]
    return RewriteResult(text, True)


def _superseded_trait_impls(
    text: str, spans: list[tuple[int, int, str]],
) -> list[tuple[int, int, str]]:
    """Deletion spans for trait impls the file already has and a replacement is
    about to write again.

    An item's definition is not always one Rust item. The layout registry's
    entry for `anyhow`'s `Error` is a struct AND the two auto-trait impls the
    model wrote beside it::

        #[repr(transparent)]
        pub struct Error(core::ptr::NonNull<crate::error::ErrorImpl>);
        unsafe impl Send for Error {}
        unsafe impl Sync for Error {}

    The span such a definition replaces is located by NAME — the `struct Error`
    declaration — so it is one item wide while the text going into it is three.
    On 2026-08-01 the crash retrigger re-authored that component into
    `.skeleton-filled/nightly/src/lib.rs`, which already held the previous
    install's copy of the two impls just below the struct. They survived the
    splice: lines 274/275 and 277/278 became four impls of two traits, rustc
    answered E0119 twice, and the whole cluster went from 93 passing tests to a
    tree that does not build. The fill loop then spent every iteration of a
    26-iteration grace window on a compile error no fn body can reach.

    Deleting the older copy is not a judgement call: two impls with the same
    header never compile, so the splice either drops the loser or lands code
    that cannot build. Only impls the incoming text re-declares are touched, and
    only those outside the span it is replacing — an impl already inside the
    replaced region goes out with it.

    "The same" is header equality, which is deliberately strict: `impl AsRef<dyn
    StdError> for Error` and `impl AsRef<dyn StdError + Send + Sync> for Error`
    both live in this crate's `error.rs` and both compile. A splice that treated
    them as one impl would delete working code to install a duplicate that never
    was one."""
    wanted = {b.header for _, _, new_def in spans
              for b in trait_impl_blocks(new_def)}
    if not wanted:
        return []
    return [(b.start, b.end, "")
            for b in trait_impl_blocks(text)
            if b.header in wanted
            and all(b.end <= s or b.start >= e for s, e, _ in spans)]
