"""The frozen module-external visibility of a type, carried onto its authored
replacement.

Who may name a field from another module is a fact of the crate's module
graph, not a layout choice. The original reached a value of the type from
another file only because its fields were visible there. t1.5 may redesign
private fields, but a replacement that drops that visibility leaves a body in
another file with no route to construct or read the type, and no later stage
can repair it. The accept gate compiles a stubbed skeleton, where no body
constructs anything. A fill writes bodies only, never a field's visibility.
The layout retrigger does not read privacy diagnostics.

Run `673664-014fd53a` (2026-10-14, cluster `stable`): the baked private
surface records `#[repr(transparent)] pub struct DisplayError<M>(pub M);` in
`wrapper.rs`. The authored replacement came back
`pub struct DisplayError<M>(M);`. The constructor that wraps a message in it
lives in `error.rs`, and every route that tried it failed on
`error[E0603]: tuple struct constructor DisplayError is private` (impl in
fills 2 and 3, per-fn in fill 5), holding the 13-15 tests whose first failure
it was. None of 17 earlier archived layouts dropped a visibility this way: a
sampling event with no guard.

Two rules, both one-directional (a visibility is only ever raised back to
what the frozen definition had):

1. Constructibility. When every frozen field carries a visibility wider than
   private, a struct literal from another module was possible, so every
   authored field is raised to at least the narrowest of those visibilities,
   whatever fields the replacement designs.
2. Kept members. A field the replacement keeps (same name, or the same index
   of a tuple struct of the same arity) and the item head are never narrower
   than their own frozen visibility.

Visibilities compare along private < `pub(super)` < `pub(crate)` < `pub`;
`pub(in path)` compares only with the same path, with private and with
`pub`. When the frozen visibilities are not comparable nothing is carried by
rule 1. Pure text, mask-aware: comments and strings never parse as code.
"""
from __future__ import annotations

from dataclasses import dataclass

from pipeline_core.domain.rust_lex import code_mask
from pipeline_core.domain.type_header import field_block_span

_IDENT_START = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_IDENT_CHARS = _IDENT_START | set("0123456789")
_RANK = {"": 0, "pub(self)": 0, "pub(super)": 1, "pub(crate)": 2, "pub": 3}
_OPEN = {"(": ")", "[": "]", "{": "}", "<": ">"}


@dataclass(frozen=True, slots=True)
class _Vis:
    """A visibility as written: ``text`` normalised (``pub ( crate )`` reads
    ``pub(crate)``, private reads ``""``), and the span it occupies — for a
    private member an empty span at the position a visibility would go."""

    text: str
    start: int
    end: int
    name: str = ""


def narrower(a: str, b: str) -> bool | None:
    """True when visibility ``a`` is strictly narrower than ``b``; None when
    the two are not comparable."""
    if a == b:
        return False
    ra, rb = _RANK.get(a), _RANK.get(b)
    if ra is not None and rb is not None:
        return ra < rb
    if _RANK.get(a) == 0 or b == "pub":
        return True
    if _RANK.get(b) == 0 or a == "pub":
        return False
    return None


def _narrowest(vs: list[str]) -> str | None:
    out = vs[0]
    for v in vs[1:]:
        n = narrower(v, out)
        if n is None:
            return None
        if n:
            out = v
    return out


def _skip_ws_and_attrs(text: str, mask: list[bool], i: int, end: int) -> int:
    """First code position at or after ``i`` that is not whitespace, a
    comment, or an outer attribute ``#[...]``."""
    while i < end:
        if mask[i] or text[i].isspace():
            i += 1
            continue
        if text[i] == "#":
            j = i + 1
            while j < end and (mask[j] or text[j].isspace()):
                j += 1
            if j < end and text[j] == "[":
                i = _close_of(text, mask, j, end) + 1
                continue
        return i
    return end


def _close_of(text: str, mask: list[bool], i: int, end: int) -> int:
    """Index of the bracket closing the one at ``i`` (same kind)."""
    want = _OPEN[text[i]]
    opener = text[i]
    depth = 0
    j = i
    while j < end:
        if not mask[j]:
            if text[j] == opener:
                depth += 1
            elif text[j] == want:
                depth -= 1
                if depth == 0:
                    return j
        j += 1
    return end - 1


