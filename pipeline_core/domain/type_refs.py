"""Pure type-reference resolution for per-fn fill prompts (H/§7.3).

`types_referenced_in_text` picks the `TypeRecord`s a fn's prompt should expose:
the types named directly in the text (signature/container) plus one level of
field-transitive expansion (so a field `inner: Own<ErrorImpl>` pulls in `Own`
and `ErrorImpl` even when only `Error` matched directly). Relocated verbatim
from pipeline.py. Pure: stdlib + domain only.

`visible_from` is the fill's file. Without it the selection is crate-wide, which
is how `trait StdError` inside `mod ext` in `context.rs` reached an `error.rs`
TYPES_IN_SCOPE table with `fn ext_context` and `call with .name(args)` — a body
there then failed `error[E0603]: module ext is private` (run `155810-cbb3b9fc`,
2026-08-28). `is_pub` cannot drop it: the trait is `pub`, and trait methods are
recorded `is_pub=false`. The wall is `mod_path`.
"""

from __future__ import annotations

from dataclasses import replace

from pipeline_core.domain.impl_routing import norm_src_rel
from pipeline_core.domain.models import FieldRecord, MethodRecord, TypeRecord
from pipeline_core.domain.rust_idents import identifier_tokens
from pipeline_core.domain.rust_scan import contains_word


def type_nameable_from(rec: TypeRecord, target_file: str) -> bool:
    """Can this type be named from `target_file`?

    Same file (or an unlocated record) is always nameable — the prompt is
    answering that file. A nonempty `mod_path` is a private-mod wall: Rust
    will not resolve the name from another file, however `is_pub` the type
    itself is. File-scope types in another file follow `is_pub`.
    """
    if not target_file or not rec.file or rec.file == target_file:
        return True
    if rec.mod_path:
        return False
    return rec.is_pub


def methods_nameable_from(rec: TypeRecord, target_file: str) -> TypeRecord:
    """Drop methods declared behind a private-mod wall in another file.

    Does not use method `is_pub`: trait methods are recorded false. A method
    with empty `file` (pre-field artifact) is kept."""
    if not target_file:
        return rec
    kept = tuple(
        m for m in rec.methods
        if not (m.file and m.file != target_file and m.mod_path)
    )
    if kept == rec.methods:
        return rec
    return replace(rec, methods=kept)


# Files whose module is the DIRECTORY they sit in rather than a directory
# named after their stem: `src/error/mod.rs` is module `error`, so its child
# modules live beside it under `src/error/`; `src/lib.rs` / `src/main.rs` are
# the crate root, whose descendants are every file under the source root.
_DIR_MODULE_FILES = frozenset({"mod.rs", "lib.rs", "main.rs"})

# Cargo's auto-discovered SEPARATE crates. After `norm_src_rel` they keep
# their prefix while library files lose `src/`, which is how a viewer in one
# of them is told apart from a descendant of the crate root: a private field
# of a `lib.rs` struct is readable from every module of the library and from
# none of these.
_SEPARATE_CRATE_DIRS = ("tests/", "benches/", "examples/")


def _module_dir(file: str) -> str:
    """Directory under which `file`'s CHILD modules live (a `norm_src_rel`
    path, no trailing slash): `error.rs` → `error`; `error/mod.rs` → `error`;
    `lib.rs` → `` (the crate root)."""
    head, _, base = file.rpartition("/")
    if base in _DIR_MODULE_FILES:
        return head
    stem = base[:-3] if base.endswith(".rs") else base
    return f"{head}/{stem}" if head else stem


def _descends_from(viewer: str, declaring: str) -> bool:
    """True iff the module of `viewer` is a descendant of the module of
    `declaring` (both `norm_src_rel` paths). Rust's privacy rule: a private
    item is visible in its module and every module nested inside it."""
    mdir = _module_dir(declaring)
    if not mdir:
        return not viewer.startswith(_SEPARATE_CRATE_DIRS)
    return viewer.startswith(mdir + "/")


