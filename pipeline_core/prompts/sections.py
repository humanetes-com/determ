"""Pure prompt-section renderers + curated guidance data.

Text→text only: no I/O, no run state, no regex. The two impure
collaborators the builders need (the pipeline's file-view annotator and
promoted-error rules) live behind :mod:`pipeline_core.prompts.runtime`.
Bodies moved verbatim from pipeline.py (leading underscores dropped on
extraction — they are this module's public entry points).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pipeline_core.domain.models import (
    DispatchFamily,
    FieldRecord,
    ImplBlock,
    MethodRecord,
    SlotFit,
    TraitImplRecord,
    TypeRecord,
)
from pipeline_core.domain.refill_evidence import collapse_ws
from pipeline_core.domain.type_refs import (
    accessor_methods_for_field,
    field_accessible_from,
)

# Catalog-driver std-trait tutorials. When a fn is being implemented
# inside `impl Trait for X`, the catalog driver injects the trait's
# documented behavior. Hardcoded for the most common stdlib traits —
# covers ~90% of impl blocks in typical Rust crates. The generic driver
# (LANGUAGE_DRIVER=generic, the default) does not read this table: the
# trait name is already in the impl header the prompt quotes, and the
# fill model is the language expert.
TRAIT_CONTRACTS: dict[str, str] = {
    "Display": (
        "Display::fmt(&self, f: &mut Formatter) -> fmt::Result\n"
        "  - Produces user-facing string representation via `write!(f, \"...\", ...)`.\n"
        "  - Return value of `write!(...)` IS the return value of fmt.\n"
        "  - Do NOT use {:?} or include type names in the output."
    ),
    "Debug": (
        "Debug::fmt(&self, f: &mut Formatter) -> fmt::Result\n"
        "  - Produces developer-facing representation, typically via\n"
        "    f.debug_struct(\"Name\").field(\"k\", &v).finish() OR\n"
        "    write!(f, \"Name({:?}, {:?})\", a, b)\n"
        "  - For structs: use debug_struct/debug_tuple/debug_list/debug_map.\n"
        "  - Return value of the helper IS the return value of fmt."
    ),
    "Error": (
        "std::error::Error has these methods (all default to None / no-op):\n"
        "  source(&self) -> Option<&(dyn Error + 'static)>  — return cause if any\n"
        "  provide(&'_ self, request: &mut Request<'_>)     — nightly only\n"
        "Most impls override `source` to return Some(&self.inner_error) or None."
    ),
    "Hash": (
        "Hash::hash<H: Hasher>(&self, state: &mut H)\n"
        "  - Feed each field into state.write_*() or call field.hash(state).\n"
        "  - Must be consistent with Eq: a == b ⟹ hash(a) == hash(b)."
    ),
    "PartialEq": (
        "PartialEq::eq(&self, other: &Self) -> bool\n"
        "  - Compare each meaningful field; return true iff all match.\n"
        "  - Reflexive in most cases (a == a); doesn't need transitivity."
    ),
    "Eq": "Eq is a marker trait extending PartialEq — no methods to impl.",
    "PartialOrd": (
        "PartialOrd::partial_cmp(&self, other: &Self) -> Option<Ordering>\n"
        "  - Return Some(Less/Equal/Greater) or None if incomparable.\n"
        "  - Often delegates: `self.field.partial_cmp(&other.field)`."
    ),
    "Ord": (
        "Ord::cmp(&self, other: &Self) -> Ordering\n"
        "  - Total ordering: always returns Less/Equal/Greater.\n"
        "  - Often delegates to inner field's .cmp()."
    ),
    "Clone": (
        "Clone::clone(&self) -> Self\n"
        "  - Construct a new value structurally identical to self.\n"
        "  - Often: Self { field1: self.field1.clone(), ... }"
    ),
    "Default": (
        "Default::default() -> Self\n"
        "  - Construct a sensible empty/zero value.\n"
        "  - Often: Self { field1: Default::default(), ... }"
    ),
    "From": (
        "From<T>::from(value: T) -> Self\n"
        "  - Convert from T into Self. Should be infallible.\n"
        "  - Self::from(x) is also reachable via x.into()."
    ),
    "Into": "Into<T> is auto-implemented from From<U> — implement From instead.",
    "Deref": (
        "Deref::deref(&self) -> &Self::Target\n"
        "  - Return a reference to the inner / wrapped value.\n"
        "  - Often: &self.inner_field"
    ),
    "DerefMut": (
        "DerefMut::deref_mut(&mut self) -> &mut Self::Target\n"
        "  - Mutable counterpart of Deref. Often: &mut self.inner_field"
    ),
    "Drop": (
        "Drop::drop(&mut self)\n"
        "  - Cleanup when value goes out of scope.\n"
        "  - Manually drop fields needing custom dispose; primitives auto-drop."
    ),
    "Iterator": (
        "Iterator has `type Item;` and `fn next(&mut self) -> Option<Self::Item>`.\n"
        "  - Return Some(item) until exhausted; then None forever after.\n"
        "  - Other methods (map, filter, ...) are provided via defaults."
    ),
    "AsRef": (
        "AsRef<T>::as_ref(&self) -> &T\n"
        "  - Return a borrowed reference to T. Should be cheap (no allocation)."
    ),
    "AsMut": (
        "AsMut<T>::as_mut(&mut self) -> &mut T\n"
        "  - Mutable counterpart of AsRef."
    ),
    "FromStr": (
        "FromStr::from_str(s: &str) -> Result<Self, Self::Err>\n"
        "  - Parse Self from a string. Return Err on parse failure."
    ),
}


def extract_trait_from_container(container: str) -> str | None:
    """Pull the trait name out of an `impl Trait for X` container string.
    Returns None for inherent impls (no trait) or unsupported forms.

    Regex-free (project rule) depth-aware walk. Improvement over the old
    regex, which required whitespace after `impl` and stopped generic lists
    at the first `>` — so every `impl<T> Trait for X` and every nested
    generic (`impl Trait<Vec<U>> for X`) silently got NO trait contract.
    """

    def _skip_generics(s: str) -> str | None:
        """`s` starts with `<` — return the text after its balanced close."""
        depth = 0
        for i, ch in enumerate(s):
            if ch == "<":
                depth += 1
            elif ch == ">":
                depth -= 1
                if depth == 0:
                    return s[i + 1:]
        return None

    s = container.lstrip()
    if not s.startswith("impl"):
        return None
    s = s[len("impl"):]
    if not s or not (s[0] == "<" or s[0].isspace()):
        return None  # e.g. `implFoo` — not an impl header
    s = s.lstrip()
    if s.startswith("<"):
        skipped = _skip_generics(s)
        if skipped is None:
            return None
        s = skipped.lstrip()
    # Trait path: `Ident` / `path::To::Ident` (chars valid in a Rust path).
    j = 0
    while j < len(s) and (s[j].isalnum() or s[j] in "_:"):
        j += 1
    path = s[:j]
    if not path or not (path[0].isalpha() or path[0] == "_"):
        return None
    rest = s[j:]
    if rest.startswith("<"):  # the trait's own generic arguments
        skipped = _skip_generics(rest)
        if skipped is None:
            return None
        rest = skipped
    # A trait impl requires ` for `: whitespace, `for`, whitespace.
    if not rest or not rest[0].isspace():
        return None
    rest = rest.lstrip()
    if not (rest.startswith("for") and len(rest) > 3 and rest[3].isspace()):
        return None
    # Last path segment is the trait name (`fmt::Display` → `Display`).
    return path.rsplit("::", 1)[-1]


def trait_contract_for(container: str) -> str:
    """Catalog-driver contract text for the trait this fn implements,
    or empty string if no matching contract is known.

    Production builders go through :func:`language_driver`. This
    function is the catalog implementation; the generic driver does not
    call it.
    """
    name = extract_trait_from_container(container)
    if not name:
        return ""
    return TRAIT_CONTRACTS.get(name, "")


# Six Rust confusion classes seen recurrently in overnight runs.
# Each has a one-line summary (always shown in the prompt) and a fuller
# detail block (injected only when the current cargo error trips the
# gotcha's trigger keyword/code, via gotchas_for_error).
RUST_GOTCHAS: list[dict[str, Any]] = [
    {
        # Observed live: four attempts burned re-guessing a call whose type
        # parameter appears only behind an associated-type projection, with
        # the model annotating the let-binding each time (which cannot help).
        "id": "assoc_type_needs_turbofish",
        "summary": "A type parameter that appears ONLY behind an associated type in the return (`-> W<M::Out>`) can never be inferred — turbofish the CALL (`x.conv::<Concrete>()`). Annotating the let-binding does NOT work.",
        "triggers": {"E0282", "E0283", "E0284", "type annotations needed",
                     "cannot infer type of the type parameter",
                     "type must be known at this point"},
        "detail": (
            "Inference solves the return type FORWARD from the parameter, not\n"
            "backward: `M::Out` is a projection, so knowing the result is\n"
            "`W<Foo>` does not tell the compiler that `M` is `Bar` — many `M`\n"
            "could project to the same `Out`. The parameter must be named at\n"
            "the call site.\n"
            "  WRONG (annotate the binding — still E0284, the projection is\n"
            "  not invertible):\n"
            "    trait Marker { type Out; }\n"
            "    impl W<T> { fn conv<M: Marker>(self) -> W<M::Out> { ... } }\n"
            "    let out: W<Foo> = value.conv();      // cannot infer `M`\n"
            "  RIGHT (name the parameter on the call):\n"
            "    let out = value.conv::<Bar>();       // `M = Bar`, Out follows\n"
            "  WRONG (free fn whose parameter is used only in the return):\n"
            "    fn build<F: Factory>() -> F::Product { ... }\n"
            "    let p: Widget = build();             // cannot infer `F`\n"
            "  RIGHT:\n"
            "    let p = build::<WidgetFactory>();\n"
            "  Rule of thumb: if the parameter is absent from the ARGUMENT\n"
            "  types, only a turbofish can supply it."
        ),
    },
    {
        # The dangling-'static class: silencing the borrow checker with a
        # raw-pointer cast turns a lifetime ERROR into a runtime SIGSEGV that
        # takes the whole test binary down (observed live: a fn-pointer table
        # built on the stack, addr_of!-cast to &'static, stored in a heap
        # object — every later dispatch jumped through dead stack memory).
        "id": "stack_to_static_cast",
        "summary": "NEVER cast a stack local's address to `&'static` (`&*(addr_of!(local) as *const T)` / transmute). It compiles, then SEGFAULTS at runtime when the frame dies. Use static promotion `&T { ... }` (const-eligible fields) or `Box::leak`.",
        "triggers": {"E0515", "E0716", "cannot return reference",
                     "temporary value dropped while borrowed",
                     "does not live long enough", "SIGSEGV",
                     "stack-use-after-return"},
        "detail": (
            "A `&'static T` must point at memory that NEVER dies. Fabricating\n"
            "one from a stack local silences the borrow checker but leaves a\n"
            "dangling pointer — every later read/dispatch through it is UB\n"
            "(silent SIGSEGV, whole test binary dies, its tests vanish).\n"
            "  WRONG (stack local + cast — compiles, segfaults later):\n"
            "    let table = FnTable { call: handler::<T>, drop: dropper::<T> };\n"
            "    let r: &'static FnTable = unsafe { &*(ptr::addr_of!(table) as *const FnTable) };\n"
            "    Holder::store(r)   // r dangles as soon as this fn returns\n"
            "  RIGHT (static promotion — `&EXPR` where every field is a\n"
            "  const-eligible value like a fn path; rvalue is promoted to 'static):\n"
            "    let r: &'static FnTable = &FnTable { call: handler::<T>, drop: dropper::<T> };\n"
            "    Holder::store(r)\n"
            "  (Promotion needs const-eligible fields: name a generic fn\n"
            "  `dropper::<T>` instead of a closure `|x| ...` — closures block it.)\n"
            "  WRONG (returning 'static view of a local buffer):\n"
            "    let s = String::from(name);\n"
            "    let out: &'static str = unsafe { mem::transmute(s.as_str()) };  // dangles\n"
            "  RIGHT (leak an owned allocation when the value is runtime-built):\n"
            "    let out: &'static str = String::from(name).leak();\n"
            "The compile error the cast silences (E0716/E0515 'temporary value\n"
            "dropped', 'does not live long enough') is the CORRECT diagnosis —\n"
            "fix the lifetime, never cast it away."
        ),
    },
    {
        # The vtable / shared-machinery class. You fill fn BODIES only —
        # you cannot introduce a new top-level item — so a referenced-but-
        # undefined module-level symbol can never resolve. Build it inline.
        "id": "undefined_symbol_build_inline",
        "summary": "`cannot find value/function X` → X is undefined and you CANNOT add a new top-level item. Build what you need INLINE from in-scope items; never reference an undefined module-level const/static/helper.",
        "triggers": {"E0425", "E0433", "cannot find value", "cannot find function", "in this scope"},
        "detail": (
            "You are filling a fn BODY. You may NOT add new top-level items\n"
            "(no new `static`, `const`, free fn, or `use`), so referencing a\n"
            "module-level symbol that doesn't already exist can never compile.\n"
            "  WRONG (invents an external constant that nothing defines):\n"
            "    unsafe { Self::construct(value, &ADHOC_VTABLE, backtrace) }   // E0425\n"
            "  RIGHT (construct the value you need INLINE, from in-scope items):\n"
            "    let vtable = &SomeInScopeType { /* fields from items shown to you */ };\n"
            "    unsafe { Self::construct(value, vtable, backtrace) }\n"
            "Use ONLY names shown in TYPES_IN_SCOPE / the impl + its siblings. If\n"
            "several fns need the same value, build it the SAME way in each (do\n"
            "not invent a shared external name — there is no place to define it).\n"
            "Do not guess a path like `core::alloc::Global`; if an allocator or\n"
            "helper isn't in scope, use one that is (e.g. `Box`)."
        ),
    },
    {
        # Archetype map (concrete generic examples; NOT crate source). Built
        # from the dominant fix-loop errors. Each shows WRONG → RIGHT so the
        # model learns the transformation, not an abstract rule.
        "id": "missing_trait_bound",
        "summary": "`T doesn't implement Display/Debug/...` → the generic has NO such bound (that's the spec). Do NOT add it; don't call that trait on T.",
        "triggers": {"E0277", "doesn't implement", "is not satisfied", "the trait bound"},
        "detail": (
            "`T doesn't implement Display` (or Debug/PartialEq/...) means the generic\n"
            "carries no such bound. Unlike `T: Sized`, this bound is the CALLER's\n"
            "contract — you may NOT add it.\n"
            "  WRONG (assumes T: Display):\n"
            "    fn label<T>(x: T) -> String { format!(\"{}\", x) }   // E0277\n"
            "  RIGHT only if the REAL signature already declares it:\n"
            "    fn label<T: Display>(x: T) -> String { format!(\"{}\", x) }\n"
            "  If the signature does NOT grant Display, the body must not format T at\n"
            "  all — the function was never meant to. Build/return the value another\n"
            "  way using only the traits the bounds already provide."
        ),
    },
    {
        # The frozen-signature ratchet class (anyhow run 30425): a bound added
        # to a trait-impl METHOD is always rejected — the trait fixes the sig.
        "id": "trait_impl_stricter_bound",
        "summary": "E0276 `impl has stricter requirements than trait` → NEVER add a `where` clause / bound to a trait-impl method. Keep the trait's exact signature; write a body that needs no extra bound.",
        "triggers": {"E0276", "impl has stricter requirements than trait",
                     "stricter requirements"},
        "detail": (
            "A method inside `impl Trait for Type` must keep EXACTLY the trait's\n"
            "signature — any extra bound is rejected (E0276), even if the body\n"
            "seems to need it.\n"
            "  WRONG (extra bound on the method):\n"
            "    impl<C, E> StdErr for Pair<C, E> {\n"
            "        fn provide<'a>(&'a self, req: &mut Request<'a>)\n"
            "        where C: 'static                      // E0276\n"
            "        { req.provide_ref(&self.first) }\n"
            "    }\n"
            "  RIGHT (trait signature untouched; body uses only members that\n"
            "  already satisfy the trait's own requirements):\n"
            "    impl<C, E> StdErr for Pair<C, E> {\n"
            "        fn provide<'a>(&'a self, req: &mut Request<'a>) {\n"
            "            self.second.provide(req)   // forward; no new bound\n"
            "        }\n"
            "    }\n"
            "  WRONG (method bound in a Display impl):\n"
            "    impl<T> Display for Wrap<T> {\n"
            "        fn fmt(&self, f: &mut Formatter) -> fmt::Result\n"
            "        where T: Debug                        // E0276\n"
            "        { write!(f, \"{:?}\", self.inner) }\n"
            "    }\n"
            "  RIGHT (use only what the impl block's existing bounds provide):\n"
            "    impl<T> Display for Wrap<T> {\n"
            "        fn fmt(&self, f: &mut Formatter) -> fmt::Result {\n"
            "            write!(f, \"{}\", self.label)   // no T needed\n"
            "        }\n"
            "    }\n"
            "If the body truly cannot be written without the bound, the bound\n"
            "belongs on the IMPL BLOCK header — but that header is frozen too;\n"
            "choose a body that works within the existing bounds."
        ),
    },
    {
        # The frozen-layout class (miner design_conflict rows): the model
        # invents a field on a private struct/enum whose EXACT layout is shown
        # in TYPES_IN_SCOPE. Those are the impls the whole crate waits on.
        "id": "invented_field_frozen_layout",
        "summary": "`no field X on type Y` / `struct Y has no field named X` → the type's layout is FROZEN and shown in TYPES_IN_SCOPE. Use ONLY the field names listed there; never invent `.inner`/`.0`/`.value`.",
        "triggers": {"E0609", "E0560", "no field", "has no field named",
                     "does not have a field"},
        "detail": (
            "The struct/enum layout is frozen — every field name + type is listed\n"
            "in TYPES_IN_SCOPE. Access ONLY those; do not guess a conventional name.\n"
            "  WRONG (invents `.inner` on a type whose real field is `object_`):\n"
            "    fn message(&self) -> &str { &self.inner.msg }   // E0609 no field `inner`\n"
            "  RIGHT (use the field the frozen def lists):\n"
            "    // TYPES_IN_SCOPE showed:  struct Wrap { object_: Box<str> }\n"
            "    fn message(&self) -> &str { &self.object_ }\n"
            "  WRONG (assumes a tuple `.0` on a NAMED-field struct):\n"
            "    fn first(&self) -> &T { &self.0 }               // E0609 no field `0`\n"
            "  RIGHT (named access — the def had `struct Pair<T> { head: T, .. }`):\n"
            "    fn first(&self) -> &T { &self.head }\n"
            "If a value you need isn't a field, derive it from the fields that ARE\n"
            "listed — never add a field, and never rename one."
        ),
    },
    {
        "id": "unsafe_call_block",
        "summary": "Calling an unsafe fn needs `unsafe { ... }` — unless the enclosing fn is itself `unsafe fn` (then the body is already an unsafe context).",
        "triggers": {"E0133", "call to unsafe", "requires unsafe", "unsafe function or block"},
        "detail": (
            "  WRONG (safe fn calls an unsafe fn bare):\n"
            "    fn first(p: *const u8) -> u8 { core::ptr::read(p) }   // E0133\n"
            "  RIGHT (wrap the unsafe call):\n"
            "    fn first(p: *const u8) -> u8 { unsafe { core::ptr::read(p) } }\n"
            "  If the signature is `unsafe fn first(...)`, the WHOLE body is already\n"
            "  unsafe — do NOT add a redundant inner `unsafe {}`. Rule: bare unsafe\n"
            "  call only inside an `unsafe fn`; otherwise wrap each call in `unsafe {}`."
        ),
    },
    {
        "id": "name_not_in_scope",
        "summary": "`cannot find X in scope` → X is not a real name here. Use ONLY items shown in TYPES_IN_SCOPE; never invent a helper or constructor.",
        "triggers": {"E0425", "E0433", "cannot find", "not found in this scope"},
        "detail": (
            "The body used a name that doesn't exist.\n"
            "  WRONG (invented constructor):\n"
            "    let w = Wrapper::from_raw(p);   // E0425 — no such fn\n"
            "  RIGHT — only use the real fields/methods TYPES_IN_SCOPE lists:\n"
            "    let w = Wrapper { ptr: p };\n"
            "Never guess function or type names. If you can't build the value from the\n"
            "items in scope, you're constructing it the wrong way — re-read the type's\n"
            "actual fields and methods."
        ),
    },
    {
        "id": "typeid_static",
        "summary": "TypeId::of::<T>() requires T: 'static. If T is generic without that bound and the signature is locked, you cannot use TypeId::of in the body.",
        "triggers": {"E0310", "E0311", "typeid"},
        "detail": (
            "If T is a generic parameter without a 'static bound, TypeId::of::<T>() "
            "will not compile. You can NOT just add the bound when the signature is "
            "locked — you need a different approach: pattern-match on a runtime "
            "TypeId stored elsewhere (e.g. a vtable field), or use the surrounding "
            "context to get the comparison value without invoking TypeId::of."
        ),
    },
    {
        "id": "dyn_parens",
        "summary": "&dyn Trait + Bounds needs parentheses → &(dyn Trait + Bounds). Same for *const, Box, etc.",
        "triggers": {"ambiguous", "&dyn", "dyn"},
        "detail": (
            "Rust parses `&dyn Trait + Bound` as `(&dyn Trait) + Bound` which is "
            "an arithmetic + on a type. Always wrap the dyn portion in parens: "
            "`&(dyn Trait + Bound)`, `*const (dyn Trait + Send)`, "
            "`Box<dyn Trait + Send + Sync>` (Box's generics already do the parens)."
        ),
    },
    {
        "id": "unsized_value",
        "summary": "Trait objects (&dyn X, *const dyn X) are unsized. Cannot store by value; format with {:?} not {}.",
        "triggers": {"E0277", "doesn't have a size known at compile-time", "unsized", "display"},
        "detail": (
            "Sized requirement: trait objects don't have known sizes, so they can "
            "only live behind a pointer (&, Box, Rc, etc.). `let x: dyn Trait = ...` "
            "fails. Also, &dyn Debug does NOT implement Display — use {:?} for "
            "trait objects, not {}."
        ),
    },
    {
        "id": "wrapper_field",
        "summary": "Wrapper struct (Own<T>, Ref<T>): if wrapper.method() fails, try wrapper.field.method() or wrapper.as_ptr().method().",
        "triggers": {"E0599", "no method"},
        "detail": (
            "Many wrapper types (Own<T>, Ref<T>) don't auto-delegate "
            "methods to their inner pointer. If `wrapper.foo()` reports "
            "'no method foo', access the inner field directly: "
            "`wrapper.inner.foo()`, `wrapper.as_ptr()`, or "
            "`unsafe { (*wrapper.as_ptr()).foo() }`."
        ),
    },
    {
        "id": "generic_field",
        "summary": "Generic params have no fields. `(*this).field` where this: *const E (E generic) is rejected; cast to a concrete type first.",
        "triggers": {"E0609", "unknown field"},
        "detail": (
            "Rust does not know the layout of an unbounded generic parameter, so "
            "field access is rejected. If you have `*const E` and you know the "
            "concrete wrapping struct is `ErrorImpl<E>`, cast first: "
            "`let p = ptr as *const ErrorImpl<E>; (*p).known_field`."
        ),
    },
    {
        "id": "nonnull_ref",
        "summary": "NonNull<T> doesn't auto-deref. Get a reference via unsafe { ptr.as_ref() }. Construct via NonNull::from(&val) or NonNull::new_unchecked(raw).",
        "triggers": {"E0308", "NonNull", "expected `&", "mismatched types"},
        "detail": (
            "NonNull<T> is a raw pointer wrapper, not Deref. Conversion table:\n"
            "  NonNull<T> → &T          : `unsafe { ptr.as_ref() }`\n"
            "  NonNull<T> → *mut T      : `ptr.as_ptr()`\n"
            "  &T → NonNull<T>          : `NonNull::from(&val)`\n"
            "  *mut T → NonNull<T>      : `unsafe { NonNull::new_unchecked(raw) }`\n"
            "Don't pass NonNull where &T is expected. Don't call constructors\n"
            "like `Ref::new(non_null_value)` — read the constructor's signature\n"
            "from TYPES_IN_SCOPE first."
        ),
    },
    {
        "id": "fn_ptr_field",
        "summary": "Function-pointer FIELDS need parens to call: (s.callback)(args), NOT s.callback(args). Method-style fails because the field isn't a method.",
        "triggers": {"E0599", "no method", "is not a method", "is a field", "callback", "vtable"},
        "detail": (
            "When a struct field holds a function pointer (e.g. `fn(X) -> Y`),\n"
            "you cannot invoke it with method syntax. Wrap the field access in\n"
            "parens before the call:\n"
            "  WRONG: vtable.object_downcast(this, target)\n"
            "  RIGHT: (vtable.object_downcast)(this, target)\n"
            "This is the standard Rust idiom for vtable dispatch. anyhow's\n"
            "`ErrorVTable` exposes its dispatch fns as fields, not methods."
        ),
    },
    {
        "id": "match_move_borrow",
        "summary": "`match self.field { ... }` moves the field. To match without moving use `match &self.field { ... }`, `match self { Foo { ref field, .. } => ... }`, or destructure with `ref`.",
        "triggers": {"E0507", "cannot move out", "moved here", "behind a shared reference"},
        "detail": (
            "Pattern matching against `self.field` consumes (moves) the field.\n"
            "When you only have `&self`, this fails with E0507. Three fixes:\n"
            "  1. Borrow the scrutinee: `match &self.field { Foo { x } => ... }`.\n"
            "     The bindings will be `&T` references.\n"
            "  2. Use `ref`: `match self { Foo { ref field } => ... }`.\n"
            "  3. Re-borrow inside arms: `Buffered { rest } => rest.iter().len()`\n"
            "     instead of `rest.len()` if `rest` itself is owned-by-binding.\n"
            "Pick (1) for most cases — least invasive."
        ),
    },
    {
        # Two `impl AsRef<T1> for X` and `impl AsRef<T2> for X` blocks must
        # remain SEPARATE. Collapsing them into one impl produces E0201
        # (duplicate method) — Rust has no overloading.
        "id": "split_trait_impl",
        "summary": "Two `impl Trait<A> for X` and `impl Trait<B> for X` blocks must stay SEPARATE — never merge into one impl block. Rust has no method overloading; same-named methods in one impl is E0201.",
        "triggers": {"E0201", "duplicate definitions", "duplicate definition"},
        "detail": (
            "If rustc reports `duplicate definitions with name`, the two same-named\n"
            "methods belong in TWO distinct trait impls, each with its own header:\n"
            "  WRONG:\n"
            "    impl AsRef<dyn StdError + Send + Sync> for Error {\n"
            "        fn as_ref(&self) -> &(dyn StdError + Send + Sync + 'static) { ... }\n"
            "        fn as_ref(&self) -> &(dyn StdError + 'static) { ... }  // E0201\n"
            "    }\n"
            "  RIGHT:\n"
            "    impl AsRef<dyn StdError + Send + Sync> for Error {\n"
            "        fn as_ref(&self) -> &(dyn StdError + Send + Sync + 'static) { ... }\n"
            "    }\n"
            "    impl AsRef<dyn StdError> for Error {\n"
            "        fn as_ref(&self) -> &(dyn StdError + 'static) { ... }\n"
            "    }\n"
            "Cross-check against FR / SIBLING_REFERENCE for the exact list of\n"
            "trait headers — never invent overloads under one header."
        ),
    },
    {
        # The trait header's generic parameter IS the return type — they must
        # match character-for-character including Send/Sync/'static bounds.
        "id": "trait_header_signature",
        "summary": "For `impl AsRef<T> for X`, the fn return type must match `T` exactly — including `+ Send + Sync + 'static` bounds. Mismatch is E0053.",
        "triggers": {"E0053", "incompatible type for trait", "expected signature"},
        "detail": (
            "E0053 means the impl's fn signature does not match the trait's\n"
            "expected signature. Read the rustc note carefully:\n"
            "  = note: expected signature `fn(&Error) -> &(dyn ... + Send + Sync + 'static)`\n"
            "                  found signature `fn(&Error) -> &(dyn ... + 'static)`\n"
            "The fix: align the fn's return type with what the trait header demands.\n"
            "Common case: `impl AsRef<dyn Trait + Send + Sync> for X` requires the\n"
            "fn to return `&(dyn Trait + Send + Sync + 'static)`, NOT `&(dyn Trait + 'static)`.\n"
            "Before writing, copy the trait header's generic parameter verbatim into\n"
            "the fn return position."
        ),
    },
    {
        # A fn returning T by value cannot inherit T: ?Sized from its impl
        # block — by-value return requires a known size at compile time.
        "id": "unsized_by_value_return",
        "summary": "A fn returning `T` by value cannot inherit `T: ?Sized` from its enclosing impl. Either add `where T: Sized` to that fn, or change return to `&T`/`Box<T>`/`*const T`.",
        "triggers": {"E0277", "doesn't have a size known at compile-time", "?Sized", "statically known size"},
        "detail": (
            "When an `impl<T> X<T> where T: ?Sized` contains a fn whose return\n"
            "type is bare `T` (by value), rustc rejects it: the return slot needs\n"
            "a known size at compile time.\n"
            "  WRONG:\n"
            "    impl<'a, T> Mut<'a, T> where T: ?Sized {\n"
            "        pub unsafe fn read(self) -> T { ... }  // E0277\n"
            "    }\n"
            "  RIGHT (tighten just this fn):\n"
            "    impl<'a, T> Mut<'a, T> where T: ?Sized {\n"
            "        pub unsafe fn read(self) -> T where T: Sized { ... }\n"
            "    }\n"
            "Or change the return type to a pointer/reference (`&T`, `*const T`,\n"
            "`Box<T>`) if the API genuinely supports unsized values. Removing the\n"
            "`?Sized` bound from the impl block is usually wrong — other fns rely on it."
        ),
    },
    {
        # Rule C: when a target API takes M: Display, pass the value
        # directly — do NOT format!() to a string first. format!() needs
        # an allocator (std or alloc::format) and may not be in scope,
        # particularly in no_std clusters.
        "id": "display_passthrough",
        "summary": "If a target API takes `M: Display`, pass the Display value DIRECTLY. Don't `format!(\"{}\", x)` first — `format!` needs an allocator and is often out of scope. `Error::msg(value)` works when `value: impl Display`.",
        "triggers": {"format", "cannot find macro", "not found in scope"},
        "detail": (
            "Many anyhow APIs accept any Display value:\n"
            "  pub fn Error::msg<M: Display + Debug + Send + Sync + 'static>(message: M)\n"
            "Pass the Display value straight through. Don't materialise it\n"
            "to a String first.\n"
            "  WRONG: Error::msg(format!(\"{}\", context))   // needs alloc::format\n"
            "  RIGHT: Error::msg(context)                    // context: impl Display\n"
            "Only fall back to `format!` when concatenating multiple values —\n"
            "and even then prefer `core::fmt::Write` on a buffer."
        ),
    },
    {
        # Starvation-miner evidence (runs 6390/187469): E0133 on
        # MaybeUninit::assume_init inside a safe fn — the model writes the
        # right call but forgets the unsafe block.
        "id": "unsafe_requires_block",
        "summary": "Calling an `unsafe fn` (e.g. `MaybeUninit::assume_init`, raw-pointer deref) needs an `unsafe { ... }` block around the CALL — even inside `unsafe fn` bodies, wrap the exact unsafe operation.",
        "triggers": {"E0133", "requires unsafe block", "is unsafe and requires"},
        "detail": (
            "E0133 means an unsafe operation sits in safe code. Wrap the exact\n"
            "call, not the whole body:\n"
            "  WRONG:\n"
            "    let value = self.slot.assume_init();          // E0133\n"
            "  RIGHT:\n"
            "    let value = unsafe { self.slot.assume_init() };\n"
            "  WRONG:\n"
            "    let inner = *self.ptr.as_ptr();               // E0133\n"
            "  RIGHT:\n"
            "    let inner = unsafe { *self.ptr.as_ptr() };\n"
            "Keep the unsafe block MINIMAL — one operation per block, so the\n"
            "compiler still checks everything around it."
        ),
    },
    {
        # Starvation-miner evidence (run 187469): E0433 `cannot find `format`\n
        # in `core`` — alloc macros do not live in core.
        "id": "no_core_format",
        "summary": "`format!`/`vec!`/`String` are NOT in `core` — never write `core::format!`. In no_std paths pass the Display value through, or `write!` into a buffer via `core::fmt::Write`.",
        "triggers": {"E0433", "in `core`", "could not find `format`",
                     "cannot find macro `format`"},
        "detail": (
            "`core` has no allocator, so alloc macros don't resolve there:\n"
            "  WRONG:\n"
            "    let s = core::format!(\"{x}\");        // E0433: not in core\n"
            "  RIGHT (no allocation — pass the Display value through):\n"
            "    target.push_msg(x)                     // x: impl Display\n"
            "  WRONG:\n"
            "    let s = format!(\"{a}{b}\");            // no_std: unresolved\n"
            "  RIGHT (build into a stack buffer):\n"
            "    use core::fmt::Write;\n"
            "    let mut buf = Buffer::new();\n"
            "    write!(buf, \"{a}{b}\").ok();\n"
            "Reach for `alloc::format!` ONLY when the crate/cluster explicitly\n"
            "links `alloc`."
        ),
    },
    {
        # Starvation-miner evidence (run 2941): E0034 on an ErrorImpl method —
        # the name is provided by two in-scope traits, so the bare call is
        # ambiguous. The fix is fully-qualified call syntax, not a rename.
        "id": "ambiguous_method_e0034",
        "summary": "E0034 `multiple applicable items in scope` = a method name is provided by two traits at once, so `x.method()` is ambiguous. Disambiguate with fully-qualified syntax `Trait::method(&x)` or `<T as Trait>::method(&x)`; never rename or drop the method.",
        "triggers": {"E0034", "multiple applicable items", "candidate #"},
        "detail": (
            "Two traits in scope both define the method; name the one you mean —\n"
            "do NOT invent a differently-named method to dodge it:\n"
            "  WRONG:\n"
            "    let n = value.len();          // E0034: TraitA::len & TraitB::len\n"
            "  RIGHT (method-path form, trait named):\n"
            "    let n = TraitA::len(&value);\n"
            "  WRONG:\n"
            "    let s = self.source();        // E0034: two `source` in scope\n"
            "  RIGHT (qualified form when the receiver type must be pinned):\n"
            "    let s = <Self as StdError>::source(self);\n"
            "Pick the trait whose behavior the failing test expects; qualified\n"
            "syntax selects exactly that one and nothing else changes."
        ),
    },
    {
        # Starvation-miner evidence (run 2941): E0599 `no function or associated
        # item named `new` for struct ContextError` — an associated constructor
        # the type never defines, invented because sibling fns disagree on how to
        # build the same type. Distinct from the instance-method `no method` case.
        "id": "invented_associated_ctor",
        "summary": "E0599 `no function or associated item named X for T` = you called an associated fn/constructor (`T::new`, `T::with`) that T never defines. Build T with the struct/enum literal the type definition shows, and use the SAME construction every sibling fn uses; never invent `T::new`.",
        "triggers": {"E0599", "no function or associated item", "no associated item named"},
        "detail": (
            "The type exists but has no such associated item — a struct literal is\n"
            "available for an in-scope type whose fields you can name (a field\n"
            "private to another module makes the literal E0451 there: use the\n"
            "constructor a sibling uses), and sibling fns pin the form:\n"
            "  WRONG:\n"
            "    ContextValue::new(ctx, err)      // E0599: no `new` on the struct\n"
            "  RIGHT (struct literal from the type's shown fields):\n"
            "    ContextValue { context: ctx, error: err }\n"
            "  WRONG:\n"
            "    Wrapper::with(inner)             // E0599: no such associated fn\n"
            "  RIGHT (reuse the constructor a sibling fn already uses):\n"
            "    Wrapper::from_inner(inner)       // exists — seen in a sibling\n"
            "If several fns build the SAME type, they MUST agree on one\n"
            "construction form — read the sibling bodies / blueprint before\n"
            "introducing a new constructor name that nothing defines."
        ),
    },
    {
        # Live evidence (anyhow stable, phase-c): the model wrote
        # `Buf::bytes.len()` for a field it had itself declared moments earlier.
        # Identical E0599 wording to an invented associated fn, but the item
        # EXISTS — it is merely per-instance, so the struct-literal remedy above
        # does not apply. The compiler's own note ("perhaps you need to implement
        # it: candidate #1: std::io::Read") points at a trait impl, which is the
        # wrong repair and drags std into a no_std crate.
        "id": "field_through_type_path",
        "summary": (
            "E0599 naming an item that IS a declared field = you reached a field "
            "through the TYPE path (`T::field`). Fields live on a value: use "
            "`self.field` / `value.field`. Ignore the compiler's \"perhaps you "
            "need to implement it: candidate #1: <trait>\" note — implementing a "
            "trait to reach your own field is always the wrong repair."
        ),
        "triggers": {"E0599", "no function or associated item",
                     "no associated item named",
                     "perhaps you need to implement it",
                     "items from traits can only be used if"},
        "detail": (
            "Check the type definition FIRST: if the name is listed as a field,\n"
            "the item exists and only the PATH is wrong — `T::field` never\n"
            "resolves, for any T.\n"
            "  WRONG:\n"
            "    if Wrapper::payload.len() > 0 { … }   // E0599 on a real field\n"
            "  RIGHT (a field is reached through a value, never the type):\n"
            "    if self.payload.len() > 0 { … }\n"
            "  WRONG (wanted the array's capacity, no instance in scope):\n"
            "    let cap = Holder::slots.len();        // E0599 on a real field\n"
            "  RIGHT (take the capacity from the declared type, not the field):\n"
            "    const CAP: usize = 32;                // from `slots: [T; 32]`\n"
            "    let cap = CAP;\n"
            "A trait that happens to define the same NAME is a coincidence, not\n"
            "the fix: implementing it changes the type's observable behaviour and\n"
            "can pull in `std` items a no_std crate must not reference."
        ),
    },
    {
        # Live evidence (anyhow nightly cluster, 2026-07-09): the model wrote
        # `feature = "error_generic_member_access"` — but compiler-capability
        # cfgs set via RUSTFLAGS `--cfg NAME` are BARE cfg names, not cargo
        # features; the mixup silently compiles the wrong branch out.
        "id": "cfg_name_vs_feature",
        "summary": "A cfg set via RUSTFLAGS `--cfg name` (e.g. error_generic_member_access, nightly probes) is a BARE cfg: `#[cfg(name)]`. `#[cfg(feature = \"name\")]` only matches cargo features declared in Cargo.toml — writing one for the other silently deactivates the code.",
        "triggers": {"unexpected `cfg` condition", "expected values for `feature`",
                     "unexpected_cfgs", "error_generic_member_access"},
        "detail": (
            "Two different namespaces — match the one the build actually sets:\n"
            "  WRONG:\n"
            "    #[cfg(feature = \"error_generic_member_access\")]   // not a cargo feature\n"
            "    fn provide(...) { ... }\n"
            "  RIGHT (bare cfg — set by RUSTFLAGS --cfg error_generic_member_access):\n"
            "    #[cfg(error_generic_member_access)]\n"
            "    fn provide(...) { ... }\n"
            "  WRONG:\n"
            "    if cfg!(nightly_probe) { ... }        // when Cargo.toml declares it as a feature\n"
            "  RIGHT:\n"
            "    if cfg!(feature = \"nightly_probe\") { ... }\n"
            "Check the error's own note: `expected values for feature are: ...`\n"
            "lists the REAL cargo features — anything not in that list is a bare cfg."
        ),
    },
    {
        # Live evidence (anyhow "stable" cluster, run 495747): a trait method
        # `fn not(self) -> bool` and a SEPARATE free dispatcher fn
        # `pub fn not(cond: impl Bool) -> bool` share a name; every fill for
        # the trait impls also failed a stub in the free fn, whose body used
        # `self` even though its OWN parameter is `cond` — E0424 "expected
        # value, found module `self`" fired 12+ times identically across the
        # run (starvation_miner cause=unknown, error_codes=['E0424']).
        "id": "self_is_not_a_free_fn_parameter",
        "summary": "E0424 `expected value, found module self` = you wrote `self` inside a fn that has NO `self` parameter (a free fn or a fn with a differently-named parameter). Use THAT fn's own parameter name — `self` only exists inside a method whose signature actually declares it.",
        "triggers": {"E0424", "expected value, found module", "can't have a `self`", "this function can't have"},
        "detail": (
            "`self` is only a bound name inside a method whose OWN signature\n"
            "declares it (`fn f(self)`/`fn f(&self)`/`fn f(&mut self)`). A FREE\n"
            "function — even one that calls a method also named `f` — has its own\n"
            "parameter name; writing `self` there resolves to the CURRENT MODULE\n"
            "path instead (E0424), not the parameter.\n"
            "  WRONG (dispatcher's own parameter is `value`, not `self`):\n"
            "    trait Marker { fn flag(self) -> bool; }\n"
            "    pub fn flag(value: impl Marker) -> bool {\n"
            "        self.flag()                    // E0424 — no `self` here\n"
            "    }\n"
            "  RIGHT (use the fn's own parameter):\n"
            "    pub fn flag(value: impl Marker) -> bool {\n"
            "        value.flag()\n"
            "    }\n"
            "  WRONG (same mixup with a differently-shaped helper):\n"
            "    pub fn describe(item: impl Show) -> String {\n"
            "        format!(\"{}\", self)            // E0424 — parameter is `item`\n"
            "    }\n"
            "  RIGHT:\n"
            "    pub fn describe(item: impl Show) -> String {\n"
            "        format!(\"{}\", item)\n"
            "    }\n"
            "Before writing `self` in ANY fn body, check that fn's OWN signature\n"
            "for a `self`/`&self`/`&mut self` parameter — if it isn't there\n"
            "(free fn, or a parameter under a different name), use that\n"
            "parameter's actual name instead."
        ),
    },
    {
        # Live evidence (anyhow, recurring across runs): whole-file rewrites of
        # error.rs kept importing `crate::backtrace::{backtrace, backtrace_if_absent}`
        # — helpers the sibling module never defines — exploding one file's 3
        # errors into crate-wide E0432/E0433 storms.
        "id": "import_must_exist_in_sibling",
        "summary": "E0432/E0433 `unresolved import crate::mod::item` = the sibling module does NOT define that item. Never import a helper you haven't seen in the pulled sibling file; either define the helper IN this file, or request the sibling and use what it actually exports.",
        "triggers": {"E0432", "E0433", "unresolved import", "cannot find", "no `"},
        "detail": (
            "An import is a CLAIM about another file — verify it against the\n"
            "sibling's real contents (pull the file), don't assume:\n"
            "  WRONG (helper assumed, never defined anywhere):\n"
            "    use crate::helpers::{compute, compute_if_absent};   // E0432\n"
            "    let b = compute!();\n"
            "  RIGHT (self-contained — define what you need where you need it):\n"
            "    fn compute() -> Value { ... }        // local, no import\n"
            "  WRONG (guessing a module path):\n"
            "    use crate::util::cache::lookup;      // E0433: no `cache` in util\n"
            "  RIGHT (use the item the pulled sibling actually exports):\n"
            "    use crate::util::lookup;             // seen in src/util.rs\n"
            "If the design NEEDS a shared helper that doesn't exist yet, put it\n"
            "in the file you are editing — you cannot edit the sibling from here."
        ),
    },
    {
        # Observed live (anyhow nightly, impl ErrorImpl): the model wrote
        # `fn provide<'a>(this: Ref<'a, Self>, ...) { ... Self::error(this) }`
        # as a NESTED fn inside another method's body — every `Self` there is
        # E0401, because a nested item opens a fresh generic scope that cannot
        # see the enclosing impl's `Self`/type params. Burned attempts re-emitting
        # the same nested shape.
        "id": "self_from_outer_item_nested_fn",
        "summary": "E0401 `can't use Self from outer item` = you declared a `fn` INSIDE another fn's body and referenced `Self` (or `Self::assoc`, or an outer generic like `E`) in it. A nested `fn` opens a fresh scope and cannot see the enclosing impl's `Self`. Either make it a SIBLING associated fn of the impl (where `Self` is in scope), or name the concrete type.",
        "triggers": {"E0401", "can't use `Self` from outer item",
                     "use of `Self` from outer item", "from outer item",
                     "can't use generic parameters from outer item"},
        "detail": (
            "A nested `fn` is a fresh item with its OWN (empty) generic scope —\n"
            "it does not inherit `Self` or the type params of the impl/fn around\n"
            "it. Referencing them there is E0401. Two fixes:\n"
            "  WRONG (nested fn names `Self` as a type — E0401):\n"
            "    impl Node {\n"
            "        fn walk(&self) {\n"
            "            fn visit(n: &Self) { /* ... */ }   // E0401\n"
            "            visit(self);\n"
            "        }\n"
            "    }\n"
            "  RIGHT (name the concrete type — `Self` is `Node` here):\n"
            "    impl Node {\n"
            "        fn walk(&self) {\n"
            "            fn visit(n: &Node) { /* ... */ }\n"
            "            visit(self);\n"
            "        }\n"
            "    }\n"
            "  WRONG (nested fn calls `Self::assoc` and uses outer generic `E` —\n"
            "  E0401 on every `Self`):\n"
            "    impl Table<E> {\n"
            "        fn get(this: Ref<Self>) {\n"
            "            fn helper(this: Ref<Self>) -> &E {   // E0401 x2\n"
            "                unsafe { Self::field(this) }     // E0401\n"
            "            }\n"
            "            let _ = helper(this);\n"
            "        }\n"
            "    }\n"
            "  RIGHT (hoist it to a SIBLING associated fn — inside the impl\n"
            "  block `Self` and `E` are both in scope):\n"
            "    impl Table<E> {\n"
            "        fn get(this: Ref<Self>) { let _ = Self::helper(this); }\n"
            "        fn helper(this: Ref<Self>) -> &E {\n"
            "            unsafe { Self::field(this) }\n"
            "        }\n"
            "    }\n"
            "Rule: if a body needs `Self`, `Self::x`, or an outer type param, it\n"
            "must be an associated fn/method of the impl — never a `fn` declared\n"
            "inside another fn's body."
        ),
    },
    {
        # Live evidence (anyhow stable, phase-c): three retro rounds burned
        # cycling `Backtrace::default()` → `disabled()` → `capture()` →
        # `force_capture()` on `the trait bound `Backtrace: Default` is not
        # satisfied`. `Default` is not universal — many std types deliberately
        # omit it because "empty" has no meaning for them, and `#[derive(Default)]`
        # on a struct holding one fails for the same reason.
        "id": "std_type_without_default",
        "summary": "E0277 `the trait bound `T: Default` is not satisfied` on a STD type = that type has no `Default` impl; `T::default()`, `unwrap_or_default()` and `#[derive(Default)]` on a struct containing it are all unavailable. Call the type's own named constructor instead.",
        "triggers": {"E0277", "Default` is not satisfied",
                     "the trait `Default` is not implemented",
                     "doesn't implement `Default`",
                     "cannot find function `default`"},
        "detail": (
            "Reach for the type's OWN constructor, not a `Default` that does not\n"
            "exist. Guessing the next Default-shaped call (`default()` →\n"
            "`new()` → `unwrap_or_default()`) never converges — read the\n"
            "constructors the type actually exposes and pick the one whose\n"
            "meaning you want.\n"
            "  WRONG (`std::backtrace::Backtrace` has no `Default`):\n"
            "    let bt = Backtrace::default();\n"
            "    let bt = maybe_bt.unwrap_or_default();\n"
            "  RIGHT (named constructors — pick by MEANING):\n"
            "    let bt = Backtrace::capture();        // capture if enabled\n"
            "    let bt = Backtrace::force_capture();  // always capture\n"
            "    let bt = maybe_bt.unwrap_or_else(Backtrace::disabled);\n"
            "  WRONG (`std::time::Instant` has no `Default` either):\n"
            "    #[derive(Default)]\n"
            "    struct Timer { start: Instant }       // E0277 on the derive\n"
            "  RIGHT (write the impl and name the constructor):\n"
            "    impl Default for Timer {\n"
            "        fn default() -> Self { Timer { start: Instant::now() } }\n"
            "    }\n"
            "Rule: `Default` means 'a meaningful empty value exists'. When the\n"
            "compiler says a std type lacks it, that is a design fact about the\n"
            "type — no other `Default`-shaped call will work either."
        ),
    },
    {
        # Fires on the U2 abort feedback, not on a compiler code: an aborting
        # body compiles cleanly. The failure mode is invisible in the usual
        # channel (no `error[E…]`, no assertion, no panic message), so without
        # this the model reads only the leftover assertions and keeps swapping
        # API calls while the real fault — what the body does to memory — goes
        # unaddressed.
        "id": "test_process_aborted",
        "summary": "A test process that ABORTS (tests produce no output at all, not a failure message) is undefined behaviour or a panic during unwinding — never a wrong API choice. Re-derive the body from the types; do not resend it with a different helper call.",
        "triggers": {"ABORTED the test process",
                     "produced no output at all",
                     "the process died"},
        "detail": (
            "An abort is not a failed assertion. libtest CATCHES a normal\n"
            "`panic!` and prints `---- name stdout ----` with the message; if\n"
            "there is no output at all, the process died outside that path and\n"
            "took every later test in the binary with it. Two causes, both in\n"
            "what the body does to memory:\n"
            "  WRONG (transmuting a pointer to a type the allocation never held —\n"
            "         reads garbage as a vtable/length, aborts or segfaults):\n"
            "    let obj: &dyn Trait = unsafe { &*(ptr as *const T as *const U) };\n"
            "  RIGHT (recover the SAME type that was stored, then coerce):\n"
            "    let obj: &T = unsafe { &*(ptr as *const T) };\n"
            "    let obj: &dyn Trait = obj;\n"
            "  WRONG (taking ownership out of a borrow, then dropping it twice —\n"
            "         the second drop aborts while the first is still unwinding):\n"
            "    let owned = unsafe { core::ptr::read(slot) };\n"
            "    drop(owned);                 // `slot` is still live and will drop\n"
            "  RIGHT (either forget the original or never own it twice):\n"
            "    let owned = unsafe { core::ptr::read(slot) };\n"
            "    core::mem::forget(original);  // exactly one owner drops\n"
            "Rule: when the process aborts, the previous body's SHAPE is wrong,\n"
            "not its API. Re-derive from the declared types — which pointer holds\n"
            "what, and who owns each value — instead of substituting another call."
        ),
    },
]


def gotchas_summary() -> str:
    """Compact one-line-per-gotcha cheatsheet (always shown)."""
    return "\n".join(f"- {g['summary']}" for g in RUST_GOTCHAS)


def gotchas_for_error(error_excerpt: str) -> str:
    """Full detail blocks for whichever gotchas the current error triggers.

    Selective injection: empty string when no trigger matches. Keeps the
    prompt small for unrelated errors and adds detailed guidance only when
    the model is actually fighting one of these confusion classes.
    """
    if not error_excerpt:
        return ""
    low = error_excerpt.lower()
    matched: list[dict[str, Any]] = []
    for g in RUST_GOTCHAS:
        for trig in g["triggers"]:
            if trig.lower() in low:
                matched.append(g)
                break
    if not matched:
        return ""
    parts = []
    for g in matched:
        parts.append(f"## {g['summary']}\n{g['detail']}")
    return "\n\n".join(parts)


def tag_block(tag: str, content: str, *, prefix: str = "", suffix: str = "") -> str:
    """Render one optional prompt section:
    `\\n<TAG>\\n{prefix}{content}{suffix}\\n</TAG>\\n`, or "" when content is
    empty. The one wrapper for every optional section — previously
    re-implemented inline per builder, which is how builders drifted apart
    (a section added to one but not the others)."""
    if not content:
        return ""
    return f"\n<{tag}>\n{prefix}{content}{suffix}\n</{tag}>\n"


def format_impl_block_for_prompt(
    block: ImplBlock,
    mark_target: bool = True,
    refill_fn_names: frozenset[str] = frozenset(),
    *,
    fill_fn_names: frozenset[str] | None = None,
    inferred_fn_names: frozenset[str] = frozenset(),
    crashed_fn_names: frozenset[str] = frozenset(),
    aimed_fn_names: frozenset[str] = frozenset(),
    candidate_fn_names: frozenset[str] = frozenset(),
    companion_fn_names: frozenset[str] = frozenset(),
) -> str:
    """Render an impl block in TARGET form: the impl header followed by every
    member's signature + a one-line body status. Stub members are tagged
    [STUB] so the model knows which to fill; already-filled members carry a
    shortened body preview so the model can mirror their structural shape
    when designing the new ones. Members named in `refill_fn_names`
    (behavioral re-fill targets: compiling but test-failing) get the FULL body
    (no truncation) tagged [REFILL] instead of the FILLED preview, so the model
    can actually rewrite them, not just pattern-match them.

    `fill_fn_names` — the stubs THIS response is asked for. None keeps the old
    reading (every stub is a target). When given, a stub outside it is a
    member the cluster's cfg never compiles (or one already handled): it is
    rendered as a stub the model must NOT answer. Until this distinction
    existed, `provide` under the stable cluster read "[STUB] — fill in this
    response" while the header listed only the active members, the model
    answered it, and the splice refused the body (`not_in_cluster_cfg`) —
    four blocks on 2026-08-20 (run `902902-daee577d`), two of which lost
    their whole reply that way.

    `inferred_fn_names` — re-fill members whose attribution is inference only
    (`domain.refill_evidence`): tagged `[REFILL?]` with an instruction to
    return the body unchanged when it is right, instead of the certainty
    "its test below FAILS — rewrite" that had a correct `source` body rewritten
    and `construct_from_context` degraded on the same day.

    `crashed_fn_names` — re-fill members a runtime fault's own frame named.
    They are tagged `[REFILL!]` and NEVER `[REFILL?]`: a crashed binary
    reports no verdict, so these units reach the prompt with no convicting
    test and used to render as "implicated by NAME only … else return it
    unchanged" — the weakest instruction in the vocabulary, handed to the one
    body the tree had just faulted inside. `src/error.rs::drop` was given that
    tag on every sweep of runs `842437`…`1519852` (2026-08-29/30), returned
    unchanged as instructed, and kept 13 tests unmeasurable the whole time.

    `aimed_fn_names` — re-fill members a callee of the faulting frame. Tagged
    `[REFILL]` with re-derive-from-types, never `[REFILL?]`. Aimed producers
    are kept out of `crash_blamed`, so without this set they arrive as
    inference. Run `1911637-d0fab212` listed `object_downcast` as
    `[REFILL?]` while `test_large_alignment` still SIGABRTed.

    `candidate_fn_names` — for a RELATED block: members that are the
    same-named re-fill candidates for the same failing tests, shown in full
    as the other candidate rather than as a settled `/* FILLED */` preview.

    `companion_fn_names` — filled members the pair will call and must not
    rewrite (a crash-recovered table slot whose attributed tests already
    pass). Shown in full with an instruction not to emit an `<FN>`. Empty
    (the default) leaves every non-refill filled member as the truncated
    `/* FILLED */` preview.

    `block` is an `ImplBlock` produced by `_collect_impl_blocks`.
    """
    container = block.container
    lines: list[str] = []
    if container:
        # Trim the {fns…} body from the container header — we render each fn
        # explicitly below. The extractor's container string captures the
        # entire impl header up to the opening brace; some variants tack on
        # the first fn signature. Cut at the first newline or `{` to keep
        # just the header.
        header = container
        brace_pos = header.find("{")
        if brace_pos != -1:
            header = header[:brace_pos].rstrip()
        lines.append(f"{header} {{")
    else:
        lines.append("// free fns at file level")
    for m in block.members:
        sig = m.sig_text.strip()
        is_stub = m.has_unimplemented_body
        if is_stub:
            if not mark_target:
                tag = " // [STUB]"
            elif fill_fn_names is None or m.fn_name in fill_fn_names:
                tag = " // [STUB] — fill in this response"
            else:
                tag = (" // [STUB] — NOT in this response (cfg-gated out of "
                       "this cluster or handled elsewhere): do NOT emit an "
                       "<FN> for it")
            lines.append(f"    {sig} {tag}")
        elif m.fn_name in refill_fn_names:
            body = m.body_text.strip().replace("\n", "\n    ")
            if m.fn_name in crashed_fn_names:
                # Checked before `inferred_fn_names`: the crash is what
                # removed this unit's convicting test, so it arrives graded
                # as inference and would otherwise be told to stand pat.
                lines.append(
                    f"    {sig} {{ /* [REFILL!] a test binary CRASHED inside "
                    f"this body — see CRASH_EVIDENCE. It compiles; it is "
                    f"still WRONG. Rewrite it — do NOT return it unchanged "
                    f"*/ {body} }}")
            elif m.fn_name in aimed_fn_names:
                lines.append(
                    f"    {sig} {{ /* [REFILL] a test binary CRASHED and this "
                    f"body is a callee of the faulting frame — the pointer "
                    f"the blamed frame dereferenced is produced here. "
                    f"Re-derive it from the types it is generic over; do "
                    f"NOT return it unchanged */ {body} }}")
            elif m.fn_name in inferred_fn_names:
                lines.append(
                    f"    {sig} {{ /* [REFILL?] compiles; implicated by NAME "
                    f"only — see REFILL_TARGETS. Rewrite ONLY if wrong for "
                    f"these bounds, else return it unchanged */ {body} }}")
            else:
                lines.append(f"    {sig} {{ /* [REFILL] compiles but its test "
                              f"below FAILS — rewrite the BEHAVIOR */ {body} }}")
        elif m.fn_name in companion_fn_names:
            body = m.body_text.strip().replace("\n", "\n    ")
            lines.append(
                f"    {sig} {{ /* FILLED — this pair will call this body; "
                f"do NOT emit an <FN> for it. It compiles and its tests "
                f"pass; rewriting it reddens tests it already pays */ "
                f"{body} }}")
        elif m.fn_name in candidate_fn_names:
            body = m.body_text.strip().replace("\n", "\n    ")
            lines.append(
                f"    {sig} {{ /* FILLED — same-named re-fill candidate for "
                f"the SAME failing tests (tried separately); shown in full */ "
                f"{body} }}")
        else:
            body = m.body_text.strip()
            preview = body
            if len(preview) > 200:
                preview = preview[:200].rsplit("\n", 1)[0] + " /* ... */"
            preview = preview.replace("\n", "\n    ")
            lines.append(f"    {sig} {{ /* FILLED */ {preview} }}")
    if container:
        lines.append("}")
    return "\n".join(lines)


def _tuple_struct_fields(fields: tuple[FieldRecord, ...]) -> bool:
    """True iff `fields` are the positional fields of a tuple struct
    (numeric or empty names) — `Quoted<D>(D)` records a single field `0`."""
    return bool(fields) and all((not f.name) or f.name.isdigit() for f in fields)


def world_gate_attr(member: FieldRecord | MethodRecord) -> str:
    """`#[cfg(…)] ` for a member only some cfg worlds compile, else "".

    The gate is the member's own effective cfg, spelled so a body can copy it:
    a reference to the member is legal only under the same predicate, and a
    struct literal has to carry the attribute on that field's initialiser.
    The extractor's token-spaced spelling is tightened; the predicate is
    otherwise verbatim."""
    if not member.worlds or not member.cfg_predicates:
        return ""
    preds = [p.replace(" (", "(").replace("( ", "(").replace(" )", ")")
              .replace(" ,", ",") for p in member.cfg_predicates]
    pred = preds[0] if len(preds) == 1 else "all(" + ", ".join(preds) + ")"
    return f"#[cfg({pred})] "


def world_only_note(member: FieldRecord | MethodRecord) -> str:
    """The trailing comment naming the worlds a gated member exists in."""
    if not member.worlds:
        return ""
    return ("   // exists ONLY in cfg world "
            + ", ".join(f"`{w}`" for w in member.worlds))


def _construct_hint(rec: TypeRecord) -> str | None:
    """A `construct:` line showing HOW to build `rec`, derived deterministically
    from the frozen definition/fields (a legitimate, always-correct input).

    Kills the dominant E0599 class: the model pulls a type, sees its fields, then
    writes the universal `Type::new(...)` idiom that doesn't exist. Showing the
    real literal (tuple `Type(a, b)` / struct `Type { f: … }`) replaces the guess.
    Placeholders are the field TYPES so the shape is unambiguous.
    """
    if rec.kind != "struct":
        return None
    name = rec.name or "?"
    fields = rec.fields
    deff = rec.definition or ""
    # Tuple struct: positional fields (numeric/empty names), or a `Name( … )`
    # definition with no brace. Named struct: real field names / a `{ … }` def.
    if _tuple_struct_fields(fields):
        args = ", ".join(f"<{f.ty}>" for f in fields)
        return (f"  construct: {name}({args})   "
                f"// tuple struct — positional; NOT {name}::new(...)")
    if not fields and "(" in deff and "{" not in deff:
        return (f"  construct: {name}(<fields...>)   "
                f"// tuple struct — positional args; NOT {name}::new(...)")
    if fields:
        # A field only some cfg worlds compile is initialised under its own
        # gate; the literal without it fails E0560 where the field is absent,
        # and the literal without the field fails E0063 where it exists.
        body = ", ".join(f"{world_gate_attr(f)}{f.name}: <{f.ty}>" for f in fields)
        gated = (" — keep each `#[cfg(…)]` on its field"
                 if any(world_gate_attr(f) for f in fields) else "")
        return (f"  construct: {name} {{ {body} }}   "
                f"// struct literal; NOT {name}::new(...){gated}")
    if "{" not in deff and "(" not in deff:
        return f"  construct: {name}   // unit struct — no fields"
    return None


# Bounds on the doc lines a type carries into a fill prompt: the constructor
# fill prompt already measures 121 147 chars on the live `stable` tree
# (2026-08-26), and a contract sentence is one or two lines by the authoring
# rule — three lines / 200 chars each keeps a documented type to ~600 chars.
_TYPE_DOC_MAX_LINES = 3
_TYPE_DOC_MAX_CHARS = 200

# Budget for doc regions kept BEYOND the leading window because they state an
# observable a failing test pins. The leading-window cap above is sized for the
# one-or-two-line contract sentence the layout author writes; the UPSTREAM
# crate's own doc on the same item is a worked example of runtime behaviour and
# is long by nature — `Error` on the live `stable` tree is 101 doc lines /
# 3 087 chars, with `Stack backtrace:` at doc line 50 (2026-09-01, run
# `617614-0abaf2d0`). Two dispatches of the formatter pair guessed at that
# header from an assertion because the block stating it was cut; neither
# guess is satisfiable, since no formatting of a `std::backtrace::Backtrace`
# emits it. These bound what a pinned region may add to a prompt already
# measuring ~77 000 chars.
_TYPE_DOC_PINNED_MAX_LINES = 30
_TYPE_DOC_PINNED_MAX_CHARS = 1600
_DOC_ELISION = "/// …"


def _leading_doc_texts(definition: str) -> list[str]:
    """Texts of the leading `///` block in order, blank doc lines INCLUDED as
    `""` and inner indentation preserved (one rustdoc leading space removed).

    Structure is what the pinned-region selection reads: a fenced example and
    the paragraph introducing it are both runs of these entries. Blank RAW
    lines and `//!` / `//` are skipped without ending the block; the first
    attribute or the item head ends it, exactly where `_type_layout_attrs`
    begins reading."""
    out: list[str] = []
    for raw in definition.splitlines():
        line = raw.strip()
        if not line or line.startswith("//!") or (
                line.startswith("//") and not line.startswith("///")):
            continue
        if not line.startswith("///"):
            break
        text = line[3:]
        out.append(text[1:] if text.startswith(" ") else text)
    return out


def _fence_spans(texts: list[str]) -> list[tuple[int, int]]:
    """(open, close) index pairs of the ``` fenced blocks in a doc block. An
    unclosed fence runs to the end — a doc that opens one and stops is still
    an example, and dropping it would lose exactly the content this selection
    exists to keep."""
    spans: list[tuple[int, int]] = []
    open_i: int | None = None
    for i, t in enumerate(texts):
        if not t.strip().startswith("```"):
            continue
        if open_i is None:
            open_i = i
        else:
            spans.append((open_i, i))
            open_i = None
    if open_i is not None:
        spans.append((open_i, len(texts) - 1))
    return spans


