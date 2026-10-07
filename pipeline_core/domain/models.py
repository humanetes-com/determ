"""Domain records for the Python↔Rust extractor seam.

These frozen dataclasses mirror the ``serde::Serialize`` structs in
``tools/skeleton_extractor/src/lib.rs`` field-for-field (the JSON record contract). They
replace the untyped ``dict`` records that flowed out of the ``Extractor`` adapter, so
consumers get attribute access (``rec.fn_name``) that mypy can check instead of stringly
``rec["fn_name"]`` / ``rec.get("sig_text")`` lookups that fail only at runtime.

The seam is sacred: ``from_dict`` is tolerant (missing keys take the field default, extra
keys are ignored) and ``to_dict`` round-trips back to the exact wire shape, so nothing
about the byte-level JSON contract changes — only the in-process representation.

Pure domain: stdlib only, no I/O. Parsing happens in the adapter, which is the seam.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from pipeline_core.domain.pass_payoff import PassPayoff

from pipeline_core.domain.cfg import CfgAtom
from pipeline_core.domain.fill_class import FillClassHistogram
from pipeline_core.domain.import_repair import ImportFix


def _strs(v: Any) -> tuple[str, ...]:
    """Coerce a JSON list (or None) into a tuple of strings."""
    if not v:
        return ()
    return tuple(str(x) for x in v)


class FnLocator(NamedTuple):
    """DISPATCH identity: ``(file, fn_name)``. The frontier key, the batch
    cap, leaf-first ordering, the reverse-map value, the fix-loop repair
    target. Several impls in one file may share this pair (six ``from``,
    eight ``fmt``) — dispatch is allowed to be ambiguous; a fill route then
    expands the locator into the containers it will actually write.

    Conviction, reversion, exhaustion, error focusing and the refill stamp
    take a :class:`FnSite`. Keying those on a locator is the collapse that
    stamped every ``From`` coercion with a panic inside ``construct_from_boxed``
    (run ``463713-b7c666c8``, 2026-08-28/29).

    A ``NamedTuple`` (deliberately, not a frozen dataclass): it stays structurally
    a 2-tuple, so it is a *drop-in* for every existing ``set`` membership, ``dict``
    key, positional unpack (``for file, fn in …``) and tuple-equality that already
    treats the pair as a bare ``tuple[str, str]`` — while adding the ``.file`` /
    ``.fn_name`` vocabulary and a named constructor. Producers may be migrated
    one slice at a time; a not-yet-converted plain tuple still interoperates.

    Caveat: it does NOT structurally differ from the unrelated ``(rel_path,
    content)`` pair also spelled ``tuple[str, str]`` — the guard against confusing
    the two is reader/annotation-level, not runtime.
    """

    file: str
    fn_name: str


class FnSite(NamedTuple):
    """IDENTITY of a reconstructable function: ``(file, fn_name, container)``.
    Where :class:`FnLocator` is the dispatch key (may be ambiguous), ``FnSite``
    is the unit conviction / reversion / exhaustion / error-focusing / refill
    stamp read. The free ``fn not(cond)`` (``container=""``) vs the ``Bool``
    trait method ``fn not`` (``container="impl Bool for &bool"``); the six
    ``fn from`` across the ``From<…>`` impls; the eight ``fn fmt`` across
    Debug/Display.

    Used by the U2 blame partition: a test that panics in a still-stubbed fn is
    only the unit's OWN failure when the panic site's ``(file, fn_name,
    container)`` matches the unit — a name-only match wrongly charges a unit for
    a same-named SIBLING's stub. Container equality must be taken canonically
    (``container_key`` / ``canon_container``) because the extractor and the
    impl-manifest render the same header with different spacing
    (``impl Bool for & bool`` vs ``&bool``)."""

    file: str
    fn_name: str
    container: str


@dataclass(frozen=True, slots=True)
class SkeletoniseResult:
    """Outcome of one ``--skeletonise`` extractor invocation: whether it
    succeeded, the per-file JSON reports (empty on failure), and captured stderr
    (the failure reason when ``ok`` is False). Replaces the anonymous
    ``{ok, reports, stderr}`` dict the ``Extractor.skeletonise`` port returned."""

    ok: bool
    reports: list[dict[str, Any]]
    stderr: str


@dataclass(frozen=True, slots=True)
class SlotFit:
    """A crate fn whose declared signature can be stored in a fn-pointer
    field: `name` as a table literal would spell it (`object_drop<E>`,
    `Wrap::make`), where it is declared, and its cfg gate so a cluster that
    does not compile it can drop it."""
    name: str = ""
    file: str = ""
    cfg_predicates: tuple[str, ...] = ()
    # The bound a generic parameter puts on the type the slot binds it to
    # (`u8: Display`), which the signature comparison cannot decide. "" when
    # the fit is unconditional.
    requires: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SlotFit:
        return cls(name=d.get("name", ""), file=d.get("file", ""),
                   cfg_predicates=_strs(d.get("cfg_predicates")),
                   requires=d.get("requires", ""))

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "file": self.file,
                "cfg_predicates": list(self.cfg_predicates),
                "requires": self.requires}


@dataclass(frozen=True, slots=True)
class FieldRecord:
    """A struct field (model.rs FieldRecord)."""
    name: str = ""
    ty: str = ""
    is_pub: bool = False
    # Effective cfg predicates — the enclosing struct's gate plus the field's
    # own, as `MethodRecord.cfg_predicates` carries an impl block's plus the
    # method's. A cluster-scoped consumer drops a field whose gate the cluster
    # decides False; empty (a pre-field artefact, a hand-made record) reads as
    # ungated, so nothing is dropped on absence of evidence.
    cfg_predicates: tuple[str, ...] = ()
    # For a fn-POINTER field, every crate fn whose signature fits it
    # (`installable_fns.slot_fits`); () when none does. None: not a fn
    # pointer, or the records were read without the crate's fns — a list
    # printed as complete must be complete, so absence is never rendered as
    # "nothing fits".
    fits: tuple[SlotFit, ...] | None = None
    # The cfg worlds that compile this field, when that is not every world of
    # the run (`cluster_membership.cluster_filter_type_members` sets it; the
    # extractor never does). Empty: every world, or no world plan in hand.
    worlds: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FieldRecord:
        raw = d.get("fits")
        return cls(name=d.get("name", ""), ty=d.get("ty", ""),
                   is_pub=bool(d.get("is_pub", False)),
                   cfg_predicates=_strs(d.get("cfg_predicates")),
                   fits=(None if raw is None
                         else tuple(SlotFit.from_dict(f) for f in raw)))

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name, "ty": self.ty, "is_pub": self.is_pub,
            "cfg_predicates": list(self.cfg_predicates)}
        if self.fits is not None:
            out["fits"] = [f.to_dict() for f in self.fits]
        return out


@dataclass(frozen=True, slots=True)
class MethodRecord:
    """A method declared on a type (model.rs MethodRecord)."""
    name: str = ""
    sig: str = ""
    is_pub: bool = False
    # Strictly `Visibility::Public` — see TypeRecord.is_exactly_pub for why
    # this is a separate field, not a redefinition of `is_pub`.
    is_exactly_pub: bool = True
    cfg_predicates: tuple[str, ...] = ()
    # Declaring file and the `mod` path that contains the method. A nonempty
    # `mod_path` in another file is a private-mod wall: the method is not
    # nameable from the fill's file, however `is_pub` the enclosing trait is.
    # Trait methods are recorded `is_pub=false` (syn has no vis on trait
    # items), so filtering on `is_pub` alone cannot drop them.
    file: str = ""
    mod_path: str = ""
    # As `FieldRecord.worlds`: the cfg worlds that compile this method when
    # that is not every world of the run.
    worlds: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MethodRecord:
        return cls(name=d.get("name", ""), sig=d.get("sig", ""),
                   is_pub=bool(d.get("is_pub", False)),
                   is_exactly_pub=bool(d.get("is_exactly_pub", True)),
                   cfg_predicates=_strs(d.get("cfg_predicates")),
                   file=d.get("file", ""),
                   mod_path=d.get("mod_path", ""))

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "sig": self.sig, "is_pub": self.is_pub,
                "is_exactly_pub": self.is_exactly_pub,
                "cfg_predicates": list(self.cfg_predicates),
                "file": self.file, "mod_path": self.mod_path}


@dataclass(frozen=True, slots=True)
class TraitImplRecord:
    """One `impl Trait for <type>` header the crate declares (model.rs
    TraitImplRecord). Header text only — the line the skeleton keeps after
    strip — never a body."""
    header: str = ""         # `impl<C> StdError for ContextError<C, Error> where C: Display`
    trait_path: str = ""     # `StdError`, `ext::StdError`, `!Send`, `From<E>`
    self_ty: str = ""        # as written, generic arguments included
    generics: str = ""
    where_clause: str = ""
    file: str = ""
    mod_path: str = ""
    cfg_predicates: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TraitImplRecord:
        return cls(header=d.get("header", ""), trait_path=d.get("trait_path", ""),
                   self_ty=d.get("self_ty", ""), generics=d.get("generics", ""),
                   where_clause=d.get("where_clause", ""), file=d.get("file", ""),
                   mod_path=d.get("mod_path", ""),
                   cfg_predicates=_strs(d.get("cfg_predicates")))

    def to_dict(self) -> dict[str, Any]:
        return {"header": self.header, "trait_path": self.trait_path,
                "self_ty": self.self_ty, "generics": self.generics,
                "where_clause": self.where_clause, "file": self.file,
                "mod_path": self.mod_path,
                "cfg_predicates": list(self.cfg_predicates)}


@dataclass(frozen=True, slots=True)
class TypeRecord:
    """A struct / enum / type-alias / trait definition (model.rs TypeRecord)."""
    file: str = ""
    kind: str = ""           # "struct" | "enum" | "type" | "trait"
    name: str = ""
    generics: str = ""       # "<T, E>" or ""
    definition: str = ""
    fields: tuple[FieldRecord, ...] = ()
    variants: tuple[str, ...] = ()
    methods: tuple[MethodRecord, ...] = ()
    is_pub: bool = False
    # Strictly `Visibility::Public` — false for `pub(crate)`/`pub(super)`/
    # `pub(in path)`, which `is_pub` folds into True (deliberate, for
    # cross-module skeletonising callers). Defaults to True so existing
    # fixtures/tests that only set `is_pub` keep their prior meaning; the
    # extractor always emits both fields explicitly.
    is_exactly_pub: bool = True
    # Nested `mod` path from the file root (`"ext"` for `mod ext { pub trait
    # StdError }`). Empty is file scope. A nonempty path is a private-mod
    # wall: Rust will not resolve the name from another file, however
    # `is_pub` the type itself is. `is_pub` ORs across declarations, so a
    # `pub trait` behind `mod ext` still reads `is_pub=True` — filtering on
    # `is_pub` alone cannot drop it from another file's TYPES_IN_SCOPE.
    mod_path: str = ""
    # Nonempty ONLY on a record describing a type OUTSIDE the crate: the fully
    # qualified path the crate binds with a `use` from `std`/`core`/`alloc`
    # (`std::backtrace::Backtrace`). The extractor never emits it —
    # `adapters/std_types.py` appends such records after the crate's own, so
    # a fill prompt can render the API of a standard-library type the crate
    # re-exports. Run `3003-93e9a9bf` (2026-09-02): the Debug formatter fill
    # saw `backtrace: Option<Backtrace>` with no record behind the name and
    # printed the backtrace without the `status()` check its tests require.
    # `kind` on such a record carries the prefix `external ` ("external
    # struct"), so every consumer that switches on the four crate kinds —
    # the private-surface classifier, the pub-items manifest, the
    # producible-type set — leaves it alone; a consumer that must tell the
    # two apart keys on this field, never on `file`.
    external_path: str = ""
    # Traits the external type's own source file implements for it
    # (`Display`, `Debug`), from a scan of `impl … Trait for Name` headers:
    # `methods` holds inherent methods only, and whether a std type prints
    # through Display or Debug is exactly what a formatter fill has to know.
    external_trait_impls: tuple[str, ...] = ()
    # Every `impl Trait for <this crate type>` header in the crate, any file
    # (extractor `TypeRecord.trait_impls`). None = the record came from an
    # extractor build without the field (or was hand-made): the renderer then
    # prints no list, because the list's whole point is that it is COMPLETE —
    # a partial one read as complete would assert an impl is absent when it
    # is not. An empty tuple is a real reading: this type implements nothing.
    trait_impls: tuple[TraitImplRecord, ...] | None = None
    # Impls whose self type is one of the impl's own type parameters
    # (`impl<T: Bound> Trait for T`). The extractor emits the list once per
    # enumeration and `type_records_from_enumeration` attaches that one tuple
    # to every record, so any subset a prompt renders still carries it. None
    # = the extractor did not report it. Empty = this crate writes none. Not
    # an impl of this named type: the section prints the list once, and the
    # per-type sentence does not treat its absence here as "this crate does
    # not implement the trait".
    blanket_impls: tuple[TraitImplRecord, ...] | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TypeRecord:
        return cls(
            file=d.get("file", ""),
            kind=d.get("kind", ""),
            name=d.get("name", ""),
            generics=d.get("generics", ""),
            definition=d.get("definition", ""),
            fields=tuple(FieldRecord.from_dict(f) for f in (d.get("fields") or [])),
            variants=_strs(d.get("variants")),
            methods=tuple(MethodRecord.from_dict(m) for m in (d.get("methods") or [])),
            is_pub=bool(d.get("is_pub", False)),
            is_exactly_pub=bool(d.get("is_exactly_pub", True)),
            mod_path=d.get("mod_path", ""),
            external_path=d.get("external_path", ""),
            external_trait_impls=_strs(d.get("external_trait_impls")),
            trait_impls=(None if d.get("trait_impls") is None else tuple(
                TraitImplRecord.from_dict(t) for t in d["trait_impls"])),
            blanket_impls=(None if d.get("blanket_impls") is None else tuple(
                TraitImplRecord.from_dict(t) for t in d["blanket_impls"])),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "kind": self.kind,
            "name": self.name,
            "generics": self.generics,
            "definition": self.definition,
            "fields": [f.to_dict() for f in self.fields],
            "variants": list(self.variants),
            "methods": [m.to_dict() for m in self.methods],
            "is_pub": self.is_pub,
            "is_exactly_pub": self.is_exactly_pub,
            "mod_path": self.mod_path,
            **({"trait_impls": [t.to_dict() for t in self.trait_impls]}
               if self.trait_impls is not None else {}),
            **({"blanket_impls": [t.to_dict() for t in self.blanket_impls]}
               if self.blanket_impls is not None else {}),
            # Only an external record carries these keys: the Rust side never
            # emits them, so a crate record round-trips to the exact wire shape.
            **({"external_path": self.external_path,
                "external_trait_impls": list(self.external_trait_impls)}
               if self.external_path else {}),
        }



def type_records_from_enumeration(data: Any) -> list[TypeRecord] | None:
    """Decode `--enumerate-types` output. The extractor emits one object:
    `types` (the records) and `blanket_impls` (the crate's type-parameter
    impls, which belong to no record). The one blanket tuple is attached to
    every record. A bare list is a build that predates the object: its
    records keep whatever they carry. Any other shape is None, the adapter's
    failure contract."""
    if isinstance(data, list):
        return [TypeRecord.from_dict(r) for r in data]
    if not isinstance(data, dict) or not isinstance(data.get("types"), list):
        return None
    raw = data.get("blanket_impls")
    blankets = (None if raw is None
                else tuple(TraitImplRecord.from_dict(t) for t in raw))
    return [replace(TypeRecord.from_dict(r), blanket_impls=blankets)
            for r in data["types"]]


@dataclass(frozen=True, slots=True)
class FnRef:
    """One reference a fn body spells (model.rs FnRef): ``kind`` is
    ``path`` (``Q::name`` / ``Q::name::<T>``, ``qualifier`` = ``Q``, or
    empty for a turbofish-only ``name::<T>``), ``call`` (``name(…)``) or
    ``method`` (``.name(…)``). Syntax only — resolution to a record is
    `domain.reference_graph`'s."""
    kind: str
    qualifier: str
    name: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FnRef:
        return cls(str(d.get("kind", "")), str(d.get("qualifier", "")),
                   str(d.get("name", "")))

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "qualifier": self.qualifier,
                "name": self.name}