def field_accessible_from(
    rec: TypeRecord, field: FieldRecord, viewer_file: str,
) -> bool:
    """Can a body written in `viewer_file` name `field` of `rec` — `x.name`,
    or `Name { name: … }` — without a privacy error?

    Rust: a field without `pub` is visible only inside the module that
    declares the struct and that module's descendants; from anywhere else
    `x.name` is E0616 and a struct literal is E0451. `is_pub` folds
    `pub(crate)`/`pub(super)` into True, which is the right reading here —
    both reach another file of the crate. Same file (or an unlocated record,
    or no viewer) is accessible: the viewer's own `mod` path inside the file
    is not known here, so the wall this draws is the file wall, as
    `type_nameable_from` does. Paths are compared through `norm_src_rel`
    because extractor records and block records disagree on the `src/`
    prefix, and a raw mismatch would read the declaring file as another
    file and every one of its own fields as private.

    Run `3003-93e9a9bf` (2026-09-02, cluster `stable`): `ErrorImpl` is
    declared in `src/error.rs` with `vtable`, `backtrace`, `_object`, none
    `pub`; the fill writing `src/fmt.rs` read `impl_ref.backtrace` directly
    and got `error[E0616]: field backtrace of struct ErrorImpl is private`
    on 3 of its 6 attempts.
    """
    if not viewer_file or not rec.file:
        return True
    if field.is_pub:
        return True
    declaring = norm_src_rel(rec.file)
    viewer = norm_src_rel(viewer_file)
    if declaring == viewer:
        return True
    return _descends_from(viewer, declaring)


# Words that occur inside a Rust type but name no type. Once the lifetime
# quote is gone, `&'static ErrorVTable` tokenises to `static` + `ErrorVTable`,
# and `ErrorImpl::error` returns `&(dyn StdError + Send + Sync + 'static)`:
# without this filter that method would read as the accessor of the `vtable`
# field on the very record that motivated `accessor_methods_for_field`.
_TYPE_KEYWORDS = frozenset({
    "dyn", "impl", "mut", "const", "static", "fn", "unsafe", "extern",
    "for", "as", "ref", "where",
})

# std containers whose name says nothing about WHAT a field holds. A field
# `backtrace: Option<Backtrace>` is reached by a method returning
# `&Backtrace`, not by every method returning an `Option` of something else.
# Dropped only when the field's type has another name to match on, so a
# field `flag: Option<()>` still matches on `Option`.
_STD_WRAPPERS = frozenset({
    "Option", "Result", "Box", "Vec", "Rc", "Arc", "Cow", "Cell", "RefCell",
    "NonNull", "PhantomData", "MaybeUninit", "ManuallyDrop", "Pin", "Mutex",
    "RwLock", "UnsafeCell",
})


def _without_lifetimes(text: str) -> str:
    """`text` with every `'name` lifetime token removed, so `identifier_tokens`
    never yields `a` for `&'a T` or `static` for `&'static T`."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "'":
            out.append(text[i])
            i += 1
            continue
        i += 1
        while i < n and (text[i].isalnum() or text[i] == "_"):
            i += 1
    return "".join(out)


def _type_words(text: str) -> frozenset[str]:
    """Identifier tokens of a Rust type text that can name a type: lifetimes
    and type-position keywords removed."""
    return frozenset(identifier_tokens(_without_lifetimes(text))) - _TYPE_KEYWORDS


def _outermost_type_name(ty: str) -> str:
    """The name of a type's outermost constructor: `PhantomData` for
    `core::marker::PhantomData<&'a mut T>`, `Option` for `Option<Backtrace>`,
    `ErrorVTable` for `&'static ErrorVTable`. Empty when nothing names one."""
    head = _without_lifetimes(ty).split("<", 1)[0]
    words = [t for t in identifier_tokens(head) if t not in _TYPE_KEYWORDS]
    return words[-1] if words else ""


def _field_type_names(ty: str) -> frozenset[str]:
    """The names a field's type is matched on: its type words, with std
    wrappers dropped whenever something more specific remains.

    A `PhantomData<…>` field holds no value, so no method returns it — the
    live `Ref`/`Mut` records carry `lifetime: PhantomData<&'a mut T>`, and
    on the bare `T` every method returning `Ref<T>` or `&mut T` read as
    that field's accessor (cluster `stable`, 2026-09-02)."""
    if _outermost_type_name(ty) == "PhantomData":
        return frozenset()
    words = _type_words(ty)
    return (words - _STD_WRAPPERS) or words