def _doc_region(texts: list[str], hit: int,
                spans: list[tuple[int, int]]) -> tuple[int, int]:
    """The doc region a hit at index `hit` belongs to.

    Inside a fence: the whole fence PLUS the prose paragraph immediately
    above it. Rustdoc's universal shape is that a fenced example is
    introduced by the sentence over it, and that sentence is where the
    CONDITION under which the example holds is stated — on the live tree,
    `and if there is a backtrace available:` sits directly above the block
    carrying `Stack backtrace:`, and it is the conditional two fills failed
    to establish. Outside a fence: the contiguous non-blank paragraph."""
    for a, b in spans:
        if a <= hit <= b:
            j = a - 1
            while j >= 0 and not texts[j].strip():
                j -= 1
            if j < 0 or texts[j].strip().startswith("```"):
                return a, b
            while j >= 0 and texts[j].strip() and not texts[
                    j].strip().startswith("```"):
                j -= 1
            return j + 1, b
    start = hit
    while start > 0 and texts[start - 1].strip():
        start -= 1
    end = hit
    while end + 1 < len(texts) and texts[end + 1].strip():
        end += 1
    return start, end


def _pinned_doc_indices(texts: list[str],
                        observables: frozenset[str]) -> list[int]:
    """Indices of the doc lines to keep because some observable is stated in
    their region, in source order, under both pinned budgets. Match is a
    plain substring of the whitespace-collapsed line — the observables arrive
    collapsed and unescaped for exactly that (`asserted_output_literals`)."""
    if not observables:
        return []
    spans = _fence_spans(texts)
    regions: list[tuple[int, int]] = []
    for i, t in enumerate(texts):
        line = collapse_ws(t)
        if not line or not any(o in line for o in observables):
            continue
        region = _doc_region(texts, i, spans)
        if not any(a <= region[0] and region[1] <= b for a, b in regions):
            regions.append(region)
    kept: list[int] = []
    used = 0
    for a, b in sorted(set(regions)):
        for i in range(a, b + 1):
            if i in kept:
                continue
            cost = len(texts[i][:_TYPE_DOC_MAX_CHARS]) + 5
            if (len(kept) >= _TYPE_DOC_PINNED_MAX_LINES
                    or used + cost > _TYPE_DOC_PINNED_MAX_CHARS):
                return kept
            kept.append(i)
            used += cost
    return kept


