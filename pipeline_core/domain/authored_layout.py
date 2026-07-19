"""Model-authored private layout — the reply protocol + coverage check (F11).

t1.5 asks the model to author a crate's PRIVATE layout (the definitions the
freeze-scope bug used to freeze verbatim) given only the frozen PUBLIC surface,
the layout-pinning tests, and the in-scope symbol inventory. The model replies
with one ``<LAYOUT_ITEM name="…" kind="…">definition</LAYOUT_ITEM>`` per
replaceable item; this module parses that reply into an :class:`AuthoredLayout`
and checks it COVERS every item in the connected component (a missing item
leaves an ``unimplemented!()`` reference dangling and the scaffold won't
compile).

Tolerant, regex-free tag scanning (Hard Rule) mirroring the ``<FN>`` protocol
parser; attribute values AND the item body are HTML-unescaped (some chat
templates round-trip generics like ``Own<T>`` through an XML/HTML-safe layer,
emitting ``Own&lt;T&gt;`` — left as-is, that text would splice as non-Rust).
"""
from __future__ import annotations

import html
from dataclasses import dataclass

from pipeline_core.domain.models import FnRecord, TypeRecord
from pipeline_core.domain.private_surface import (
    PrivateSurfaceItem,
    SurfaceItemKind,
)
from pipeline_core.domain.rust_fn_headers import locate_all_fns

_OPEN = "<LAYOUT_ITEM"
_CLOSE = "</LAYOUT_ITEM>"


@dataclass(frozen=True, slots=True)
class AuthoredItem:
    """One private definition the model authored to replace a frozen item."""

    name: str
    kind: str          # "struct" | "enum" | "type" | "trait" | "fn" (free-form tag)
    definition: str    # the verbatim replacement text


@dataclass(frozen=True, slots=True)
class AuthoredLayout:
    """The model's private layout for one connected component / cfg-cluster."""

    items: tuple[AuthoredItem, ...] = ()

    @property
    def names(self) -> frozenset[str]:
        return frozenset(i.name for i in self.items)

    def item(self, name: str) -> AuthoredItem | None:
        return next((i for i in self.items if i.name == name), None)


def _read_attr(text: str, pos: int, name: str) -> tuple[str, int] | None:
    lead = name + '="'
    if not text.startswith(lead, pos):
        return None
    v = pos + len(lead)
    end = text.find('"', v)
    if end in (-1, v):
        return None
    return html.unescape(text[v:end]), end + 1


def _skip_ws(text: str, i: int) -> int:
    while i < len(text) and text[i].isspace():
        i += 1
    return i


def parse_authored_layout(reply: str) -> AuthoredLayout:
    """Parse every well-formed ``<LAYOUT_ITEM name kind>def</LAYOUT_ITEM>`` block,
    in reply order. A block missing a name/kind attribute or a close tag is
    skipped (retry at the next ``<LAYOUT_ITEM``) rather than aborting the parse —
    one malformed block must not discard the rest of the authored layout."""
    items: list[AuthoredItem] = []
    i = 0
    while True:
        s = reply.find(_OPEN, i)
        if s == -1:
            break
        p = _skip_ws(reply, s + len(_OPEN))
        name_kv = _read_attr(reply, p, "name")
        if name_kv is None:
            i = s + len(_OPEN)
            continue
        name, p = name_kv
        p = _skip_ws(reply, p)
        kind_kv = _read_attr(reply, p, "kind")
        if kind_kv is None:
            i = s + len(_OPEN)
            continue
        kind, p = kind_kv
        gt = reply.find(">", p)
        if gt == -1:
            break
        close = reply.find(_CLOSE, gt + 1)
        if close == -1:
            break
        definition = html.unescape(reply[gt + 1:close].strip())
        if name.strip() and definition:
            items.append(AuthoredItem(name.strip(), kind.strip(), definition))
        i = close + len(_CLOSE)
    return AuthoredLayout(items=tuple(items))


