"""Pure type-reference resolution for per-fn fill prompts (H/§7.3).

`types_referenced_in_text` picks the `TypeRecord`s a fn's prompt should expose:
the types named directly in the text (signature/container) plus one level of
field-transitive expansion (so a field `inner: Own<ErrorImpl>` pulls in `Own`
and `ErrorImpl` even when only `Error` matched directly). Relocated verbatim
from pipeline.py. Pure: stdlib + domain only.

`visible_from` is the fill's file. Without it the selection is crate-wide, which
is how `trait StdError` inside `mod ext` in `context.rs` reached an `error.rs`
TYPES_IN_SCOPE table with `fn ext_context` and `call with .name(args)` — a body
there then failed `error[E0603]: module ext is private` (run `155810-cbb3b9fc`,
2026-08-28). `is_pub` cannot drop it: the trait is `pub`, and trait methods are
recorded `is_pub=false`. The wall is `mod_path`.
"""

from __future__ import annotations

from dataclasses import replace

from pipeline_core.domain.models import TypeRecord
from pipeline_core.domain.rust_idents import identifier_tokens
from pipeline_core.domain.rust_scan import contains_word


def type_nameable_from(rec: TypeRecord, target_file: str) -> bool:
    """Can this type be named from `target_file`?

    Same file (or an unlocated record) is always nameable — the prompt is
    answering that file. A nonempty `mod_path` is a private-mod wall: Rust
    will not resolve the name from another file, however `is_pub` the type
    itself is. File-scope types in another file follow `is_pub`.
    """
    if not target_file or not rec.file or rec.file == target_file:
        return True
    if rec.mod_path:
        return False
    return rec.is_pub


def methods_nameable_from(rec: TypeRecord, target_file: str) -> TypeRecord:
    """Drop methods declared behind a private-mod wall in another file.

    Does not use method `is_pub`: trait methods are recorded false. A method
    with empty `file` (pre-field artifact) is kept."""
    if not target_file:
        return rec
    kept = tuple(
        m for m in rec.methods
        if not (m.file and m.file != target_file and m.mod_path)
    )
    if kept == rec.methods:
        return rec
    return replace(rec, methods=kept)


def hidden_symbols_from_types(
    type_records: list[TypeRecord], target_file: str,
) -> frozenset[str]:
    """Method names whose only declaration is behind a private-mod wall in
    another file — not callable from `target_file`.

    These are names, not prose markers. The caller must test them in CALL
    position (`names_method_call`), because a method name is free to collide
    with an ordinary English word: this crate declares `Bool::not` inside
    `mod __private::not` in `src/lib.rs`, so `not` is returned here for every
    other file, and matching it as a bare word drops any note containing "not".
    `is`, `new`, `from`, `context` and `source` are method names here too.
    """
    if not target_file:
        return frozenset()
    other_walled: set[str] = set()
    visible: set[str] = set()
    for rec in type_records:
        for m in rec.methods:
            if not m.name:
                continue
            other = bool(m.file) and m.file != target_file
            if other and m.mod_path:
                other_walled.add(m.name)
            else:
                visible.add(m.name)
    return frozenset(other_walled - visible)


def types_referenced_in_text(
    text: str, all_types: list[TypeRecord],
    visible_from: str = "",
) -> list[TypeRecord]:
    """Return type records that the fn's prompt should expose, with one
    level of transitive expansion through fields.

    Pass 1: direct text match — types whose `name` appears in `text` as a
    standalone identifier. Picks up names mentioned in the fn signature
    or container.

    Pass 2: field-transitive expansion — for each type matched in pass 1,
    parse the type names out of its fields' Rust type strings and pull
    those records in too. This is what lets the model see `Own::by_ref()`
    when the only direct match was `Error` (whose field is
    `inner: Own<ErrorImpl>`). Without this step the model sees the field
    type as a bare name with no API and starts inventing methods.

    Word boundaries guard against `Boxed` matching when searching for
    `Box`. Field expansion is one level only — going deeper would bloat
    the prompt.

    `visible_from` nonempty filters to types (and methods) nameable from
    that file. Empty keeps the crate-wide selection, which is every
    existing call site that has not yet been given a file.
    """
    if not text or not all_types:
        return []
    candidates = (
        [r for r in all_types if type_nameable_from(r, visible_from)]
        if visible_from else all_types
    )
    by_name = {rec.name: rec for rec in candidates if rec.name}
    out: list[TypeRecord] = []
    seen_names: set[str] = set()
    for rec in candidates:
        name = rec.name
        if not name or name in seen_names:
            continue
        if contains_word(text, name):
            out.append(rec)
            seen_names.add(name)

    # Pass 2: expand by one field-hop. For each matched type, look at the
    # type names appearing in its fields and pull those records in.
    field_hop: list[TypeRecord] = []
    for rec in list(out):
        for f in rec.fields or ():
            ty = f.ty or ""
            if not ty:
                continue
            # Extract identifier-shaped tokens from the field's type
            # string. Tokens like `Own`, `ErrorImpl`, `Backtrace` get
            # added; generics and primitives are dropped via the
            # by_name lookup (we only keep names that match real records).
            for tok in identifier_tokens(ty):
                if tok in seen_names:
                    continue
                hop_rec = by_name.get(tok)
                if hop_rec is None:
                    continue
                field_hop.append(hop_rec)
                seen_names.add(tok)
    out.extend(field_hop)
    if visible_from:
        out = [methods_nameable_from(r, visible_from) for r in out]
    return out
