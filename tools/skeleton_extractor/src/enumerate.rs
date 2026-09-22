//! Enumeration subcommands: fns / types / mods / dispatch families / feature-gate cfgs.
//! The structured records here are the JSON seam payloads. Moved verbatim from lib.rs.

use crate::cfg::*;
use crate::error::{ExtractError, Result};
use crate::model::*;
use crate::skeleton::*;
use proc_macro2::TokenStream;
use quote::quote;
use std::path::Path;
use syn::{Expr, ImplItem, Item, ItemImpl, Stmt, TraitItem, parse_file};

pub(crate) struct FnEnumerator<'a> {
    source: &'a str,
    file_rel: String,
    cfg_stack: Vec<String>,
    container_stack: Vec<String>,
    records: Vec<FnRecord>,
}

impl<'a> FnEnumerator<'a> {
    fn new(source: &'a str, file_rel: String) -> Self {
        Self {
            source,
            file_rel,
            cfg_stack: Vec::new(),
            container_stack: Vec::new(),
            records: Vec::new(),
        }
    }

    fn record_from_fn(
        &mut self,
        fn_name: &str,
        attrs: &[syn::Attribute],
        vis: &syn::Visibility,
        sig: &syn::Signature,
        block: &syn::Block,
        impl_self_generic: bool,
    ) {
        // Both visibility fields derive from the one syn node; deriving them
        // here keeps the call sites from repeating the same two `matches!`
        // (trait default methods carry no `vis` in syn and pass `Inherited`,
        // which reads false on both — the values they always had).
        let is_pub = matches!(
            vis,
            syn::Visibility::Public(_) | syn::Visibility::Restricted(_)
        );
        let is_exactly_pub = matches!(vis, syn::Visibility::Public(_));
        // sig_byte_start: best-effort using the fn token's span, fallback to
        // the first attribute's span if attrs are present.
        let fn_span = sig.fn_token.span;
        let sig_start = fn_span.byte_range().start;
        // body open/close braces. block.brace_token has a Span covering the
        // full {..} region; we want the inside.
        let brace_span = block.brace_token.span;
        // brace_token.span() returns a Span covering the entire {...} including
        // both braces. We use .join().byte_range() but that's not directly
        // available; instead use the open/close delimited spans.
        let body_open = brace_span.open().byte_range().end; // just past `{`
        let body_close = brace_span.close().byte_range().start; // position of `}`
        let body_byte_start = body_open;
        let body_byte_end = body_close;
        // sig_text: from sig_start to the opening brace
        let sig_text = self
            .source
            .get(sig_start..body_byte_start.saturating_sub(1))
            .unwrap_or("")
            .trim()
            .trim_end_matches('{')
            .trim()
            .to_string();
        let body_text = self
            .source
            .get(body_byte_start..body_byte_end)
            .unwrap_or("")
            .to_string();
        // The fn's own cfg attrs are already on `cfg_stack` (pushed by the caller in
        // `walk_items` before this fn is recorded), alongside any enclosing impl/mod/trait
        // cfgs. Re-adding `extract_cfg_predicates(attrs)` here double-counted the fn's own
        // predicate (e.g. a free `#[cfg(feature="std")] fn` reported it twice). Fixed: trust
        // the stack. `attrs` is still read for the fn's own test-attribute detection.
        let is_test = attrs_have_test(attrs);
        let cfgs = self.cfg_stack.clone();
        let container = self.container_stack.last().cloned().unwrap_or_default();
        self.records.push(FnRecord {
            file: self.file_rel.clone(),
            fn_name: fn_name.to_string(),
            sig_text,
            body_text: body_text.clone(),
            sig_byte_start: sig_start,
            body_byte_start,
            body_byte_end,
            cfg_predicates: cfgs,
            is_pub,
            is_exactly_pub,
            container,
            impl_self_generic,
            mod_path: self
                .container_stack
                .iter()
                .filter_map(|c| c.strip_prefix("mod "))
                .collect::<Vec<_>>()
                .join("::"),
            has_unimplemented_body: body_looks_unimplemented(&body_text),
            is_test,
        });
    }

    fn walk_items(&mut self, items: &[Item]) {
        for item in items {
            self.walk_item(item);
        }
    }

    /// Descend into a `const`/`static` initializer for the items nested in it.
    ///
    /// `const _: () = { … };` is the idiom for scoping trait impls and
    /// compile-time assertions, and the items inside it are ordinary items that
    /// rustc compiles. The skeletoniser already reaches them — it is a
    /// `syn::visit_mut::VisitMut`, which recurses through expressions by
    /// default — so it HOLLOWS bodies there. This walk is hand-rolled and its
    /// catch-all arm used to swallow `Item::Const` whole, so the same fns were
    /// never enumerated.
    ///
    /// The asymmetry made those bodies unaddressable in both directions.
    /// Measured on `tmp/anyhow/src/nightly.rs` (2026-08-06): the file holds
    /// seven fns and `--enumerate-fns` returned FOUR, omitting `Debug::fmt`,
    /// `Display::fmt` and `Error::provide` on the build probe's `MyError` —
    /// all three inside `#[cfg(anyhow_build_probe)] const _: () = { … }`, all
    /// three carrying `::core::unimplemented!()` that the skeletoniser itself
    /// had written (`skeleton.rs`'s stub form; upstream writes bare
    /// `unimplemented!()` in two of them and real code in the third). Across
    /// that whole tree the enumerator reported ZERO hollow bodies.
    ///
    /// Every fill route keys on `FnRecord` — the frontier, the container index,
    /// the attribution reverse-map, the leaf-first ordering — so a fn with no
    /// record is not hard to fill, it is unaddressable; and both completion
    /// stub gates filter extractor records, so such a body could never be
    /// reported either. On `anyhow` the block is cfg-dead and nothing was lost.
    /// On a crate that puts live code in a `const _` block, the pipeline would
    /// strip it, never fill it, and still claim success.
    fn walk_const_initializer(&mut self, expr: &Expr) {
        if let Expr::Block(block) = expr {
            for stmt in &block.block.stmts {
                if let Stmt::Item(item) = stmt {
                    self.walk_item(item);
                }
            }
        }
    }

    fn walk_item(&mut self, item: &Item) {
        {
            // Push cfg from item attrs (so nested fns inherit).
            let item_cfgs = extract_cfg_predicates(item_attrs(item));
            for c in &item_cfgs {
                self.cfg_stack.push(c.clone());
            }
            match item {
                Item::Fn(item_fn) => {
                    self.record_from_fn(
                        &item_fn.sig.ident.to_string(),
                        &item_fn.attrs,
                        &item_fn.vis,
                        &item_fn.sig,
                        &item_fn.block,
                        false,
                    );
                }
                Item::Impl(item_impl) => {
                    let container = render_impl_header(item_impl);
                    let impl_self_generic = impl_self_ty_is_own_generic(item_impl);
                    self.container_stack.push(container);
                    for impl_item in &item_impl.items {
                        let ii_cfgs = extract_cfg_predicates(impl_item_attrs(impl_item));
                        for c in &ii_cfgs {
                            self.cfg_stack.push(c.clone());
                        }
                        if let ImplItem::Fn(item_fn) = impl_item {
                            self.record_from_fn(
                                &item_fn.sig.ident.to_string(),
                                &item_fn.attrs,
                                &item_fn.vis,
                                &item_fn.sig,
                                &item_fn.block,
                                impl_self_generic,
                            );
                        }
                        for _ in &ii_cfgs {
                            self.cfg_stack.pop();
                        }
                    }
                    self.container_stack.pop();
                }
                Item::Trait(item_trait) => {
                    let container = format!("trait {}", item_trait.ident);
                    self.container_stack.push(container);
                    for trait_item in &item_trait.items {
                        let ti_cfgs = extract_cfg_predicates(trait_item_attrs(trait_item));
                        for c in &ti_cfgs {
                            self.cfg_stack.push(c.clone());
                        }
                        if let TraitItem::Fn(item_fn) = trait_item
                            && let Some(block) = &item_fn.default
                        {
                            self.record_from_fn(
                                &item_fn.sig.ident.to_string(),
                                &item_fn.attrs,
                                &syn::Visibility::Inherited,
                                &item_fn.sig,
                                block,
                                false,
                            );
                        }
                        for _ in &ti_cfgs {
                            self.cfg_stack.pop();
                        }
                    }
                    self.container_stack.pop();
                }
                Item::Mod(item_mod) => {
                    if let Some((_, items)) = &item_mod.content {
                        self.container_stack.push(format!("mod {}", item_mod.ident));
                        self.walk_items(items);
                        self.container_stack.pop();
                    }
                }
                // See `walk_const_initializer`: the skeletoniser hollows fns in
                // here, so the enumerator has to be able to name them.
                // See `walk_const_initializer`: the skeletoniser hollows fns in
                // here, so the enumerator has to be able to name them.
                Item::Const(item_const) => {
                    self.walk_const_initializer(&item_const.expr);
                }
                Item::Static(item_static) => {
                    self.walk_const_initializer(&item_static.expr);
                }
                _ => {}
            }
            for _ in &item_cfgs {
                self.cfg_stack.pop();
            }
        }
    }
}

