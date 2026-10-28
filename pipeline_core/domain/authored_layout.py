"""Model-authored private layout — the reply protocol + coverage check.

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
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from pipeline_core.domain.frozen_visibility import carry_frozen_visibility
from pipeline_core.domain.models import FnRecord, TypeRecord
from pipeline_core.domain.private_surface import (
    PrivateSurfaceItem,
    SurfaceItemKind,
)
from pipeline_core.domain.rust_fn_headers import locate_all_fns
from pipeline_core.domain.rust_scan import attr_bracket_delta
from pipeline_core.domain.scaffold_rewrite import (
    DefReplacement,
    find_definition_span,
)
from pipeline_core.domain.type_header import (
    TypeHeader,
    declaration_text,
    parse_type_header,
)

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


# The file, the surface kind, and either the parsed type head or the
# declaration text: see :func:`item_slot`.
ItemSlot = tuple[str, SurfaceItemKind, TypeHeader | str]


def item_slot(item: PrivateSurfaceItem) -> ItemSlot:
    """The declaration a protocol uid denotes, not the body the model writes.

    Duplicate names are numbered by position in one component
    (:func:`component_uids`). Two worlds that list those names in a different
    order therefore share the ordinals and not the items, and a reply keyed
    by the ordinals is spliced into the other world's declaration. A type
    whose head parses is the file, the surface kind, and that head
    (visibility, kind, name, generics). Fields are not in it: they are what
    the reply writes, and the two worlds' ``ErrorImpl`` on run
    ``42057-641e589c`` differed by fields and were still one item. Attributes
    are not in it. A fn, or an item whose head does not parse, is the file,
    the kind, and :func:`declaration_text` of its definition.
    """
    header = parse_type_header(item.definition)
    if header is not None:
        return (item.file, item.kind, header)
    return (item.file, item.kind, declaration_text(item.definition))


def shared_item_ids_agree(
    groups: Sequence[tuple[tuple[str, ...], tuple[PrivateSurfaceItem, ...]]],
) -> bool:
    """True when every uid that occurs in more than one group is the same slot.

    A uid that occurs once has nothing to disagree with. This is the check a
    union of two worlds' items has to pass on top of the ordinal strings
    matching: equal ordinals are not equal declarations.
    """
    seen: dict[str, ItemSlot] = {}
    for uids, items in groups:
        for uid, item in zip(uids, items, strict=True):
            slot = item_slot(item)
            prev = seen.get(uid)
            if prev is not None and prev != slot:
                return False
            seen[uid] = slot
    return True


_REPR_ATTR = re.compile(r"^[ \t]*#\[repr\([^\]]*\)\][ \t]*$", re.MULTILINE)
_ITEM_DECL = re.compile(
    r"^([ \t]*)(?:pub(?:[ \t]*\([^)]*\))?[ \t]+)?(?:struct|enum|union)[ \t]",
    re.MULTILINE)


def carry_frozen_repr(frozen: str, authored: str) -> str:
    """Re-attach the `#[repr(...)]` the frozen definition carried when the
    authored replacement dropped it. Returns `authored` unchanged otherwise.

    A type's memory representation is a fact of the crate being reconstructed,
    not part of the layout the model is free to design. On 2026-08-07 the crash
    retrigger of run `241132-f9fae0a6` re-authored `stable`'s
    Error/ErrorImpl/ErrorVTable/Own/Ref component and its `pub struct Error`
    came back without the `#[repr(transparent)]` the frozen definition has. The
    next sweep could not build the cluster at all — `tests/test_ffi.rs:7:
    error: extern fn uses type anyhow::Error, which is not FFI-safe`, from a
    write-protected test that passes `anyhow::Error` by value through
    `extern "C"` under `#![deny(improper_ctypes)]`. By then 56 of the 84
    referencing fns had been reverted to stubs against the new layout and the
    cluster read pass=0 for two iterations, until the batch ratchet restored 8
    files.

    Nothing upstream of the splice can catch this: the accept gate is
    `cargo check --lib --quiet` inside `.skeleton/<cluster>`, a directory with
    no `tests/` at all, and that check returns 0 on the very layout `test_ffi`
    rejects (measured on a copy of the tree, 2026-08-07). Nine of the 53 frozen
    definitions in `stable`'s private surface carry a repr, and losing one is
    not always a compile error: `ErrorImpl` and `ContextError` are `#[repr(C)]`
    "to ensure that E remains in the final position", so dropping THAT one
    silently reorders fields under the pointer casts that read them.

    Deliberately one-directional: a definition that already carries a repr of
    its own is returned untouched. The model that re-represents a type on
    purpose still gets its way and still answers to the compile gate, and this
    can never emit a conflicting `transparent` + `C` pair.
    """
    if _REPR_ATTR.search(authored):
        return authored
    attrs = _REPR_ATTR.findall(frozen)
    decl = _ITEM_DECL.search(authored)
    if not attrs or decl is None:
        return authored
    indent = decl.group(1)
    carried = "".join(f"{indent}{a.strip()}\n" for a in attrs)
    return authored[:decl.start()] + carried + authored[decl.start():]


_DERIVE_ATTR = re.compile(r"^[ \t]*#\[\s*derive\s*\([^\]]*\)\s*\][ \t]*\n?", re.MULTILINE)


def carry_frozen_derives(frozen: str, authored: str) -> str:
    """A public type's `#[derive(...)]` list as the frozen declaration has it.

    A derive is a trait impl, and on a public type a trait impl is public API:
    the reconstruction keeps the crate's public surface, so the list is not
    the model's to change. The model designs the private fields of such a
    type (`PUB_FIELDS`), and `_public_contract_broken` already holds its head;
    the head parser skips attributes, so a derive the reply added or dropped
    used to land. On run `42057-641e589c` (2026-10-26) the reply for `Error`
    was `#[derive(Debug, PartialEq)] pub struct Error { ptr: NonNull<()> }`:
    `Debug` conflicts with the crate's hand-written impl (E0119), and
    `PartialEq` would have compiled and widened the public API.

    The frozen declaration covers every trait the crate's tests need of the
    type, because those tests compile against it: what it does not derive is
    implemented elsewhere by hand or not at all. So the reply's plain
    `#[derive]` attributes are replaced by the frozen ones, verbatim, before
    the item's declaration line. `cfg_attr`-wrapped derives are left as they
    are, the same scope `derive_traits` reads."""
    stripped = _DERIVE_ATTR.sub("", authored)
    attrs = [m.group(0).strip() for m in _DERIVE_ATTR.finditer(frozen)]
    decl = _ITEM_DECL.search(stripped)
    if decl is None:
        return authored
    indent = decl.group(1)
    carried = "".join(f"{indent}{a}\n" for a in attrs)
    return stripped[:decl.start()] + carried + stripped[decl.start():]


_CFG_HEAD = re.compile(r"#\[\s*cfg\s*\(")


def _attribute_prefix(text: str) -> tuple[list[str], list[tuple[int, int, str]], int] | None:
    """``text``'s lines, the outer attributes in front of its item (first
    line, last line, the attribute joined to one line), and the index of the
    item's first line. Blank lines and comments in front of the item belong
    to the prefix. None when no line follows the prefix (no item)."""
    lines = text.split("\n")
    attrs: list[tuple[int, int, str]] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("//"):
            i += 1
            continue
        if not stripped.startswith("#["):
            return lines, attrs, i
        depth, j, buf = 0, i, []
        while j < len(lines):
            part = lines[j].strip()
            buf.append(part)
            depth += attr_bracket_delta(part)
            if depth <= 0:
                break
            j += 1
        else:
            return None
        attrs.append((i, j, " ".join(buf)))
        i = j + 1
    return None


def carry_frozen_cfg(frozen: str, authored: str) -> str:
    """The item's own `#[cfg(...)]` gate as the frozen definition has it.

    Whether an item exists in a world is a fact of the crate, not a layout
    choice. On run `42057-641e589c` (2026-10-26) the authored `Backtrace`
    enum dropped the frozen `#[cfg(not(feature = "std"))]`, so under `std`
    the skeleton held both the enum and `pub(crate) use std::backtrace::
    Backtrace`, and every attempt of the component failed E0255.

    Only the outer attributes in front of the item are read: the reply's
    `#[cfg(...)]` there are replaced by the frozen definition's, and removed
    when the frozen item has none. A `cfg` inside the body (a field, a
    variant, a statement) is the item's design and is left; so is
    `#[cfg_attr(...)]`, the same scope the derive carry has. Every item kind:
    the prefix is read off the text, not off a struct/enum/union matcher.
    Returns ``authored`` unchanged when the two gates already agree."""
    f = _attribute_prefix(frozen)
    a = _attribute_prefix(authored)
    if f is None or a is None:
        return authored
    want = [txt for _s, _e, txt in f[1] if _CFG_HEAD.match(txt)]
    lines, attrs, decl = a
    have = [(s, e, txt) for s, e, txt in attrs if _CFG_HEAD.match(txt)]
    if ["".join(x.split()) for x in want] == ["".join(t.split()) for _s, _e, t in have]:
        return authored
    drop = {k for s, e, _t in have for k in range(s, e + 1)}
    indent = lines[decl][:len(lines[decl]) - len(lines[decl].lstrip(" \t"))]
    out = [ln for k, ln in enumerate(lines[:decl]) if k not in drop]
    out += [f"{indent}{txt}" for txt in want]
    out += lines[decl:]
    return "\n".join(out)


def carry_frozen_facts(
    items: tuple[PrivateSurfaceItem, ...], authored: Mapping[str, AuthoredItem],
) -> dict[str, AuthoredItem]:
    """``authored`` (keyed by protocol uid) with the facts of the crate that
    are not layout choices put back: the frozen ``#[repr(...)]``
    (:func:`carry_frozen_repr`), the frozen module-external visibility
    (:func:`frozen_visibility.carry_frozen_visibility`), and on a public type
    its frozen ``#[derive(...)]`` list (:func:`carry_frozen_derives`).

    This dict is the ONE text every consumer of a committed layout stores:
    the splice writes it, and the registry, the layout store's locate keys and
    the component's authored items are built from it. Carrying only inside
    the splice left those stores on the model's text, and the cluster-start
    reconciliation reinstalls a stored definition the live tree does not
    contain verbatim.

    A definition stored before this carry ran is not rewritten by
    ``setdefault`` (the first cluster to reach a uid wins). Readers of an
    already-stored definition go through :func:`carry_stored_definition`,
    which is this function on one item, so the match, the install and the
    store share the carried text.

    ``PUB_FIELDS`` items are not given visibility: their frozen fields are
    elided (there is nothing to carry), and `_public_contract_broken` refuses
    a field turned ``pub`` on a public type. They are given their derives."""
    out = dict(authored)
    for uid, it in zip(component_uids(items), items, strict=True):
        block = out.get(uid)
        if block is None:
            continue
        text = _carry_item(it, block.definition)
        if text != block.definition:
            out[uid] = replace(block, definition=text)
    return out


def carry_stored_definition(item: PrivateSurfaceItem, stored: str) -> str:
    """``stored`` with this item's frozen repr and visibility carried.

    The text a match, an install and a store share. An entry already in the
    registry stays there (``setdefault`` does not replace it), and the
    cluster-start reconciliation used to treat that stored text as installed
    whenever the live file still contained it — a field the frozen definition
    exposed outside its module stayed private for the life of the tree, and
    the next seed copied it back out of the skeleton. Fn items are returned
    unchanged: their stored key is a signature the fill loop rewrites, and
    reconciliation does not reinstall them."""
    if item.kind is SurfaceItemKind.FN_SIG:
        return stored
    return _carry_item(item, stored)


def _carry_item(item: PrivateSurfaceItem, stored: str) -> str:
    text = carry_frozen_cfg(item.definition, stored)
    text = carry_frozen_repr(item.definition, text)
    if item.kind is SurfaceItemKind.PUB_FIELDS:
        text = carry_frozen_derives(item.definition, text)
    else:
        text = carry_frozen_visibility(item.definition, text)
    return text


def derive_traits(definition: str) -> frozenset[str]:
    """The trait names inside every plain ``#[derive(...)]`` attribute of
    ``definition``. Token scan; ``cfg_attr``-wrapped derives are deliberately
    not read — they are conditional, and a guard that treats a conditional
    derive as unconditional would demand it where its cfg is off."""
    out: set[str] = set()
    i = 0
    while True:
        j = definition.find("#[derive", i)
        if j < 0:
            return frozenset(out)
        k = definition.find("(", j)
        if k < 0:
            return frozenset(out)
        depth = 0
        e = k
        while e < len(definition):
            if definition[e] == "(":
                depth += 1
            elif definition[e] == ")":
                depth -= 1
                if depth == 0:
                    break
            e += 1
        out.update(t.strip() for t in definition[k + 1:e].split(",")
                   if t.strip())
        i = e + 1


def _head_is_pub(definition: str) -> bool:
    """True when the item's declaration line begins with ``pub`` (any form),
    skipping doc comments and attributes."""
    for line in definition.splitlines():
        s = line.strip()
        if not s or s.startswith("#[") or s.startswith("//"):
            continue
        return s.startswith("pub ") or s.startswith("pub(")
    return False


def layout_regressions(current: str, authored: str) -> tuple[str, ...]:
    """The ways ``authored`` is strictly worse than the ``current`` definition
    it would replace: derives lost, or a ``pub`` item made private.

    For the RE-author path only. The current layout is one that COMPILED and
    was accepted carrying these properties, so a replacement dropping one is a
    regression, not a design choice — observed live on 2026-08-11 (arm C,
    microcrate): re-author attempt 1 produced `Counter` with
    `#[derive(Debug, Default, Clone, PartialEq, Eq)]`, a later attempt on the
    same item produced no derive at all, and another produced a private
    struct (`error[E0603]`). Nothing compared the replacement against what it
    replaced; each regression cost a sweep to rediscover by compile error —
    or, for a derive only a test binary needs, was not a compile error at
    all. Initial authoring deliberately does NOT run this: there the
    reference text is the frozen original, and a redesigned layout may
    legitimately not carry its every derive."""
    out = [f"dropped `#[derive({t})]` the replaced layout carries"
           for t in sorted(derive_traits(current) - derive_traits(authored))]
    if _head_is_pub(current) and not _head_is_pub(authored):
        out.append("made a `pub` item private")
    return tuple(out)


def definition_replacements(
    items: tuple[PrivateSurfaceItem, ...],
    authored: Mapping[str, AuthoredItem],
) -> tuple[dict[str, list[DefReplacement]], dict[str, list[str]], str]:
    """Per-file `(locate-key → authored text)` replacement plan for ONE
    component, plus the parallel uid lists (so a locate miss can name the uid
    that missed) and a non-empty reason when an item has no authored block.

    Shared by every tree the layout must land in. A re-author that reaches only
    one of them is silent drift, not a re-author: the skeleton is what a later
    cluster seeds from, the filled tree is what cargo actually compiles, and a
    type definition present in one and absent from the other builds a crate
    nobody authored."""
    per_file: dict[str, list[DefReplacement]] = {}
    per_file_uids: dict[str, list[str]] = {}
    for uid, it in zip(component_uids(items), items, strict=True):
        block = authored.get(uid)
        if block is None:
            return {}, {}, f"authored blocks missing uid {uid}"
        # Fn definitions are enumerated as bare signatures (no vis/`unsafe`
        # prefix, no stub body) — name the fn so the splice swaps the whole
        # item instead of nesting the new def inside the old one.
        fn_name = it.name if it.kind is SurfaceItemKind.FN_SIG else ""
        per_file.setdefault(it.file, []).append(
            DefReplacement(it.definition, block.definition, fn_name))
        per_file_uids.setdefault(it.file, []).append(uid)
    return per_file, per_file_uids, ""


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
    SurfaceItemKind.PUB_FIELDS: "struct",
}


def private_fn_matches(
    fn_records: list[FnRecord], item: PrivateSurfaceItem,
) -> list[FnRecord]:
    """The live records a PRIVATE surface fn may bind to: same file and name, and
    not a strictly-`pub` item. Visibility is the discriminator because a private
    surface item carries no container, and a public twin of the same name in the
    same file is routine:

        impl Error     { pub fn backtrace(&self) -> &Backtrace { ... } }        // frozen API
        impl ErrorImpl { pub(crate) unsafe fn backtrace(this: Ref<Self>) ... }  // the layout item

    Binding on (file, name) alone takes the FIRST — the public one — and the
    caller then splices the authored private definition over the frozen public
    method, deleting it from the crate. `Error::chain`/`Error::backtrace` were
    lost exactly this way, leaving `Error is not an iterator` on every sweep."""
    return [r for r in fn_records
            if r.file == item.file and r.fn_name == item.name
            and not r.is_exactly_pub]


def unlocatable_private_fns(
    items: tuple[PrivateSurfaceItem, ...], fn_records: list[FnRecord],
) -> tuple[str, ...]:
    """Names of the FN_SIG `items` with fewer private records than occurrences
    needed — why `derive_locate_keys` returned None, for the caller's log."""
    need: dict[tuple[str, str], int] = {}
    out: list[str] = []
    for it in items:
        if it.kind is not SurfaceItemKind.FN_SIG:
            continue
        k = (it.file, it.name)
        need[k] = need.get(k, 0) + 1
        if need[k] > len(private_fn_matches(fn_records, it)):
            out.append(it.name)
    return tuple(out)