def _type_doc_lines(definition: str,
                    observables: frozenset[str] = frozenset()) -> list[str]:
    """The LEADING `///` doc lines of a type's rendered `definition`, trimmed,
    at most `_TYPE_DOC_MAX_LINES` of `_TYPE_DOC_MAX_CHARS` each (an over-long
    line ends in `…`, and so does the last kept line when more follow) — plus,
    when `observables` is non-empty, the doc regions further down that STATE
    one of them.

    Authored contract lines reach the fill prompts only through this renderer.
    `format_type_for_prompt` rebuilds the type line from kind/name/fields/
    methods and, until this function existed, read `definition` for nothing
    but its `repr`/`derive` attributes — so a `///` line t1.5 wrote on a type
    was spliced into the skeleton, survived the extractor (which prints doc
    comments FIRST in `render_item_definition`), and was dropped here: the
    live `stable` tree's documented `Chain` rendered bare (2026-08-26). Only
    the leading block counts — `//!` inner docs and `//` comments are not docs
    of the item and are skipped; the first attribute or the item head ends the
    block, exactly where `_type_layout_attrs` begins reading.

    `observables` is the set of literal texts the target's attributed failing
    tests assert appear in some rendering. A crate that documents its own
    output format documents the answer to those assertions, and the leading
    window — three lines, sized for an authored contract sentence — cuts it
    out: `Error`'s doc rendered as two lines and an ellipsis while its
    `Stack backtrace:` example sat at doc line 50, and two formatter fills
    guessed the header from the assertion instead (run `617614-0abaf2d0`,
    2026-09-01, both reverted on the same ten sibling tests). Empty
    `observables` renders byte-identically to before this argument existed,
    so every prompt with no attributed failing test is unchanged."""
    texts = _leading_doc_texts(definition)
    out: list[str] = []
    truncated = False
    leading_idx: set[int] = set()
    for i, text in enumerate(texts):
        text = text.strip()
        if not text:
            continue
        if len(out) >= _TYPE_DOC_MAX_LINES:
            truncated = True
            break
        if len(text) > _TYPE_DOC_MAX_CHARS:
            text = text[:_TYPE_DOC_MAX_CHARS - 1].rstrip() + "…"
        out.append(f"/// {text}")
        leading_idx.add(i)
    if truncated and out and not out[-1].endswith("…"):
        out[-1] += " …"
    pinned = [i for i in _pinned_doc_indices(texts, observables)
              if i not in leading_idx]
    if not pinned:
        return out
    if out:
        out.append(_DOC_ELISION)
    prev: int | None = None
    for i in pinned:
        if prev is not None and i != prev + 1:
            out.append(_DOC_ELISION)
        text = texts[i]
        if len(text) > _TYPE_DOC_MAX_CHARS:
            text = text[:_TYPE_DOC_MAX_CHARS - 1].rstrip() + "…"
        out.append(f"/// {text}".rstrip())
        prev = i
    return out


