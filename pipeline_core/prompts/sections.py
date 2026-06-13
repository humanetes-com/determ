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
            "Many wrapper types in anyhow (Own<T>, Ref<T>) don't auto-delegate "
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
) -> str:
    """Render an impl block in TARGET form: the impl header followed by every
    member's signature + a one-line body status. Stub members are tagged
    [STUB] so the model knows which to fill; already-filled members carry a
    shortened body preview so the model can mirror their structural shape
    when designing the new ones.

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
    if methods:
        # Dedup by method name (same fn can appear in multiple impl blocks);
        # keep the first sig we saw. Then render WITH SIGNATURE (P6.1).
        seen: set[str] = set()
        sigs: list[tuple[str, str, bool]] = []  # (name, sig, is_pub)
        for m in methods:
            mn = m.name
            if mn and mn not in seen:
                seen.add(mn)
                sig = m.sig or ""
                # Trim verbose where-clauses to fit the prompt; keep up to
                # 120 chars of the sig (covers virtually all real fn sigs).
                if len(sig) > 120:
                    sig = sig[:117] + "..."
                sigs.append((mn, sig.strip(), bool(m.is_pub)))
        if sigs:
            lines.append("  methods (sig shown; call with .name(args)):")
            # Cap to top 10 methods to keep prompts compact for types with
            # many. The first 10 are typically the most-used.
            for mn, sig, is_pub in sigs[:10]:
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