def unlocatable_items(
    items: Sequence[PrivateSurfaceItem],
    read_file: Callable[[str], str | None],
) -> tuple[str, ...]:
    """Names of `items` whose locate-key text is NOT in the file it names —
    i.e. the splice that is about to be attempted will reject, whatever the
    model replies.

    WHY THIS RUNS BEFORE THE MODEL CALL. A stored locate-key is the text a
    re-author matches against the LIVE skeleton, and it can be stale: it is
    written from what the model AUTHORED, and any later rewrite of that file
    (a re-bake, a reconciliation, a splice that landed something else) moves
    the bytes out from under it. `replace_surface_definitions` then reports
    `could not locate a frozen definition to replace`, which
    `is_mechanical_reason` correctly calls attempt-invariant — and the whole
    fire dies, including the components that authored perfectly.

    MEASURED, cluster `stable` of `tmp/anyhow` on 2026-08-16. Two rows of
    `.skeleton/_layout_outcomes_stable.json` cannot be found in
    `.skeleton/stable/src/ptr.rs`: `CastTo`'s key is the trait FOLLOWED BY
    `impl<T> CastTo for T`, and the skeleton holds the trait alone (the blanket
    impl exists only in the filled tree, where a fill wrote it); `Mut`'s key
    spells `core::ptr::NonNull<T>` / `core::marker::PhantomData` where the
    skeleton — which imports both — reads `NonNull<T>` / `PhantomData`. Their
    siblings `Own` and `Ref` in the same file carry the qualified spelling and
    match, which is why only those two components failed. Every layout
    retrigger of that cluster had died on them since at least run
    `669347-cf33eae3` (2026-08-15), each costing a 31-name component's
    authoring round and landing nothing.

    Occurrence-stepped like the splice itself (`replace_surface_definitions`):
    two items sharing one definition text need two occurrences in the file, and
    checking them independently would pass a file that has only one."""
    missing: list[str] = []
    cursor: dict[tuple[str, str], int] = {}
    for it in items:
        text = read_file(it.file)
        if text is None:
            missing.append(it.name)
            continue
        at = cursor.get((it.file, it.definition), 0)
        span = find_definition_span(text, it.definition, start_at=at)
        if span is None:
            missing.append(it.name)
            continue
        cursor[(it.file, it.definition)] = span[1]
    return tuple(missing)


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
    tolerant matcher locates them identically. Same-named PRIVATE fns in one
    file bind by source order, matching the splice's occurrence ordering. None
    when any item has no matching record: the caller must skip, never guess.

    Pass the whole component in ONE call: the occurrence stepping below is what
    keeps two same-named private items off the same span, and it resets on
    every call."""
    keys: list[tuple[str, str]] = []
    fn_taken: dict[tuple[str, str], int] = {}
    for uid, it in zip(component_uids(items), items, strict=True):
        if it.kind is SurfaceItemKind.FN_SIG:
            matches = private_fn_matches(fn_records, it)
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


def adopt_component(
    uids: tuple[str, ...], registry: Mapping[str, AuthoredItem],
) -> dict[str, AuthoredItem] | None:
    """The already-fixed definitions for EVERY uid of one component, or None
    when the registry does not cover all of them.

    A crate compiles as ONE source tree: each private struct/fn has exactly one
    definition, whatever cfg-cluster reached it first. When a later cluster is
    seeded with an earlier cluster's filled bodies, those bodies name the
    earlier cluster's INVENTED field names — re-authoring the same type
    independently makes every seeded body reference a field that no longer
    exists (`no field lifetime on Ref`, `no field object_boxed on ErrorVTable`)
    and the whole cluster stops compiling. Adoption is therefore the default and
    re-authoring the fallback, never the other way round."""
    picked = {uid: registry[uid] for uid in uids if uid in registry}
    return picked if len(picked) == len(uids) and uids else None


def shared_definitions_text(items: Sequence[AuthoredItem]) -> str:
    """The already-fixed definitions rendered for a prompt, verbatim.

    Used when a component is only PARTLY covered by the registry: the model
    must extend the crate's existing layout (adding what this cluster's cfg
    needs) instead of redesigning it, so it needs the exact current text of the
    definitions it may not change."""
    return "\n\n".join(f"// {i.kind} {i.name} — already fixed crate-wide, "
                       f"reproduce VERBATIM\n{i.definition}" for i in items)