/// True iff any attribute is a test attribute — the set the pipeline's former
/// test-marker regex matched: `#[test]` / `#[tokio::test]` / `#[async_std::test]`
/// (last path segment `test`), `#[rstest]`, `#[parameterized]`, `#[test_case]`,
/// `#[serial_test::serial]` / `#[serial]`.
pub(crate) fn attrs_have_test(attrs: &[syn::Attribute]) -> bool {
    attrs.iter().any(|a| {
        a.path().segments.last().is_some_and(|seg| {
            let id = seg.ident.to_string();
            id == "test"
                || id == "rstest"
                || id == "parameterized"
                || id == "test_case"
                || id == "serial"
        })
    })
}

pub(crate) fn impl_item_attrs(it: &ImplItem) -> &[syn::Attribute] {
    match it {
        ImplItem::Fn(i) => &i.attrs,
        ImplItem::Const(i) => &i.attrs,
        ImplItem::Type(i) => &i.attrs,
        _ => &[],
    }
}

pub(crate) fn trait_item_attrs(it: &TraitItem) -> &[syn::Attribute] {
    match it {
        TraitItem::Fn(i) => &i.attrs,
        TraitItem::Const(i) => &i.attrs,
        TraitItem::Type(i) => &i.attrs,
        _ => &[],
    }
}

/// True iff the impl's self type is one of the impl's OWN generic type
/// parameters — `impl<E> Trait for E`, where a method's `self` is a value the
/// caller chose that this crate has not yet erased. Decided here, on the syn
/// AST, because it is undecidable downstream: `render_impl_header` below
/// deliberately drops the generics list, so `impl Trait for E` prints the
/// same whether `E` is the impl's parameter or a concrete type named `E`.
/// Deliberately strict — a qualified self (`<E as T>::X`), a multi-segment
/// path, or a self type with its own arguments (`Wrapper<E>`) all return
/// false: the consumer (the producer-bypass instruction split) treats false
/// as "undecidable, take the cautious instruction", so a miss costs a missed
/// shortcut, never a wrong claim.
pub(crate) fn impl_self_ty_is_own_generic(item_impl: &ItemImpl) -> bool {
    let syn::Type::Path(tp) = &*item_impl.self_ty else {
        return false;
    };
    if tp.qself.is_some() || tp.path.segments.len() != 1 {
        return false;
    }
    let seg = &tp.path.segments[0];
    if !matches!(seg.arguments, syn::PathArguments::None) {
        return false;
    }
    item_impl
        .generics
        .type_params()
        .any(|p| p.ident == seg.ident)
}