def _type_layout_attrs(definition: str) -> list[str]:
    """The layout-deciding outer attributes on a type — `#[repr(...)]` and
    `#[derive(...)]` — pulled from the record's rendered `definition`.

    A `repr` is part of a type's contract exactly as its fields are: it decides
    which casts are legal, and the pipeline already treats it as load-bearing
    elsewhere (`carry_frozen_repr`, the layout pins). The compact type line the
    prompt renders from `name`/`fields` drops it, so a producer rewrite that has
    to reason about a transparent wrapper — `object_downcast::<M>` is sound only
    because `MessageError<M>` is `#[repr(transparent)]` — never sees the fact
    that makes its own cast legal (measured: `95878` iter 2, the constructor
    REWRITE reached the delegation defect in its own reasoning and could not
    name the transparent-cast escape; the type line read bare
    `struct MessageError< M >`). `cfg` is deliberately excluded — it gates
    presence, not the type's cast/trait contract, and is carried on other
    channels. Scans only the LEADING attribute block (before the `struct`/
    `enum`/`pub` keyword) so an attribute on an inner field is never hoisted.

    The block is whatever precedes the item's first code line: `definition`
    is `prettyplease::unparse` of the whole item (`render_item_definition`
    in the extractor), which prints the doc comments FIRST, as `///` lines,
    and only then the attributes — so a documented type reads
    `/// Iterator of a chain…` / `#[derive(Clone)]` / `pub struct Chain`.
    Comment lines (`///`, `//!`, `//`) and blank lines are skipped, not
    treated as the end of the block, or every documented type would hide its
    own repr behind its docs (live stable tree: `Chain`). An attribute the
    printer wraps across lines (`#[derive(\\n    Clone,\\n    Debug\\n)]`) is
    joined on its bracket depth before the head is read."""
    out: list[str] = []
    pending: list[str] = []
    depth = 0
    for raw in definition.splitlines():
        line = raw.strip()
        if pending:
            pending.append(line)
            depth += line.count("[") - line.count("]")
            if depth <= 0:
                _emit_layout_attr(" ".join(pending), out)
                pending = []
                depth = 0
            continue
        if not line or line.startswith(("///", "//!", "//")):
            continue
        if not line.startswith("#["):
            break  # the item's first code line ends the outer-attribute block
        depth = line.count("[") - line.count("]")
        if depth > 0:
            pending = [line]
            continue
        _emit_layout_attr(line, out)
    return out