def component_uids(items: tuple[PrivateSurfaceItem, ...]) -> tuple[str, ...]:
    """Protocol identity for each item of one component, in component order.
    A bare name is ambiguous when several items share it (the same trait
    method on different impls, e.g. two `as_ref` with DIFFERENT signatures):
    one authored block would then cover — and get spliced into — every
    same-named slot. Duplicated names get a deterministic ordinal suffix
    (`as_ref.1`, `as_ref.2`); unique names stay bare. Recomputable identically
    at prompt-build, coverage and splice time from the same component tuple;
    the layout-outcome store keys authored locate-keys by these uids, relying
    on exactly that determinism."""
    total: dict[str, int] = {}
    for it in items:
        total[it.name] = total.get(it.name, 0) + 1
    seen: dict[str, int] = {}
    uids: list[str] = []
    for it in items:
        if total[it.name] == 1:
            uids.append(it.name)
        else:
            seen[it.name] = seen.get(it.name, 0) + 1
            uids.append(f"{it.name}.{seen[it.name]}")
    return tuple(uids)


def authored_locate_keys(
    items: tuple[PrivateSurfaceItem, ...],
    authored: dict[str, AuthoredItem],
) -> tuple[tuple[str, str], ...]:
    """(uid, locate-key) per component item after a successful splice: the text
    a FUTURE re-author can whitespace-tolerantly match against the LIVE
    skeleton to find this authored item again. Type/trait definitions are
    stable verbatim (fills never rewrite them), so the whole authored def is
    the key. A fn item's BODY gets rewritten by every later fill, so its key
    is the authored item's header (signature incl. qualifiers) — that survives
    any body fill; if the header can't be parsed the whole def is kept and a
    future locate simply fails loudly (batch reject), never splices wrong."""
    keys: list[tuple[str, str]] = []
    for uid, it in zip(component_uids(items), items, strict=True):
        block = authored.get(uid)
        if block is None:
            continue
        key = block.definition
        if it.kind is SurfaceItemKind.FN_SIG:
            locs = locate_all_fns(block.definition, it.name)
            if locs:
                key = locs[0][0]
        keys.append((uid, key))
    return tuple(keys)


_EXTRACTOR_KIND = {
    SurfaceItemKind.STRUCT: "struct",
    SurfaceItemKind.ENUM: "enum",
    SurfaceItemKind.TYPE_ALIAS: "type",
    SurfaceItemKind.TRAIT: "trait",
}


def derive_locate_keys(
    items: tuple[PrivateSurfaceItem, ...],
    fn_records: list[FnRecord],
    type_records: list[TypeRecord],
) -> tuple[tuple[str, str], ...] | None:
    """Reconstruct (uid, locate-key) pairs for a component from the CURRENT
    scaffold's parsed records — the backfill for AUTHORED store rows persisted
    before locate-keys existed (their authored text was never saved, but it
    lives in the scaffold and the AST extractor reads it back verbatim). Fn
    keys = the parsed signature; type keys = the parsed definition — the same
    text classes the original bake enumerated, so the splice's whitespace-
    tolerant matcher locates them identically. Same-named fns in one file
    bind by source order, matching the splice's occurrence ordering. None
    when any item has no matching record: the caller must skip, never guess."""
    keys: list[tuple[str, str]] = []
    fn_taken: dict[tuple[str, str], int] = {}
    for uid, it in zip(component_uids(items), items, strict=True):
        if it.kind is SurfaceItemKind.FN_SIG:
            matches = [r for r in fn_records
                       if r.file == it.file and r.fn_name == it.name]
            k = fn_taken.get((it.file, it.name), 0)
            fn_taken[(it.file, it.name)] = k + 1
            if k >= len(matches) or not matches[k].sig_text:
                return None
            keys.append((uid, matches[k].sig_text))
        else:
            kind = _EXTRACTOR_KIND[it.kind]
            rec = next((r for r in type_records
                        if r.file == it.file and r.name == it.name
                        and r.kind == kind), None)
            if rec is None or not rec.definition:
                return None
            keys.append((uid, rec.definition))
    return tuple(keys)


def layout_coverage(
    required: tuple[PrivateSurfaceItem, ...], layout: AuthoredLayout,
) -> tuple[frozenset[str], frozenset[str]]:
    """(missing, extra): required item uids the layout did NOT author, and
    authored names not in the required uid set. A non-empty ``missing`` means
    the authored scaffold is incomplete and must be re-requested — never
    spliced. Duplicate-named items are covered per-uid: an authored `as_ref`
    block does NOT cover `as_ref.1` and `as_ref.2`."""
    req = frozenset(component_uids(required))
    got = layout.names
    return req - got, got - req