pub(crate) fn render_impl_header(item_impl: &ItemImpl) -> String {
    let ty = &item_impl.self_ty;
    let ty_str = quote::quote!(#ty).to_string();
    let trait_str = item_impl
        .trait_
        .as_ref()
        .map(|(_, path, _)| quote::quote!(#path).to_string())
        .unwrap_or_default();
    if trait_str.is_empty() {
        format!("impl {}", ty_str)
    } else {
        format!("impl {} for {}", trait_str, ty_str)
    }
}

// ---------------------------------------------------------------------------
// Type records — cross-file type definitions for prompt context.
// One record per struct/enum, including its fields and known method names.
// Used by Python to inject relevant type definitions into per-fn prompts
// so the model knows e.g. `Own<T>` has a `.ptr` field (not a `.foo()` method).
// ---------------------------------------------------------------------------

pub(crate) fn render_type<T: quote::ToTokens>(t: &T) -> String {
    quote::quote!(#t).to_string()
}

/// Render a whole item exactly as the skeleton emitter prints it.
/// The private-surface splice locates a frozen definition inside a skeleton
/// file (itself `prettyplease::unparse` output) by whitespace-tolerant text
/// match; a `quote!` token-stream rendering turns `/// doc` into
/// `# [doc = "..."]`, which can never match the printed file, so every
/// doc-commented type silently failed to locate. Same printer ⇒ same tokens.
pub(crate) fn render_item_definition(item: &Item) -> String {
    let file = syn::File {
        shebang: None,
        attrs: Vec::new(),
        items: vec![item.clone()],
    };
    prettyplease::unparse(&file).trim().to_string()
}

/// Visibility of a name over ALL its declarations, never just the one the walk
/// happened to keep.
///
/// Type records are keyed by bare name tree-wide, so a crate that declares a
/// name twice gets ONE record and the walk order decides which. anyhow declares
/// `Chain` as `pub` in lib.rs and `pub(crate)` in chain.rs, and `StdError` as a
/// private trait in lib.rs and a `pub trait` in context.rs — and the same
/// extractor over the upstream crate and over its reconstruction disagreed on
/// both, purely because walkdir reached the files in a different order. That
/// made `validate_task2_api_surface_conformance` report `API drift — 2 of 52
/// pub item(s) missing: struct: Chain; trait: StdError` on every task3 entry of
/// the 2026-08-05 run, accusing the reconstruction of losing public API it had
/// not lost.
///
/// Raising rather than overwriting makes the answer order-independent: the
/// record reports the most public declaration of the name that exists anywhere
/// in the tree. It is still a flat, name-keyed view — a `pub` item inside a
/// private module reads as public here — but both sides of the conformance
/// check now read it the same way, which is what the check compares.
fn raise_visibility(entry: &mut TypeRecord, is_pub: bool, is_exactly_pub: bool) {
    entry.is_pub |= is_pub;
    entry.is_exactly_pub |= is_exactly_pub;
}

pub(crate) fn collect_types_and_methods(
    items: &[Item],
    file_rel: &str,
    mod_path: &str,
    out_types: &mut std::collections::BTreeMap<String, TypeRecord>,
) {
    use syn::Fields;
    for item in items {
        match item {
            Item::Struct(s) => {
                let name = s.ident.to_string();
                let generics = render_type(&s.generics).trim().to_string();
                // A field's effective cfg is the struct's own gate plus the
                // field's: a field of a cfg'd struct exists only where the
                // struct does, exactly as a method of a cfg'd impl block does.
                let struct_cfgs = extract_cfg_predicates(&s.attrs);
                let field_cfgs = |attrs: &[syn::Attribute]| -> Vec<String> {
                    let mut cfgs = struct_cfgs.clone();
                    cfgs.extend(extract_cfg_predicates(attrs));
                    cfgs
                };
                let mut fields = Vec::new();
                match &s.fields {
                    Fields::Named(f) => {
                        for nf in &f.named {
                            fields.push(FieldRecord {
                                name: nf.ident.as_ref().map(|i| i.to_string()).unwrap_or_default(),
                                ty: render_type(&nf.ty).trim().to_string(),
                                is_pub: matches!(nf.vis, syn::Visibility::Public(_)),
                                cfg_predicates: field_cfgs(&nf.attrs),
                            });
                        }
                    }
                    Fields::Unnamed(f) => {
                        for (i, nf) in f.unnamed.iter().enumerate() {
                            fields.push(FieldRecord {
                                name: i.to_string(),
                                ty: render_type(&nf.ty).trim().to_string(),
                                is_pub: matches!(nf.vis, syn::Visibility::Public(_)),
                                cfg_predicates: field_cfgs(&nf.attrs),
                            });
                        }
                    }
                    Fields::Unit => {}
                }
                let is_pub = matches!(
                    s.vis,
                    syn::Visibility::Public(_) | syn::Visibility::Restricted(_)
                );
                let is_exactly_pub = matches!(s.vis, syn::Visibility::Public(_));
                let definition = render_item_definition(item);
                // Update the entry: an earlier impl block may have created a
                // placeholder with empty fields. We always overwrite the
                // struct-level fields (definition, generics, kind, fields).
                let entry = out_types.entry(name.clone()).or_insert_with(|| TypeRecord {
                    file: file_rel.to_string(),
                    kind: "struct".to_string(),
                    name: name.clone(),
                    generics: String::new(),
                    definition: String::new(),
                    fields: Vec::new(),
                    variants: Vec::new(),
                    methods: Vec::new(),
                    is_pub: false,
                    is_exactly_pub: false,
                    mod_path: mod_path.to_string(),
                    trait_impls: Vec::new(),
                });
                entry.kind = "struct".to_string();
                entry.file = file_rel.to_string();
                entry.generics = generics;
                entry.definition = definition;
                entry.fields = fields;
                if entry.mod_path.is_empty() && !mod_path.is_empty() {
                    entry.mod_path = mod_path.to_string();
                }
                raise_visibility(entry, is_pub, is_exactly_pub);
            }
            Item::Enum(e) => {
                let name = e.ident.to_string();
                let generics = render_type(&e.generics).trim().to_string();
                let mut variants = Vec::new();
                for v in &e.variants {
                    variants.push(render_type(v).trim().to_string());
                }
                let is_pub = matches!(
                    e.vis,
                    syn::Visibility::Public(_) | syn::Visibility::Restricted(_)
                );
                let is_exactly_pub = matches!(e.vis, syn::Visibility::Public(_));
                let definition = render_item_definition(item);
                let entry = out_types.entry(name.clone()).or_insert(TypeRecord {
                    file: file_rel.to_string(),
                    kind: "enum".to_string(),
                    name: name.clone(),
                    generics,
                    definition,
                    fields: Vec::new(),
                    variants,
                    methods: Vec::new(),
                    is_pub,
                    is_exactly_pub,
                    mod_path: mod_path.to_string(),
                    trait_impls: Vec::new(),
                });
                raise_visibility(entry, is_pub, is_exactly_pub);
            }
            Item::Type(t) => {
                let name = t.ident.to_string();
                let generics = render_type(&t.generics).trim().to_string();
                let is_pub = matches!(
                    t.vis,
                    syn::Visibility::Public(_) | syn::Visibility::Restricted(_)
                );
                let is_exactly_pub = matches!(t.vis, syn::Visibility::Public(_));
                let definition = render_item_definition(item);
                let entry = out_types.entry(name.clone()).or_insert(TypeRecord {
                    file: file_rel.to_string(),
                    kind: "type".to_string(),
                    name: name.clone(),
                    generics,
                    definition,
                    fields: Vec::new(),
                    variants: Vec::new(),
                    methods: Vec::new(),
                    is_pub,
                    is_exactly_pub,
                    mod_path: mod_path.to_string(),
                    trait_impls: Vec::new(),
                });
                raise_visibility(entry, is_pub, is_exactly_pub);
            }
            Item::Trait(tr) => {
                let name = tr.ident.to_string();
                let generics = render_type(&tr.generics).trim().to_string();
                let is_pub = matches!(
                    tr.vis,
                    syn::Visibility::Public(_) | syn::Visibility::Restricted(_)
                );
                let is_exactly_pub = matches!(tr.vis, syn::Visibility::Public(_));
                let definition = render_item_definition(item);
                let mut methods = Vec::new();
                let tr_cfgs = extract_cfg_predicates(&tr.attrs);
                for ti in &tr.items {
                    if let TraitItem::Fn(itf) = ti {
                        let mut cfgs = tr_cfgs.clone();
                        cfgs.extend(extract_cfg_predicates(trait_item_attrs(ti)));
                        methods.push(MethodRecord {
                            name: itf.sig.ident.to_string(),
                            sig: render_type(&itf.sig).trim().to_string(),
                            is_pub: false,
                            is_exactly_pub: false,
                            cfg_predicates: cfgs,
                            file: file_rel.to_string(),
                            mod_path: mod_path.to_string(),
                        });
                    }
                }
                let entry = out_types.entry(name.clone()).or_insert(TypeRecord {
                    file: file_rel.to_string(),
                    kind: "trait".to_string(),
                    name: name.clone(),
                    generics,
                    definition,
                    fields: Vec::new(),
                    variants: Vec::new(),
                    methods,
                    is_pub,
                    is_exactly_pub,
                    mod_path: mod_path.to_string(),
                    trait_impls: Vec::new(),
                });
                raise_visibility(entry, is_pub, is_exactly_pub);
            }
            Item::Impl(ii) => {
                // Inherent impls (no trait_) attach methods to the self_ty.
                if ii.trait_.is_some() {
                    continue;
                }
                let ty_name = self_ty_name(&ii.self_ty);
                if ty_name.is_empty() {
                    continue;
                }
                let entry = out_types.entry(ty_name.clone()).or_insert(TypeRecord {
                    file: file_rel.to_string(),
                    kind: "struct".to_string(), // we don't know yet; corrected by struct/enum entry if it exists
                    name: ty_name.clone(),
                    generics: String::new(),
                    definition: String::new(),
                    fields: Vec::new(),
                    variants: Vec::new(),
                    methods: Vec::new(),
                    is_pub: false,
                    is_exactly_pub: false,
                    mod_path: mod_path.to_string(),
                    trait_impls: Vec::new(),
                });
                let impl_cfgs = extract_cfg_predicates(&ii.attrs);
                for impl_item in &ii.items {
                    if let ImplItem::Fn(item_fn) = impl_item {
                        let mut cfgs = impl_cfgs.clone();
                        cfgs.extend(extract_cfg_predicates(impl_item_attrs(impl_item)));
                        entry.methods.push(MethodRecord {
                            name: item_fn.sig.ident.to_string(),
                            sig: render_type(&item_fn.sig).trim().to_string(),
                            is_pub: matches!(
                                item_fn.vis,
                                syn::Visibility::Public(_) | syn::Visibility::Restricted(_)
                            ),
                            is_exactly_pub: matches!(item_fn.vis, syn::Visibility::Public(_)),
                            cfg_predicates: cfgs,
                            file: file_rel.to_string(),
                            mod_path: mod_path.to_string(),
                        });
                    }
                }
            }
            Item::Mod(item_mod) => {
                if let Some((_, items)) = &item_mod.content {
                    let child = if mod_path.is_empty() {
                        item_mod.ident.to_string()
                    } else {
                        format!("{}::{}", mod_path, item_mod.ident)
                    };
                    collect_types_and_methods(items, file_rel, &child, out_types);
                }
            }
            _ => {}
        }
    }
}

/// Every trait impl header in `items` (nested inline `mod`s included), keyed
/// by the bare name of its self type. A self type written as a qualified path
/// that does not start in this crate (`std::io::Error`, `fmt::Error`) is a
/// foreign type and is skipped: records are keyed by bare name, and attaching
/// `impl From<X> for io::Error` to the crate's own `Error` would state an impl
/// the crate type does not have — the one lie a list read as complete cannot
/// carry.
pub(crate) fn collect_trait_impls(
    items: &[Item],
    file_rel: &str,
    mod_path: &str,
    out: &mut Vec<(String, TraitImplRecord)>,
) {
    // Traits declared in THIS scope. Inside a nested `mod`, an impl whose
    // trait is a bare name declared beside it implements that local trait,
    // not whatever the crate root means by the name: anyhow's `context.rs`
    // holds `mod ext { pub trait StdError { .. } impl StdError for Error }`,
    // and printing that as `impl StdError for Error` would tell the reader the
    // crate's error type implements the standard trait it does not implement.
    // Such a path is printed qualified by the module (`impl ext::StdError for
    // Error`).
    let local_traits: std::collections::BTreeSet<String> = items
        .iter()
        .filter_map(|it| match it {
            Item::Trait(t) => Some(t.ident.to_string()),
            _ => None,
        })
        .collect();
    for item in items {
        match item {
            Item::Impl(ii) => {
                let Some((_, raw_path, _)) = &ii.trait_ else {
                    continue;
                };
                let mut ii = ii.clone();
                if !mod_path.is_empty()
                    && raw_path.leading_colon.is_none()
                    && raw_path.segments.len() == 1
                    && local_traits.contains(&raw_path.segments[0].ident.to_string())
                {
                    let qualified = format!("{}::{}", mod_path, quote::quote!(#raw_path));
                    if let Ok(p) = syn::parse_str::<syn::Path>(&qualified)
                        && let Some((_, path, _)) = ii.trait_.as_mut()
                    {
                        *path = p;
                    }
                }
                let ii = &ii;
                let Some((bang, path, _)) = &ii.trait_ else {
                    continue;
                };
                let syn::Type::Path(tp) = ii.self_ty.as_ref() else {
                    continue;
                };
                if tp.qself.is_some() {
                    continue;
                }
                let segs: Vec<String> = tp
                    .path
                    .segments
                    .iter()
                    .map(|s| s.ident.to_string())
                    .collect();
                if segs.len() > 1 && !matches!(segs[0].as_str(), "crate" | "self" | "super") {
                    continue;
                }
                let ty_name = self_ty_name(&ii.self_ty);
                if ty_name.is_empty() {
                    continue;
                }
                out.push((
                    ty_name,
                    TraitImplRecord {
                        header: render_trait_impl_header(ii),
                        trait_path: format!(
                            "{}{}",
                            if bang.is_some() { "!" } else { "" },
                            unparse_type_like(path)
                        ),
                        self_ty: unparse_type_like(ii.self_ty.as_ref()),
                        generics: render_generics_parts(&ii.generics).0,
                        where_clause: render_generics_parts(&ii.generics).1,
                        file: file_rel.to_string(),
                        mod_path: mod_path.to_string(),
                        cfg_predicates: extract_cfg_predicates(&ii.attrs),
                    },
                ));
            }
            Item::Mod(item_mod) => {
                if let Some((_, inner)) = &item_mod.content {
                    let child = if mod_path.is_empty() {
                        item_mod.ident.to_string()
                    } else {
                        format!("{}::{}", mod_path, item_mod.ident)
                    };
                    collect_trait_impls(inner, file_rel, &child, out);
                }
            }
            _ => {}
        }
    }
}

/// `(<A, B: Marker>, where A: Clone)` printed by the skeleton's printer, each
/// on one line; empty strings when absent. Printed through a stand-in fn
/// because a bare generics list is not an item prettyplease can print.
fn render_generics_parts(g: &syn::Generics) -> (String, String) {
    let item: syn::ItemFn = syn::parse_quote!(
        fn __x() {}
    );
    let mut item = item;
    item.sig.generics = g.clone();
    let printed = collapse_ws(&render_item_definition(&Item::Fn(item)));
    // `fn __x<A>() where A: Clone {}`
    let rest = printed.trim_start_matches("fn __x");
    let (params, tail) = match rest.find("()") {
        Some(i) => (&rest[..i], &rest[i + 2..]),
        None => ("", rest),
    };
    let tail = tail
        .trim()
        .trim_end_matches("{}")
        .trim()
        .trim_end_matches(',');
    (params.trim().to_string(), tail.to_string())
}

fn collapse_ws(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// A type-like fragment printed by the same printer as the skeleton, so it
/// reads `ContextError<C, Error>` and not `ContextError < C , Error >`.
fn unparse_type_like<T: quote::ToTokens>(t: &T) -> String {
    let src = format!("type __X = {};", quote::quote!(#t));
    match syn::parse_file(&src) {
        Ok(f) => {
            let printed = prettyplease::unparse(&f);
            let body = printed
                .trim()
                .trim_start_matches("type __X = ")
                .trim_end_matches(';');
            collapse_ws(body)
        }
        Err(_) => collapse_ws(&render_type(t)),
    }
}

/// `impl<C> StdError for ContextError<C, Error> where C: Display` — the impl
/// block printed with no attributes and no items, its braces removed, on one
/// line.
fn render_trait_impl_header(ii: &syn::ItemImpl) -> String {
    let mut bare = ii.clone();
    bare.attrs.clear();
    bare.items.clear();
    let printed = render_item_definition(&Item::Impl(bare));
    let head = printed.trim_end();
    let head = head.strip_suffix('}').unwrap_or(head).trim_end();
    let head = head.strip_suffix('{').unwrap_or(head);
    let one = collapse_ws(head);
    one.trim_end_matches(',').to_string()
}

pub(crate) fn self_ty_name(ty: &syn::Type) -> String {
    // Get the bare ident from a possibly-generic type: `Foo<X, Y>` → "Foo".
    if let syn::Type::Path(tp) = ty
        && let Some(seg) = tp.path.segments.last()
    {
        return seg.ident.to_string();
    }
    String::new()
}

/// Walk a directory tree and emit one TypeRecord per (file × struct/enum/type/trait).
pub fn enumerate_types_in_tree(input_dir: &Path) -> Result<Vec<TypeRecord>> {
    let mut types: std::collections::BTreeMap<String, TypeRecord> =
        std::collections::BTreeMap::new();
    let mut trait_impls: Vec<(String, TraitImplRecord)> = Vec::new();
    for entry in walkdir::WalkDir::new(input_dir)
        .into_iter()
        .filter_map(|e| e.ok())
    {
        let p = entry.path();
        if !p.is_file() {
            continue;
        }
        if p.extension().and_then(|s| s.to_str()) != Some("rs") {
            continue;
        }
        let rel = p.strip_prefix(input_dir).unwrap_or(p);
        if rel.components().any(|c| {
            let s = c.as_os_str().to_string_lossy();
            s.starts_with('.') || s == "target"
        }) {
            continue;
        }
        let source = match std::fs::read_to_string(p) {
            Ok(s) => s,
            Err(_) => continue,
        };
        let ast: syn::File = match parse_file(&source) {
            Ok(a) => a,
            Err(_) => continue,
        };
        let rel_str = rel.to_string_lossy().to_string();
        collect_types_and_methods(&ast.items, &rel_str, "", &mut types);
        collect_trait_impls(&ast.items, &rel_str, "", &mut trait_impls);
    }
    attach_trait_impls(&mut types, trait_impls);
    Ok(types.into_values().collect())
}

/// Attach each collected trait impl to the crate type of that name, after the
/// whole tree is walked so file order cannot decide it. Only struct / enum /
/// alias records take impls; a name with no record is a foreign type or a
/// type parameter (`impl<T: Display> Ext for T`) and is dropped. Sorted by
/// (file, header) so the list, and the prompt it renders into, is stable.
pub(crate) fn attach_trait_impls(
    types: &mut std::collections::BTreeMap<String, TypeRecord>,
    impls: Vec<(String, TraitImplRecord)>,
) {
    for (name, rec) in impls {
        if let Some(t) = types.get_mut(&name)
            && t.kind != "trait"
        {
            t.trait_impls.push(rec);
        }
    }
    for t in types.values_mut() {
        t.trait_impls.sort_by(|a, b| {
            (a.file.as_str(), a.header.as_str()).cmp(&(b.file.as_str(), b.header.as_str()))
        });
    }
}

/// Parse a single file and emit one FnRecord per fn item.
pub fn enumerate_fns_in_file(path: &Path, root: &Path) -> Result<Vec<FnRecord>> {
    let source = std::fs::read_to_string(path).map_err(|e| ExtractError::read(path, e))?;
    let rel = path
        .strip_prefix(root)
        .map(|p| p.to_string_lossy().to_string())
        .unwrap_or_else(|_| path.to_string_lossy().to_string());
    let ast: syn::File = parse_file(&source).map_err(|e| ExtractError::parse(path, e))?;
    let mut en = FnEnumerator::new(&source, rel);
    en.walk_items(&ast.items);
    Ok(en.records)
}

/// Same enumeration over source held in memory. Exists so the walk can be
/// exercised on a string — in particular against skeletoniser OUTPUT, which is
/// how the two halves are held to the same set of fns.
pub fn enumerate_fns_in_source(source: &str, rel: &str) -> Result<Vec<FnRecord>> {
    let ast: syn::File = parse_file(source).map_err(|e| ExtractError::parse(Path::new(rel), e))?;
    let mut en = FnEnumerator::new(source, rel.to_string());
    en.walk_items(&ast.items);
    Ok(en.records)
}

/// Walk a directory tree and enumerate all fns across all .rs files.
pub fn enumerate_fns_in_tree(input_dir: &Path) -> Result<Vec<FnRecord>> {
    let mut out = Vec::new();
    // `follow_links` because a per-cluster tree does not own its tests: the
    // pipeline symlinks `tests` into each one (measured 2026-08-01,
    // `.skeleton-filled/{stable,nightly}/tests -> tmp/anyhow/tests`). WalkDir
    // does not follow links by default, so this walk returned the crate's 186
    // src fns and NOT one of the 105 fns in its own tests/ — and the sweep
    // inventory, which indexes test fns by crate-relative path, silently fell
    // back to the marker scan for every tests/ file. That fallback counts but
    // cannot NAME, so the expected-name set came back 24 instead of 110 and
    // the crash localizer lost the vanished names it aims with.
    for entry in walkdir::WalkDir::new(input_dir)
        .follow_links(true)
        .into_iter()
        .filter_map(|e| e.ok())
    {
        let p = entry.path();
        if !p.is_file() {
            continue;
        }
        if p.extension().and_then(|s| s.to_str()) != Some("rs") {
            continue;
        }
        // Skip our own scratch dirs and every cargo artifact root. `target-asan`
        // is a second artifact tree the ASAN sweep builds beside `target`; it is
        // not named `target`, so the old check walked into it and yielded a
        // trybuild scratch source as if it were crate code.
        let rel = p.strip_prefix(input_dir).unwrap_or(p);
        if rel.components().any(|c| {
            let s = c.as_os_str().to_string_lossy();
            s.starts_with('.') || s == "target" || s.starts_with("target-")
        }) {
            continue;
        }
        match enumerate_fns_in_file(p, input_dir) {
            Ok(mut recs) => out.append(&mut recs),
            Err(_e) => continue, // skip files that don't parse (gen_tests etc.)
        }
    }
    Ok(out)
}

pub(crate) fn extract_path_attr(attrs: &[syn::Attribute]) -> Option<String> {
    for a in attrs {
        if a.path().is_ident("path")
            && let syn::Meta::NameValue(nv) = &a.meta
            && let syn::Expr::Lit(el) = &nv.value
            && let syn::Lit::Str(s) = &el.lit
        {
            return Some(s.value());
        }
    }
    None
}

/// Parse the crate-root file (lib.rs or main.rs) and return one ModRecord
/// per top-level `mod foo;` declaration found there. Inline modules
/// (`mod foo { ... }`) are skipped — those don't correspond to a separate
/// file the pipeline needs to gate.
pub fn enumerate_mods_in_file(path: &Path, root: &Path) -> Result<Vec<ModRecord>> {
    let source = std::fs::read_to_string(path).map_err(|e| ExtractError::read(path, e))?;
    let ast: syn::File = parse_file(&source).map_err(|e| ExtractError::parse(path, e))?;
    let declared_in = path
        .strip_prefix(root)
        .map(|p| p.to_string_lossy().to_string())
        .unwrap_or_else(|_| path.to_string_lossy().to_string());
    let mut out = Vec::new();
    for item in &ast.items {
        if let syn::Item::Mod(m) = item {
            if m.content.is_some() {
                continue; // inline module — no separate file
            }
            let name = m.ident.to_string();
            let file = extract_path_attr(&m.attrs).unwrap_or_else(|| format!("{name}.rs"));
            let cfg_predicates = extract_cfg_predicates(&m.attrs);
            let vis = match m.vis {
                syn::Visibility::Public(_) => "public",
                syn::Visibility::Restricted(_) => "restricted",
                syn::Visibility::Inherited => "private",
            };
            out.push(ModRecord {
                name,
                file,
                cfg_predicates,
                declared_in: declared_in.clone(),
                vis: vis.to_string(),
            });
        }
    }
    Ok(out)
}

/// Walk a directory tree and emit ModRecord entries for every external mod
/// declaration in every `.rs` file — not just the crate roots: intermediate
/// files declare submodules too (`mod deep;` in counter.rs), and the pipeline
/// follows the full declaration CHAIN to decide which files are gated by
/// which `#[cfg(...)]` and therefore active under a given cluster. Replaces
/// a regex-based scan that could conflate adjacent mod declarations' cfgs.
pub fn enumerate_mods_in_tree(input_dir: &Path) -> Result<Vec<ModRecord>> {
    let mut out = Vec::new();
    for entry in walkdir::WalkDir::new(input_dir)
        .into_iter()
        .filter_map(|e| e.ok())
    {
        let p = entry.path();
        if !p.is_file() {
            continue;
        }
        let name = match p.file_name().and_then(|s| s.to_str()) {
            Some(n) => n,
            None => continue,
        };
        if !name.ends_with(".rs") {
            continue;
        }
        let rel = p.strip_prefix(input_dir).unwrap_or(p);
        if rel.components().any(|c| {
            let s = c.as_os_str().to_string_lossy();
            s.starts_with('.') || s == "target"
        }) {
            continue;
        }
        match enumerate_mods_in_file(p, input_dir) {
            Ok(mut recs) => out.append(&mut recs),
            Err(_) => continue,
        }
    }
    Ok(out)
}

/// Collect `const` / `static` items (recursing into inline `mod`s) into ConstRecords.
fn collect_consts(items: &[Item], file: &str, out: &mut Vec<ConstRecord>) {
    fn is_pub(vis: &syn::Visibility) -> bool {
        matches!(vis, syn::Visibility::Public(_))
    }
    for item in items {
        match item {
            Item::Const(c) => out.push(ConstRecord {
                file: file.to_string(),
                name: c.ident.to_string(),
                ty: render_type(&c.ty),
                is_pub: is_pub(&c.vis),
            }),
            Item::Static(s) => out.push(ConstRecord {
                file: file.to_string(),
                name: s.ident.to_string(),
                ty: render_type(&s.ty),
                is_pub: is_pub(&s.vis),
            }),
            Item::Mod(m) => {
                if let Some((_, inner)) = &m.content {
                    collect_consts(inner, file, out);
                }
            }
            _ => {}
        }
    }
}

pub fn enumerate_consts_in_file(path: &Path, root: &Path) -> Result<Vec<ConstRecord>> {
    let source = std::fs::read_to_string(path).map_err(|e| ExtractError::read(path, e))?;
    let ast: syn::File = parse_file(&source).map_err(|e| ExtractError::parse(path, e))?;
    let rel = path
        .strip_prefix(root)
        .map(|p| p.to_string_lossy().to_string())
        .unwrap_or_else(|_| path.to_string_lossy().to_string());
    let mut out = Vec::new();
    collect_consts(&ast.items, &rel, &mut out);
    Ok(out)
}

pub fn enumerate_consts_in_tree(input_dir: &Path) -> Result<Vec<ConstRecord>> {
    let mut out = Vec::new();
    for entry in walkdir::WalkDir::new(input_dir)
        .into_iter()
        .filter_map(|e| e.ok())
    {
        let p = entry.path();
        if !p.is_file() {
            continue;
        }
        if p.extension().and_then(|s| s.to_str()) != Some("rs") {
            continue;
        }
        let rel = p.strip_prefix(input_dir).unwrap_or(p);
        if rel.components().any(|c| {
            let s = c.as_os_str().to_string_lossy();
            s.starts_with('.') || s == "target"
        }) {
            continue;
        }
        match enumerate_consts_in_file(p, input_dir) {
            Ok(mut recs) => out.append(&mut recs),
            Err(_e) => continue,
        }
    }
    Ok(out)
}

/// Flatten one `use` tree into the (alias, full path) pairs it binds.
fn collect_use_tree(
    tree: &syn::UseTree,
    prefix: &mut Vec<String>,
    file: &str,
    out: &mut Vec<UseRecord>,
) {
    match tree {
        syn::UseTree::Path(p) => {
            prefix.push(p.ident.to_string());
            collect_use_tree(&p.tree, prefix, file, out);
            prefix.pop();
        }
        syn::UseTree::Name(n) => {
            let mut path = prefix.clone();
            path.push(n.ident.to_string());
            out.push(UseRecord {
                file: file.to_string(),
                alias: n.ident.to_string(),
                path,
                is_glob: false,
            });
        }
        syn::UseTree::Rename(r) => {
            let mut path = prefix.clone();
            path.push(r.ident.to_string());
            out.push(UseRecord {
                file: file.to_string(),
                alias: r.rename.to_string(),
                path,
                is_glob: false,
            });
        }
        syn::UseTree::Glob(_) => out.push(UseRecord {
            file: file.to_string(),
            alias: "*".to_string(),
            path: prefix.clone(),
            is_glob: true,
        }),
        syn::UseTree::Group(g) => {
            for item in &g.items {
                collect_use_tree(item, prefix, file, out);
            }
        }
    }
}

pub fn enumerate_uses_in_file(path: &Path, root: &Path) -> Result<Vec<UseRecord>> {
    let source = std::fs::read_to_string(path).map_err(|e| ExtractError::read(path, e))?;
    let ast: syn::File = parse_file(&source).map_err(|e| ExtractError::parse(path, e))?;
    let rel = path
        .strip_prefix(root)
        .map(|p| p.to_string_lossy().to_string())
        .unwrap_or_else(|_| path.to_string_lossy().to_string());
    let mut out = Vec::new();
    // Top-level only — see UseRecord: a nested `use` binds names in the inline
    // module's scope, not the file's.
    for item in &ast.items {
        if let Item::Use(u) = item {
            let mut prefix = Vec::new();
            collect_use_tree(&u.tree, &mut prefix, &rel, &mut out);
        }
    }
    Ok(out)
}

pub fn enumerate_uses_in_tree(input_dir: &Path) -> Result<Vec<UseRecord>> {
    let mut out = Vec::new();
    for entry in walkdir::WalkDir::new(input_dir)
        .into_iter()
        .filter_map(|e| e.ok())
    {
        let p = entry.path();
        if !p.is_file() {
            continue;
        }
        if p.extension().and_then(|s| s.to_str()) != Some("rs") {
            continue;
        }
        let rel = p.strip_prefix(input_dir).unwrap_or(p);
        if rel.components().any(|c| {
            let s = c.as_os_str().to_string_lossy();
            s.starts_with('.') || s == "target"
        }) {
            continue;
        }
        match enumerate_uses_in_file(p, input_dir) {
            Ok(mut recs) => out.append(&mut recs),
            Err(_e) => continue,
        }
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// Cfg occurrences — replaces the Python regex scan of `#[cfg(...)]` /
// `#[cfg_attr(...)]` predicates in `task0_cfg_scan`. A `syn::visit::Visit` walk
// reaches EVERY attribute (items, fields, variants, statements, inner `#![cfg]`),
// so cluster derivation no longer guesses predicates from line text.
// ---------------------------------------------------------------------------

struct CfgCollector {
    file: String,
    occurrences: Vec<CfgOccurrence>,
}

impl CfgCollector {
    fn handle_attr(&mut self, attr: &syn::Attribute) {
        let mut atoms: Vec<CfgAtomWire> = Vec::new();
        if attr.path().is_ident("cfg") {
            // `#[cfg(COND)]` — meta tokens are the single condition meta.
            if let syn::Meta::List(list) = &attr.meta
                && let Ok(cond) = syn::parse2::<syn::Meta>(list.tokens.clone())
            {
                cfg_meta_atoms(&cond, &mut atoms);
            }
        } else if attr.path().is_ident("cfg_attr") {
            // `#[cfg_attr(COND, applied...)]` — only COND (the first nested meta)
            // gates compilation; the applied attrs do not name clusters.
            if let syn::Meta::List(list) = &attr.meta
                && let Ok(nested) = syn::parse2::<NestedMetaList>(list.tokens.clone())
                && let Some(cond) = nested.items.first()
            {
                cfg_meta_atoms(cond, &mut atoms);
            }
        }
        if !atoms.is_empty() {
            self.occurrences.push(CfgOccurrence {
                file: self.file.clone(),
                atoms,
            });
        }
    }
}

impl<'ast> syn::visit::Visit<'ast> for CfgCollector {
    fn visit_attribute(&mut self, attr: &'ast syn::Attribute) {
        self.handle_attr(attr);
        syn::visit::visit_attribute(self, attr);
    }
}

/// Walk a tree and emit one [`CfgOccurrence`] per `#[cfg(...)]` /
/// `#[cfg_attr(COND, ...)]` attribute, with the leaf atoms of its condition.
pub fn enumerate_cfgs_in_tree(input_dir: &Path) -> Result<Vec<CfgOccurrence>> {
    let mut out = Vec::new();
    for entry in walkdir::WalkDir::new(input_dir)
        .into_iter()
        .filter_map(|e| e.ok())
    {
        let p = entry.path();
        if !p.is_file() || p.extension().and_then(|s| s.to_str()) != Some("rs") {
            continue;
        }
        let rel = p.strip_prefix(input_dir).unwrap_or(p);
        if rel.components().any(|c| {
            let s = c.as_os_str().to_string_lossy();
            s.starts_with('.') || s == "target"
        }) {
            continue;
        }
        let Ok(source) = std::fs::read_to_string(p) else {
            continue;
        };
        let Ok(ast) = parse_file(&source) else {
            continue;
        };
        let mut c = CfgCollector {
            file: rel.to_string_lossy().to_string(),
            occurrences: Vec::new(),
        };
        syn::visit::Visit::visit_file(&mut c, &ast);
        out.append(&mut c.occurrences);
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// Doc items — the doc text rustdoc collects examples from, per item, with the
// item's accumulated cfg chain. Walks the places rustdoc documents (items,
// impl/trait members, fields, variants, foreign items, inline mods) and never
// enters a fn body or a const initializer, which rustdoc does not document.
// ---------------------------------------------------------------------------

struct DocCollector {
    file: String,
    cfg_stack: Vec<String>,
    out: Vec<DocItemRecord>,
}

fn doc_fragments(attrs: &[syn::Attribute]) -> Option<(String, usize)> {
    use syn::spanned::Spanned;
    let mut parts: Vec<String> = Vec::new();
    let mut line = 0;
    for attr in attrs {
        if !attr.path().is_ident("doc") {
            continue;
        }
        if let syn::Meta::NameValue(nv) = &attr.meta
            && let Expr::Lit(syn::ExprLit {
                lit: syn::Lit::Str(s),
                ..
            }) = &nv.value
        {
            if parts.is_empty() {
                line = attr.span().start().line;
            }
            parts.push(s.value());
        }
    }
    if parts.is_empty() {
        None
    } else {
        Some((parts.join("\n"), line))
    }
}

impl DocCollector {
    /// Record `attrs`' doc text under the current chain plus `attrs`' own cfg,
    /// then run `inner` with that cfg still pushed.
    fn scoped(&mut self, attrs: &[syn::Attribute], inner: impl FnOnce(&mut Self)) {
        let own = extract_cfg_predicates(attrs);
        let pushed = own.len();
        self.cfg_stack.extend(own);
        if let Some((doc, line)) = doc_fragments(attrs) {
            self.out.push(DocItemRecord {
                file: self.file.clone(),
                line,
                cfg_predicates: self.cfg_stack.clone(),
                doc,
            });
        }
        inner(self);
        self.cfg_stack.truncate(self.cfg_stack.len() - pushed);
    }

    /// Doc attributes written inside a macro's token stream (a `macro_rules!`
    /// body, an item-position invocation) document whatever the expansion
    /// produces, which no syntactic walk can evaluate. Each contiguous run of
    /// `#[doc = "…"]` tokens is recorded under the macro item's own chain, the
    /// same once-per-occurrence count a text scan gives it: anyhow's
    /// `__ensure!` carries the two `ensure!` examples this way, and rustdoc
    /// runs both.
    fn macro_tokens(&mut self, tokens: &TokenStream) {
        use proc_macro2::{Delimiter, TokenTree};
        let trees: Vec<TokenTree> = tokens.clone().into_iter().collect();
        let mut run: Vec<String> = Vec::new();
        let mut run_line = 0;
        let mut i = 0;
        while i < trees.len() {
            let doc = match (&trees[i], trees.get(i + 1)) {
                (TokenTree::Punct(p), Some(TokenTree::Group(g)))
                    if p.as_char() == '#' && g.delimiter() == Delimiter::Bracket =>
                {
                    syn::parse2::<syn::MetaNameValue>(g.stream())
                        .ok()
                        .filter(|nv| nv.path.is_ident("doc"))
                        .and_then(|nv| match nv.value {
                            Expr::Lit(syn::ExprLit {
                                lit: syn::Lit::Str(s),
                                ..
                            }) => Some((s.value(), p.span().start().line)),
                            _ => None,
                        })
                }
                _ => None,
            };
            if let Some((text, line)) = doc {
                if run.is_empty() {
                    run_line = line;
                }
                run.push(text);
                i += 2;
                continue;
            }
            self.flush_macro_run(&mut run, run_line);
            if let TokenTree::Group(g) = &trees[i] {
                self.macro_tokens(&g.stream());
            }
            i += 1;
        }
        self.flush_macro_run(&mut run, run_line);
    }

    fn flush_macro_run(&mut self, run: &mut Vec<String>, line: usize) {
        if run.is_empty() {
            return;
        }
        self.out.push(DocItemRecord {
            file: self.file.clone(),
            line,
            cfg_predicates: self.cfg_stack.clone(),
            doc: run.join("\n"),
        });
        run.clear();
    }

    fn fields(&mut self, fields: &syn::Fields) {
        for f in fields {
            self.scoped(&f.attrs, |_| {});
        }
    }

    fn walk_items(&mut self, items: &[Item]) {
        for item in items {
            self.walk_item(item);
        }
    }

    fn walk_item(&mut self, item: &Item) {
        // `ItemMod.attrs` carries the inline mod's inner `#![doc]`/`#![cfg]` too.
        self.scoped(item_attrs(item), |s| match item {
            Item::Impl(i) => {
                for ii in &i.items {
                    let attrs: &[syn::Attribute] = match ii {
                        ImplItem::Fn(x) => &x.attrs,
                        ImplItem::Const(x) => &x.attrs,
                        ImplItem::Type(x) => &x.attrs,
                        ImplItem::Macro(x) => &x.attrs,
                        _ => &[],
                    };
                    s.scoped(attrs, |s| {
                        if let ImplItem::Macro(m) = ii {
                            s.macro_tokens(&m.mac.tokens);
                        }
                    });
                }
            }
            Item::Trait(t) => {
                for ti in &t.items {
                    let attrs: &[syn::Attribute] = match ti {
                        TraitItem::Fn(x) => &x.attrs,
                        TraitItem::Const(x) => &x.attrs,
                        TraitItem::Type(x) => &x.attrs,
                        TraitItem::Macro(x) => &x.attrs,
                        _ => &[],
                    };
                    s.scoped(attrs, |s| {
                        if let TraitItem::Macro(m) = ti {
                            s.macro_tokens(&m.mac.tokens);
                        }
                    });
                }
            }
            Item::Mod(m) => {
                if let Some((_, items)) = &m.content {
                    s.walk_items(items);
                }
            }
            Item::Macro(m) => s.macro_tokens(&m.mac.tokens),
            Item::Struct(st) => s.fields(&st.fields),
            Item::Union(u) => {
                for f in &u.fields.named {
                    s.scoped(&f.attrs, |_| {});
                }
            }
            Item::Enum(e) => {
                for v in &e.variants {
                    s.scoped(&v.attrs, |s| s.fields(&v.fields));
                }
            }
            Item::ForeignMod(fm) => {
                for fi in &fm.items {
                    let attrs: &[syn::Attribute] = match fi {
                        syn::ForeignItem::Fn(x) => &x.attrs,
                        syn::ForeignItem::Static(x) => &x.attrs,
                        syn::ForeignItem::Type(x) => &x.attrs,
                        syn::ForeignItem::Macro(x) => &x.attrs,
                        _ => &[],
                    };
                    s.scoped(attrs, |_| {});
                }
            }
            _ => {}
        });
    }
}

/// Walk a tree and emit one [`DocItemRecord`] per item that carries doc text,
/// with the item's accumulated cfg chain. The file's inner attributes (`//!`,
/// `#![cfg]`) scope the whole file.
pub fn enumerate_doc_items_in_tree(input_dir: &Path) -> Result<Vec<DocItemRecord>> {
    let mut out = Vec::new();
    for entry in walkdir::WalkDir::new(input_dir)
        .into_iter()
        .filter_map(|e| e.ok())
    {
        let p = entry.path();
        if !p.is_file() || p.extension().and_then(|s| s.to_str()) != Some("rs") {
            continue;
        }
        let rel = p.strip_prefix(input_dir).unwrap_or(p);
        if rel.components().any(|c| {
            let s = c.as_os_str().to_string_lossy();
            s.starts_with('.') || s == "target"
        }) {
            continue;
        }
        let Ok(source) = std::fs::read_to_string(p) else {
            continue;
        };
        let Ok(ast) = parse_file(&source) else {
            continue;
        };
        let mut c = DocCollector {
            file: rel.to_string_lossy().to_string(),
            cfg_stack: Vec::new(),
            out: Vec::new(),
        };
        c.scoped(&ast.attrs, |s| s.walk_items(&ast.items));
        out.append(&mut c.out);
    }
    Ok(out)
}

/// Walk a macro body's token stream and collect every method name invoked as
/// `.method(...)` — i.e. a `.` punct, then an ident, then a parenthesised
/// group. Recurses into all delimiter groups. AST/token walk, no regex.
pub(crate) fn collect_method_calls(ts: TokenStream, out: &mut std::collections::BTreeSet<String>) {
    use proc_macro2::{Delimiter, TokenTree};
    let trees: Vec<TokenTree> = ts.into_iter().collect();
    for (i, tt) in trees.iter().enumerate() {
        if let TokenTree::Punct(p) = tt
            && p.as_char() == '.'
            && let (Some(TokenTree::Ident(id)), Some(TokenTree::Group(g))) =
                (trees.get(i + 1), trees.get(i + 2))
            && g.delimiter() == Delimiter::Parenthesis
        {
            out.insert(id.to_string());
        }
        if let TokenTree::Group(g) = tt {
            collect_method_calls(g.stream(), out);
        }
    }
}

/// Walk a macro body and collect CHAINED calls `.first().second(...)` — i.e.
/// `.` Ident `()` `.` Ident `(...)`. Records (first, second) so the dispatch
/// chain `x.anyhow_kind().new(arg)` yields ("anyhow_kind", "new"). The second
/// hop is invoked on the RETURN TYPE of the first, so it lives on that type's
/// inherent impl, not on a sibling trait — which is why it is invisible to the
/// trait-only scan. Recurses into all groups. AST/token walk, no regex.
pub(crate) fn collect_method_chains(
    ts: TokenStream,
    out: &mut std::collections::BTreeSet<(String, String)>,
) {
    use proc_macro2::{Delimiter, TokenTree};
    let trees: Vec<TokenTree> = ts.into_iter().collect();
    for (i, tt) in trees.iter().enumerate() {
        if let TokenTree::Punct(p) = tt
            && p.as_char() == '.'
        {
            // .first ( ) . second ( ...
            if let (
                Some(TokenTree::Ident(first)),
                Some(TokenTree::Group(g1)),
                Some(TokenTree::Punct(dot2)),
                Some(TokenTree::Ident(second)),
                Some(TokenTree::Group(g2)),
            ) = (
                trees.get(i + 1),
                trees.get(i + 2),
                trees.get(i + 3),
                trees.get(i + 4),
                trees.get(i + 5),
            ) && g1.delimiter() == Delimiter::Parenthesis
                && dot2.as_char() == '.'
                && g2.delimiter() == Delimiter::Parenthesis
            {
                out.insert((first.to_string(), second.to_string()));
            }
        }
        if let TokenTree::Group(g) = tt {
            collect_method_chains(g.stream(), out);
        }
    }
}

/// Name of a fn's return type, if it is a simple named path type — the last
/// path segment's ident (e.g. `-> Adhoc` → "Adhoc", `-> kind::Trait` →
/// "Trait"). `None` for `-> Self`, references, tuples, generics, or `()`.
pub(crate) fn return_type_name(output: &syn::ReturnType) -> Option<String> {
    if let syn::ReturnType::Type(_, ty) = output
        && let syn::Type::Path(tp) = &**ty
        && let Some(seg) = tp.path.segments.last()
    {
        let id = seg.ident.to_string();
        if id != "Self" {
            return Some(id);
        }
    }
    None
}

/// Recursively collect, from a list of items (descending into inline modules):
///   - method name -> set of traits declaring it
///   - trait name  -> set of files it appears in
///   - method name -> set of `macro_rules!` macros that invoke `.method(`
pub(crate) struct FamilyScan {
    /// method name -> traits declaring it
    method_traits: std::collections::BTreeMap<String, std::collections::BTreeSet<String>>,
    /// trait name -> files it appears in
    trait_files: std::collections::BTreeMap<String, std::collections::BTreeSet<String>>,
    /// method name -> macros invoking `.method(`
    method_macros: std::collections::BTreeMap<String, std::collections::BTreeSet<String>>,
    /// macro name -> its source text
    macro_src: std::collections::BTreeMap<String, String>,
    /// trait-method name -> return type names (across all declaring traits)
    method_return_types: std::collections::BTreeMap<String, std::collections::BTreeSet<String>>,
    /// chained calls (first, second) seen in any macro body
    macro_chains: std::collections::BTreeSet<(String, String)>,
    /// type name (of an inherent `impl Type`) -> methods it declares
    type_methods: std::collections::BTreeMap<String, std::collections::BTreeSet<String>>,
    /// type name -> files its inherent impls appear in
    type_files: std::collections::BTreeMap<String, std::collections::BTreeSet<String>>,
}

impl FamilyScan {
    fn new() -> Self {
        FamilyScan {
            method_traits: Default::default(),
            trait_files: Default::default(),
            method_macros: Default::default(),
            macro_src: Default::default(),
            method_return_types: Default::default(),
            macro_chains: Default::default(),
            type_methods: Default::default(),
            type_files: Default::default(),
        }
    }
}

pub(crate) fn collect_traits_and_macros(items: &[Item], file: &str, s: &mut FamilyScan) {
    for it in items {
        match it {
            Item::Trait(t) => {
                let tname = t.ident.to_string();
                for ti in &t.items {
                    if let TraitItem::Fn(f) = ti {
                        let m = f.sig.ident.to_string();
                        s.method_traits
                            .entry(m.clone())
                            .or_default()
                            .insert(tname.clone());
                        s.trait_files
                            .entry(tname.clone())
                            .or_default()
                            .insert(file.to_string());
                        if let Some(rt) = return_type_name(&f.sig.output) {
                            s.method_return_types.entry(m).or_default().insert(rt);
                        }
                    }
                }
            }
            Item::Impl(i) if i.trait_.is_none() => {
                // Inherent `impl Type { ... }` — record its methods so the
                // chained call (e.g. tag `new`) can be attached to the type.
                if let syn::Type::Path(tp) = &*i.self_ty
                    && let Some(seg) = tp.path.segments.last()
                {
                    let tyname = seg.ident.to_string();
                    for ii in &i.items {
                        if let ImplItem::Fn(f) = ii {
                            s.type_methods
                                .entry(tyname.clone())
                                .or_default()
                                .insert(f.sig.ident.to_string());
                        }
                    }
                    s.type_files
                        .entry(tyname)
                        .or_default()
                        .insert(file.to_string());
                }
            }
            Item::Macro(m) => {
                if m.mac.path.is_ident("macro_rules")
                    && let Some(id) = &m.ident
                {
                    let mut calls = std::collections::BTreeSet::new();
                    collect_method_calls(m.mac.tokens.clone(), &mut calls);
                    for c in calls {
                        s.method_macros.entry(c).or_default().insert(id.to_string());
                    }
                    collect_method_chains(m.mac.tokens.clone(), &mut s.macro_chains);
                    s.macro_src
                        .entry(id.to_string())
                        .or_insert_with(|| quote!(#m).to_string());
                }
            }
            Item::Mod(md) => {
                if let Some((_, inner)) = &md.content {
                    collect_traits_and_macros(inner, file, s);
                }
            }
            _ => {}
        }
    }
}

/// Walk a tree and return every dispatch family (see `DispatchFamily`). A
/// shared-method trait set is only a family when a macro actually dispatches
/// it — that gate excludes incidental shared names (e.g. std's `source`).
pub fn enumerate_dispatch_families_in_tree(input_dir: &Path) -> Result<Vec<DispatchFamily>> {
    use std::collections::BTreeSet;
    let mut s = FamilyScan::new();

    for entry in walkdir::WalkDir::new(input_dir)
        .into_iter()
        .filter_map(|e| e.ok())
    {
        let p = entry.path();
        if !p.is_file() || p.extension().and_then(|s| s.to_str()) != Some("rs") {
            continue;
        }
        let rel = p.strip_prefix(input_dir).unwrap_or(p);
        if rel.components().any(|c| {
            let s = c.as_os_str().to_string_lossy();
            s.starts_with('.') || s == "target"
        }) {
            continue;
        }
        let source = match std::fs::read_to_string(p) {
            Ok(s) => s,
            Err(_) => continue,
        };
        let ast = match parse_file(&source) {
            Ok(a) => a,
            Err(_) => continue,
        };
        let file = rel.to_string_lossy().to_string();
        collect_traits_and_macros(&ast.items, &file, &mut s);
    }

    let mut out = Vec::new();
    for (method, traits) in &s.method_traits {
        if traits.len() < 2 {
            continue;
        }
        let macros = s.method_macros.get(method).cloned().unwrap_or_default();
        if macros.is_empty() {
            continue; // shared name but no macro dispatch — not a family
        }
        let mut tfiles: BTreeSet<String> = BTreeSet::new();
        for t in traits {
            if let Some(fs) = s.trait_files.get(t) {
                tfiles.extend(fs.iter().cloned());
            }
        }
        let macro_defs: Vec<String> = macros
            .iter()
            .filter_map(|m| s.macro_src.get(m).cloned())
            .collect();

        // Chain-following: the dispatched method returns tag types;
        // the macro calls a second method on that result, which lives on the
        // tag's inherent impl (not a trait). Pull those types + their chained
        // methods + files into the family surface so co-design sees the WHOLE
        // `.method().chained()` path, not just the trivial first hop.
        let return_types: BTreeSet<String> = s
            .method_return_types
            .get(method)
            .cloned()
            .unwrap_or_default();
        let mut chained_methods: BTreeSet<String> = BTreeSet::new();
        let mut member_files: BTreeSet<String> = BTreeSet::new();
        for (first, second) in &s.macro_chains {
            if first != method {
                continue;
            }
            // keep only chained methods that actually exist on a return type
            for rt in &return_types {
                if s.type_methods.get(rt).is_some_and(|ms| ms.contains(second)) {
                    chained_methods.insert(second.clone());
                    if let Some(fs) = s.type_files.get(rt) {
                        member_files.extend(fs.iter().cloned());
                    }
                }
            }
        }

        out.push(DispatchFamily {
            method: method.clone(),
            traits: traits.iter().cloned().collect(),
            macros: macros.iter().cloned().collect(),
            trait_files: tfiles.into_iter().collect(),
            macro_defs,
            return_types: return_types.into_iter().collect(),
            chained_methods: chained_methods.into_iter().collect(),
            member_files: member_files.into_iter().collect(),
        });
    }
    Ok(out)
}

/// If `attr` is `#[cfg_attr(COND, ... feature(...) ...)]`, record COND's leaf
/// cfg predicates — i.e. the cfgs that, when active, enable a nightly
/// `#![feature(...)]`. Such a cluster must compile with `+nightly`.
pub(crate) fn collect_feature_gate_cfgs_from_attr(
    attr: &syn::Attribute,
    out: &mut std::collections::BTreeSet<String>,
) {
    if !attr.path().is_ident("cfg_attr") {
        return;
    }
    let syn::Meta::List(list) = &attr.meta else {
        return;
    };
    let Ok(nested) = syn::parse2::<NestedMetaList>(list.tokens.clone()) else {
        return;
    };
    let mut it = nested.items.iter();
    let Some(cond) = it.next() else { return };
    // The remaining metas are the attributes applied when COND holds.
    let enables_feature = it.any(|m| {
        matches!(
            m, syn::Meta::List(l) if l.path.is_ident("feature")
        )
    });
    if enables_feature {
        cfg_meta_leaves(cond, out);
    }
}

pub(crate) fn scan_items_for_feature_gates(
    items: &[Item],
    out: &mut std::collections::BTreeSet<String>,
) {
    for it in items {
        for attr in item_attrs(it) {
            collect_feature_gate_cfgs_from_attr(attr, out);
        }
        if let Item::Mod(m) = it
            && let Some((_, inner)) = &m.content
        {
            scan_items_for_feature_gates(inner, out);
        }
    }
}

/// Walk a tree and return the set of cfg predicates that gate a nightly
/// `#![feature(...)]` attribute (via `#[cfg_attr(COND, feature(...))]`). A
/// compiler cluster whose active cfg-set intersects this needs `+nightly`.
pub fn enumerate_feature_gate_cfgs_in_tree(input_dir: &Path) -> Result<Vec<String>> {
    use std::collections::BTreeSet;
    let mut out: BTreeSet<String> = BTreeSet::new();
    for entry in walkdir::WalkDir::new(input_dir)
        .into_iter()
        .filter_map(|e| e.ok())
    {
        let p = entry.path();
        if !p.is_file() || p.extension().and_then(|s| s.to_str()) != Some("rs") {
            continue;
        }
        let rel = p.strip_prefix(input_dir).unwrap_or(p);
        if rel.components().any(|c| {
            let s = c.as_os_str().to_string_lossy();
            s.starts_with('.') || s == "target"
        }) {
            continue;
        }
        let Ok(source) = std::fs::read_to_string(p) else {
            continue;
        };
        let Ok(ast) = parse_file(&source) else {
            continue;
        };
        for attr in &ast.attrs {
            collect_feature_gate_cfgs_from_attr(attr, &mut out);
        }
        scan_items_for_feature_gates(&ast.items, &mut out);
    }
    Ok(out.into_iter().collect())
}