def _emit_layout_attr(attr_line: str, out: list[str]) -> None:
    """Append `#[repr(...)]` / `#[derive(...)]` from one complete attribute
    line; any other attribute (`cfg`, `doc`, `must_use`, …) is dropped."""
    inner = attr_line[2:].rsplit("]", 1)[0].strip()
    head = inner.split("(", 1)[0].strip()
    if head in ("repr", "derive"):
        out.append(f"#[{' '.join(inner.split())}]")


def _trim_sig(sig: str) -> str:
    """A method signature cut to the prompt's budget: keep up to 120 chars
    (covers virtually all real fn sigs; verbose where-clauses are what goes)."""
    sig = sig or ""
    if len(sig) > 120:
        sig = sig[:117] + "..."
    return sig.strip()


def _private_field_lines(
    rec: TypeRecord, private: list[FieldRecord], viewer_file: str,
) -> list[str]:
    """The `private fields` block of a struct rendered for a body in ANOTHER
    module: one header naming the declaring file and the viewer file, then
    one line per field carrying the accessor that returns it — or the fact
    that none is declared.

    Run `3003-93e9a9bf` (2026-09-02, cluster `stable`): the pair prompt for
    `src/error.rs::fmt` + `src/fmt.rs::debug` rendered `ErrorImpl` (declared
    in `src/error.rs`; `vtable`, `backtrace`, `_object`, none `pub`) under
    `fields (access with .name):` 15 times. The `src/fmt.rs` half read
    `impl_ref.backtrace` directly — `error[E0616]: field backtrace of struct
    ErrorImpl is private` on 3 of its 6 attempts (two on `backtrace`, one on
    `vtable`), roughly 25 GPU-minutes each — while the record's own
    `unsafe fn backtrace(this: Ref<Self>) -> &Backtrace` sat a few lines
    below under methods. The rendering invited the error; this block states
    the rule the compiler applies and puts the accessor on the field's line.
    """
    with_accessors = [(f, accessor_methods_for_field(rec, f)) for f in private]
    reach = ("reach each one through the accessor on its line"
             if any(ms for _, ms in with_accessors)
             else "no method of this type returns them")
    # The construction a body there cannot write: a named literal is E0451
    # (private field in a struct literal); a tuple struct's constructor call
    # `Name(..)` is E0603 (private tuple struct constructor).
    literal = (f"the `{rec.name or '?'}(..)` constructor is E0603 there"
               if _tuple_struct_fields(rec.fields)
               else "a struct literal naming them is E0451 there")
    lines = [
        f"  private fields — declared in {rec.file}, NOT accessible from "
        f"{viewer_file} (no .name access: E0616; {literal}; {reach}):",
    ]
    for f, ms in with_accessors:
        via = (" / ".join(_trim_sig(m.sig) for m in ms) if ms
               else "(no accessor declared here)")
        lines.append(f"    {world_gate_attr(f)}{f.name}: {f.ty}   → {via}"
                     f"{world_only_note(f)}")
    return lines


