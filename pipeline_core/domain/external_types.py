"""External (standard-library) type records — the pure rules.

A fill prompt renders TYPES_IN_SCOPE from the crate's own type records, so a
type the crate binds from `std`/`core`/`alloc` has no record behind its name.
Run `3003-93e9a9bf` (2026-09-02, cluster `stable`): the paired fill prompt for
the Debug formatter (87 995 chars) mentioned `BacktraceStatus` 0 times and
`status()` 0 times although the two convicting tests require printing a
`std::backtrace::Backtrace` only when `backtrace.status()` is
`BacktraceStatus::Captured`. The crate binds the name — `pub(crate) use
std::backtrace::Backtrace;` in `src/backtrace.rs` — so `Backtrace` appears in
the slice's fields and signatures with nothing to say what it is; the model
printed the backtrace unconditionally (a disabled backtrace's Debug is
`<disabled>`, measured on this toolchain) and reddened ten sibling tests,
twice. `std_api_sigs` answers one hand-written nightly family and stays the
last fallback; this module is the generic mechanism: resolve what the crate's
`use` lines bind, find the definition in the toolchain's `rust-src`, and turn
the extractor's records for that file into records a prompt can render.

Pure: stdlib + domain only. The I/O half — sysroot lookup, file copies, the
extractor run, the cache — is `adapters/std_types.py`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from typing import NamedTuple

from pipeline_core.domain.cfg import cfg_inline_predicates
from pipeline_core.domain.models import FieldRecord, MethodRecord, TypeRecord, UseRecord
from pipeline_core.domain.rust_idents import identifier_tokens

# Crates whose source ships with the `rust-src` component under
# `<sysroot>/lib/rustlib/src/rust/library/<crate>/src/`.
EXTERNAL_ROOTS: tuple[str, ...] = ("std", "core", "alloc")

# The record `kind` of an external record is the item's kind behind this
# prefix ("external struct"). Every consumer that treats type records as the
# crate's own items switches on the four crate kinds — the private-surface
# classifier (`_TYPE_KINDS`), the pub-items manifest, the producible-type set,
# the layout locate-keys — and so ignores these records without knowing the
# field; name-based selection for prompts (`types_referenced_in_text`) and the
# renderer are kind-agnostic and see them.
EXTERNAL_KIND_PREFIX = "external "

# Relative to the sysroot: where rust-src puts the library crates' sources.
SYSROOT_LIBRARY_REL = "lib/rustlib/src/rust/library"

# std's own name for the alloc crate (`extern crate alloc as alloc_crate;`):
# `std::sync::Arc` is `pub use alloc_crate::sync::{Arc, Weak};`.
_CRATE_RENAMES: dict[str, str] = {"alloc_crate": "alloc"}

# Bound on how many top-level re-exports are followed for one path:
# `std::error::Error` → `core::error::Error` is one hop, `core::ptr::NonNull`
# → `non_null::NonNull` one more; nothing in the library needs three.
MAX_REEXPORT_DEPTH = 3


def external_kind(kind: str) -> str:
    """`kind` marked external ("struct" → "external struct"); idempotent."""
    return kind if kind.startswith(EXTERNAL_KIND_PREFIX) else f"{EXTERNAL_KIND_PREFIX}{kind}"


def is_external_record(rec: TypeRecord) -> bool:
    return bool(rec.external_path)


# --- what the crate binds -----------------------------------------------------

class ExternalBindings(NamedTuple):
    """What a crate's top-level `use` lines bind from the external roots.

    `aliases`: type-like names (uppercase-initial last segment, or a rename)
    → fully qualified path (`Backtrace` → `std::backtrace::Backtrace`,
    `StdError` → `std::error::Error`). `modules`: module bindings → path
    (`use core::fmt;` and `use core::fmt::{self, …}` both give `fmt` →
    `core::fmt`), so `fmt::Formatter` qualifies to `core::fmt::Formatter`.
    `binding_files`: name → every file binding it, in file order. Records
    are read in file order (sorted, stable) so the first binding of a name
    is the same whatever order the extractor walked the tree in; a glob is
    never a binding."""

    aliases: dict[str, str]
    modules: dict[str, str]
    binding_files: dict[str, tuple[str, ...]]


def resolve_external_paths(
    uses: Sequence[UseRecord], roots: Iterable[str] = EXTERNAL_ROOTS,
) -> ExternalBindings:
    """See `ExternalBindings`. Only non-glob `use` records whose first segment
    is one of `roots` count; edition-2018 paths starting anywhere else name
    this crate (`crate::`/`self::`/`super::`) or another dependency."""
    root_set = frozenset(roots)
    aliases: dict[str, str] = {}
    modules: dict[str, str] = {}
    files: dict[str, list[str]] = {}
    for u in sorted(uses, key=lambda u: u.file):
        if u.is_glob or len(u.path) < 2 or u.path[0] not in root_set or not u.alias:
            continue
        if u.alias == "self":
            # `use core::fmt::{self, Debug}` binds the MODULE under its own name.
            name, target = u.path[-2], "::".join(u.path[:-1])
            modules.setdefault(name, target)
        else:
            name, target = u.alias, "::".join(u.path)
            if u.path[-1][:1].isupper():
                aliases.setdefault(name, target)
            else:
                modules.setdefault(name, target)
        bound = files.setdefault(name, [])
        if u.file not in bound:
            bound.append(u.file)
    return ExternalBindings(aliases, modules, {k: tuple(v) for k, v in files.items()})


def qualify(
    name_or_path: str, aliases: Mapping[str, str], modules: Mapping[str, str],
    roots: Iterable[str] = EXTERNAL_ROOTS,
) -> str | None:
    """The fully qualified external path `name_or_path` denotes under the
    crate's bindings, or None when it denotes nothing external: a bare alias
    (`Backtrace`), a module-qualified name (`fmt::Formatter`), or a path
    already rooted at one of `roots` (`std::backtrace::Backtrace`)."""
    parts = [p for p in name_or_path.strip().split("::") if p]
    if not parts:
        return None
    if len(parts) == 1:
        return aliases.get(parts[0])
    if parts[0] in frozenset(roots):
        return "::".join(parts)
    head = modules.get(parts[0])
    if head is None:
        return None
    return "::".join([head, *parts[1:]])


def sysroot_file_candidates(path: str) -> tuple[str, ...]:
    """Relative candidates under `<sysroot>/lib/rustlib/src/rust/library/` for
    the file that may define `path`, deepest module first, then each parent
    module as a fallback (re-exports are the norm: `std::sync::Arc` is
    written in `std/src/sync/mod.rs` as a `pub use`). Pure string rule, no
    I/O: `std::backtrace::Backtrace` → (`std/src/backtrace.rs`,
    `std/src/backtrace/mod.rs`); `std::collections::hash_map::HashMap` → the
    `hash_map` pair, then `std/src/collections.rs`,
    `std/src/collections/mod.rs`. The crate root file is never a candidate."""
    parts = [p for p in path.split("::") if p]
    if len(parts) < 2:
        return ()
    crate, mods = parts[0], parts[1:-1]
    out: list[str] = []
    for k in range(len(mods), 0, -1):
        stem = f"{crate}/src/" + "/".join(mods[:k])
        out.append(f"{stem}.rs")
        out.append(f"{stem}/mod.rs")
    return tuple(out)


def module_of_library_file(rel_file: str) -> str:
    """The module path a library source file defines: `std/src/backtrace.rs`
    → `std::backtrace`, `std/src/collections/mod.rs` → `std::collections`,
    `core/src/lib.rs` → `core`. Inverse of `sysroot_file_candidates`."""
    parts = [p for p in rel_file.split("/") if p]
    if len(parts) < 3 or parts[1] != "src":
        return ""
    crate, rest = parts[0], parts[2:]
    if rest and rest[-1].endswith(".rs"):
        rest[-1] = rest[-1][:-3]
    if rest and rest[-1] in ("mod", "lib"):
        rest.pop()
    return "::".join([crate, *rest])


def resolve_use_path(
    segments: Sequence[str], *, crate: str, module: str,
    roots: Iterable[str] = EXTERNAL_ROOTS,
) -> str | None:
    """A `use` path as written inside library file of module `module` (of
    crate `crate`), as a fully qualified path: `crate::x` → `<crate>::x`,
    `self::x` → `<module>::x`, `super::x` → parent, a bare relative path
    (`non_null::NonNull`, edition-2018 child module) → `<module>::…`, a root
    or renamed root as is. None for an empty or `self`-terminated path."""
    segs = [s for s in segments if s]
    if not segs or segs[-1] == "self":
        return None
    head = segs[0]
    module_parts = [p for p in module.split("::") if p]
    if head == "crate":
        return "::".join([crate, *segs[1:]])
    if head == "self":
        return "::".join([*module_parts, *segs[1:]])
    if head == "super":
        depth = 0
        while depth < len(segs) and segs[depth] == "super":
            depth += 1
        if depth > len(module_parts) - 1:
            return None
        return "::".join([*module_parts[:len(module_parts) - depth], *segs[depth:]])
    if head in _CRATE_RENAMES:
        return "::".join([_CRATE_RENAMES[head], *segs[1:]])
    if head in frozenset(roots):
        return "::".join(segs)
    return "::".join([*module_parts, *segs])


# --- text cleaning -------------------------------------------------------------

def split_attributes(text: str) -> tuple[tuple[str, ...], str]:
    """Every `#[…]` / `# […]` / `#![…]` attribute group in `text`, as its
    inner text with whitespace collapsed, plus the text with the groups
    removed. Bracket depth is tracked and string literals are skipped, so a
    `#[doc = "see [link]"]` group ends at its own `]`, not the one inside the
    string. The extractor's variant strings carry these groups verbatim:
    `# [doc = " …"] # [stable (feature = "backtrace" , since = "1.65.0")]
    Unsupported`."""
    attrs: list[str] = []
    rest: list[str] = []
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch == "#":
            j = i + 1
            while j < n and text[j].isspace():
                j += 1
            if j < n and text[j] == "!":
                j += 1
                while j < n and text[j].isspace():
                    j += 1
            if j < n and text[j] == "[":
                end = _matching_bracket(text, j)
                if end > j:
                    attrs.append(" ".join(text[j + 1:end].split()))
                    i = end + 1
                    continue
        rest.append(ch)
        i += 1
    return tuple(attrs), "".join(rest)


def _matching_bracket(text: str, open_at: int) -> int:
    """Index of the `]` closing the `[` at `open_at`, skipping `"…"` literals
    (backslash escapes honoured); -1 when unbalanced."""
    depth = 0
    i = open_at
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def strip_attributes(text: str) -> str:
    """`text` without its attribute groups, whitespace collapsed."""
    return " ".join(split_attributes(text)[1].split())


# Token ENDINGS that glue to the next token and token STARTS that glue to the
# previous one: the extractor emits `(& self)` as the two tokens `(&` and
# `self)`, so the rule reads the boundary characters, not whole tokens.
_GLUE_AFTER = ("(", "[", "<", "&", ".", "*", "!", "?", "#", "::")
_GLUE_BEFORE = (",", ";", ")", "]", ">", ":", ".")
# Binary operators keep a space on both sides (`-> &'a T`, `A = Global`).
_SPACED_OPERATORS = frozenset({"->", "=>", "=", "+", "|", "..", "..."})
# Tokens that are keywords, not names: a `(` or `<` after one of these keeps
# its space (`impl (FnOnce() -> T) + Send`, `for<'a>` is handled by `<`).
_KEYWORDS = frozenset({
    "fn", "impl", "dyn", "where", "for", "in", "mut", "const", "unsafe", "async",
    "extern", "pub", "return", "as", "ref", "move", "if", "else", "match", "let",
    "use", "type", "struct", "enum", "trait", "static", "crate", "super",
})


def compact_rust_spacing(text: str) -> str:
    """Token-spaced extractor text in source spacing: `fn status (& self) ->
    BacktraceStatus` → `fn status(&self) -> BacktraceStatus`, `Option < &
    (dyn Error + 'static) >` → `Option<&(dyn Error + 'static)>`, `T : ? Sized`
    → `T: ?Sized`. Splits on whitespace only, so already-compact text and
    every string-free signature round-trip; attributes are removed first by
    the callers (their string literals would be re-spaced)."""
    toks = text.split()
    out: list[str] = []
    for i, tok in enumerate(toks):
        if i and not _no_space_between(toks[i - 1], tok):
            out.append(" ")
        out.append(tok)
    return "".join(out)


def _ident_end(tok: str) -> bool:
    return bool(tok) and (tok[-1].isalnum() or tok[-1] == "_")


def _no_space_between(a: str, b: str) -> bool:
    if a in _SPACED_OPERATORS or b in _SPACED_OPERATORS:
        return False
    if a.endswith(_GLUE_AFTER) or b.startswith(_GLUE_BEFORE):
        return True
    if b.startswith("<"):
        return _ident_end(a) or a.endswith((")", ">", "]"))
    if b.startswith("("):
        if a in _KEYWORDS:
            return False
        return _ident_end(a) or a.endswith((")", ">", "]"))
    return False


def _tidy_generics(text: str) -> str:
    """Drop the trailing comma the pretty-printer leaves in a wrapped
    generics list (`<T: ?Sized, A: Allocator = Global,>`)."""
    return text.replace(",>", ">").replace(", >", ">")


def clean_generics(text: str) -> str:
    return _tidy_generics(compact_rust_spacing(strip_attributes(text)))


def clean_variant(text: str) -> str:
    """One enum variant as source text: attribute groups dropped (`doc`,
    `stable`, `cfg`), token spacing compacted."""
    return compact_rust_spacing(strip_attributes(text))


def variant_is_test_only(text: str) -> bool:
    """A variant gated `#[cfg(test)]` exists only in the library's own test
    build (`RawFrame::Fake` in `std/src/backtrace.rs`); the extractor's
    `--cfg-false test` filters items and methods, not variants."""
    return any("".join(a.split()) == "cfg(test)" for a in split_attributes(text)[0])


def leading_doc_texts(definition: str) -> list[str]:
    """Texts of the leading `///` block of an item definition (one rustdoc
    space removed), the same reading the prompt renderer takes: blank lines,
    `//!` and `//` are skipped without ending the block; the first attribute
    or code line ends it."""
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


def leading_attribute_text(definition: str) -> str:
    """The outer-attribute block between an item's doc comments and its first
    code line, joined on bracket depth (a `#[derive(\\n Clone,\\n)]` the printer
    wraps is one attribute)."""
    out: list[str] = []
    depth = 0
    for raw in definition.splitlines():
        line = raw.strip()
        if depth > 0:
            out.append(line)
            depth += line.count("[") - line.count("]")
            continue
        if not line or line.startswith("//"):
            continue
        if not line.startswith("#"):
            break
        out.append(line)
        depth = line.count("[") - line.count("]")
    return " ".join(out)


def leading_cfg_predicates(definition: str) -> tuple[str, ...]:
    """Predicates of the `#[cfg(…)]` attributes gating an item's own
    definition — the arm the crate compiles under, `not(feature = "std")` on
    the no-std `enum Backtrace {}` shim."""
    return tuple(cfg_inline_predicates(leading_attribute_text(definition)))


def cfg_complement(predicates: Sequence[str]) -> str | None:
    """The predicate under which an item gated by ALL of `predicates` is
    absent. Two items of one name at one module's file scope compile only as
    cfg-exclusive arms, so a `use std::backtrace::Backtrace;` beside a
    `#[cfg(not(feature = "std"))] enum Backtrace {}` is live exactly under
    `not(not(feature = "std"))` — which the cluster cfg evaluator decides
    (True in a default-features build) so the record-level cfg filter drops
    the arm the build does not compile and keeps the other."""
    preds = [p.strip() for p in predicates if p.strip()]
    if not preds:
        return None
    if len(preds) == 1:
        return f"not({preds[0]})"
    return f"not(all({', '.join(preds)}))"


# --- source scans ---------------------------------------------------------------

def trait_impls_for(source_text: str, type_name: str) -> tuple[str, ...]:
    """Traits `source_text` implements for `type_name`, in source order, from
    `impl<…>? [path::]Trait<…>? for Name<…>?` headers — `impl fmt::Display for
    Backtrace {`, `impl<'a> Iterator for Frames<'a> {`, a header whose
    generics the formatter wrapped over lines. Negative impls keep their bang
    (`!Send`); inherent impls (no `for`) and blanket impls on a parameter are
    not impls of this type. The extractor's `methods` are inherent methods
    only, and whether a std type prints through `Display` or `Debug` is the
    fact a formatter fill needs."""
    out: list[str] = []
    lines = source_text.splitlines()
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        i += 1
        if not (s.startswith("impl") or s.startswith("unsafe impl")):
            continue
        header = s
        j = i
        while "{" not in header and " for " not in header and j < len(lines) and j < i + 6:
            header = f"{header} {lines[j].strip()}"
            j += 1
        parsed = parse_impl_header(header)
        if parsed is None:
            continue
        trait, ty = parsed
        if ty == type_name and trait not in out:
            out.append(trait)
    return tuple(out)


def parse_impl_header(header: str) -> tuple[str, str] | None:
    """(trait name, self type name) of a trait-impl header, None for an
    inherent impl or text that is not an impl header."""
    h = header.strip()
    if h.startswith("unsafe "):
        h = h[len("unsafe "):].lstrip()
    if not h.startswith("impl"):
        return None
    h = h[len("impl"):]
    if h.startswith("<"):
        end = _matching_angle(h, 0)
        if end < 0:
            return None
        h = h[end + 1:]
    h = h.strip()
    if h.startswith("const "):
        h = h[len("const "):].lstrip()
    negative = h.startswith("!")
    if negative:
        h = h[1:].lstrip()
    sep = h.find(" for ")
    if sep < 0:
        return None
    trait_text, ty_text = h[:sep].strip(), h[sep + 5:].strip()
    trait = _path_last_segment(trait_text)
    if not trait:
        return None
    ty = _self_type_name(ty_text)
    if not ty:
        return None
    return (f"!{trait}" if negative else trait), ty


def _matching_angle(text: str, open_at: int) -> int:
    depth = 0
    i = open_at
    while i < len(text):
        ch = text[i]
        if ch == "<":
            depth += 1
        elif ch == ">" and not (i and text[i - 1] == "-"):
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _path_last_segment(text: str) -> str:
    cut = text.find("<")
    head = text if cut < 0 else text[:cut]
    seg = head.strip().rsplit("::", 1)[-1].strip()
    return seg if seg and (seg[0].isalpha() or seg[0] == "_") and all(
        c.isalnum() or c == "_" for c in seg) else ""


def _self_type_name(text: str) -> str:
    """`Backtrace` from `Backtrace {`, `Frames<'a> where …`, `&'a Foo<T> {`.
    "" for a path-qualified self type: the file that defines a type names
    it bare in its own impls, so `impl Error for crate::fmt::Error {}` in
    `core/src/error.rs` is an impl for another module's `Error`, not for
    the trait `Error` declared beside it."""
    t = text
    for stop in (" where ", "{"):
        k = t.find(stop)
        if k >= 0:
            t = t[:k]
    t = t.strip()
    while t and t[0] in "&*":
        t = t[1:].lstrip()
        for kw in ("'static ", "mut ", "const "):
            if t.startswith(kw):
                t = t[len(kw):].lstrip()
        if t.startswith("'"):
            t = t.split(None, 1)[1] if " " in t else ""
    if t.startswith("dyn "):
        t = t[4:].lstrip()
    head = t.split("<", 1)[0]
    if "::" in head:
        return ""
    return _path_last_segment(t)


_ITEM_KEYWORDS = frozenset({"fn", "struct", "enum", "trait", "type", "const", "mod", "union"})


def unstable_items(source_text: str) -> dict[str, str]:
    """Items whose attribute block carries `#[unstable(…)]`, name → the
    feature gate (`frames` → `backtrace_frames`, "" when the attribute names
    none). Such an item is usable from a consumer crate only under
    `#![feature(<gate>)]` on nightly; a record has to say so rather than
    advertise the call as if any build compiled it. Name-level: one file's
    same-named fns across impl blocks share a verdict."""
    out: dict[str, str] = {}
    pending: str | None = None
    attr: list[str] = []
    depth = 0
    for raw in source_text.splitlines():
        s = raw.strip()
        if depth > 0:
            # Continuation of a wrapped attribute: `#[unstable(\n feature = …`.
            attr.append(s)
            depth += s.count("[") - s.count("]")
            if depth <= 0:
                pending = _pending_gate(" ".join(attr), pending)
            continue
        if not s or s.startswith("//"):
            continue
        if s.startswith("#"):
            attr = [s]
            depth = s.count("[") - s.count("]")
            if depth <= 0:
                pending = _pending_gate(s, pending)
            continue
        name = _declared_item_name(s)
        if name and pending is not None:
            out[name] = pending
        pending = None
    return out


def _pending_gate(attr_text: str, pending: str | None) -> str | None:
    inner = attr_text.lstrip("#").lstrip("!").lstrip().lstrip("[")
    return _feature_gate(inner) if inner.startswith("unstable") else pending


def _feature_gate(attr_text: str) -> str:
    k = attr_text.find("feature")
    if k < 0:
        return ""
    q = attr_text.find('"', k)
    if q < 0:
        return ""
    e = attr_text.find('"', q + 1)
    return attr_text[q + 1:e] if e > q else ""


def unstable_item_names(source_text: str) -> frozenset[str]:
    return frozenset(unstable_items(source_text))


def code_only(source_text: str) -> str:
    """`source_text` without its comment lines (`///`, `//!`, `//`): a name
    an example in a doc comment writes (`use std::fs::File;`) is not a name
    the crate's code uses, and on the live `error.rs` those examples pulled
    thirteen `std::fs` / `std::path` records into the tree (2026-09-02)."""
    return "\n".join(
        line for line in source_text.splitlines() if not line.lstrip().startswith("//"))


def _declared_item_name(line: str) -> str:
    toks = line.replace("(", " (").replace("<", " <").replace("{", " {").replace(";", " ;").replace(":", " :").split()
    for i, tok in enumerate(toks):
        if tok in _ITEM_KEYWORDS and i + 1 < len(toks):
            nxt = toks[i + 1]
            return nxt if nxt[:1].isalpha() or nxt[:1] == "_" else ""
    return ""


def external_names_in(
    text: str, aliases: Mapping[str, str], modules: Mapping[str, str],
    crate_names: Iterable[str], roots: Iterable[str] = EXTERNAL_ROOTS,
) -> list[str]:
    """Names written in `text` that resolve to an external path and are not
    a name the crate declares itself: bare aliases (`Backtrace`) first, then
    qualified paths (`fmt::Formatter`, `std::backtrace::Backtrace`) as
    written, each once, in order of first appearance. A qualified path is
    read back to its first segment, so `crate::fmt::Formatter` never
    qualifies through a `fmt` module alias."""
    shadow = frozenset(crate_names)
    out: list[str] = []
    seen: set[str] = set()
    for tok in identifier_tokens(text):
        if tok in seen or tok in shadow or not tok[:1].isupper() or tok not in aliases:
            continue
        seen.add(tok)
        out.append(tok)
    for written in _qualified_paths(text):
        name = written.rsplit("::", 1)[-1]
        if written in seen or name in shadow:
            continue
        if qualify(written, aliases, modules, roots) is None:
            continue
        seen.add(written)
        out.append(written)
    return out


def _is_ident_char(c: str) -> bool:
    return c.isalnum() or c == "_"


def _qualified_paths(text: str) -> list[str]:
    """Every `a::b::Name` path in `text` whose last segment is uppercase-
    initial, as written, in order (each `::` chain reported once)."""
    out: list[str] = []
    n = len(text)
    i = 0
    while (k := text.find("::", i)) >= 0:
        # Walk back over the whole `seg::seg::` chain to its first segment.
        start = k
        while True:
            s = start
            while s > 0 and _is_ident_char(text[s - 1]):
                s -= 1
            if s == start:
                break
            start = s
            if start >= 2 and text[start - 2:start] == "::":
                start -= 2
                continue
            break
        # Walk forward over the chain to its last segment.
        end = k
        while True:
            e = end + 2
            while e < n and _is_ident_char(text[e]):
                e += 1
            if e == end + 2:
                break
            end = e
            if text[end:end + 2] == "::" and end + 2 < n and _is_ident_char(text[end + 2]):
                continue
            break
        i = max(end, k + 2)
        if start >= k or end <= k:
            continue
        segs = text[start:end].split("::")
        if not all(segs) or not (segs[0][:1].isalpha() or segs[0][:1] == "_"):
            continue
        # The type is the first uppercase-initial segment: what follows it is
        # an associated item or a variant (`Backtrace::capture`, `Result::Ok`).
        cut = next((k for k, seg in enumerate(segs) if seg[:1].isupper()), -1)
        if cut < 1:
            continue
        chain = "::".join(segs[:cut + 1])
        if chain not in out:
            out.append(chain)
    return out


# --- record building ----------------------------------------------------------

def _keep_attribute(inner: str) -> bool:
    head = inner.split("(", 1)[0].strip()
    return head in ("derive", "repr", "non_exhaustive")


def _definition_head(definition: str) -> str:
    """The item's first code statement — doc and attribute lines skipped,
    lines joined until the first `{` or `;` — attributes stripped and
    spacing compacted, cut before `{`: `pub struct Backtrace`, `pub trait
    Error: Debug + Display`, `pub type LazyResolve = …;`."""
    code: list[str] = []
    depth = 0
    started = False
    for raw in definition.splitlines():
        line = raw.strip()
        if not started:
            if depth > 0:
                depth += line.count("[") - line.count("]")
                continue
            if not line or line.startswith("//"):
                continue
            if line.startswith("#"):
                depth = line.count("[") - line.count("]")
                continue
            started = True
        code.append(line)
        if "{" in line or line.endswith(";"):
            break
        if len(code) >= 12:
            break
    text = _tidy_generics(compact_rust_spacing(strip_attributes(" ".join(code))))
    cut = text.find("{")
    return (text[:cut] if cut >= 0 else text).strip()


def _method_return_and_params(sig: str) -> str:
    return sig.split(" where ", 1)[0]


def hop_names_for(
    rec: TypeRecord, siblings: Mapping[str, TypeRecord],
) -> list[str]:
    """Sibling records (same source file, `pub`) that `rec`'s public method
    signatures or public fields name, in first-seen order — the one hop the
    prompt needs beyond the type itself: `Backtrace::status() ->
    BacktraceStatus` is worthless without `BacktraceStatus`'s variants, and
    `Error::provide(&mut Request<'a>)` without `Request`'s methods."""
    out: list[str] = []
    texts = [_method_return_and_params(m.sig) for m in rec.methods]
    texts += [f.ty for f in rec.fields if f.is_pub]
    for text in texts:
        for tok in identifier_tokens(text):
            if tok != rec.name and tok in siblings and tok not in out:
                out.append(tok)
    return out