def _vis_at(text: str, mask: list[bool], i: int, end: int) -> _Vis:
    """The visibility written at code position ``i`` (empty when none)."""
    j = i
    while j < end and not mask[j] and text[j] in _IDENT_CHARS:
        j += 1
    if text[i:j] != "pub":
        return _Vis("", i, i)
    k = j
    while k < end and (mask[k] or text[k].isspace()):
        k += 1
    if k < end and text[k] == "(":
        close = _close_of(text, mask, k, end)
        inner = " ".join(text[k + 1:close].split())
        return _Vis(f"pub({inner})", i, close + 1)
    return _Vis("pub", i, j)


def head_visibility(text: str) -> _Vis | None:
    """The item head's visibility, after doc comments and attributes."""
    mask = code_mask(text)
    i = _skip_ws_and_attrs(text, mask, 0, len(text))
    if i >= len(text) or text[i] not in _IDENT_START:
        return None
    return _vis_at(text, mask, i, len(text))


def field_visibilities(text: str) -> tuple[str, list[_Vis]] | None:
    """(``"("`` or ``"{"``, one entry per field in order) for a struct's field
    block; None when the text declares no field block. A named field's entry
    carries its name."""
    span = field_block_span(text)
    if span is None:
        return None
    open_idx, close_idx = span
    mask = code_mask(text)
    kind = text[open_idx]
    fields: list[_Vis] = []
    start = open_idx + 1
    depth = 0
    i = start
    while i <= close_idx:
        ch = text[i] if i < close_idx else ","
        at_end = i == close_idx
        if not at_end and mask[i]:
            i += 1
            continue
        if not at_end and ch in "([{":
            depth += 1
        elif not at_end and ch in ")]}":
            depth -= 1
        elif not at_end and ch == "<":
            depth += 1
        elif not at_end and ch == ">" and text[i - 1] != "-":
            depth -= 1
        elif ch == "," and depth == 0:
            p = _skip_ws_and_attrs(text, mask, start, i)
            if p < i:
                v = _vis_at(text, mask, p, i)
                name = ""
                if kind == "{":
                    q = _skip_ws_and_attrs(text, mask, v.end, i)
                    r = q
                    while r < i and text[r] in _IDENT_CHARS:
                        r += 1
                    name = text[q:r]
                fields.append(_Vis(v.text, v.start, v.end, name))
            start = i + 1
        i += 1
    return kind, fields


def carry_frozen_visibility(frozen: str, authored: str) -> str:
    """``authored`` with every head and field visibility that is narrower
    than the frozen definition's raised back to it (the module docstring's
    two rules). Returns ``authored`` unchanged when nothing is narrower."""
    edits: list[tuple[int, int, str]] = []
    fh, ah = head_visibility(frozen), head_visibility(authored)
    if fh is not None and ah is not None and narrower(ah.text, fh.text):
        edits.append(_edit(ah, fh.text))
    ff, af = field_visibilities(frozen), field_visibilities(authored)
    if ff is not None and af is not None and ff[1] and af[1]:
        fkind, ffields = ff
        akind, afields = af
        floor: str | None = None
        if all(_RANK.get(f.text) != 0 for f in ffields):
            floor = _narrowest([f.text for f in ffields])
        own: dict[int, str] = {}
        if fkind == akind == "{":
            by_name = {f.name: f.text for f in ffields if f.name}
            own = {n: by_name[a.name] for n, a in enumerate(afields)
                   if a.name in by_name}
        elif fkind == akind == "(" and len(ffields) == len(afields):
            own = {n: f.text for n, f in enumerate(ffields)}
        for n, a in enumerate(afields):
            wanted = floor
            if n in own:
                if wanted is None:
                    wanted = own[n]
                else:
                    wider = narrower(wanted, own[n])
                    wanted = own[n] if wider else wanted
            if wanted is not None and narrower(a.text, wanted):
                edits.append(_edit(a, wanted))
    out = authored
    for start, end, new in sorted(edits, reverse=True):
        out = out[:start] + new + out[end:]
    return out


def _edit(v: _Vis, wanted: str) -> tuple[int, int, str]:
    if v.start == v.end:
        return v.start, v.end, f"{wanted} "
    return v.start, v.end, wanted
