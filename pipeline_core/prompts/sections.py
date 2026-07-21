"""Pure prompt-section renderers + curated guidance data (slice 8).

Text→text only: no I/O, no run state, no regex. The two impure
collaborators the builders need (the pipeline's file-view annotator and
promoted-error rules) live behind :mod:`pipeline_core.prompts.runtime`.
Bodies moved verbatim from pipeline.py (leading underscores dropped on
extraction — they are this module's public entry points).
"""

from __future__ import annotations

from typing import Any

from pipeline_core.domain.models import DispatchFamily, ImplBlock, TypeRecord

# P5.3: standard library trait contracts. When a fn is being implemented
# inside `impl Trait for X`, inject the trait's documented behavior so the
# model knows what's expected (return type semantics, format conventions,
# etc.). Hardcoded for the most common stdlib traits — covers ~90% of impl
# blocks in typical Rust crates.
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
    """Return the standard contract text for the trait this fn implements,
    or empty string if no matching contract is known. P5.3.
    """
    name = extract_trait_from_container(container)
    if not name:
        return ""
    return TRAIT_CONTRACTS.get(name, "")


# Six Rust confusion classes seen recurrently in overnight runs (P3.2).
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
        # FR-040: the vtable / shared-machinery class. You fill fn BODIES only —
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
            "ALWAYS available for an in-scope type, and sibling fns pin the form:\n"
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
    empty. Slice 8a: the one wrapper for every optional section — previously
    re-implemented inline per builder, which is how builders drifted apart
    (a section added to one but not the others)."""
    if not content:
        return ""
    return f"\n<{tag}>\n{prefix}{content}{suffix}\n</{tag}>\n"


def format_impl_block_for_prompt(
    block: ImplBlock,
    mark_target: bool = True,
    refill_fn_names: frozenset[str] = frozenset(),
) -> str:
    """Render an impl block in TARGET form: the impl header followed by every
    member's signature + a one-line body status. Stub members are tagged
    [STUB] so the model knows which to fill; already-filled members carry a
    shortened body preview so the model can mirror their structural shape
    when designing the new ones. Members named in `refill_fn_names` (debt #46
    point 2 — behavioral re-fill targets: compiling but test-failing) get the
    FULL body (no truncation) tagged [REFILL] instead of the FILLED preview,
    so the model can actually rewrite them, not just pattern-match them.

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
            tag = " // [STUB] — fill in this response" if mark_target else " // [STUB]"
            lines.append(f"    {sig} {tag}")
        elif m.fn_name in refill_fn_names:
            body = m.body_text.strip().replace("\n", "\n    ")
            lines.append(f"    {sig} {{ /* [REFILL] compiles but its test "
                          f"below FAILS — rewrite the BEHAVIOR */ {body} }}")
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
    if fields and all((not f.name) or f.name.isdigit() for f in fields):
        args = ", ".join(f"<{f.ty}>" for f in fields)
        return (f"  construct: {name}({args})   "
                f"// tuple struct — positional; NOT {name}::new(...)")
    if not fields and "(" in deff and "{" not in deff:
        return (f"  construct: {name}(<fields...>)   "
                f"// tuple struct — positional args; NOT {name}::new(...)")
    if fields:
        body = ", ".join(f"{f.name}: <{f.ty}>" for f in fields)
        return (f"  construct: {name} {{ {body} }}   "
                f"// struct literal; NOT {name}::new(...)")
    if "{" not in deff and "(" not in deff:
        return f"  construct: {name}   // unit struct — no fields"
    return None


def format_type_for_prompt(rec: TypeRecord) -> str:
    """Render a type record as a compact Rust-like definition for prompts.

    P5.2 includes a field-vs-method annotation: each name is shown with `.x`
    for fields and `.x()` for methods so the model doesn't confuse them.

    P6.1: methods are shown with their FULL SIGNATURE so the model knows
    exactly what each takes/returns. Without this it kept calling e.g.
    `Ref::new(NonNull<T>)` when `Ref::new` actually takes `&T` — visible
    method NAME but invisible argument types.
    """
    kind = rec.kind or "?"
    name = rec.name or "?"
    generics = rec.generics or ""
    fields = rec.fields
    variants = rec.variants
    methods = rec.methods
    lines = [f"{kind} {name}{generics}"]
    if fields:
        lines.append("  fields (access with .name):")
        for f in fields:
            pub = "pub " if f.is_pub else ""
            lines.append(f"    {pub}{f.name}: {f.ty}")
    if variants:
        lines.append("  variants:")
        for v in variants[:8]:
            lines.append(f"    {v}")
        if len(variants) > 8:
            lines.append(f"    ... ({len(variants) - 8} more)")
    _ctor = _construct_hint(rec)
    if _ctor:
        lines.append(_ctor)
    if methods:
        # Dedup by method name (same fn can appear in multiple impl blocks);
        # keep the first sig we saw. Then render WITH SIGNATURE (P6.1).
        seen: set[str] = set()
        sigs: list[tuple[str, str, bool]] = []  # (name, sig, is_exactly_pub)
        for m in methods:
            mn = m.name
            if mn and mn not in seen:
                seen.add(mn)
                sig = m.sig or ""
                # Trim verbose where-clauses to fit the prompt; keep up to
                # 120 chars of the sig (covers virtually all real fn sigs).
                if len(sig) > 120:
                    sig = sig[:117] + "..."
                # Conjunction: is_exactly_pub defaults True when absent (pre-
                # field artifact / stale extractor) — alone it would render a
                # PRIVATE method as `pub`; is_pub can only narrow.
                sigs.append((mn, sig.strip(), bool(m.is_pub and m.is_exactly_pub)))
        if sigs:
            lines.append("  methods (sig shown; call with .name(args)):")
            # Cap to top 10 methods to keep prompts compact for types with
            # many. The first 10 are typically the most-used.
            for mn, sig, is_pub in sigs[:10]:
                # `pub ` is literal rendered text, so it must reflect TRUE
                # external visibility (is_exactly_pub), not `pub(crate)`.
                vis = "pub " if is_pub else ""
                if sig:
                    lines.append(f"    {vis}{sig}")
                else:
                    lines.append(f"    {vis}fn {mn}(...)  // sig unknown")
            if len(sigs) > 10:
                rest = ", ".join(n for n, _, _ in sigs[10:])
                if len(rest) > 200:
                    rest = rest[:197] + "..."
                lines.append(f"    // also: {rest}")
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
