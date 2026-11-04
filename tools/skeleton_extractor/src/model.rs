//! Serde record types — the JSON seam to the Python side.
//!
//! These `serde::Serialize` structs are the wire contract enumerated by the CLI subcommands
//! and parsed by `pipeline_core.domain.models`. Moved verbatim from `lib.rs`; the
//! serialized shape is unchanged — field names and order are the contract.

/// Result of skeletonising a single file.
#[derive(Debug, Clone, serde::Serialize)]
pub struct FileReport {
    pub relative_path: String,
    pub functions_stubbed: usize,
    pub test_mods_dropped: usize,
    pub bytes_in: usize,
    pub bytes_out: usize,
}

#[derive(serde::Serialize, Debug, Clone)]
pub struct FnRecord {
    pub file: String, // relative path from input root
    pub fn_name: String,
    pub sig_text: String,       // verbatim from source, up to but excluding `{`
    pub body_text: String,      // verbatim contents between `{` and `}`, no braces
    pub sig_byte_start: usize,  // byte offset of fn declaration start (incl any pub/async kws)
    pub body_byte_start: usize, // byte offset just AFTER the opening `{`
    pub body_byte_end: usize,   // byte offset of the closing `}` (exclusive end of body)
    pub cfg_predicates: Vec<String>, // accumulated cfg predicates gating this fn
    pub is_pub: bool,           // any pub visibility (incl pub(crate), pub(super))
    // Strictly `Visibility::Public` — see TypeRecord::is_exactly_pub for why
    // this is a SEPARATE field from `is_pub` rather than a redefinition.
    pub is_exactly_pub: bool,
    pub container: String, // "" for free fn, "impl X for Y" for inherent/trait impl, "trait T" for trait method
    // True iff the enclosing impl's SELF TYPE is one of that impl's own
    // generic type parameters (`impl<E> Trait for E`). `container` cannot
    // carry this: `render_impl_header` drops the generics list and the where
    // clause, so `impl Trait for E` (E generic — the method's `self` is still
    // an UNERASED value the caller chose) and `impl Trait for Error` (concrete
    // self) print identically. The producer-bypass instruction split keys on
    // exactly this fact: a generic self can be handed to a typed generic
    // producer; a concrete self needs its own per-type table (three identical
    // instructed `E0277`s on `Error::context`, run 57025-6ba652f2,
    // 2026-08-24). False for free fns and trait default methods.
    pub impl_self_generic: bool,
    // `::`-joined inline-module path within the file ("" at file scope). Rust
    // forbids two items of one name in one module, so (file, mod_path, fn_name)
    // identifies at most ONE compiled fn — the key that lets a test inventory
    // collapse mutually-exclusive source variants gated by an attribute macro
    // (`#[rustversion::nightly]` / `#[rustversion::not(nightly)]`), which no
    // `#[cfg]` evaluation can decide.
    pub mod_path: String,
    pub has_unimplemented_body: bool, // body is a single `unimplemented!()` or annotated stub
    pub is_test: bool, // fn carries a test attribute (#[test], #[tokio::test], #[rstest], …)
    // Every fn-shaped reference the body spells, macro arguments included
    // (`collect_fn_refs`). Syntax only: the pipeline resolves a reference to
    // a record, and refuses what the spelling does not decide. The tree had
    // no reference data at all, and four Python modules each re-read callees
    // from body text their own way.
    pub refs: Vec<FnRef>,
}

/// One reference a fn body spells: `Q::name(…)` / `Q::name::<T>` (kind
/// `path`, qualifier `Q`), `name(…)` (kind `call`, no qualifier), or
/// `.name(…)` (kind `method`, no qualifier). Sorted and deduplicated.
#[derive(serde::Serialize, Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct FnRef {
    pub kind: String,
    pub qualifier: String,
    pub name: String,
}

#[derive(serde::Serialize, Debug, Clone)]
pub struct TypeRecord {
    pub file: String,
    pub kind: String, // "struct" | "enum" | "type" (alias) | "trait"
    pub name: String,
    pub generics: String,           // "<T, E>" or "" if none
    pub definition: String,         // full source-level definition (sig + body for struct/enum)
    pub fields: Vec<FieldRecord>,   // struct fields (empty for enums and tuples-only structs)
    pub variants: Vec<String>,      // enum variants (name and parenthesised shape)
    pub methods: Vec<MethodRecord>, // names of methods declared on this type
    pub is_pub: bool,
    // Strictly `syn::Visibility::Public` — false for `pub(crate)`/`pub(super)`/
    // `pub(in path)`, which `is_pub` (deliberately) folds into `true` for other
    // callers that just need "any pub keyword, for cross-module skeletonising
    // purposes". A type's TRUE external-API membership requires this to be
    // true (Rust visibility can never be widened past the defining crate by a
    // restricted `pub(...)`, whatever `is_pub` says).
    pub is_exactly_pub: bool,
    // Nested `mod` path from the file root (`"ext"` for `mod ext { pub trait
    // StdError }`). Empty is file scope. A nonempty path is a private-mod
    // wall: the name is not resolvable from another file.
    pub mod_path: String,
    // Every `impl Trait for <this type>` header in the crate, from any file,
    // including partially specialised ones (`impl<C> StdError for
    // ContextError<C, Error>`). Headers only — the line the skeleton keeps
    // after strip, never a body. Without it the type section listed a crate
    // type's fields and methods and no trait impls while the same section's
    // external types did carry theirs, so a fill that needed "does this crate
    // type satisfy this bound" could not reason from absence: run
    // `450977-5f48b68a` (2026-09-21) asked about one impl 14 times in one
    // trace, concluded 9 times it was "omitted from the snippet", and
    // emitted the E0277 body for the fourth run running.
    pub trait_impls: Vec<TraitImplRecord>,
}