def canonical_external_record(
    raw: TypeRecord, *, external_path: str, source_text: str,
    unstable: Mapping[str, str] | None = None,
) -> TypeRecord:
    """The extractor's record for a library file, as the record a consumer
    crate sees: private fields and private methods dropped (invisible from
    outside the crate — and a `construct: Backtrace { inner: <Inner> }` hint
    derived from them would be wrong), variants and signatures cleaned, the
    definition rebuilt as the item's docs, its layout attributes and a
    one-line head, with the trait impls found in the source as a trailing
    comment. `file`/`mod_path` empty on the record and its methods; the
    binding file is stamped by `annotate_external_record`.

    `unstable` (`unstable_items` of the source) marks rather than drops: an
    unstable method keeps its signature, carries its feature gate as a
    `cfg_predicates` entry — so a crate whose cfg vocabulary names the gate
    (`error_generic_member_access` is a cfg atom of the live crate's
    clusters) gets it filtered per cluster by the existing method-level cfg
    filter — and is listed on a `nightly-only` doc line for the clusters
    that cannot decide it. Dropping them would starve the nightly cluster's
    `provide`/`Request` fills of the one API they exist for."""
    unstable = dict(unstable or {})
    kind = external_kind(raw.kind)
    is_trait = raw.kind == "trait"
    fields = tuple(
        FieldRecord(name=f.name, ty=compact_rust_spacing(strip_attributes(f.ty)), is_pub=True)
        for f in raw.fields if f.is_pub)
    dropped_private_fields = len(fields) < len(raw.fields)
    variants = tuple(
        v for v in (clean_variant(x) for x in raw.variants if not variant_is_test_only(x)) if v)
    methods: list[MethodRecord] = []
    seen: set[str] = set()
    nightly: dict[str, list[str]] = {}
    for m in raw.methods:
        if not m.name or m.name in seen:
            continue
        if not is_trait and not m.is_pub:
            continue
        seen.add(m.name)
        cfg = m.cfg_predicates
        if m.name in unstable:
            gate = unstable[m.name]
            nightly.setdefault(gate, []).append(m.name)
            if gate and gate not in cfg:
                cfg = (*cfg, gate)
        methods.append(MethodRecord(
            name=m.name, sig=compact_rust_spacing(strip_attributes(m.sig)),
            is_pub=is_trait or m.is_pub, is_exactly_pub=is_trait or m.is_exactly_pub,
            cfg_predicates=cfg, file="", mod_path=""))
    impls = trait_impls_for(source_text, raw.name)
    generics = clean_generics(raw.generics)
    attrs = [a for a in split_attributes(leading_attribute_text(raw.definition))[0]
             if _keep_attribute(a)]
    head = _definition_head(raw.definition)
    if raw.kind == "struct":
        if fields:
            body = ", ".join(f"pub {f.name}: {f.ty}" for f in fields)
            head = f"{head} {{ {body} }}"
        elif dropped_private_fields or "{" in raw.definition:
            head = f"{head} {{ /* private fields */ }}"
    elif raw.kind == "enum":
        head = f"{head} {{ {', '.join(variants)} }}" if variants else f"{head} {{}}"
    lines = [f"/// {t}".rstrip() for t in leading_doc_texts(raw.definition)]
    lines = [external_doc_line(external_path, "standard library", impls,
                               unstable_gate=unstable.get(raw.name)),
             *nightly_doc_lines(nightly), *lines]
    lines += [f"#[{a}]" for a in attrs]
    lines.append(head)
    if impls:
        lines.append(f"// trait impls: {', '.join(impls)}")
    return TypeRecord(
        file="", kind=kind, name=raw.name, generics=generics,
        definition="\n".join(lines), fields=fields, variants=variants,
        methods=tuple(methods), is_pub=True, is_exactly_pub=True, mod_path="",
        external_path=external_path, external_trait_impls=impls,
    )


