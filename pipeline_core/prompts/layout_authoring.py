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

from collections.abc import Collection, Mapping, Sequence

from pipeline_core.domain import behaviour as _behaviour
from pipeline_core.domain.authored_layout import component_uids
from pipeline_core.domain.behaviour import BehaviourSpec
from pipeline_core.domain.behaviour_select import (
    IDENTITY_MUST_CAP,
    behaviour_class_name,
    class_rank,
    select_identity_must,
)
from pipeline_core.domain.private_surface import PrivateSurfaceItem, SurfaceItemKind

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
    "\n"
    "A `kind=\"fn\"` block is spliced IN PLACE, inside the `impl`/`trait` the fn\n"
    "already lives in. So emit the BARE fn item — never wrap it in an `impl` or\n"
    "`trait` header, or the result nests one impl inside another and rustc\n"
    "rejects it with `implementation is not supported in `trait`s or `impl`s`.\n"
    'WRONG: <LAYOUT_ITEM name="drop_inner" kind="fn">\n'
    "impl Inner { fn drop_inner(&mut self) { unimplemented!() } }\n"
    "</LAYOUT_ITEM>\n"
    'RIGHT: <LAYOUT_ITEM name="drop_inner" kind="fn">\n'
    "fn drop_inner(&mut self) { unimplemented!() }\n"
    "</LAYOUT_ITEM>\n"
    'WRONG: <LAYOUT_ITEM name="width" kind="fn">\n'
    "impl Layout for Grid { unsafe fn width(p: NonNull<Cell>) -> usize { unimplemented!() } }\n"
    "</LAYOUT_ITEM>\n"
    'RIGHT: <LAYOUT_ITEM name="width" kind="fn">\n'
    "unsafe fn width(p: NonNull<Cell>) -> usize { unimplemented!() }\n"
    "</LAYOUT_ITEM>\n"
    "(A `kind=\"struct\"|\"enum\"|\"trait\"` block IS the whole item — those DO\n"
    " carry their own `struct`/`trait` header. Only `kind=\"fn\"` is bare.)\n"
)

_RULES = (
    "RULES:\n"
    "- You MUST author a block for EVERY item listed under ITEMS TO AUTHOR.\n"
    "- You are FREE to choose any representation — you are NOT reproducing the\n"
    "  original library's internals. A simple safe layout is preferred over an\n"
    "  unsafe one whenever it satisfies the pins (e.g. a boxed trait object\n"
    "  instead of a hand-rolled vtable).\n"
    "- The PUBLIC items below are frozen: keep their names/signatures. You\n"
    "  design the PRIVATE types their fields point at — plus, for any public\n"
    "  struct explicitly listed under ITEMS TO AUTHOR, its private fields.\n"
    "- Every LAYOUT-PINNING TEST must pass under your layout (size/align/\n"
    "  autotraits/niche). Design to satisfy them by construction.\n"
    "- Use ONLY the symbols in the IN-SCOPE INVENTORY plus std/core.\n"
    "- Give every private fn body `{ unimplemented!() }` — bodies are filled later.\n"
)


# The same class of defect as the auto-trait rule below, for traits that are
# DERIVED rather than inferred, and it was measured rather than guessed.
#
# `tests/integration.rs::counter_from_default` calls `Counter::default()`. The
# authoring prompt shows the item to author and the behaviour spec, but never
# the test, and never says the trait is needed — so the model has to guess that
# `Default` belongs on the struct. Across eight authorings on 2026-08-11
# (microcrate and hardcrate, Qwen3.6 and Muse-Glimmer, at both 32768 and 131072,
# and at reasoning strengths high and xhigh) the guess came out right 3 times
# and wrong 5. Every wrong one cost the same thing: the cluster does not
# compile, `error[E0599]: no function or associated item named 'default' found
# for struct 'Counter'`, no test binary runs, so NO test reports a verdict and
# the sweep scores 0 — then the DESIGN_CONFLICT retrigger spends sweeps
# re-authoring. One run needed the task3 fix loop to recover; one had the
# re-author come back with `error[E0603]: struct 'Counter' is private`, worse
# than what it replaced.
#
# Neither model and no reasoning strength fixed it, because the information was
# never in the prompt. Raising Muse from `high` to `xhigh` made it worse, not
# better. So this states the requirement instead of hoping it is inferred.
_DERIVED_TRAIT_RULE = (
    "TRAITS THE TESTS CALL MUST BE DERIVED:\n"
    "The frozen tests are the specification. If a test constructs or compares a\n"
    "type through a trait method, that trait must be on the type — deriving it\n"
    "is your job, and nothing else in this prompt will remind you.\n"
    "`T::default()` needs `#[derive(Default)]`; `assert_eq!` on a value needs\n"
    "`PartialEq` and `Debug`; cloning needs `Clone`. A missing derive does not\n"
    "fail gracefully: the cluster stops compiling, no test binary runs at all,\n"
    "and every test scores as failing regardless of the bodies.\n"
    "When in doubt on a plain data-carrying struct or enum, derive the standard\n"
    "set — it costs nothing and an absent one costs the whole build:\n"
    "WRONG (a test calls `Counter::default()`):\n"
    "pub struct Counter { value: u32 }\n"
    "RIGHT:\n"
    "#[derive(Debug, Default, Clone, PartialEq, Eq)]\n"
    "pub struct Counter { value: u32 }\n"
)