@dataclass(frozen=True, slots=True)
class FnRecord:
    """A free fn / inherent or trait-impl method / trait method (model.rs FnRecord)."""
    file: str = ""              # relative path from input root
    fn_name: str = ""
    sig_text: str = ""          # verbatim, up to but excluding `{`
    body_text: str = ""         # verbatim between `{` and `}`, no braces
    sig_byte_start: int = 0
    body_byte_start: int = 0    # byte offset just AFTER the opening `{`
    body_byte_end: int = 0      # byte offset of the closing `}` (exclusive)
    cfg_predicates: tuple[str, ...] = ()
    is_pub: bool = False
    # Strictly `Visibility::Public` — see TypeRecord.is_exactly_pub for why
    # this is a separate field, not a redefinition of `is_pub`. Defaults to
    # True (matches TypeRecord's default) so existing fixtures/tests that
    # only set `is_pub` keep their prior meaning.
    is_exactly_pub: bool = True
    container: str = ""         # "" free fn | "impl X for Y" | "trait T"
    # True iff the enclosing impl's self type is one of that impl's OWN generic
    # type parameters (`impl<E> Trait for E`): the method's `self` is a value
    # this crate has not yet erased. Decided by the extractor on the syn AST —
    # `container` cannot carry it, because the rendered header drops the
    # generics list and `impl Trait for E` prints the same for a generic E and
    # a concrete type named E. The producer-bypass instruction split keys on
    # it; False (free fns, concrete self, anything undecidable) takes the
    # cautious table-form instruction.
    impl_self_generic: bool = False
    # `::`-joined inline-module path within the file ("" at file scope). Rust
    # forbids two items of one name in one module, so (file, mod_path, fn_name)
    # names at most one COMPILED fn even when the source declares several
    # variants gated by an attribute macro no cfg evaluation can decide.
    mod_path: str = ""
    has_unimplemented_body: bool = False
    is_test: bool = False       # carries a test attr (#[test]/#[tokio::test]/#[rstest]/…)
    # Every fn-shaped reference the body spells (model.rs FnRef), as syntax;
    # `domain.reference_graph` resolves them. Empty for records written
    # before the extractor emitted them.
    refs: tuple[FnRef, ...] = ()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FnRecord:
        return cls(
            file=d.get("file", ""),
            fn_name=d.get("fn_name", ""),
            sig_text=d.get("sig_text", ""),
            body_text=d.get("body_text", ""),
            sig_byte_start=int(d.get("sig_byte_start", 0)),
            body_byte_start=int(d.get("body_byte_start", 0)),
            body_byte_end=int(d.get("body_byte_end", 0)),
            cfg_predicates=_strs(d.get("cfg_predicates")),
            is_pub=bool(d.get("is_pub", False)),
            is_exactly_pub=bool(d.get("is_exactly_pub", True)),
            container=d.get("container", ""),
            impl_self_generic=bool(d.get("impl_self_generic", False)),
            mod_path=d.get("mod_path", ""),
            has_unimplemented_body=bool(d.get("has_unimplemented_body", False)),
            is_test=bool(d.get("is_test", False)),
            refs=tuple(FnRef.from_dict(r) for r in d.get("refs") or ()
                       if isinstance(r, dict)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "fn_name": self.fn_name,
            "sig_text": self.sig_text,
            "body_text": self.body_text,
            "sig_byte_start": self.sig_byte_start,
            "body_byte_start": self.body_byte_start,
            "body_byte_end": self.body_byte_end,
            "cfg_predicates": list(self.cfg_predicates),
            "is_pub": self.is_pub,
            "is_exactly_pub": self.is_exactly_pub,
            "container": self.container,
            "impl_self_generic": self.impl_self_generic,
            "mod_path": self.mod_path,
            "has_unimplemented_body": self.has_unimplemented_body,
            "is_test": self.is_test,
            "refs": [r.to_dict() for r in self.refs],
        }


@dataclass(frozen=True, slots=True)
class ModRecord:
    """A `mod` declaration (model.rs ModRecord)."""
    name: str = ""
    file: str = ""
    cfg_predicates: tuple[str, ...] = ()
    declared_in: str = ""
    # The `mod` declaration's OWN visibility: "public" | "restricted" | "private".
    # A non-"public" mod wall makes everything behind it externally
    # unreachable regardless of the nested items' own visibility — see
    # domain/module_reachability.py.
    vis: str = "private"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ModRecord:
        return cls(
            name=d.get("name", ""),
            file=d.get("file", ""),
            cfg_predicates=_strs(d.get("cfg_predicates")),
            declared_in=d.get("declared_in", ""),
            vis=d.get("vis", "private"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "file": self.file,
            "vis": self.vis,
            "cfg_predicates": list(self.cfg_predicates),
            "declared_in": self.declared_in,
        }


@dataclass(frozen=True, slots=True)
class UseRecord:
    """One name a file's TOP-LEVEL `use` declarations bind (model.rs UseRecord).

    A bare type identifier does not identify a crate item on its own: with
    `use std::error::Error as StdError;` the ident `StdError` denotes std's
    trait even in a crate that also declares a private `StdError` of its own.
    These records are what `domain.rust_name_scope` resolves names against."""

    file: str = ""
    alias: str = ""             # ident bound in the file's scope; "*" for a glob
    path: tuple[str, ...] = ()  # segments as written, terminal included
    is_glob: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> UseRecord:
        return cls(
            file=d.get("file", ""),
            alias=d.get("alias", ""),
            path=_strs(d.get("path")),
            is_glob=bool(d.get("is_glob", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "alias": self.alias,
            "path": list(self.path),
            "is_glob": self.is_glob,
        }


@dataclass(frozen=True, slots=True)
class CfgOccurrence:
    """One `#[cfg(...)]` / `#[cfg_attr(COND, ...)]` occurrence and the leaf atoms
    of its condition (model.rs CfgOccurrence). Emitted by `--enumerate-cfgs`;
    consumed by `domain.cluster_plan` to derive compiler clusters."""
    file: str = ""
    atoms: tuple[CfgAtom, ...] = ()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CfgOccurrence:
        return cls(
            file=d.get("file", ""),
            atoms=tuple(
                CfgAtom(key=a.get("key", ""), value=a.get("value"))
                for a in (d.get("atoms") or [])
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "atoms": [{"key": a.key, "value": a.value} for a in self.atoms],
        }


@dataclass(frozen=True, slots=True)
class ConstRecord:
    """A `const` / `static` item (model.rs ConstRecord)."""
    file: str = ""
    name: str = ""
    ty: str = ""
    is_pub: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ConstRecord:
        return cls(
            file=d.get("file", ""),
            name=d.get("name", ""),
            ty=d.get("ty", ""),
            is_pub=bool(d.get("is_pub", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.file, "name": self.name, "ty": self.ty, "is_pub": self.is_pub}


@dataclass(frozen=True, slots=True)
class DocItemRecord:
    """One item's doc text and the cfg chain that decides whether rustdoc
    collects its examples (model.rs DocItemRecord, `--enumerate-doc-items`)."""
    file: str = ""
    line: int = 0
    cfg_predicates: tuple[str, ...] = ()
    doc: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DocItemRecord:
        return cls(
            file=d.get("file", ""),
            line=int(d.get("line", 0) or 0),
            cfg_predicates=_strs(d.get("cfg_predicates")),
            doc=d.get("doc", ""),
        )


@dataclass(frozen=True, slots=True)
class DispatchFamily:
    """A method-dispatch family: trait + macro chain (model.rs DispatchFamily)."""
    method: str = ""
    traits: tuple[str, ...] = ()
    macros: tuple[str, ...] = ()
    trait_files: tuple[str, ...] = ()
    macro_defs: tuple[str, ...] = ()
    return_types: tuple[str, ...] = ()
    chained_methods: tuple[str, ...] = ()
    member_files: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DispatchFamily:
        return cls(
            method=d.get("method", ""),
            traits=_strs(d.get("traits")),
            macros=_strs(d.get("macros")),
            trait_files=_strs(d.get("trait_files")),
            macro_defs=_strs(d.get("macro_defs")),
            return_types=_strs(d.get("return_types")),
            chained_methods=_strs(d.get("chained_methods")),
            member_files=_strs(d.get("member_files")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "traits": list(self.traits),
            "macros": list(self.macros),
            "trait_files": list(self.trait_files),
            "macro_defs": list(self.macro_defs),
            "return_types": list(self.return_types),
            "chained_methods": list(self.chained_methods),
            "member_files": list(self.member_files),
        }


# A reconstruction frontier: maps (file_rel, fn_name) → the set of test names (or the
# sentinel "<compile>") that reach that still-broken fn at runtime/compile time. Produced
# by the TDD driver from cargo output and consumed by the per-fn / per-impl fill planners.
# It is a mapping keyed by identity, not a record with fields, so a type alias — not a
# dataclass — is its correct domain form. The key is a `FnLocator` (file, fn_name).
Frontier = dict[FnLocator, set[str]]

# Conviction / stamp identity: one site per (file, name, canonical container).
# Invariant: every test on site ``s`` is a subset of
# ``dispatch[FnLocator(s.file, s.fn_name)]``.
SiteFrontier = dict[FnSite, set[str]]


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One rustc error from cargo's `--message-format=json` output — the cargo↔Python
    seam. Produced by `_cargo_diagnostics_from_json`; `code` is "" when rustc gave no
    error code, `file`/`line` come from the primary span ("" / 0 when absent)."""
    code: str = ""           # "E0277" or ""
    file: str = ""           # primary span file, relative; "" if none
    line: int = 0            # primary span start line; 0 if none
    message: str = ""
    rendered: str = ""       # rustc's own rendered text (may carry ANSI)
    # Unambiguous "consider importing ..." suggestions rustc attached to this
    # error — applied mechanically by the import-repair step (a body-only
    # splice cannot add `use` items, so the compiler supplies them).
    import_fixes: tuple[ImportFix, ...] = ()
    # When the error surfaces INSIDE a macro expansion, the primary span points
    # at the macro DEFINITION (`backtrace_if_absent!`'s body), never at the fn
    # that invoked it. rustc's `expansion.span` chain names each invocation
    # call-site — the real culprit a fn-granular revert must reach. Empty for a
    # non-macro error. Each entry is (file_rel, line) of one call-site.
    macro_call_sites: tuple[tuple[str, int], ...] = ()
    # Every NON-primary labelled span, in rustc's order. A cross-file error
    # names both of its ends and only one of them is primary: `E0592`
    # ("duplicate definitions with name `debug`") rendered its primary span at
    # `src/error.rs:301` and the definition it collides with at
    # `src/fmt.rs:11`, under `:::`. Carrying only the primary made the second
    # file unreachable to every consumer — the frontier attributed one fn, the
    # repair rewrote its body, and the error re-checked byte-identical, which
    # is how one duplicate definition survived two full repair passes on
    # 2026-08-11. Each entry is (file_rel, line).
    secondary_spans: tuple[tuple[str, int], ...] = ()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Diagnostic:
        return cls(
            code=d.get("code", ""),
            file=d.get("file", ""),
            line=int(d.get("line", 0)),
            message=d.get("message", ""),
            rendered=d.get("rendered", ""),
            import_fixes=tuple(
                ImportFix(**f) for f in d.get("import_fixes", ())),
            macro_call_sites=tuple(
                (str(f), int(ln)) for f, ln in d.get("macro_call_sites", ())),
            secondary_spans=tuple(
                (str(f), int(ln)) for f, ln in d.get("secondary_spans", ())),
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "code": self.code,
            "file": self.file,
            "line": self.line,
            "message": self.message,
            "rendered": self.rendered,
        }
        if self.import_fixes:  # wire-compat: absent key ⇔ no fixes
            d["import_fixes"] = [
                {"file": f.file, "byte_start": f.byte_start,
                 "byte_end": f.byte_end, "replacement": f.replacement}
                for f in self.import_fixes]
        if self.macro_call_sites:  # wire-compat: absent key ⇔ no expansion
            d["macro_call_sites"] = [[f, ln] for f, ln in self.macro_call_sites]
        if self.secondary_spans:  # wire-compat: absent key ⇔ single-span error
            d["secondary_spans"] = [[f, ln] for f, ln in self.secondary_spans]
        return d


@dataclass
class BuildCluster:
    """One compiler configuration (e.g. stable, nightly, feature/async).

    Mutable: `requires_nightly` is flipped at runtime when an E0554 is detected.
    `cfg_true`/`cfg_false` expand each bare active/excluded predicate into both the
    bare form (`N`) and the cargo-feature form (`feature = "N"`) because source code
    may use either; the extractor's CfgSet ignores keys that match no real predicate.
    """
    name: str                           # short identifier used in logs and state keys
    env_extra: dict[str, str]           # env vars merged into _cargo_env() for this cluster
    tarpaulin_exclude_files: list[str]  # --exclude-files patterns for tarpaulin
    excluded_cfg: list[str]             # cfg predicates inactive in this cluster
    active_cfg: list[str]               # cfg predicates active (on top of default)
    prompt_nightly_rule: str            # nightly boundary sentence(s) for generation prompts
    is_default: bool = True             # True for the baseline stable cluster
    # Cargo features the crate turns on by itself (`[features] default = [...]`).
    # NOT part of `cfg_true`: that list is what this cluster adds on TOP of the
    # default build and is what reaches cargo/RUSTFLAGS and the skeletoniser,
    # whereas "is this item compiled here" additionally has to know the crate's
    # own defaults. Conflating the two is what made `excludes_for_cargo` answer
    # that `feature = "std"` — anyhow's default-on feature, and the arm the
    # whole build compiles — was EXCLUDED, while keeping the `not(feature =
    # "std")` arm (live cluster `stable`, 2026-09-01). `eval_cfg_true` is the
    # evaluation half; `domain.cfg.cluster_cfg_set` is its one reader.
    default_features: list[str] = field(default_factory=list)
    gated_files: set[str] | None = None  # source files (rel paths) gated behind excluded_cfg — skip augmentation
    requires_nightly: bool = False      # cluster needs nightly toolchain (set at runtime when E0554 detected)

    def _expand(self, preds: list[str]) -> list[str]:
        out: list[str] = []
        for pred in (preds or []):
            out.append(pred)
            if "=" not in pred and "(" not in pred:
                out.append(f'feature = "{pred}"')
        return out

    @property
    def cfg_true(self) -> list[str]:
        """cfg predicates that evaluate true under this cluster (bare + feature form)."""
        return self._expand(self.active_cfg)

    @property
    def cfg_false(self) -> list[str]:
        """cfg predicates that evaluate false under this cluster (bare + feature form)."""
        return self._expand(self.excluded_cfg)

    @property
    def eval_cfg_true(self) -> list[str]:
        """`cfg_true` plus the crate's own default features, for deciding
        whether an item is COMPILED here.

        Only the `feature = "X"` form: a cargo feature is never a bare cfg
        ident, and `_expand`'s bare form exists for `--cfg N` flags, which a
        default feature is not. Kept separate from `cfg_true` because that
        property is also the skeletoniser's argument, and a bake that starts
        dropping the crate's inactive cfg arms rewrites the authored layout —
        a tree-construction change, validatable only by a wipe."""
        return [*self.cfg_true,
                *(f'feature = "{f}"' for f in (self.default_features or []))]

    @property
    def features(self) -> list[str]:
        """Cargo feature names enabled in this cluster: `feature = "X"` → X, plus bare
        lowercase identifiers in active_cfg treated as feature names. (Parsed with plain
        string ops — no regex — per the project's structured-parsing rule.)"""
        out: list[str] = []
        for pred in (self.active_cfg or []):
            key, eq, val = pred.partition("=")
            if eq and key.strip() == "feature":
                v = val.strip()
                if len(v) > 2 and v[0] == '"' and v[-1] == '"':   # feature = "X", X non-empty
                    out.append(v[1:-1])
            elif "=" not in pred and "(" not in pred and pred \
                    and (pred[0] == "_" or (pred[0].isalpha() and pred[0].islower())) \
                    and all(c.isalnum() or c == "_" for c in pred):
                out.append(pred)
        return out

    @property
    def toolchain(self) -> str | None:
        """Required toolchain name (e.g. "nightly") or None for the stable default."""
        return "nightly" if self.requires_nightly else None


# Version of augmentation's function-execution criterion
# (`domain.fn_execution`). A `done` verdict or a function record that does not
# carry this exact value was produced by a stop that never asked whether each
# function the cluster compiles is executed by some test.
FN_EXECUTION_CRITERION = "fn-execution/1"


@dataclass(frozen=True, slots=True)
class AugmentVerdict:
    """Why a (cluster, module) stopped being augmented, and under which pipeline.

    The cluster sentinel used to hold a bare coverage number, which cannot
    distinguish "the target was reached" from "this code gave up" — so a verdict
    produced by a defect outlived the commit that fixed it. On 2026-08-27, run
    `2881435-34981ab0` (`code=3264439950bf`) dropped every fenced reply; the run
    logged 63 `llm.request` events and `_pass_had_work` was set only on a
    committed block,
    so both passes over anyhow's `stable` cluster reported a cfg ceiling against
    119 actionable uncovered lines. The cluster was sealed at 83.9% against a
    ~98.1% estimate. The next launch (`1566274-2e1d805c`, `code=4a6d8d3f3e95`)
    skipped it: the only remover of a cluster sentinel is the full `--clean`
    workspace wipe, and `clear_module_sentinel` clears a different file.

    `reason` is the terminating status: "done" when the coverage target was met,
    or the crate is proc-macro and cannot be augmented; otherwise the give-up
    ("ceiling", "hard_block", "precheck_failed"). Only "done" is a claim about
    the TREE. Every other value is a claim about the pipeline that was running,
    so it binds only while `code_id` still matches `code_identity` — the 12-hex
    digest of `pipeline.py` plus `pipeline_core/**/*.py`. That is a different
    identity from the crate `source_hash` (`Cargo.toml` + `src/` + test heads)
    that `pipeline_state.md` records on phase lines; `phase_valid` does not even
    compare those hashes, and a pipeline-only extractor fix leaves the crate
    hash unchanged. Keying a give-up on the crate hash cannot reopen the
    cluster the commit that fixed the cause is supposed to re-enter.

    The canonical text form keeps the coverage on the first line, so a sentinel
    written before this field existed still reads back as a number. `from_text`
    also accepts a legacy `source_hash=` line as the code identity.

    `criterion` is the function-execution criterion the stop applied
    (`FN_EXECUTION_CRITERION`), with its summary: whether function execution
    was measured, how many functions were still unexecuted within their
    attempt budget, and the sites measured unreached. A `done` written without
    it is a line-coverage `done` — on anyhow's `nightly` (run
    `330242-0be2c3e1`, 2026-09-03) it recorded `95.2 / reason=done` over
    functions no test had entered — and it does not bind: a resume under the
    criterion re-augments instead of inheriting it.
    """

    coverage: float
    reason: str = ""
    code_id: str = ""
    criterion: str = ""
    fn_measured: bool = False
    unexecuted: int = 0
    unreached: tuple[str, ...] = ()
    # The per-file line floor the stop applied (0 = none) and the files it
    # left below that floor, each as `<file> <pct>% <verdict>`.
    file_floor: float = 0.0
    files_below: tuple[str, ...] = ()

    def is_final(self) -> bool:
        """True when the verdict is a claim about the tree rather than about the
        augmentation code — the target was reached, or the crate cannot be
        augmented at all."""
        return self.reason == "done"

    def still_binding(self, code_id: str, file_floor: float = 0.0) -> bool:
        """True when this verdict still answers "skip this cluster".

        A final verdict binds under any pipeline code, but only when the stop
        that wrote it applied the current function-execution criterion. A
        give-up verdict binds only while the pipeline that produced it is the
        pipeline on disk (`code_identity`). A verdict with no recorded
        provenance — a bare-number sentinel written before this field existed
        — never binds, so the first run after the change re-measures instead
        of inheriting an unattributable claim.

        `file_floor` is the per-file line floor the current stop applies: a
        final verdict written under a laxer floor (or none) binds no longer,
        since the files it left below that floor were never asked of a test.
        """
        return not self.unbinding_reason(code_id, file_floor)

    def unbinding_reason(self, code_id: str, file_floor: float = 0.0) -> str:
        """Why this verdict no longer binds ("" when it does)."""
        if self.is_final():
            if self.criterion != FN_EXECUTION_CRITERION:
                return ("written by a stop that did not apply the function-"
                        f"execution criterion (recorded "
                        f"'{self.criterion or 'none'}', now "
                        f"'{FN_EXECUTION_CRITERION}')")
            if self.file_floor < file_floor:
                return ("written by a stop that applied a per-file floor of "
                        f"{self.file_floor:g}%, now {file_floor:g}%")
            return ""
        if not self.code_id:
            return "no recorded pipeline identity"
        if self.code_id != code_id:
            return (f"from pipeline code that is no longer on disk (recorded "
                    f"{self.code_id}, now {code_id})")
        return ""

    def to_text(self) -> str:
        """The sentinel's canonical text: coverage first (legacy-readable), then
        the provenance this class exists to carry."""
        text = (f"{self.coverage:.1f}\n"
                f"reason={self.reason}\n"
                f"code_id={self.code_id}\n")
        if self.criterion:
            text += (f"criterion={self.criterion}\n"
                     f"fn_measured={'yes' if self.fn_measured else 'no'}\n"
                     f"unexecuted={self.unexecuted}\n"
                     f"unreached={';'.join(self.unreached)}\n")
        if self.file_floor:
            text += (f"file_floor={self.file_floor:g}\n"
                     f"files_below={';'.join(self.files_below)}\n")
        return text

    @classmethod
    def from_text(cls, text: str) -> AugmentVerdict:
        """Parse a sentinel. Tolerant by design: an unparseable or bare-number
        file yields a verdict with no provenance, which `still_binding` refuses
        rather than trusting. A `source_hash=` line is the previous name for
        the same field and is read as `code_id`."""
        lines = text.strip().splitlines()
        try:
            coverage = float(lines[0].strip()) if lines else 0.0
        except ValueError:
            coverage = 0.0
        fields = {}
        for line in lines[1:]:
            key, sep, value = line.partition("=")
            if sep:
                fields[key.strip()] = value.strip()
        try:
            unexecuted = int(fields.get("unexecuted", "0") or 0)
        except ValueError:
            unexecuted = 0
        try:
            file_floor = float(fields.get("file_floor", "0") or 0)
        except ValueError:
            file_floor = 0.0
        return cls(coverage=coverage,
                   reason=fields.get("reason", ""),
                   code_id=fields.get("code_id") or fields.get("source_hash", ""),
                   criterion=fields.get("criterion", ""),
                   fn_measured=fields.get("fn_measured", "") == "yes",
                   unexecuted=unexecuted,
                   unreached=tuple(u for u in fields.get("unreached", "").split(";") if u),
                   file_floor=file_floor,
                   files_below=tuple(f for f in fields.get("files_below", "").split(";") if f))


@dataclass
class ModuleAugmentState:
    """Per-(module, cluster) augmentation lifecycle.

    Consolidates the five outer-loop state variables that used to live as parallel
    dicts (_c_retries, _c_best_covered, _c_total + _stall + _at_ceiling). Every exit
    path goes through `status()`, so adding a new termination condition is a single-case
    edit instead of a five-place edit.

    Lifecycle:
      record_pass(...) is called after each task0c_augment_module() return.
      status(crate_type) returns one of:
        'done'       — coverage at target AND no function the cluster compiles is
                       still unexecuted within its attempt budget AND no file is
                       held under the per-file floor, or crate is proc-macro
                       (cannot augment)
        'ceiling'    — task0c reported at_ceiling (no actionable lines remain)
        'hard_block' — stall_passes >= stall_limit (genuinely stuck)
        'retry'      — keep going
    """
    module_name: str
    cluster_name: str
    total_passes: int = 0
    best_covered_lines: int = -1
    stall_passes: int = 0    # consecutive passes with no improvement in covered count
    ceiling_passes: int = 0  # consecutive passes where task0c reported at_ceiling
    last_pct: float = 0.0
    last_covered: int = 0
    last_at_ceiling: bool = False
    target_pct: float = 100.0  # coverage % at/above which the module is 'done'
    # Consecutive passes with no new best covered-line count and no function
    # newly accounted for before the module is accepted as 'hard_block'
    # (`TASK0C_PASS_STALL_LIMIT`). Any single new covered line resets it.
    stall_limit: int = 10
    # Functions the cluster compiles that no test executes and that are still
    # inside their targeted-attempt budget, as of the last pass (0 when the
    # criterion could not be measured — the pass logged why).
    last_fns_pending: int = 0
    best_fns_pending: int = -1
    # Files under the per-file line floor on reachable lines and not accounted
    # for (`domain.file_coverage_floor`), as of the last pass; 0 when the floor
    # is off.
    last_files_held: int = 0

    CEILING_CONFIRM: int = 2  # consecutive at_ceiling reports required before accepting ceiling

    def record_pass(self, pct: float, covered: int, at_ceiling: bool,
                    fns_pending: int = 0, files_held: int = 0) -> None:
        self.total_passes += 1
        self.last_pct = pct
        self.last_covered = covered
        self.last_at_ceiling = at_ceiling
        self.last_fns_pending = fns_pending
        self.last_files_held = files_held
        if at_ceiling:
            self.ceiling_passes += 1
        else:
            self.ceiling_passes = 0
        # Accounting a function (executed, or unreached after its budget) is
        # progress on the stop's second half even when no line moved.
        fns_improved = 0 <= fns_pending < self.best_fns_pending
        if self.best_fns_pending < 0 or fns_pending < self.best_fns_pending:
            self.best_fns_pending = fns_pending
        if covered > self.best_covered_lines or fns_improved:
            self.best_covered_lines = max(self.best_covered_lines, covered)
            self.stall_passes = 0
        else:
            self.stall_passes += 1

    def status(self, crate_type: str) -> str:
        if (self.last_pct >= self.target_pct and self.last_fns_pending == 0
                and self.last_files_held == 0):
            return 'done'
        if crate_type == 'proc-macro':
            return 'done'
        if self.ceiling_passes >= self.CEILING_CONFIRM:
            return 'ceiling'
        if self.stall_passes >= self.stall_limit:
            return 'hard_block'
        return 'retry'


@dataclass(frozen=True, slots=True)
class FillResult:
    """Outcome of one per-fn fill unit (de-closure of
    `_process_one_stub`). The unit returns this instead of mutating the
    pass's counters through a closure — the dispatcher aggregates under its
    own lock.

    attempts counts the LLM attempts this fn consumed (0 when skipped before
    any model call). `error` carries the exhaustion message destined for the
    pass's errors list; None for success, skips, and silent early exits.

    `blocked_by` names the still-stubbed sites that held every test attributed
    to this fn — non-empty only when the body COMPILED and was kept without a
    verdict on its own merits: either the tests could not run at all
    (`U2Decision.ACCEPT_EXTERNAL_ONLY`, sites from the panic location, with
    container), or they ran and failed but every failure also needs a
    still-stubbed sibling (`sibling_blocked_u2_failures`, sites from the
    frontier attribution, container unknown and left ``""``). Empty means the
    unit was judged on its own merits, or landed nothing at all.
    `domain.pass_payoff` reads these together: a pass in which one site blocks
    every landed body cannot move a verdict."""
    file: str = ""
    fn_name: str = ""
    filled: bool = False
    attempts: int = 0
    error: str | None = None
    blocked_by: tuple[FnSite, ...] = ()
    # Terminal :class:`~pipeline_core.domain.fill_class.FillOutcomeClass`
    # value for this unit, or "" when the engine did not classify it
    # (skip-before-dispatch, tests). Aggregated onto ClusterFillResult.
    fill_class: str = ""
    # The other functions of the unit's file its reply named as one write
    # with it (`domain.joint_write`): `joint_named` every set the model
    # asked for and the gate admitted, whatever became of it — the model's
    # claim that these bodies belong together; `joint` the extras whose
    # bodies are on the tree when the unit returns (landed with the target
    # or restored by the salvage). Both empty for a single-body fill.
    joint: tuple[FnLocator, ...] = ()
    joint_named: tuple[tuple[FnLocator, ...], ...] = ()


@dataclass(frozen=True, slots=True)
class FnFixContext:
    """The per-fn prompt context the fill engine assembles before each model
    call: the impl where-clause, the TYPES_IN_SCOPE section, and the
    relevance-filtered model notes. A pure value object so the fill use-cases
    can type the `PromptContext.gather(...)` port result without importing the
    static-analysis machinery that derives it (that lives behind the port)."""
    container: str = ""
    impl_where: str = ""
    types_section: str = ""
    notes_section: str = ""
    bound_methods_section: str = ""
    # The crate blueprint slice relevant to this fn ("" without one).
    blueprint_section: str = ""


@dataclass(frozen=True, slots=True)
class ClusterFillResult:
    """Outcome of one cluster's phase-C fill — the typed result every fill
    engine (per-fn, per-impl, TDD driver) returns end-to-end; the legacy dict
    bridge is gone."""
    fns_filled: int = 0
    bytes_filled: int = 0
    attempts: int = 0
    errors: tuple[str, ...] = ()
    remaining: int = 0
    impls_filled: int = 0
    impls_failed: int = 0
    # True when the fill loop gave up because the tree does not BUILD and no
    # diagnostic resolved to a fillable fn — "the baseline itself is broken",
    # not "this cluster is finished". The distinction has to survive the
    # return, because the caller checkpoints on it: on 2026-08-01 `nightly`
    # stopped this way at pass=0 and was still recorded `done — validated`,
    # which permanently skips the cluster on every later run. The final
    # validation could not catch it either — it runs `cargo check`, which
    # builds the lib, and the broken targets were the TEST binaries.
    baseline_broken: bool = False
    # The last sweep's verdict sets, carried out of the driver because the MERGE
    # needs them and cannot recompute them: a checkpointed cluster is skipped on
    # every later run, so by merge time its tree is on disk and its evidence is
    # not. Without these the merge can only gate a divergent body by the cluster
    # it came from, which is how `stable`'s 2026-07-30 bodies kept reaching the
    # default build after `nightly` had fixed the same fns on 08-04.
    # `ran_tests` is the scope the two sets are meaningful in — a test one
    # cluster's cfg excludes from the build is absent, not passing.
    failed_tests: frozenset[str] = frozenset()
    ran_tests: frozenset[str] = frozenset()
    # What the cluster gave up ON, and whether it gave up at all. A cluster that
    # plateaus with failing tests is checkpointed with the same marker as one
    # that finished, and clusters seed forward only — so `nightly` was retired
    # on 2026-08-04 at 100/104 with four tests still failing, and everything it
    # had learned while failing stayed behind. Two of those four were fixed for
    # free by the merge taking the other cluster's bodies; the rest became the
    # task3 retry's problem, and that loop starts from a bare failing-test name.
    # `exhausted_fns` is the frontier the driver marked permanently exhausted at
    # its plateau exit: the fns it tried and could not make pass.
    exhausted: bool = False
    exhausted_fns: tuple[tuple[str, str], ...] = ()
    # The fns a fill route ACTUALLY attempted this call (a model attempt was
    # made, or a body landed) — as opposed to the frontier the caller asked
    # for. The two differ whenever a route stands down: the impl route defers
    # itself while free-fn blockers remain, and the per-fn route takes only
    # free fns, so an impl member can sit in the frontier of every iteration
    # and never be dispatched by anyone. The TDD driver used to charge an
    # exhaustion strike to every frontier key still a stub after the pass;
    # on 2026-08-11/12 that marked the `impl Counter` members exhausted at
    # iteration 1 in all six A/B/C cluster runs — before any route had seen
    # them — which made their block "not viable" from iteration 2 on and left
    # every cluster plateaued below its oracle on fns the post-merge impl
    # route then filled on attempt 1. `None` (the default) means the engine
    # did not report dispatch — legacy callers and test stubs — and the
    # driver falls back to treating the whole frontier as dispatched.
    dispatched_fns: tuple[tuple[str, str], ...] | None = None
    # Units whose ONLY dispatch this call was a file-first prompt that came
    # back with nothing to route — the LLM errored, the reply parsed to zero
    # FN blocks, or nothing spliced. They are in `dispatched_fns` (the stamp
    # is written before the call), but no body of theirs was ever judged, so
    # the landed-change audit must not grade them: run `347397-01e2463b`
    # (2026-08-31) wrote `flat` ledger rows for two src/ensure.rs units off
    # an empty reply, and one of them — an owner of the still-failing
    # test_trailer — was deranked into the next iteration's flat sink on
    # that fabricated row. A unit a later route genuinely attempted, or one
    # with a reverted try, is excluded by the producer. Empty (the default)
    # means "none lost", so legacy callers and stubs behave as before.
    answer_lost_fns: tuple[tuple[str, str], ...] = ()
    # Units this fill spliced then RESTORED to the pre-fill body — a peel of
    # compiler-blamed bodies, or a whole-file compile / U2 / regression
    # revert. The landed-change audit's FLAT arm must not treat these as
    # "the model returned this body": the tree holds the snapshot. Empty
    # (the default) is "the fill reported none" — tests, the per-fn-only
    # path — so the arm behaves as before for those callers.
    reverted_fns: tuple[tuple[str, str], ...] = ()
    # What this fill pass could have moved, and which still-stubbed sites
    # held the landed bodies. Default None: callers and tests that do not
    # populate it behave as before. The TDD driver reads it to rank the
    # next batch by blocker frequency — the field the two fill routes used
    # to leave on a log line that nothing outer consumed.
    payoff: PassPayoff | None = None
    # Units a route's own gate refused after the schedule fixed them, with
    # the gate's reason: `(file, fn, reason)` (per-fn's claim refusals —
    # `premise`, `share`). Empty: none refused, or a caller that did not say.
    unclaimed_fns: tuple[tuple[str, str, str], ...] = ()
    # Per-fill class histogram for this call (impl + per-fn combined).
    # None: caller/test did not populate it. The TDD driver prints the
    # previous call's histogram on the next iter line.
    fill_classes: FillClassHistogram | None = None
    # The write sets a reply named as one (target first, then its extras;
    # `FillResult.joint_named`), for the route ledger: a later schedule
    # does not carve a named set apart (`fill_schedule.carve_rebuys`).
    joint_sets: tuple[tuple[FnLocator, ...], ...] = ()
    # The tests whose first failure moved when the fill ended on its
    # premise (`fill_scheduler._GroundGate`); empty when it ran its plan
    # out. The driver reads it so units the cut fill never opened go back
    # on the pass without spending the once-per-pass re-offer: the plan
    # changed under them, the schedule did not judge them.
    premise_moved: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Stub:
    """The input to one per-fn fill unit: an `unimplemented!()` body to
    reconstruct, identified by location + signature. A narrower projection than
    `FnRecord` — the fill engine only needs these four fields to locate, prompt,
    and splice. Threading this instead of bare dicts kills the `"fn"` vs
    `"fn_name"` key drift across the engine.

    `from_dict` is tolerant of BOTH a `FnRecord.to_dict()` (extra keys ignored)
    and the hand-built `{file, fn_name, container, sig_text}` fallback dict, so
    it accepts either stub-source path unchanged."""
    file: str = ""
    fn_name: str = ""
    container: str = ""
    sig_text: str = ""
    # The fn's accumulated `#[cfg(...)]` chain — lets the
    # splice's `locate_fn_by_record` cfg tier pick the RIGHT one of two
    # cfg-variant fns that share file+name+container+sig (the cap_limit pair),
    # so each variant fills into its own body instead of colliding on the first.
    cfg_predicates: tuple[str, ...] = ()
    # A BEHAVIORAL RE-FILL target — not an `unimplemented!()` stub but
    # an already-filled body that COMPILES yet whose attributed test fails (the
    # keep-while-red-wrong case). The fill unit then splices with `allow_nonstub=True`
    # and seeds the prompt with the current body + failing test (fix framing),
    # so the TDD driver can CORRECT wrong bodies, not only fill stubs. Default
    # False = ordinary stub fill (all scanned stubs, unchanged).
    refill: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Stub:
        return cls(
            file=d.get("file", ""),
            fn_name=d.get("fn_name", ""),
            container=d.get("container", "") or "",
            sig_text=d.get("sig_text", "") or "",
            cfg_predicates=tuple(d.get("cfg_predicates") or ()),
        )

    @classmethod
    def from_record(cls, r: FnRecord) -> Stub:
        return cls(file=r.file, fn_name=r.fn_name,
                   container=r.container, sig_text=r.sig_text,
                   cfg_predicates=tuple(r.cfg_predicates or ()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "fn_name": self.fn_name,
            "container": self.container,
            "sig_text": self.sig_text,
            "cfg_predicates": list(self.cfg_predicates),
        }

    @property
    def key(self) -> FnLocator:
        """The (file, fn_name) identity used as a frontier / ordering key."""
        return FnLocator(self.file, self.fn_name)


@dataclass(frozen=True, slots=True)
class ImplBlock:
    """One impl/trait/free-fn container grouped for the per-impl fill path,
    mirroring the dict `_collect_impl_blocks` builds. `members`/`stub_members` are
    `FnRecord`s (threading the typed record through the impl engine — kills the
    raw-dict member access); `stub_members` is the subset with an unimplemented
    body. `container` is the FULL impl header text (incl. where-clauses, "" for
    free fns); `related_containers` are sibling blocks in the same file sharing
    this block's trait name.

    `from_dict` parses members tolerantly (FnRecord.from_dict per member), so it
    accepts the legacy block dict unchanged; `to_dict` round-trips the exact wire
    shape the existing consumers read.

    `refill_members` (block-level analog of `Stub.refill`): already-filled,
    compiling members whose attributed test still fails — a co-design correction
    target. Populated only when the caller supplies a TDD frontier + failing-test
    map; the whole point of tracking these at BLOCK (not fn) granularity is that
    two co-designed members needing joint correction (e.g. a shared private tag
    scheme two fns must agree on) reach the model in the SAME LLM call, which a
    per-fn refill can never do."""
    file: str = ""
    container: str = ""
    members: tuple[FnRecord, ...] = ()
    stub_members: tuple[FnRecord, ...] = ()
    related_containers: tuple[str, ...] = ()
    refill_members: tuple[FnRecord, ...] = ()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ImplBlock:
        return cls(
            file=d.get("file", ""),
            container=d.get("container", "") or "",
            members=tuple(FnRecord.from_dict(m) for m in (d.get("members") or [])),
            stub_members=tuple(FnRecord.from_dict(m) for m in (d.get("stub_members") or [])),
            related_containers=_strs(d.get("related_containers")),
            refill_members=tuple(FnRecord.from_dict(m) for m in (d.get("refill_members") or [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "container": self.container,
            "members": [m.to_dict() for m in self.members],
            "stub_members": [m.to_dict() for m in self.stub_members],
            "related_containers": list(self.related_containers),
            "refill_members": [m.to_dict() for m in self.refill_members],
        }

    @property
    def id(self) -> str:
        """Stable hashable id (file::container) — the per-block lock / frontier key."""
        return f"{self.file}::{self.container}"

    @property
    def needs_fill(self) -> bool:
        """True if this block has ANY work left — a stub to fill or a wrong-but-
        compiling member to correct. Named once so `_viable`-style checks don't
        re-derive "does this block have work" from raw truthiness in multiple
        places (one of which would otherwise miss `refill_members`)."""
        return bool(self.stub_members or self.refill_members)


__all__ = [
    "FieldRecord", "MethodRecord", "TypeRecord",
    "FnRecord", "ModRecord", "ConstRecord", "DispatchFamily", "Diagnostic",
    "BuildCluster", "ModuleAugmentState", "FillResult", "Stub", "ImplBlock",
    "FnFixContext", "ClusterFillResult",
]
