"""Fn-body elision by AST byte span — pure domain.

The pre-strip API-surface artefact promises "signatures and type definitions
only". Deriving that with a line/brace scanner is what broke it: a scanner can
only recognise a body when the declaration and its opening `{` share a line, so
every multi-line signature (long generics, multi-line params, a `where` clause)
leaked its body verbatim — precisely the hard fns the artefact exists to hide.

The syn extractor already reports exact spans (`body_byte_start` just after the
opening brace, `body_byte_end` at the closing one), so elision is a span
replacement with no heuristics and nothing to get wrong about layout.
"""
from __future__ import annotations

from collections.abc import Iterable

from pipeline_core.domain.models import FnRecord
from pipeline_core.domain.rust_scan import struct_declarations
from pipeline_core.domain.type_header import field_block_span

BODY_PLACEHOLDER = " /* ... */ "


def outermost_spans(
    fns: Iterable[FnRecord],
) -> tuple[tuple[int, int], ...]:
    """The body spans to replace, dropping any span nested inside another.

    A fn declared inside another fn's body has a span contained by its parent's.
    Replacing the child first and then the parent would apply the parent's
    ORIGINAL offsets to text whose length has already changed, corrupting the
    output — so only outermost spans survive, and the caller applies them from
    the end backwards.
    """
    spans = sorted(
        (f.body_byte_start, f.body_byte_end) for f in fns
        if 0 <= f.body_byte_start < f.body_byte_end)
    kept: list[tuple[int, int]] = []
    for start, end in spans:
        if kept and start < kept[-1][1]:
            continue          # nested inside the last kept span
        kept.append((start, end))
    return tuple(kept)


def elide_fn_bodies(
    source: str,
    fns: Iterable[FnRecord],
    placeholder: str = BODY_PLACEHOLDER,
) -> str:
    """`source` with every fn body replaced by `placeholder`.

    Spans are byte offsets into the file AS THE EXTRACTOR READ IT, so `source`
    must be that exact text — any prior rewrite (gen-test stripping, dedent)
    invalidates them and must be applied to the RESULT, never the input.
    """
    data = source.encode("utf-8")
    ph = placeholder.encode("utf-8")
    for start, end in reversed(outermost_spans(fns)):
        if end > len(data):
            continue          # stale span vs the text handed in — skip, never truncate
        data = data[:start] + ph + data[end:]
    return data.decode("utf-8", errors="replace")


FIELDS_PLACEHOLDER = " /* private fields */ "


def elide_private_struct_fields(
    source: str,
    names: Iterable[str],
    placeholder: str = FIELDS_PLACEHOLDER,
) -> str:
    """`source` with the field block of every named struct emptied.

    A public struct's private fields are NOT API — nothing outside the crate can
    name them. When the model is the one designing those fields, the pre-strip
    API artefact must not carry the original's choice, or every downstream
    prompt hands back the very layout the authoring stage exists to replace
    (the artefact is read long after strip, when nothing else can correct it).

    Only the listed names are touched, and only their field block: the head
    (`pub struct Foo<T: Clone>`) stays verbatim because it IS the contract.
    Tuple structs keep their parens (`struct Foo(/* … */);` is a legal
    zero-field tuple struct), so the result still parses.
    """
    wanted = frozenset(names)
    if not wanted:
        return source
    out = source
    # Right-to-left: an earlier declaration's indices stay valid after a later
    # one is rewritten.
    for name, start in reversed(struct_declarations(out)):
        if name not in wanted:
            continue
        span = field_block_span(out[start:])
        if span is None:                    # unit struct — nothing to elide
            continue
        open_idx, close_idx = start + span[0], start + span[1]
        out = out[:open_idx + 1] + placeholder + out[close_idx:]
    return out