# A public type's auto-traits are part of its API even though nothing in the
# frozen surface spells them out: `Send`/`Sync` are inferred from the FIELDS,
# so a private layout choice silently removes them and the break surfaces only
# when a test binary fails to compile — far from the layout that caused it.
# Concrete examples, not "keep autotraits in mind": the model acts on examples.
_AUTOTRAIT_RULE = (
    "AUTO-TRAITS ARE PART OF THE FROZEN PUBLIC API:\n"
    "`Send`/`Sync` are auto-derived from a struct's FIELDS. A private field\n"
    "type that is not `Send`/`Sync` silently takes those traits away from the\n"
    "PUBLIC type that holds it — the compiler accepts your layout and the\n"
    "failure appears later as an unrelated-looking test-binary error\n"
    "(`... cannot be sent between threads safely`). If a public type is used\n"
    "across threads anywhere in the tests, your layout must keep it\n"
    "`Send`/`Sync` — either pick a field type that already carries them, or\n"
    "add the `unsafe impl` yourself.\n"
    "WRONG (raw pointer field — `Handle` is no longer Send/Sync):\n"
    "pub struct Handle { inner: core::ptr::NonNull<Repr> }\n"
    "RIGHT (wrapper carries the guarantee explicitly):\n"
    "pub struct Handle { inner: core::ptr::NonNull<Repr> }\n"
    "unsafe impl Send for Handle {}\n"
    "unsafe impl Sync for Handle {}\n"
    "RIGHT (a field type that is already Send + Sync):\n"
    "pub struct Handle { inner: Box<Repr> }\n"
    "\n"
    "WRONG (`Rc`/`Cell` are never Sync — `Registry` loses Sync):\n"
    "pub struct Registry { slots: alloc::rc::Rc<core::cell::RefCell<Slots>> }\n"
    "RIGHT:\n"
    "pub struct Registry { slots: alloc::sync::Arc<std::sync::Mutex<Slots>> }\n"
)


# Every authored item opens with the contract it serves, as `///` lines. The
# sentence is the one channel that survives from t1.5 to the fill prompts:
# the splice is textual (`scaffold_rewrite.replace_surface_definitions`), so
# the lines land in the skeleton iff they are in the authored block, the
# extractor renders them first in the type's `definition`
# (`render_item_definition`), and `prompts.sections.format_type_for_prompt`
# prints them above the type line. Rendered ALWAYS, not only when behaviours
# are listed: the rule is a few lines, and the layout pins alone justify a
# contract sentence on the type they pin.
_DOC_CONTRACT_RULE = (
    "EVERY ITEM OPENS WITH ITS CONTRACT, AS `///` DOC LINES:\n"
    "Begin each block you author with one or two `///` lines that state, in\n"
    "your own words, the contract that item serves — which behaviour listed\n"
    "above (or which layout-pinning test) its representation makes\n"
    "expressible. Write the sentence from THIS prompt's BEHAVIOURS and\n"
    "LAYOUT-PINNING sections; never copy any library's documentation. At most\n"
    "two lines. They stay on the item and are shown again to whoever fills\n"
    "its bodies, so a wrong contract misleads every later fill.\n"
    "RIGHT:\n"
    "/// The TypeId stored for a message error is the message's own type, so a user's downcast::<&str>() finds it.\n"
    "#[repr(transparent)]\n"
    "struct Payload<M>(M);\n"
    "RIGHT (a fn):\n"
    "/// Yields the wrapped payload's own address, so downcast_ref on it sees the payload's TypeId.\n"
    "fn payload_ptr(&self) -> *const () { unimplemented!() }\n"
)


