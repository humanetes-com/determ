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

__all__ = ["UseItem", "bound_names", "insert_use_items", "missing_use_items",
           "names_bound", "use_items", "divergent_item_names"]

# The item kinds whose SHAPE a body can depend on. `fn` is excluded on purpose:
# a differing fn is a differing body, which is the thing being moved.
_SHAPE_KEYWORDS = ("struct", "enum", "trait", "type", "union")


def divergent_item_names(target_text: str, donor_text: str) -> frozenset[str]:
    """Names a body must not mention to be moved from `donor_text` into
    `target_text`: the types both files declare with a different SHAPE, and the
    fields of theirs that changed.

    A private layout is authored per cluster (`_layout_outcomes_<cluster>.json`),
    while filled bodies are shared across clusters ("fill once, propagate").
    Those two designs contradict each other the moment two clusters author the
    same type differently, and they do: measured on `anyhow`, `stable` authored
    `ErrorImpl` as `{vtable, backtrace, _error}` and `nightly` authored the same
    type as `{vtable, _data}` — independently, both recorded AUTHORED. A body
    written against one cannot compile against the other, and no per-fn fill can
    repair it because the fix is a DECLARATION, not a body.

    Shape, not text (`rust_item_shape.shape_divergence`): an inline bound and a
    `where` bound, a path spelled through an import and in full, a trailing
    comma and a doc comment are the same declaration. Comparing whitespace-
    normalised text refused 34 verified `stable` bodies at the `nightly` entry of
    run `119837-09e3ac57` (2026-09-15) over exactly those spellings, and read
    `where T: ?Sized` as a field named `T`.

    What goes in the set follows what a body can depend on. A type whose kind,
    attributes, generics or `#[repr(C)]` field order changed contributes its
    name and every field of both sides. A type whose fields changed only
    individually contributes those fields — a body reaches a field by name, and
    a field can change without changing its name (`Mut.ptr` declared in both,
    `pub(crate)` in only one, is `E0616 field 'ptr' is private`) — plus its own
    name when the field SET differs, because a struct literal written against
    the donor never names the field the target added. A declaration either side
    cannot parse keeps the conservative whole-type answer on a text difference."""
    from .rust_item_shape import (
        WHOLE,
        declaration_field_names,
        item_shape,
        shape_divergence,
    )
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
            t_shape = item_shape(target_text, kw, name)
            d_shape = item_shape(donor_text, kw, name)
            if t_shape is not None and d_shape is not None:
                verdict = shape_divergence(t_shape, d_shape)
            elif " ".join(t_raw.split()) != " ".join(d_raw.split()):
                verdict = WHOLE
            else:
                verdict = frozenset()
            if isinstance(verdict, frozenset):
                out |= verdict
            else:
                out.add(name)
                out |= (declaration_field_names(t_shape, t_raw)
                        | declaration_field_names(d_shape, d_raw))
            break
    return frozenset(out)


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

    Brace groups and globs return `*` so a grouped import is copied only when
    its exact text is absent. Whether any name it binds is already taken is
    :func:`names_bound`, which sees the members."""
    body = use_line.rstrip().rstrip(";").strip()
    if body.startswith("pub "):
        body = body[4:].strip()
    if body.startswith("use "):
        body = body[4:].strip()
    if "{" in body or body.endswith("*"):
        return "*"
    if " as " in body:
        return body.rsplit(" as ", 1)[1].strip()
    return body.rsplit("::", 1)[-1].strip()


def _use_statement(item_text: str) -> str:
    """The `use` line of one item, attributes stripped."""
    for line in item_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("use ") or stripped.startswith("pub use "):
            return stripped
    return ""


def names_bound(use_line: str) -> frozenset[str]:
    """Every simple name one `use` statement binds.

    A glob binds nothing nameable. A brace group binds each member, an alias
    instead of the member, and `self` as the last segment before the brace.
    A single path binds its alias or its last segment."""
    body = use_line.strip()
    if body.startswith("pub "):
        body = body[4:].strip()
    if body.startswith("use "):
        body = body[4:].strip()
    if body.endswith(";"):
        body = body[:-1].strip()
    brace = body.find("{")
    if brace >= 0 and "}" in body[brace:]:
        head = body[:brace].strip().rstrip(":")
        prefix = head.rsplit("::", 1)[-1].strip() if head else ""
        inner = body[brace + 1:body.find("}", brace)]
        names: set[str] = set()
        for part in inner.split(","):
            part = part.strip()
            if not part or part == "*":
                continue
            if part == "self":
                if prefix:
                    names.add(prefix)
                continue
            if " as " in part:
                alias = part.rsplit(" as ", 1)[1].strip()
                if alias:
                    names.add(alias)
                continue
            name = part.rsplit("::", 1)[-1].strip()
            if name:
                names.add(name)
        return frozenset(names)
    if body.endswith("*"):
        return frozenset()
    if " as " in body:
        alias = body.rsplit(" as ", 1)[1].strip()
        return frozenset({alias}) if alias else frozenset()
    name = body.rsplit("::", 1)[-1].strip()
    return frozenset({name}) if name else frozenset()


def bound_names(text: str) -> frozenset[str]:
    """Every simple name a top-level `use` in `text` binds."""
    out: set[str] = set()
    for item in use_items(text):
        out |= names_bound(_use_statement(item.text))
    return frozenset(out)


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
    """Donor imports the target lacks — by exact text, and by bound NAME.

    Name matching is what makes this idempotent across formatting: the target
    may already import the same item as part of a brace group, or under the
    other of `crate::`/`super::`. Re-adding it would be an E0252 duplicate, and
    a same-name import of a DIFFERENT path is a real conflict this must not
    create — either way, "the name is already bound here" means leave it. A
    glob binds no name, so it is copied only when its exact text is absent.
    One member of a brace group already bound refuses the whole item: inserting
    it would define that member twice."""
    have_text = {u.text.strip() for u in use_items(target_text)}
    have_names = bound_names(target_text)
    out: list[UseItem] = []
    for u in use_items(donor_text):
        if u.text.strip() in have_text:
            continue
        if names_bound(_use_statement(u.text)) & have_names:
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