#: The sentence that makes the list usable. A list the reader can see is
#: partial is a list it cannot reason from by absence: run `450977-5f48b68a`
#: (2026-09-21) had crate types rendered with no impls beside external types
#: rendered with theirs, and the fill's trace asked about one impl 14 times
#: and concluded 9 times that it was "omitted from the snippet". Auto traits
#: and the standard library's blanket impls are not headers this crate writes,
#: so the sentence says so rather than claim they are absent.
_TRAIT_IMPLS_BODY = (
    "  trait impls (COMPLETE: every impl this crate writes whose self type is "
    "this type, any file; a trait not listed is NOT implemented for it by an "
    "impl that names this type — auto traits and std blanket impls aside"
)
TRAIT_IMPLS_HEADER = _TRAIT_IMPLS_BODY + "):"
TRAIT_IMPLS_HEADER_WITH_BLANKETS = (
    _TRAIT_IMPLS_BODY
    + "; an impl this crate writes for a type parameter is not in this list, "
    "it is in the crate blanket list in this section):")
# The record's crate writes type-parameter impls and this rendering does not
# print them: the sentence must not point at a list that is not there, and
# must not let the per-type list read as covering them either.
TRAIT_IMPLS_HEADER_BLANKETS_NOT_SHOWN = (
    _TRAIT_IMPLS_BODY
    + "; this crate also writes impls for a type parameter, not shown here, "
    "which may implement a trait not listed):")