# Only emitted when the chunk actually contains a PUB_FIELDS item — a rule
# about items that are not on the list reads as a contradiction of the one
# above it ("the PUBLIC items are frozen").
_PUB_FIELDS_RULE = (
    "PUBLIC STRUCTS ON THE LIST (kind `struct`, marked PUBLIC type):\n"
    "Their DECLARATION HEAD is frozen API — visibility, name, generics and\n"
    "bounds must come back character-for-character. Their FIELDS are yours to\n"
    "design, and every field must stay private (no `pub`).\n"
    "Frozen head: `pub struct Handle<T: ?Sized>`\n"
    'WRONG (bound dropped — different public type): <LAYOUT_ITEM name="Handle" kind="struct">\n'
    "pub struct Handle<T> { inner: Box<T> }\n"
    "</LAYOUT_ITEM>\n"
    'WRONG (field exposed — widens the API): <LAYOUT_ITEM name="Handle" kind="struct">\n'
    "pub struct Handle<T: ?Sized> { pub inner: Box<T> }\n"
    "</LAYOUT_ITEM>\n"
    'RIGHT: <LAYOUT_ITEM name="Handle" kind="struct">\n'
    "pub struct Handle<T: ?Sized> { inner: Box<T> }\n"
    "</LAYOUT_ITEM>\n"
    "\n"
    "Frozen head: `pub struct Cursor<'a>`\n"
    'WRONG (renamed lifetime — different public type): <LAYOUT_ITEM name="Cursor" kind="struct">\n'
    "pub struct Cursor<'s> { at: usize, src: &'s [u8] }\n"
    "</LAYOUT_ITEM>\n"
    'RIGHT: <LAYOUT_ITEM name="Cursor" kind="struct">\n'
    "pub struct Cursor<'a> { at: usize, src: &'a [u8] }\n"
    "</LAYOUT_ITEM>\n"
)


def _fmt_pins(pins: list[BehaviourSpec]) -> str:
    if not pins:
        return "(none — the layout is behaviour-pinned only)"
    lines = []
    for p in pins:
        expr = p.then.static_expr if p.then else p.when
        lines.append(f"- {expr}")
    return "\n".join(lines)


# What the model may still decide once the behaviours are stated. Read from
# the behaviour module when it defines the sentence (so the two channels that
# quote it — this prompt and the fill-side evidence — cannot drift), with an
# identical local fallback until it does: the attribute is looked up at call
# time, not import time, so no import can fail on its absence.
_MAY_REPRESENTATION_FALLBACK = (
    "The layout, allocation strategy and private names are yours (MAY); "
    "every behaviour listed is a MUST.")


def _may_note() -> str:
    return str(getattr(_behaviour, "MAY_REPRESENTATION_NOTE",
                       _MAY_REPRESENTATION_FALLBACK))


# Bounds on the behaviours section. The constructor fill prompt already
# measures 121 147 chars on the live `stable` tree (2026-08-26); the
# authoring prompt is smaller, but a suite can hold hundreds of harvested
# doc-tests and a whole-crate dump would drown the pins and the item list.
_BEHAVIOUR_MAX = 40
_BEHAVIOUR_CHAR_CAP = 6000

# Group headers: the class name plus what it decides about a representation,
# so the model reads the grouping as a design constraint, not a taxonomy.
_CLASS_GLOSS = {
    "IDENTITY": "what a TypeId lookup / downcast on the value finds",
    "TRAIT_OBJECT_IDENTITY": "what the value is as a trait object",
    "EXACT_FORMAT": "byte-exact Display / Debug / chain text",
    "BOUNDED_RESOURCE": "allocation and size bounds the value keeps",
    "OPTION_SEMANTICS": "when an Option / Result is Some / None / Ok / Err",
    "LAYOUT_PIN": "compile-time layout facts",
    "CFG_WORLD": "cfg-dependent outcomes",
    "OTHER": "usage the public API must keep accepting",
}