def _is_enum_kind(kind: str) -> bool:
    """A crate `enum` or an external record of that kind (`external enum`).

    Pass 3 of `types_referenced_in_text` hops only enums. External records
    prefix the item kind (`domain.external_types.external_kind`); a check
    for `kind == "enum"` alone never hops `BacktraceStatus` off
    `Backtrace::status()`, which is the record the hop exists to carry
    (run `3003-93e9a9bf`, 2026-09-02)."""
    return kind == "enum" or kind.endswith(" enum")


def _return_type(sig: str) -> str:
    """The return type of a fn signature: the text after the `->` that sits
    at bracket depth zero once the parameter list has closed, cut before a
    `where` clause or a body. `->` inside the parameters (`f: fn(u8) -> u8`)
    or inside the generics (`<F: Fn() -> u8>`) is not the return arrow.
    Empty when the signature returns `()` implicitly."""
    depth = 0   # ( [ {
    angle = 0   # < >
    params_closed = False
    i = 0
    n = len(sig)
    while i < n:
        ch = sig[i]
        if ch == "-" and i + 1 < n and sig[i + 1] == ">":
            if depth == 0 and angle == 0 and params_closed:
                rest = sig[i + 2:]
                for stop in (" where ", "{", ";"):
                    cut = rest.find(stop)
                    if cut != -1:
                        rest = rest[:cut]
                return rest.strip()
            i += 2
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if ch == ")" and depth == 0 and angle == 0:
                params_closed = True
        elif ch == "<":
            angle += 1
        elif ch == ">" and angle:
            angle -= 1
        i += 1
    return ""


def accessor_methods_for_field(
    rec: TypeRecord, field: FieldRecord,
) -> tuple[MethodRecord, ...]:
    """The record's methods whose RETURN type names a type of `field` — the
    route a body in another module has to a private field. One entry per
    method name, first declaration kept (the same fn is recorded once per
    impl block), in declaration order.

    For `backtrace: Option<Backtrace>` on `ErrorImpl` that is
    `unsafe fn backtrace(this: Ref<Self>) -> &Backtrace`; for `_object: E`
    with no method returning `E`, nothing; and for
    `vtable: &'static ErrorVTable`, nothing — `error` returning
    `&(dyn StdError + Send + Sync + 'static)` shares only the lifetime.
    """
    wanted = _field_type_names(field.ty or "")
    if not wanted:
        return ()
    out: list[MethodRecord] = []
    seen: set[str] = set()
    for m in rec.methods:
        key = m.name or m.sig
        if not m.sig or key in seen:
            continue
        ret = _return_type(m.sig)
        if ret and (_type_words(ret) & wanted):
            out.append(m)
            seen.add(key)
    return tuple(out)


def hidden_symbols_from_types(
    type_records: list[TypeRecord], target_file: str,
) -> frozenset[str]:
    """Method names whose only declaration is behind a private-mod wall in
    another file — not callable from `target_file`.

    These are names, not prose markers. The caller must test them in CALL
    position (`names_method_call`), because a method name is free to collide
    with an ordinary English word: this crate declares `Bool::not` inside
    `mod __private::not` in `src/lib.rs`, so `not` is returned here for every
    other file, and matching it as a bare word drops any note containing "not".
    `is`, `new`, `from`, `context` and `source` are method names here too.
    """
    if not target_file:
        return frozenset()
    other_walled: set[str] = set()
    visible: set[str] = set()
    for rec in type_records:
        for m in rec.methods:
            if not m.name:
                continue
            other = bool(m.file) and m.file != target_file
            if other and m.mod_path:
                other_walled.add(m.name)
            else:
                visible.add(m.name)
    return frozenset(other_walled - visible)


