"""Layout-authoring prompt (F11 t1.5) — asks the model to DESIGN a crate's
private layout instead of reproducing the original's.

The freeze-scope bug forced the model to satisfy the original's hand-rolled
unsafe layout (anyhow's vtable type-erasure) that no test asks for. This prompt
gives the model only what it is legally allowed (Hard Rule 11): the frozen
PUBLIC surface it must keep, the layout-pinning tests it must satisfy, and the
in-scope symbol inventory — then invites it to invent ANY private layout that
compiles and satisfies the pins. The original private layout is NEVER shown.

Pure text→text; no IO.
"""
from __future__ import annotations

from pipeline_core.domain.behaviour import BehaviourSpec
from pipeline_core.domain.private_surface import PrivateSurfaceItem

_PROTOCOL = (
    "Reply with ONE block per private item you are authoring, and NOTHING else:\n"
    '<LAYOUT_ITEM name="ItemName" kind="struct|enum|type|trait|fn">\n'
    "<the full Rust definition — for a struct/enum include all fields/variants;\n"
    " for a fn emit only the signature followed by `{ unimplemented!() }`>\n"
    "</LAYOUT_ITEM>\n"
)

_RULES = (
    "RULES:\n"
    "- You MUST author a block for EVERY item listed under ITEMS TO AUTHOR.\n"
    "- You are FREE to choose any representation — you are NOT reproducing the\n"
    "  original library's internals. A simple safe layout is preferred over an\n"
    "  unsafe one whenever it satisfies the pins (e.g. a boxed trait object\n"
    "  instead of a hand-rolled vtable).\n"
    "- The PUBLIC items below are frozen: keep their names/signatures; you only\n"
    "  design the PRIVATE types their fields point at.\n"
    "- Every LAYOUT-PINNING TEST must pass under your layout (size/align/\n"
    "  autotraits/niche). Design to satisfy them by construction.\n"
    "- Use ONLY the symbols in the IN-SCOPE INVENTORY plus std/core.\n"
    "- Give every private fn body `{ unimplemented!() }` — bodies are filled later.\n"
)


def _fmt_pins(pins: list[BehaviourSpec]) -> str:
    if not pins:
        return "(none — the layout is behaviour-pinned only)"
    lines = []
    for p in pins:
        expr = p.then.static_expr if p.then else p.when
        lines.append(f"- {expr}")
    return "\n".join(lines)


def _fmt_items(items: tuple[PrivateSurfaceItem, ...]) -> str:
    return "\n".join(
        f"- {it.name} ({it.kind.name.lower()}) in {it.file}" for it in items)


def build_layout_authoring_prompt(
    *,
    module_name: str,
    cfg_cluster: str,
    items: tuple[PrivateSurfaceItem, ...],
    public_surface: str,
    layout_pins: list[BehaviourSpec],
    symbol_inventory: str,
) -> str:
    """Assemble the t1.5 prompt for one connected component of the private
    surface. `items` are the replaceable items the model must author."""
    return "\n".join([
        f"You are designing the PRIVATE layout of the `{module_name}` crate "
        f"(cfg cluster: {cfg_cluster}).",
        "",
        "Your job: author the private types and private helper signatures that "
        "the public API points at, choosing WHATEVER internal representation is "
        "simplest and satisfies the constraints. Do not try to match any "
        "particular library's internals — invent your own scheme.",
        "",
        "=== FROZEN PUBLIC SURFACE (keep these; design the private types they use) ===",
        public_surface.strip() or "(none)",
        "",
        "=== LAYOUT-PINNING TESTS (your layout MUST satisfy every one) ===",
        _fmt_pins(layout_pins),
        "",
        "=== IN-SCOPE SYMBOL INVENTORY ===",
        symbol_inventory.strip() or "(std/core only)",
        "",
        "=== ITEMS TO AUTHOR ===",
        _fmt_items(items),
        "",
        _RULES,
        "",
        _PROTOCOL,
    ])