/// What `--enumerate-types` emits: the type records, and the crate's impls
/// whose self type is one of the impl's own type parameters (`impl<T: Bound>
/// Trait for T`). Those are not an impl of any one record, so they are one
/// list beside the records rather than a copy on each: on the anyhow stable
/// tree (2026-09-22) three such impls copied onto 30 records were 22 KB of a
/// 72 KB payload.
#[derive(serde::Serialize, Debug, Clone)]
pub struct TypeEnumeration {
    pub types: Vec<TypeRecord>,
    // Sorted by (file, header). Empty when this crate writes none.
    pub blanket_impls: Vec<TraitImplRecord>,
}

#[derive(serde::Serialize, Debug, Clone)]
pub struct TraitImplRecord {
    // The header on one line, as the skeleton prints it, without the body:
    // `impl<C> StdError for ContextError<C, Error> where C: Display`.
    pub header: String,
    pub trait_path: String, // `StdError`, `!Send`, `From<E>`
    pub self_ty: String,    // as written, with generic arguments
    pub generics: String,   // `<C>` or ""
    pub where_clause: String,
    pub file: String,
    pub mod_path: String,
    // The impl block's own cfg predicates, so a cluster-aware renderer can
    // drop an impl its cfg world does not compile — the same shape
    // `MethodRecord::cfg_predicates` carries.
    pub cfg_predicates: Vec<String>,
}

#[derive(serde::Serialize, Debug, Clone)]
pub struct FieldRecord {
    pub name: String,
    pub ty: String,
    pub is_pub: bool,
    // Effective cfg predicates: the enclosing struct's cfgs unioned with the
    // field's own — the same shape `MethodRecord::cfg_predicates` carries for
    // an impl block's items — so a cluster-aware consumer can drop a field
    // the cluster does not compile. Without it the type section listed every
    // field of `ErrorVTable` under `nightly`, including `#[cfg(all(not(
    // error_generic_member_access), feature = "std"))] object_backtrace`,
    // and the `construct:` hint spelled out a seven-field literal the
    // cluster rejects with E0560 (runs `2065433-a1c7219a`, `2131920-86818224`,
    // 2026-09-07: the `impl Error` fill wrote exactly the hinted literal in
    // four of five attempts).
    pub cfg_predicates: Vec<String>,
}

#[derive(serde::Serialize, Debug, Clone)]
pub struct MethodRecord {
    pub name: String,
    pub sig: String, // sig text up to but excluding `{`
    pub is_pub: bool,
    // Strictly `Visibility::Public` — see TypeRecord::is_exactly_pub for why
    // this is a separate field, not a redefinition of `is_pub`.
    pub is_exactly_pub: bool,
    // Effective cfg predicates: the enclosing impl/trait block's cfgs unioned
    // with the method's own. Lets the pipeline drop methods that don't compile
    // in a given cluster's cfg-set (cluster-aware TYPES_IN_SCOPE).
    pub cfg_predicates: Vec<String>,
    // Declaring file and the `mod` path that contains the method. A nonempty
    // `mod_path` in another file is a private-mod wall; trait methods are
    // recorded `is_pub=false`, so filtering on `is_pub` alone cannot drop them.
    pub file: String,
    pub mod_path: String,
}

/// One module declaration in a crate root file (lib.rs or main.rs).
/// `name` is the mod identifier, `file` is the relative path to its source
/// (`<name>.rs` by default, or whatever `#[path = "..."]` points at), and
/// `cfg_predicates` is the list of `#[cfg(...)]` predicates that gate the
/// declaration in the parent file. AST-derived via syn; supersedes any
/// regex-based discovery on the caller side.
#[derive(Debug, Clone, serde::Serialize)]
pub struct ModRecord {
    pub name: String,
    pub file: String,
    pub cfg_predicates: Vec<String>,
    pub declared_in: String,
    // The `mod` declaration's OWN visibility: "public" (`pub mod`), "restricted"
    // (`pub(crate) mod` / `pub(super) mod` / `pub(in path) mod`), or "private"
    // (bare `mod`, the default). A non-"public" mod wall makes every item
    // behind it externally unreachable, however those items are themselves
    // marked — see `module_reachability.py` on the Python side.
    pub vis: String,
}