def _fenced(text: str, indent: str = "  ") -> str:
    body = "\n".join(f"{indent}{ln}" for ln in text.splitlines())
    return f"\n{indent}```\n{body}\n{indent}```"


def _with_value(prefix: str, value: str) -> str:
    """`prefix` followed by a behaviour's exact value: quoted on the same line,
    or — when the value spans lines — a fenced block opening right after the
    prefix (no trailing blank before the fence)."""
    return f"{prefix}{_fenced(value)}" if "\n" in value else f'{prefix} "{value}"'


def _chain_values(chain: tuple[str, ...]) -> str:
    if any("\n" in c for c in chain):
        return "".join(_fenced(c) for c in chain)
    return " " + ", ".join(f'"{c}"' for c in chain)


def _fmt_outcome(then: _behaviour.ObservableOutcome | None) -> str:
    """The Then of one behaviour, as the observable the representation must
    produce. Keyed by the kind's NAME so kinds added beside the current ones
    (`TYPE_IDENTITY`, `ALT_DEBUG_STRING`) render as soon as they exist."""
    if then is None:
        return "the snippet's own assertions hold"
    kind = then.kind.name
    ok = "true" if then.ok else "false"
    if kind == "TYPE_IDENTITY":
        return (f"`is::<{then.value}>()` / `downcast::<{then.value}>()` on "
                f"that value is {ok} — the TypeId a user downcasts to is the "
                "payload's type, never a wrapper's")
    if kind == "DOWNCAST_OK":
        return f"the downcast is {'Some' if then.ok else 'None'} ({ok})"
    if kind == "DISPLAY_STRING":
        return _with_value("to_string() ==", then.value)
    if kind == "DEBUG_STRING":
        return _with_value('format!("{:?}") ==', then.value)
    if kind == "ALT_DEBUG_STRING":
        return _with_value('format!("{:#?}") ==', then.value)
    if kind == "CHAIN_SEQUENCE":
        return "chain() yields, in order:" + _chain_values(then.chain)
    if kind == "PANIC_MESSAGE":
        return _with_value("panics with", then.value)
    if kind == "RETURN_RENDERED":
        return _with_value("returns", then.value)
    if kind == "NORMALIZED_STDOUT":
        return _with_value("prints", then.value)
    if kind == "STATIC_ASSERTION":
        return f"`{then.static_expr}` holds"
    return _with_value(f"{kind.lower()} ==", then.value)


def identity_must_instruction(
    specs: Sequence[BehaviourSpec],
    *,
    fn_names: Collection[str],
    cap: int = IDENTITY_MUST_CAP,
) -> str:
    """The identity Then as a fill-prompt instruction, via `_fmt_outcome`.

    Empty when no IDENTITY spec names one of `fn_names`. This is the wrapping
    MUST t1.5 already renders; fill prompts never called it. Does not dump
    `given`/`when` as usage examples — that is `select_usage_examples`, and
    on the live suite it spends the cap on doctest OTHER with
    `unimplemented!()`.
    """
    picked = select_identity_must(specs, fn_names=fn_names, cap=cap)
    if not picked:
        return ""
    thens = [_fmt_outcome(s.then) for s in picked]
    return "MUST: " + "; ".join(thens)


def _fmt_behaviour_line(spec: BehaviourSpec) -> str:
    when = spec.when.strip() or spec.given.strip() or spec.behaviour_id
    then = _fmt_outcome(spec.then)
    if "\n" in when:
        return f"- when:{_fenced(when)}\n  then {then}"
    return f"- when `{when}` then {then}"


def _fmt_behaviours(specs: Sequence[BehaviourSpec]) -> str:
    """The behaviours section body: one line per behaviour, grouped by class,
    MUST-classes first, capped by count and by chars with a trailing
    `(+n more)`, then the MAY sentence. Empty input renders nothing — the
    caller omits the section, so a prompt built without behaviours is
    byte-identical to one built before the section existed."""
    ordered = sorted(specs, key=lambda s: class_rank(behaviour_class_name(s)))
    lines: list[str] = []
    used = 0
    shown = 0
    current = ""
    for spec in ordered:
        if shown >= _BEHAVIOUR_MAX:
            break
        group = behaviour_class_name(spec)
        entry = _fmt_behaviour_line(spec)
        header = None
        if group != current:
            gloss = _CLASS_GLOSS.get(group)
            header = f"{group} ({gloss}):" if gloss else f"{group}:"
        cost = len(entry) + 1 + (len(header) + 1 if header else 0)
        if used + cost > _BEHAVIOUR_CHAR_CAP and shown:
            break
        if header:
            lines.append(header)
            current = group
        lines.append(entry)
        used += cost
        shown += 1
    rest = len(ordered) - shown
    if rest > 0:
        lines.append(f"(+{rest} more)")
    lines.append(_may_note())
    return "\n".join(lines)