BLANKET_IMPLS_HEADER = (
    "  crate blanket impls (COMPLETE: every impl this crate writes whose self "
    "type is a type parameter, any file; a trait not listed is NOT "
    "implemented for any type by such an impl — auto traits and std blanket "
    "impls aside):")
# A cap that lands inside either list used to keep the COMPLETE sentence and
# drop the lines under it, which is the absence lie the sentence forbids.
_LIST_OMITTED = (
    "  trait-impl list omitted: this record did not fit the section, so the "
    "list is not shown and is not a statement that any impl is absent.")
_COMPLETE_HEADERS = (
    TRAIT_IMPLS_HEADER, TRAIT_IMPLS_HEADER_WITH_BLANKETS,
    TRAIT_IMPLS_HEADER_BLANKETS_NOT_SHOWN, BLANKET_IMPLS_HEADER)


def trait_impl_lines(rec: TypeRecord) -> list[str]:
    """The `trait impls` block of a crate type rendered on its own: nothing
    beside it prints the crate's type-parameter impls, so when the record
    carries any the sentence says they exist and are not shown."""
    return _trait_impl_lines(rec, blankets_shown=False)


def _trait_impl_lines(rec: TypeRecord, *, blankets_shown: bool) -> list[str]:
    """The `trait impls` block of a crate type: one line per header the
    extractor found, the declaring file beside it. Headers only (the line the
    skeleton keeps after strip). Nothing for an external record (it carries
    its own `trait impls:` note) or a record whose extractor predates the
    field (`trait_impls is None`) — a list printed as complete must be
    complete. An empty reading prints the header with `(none)`, which is a
    statement the reader can use. When the record carries blanket impls,
    absence here does not cover an impl whose self type is a type parameter:
    the sentence points at the crate blanket list when `blankets_shown` (only
    `render_types_section`, which prints that list, passes it), and says the
    list is not shown otherwise."""
    if rec.external_path or rec.trait_impls is None or rec.kind == "trait":
        return []
    if not rec.blanket_impls:
        header = TRAIT_IMPLS_HEADER
    elif blankets_shown:
        header = TRAIT_IMPLS_HEADER_WITH_BLANKETS
    else:
        header = TRAIT_IMPLS_HEADER_BLANKETS_NOT_SHOWN
    out = [header]
    if not rec.trait_impls:
        out.append("    (none)")
    for t in rec.trait_impls:
        where = f"  // {t.file}" if t.file else ""
        out.append(f"    {t.header}{where}")
    return out


def blanket_impl_block(records: Sequence[TypeRecord]) -> str:
    """The crate's type-parameter impls, once. Empty when no record carries
    any: the per-type sentence then does not point at a list that is not
    here. The same headers are stored on every record of one enumeration;
    the first occurrence wins so the block is stable."""
    chosen: list[TraitImplRecord] = []
    seen: set[tuple[str, str]] = set()
    for rec in records:
        for t in rec.blanket_impls or ():
            key = (t.header, t.file)
            if key in seen:
                continue
            seen.add(key)
            chosen.append(t)
    if not chosen:
        return ""
    out = [BLANKET_IMPLS_HEADER]
    for t in chosen:
        where = f"  // {t.file}" if t.file else ""
        out.append(f"    {t.header}{where}")
    return "\n".join(out)


def _first_complete_header(block: str) -> int | None:
    found = [i for h in _COMPLETE_HEADERS if (i := block.find(h)) >= 0]
    return min(found) if found else None


def _fit_block(block: str, cap: int) -> str:
    """`block` when it fits. Otherwise a prefix that does not end inside a
    list whose header says the list is complete, plus a note that the list
    was omitted. A block with no such header is sliced: it makes no absence
    claim."""
    if len(block) <= cap:
        return block
    at = _first_complete_header(block)
    if at is None:
        return block[:cap]
    prefix = block[:at].rstrip()
    if not prefix:
        return _LIST_OMITTED[:cap]
    candidate = prefix + "\n" + _LIST_OMITTED
    if len(candidate) <= cap:
        return candidate
    room = cap - len(_LIST_OMITTED) - 1
    if room > 0:
        return prefix[:room].rstrip() + "\n" + _LIST_OMITTED
    return prefix[:cap]


