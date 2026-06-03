//! Serde record types — the JSON seam to the Python side (CODE_ARCHITECTURE.md §4).
//!
//! These `serde::Serialize` structs are the wire contract enumerated by the CLI subcommands
//! and parsed by `pipeline_core.domain.models`. Moved verbatim from `lib.rs` (slice 7); the
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
    pub container: String, // "" for free fn, "impl X for Y" for inherent/trait impl, "trait T" for trait method
    pub has_unimplemented_body: bool, // body is a single `unimplemented!()` or annotated stub
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
}

#[derive(serde::Serialize, Debug, Clone)]
pub struct FieldRecord {
    pub name: String,
    pub ty: String,
    pub is_pub: bool,
}

#[derive(serde::Serialize, Debug, Clone)]
pub struct MethodRecord {
    pub name: String,
    pub sig: String, // sig text up to but excluding `{`
    pub is_pub: bool,
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