def _fmt_items(
    items: tuple[PrivateSurfaceItem, ...], uids: tuple[str, ...],
    anchors: Mapping[str, str] | None = None,
) -> str:
    """One listing line per item, keyed by protocol uid.

    The item's CURRENT definition is deliberately absent. Showing it — even
    just its first line — hands over the design the model is being asked to
    invent (`ErrorImpl — currently: #[repr(C)]`, `vtable — currently: fn
    vtable(p: NonNull<ErrorImpl>) -> &'static ErrorVTable` is an architecture,
    not a disambiguator), and the reply comes back shaped like it. Same-named
    items are told apart by the uid's ordinal suffix plus the file and the
    public type the item is reachable from — facts already visible in the
    frozen public surface above."""
    lines = []
    for uid, it in zip(uids, items, strict=True):
        if it.kind is SurfaceItemKind.PUB_FIELDS:
            # The protocol kind stays `struct` — what differs is the CONTRACT,
            # so the line states it where the model reads the item, not only in
            # the rules block far above.
            lines.append(
                f"- {uid} (struct) in {it.file} — PUBLIC type: re-emit its "
                "declaration head EXACTLY as shown in the frozen public "
                "surface (same visibility, name, generics and bounds) and "
                "design ONLY its fields; every field must stay private")
            continue
        anchor = (anchors or {}).get(uid, "")
        via = f" — reached from the public `{anchor}`" if anchor else ""
        lines.append(f"- {uid} ({it.kind.name.lower()}) in {it.file}{via}")
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
    item_anchors: Mapping[str, str] | None = None,
    behaviours: Sequence[BehaviourSpec] = (),
) -> str:
    """Assemble the t1.5 prompt for one connected component of the private
    surface (or ONE CHUNK of a large component — `items` is then the chunk and
    `item_uids` carries the COMPONENT-scoped protocol uids, which a chunk
    cannot recompute from its own subset). `extra_guidance` (e.g.
    CRASH_GUIDANCE) is appended before the rules. `authored_scheme` carries
    the definitions already authored by earlier chunks of the same pass —
    fixed context the new items must fit. `item_anchors` maps a uid to the
    frozen PUBLIC type that reaches it, the only disambiguation the listing
    offers (never the item's current definition — see `_fmt_items`).
    `behaviours` are the RUNTIME behaviours the component's representation
    must make expressible (`domain.behaviour_select.
    select_component_behaviours`); empty, the section is omitted and the
    prompt is byte-for-byte what it was before the section existed."""
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
        ("=== BEHAVIOURS THIS REPRESENTATION MUST MAKE EXPRESSIBLE (MUST) ==="
         if behaviours else None),
        _fmt_behaviours(behaviours) if behaviours else None,
        "" if behaviours else None,
        "=== IN-SCOPE SYMBOL INVENTORY ===",
        symbol_inventory.strip() or "(std/core only)",
        "",
        ("=== ALREADY AUTHORED THIS PASS (fixed — design consistently with "
         "these; do NOT re-emit them) ===") if authored_scheme.strip() else None,
        authored_scheme.strip() or None,
        "" if authored_scheme.strip() else None,
        "=== ITEMS TO AUTHOR ===",
        _fmt_items(items, uids, item_anchors),
        "",
        extra_guidance or None,
        "" if extra_guidance else None,
        _RULES,
        _DERIVED_TRAIT_RULE,
        _AUTOTRAIT_RULE,
        _DOC_CONTRACT_RULE,
        (_PUB_FIELDS_RULE
         if any(i.kind is SurfaceItemKind.PUB_FIELDS for i in items) else None),
        "",
        _PROTOCOL,
    ] if s is not None])