def join_type_blocks(blocks: list[str], cap: int) -> str:
    """Join rendered type blocks under a char cap. A later block that does
    not fit is dropped whole. A first block that does not fit is cut before
    a COMPLETE list (`TRAIT_IMPLS_HEADER`, `BLANKET_IMPLS_HEADER`): slicing
    after that header would state that an impl the cut removed does not
    exist."""
    out: list[str] = []
    used = 0
    for b in blocks:
        sep = 2 if out else 0
        if used + sep + len(b) <= cap:
            out.append(b)
            used += sep + len(b)
            continue
        if not out:
            fitted = _fit_block(b, cap)
            if fitted:
                out.append(fitted)
        break
    return "\n\n".join(out)


def render_types_section(
    records: Sequence[TypeRecord],
    observables: frozenset[str] = frozenset(),
    *,
    viewer_file: str = "",
    cap: int | None = None,
) -> str:
    """Type blocks for a prompt, with the crate's blanket impls printed once
    ahead of them. The one renderer whose per-type sentence may point at the
    blanket list, because it is the one that prints it.

    `cap` drops later blocks whole and will not slice a complete list. The
    first record outranks the blanket list: when the two do not fit
    together, the list is left out (the per-type sentence then says it is
    not shown) and the first record is shown, cut before its own impl list
    if it alone exceeds the cap. Printing the list first and dropping the
    record whole would leave a section with no type in it."""
    blanket = blanket_impl_block(records)
    if blanket:
        shown = [_type_block(r, observables, viewer_file=viewer_file,
                             blankets_shown=True) for r in records]
        if cap is None:
            return "\n\n".join([blanket, *shown])
        if len(blanket) + 2 + len(shown[0]) <= cap:
            return join_type_blocks([blanket, *shown], cap)
    plain = [_type_block(r, observables, viewer_file=viewer_file,
                         blankets_shown=False) for r in records]
    if cap is None:
        return "\n\n".join(plain)
    return join_type_blocks(plain, cap)


def _slot_fit_lines(f: FieldRecord, type_file: str) -> list[str]:
    """For a fn-pointer field, the crate fns that fit it — the only names a
    table literal can put there — or the statement that none does. Nothing
    for any other field, or a record read without the crate's fns
    (`fits is None`): a list printed as complete must be complete."""
    if f.fits is None:
        return []
    if not f.fits:
        return ["      fits: NONE — no crate fn is declared with a signature "
                "this field can hold"]
    def _one(c: SlotFit) -> str:
        notes = [n for n in (c.file if c.file and c.file != type_file else "",
                             f"only if {c.requires}" if c.requires else "") if n]
        return f"{c.name} ({'; '.join(notes)})" if notes else c.name

    names = ", ".join(_one(c) for c in f.fits)
    return [f"      fits: {names}"]


def format_type_for_prompt(
    rec: TypeRecord, observables: frozenset[str] = frozenset(),
    *, viewer_file: str = "",
) -> str:
    """One type record on its own. It cannot point at the crate blanket list,
    because it does not print one; a prompt section is
    `render_types_section`."""
    return _type_block(rec, observables, viewer_file=viewer_file,
                       blankets_shown=False)


def _type_block(
    rec: TypeRecord, observables: frozenset[str] = frozenset(),
    *, viewer_file: str, blankets_shown: bool,
) -> str:
    """Render a type record as a compact Rust-like definition for prompts.

    The rendering includes a field-vs-method annotation: each name is shown with `.x`
    for fields and `.x()` for methods so the model doesn't confuse them.

    Methods are shown with their FULL SIGNATURE so the model knows
    exactly what each takes/returns. Without this it kept calling e.g.
    `Ref::new(NonNull<T>)` when `Ref::new` actually takes `&T` — visible
    method NAME but invisible argument types.

    The layout-deciding outer attributes (`repr`, `derive`) are rendered above
    the type line — see `_type_layout_attrs` — and the type's leading `///`
    doc lines above those (`_type_doc_lines`): the contract sentence t1.5
    authors on an item has no other route into a fill prompt, and was
    dropped here before that renderer existed.

    `observables` are the literal texts the target's attributed failing tests
    assert appear in some rendering; a doc region of this type that STATES one
    of them survives the leading-lines cap. Default empty renders exactly as
    before the argument existed.

    `viewer_file` is the file the prompt is writing. Given, a struct's fields
    split by `field_accessible_from`: the accessible ones render under the
    `fields (access with .name):` header as before, the ones private to
    another file under a `private fields` header that names both files and
    carries each field's accessor (`_private_field_lines`), and the
    `construct:` hint is dropped — a struct literal from another module
    cannot name a private field. Empty (every caller without a file) renders
    byte-identically to before the argument existed.
    """
    kind = rec.kind or "?"
    name = rec.name or "?"
    generics = rec.generics or ""
    fields = rec.fields
    variants = rec.variants
    methods = rec.methods
    definition = rec.definition or ""
    lines = [*_type_doc_lines(definition, observables),
             *_type_layout_attrs(definition),
             f"{kind} {name}{generics}"]
    private: list[FieldRecord] = []
    if fields:
        accessible: list[FieldRecord] = []
        for f in fields:
            (accessible if field_accessible_from(rec, f, viewer_file)
             else private).append(f)
        if accessible:
            lines.append(
                "  fields (access with .name; `fits:` under a fn-pointer field "
                "is the COMPLETE list of crate fns it can hold):"
                if any(f.fits is not None for f in accessible)
                else "  fields (access with .name):")
            for f in accessible:
                pub = "pub " if f.is_pub else ""
                lines.append(f"    {world_gate_attr(f)}{pub}{f.name}: {f.ty}"
                             f"{world_only_note(f)}")
                lines.extend(_slot_fit_lines(f, rec.file))
        if private:
            lines.extend(_private_field_lines(rec, private, viewer_file))
    if variants:
        lines.append("  variants:")
        for v in variants[:8]:
            lines.append(f"    {v}")
        if len(variants) > 8:
            lines.append(f"    ... ({len(variants) - 8} more)")
    # A struct literal written in another module cannot name a private field
    # (E0451), so the literal the hint shows is not writable there.
    _ctor = None if private else _construct_hint(rec)
    if _ctor:
        lines.append(_ctor)
    if methods:
        # Dedup by method name (same fn can appear in multiple impl blocks);
        # keep the first sig we saw. Then render WITH SIGNATURE.
        seen: set[str] = set()
        # (name, sig, is_exactly_pub, cfg gate, worlds note)
        sigs: list[tuple[str, str, bool, str, str]] = []
        for m in methods:
            mn = m.name
            if mn and mn not in seen:
                seen.add(mn)
                # Conjunction: is_exactly_pub defaults True when absent (pre-
                # field artifact / stale extractor) — alone it would render a
                # PRIVATE method as `pub`; is_pub can only narrow.
                sigs.append((mn, _trim_sig(m.sig),
                             bool(m.is_pub and m.is_exactly_pub),
                             world_gate_attr(m), world_only_note(m)))
        if sigs:
            lines.append("  methods (sig shown; call with .name(args)):")
            # Cap to top 10 methods to keep prompts compact for types with
            # many. The first 10 are typically the most-used.
            for mn, sig, is_pub, gate, only in sigs[:10]:
                # `pub ` is literal rendered text, so it must reflect TRUE
                # external visibility (is_exactly_pub), not `pub(crate)`.
                vis = "pub " if is_pub else ""
                if sig:
                    lines.append(f"    {gate}{vis}{sig}{only}")
                else:
                    lines.append(f"    {gate}{vis}fn {mn}(...)  // sig unknown{only}")
            if len(sigs) > 10:
                rest = ", ".join(n for n, *_ in sigs[10:])
                if len(rest) > 200:
                    rest = rest[:197] + "..."
                lines.append(f"    // also: {rest}")
    # After the methods, so a section cap that cannot fit this record cuts
    # the complete impl list off the end and keeps the fields and methods.
    lines.extend(_trait_impl_lines(rec, blankets_shown=blankets_shown))
    return "\n".join(lines)


def build_dispatch_family_section(
    family: DispatchFamily, *, single_call_char_limit: int,
) -> str:
    """Render the <DISPATCH_FAMILY> prompt block: the family's traits, the
    dispatch method, and the dispatching macro_rules! source — the behavioural
    spec the co-design fill must satisfy. Macro defs are capped so the section
    can't dominate the budget."""
    traits = ", ".join(family.traits)
    method = family.method
    macros = ", ".join(family.macros)
    chained = ", ".join(family.chained_methods)
    defs = list(family.macro_defs)
    _cap = max(2000, single_call_char_limit // 8)
    macro_text = "\n\n".join(defs)
    if len(macro_text) > _cap:
        macro_text = macro_text[:_cap] + "\n/* … macro truncated … */"
    tag_list = family.return_types
    trait_list = family.traits
    # Concrete WRONG→RIGHT examples (generic placeholder names, NOT crate
    # source) — the model acts on examples, not on "autoref-specialization"
    # jargon. Two examples so the pattern generalises.
    ex = (
        "Each trait's `" + (method or "kind") + "` returns ITS OWN distinct tag; "
        "the macro chains a builder on that tag. Get any one tag/return wrong and "
        "the macro silently resolves to the wrong branch everywhere.\n"
        "  WRONG (two impls return the SAME tag → dispatch collapses):\n"
        "    impl KindA for A { fn kind(self) -> TagA { TagA } }\n"
        "    impl KindB for B { fn kind(self) -> TagA { TagA } }   // should be TagB\n"
        "  RIGHT (each returns its OWN tag; builders match per tag):\n"
        "    impl KindA for A { fn kind(self) -> TagA { TagA } }\n"
        "    impl KindB for B { fn kind(self) -> TagB { TagB } }\n"
        "    impl TagA { fn build(self, x: A) -> Out { /* A-specific */ } }\n"
        "    impl TagB { fn build(self, x: B) -> Out { /* B-specific */ } }\n"
        "  WRONG (inventing one shared name across impls — there is no such item):\n"
        "    fn kind(self) -> Tag { SHARED_TAG }                   // E0425, undefined\n"
        "  RIGHT (return the per-impl tag value directly):\n"
        "    fn kind(self) -> TagA { TagA }\n"
    )
    _map = ""
    if trait_list and tag_list and len(trait_list) == len(tag_list):
        _map = ("In THIS family map each trait to its own tag: "
                + ", ".join(f"`{t}`→`{g}`" for t, g in zip(trait_list, tag_list, strict=False))
                + (f", then `.{chained}(...)` builds the result on that tag. "
                   if chained else ". "))
    return (
        "\n<DISPATCH_FAMILY>\n"
        f"`{traits}` are dispatched together by macro(s) `{macros}` "
        f"(`x.{method}()" + (f".{chained}()" if chained else "") + "...`) — fill "
        "them in this one response, consistently.\n"
        f"{ex}{_map}"
        "The dispatching macro (the behaviour these must satisfy):\n```rust\n"
        f"{macro_text}\n```\n</DISPATCH_FAMILY>\n"
    )


from pipeline_core.domain.text import smart_truncate


def format_api_surface_section(api_surface: str, *, cap: int) -> str:
    """Wrap the captured API surface into a labelled prompt section, head+tail
    truncated to `cap`. Returns '' if api_surface is empty."""
    if not api_surface or not api_surface.strip():
        return ""
    text = smart_truncate(
        api_surface,
        total_limit=cap,
        head=2 * cap // 3,
        tail=cap // 3,
    )
    return (
        "\n--- Public API surface (ground truth — pre-strip skeleton) ---\n"
        "Use these signatures and type definitions exactly. Do not invent items not\n"
        "listed here, do not change signatures, and do not contradict the shapes shown\n"
        "below. The behavioral semantics (what each function returns, invariants)\n"
        "are still derived from the tests; this skeleton only defines the SHAPE.\n"
        f"{text}\n"
    )