/// One name a file's TOP-LEVEL `use` declarations bind into that file's scope.
///
/// Needed because a bare type identifier does NOT identify a crate item on its
/// own: `use std::error::Error as StdError;` makes `StdError` mean std's trait
/// in every file that imports it, even when the crate ALSO declares a private
/// `StdError` of its own in some inline module. Resolving references by bare
/// name alone welds unrelated items together; these records let the Python side
/// resolve a name to the module path it actually denotes.
///
/// Only top-level `use`s are emitted: a `use` nested inside an inline `mod`
/// binds names in THAT module's scope, which the file-granular records here
/// cannot represent — emitting it would over-bind the enclosing file.
#[derive(Debug, Clone, serde::Serialize)]
pub struct UseRecord {
    pub file: String,
    /// Ident bound in the file's scope; `"*"` for a glob import.
    pub alias: String,
    /// Path segments as written, terminal included (`crate`/`self`/`super`
    /// prefixes preserved). For a glob this is the globbed module's path.
    pub path: Vec<String>,
    pub is_glob: bool,
}

/// One leaf of a `#[cfg(...)]` condition: a bare ident (`value: None`, e.g.
/// `nightly`) or a `key = "value"` (`feature = "std"` → key `feature`, value
/// `"std"` with quotes, spaces stripped). The Python side counts these to derive
/// compiler clusters (`feature` leaves by feature name, bare leaves by ident).
#[derive(Debug, Clone, serde::Serialize)]
pub struct CfgAtomWire {
    pub key: String,
    pub value: Option<String>,
}

/// One `#[cfg(...)]` / `#[cfg_attr(COND, ...)]` occurrence anywhere in a file
/// (item, field, statement, …), with the leaf atoms of its condition. Emitted by
/// `--enumerate-cfgs`. Replaces the Python regex scan of `#[cfg(...)]` predicates
/// in `task0_cfg_scan` (Hard Rule: no regex on Rust source — use syn).
#[derive(Debug, Clone, serde::Serialize)]
pub struct CfgOccurrence {
    pub file: String,
    pub atoms: Vec<CfgAtomWire>,
}

/// The doc text attached to one item, with the cfg chain that decides whether
/// rustdoc collects the examples in it. Emitted by `--enumerate-doc-items`.
///
/// rustdoc only collects doc tests from items that compile under the build's
/// cfg, so the chain is the item's own `#[cfg]` plus every enclosing impl,
/// trait, inline mod and the file's inner `#![cfg]`. A merged multi-cluster
/// tree keeps one cfg-gated copy of each divergent item per cluster, doc
/// comment included; a text scan counts the examples of every copy.
#[derive(Debug, Clone, serde::Serialize)]
pub struct DocItemRecord {
    pub file: String,
    /// 1-based line of the item's first doc attribute.
    pub line: usize,
    pub cfg_predicates: Vec<String>,
    /// The doc fragments joined by newlines, as rustdoc joins them.
    pub doc: String,
    /// The doc was written inside a macro's token stream. rustdoc numbers
    /// its examples from the item the expansion produces, whose line no
    /// syntactic walk knows, so `line` is not rustdoc's base for this record.
    pub in_macro_body: bool,
}

/// A `const` or `static` item (public-API surface), enumerated by `--enumerate-consts`.
#[derive(Debug, Clone, serde::Serialize)]
pub struct ConstRecord {
    pub file: String,
    pub name: String,
    pub ty: String, // the declared type, rendered
    pub is_pub: bool,
}

/// An autoref-specialization "dispatch family": a set of 2+ sibling traits
/// that declare the SAME method name, where that method name is also invoked
/// (`.method(...)`) from a `macro_rules!` body. anyhow's
/// `AdhocKind`/`TraitKind`/`BoxedKind` (all expose `anyhow_kind`, dispatched by
/// `anyhow!`) is the canonical case; `BothDebug`/`NotBothDebug`
/// (`__dispatch_ensure`, dispatched by `__fancy_ensure`) is another. Dispatch
/// correctness is a cross-impl invariant, so the whole family must be designed
/// in ONE fill prompt.
#[derive(Debug, Clone, serde::Serialize)]
pub struct DispatchFamily {
    pub method: String,
    pub traits: Vec<String>,
    pub macros: Vec<String>,
    pub trait_files: Vec<String>,
    /// Source text of each dispatching `macro_rules!` — the behavioural spec
    /// the co-design fill must satisfy (shows the `.method().new(...)` chain).
    pub macro_defs: Vec<String>,
    /// Return types of the dispatched method across the sibling traits (the
    /// "tag" types — e.g. Adhoc/Trait/Boxed). The macro chains a second call
    /// onto these, so their inherent impls are part of the family surface.
    pub return_types: Vec<String>,
    /// Methods the macro chains onto the dispatched method's result (e.g.
    /// `new`), declared on the return types' inherent impls — NOT on any trait.
    pub chained_methods: Vec<String>,
    /// Files holding the return types' inherent impls (where `chained_methods`
    /// live) — so the fill can pull those blocks into the co-design context.
    pub member_files: Vec<String>,
}