UNSTABLE_MARK = "[unstable: nightly-only"
NIGHTLY_MARK = "/// nightly-only methods:"


def external_doc_line(
    external_path: str, note: str, impls: Sequence[str], *,
    unstable_gate: str | None = None,
) -> str:
    """The first doc line of an external record — the only channel the
    renderer keeps for what the type IS: `/// external type
    std::backtrace::Backtrace (standard library; re-exported by this crate's
    use lines); trait impls: Display, Debug`. `unstable_gate` (a feature
    name, or "" for an unstable item naming none) marks a nightly-only type."""
    text = f"/// external type {external_path} ({note})"
    if unstable_gate is not None:
        gate = f" behind #![feature({unstable_gate})]" if unstable_gate else ""
        text += f" {UNSTABLE_MARK}{gate}]"
    if impls:
        text += f"; trait impls: {', '.join(impls)}"
    return text


def nightly_doc_lines(nightly: Mapping[str, Sequence[str]]) -> list[str]:
    """One doc line per feature gate naming the methods behind it:
    `/// nightly-only methods: frames() need #![feature(backtrace_frames)]`."""
    out: list[str] = []
    for gate, names in nightly.items():
        shown = list(names[:4])
        calls = ", ".join(f"{n}()" for n in shown)
        if len(names) > len(shown):
            calls += f", … ({len(names) - len(shown)} more)"
        need = f" need #![feature({gate})]" if gate else " need a nightly feature gate"
        out.append(f"{NIGHTLY_MARK} {calls}{need}")
    return out


