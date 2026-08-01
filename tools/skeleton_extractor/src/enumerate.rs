//! Enumeration subcommands: fns / types / mods / dispatch families / feature-gate cfgs.
//! The structured records here are the JSON seam payloads. Moved verbatim from lib.rs (slice 7).

use crate::cfg::*;
use crate::error::{ExtractError, Result};
use crate::model::*;
use crate::skeleton::*;
use proc_macro2::TokenStream;
use quote::quote;
use std::path::Path;
use syn::{ImplItem, Item, ItemImpl, TraitItem, parse_file};

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
        is_pub: bool,
        is_exactly_pub: bool,
        sig: &syn::Signature,
        block: &syn::Block,
    ) {
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
                        matches!(
                            item_fn.vis,
                            syn::Visibility::Public(_) | syn::Visibility::Restricted(_)
                        ),
                        matches!(item_fn.vis, syn::Visibility::Public(_)),
                        &item_fn.sig,
                        &item_fn.block,
                    );
                }
                Item::Impl(item_impl) => {
                    let container = render_impl_header(item_impl);
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
                                matches!(
                                    item_fn.vis,
                                    syn::Visibility::Public(_) | syn::Visibility::Restricted(_)
                                ),
                                matches!(item_fn.vis, syn::Visibility::Public(_)),
                                &item_fn.sig,
                                &item_fn.block,
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
                                false,
                                false,
                                &item_fn.sig,
                                block,
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
            id == "test" || id == "rstest" || id == "parameterized" || id == "test_case"
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
// Type records — P5.1: cross-file type definitions for prompt context.
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

pub(crate) fn collect_types_and_methods(
    items: &[Item],
    file_rel: &str,
    out_types: &mut std::collections::BTreeMap<String, TypeRecord>,
) {
    use syn::Fields;
    for item in items {
        match item {
            Item::Struct(s) => {
                let name = s.ident.to_string();
                let generics = render_type(&s.generics).trim().to_string();
                let mut fields = Vec::new();
                match &s.fields {
                    Fields::Named(f) => {
                        for nf in &f.named {
                            fields.push(FieldRecord {
                                name: nf.ident.as_ref().map(|i| i.to_string()).unwrap_or_default(),
                                ty: render_type(&nf.ty).trim().to_string(),
                                is_pub: matches!(nf.vis, syn::Visibility::Public(_)),
                            });
                        }
                    }
                    Fields::Unnamed(f) => {
                        for (i, nf) in f.unnamed.iter().enumerate() {
                            fields.push(FieldRecord {
                                name: i.to_string(),
                                ty: render_type(&nf.ty).trim().to_string(),
                                is_pub: matches!(nf.vis, syn::Visibility::Public(_)),
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
                });
                entry.kind = "struct".to_string();
                entry.file = file_rel.to_string();
                entry.generics = generics;
                entry.definition = definition;
                entry.fields = fields;
                entry.is_pub = is_pub;
                entry.is_exactly_pub = is_exactly_pub;
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
                out_types.entry(name.clone()).or_insert(TypeRecord {
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
                });
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
                out_types.entry(name.clone()).or_insert(TypeRecord {
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
                });
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
                        });
                    }
                }
                out_types.entry(name.clone()).or_insert(TypeRecord {
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
                });
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
                        });
                    }
                }
            }
            Item::Mod(item_mod) => {
                if let Some((_, items)) = &item_mod.content {
                    collect_types_and_methods(items, file_rel, out_types);
                }
            }
            _ => {}
        }
    }
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
        collect_types_and_methods(&ast.items, &rel_str, &mut types);
    }
    Ok(types.into_values().collect())
}

/// Parse a single file and emit one FnRecord per fn item.
pub fn enumerate_fns_in_file(path: &Path, root: &Path) -> Result<Vec<FnRecord>> {
    let source = std::fs::read_to_string(path).map_err(|e| ExtractError::read(path, e))?;
    let ast: syn::File = parse_file(&source).map_err(|e| ExtractError::parse(path, e))?;
    let rel = path
        .strip_prefix(root)
        .map(|p| p.to_string_lossy().to_string())
        .unwrap_or_else(|_| path.to_string_lossy().to_string());
    let mut en = FnEnumerator::new(&source, rel);
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
// Cfg occurrences — P5: replaces the Python regex scan of `#[cfg(...)]` /
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
