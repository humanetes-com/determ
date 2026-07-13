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
parser; attribute values are HTML-unescaped (models emit XML-safe generics).
"""
from __future__ import annotations

import html
from dataclasses import dataclass

from pipeline_core.domain.private_surface import PrivateSurfaceItem

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
        definition = reply[gt + 1:close].strip()
        if name.strip() and definition:
            items.append(AuthoredItem(name.strip(), kind.strip(), definition))
        i = close + len(_CLOSE)
    return AuthoredLayout(items=tuple(items))


def layout_coverage(
    required: tuple[PrivateSurfaceItem, ...], layout: AuthoredLayout,
) -> tuple[frozenset[str], frozenset[str]]:
    """(missing, extra): required item names the layout did NOT author, and
    authored names not in the required set. A non-empty ``missing`` means the
    authored scaffold is incomplete and must be re-requested — never spliced."""
    req = frozenset(i.name for i in required)
    got = layout.names
    return req - got, got - req
