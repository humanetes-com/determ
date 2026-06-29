"""Pure type-reference resolution for per-fn fill prompts (H/§7.3).

`types_referenced_in_text` picks the `TypeRecord`s a fn's prompt should expose:
the types named directly in the text (signature/container) plus one level of
field-transitive expansion (so a field `inner: Own<ErrorImpl>` pulls in `Own`
and `ErrorImpl` even when only `Error` matched directly). Relocated verbatim
from pipeline.py. Pure: stdlib + domain only.
"""

from __future__ import annotations

from pipeline_core.domain.models import TypeRecord
from pipeline_core.domain.rust_idents import identifier_tokens
from pipeline_core.domain.rust_scan import contains_word


def types_referenced_in_text(
    text: str, all_types: list[TypeRecord],
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
    """
    if not text or not all_types:
        return []
    by_name = {rec.name: rec for rec in all_types if rec.name}
    out: list[TypeRecord] = []
    seen_names: set[str] = set()
    for rec in all_types:
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
    return out