def hop_doc_line(rec: TypeRecord, hop: TypeRecord) -> str | None:
    """One doc line on `rec` summarising an enum it hands out through a
    method — `/// status() -> BacktraceStatus { Unsupported | Disabled |
    Captured } (#[non_exhaustive]: match with a `_` arm)` — because the
    prompt's type selection matches names in the fn's text and one level of
    FIELD expansion, never a method's return type, so the hop record itself
    is rendered only when something names it. None for a non-enum hop."""
    if not hop.variants:
        return None
    method = next((m.name for m in rec.methods if hop.name in identifier_tokens(
        _method_return_and_params(m.sig).split("->", 1)[-1])), None)
    if method is None:
        return None
    variants = " | ".join(hop.variants)
    if len(variants) > 120:
        variants = variants[:117].rstrip() + "…"
    note = ""
    if any(a.strip() == "non_exhaustive"
           for a in split_attributes(leading_attribute_text(hop.definition))[0]):
        note = " (#[non_exhaustive]: match with a `_` arm)"
    return f"/// {method}() -> {hop.name} {{ {variants} }}{note}"


def annotate_external_record(
    rec: TypeRecord, *, name: str, binding_file: str, binding_note: str,
    hops: Sequence[TypeRecord] = (), cfg_complement: str | None = None,
) -> TypeRecord:
    """`rec` (a canonical external record) as the crate binds it: named by
    the crate's alias (`StdError` for `std::error::Error`), located in the
    binding file, its first doc line saying how the name reaches this crate,
    a doc line per enum hop, and — when the binding shares its file-scope
    name with a cfg-gated crate item — the complementary `#[cfg(…)]` so the
    cluster's record-level cfg filter keeps exactly one arm."""
    doc_lines: list[str] = []
    rest: list[str] = []
    in_docs = True
    for line in rec.definition.splitlines():
        if in_docs and line.startswith("///"):
            doc_lines.append(line)
            continue
        in_docs = False
        rest.append(line)
    head_line = doc_lines[0] if doc_lines else ""
    gate: str | None = None
    if UNSTABLE_MARK in head_line:
        tail = head_line.split(UNSTABLE_MARK, 1)[1]
        gate = tail[tail.find("(") + 1:tail.find(")")] if "#![feature(" in tail else ""
    first = external_doc_line(
        rec.external_path, f"standard library; {binding_note}", rec.external_trait_impls,
        unstable_gate=gate)
    hop_lines = [ln for ln in (hop_doc_line(rec, h) for h in hops) if ln]
    nightly_lines = [ln for ln in doc_lines[1:] if ln.startswith(NIGHTLY_MARK)]
    other_docs = [ln for ln in doc_lines[1:] if not ln.startswith(NIGHTLY_MARK)]
    body = [first, *hop_lines, *nightly_lines, *other_docs]
    if cfg_complement:
        body.append(f"#[cfg({cfg_complement})]")
    body.extend(rest)
    return replace(rec, name=name, file=binding_file, definition="\n".join(body))
