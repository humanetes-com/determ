"""File-level `use` items — pure domain.

A body-only splice cannot bring an import with it. That is fine while bodies are
authored against the file they land in, and wrong the moment bodies MOVE: the
cross-cluster seed adopts a donor's body into a sibling bake of the same file,
and a body that names `MessageError` unqualified compiles in the donor only
because the donor's fill added `use crate::wrapper::MessageError;` at the top.

Observed live (`anyhow`, cluster `nightly`, immediately after the slot fix landed):
121 bodies seeded correctly, then 41 rustc errors, every one of them
`E0425: cannot find ... in this scope` for `MessageError` / `DisplayError` —
two `use` lines that exist in `stable/src/error.rs` and not in the nightly bake.
Correct bodies, missing plumbing.

Copying is safe in the direction it is used. Donor and target are two bakes of
one file, so their import sets differ only by what a fill ADDED; a cfg-gated
`use` carries its own `#[cfg]` attribute and stays inert where it does not
apply; and every generated file opens with `#![allow(unused, dead_code, …)]`, so
an import this cluster happens not to need costs nothing. Items already present
by NAME are never re-added — a same-name import of a different path is the one
case where copying would make things worse (E0659), so it is left alone.
"""

from __future__ import annotations

from dataclasses import dataclass

from .rust_lex import code_mask

__all__ = ["UseItem", "use_items", "missing_use_items", "insert_use_items",
           "divergent_item_names"]

# The item kinds whose SHAPE a body can depend on. `fn` is excluded on purpose:
# a differing fn is a differing body, which is the thing being moved.
_SHAPE_KEYWORDS = ("struct", "enum", "trait", "type", "union")


def divergent_item_names(target_text: str, donor_text: str) -> frozenset[str]:
    """Items both files declare but SHAPE differently.

    A private layout is authored per cluster (`_layout_outcomes_<cluster>.json`),
    while filled bodies are shared across clusters ("fill once, propagate").
    Those two designs contradict each other the moment two clusters author the
    same type differently, and they do: measured on `anyhow`, `stable` authored
    `ErrorImpl` as `{vtable, backtrace, _error}` and `nightly` authored the same
    type as `{vtable, _data}` — independently, both recorded AUTHORED. A body
    written against one cannot compile against the other, and no per-fn fill can
    repair it because the fix is a DECLARATION, not a body.

    So the seed needs to know which names it must not move a body through. This
    is that set: declared in both, and not textually identical. Whitespace is
    normalised because the two files are rendered by the same extractor but not
    necessarily wrapped identically.

    The set also carries the donor-only FIELD names of each divergent type.
    A body reaches a field through the receiver — `self._error`, never
    `ErrorImpl::_error` — so type names alone caught only two thirds of the
    damage (31 rustc errors down to 11, the remainder all `no field '_error'` /
    `field 'ptr' is private`). What a body depends on is the field, so the field
    is what has to be matched."""
    from .rust_scan import item_names, locate_item_declaration

    shared = set(item_names(target_text)) & set(item_names(donor_text))
    out: set[str] = set()
    for name in shared:
        for kw in _SHAPE_KEYWORDS:
            t_span = locate_item_declaration(target_text, kw, name)
            d_span = locate_item_declaration(donor_text, kw, name)
            if t_span is None or d_span is None:
                continue
            t_raw = target_text[t_span[0]:t_span[1]]
            d_raw = donor_text[d_span[0]:d_span[1]]
            if " ".join(t_raw.split()) != " ".join(d_raw.split()):
                out.add(name)
                # EVERY field of a divergent type, not just the donor-only ones.
                # A field can change without changing its name: `Mut.ptr` is
                # declared in both and `pub(crate)` in only one, so a body using
                # it compiled in the donor and is `E0616 field 'ptr' is private`
                # here. Same name, different meaning — which is the whole class.
                out |= _field_names(d_raw) | _field_names(t_raw)
            break
    return frozenset(out)


def _field_names(decl: str) -> set[str]:
    """Field names declared in a struct/enum body — the `ident` of each
    `ident: Type`. Token-level, so a `:` inside a generic bound or a path
    (`crate::backtrace::Backtrace`) contributes nothing."""
    out: set[str] = set()
    for chunk in decl.replace("{", " ").replace("}", " ").replace(",", " ").split():
        if chunk.endswith(":") and not chunk.endswith("::"):
            ident = chunk[:-1]
            if ident and (ident[0].isalpha() or ident[0] == "_") and \
                    all(c.isalnum() or c == "_" for c in ident):
                out.add(ident)
    return out


