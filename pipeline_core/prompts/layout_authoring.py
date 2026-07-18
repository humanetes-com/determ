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

from pipeline_core.domain.authored_layout import component_uids
from pipeline_core.domain.behaviour import BehaviourSpec
from pipeline_core.domain.private_surface import PrivateSurfaceItem

_PROTOCOL = (
    "Reply with ONE block per private item you are authoring, and NOTHING else:\n"
    '<LAYOUT_ITEM name="ItemName" kind="struct|enum|type|trait|fn">\n'
    "<the full Rust definition — for a struct/enum include all fields/variants;\n"
    " for a fn emit only the signature followed by `{ unimplemented!() }`>\n"
    "</LAYOUT_ITEM>\n"
    "\n"
    "The name attribute must copy the listed id EXACTLY — including any `.N`\n"
    "suffix that distinguishes same-named items on different impls. The Rust\n"
    "code inside the block always uses the plain name.\n"
    'WRONG (suffix dropped — cannot tell the two apart): <LAYOUT_ITEM name="as_ref" kind="fn">\n'
    'RIGHT: <LAYOUT_ITEM name="as_ref.2" kind="fn">\n'
    "fn as_ref(&self) -> &(dyn std::error::Error + 'static) { unimplemented!() }\n"
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


def _fmt_items(
    items: tuple[PrivateSurfaceItem, ...], uids: tuple[str, ...],
) -> str:
    """One listing line per item, keyed by protocol uid. The frozen first line
    of the current definition is shown so the model can tell same-named items
    apart (two `as_ref` on different impls differ only by signature)."""
    lines = []
    for uid, it in zip(uids, items, strict=True):
        head = it.definition.strip().splitlines()[0] if it.definition.strip() else ""
        lines.append(f"- {uid} ({it.kind.name.lower()}) in {it.file} — currently: {head}")
    return "\n".join(lines)


# Cause-C (runtime crash) authoring guidance: the frozen scheme compiles but
# segfaults through raw fn pointers — steer the redesign toward compiler-checked
# safety, in generic placeholder terms only (never a real library's internals).
CRASH_GUIDANCE = """=== RUNTIME-CRASH GUIDANCE ===
The previous design for these items compiled cleanly but CRASHED at runtime
(segfault through a function pointer). Prefer a scheme the compiler can check
end-to-end. Concrete pattern to avoid vs prefer (placeholder names):

WRONG (compiles, crashes — hand-rolled fn-pointer table + raw casts):
struct VTable { drop_fn: unsafe fn(*mut ()), view_fn: unsafe fn(*const ()) -> *const () }
struct Holder { ptr: *mut (), vtable: &'static VTable }
// every call site casts through *mut () — one inconsistent assumption segfaults

RIGHT (all safe code, still one machine word for the public wrapper):
struct Inner { source: Box<dyn std::error::Error + Send + Sync + 'static>, notes: Vec<String> }
struct Holder { inner: Box<Inner> }
// the OUTER Box keeps Holder pointer-sized even though Inner holds a wide dyn Box
// downcasting is built in: holder.inner.source.downcast_ref::<T>()

You must still author EVERY listed item by name; an item your design makes
unnecessary may become a minimal safe stub that nothing calls."""


def build_layout_authoring_prompt(
    *,
    module_name: str,
    cfg_cluster: str,
    items: tuple[PrivateSurfaceItem, ...],
    public_surface: str,
    layout_pins: list[BehaviourSpec],
    symbol_inventory: str,
    extra_guidance: str = "",
    authored_scheme: str = "",
    item_uids: tuple[str, ...] | None = None,
) -> str:
    """Assemble the t1.5 prompt for one connected component of the private
    surface (or ONE CHUNK of a large component — `items` is then the chunk and
    `item_uids` carries the COMPONENT-scoped protocol uids, which a chunk
    cannot recompute from its own subset). `extra_guidance` (e.g.
    CRASH_GUIDANCE) is appended before the rules. `authored_scheme` carries
    the definitions already authored by earlier chunks of the same pass —
    fixed context the new items must fit."""
    uids = item_uids if item_uids is not None else component_uids(items)
    return "\n".join([s for s in [
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
        ("=== ALREADY AUTHORED THIS PASS (fixed — design consistently with "
         "these; do NOT re-emit them) ===") if authored_scheme.strip() else None,
        authored_scheme.strip() or None,
        "" if authored_scheme.strip() else None,
        "=== ITEMS TO AUTHOR ===",
        _fmt_items(items, uids),
        "",
        extra_guidance or None,
        "" if extra_guidance else None,
        _RULES,
        "",
        _PROTOCOL,
    ] if s is not None])