def types_referenced_in_text(
    text: str, all_types: list[TypeRecord],
    visible_from: str = "",
) -> list[TypeRecord]:
    """Return type records that the fn's prompt should expose, with one
    level of transitive expansion through fields.

    Pass 1: direct text match — types whose `name` appears in `text` as a
    standalone identifier. Picks up names mentioned in the fn signature
    or container.

    Pass 2: field-transitive expansion — for each type matched in pass 1,
    parse the type names out of its fields' Rust type strings and pull
    those records in too. This is what lets the model see `Own::by_ref()`
    when the only direct match was `Error` (whose field is
    `inner: Own<ErrorImpl>`). Without this step the model sees the field
    type as a bare name with no API and starts inventing methods.

    Word boundaries guard against `Boxed` matching when searching for
    `Box`. Field expansion is one level only — going deeper would bloat
    the prompt.

    `visible_from` nonempty filters to types (and methods) nameable from
    that file. Empty keeps the crate-wide selection, which is every
    existing call site that has not yet been given a file.
    """
    if not text or not all_types:
        return []
    candidates = (
        [r for r in all_types if type_nameable_from(r, visible_from)]
        if visible_from else all_types
    )
    by_name = {rec.name: rec for rec in candidates if rec.name}
    out: list[TypeRecord] = []
    seen_names: set[str] = set()
    for rec in candidates:
        name = rec.name
        if not name or name in seen_names:
            continue
        if contains_word(text, name):
            out.append(rec)
            seen_names.add(name)

    # Pass 2: expand by one field-hop. For each matched type, look at the
    # type names appearing in its fields and pull those records in.
    field_hop: list[TypeRecord] = []
    for rec in list(out):
        for f in rec.fields or ():
            ty = f.ty or ""
            if not ty:
                continue
            # Extract identifier-shaped tokens from the field's type
            # string. Tokens like `Own`, `ErrorImpl`, `Backtrace` get
            # added; generics and primitives are dropped via the
            # by_name lookup (we only keep names that match real records).
            for tok in identifier_tokens(ty):
                if tok in seen_names:
                    continue
                hop_rec = by_name.get(tok)
                if hop_rec is None:
                    continue
                field_hop.append(hop_rec)
                seen_names.add(tok)
    out.extend(field_hop)
    # Pass 3: one hop through the matched records' method RETURN types, for
    # ENUMS only. A caller that holds a `Backtrace` reads `status()`, and
    # the enum it returns is what the branch is written against —
    # `BacktraceStatus` names no field and appears in no signature of the
    # slice, so the two passes above never reached it (run `3003-93e9a9bf`,
    # 2026-09-02: the record existed, the prompt carried it 0 times). A
    # returned struct is not hopped: its API is a second-level expansion the
    # field hop deliberately does not make, and the crate's own `-> Error`
    # methods would pull the whole error type into every prompt.
    return_hop: list[TypeRecord] = []
    for rec in list(out):
        for m in rec.methods or ():
            ret = _return_type(m.sig or "")
            if not ret:
                continue
            for tok in identifier_tokens(ret):
                if tok in seen_names:
                    continue
                hop_rec = by_name.get(tok)
                if hop_rec is None or not _is_enum_kind(hop_rec.kind):
                    continue
                return_hop.append(hop_rec)
                seen_names.add(tok)
    out.extend(return_hop)
    if visible_from:
        out = [methods_nameable_from(r, visible_from) for r in out]
    return out