@dataclass(frozen=True, slots=True)
class UseItem:
    """One top-level `use` declaration, with any `#[cfg(…)]` attributes that
    gate it — the attributes are part of the item, not decoration: copied
    without them a nightly-only import becomes a hard error in `stable`."""

    text: str          # the full item, attributes included, no trailing newline
    imported: str      # the last path segment / alias — the name it binds

    @property
    def is_glob(self) -> bool:
        return self.imported == "*"


def _binding_name(use_line: str) -> str:
    """The name a single-path `use` binds: its alias, else its last segment.
    Brace groups and globs return `*` — deliberately never matched as a name, so
    a grouped import is copied only when its exact text is absent."""
    body = use_line.rstrip().rstrip(";").strip()
    if body.startswith("use "):
        body = body[4:].strip()
    if "{" in body or body.endswith("*"):
        return "*"
    if " as " in body:
        return body.rsplit(" as ", 1)[1].strip()
    return body.rsplit("::", 1)[-1].strip()


def use_items(text: str) -> list[UseItem]:
    """Top-level `use` declarations in source order, each carrying the `#[…]`
    attribute lines directly above it.

    Line-oriented and comment-aware: a `use` inside a nested module or a doc
    comment is not a file-level import and must not be treated as one — copying
    one out of its module changes what it means."""
    mask = code_mask(text)
    out: list[UseItem] = []
    offset = 0
    depth = 0
    pending: list[str] = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        line_start = offset
        offset += len(line)
        masked_start = mask[line_start] if line_start < len(mask) else False
        if depth == 0 and not masked_start and stripped.startswith("use "):
            item = "\n".join([*pending, line.rstrip("\n")])
            out.append(UseItem(text=item, imported=_binding_name(stripped)))
            pending = []
        elif depth == 0 and not masked_start and stripped.startswith("#["):
            pending.append(line.rstrip("\n"))
        elif stripped:
            pending = []
        # Track brace depth over CODE only, so a `use` inside `mod x { … }` is
        # not mistaken for a file-level one.
        for i in range(line_start, min(offset, len(mask))):
            if mask[i]:
                continue
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth = max(depth - 1, 0)
    return out


def missing_use_items(target_text: str, donor_text: str) -> list[UseItem]:
    """Donor imports the target lacks — by exact text, and by bound NAME for the
    single-path ones.

    Name matching is what makes this idempotent across formatting: the target
    may already import the same item as part of a brace group, or under the
    other of `crate::`/`super::`. Re-adding it would be an E0252 duplicate, and
    a same-name import of a DIFFERENT path is a real conflict this must not
    create — either way, "the name is already bound here" means leave it."""
    have_text = {u.text.strip() for u in use_items(target_text)}
    have_names = {u.imported for u in use_items(target_text) if not u.is_glob}
    out: list[UseItem] = []
    for u in use_items(donor_text):
        if u.text.strip() in have_text:
            continue
        if not u.is_glob and u.imported in have_names:
            continue
        out.append(u)
    return out


def insert_use_items(text: str, items: list[UseItem]) -> str:
    """`text` with `items` inserted after its last top-level `use`, or after the
    leading inner attributes (`#![…]`) when it has none.

    Never at byte 0: an import placed above `#![allow(…)]` makes the inner
    attribute no longer the first item in the file, which is a hard error."""
    if not items:
        return text
    block = "\n".join(i.text for i in items) + "\n"
    existing = use_items(text)
    lines = text.splitlines(keepends=True)
    if existing:
        last = existing[-1].text.splitlines()[-1]
        for n, line in enumerate(lines):
            if line.rstrip("\n") == last:
                insert_at = n + 1
                break
        else:
            insert_at = len(lines)
    else:
        insert_at = 0
        for n, line in enumerate(lines):
            s = line.strip()
            if s.startswith("#!["):
                insert_at = n + 1
            elif s:
                break
    return "".join(lines[:insert_at]) + block + "".join(lines[insert_at:])
