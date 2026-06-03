//! Cross-cluster AST merge (syn + prettyplease). Moved verbatim from lib.rs (slice 7).

use crate::error::{ExtractError, Result};
use proc_macro2::TokenStream;
use quote::quote;
use syn::parse_file;

// ---------------------------------------------------------------------------
// AST-level cluster merge.
//
// The pipeline runs Phase C (per-fn fill) independently inside each compiler
// cluster's filled tree (`.skeleton-filled/{cluster}/src/`). After all
// clusters finish, the per-cluster outputs must be composed into a single
// `project_dir/src/` tree that compiles correctly under EACH cluster's cfg
// flags (TASK 3 runs cargo test against this merged tree).
//
// The old textual merger (regex-based item splicing in pipeline.py) breaks
// down on nested items: e.g. when a trait method's declaration is identical
// across clusters but its impl-block body diverges, the textual splice
// emitted both body variants outside their impl block. This module replaces
// that with a syn-driven recursive merge:
//
//   1. Walk top-level items in each cluster's AST.
//   2. Pair items by identity key (kind + name + sig hash).
//   3. Items identical across all clusters → emit once.
//   4. Items diverging or cluster-exclusive → emit each variant with that
//      cluster's cfg gate attached.
//   5. For Trait / Impl / Mod items: recurse into their inner items BEFORE
//      step 3 — the body's merged result becomes the outer item's new body.
//
// Round-tripped via prettyplease for stable formatting.
// ---------------------------------------------------------------------------

use std::collections::BTreeMap;

/// One cluster's view of a file plus the cfg expression that gates the
/// cluster (e.g. "all(not(error_generic_member_access), not(anyhow_no_core_error))"
/// for stable).
pub struct ClusterFile {
    pub name: String,
    pub source: String,
    pub cfg_gate: String, // raw predicate body, NOT wrapped in #[cfg(...)]
}

impl ClusterFile {
    fn ast(&self) -> Result<syn::File> {
        parse_file(&self.source).map_err(|e| ExtractError::parse_named(&self.name, e))
    }
}

/// Identity key for an item — items with the same key from different
/// clusters are considered the "same" item and get merged. Items with
/// different keys are emitted as separate, gated entries.
pub(crate) type ItemKey = (String, String);

pub(crate) fn token_hash(ts: &TokenStream) -> String {
    // Stable string repr of a token stream, normalized by syn re-rendering.
    // Two ASTs that produce the same token stream produce equal hashes.
    ts.to_string()
}

pub(crate) fn item_key(item: &syn::Item) -> ItemKey {
    match item {
        syn::Item::Fn(f) => ("fn".to_string(), f.sig.ident.to_string()),
        syn::Item::Struct(s) => ("struct".to_string(), s.ident.to_string()),
        syn::Item::Enum(e) => ("enum".to_string(), e.ident.to_string()),
        syn::Item::Union(u) => ("union".to_string(), u.ident.to_string()),
        syn::Item::Trait(t) => ("trait".to_string(), t.ident.to_string()),
        syn::Item::TraitAlias(t) => ("trait_alias".to_string(), t.ident.to_string()),
        syn::Item::Type(t) => ("type".to_string(), t.ident.to_string()),
        syn::Item::Const(c) => ("const".to_string(), c.ident.to_string()),
        syn::Item::Static(s) => ("static".to_string(), s.ident.to_string()),
        syn::Item::Mod(m) => ("mod".to_string(), m.ident.to_string()),
        syn::Item::Macro(m) => {
            let name = m
                .ident
                .as_ref()
                .map(|i| i.to_string())
                .unwrap_or_else(|| token_hash(&m.mac.tokens));
            ("macro".to_string(), name)
        }
        syn::Item::Impl(i) => {
            // Identity = the FULL impl header (FR-025): impl generics, the
            // trait ref WITH its generic args, the self-type WITH its
            // generics, and the where-clause. `path_to_string` is NOT used
            // here because it drops generic arguments, which would collapse
            // `impl AsRef<dyn StdError>` and
            // `impl AsRef<dyn StdError + Send + Sync>` (and likewise impls
            // that differ only by their where-clause) into one merged block.
            let generics = &i.generics;
            let where_clause = &i.generics.where_clause;
            let self_ty = &*i.self_ty;
            let trait_part = i
                .trait_
                .as_ref()
                .map(|(bang, p, _)| quote!(#bang #p).to_string())
                .unwrap_or_default();
            let header = format!(
                "{}|{}|{}|{}",
                quote!(#generics),
                trait_part,
                quote!(#self_ty),
                quote!(#where_clause),
            );
            ("impl".to_string(), header)
        }
        syn::Item::Use(u) => {
            // Each `use` is unique by its full path.
            ("use".to_string(), quote!(#u).to_string())
        }
        syn::Item::ExternCrate(e) => ("extern_crate".to_string(), e.ident.to_string()),
        syn::Item::ForeignMod(_) | syn::Item::Verbatim(_) => {
            // Identify verbatim/foreign blocks by their full content.
            ("verbatim".to_string(), quote!(#item).to_string())
        }
        _ => ("other".to_string(), quote!(#item).to_string()),
    }
}

/// Identity key for items inside a trait body (associated items).
pub(crate) fn trait_item_key(item: &syn::TraitItem) -> ItemKey {
    match item {
        syn::TraitItem::Fn(f) => ("trait_fn".to_string(), f.sig.ident.to_string()),
        syn::TraitItem::Const(c) => ("trait_const".to_string(), c.ident.to_string()),
        syn::TraitItem::Type(t) => ("trait_type".to_string(), t.ident.to_string()),
        syn::TraitItem::Macro(m) => ("trait_macro".to_string(), quote!(#m).to_string()),
        _ => ("trait_other".to_string(), quote!(#item).to_string()),
    }
}

/// Identity key for items inside an impl body.
pub(crate) fn impl_item_key(item: &syn::ImplItem) -> ItemKey {
    match item {
        syn::ImplItem::Fn(f) => ("impl_fn".to_string(), f.sig.ident.to_string()),
        syn::ImplItem::Const(c) => ("impl_const".to_string(), c.ident.to_string()),
        syn::ImplItem::Type(t) => ("impl_type".to_string(), t.ident.to_string()),
        syn::ImplItem::Macro(m) => ("impl_macro".to_string(), quote!(#m).to_string()),
        _ => ("impl_other".to_string(), quote!(#item).to_string()),
    }
}

/// Parse a cfg-predicate string ("not(foo), feature = \"bar\"") into a
/// syn::Attribute as `#[cfg(<predicate>)]`. Returns None if the predicate
/// is empty (no gating needed).
pub(crate) fn cfg_attr_from_predicate(pred: &str) -> Option<syn::Attribute> {
    let trimmed = pred.trim();
    if trimmed.is_empty() {
        return None;
    }
    let src = format!("#[cfg({})]", trimmed);
    let attrs: syn::File = match parse_file(&format!("{src}\nfn __probe(){{}}")) {
        Ok(f) => f,
        Err(_) => return None,
    };
    if let Some(syn::Item::Fn(f)) = attrs.items.into_iter().next() {
        return f.attrs.into_iter().next();
    }
    None
}

/// Attach a cluster cfg gate to an item's existing attributes. If the item
/// already has a `#[cfg(...)]`, the gate is composed via `all(...)`.
pub(crate) fn attach_cluster_gate_to_item(item: &mut syn::Item, gate_pred: &str) {
    if gate_pred.trim().is_empty() {
        return;
    }
    let new_attr = match cfg_attr_from_predicate(gate_pred) {
        Some(a) => a,
        None => return,
    };
    let attrs = item_attrs_mut(item);
    if let Some(attrs) = attrs {
        attrs.insert(0, new_attr);
    }
}

pub(crate) fn item_attrs_mut(item: &mut syn::Item) -> Option<&mut Vec<syn::Attribute>> {
    Some(match item {
        syn::Item::Fn(f) => &mut f.attrs,
        syn::Item::Struct(s) => &mut s.attrs,
        syn::Item::Enum(e) => &mut e.attrs,
        syn::Item::Union(u) => &mut u.attrs,
        syn::Item::Trait(t) => &mut t.attrs,
        syn::Item::TraitAlias(t) => &mut t.attrs,
        syn::Item::Type(t) => &mut t.attrs,
        syn::Item::Const(c) => &mut c.attrs,
        syn::Item::Static(s) => &mut s.attrs,
        syn::Item::Mod(m) => &mut m.attrs,
        syn::Item::Macro(m) => &mut m.attrs,
        syn::Item::Impl(i) => &mut i.attrs,
        syn::Item::Use(u) => &mut u.attrs,
        syn::Item::ExternCrate(e) => &mut e.attrs,
        syn::Item::ForeignMod(f) => &mut f.attrs,
        _ => return None,
    })
}

pub(crate) fn trait_item_attrs_mut(item: &mut syn::TraitItem) -> Option<&mut Vec<syn::Attribute>> {
    Some(match item {
        syn::TraitItem::Fn(f) => &mut f.attrs,
        syn::TraitItem::Const(c) => &mut c.attrs,
        syn::TraitItem::Type(t) => &mut t.attrs,
        syn::TraitItem::Macro(m) => &mut m.attrs,
        _ => return None,
    })
}

pub(crate) fn impl_item_attrs_mut(item: &mut syn::ImplItem) -> Option<&mut Vec<syn::Attribute>> {
    Some(match item {
        syn::ImplItem::Fn(f) => &mut f.attrs,
        syn::ImplItem::Const(c) => &mut c.attrs,
        syn::ImplItem::Type(t) => &mut t.attrs,
        syn::ImplItem::Macro(m) => &mut m.attrs,
        _ => return None,
    })
}

/// Recursive merge for trait body items. Returns the merged list of
/// associated items: identical entries across all clusters appear once;
/// divergent entries appear once per cluster, each gated with that cluster's
/// cfg predicate.
pub(crate) fn merge_trait_items(
    cluster_items: &[(&str, &str, &[syn::TraitItem])],
) -> Vec<syn::TraitItem> {
    let mut groups: BTreeMap<ItemKey, Vec<(&str, &str, &syn::TraitItem)>> = BTreeMap::new();
    let mut order: Vec<ItemKey> = Vec::new();
    for (cluster, gate, items) in cluster_items {
        for it in *items {
            let key = trait_item_key(it);
            if !groups.contains_key(&key) {
                order.push(key.clone());
            }
            groups.entry(key).or_default().push((cluster, gate, it));
        }
    }

    let total_clusters = cluster_items.len();
    let mut out: Vec<syn::TraitItem> = Vec::new();
    for key in order {
        let bucket = groups
            .remove(&key)
            .expect("`order` is built from `groups`' own keys — remove cannot miss");
        // Group by token hash so identical content collapses.
        let mut by_content: BTreeMap<String, Vec<(&str, &str, &syn::TraitItem)>> = BTreeMap::new();
        for entry in &bucket {
            let item_ref = entry.2;
            let h = quote!(#item_ref).to_string();
            by_content.entry(h).or_default().push(*entry);
        }
        // Single content group present in all clusters → emit once unaltered.
        if by_content.len() == 1 && bucket.len() == total_clusters {
            let item = bucket[0].2.clone();
            out.push(item);
            continue;
        }
        // Otherwise: emit each unique content with cluster cfg gates joined by `any(...)`.
        for (_h, entries) in by_content {
            let mut item = entries[0].2.clone();
            let gates: Vec<&str> = entries.iter().map(|(_, g, _)| *g).collect();
            let composed = compose_gates_any(&gates);
            if let Some(new_attr) = cfg_attr_from_predicate(&composed)
                && let Some(attrs) = trait_item_attrs_mut(&mut item)
            {
                attrs.insert(0, new_attr);
            }
            out.push(item);
        }
    }
    out
}

/// Recursive merge for impl body items. Same shape as `merge_trait_items`.
pub(crate) fn merge_impl_items(
    cluster_items: &[(&str, &str, &[syn::ImplItem])],
) -> Vec<syn::ImplItem> {
    let mut groups: BTreeMap<ItemKey, Vec<(&str, &str, &syn::ImplItem)>> = BTreeMap::new();
    let mut order: Vec<ItemKey> = Vec::new();
    for (cluster, gate, items) in cluster_items {
        for it in *items {
            let key = impl_item_key(it);
            if !groups.contains_key(&key) {
                order.push(key.clone());
            }
            groups.entry(key).or_default().push((cluster, gate, it));
        }
    }
    let total_clusters = cluster_items.len();
    let mut out: Vec<syn::ImplItem> = Vec::new();
    for key in order {
        let bucket = groups
            .remove(&key)
            .expect("`order` is built from `groups`' own keys — remove cannot miss");
        let mut by_content: BTreeMap<String, Vec<(&str, &str, &syn::ImplItem)>> = BTreeMap::new();
        for entry in &bucket {
            let item_ref = entry.2;
            let h = quote!(#item_ref).to_string();
            by_content.entry(h).or_default().push(*entry);
        }
        if by_content.len() == 1 && bucket.len() == total_clusters {
            out.push(bucket[0].2.clone());
            continue;
        }
        for (_h, entries) in by_content {
            let mut item = entries[0].2.clone();
            let gates: Vec<&str> = entries.iter().map(|(_, g, _)| *g).collect();
            let composed = compose_gates_any(&gates);
            if let Some(new_attr) = cfg_attr_from_predicate(&composed)
                && let Some(attrs) = impl_item_attrs_mut(&mut item)
            {
                attrs.insert(0, new_attr);
            }
            out.push(item);
        }
    }
    out
}

/// Compose multiple cluster gates as `any(g1, g2, ...)`. Each gate is a
/// raw predicate (without `#[cfg(...)]` wrapping). Empty gates → no
/// gating needed (item appears in those clusters unconditionally, so the
/// composed predicate is effectively true).
pub(crate) fn compose_gates_any(gates: &[&str]) -> String {
    let trimmed: Vec<&str> = gates.iter().map(|g| g.trim()).collect();
    if trimmed.iter().any(|g| g.is_empty()) {
        return String::new(); // item is unconditional in at least one cluster
    }
    if trimmed.len() == 1 {
        return trimmed[0].to_string();
    }
    format!("any({})", trimmed.join(", "))
}

/// Recursive merge of top-level items across clusters.
pub(crate) fn merge_top_items(cluster_files: &[(String, String, syn::File)]) -> Vec<syn::Item> {
    let mut groups: BTreeMap<ItemKey, Vec<(&str, &str, &syn::Item)>> = BTreeMap::new();
    let mut order: Vec<ItemKey> = Vec::new();
    for (name, gate, file) in cluster_files {
        for it in &file.items {
            let key = item_key(it);
            if !groups.contains_key(&key) {
                order.push(key.clone());
            }
            groups
                .entry(key)
                .or_default()
                .push((name.as_str(), gate.as_str(), it));
        }
    }

    let total_clusters = cluster_files.len();
    let mut out: Vec<syn::Item> = Vec::new();
    for key in order {
        let bucket = groups
            .remove(&key)
            .expect("`order` is built from `groups`' own keys — remove cannot miss");

        // Items that have inner bodies (Trait, Impl, Mod) need recursive
        // merge regardless of whether the outer signatures match — the
        // bodies may diverge while signatures don't.
        let kind = &key.0;
        if kind == "trait" {
            let merged = merge_trait_block(&bucket);
            out.push(syn::Item::Trait(merged));
            continue;
        }
        if kind == "impl" {
            let merged = merge_impl_block(&bucket);
            out.push(syn::Item::Impl(merged));
            continue;
        }
        if kind == "mod" {
            let merged = merge_mod_block(&bucket);
            out.push(syn::Item::Mod(merged));
            continue;
        }

        // Non-recursive items: group by token-hash, dedupe, gate divergent.
        let mut by_content: BTreeMap<String, Vec<(&str, &str, &syn::Item)>> = BTreeMap::new();
        for entry in &bucket {
            let item_ref = entry.2;
            let h = quote!(#item_ref).to_string();
            by_content.entry(h).or_default().push(*entry);
        }
        if by_content.len() == 1 && bucket.len() == total_clusters {
            out.push(bucket[0].2.clone());
            continue;
        }
        for (_h, entries) in by_content {
            let mut item = entries[0].2.clone();
            let gates: Vec<&str> = entries.iter().map(|(_, g, _)| *g).collect();
            let composed = compose_gates_any(&gates);
            attach_cluster_gate_to_item(&mut item, &composed);
            out.push(item);
        }
    }
    out
}

pub(crate) fn merge_trait_block(bucket: &[(&str, &str, &syn::Item)]) -> syn::ItemTrait {
    // Base trait from first entry; merge bodies across all entries.
    let base = match bucket[0].2 {
        syn::Item::Trait(t) => t.clone(),
        _ => unreachable!("merge_trait_block called on non-trait item"),
    };
    let cluster_items: Vec<(&str, &str, &[syn::TraitItem])> = bucket
        .iter()
        .map(|(c, g, it)| match it {
            syn::Item::Trait(t) => (*c, *g, t.items.as_slice()),
            _ => (*c, *g, &[] as &[syn::TraitItem]),
        })
        .collect();
    let merged_items = merge_trait_items(&cluster_items);
    syn::ItemTrait {
        items: merged_items,
        ..base
    }
}

pub(crate) fn merge_impl_block(bucket: &[(&str, &str, &syn::Item)]) -> syn::ItemImpl {
    let base = match bucket[0].2 {
        syn::Item::Impl(i) => i.clone(),
        _ => unreachable!("merge_impl_block called on non-impl item"),
    };
    let cluster_items: Vec<(&str, &str, &[syn::ImplItem])> = bucket
        .iter()
        .map(|(c, g, it)| match it {
            syn::Item::Impl(i) => (*c, *g, i.items.as_slice()),
            _ => (*c, *g, &[] as &[syn::ImplItem]),
        })
        .collect();
    let merged_items = merge_impl_items(&cluster_items);
    syn::ItemImpl {
        items: merged_items,
        ..base
    }
}

pub(crate) fn merge_mod_block(bucket: &[(&str, &str, &syn::Item)]) -> syn::ItemMod {
    let base = match bucket[0].2 {
        syn::Item::Mod(m) => m.clone(),
        _ => unreachable!("merge_mod_block called on non-mod item"),
    };
    // Only inline modules (with content) have inner items to recurse into.
    // For external mod declarations (mod foo;), there's nothing to merge.
    let has_content = bucket.iter().any(|(_, _, it)| match it {
        syn::Item::Mod(m) => m.content.is_some(),
        _ => false,
    });
    if !has_content {
        return base;
    }
    // Recurse: treat each cluster's inline mod body as a virtual File.
    let virtual_files: Vec<(String, String, syn::File)> = bucket
        .iter()
        .filter_map(|(c, g, it)| match it {
            syn::Item::Mod(m) => m.content.as_ref().map(|(_, items)| {
                (
                    c.to_string(),
                    g.to_string(),
                    syn::File {
                        shebang: None,
                        attrs: Vec::new(),
                        items: items.clone(),
                    },
                )
            }),
            _ => None,
        })
        .collect();
    let merged_items = merge_top_items(&virtual_files);
    let mut out = base;
    if let Some((brace, _)) = &out.content {
        out.content = Some((*brace, merged_items));
    }
    out
}

/// Top-level entry: merge N cluster files into a single source string.
/// If `attr` is a `#[doc = "...\n..."]` whose string spans multiple lines,
/// return the individual lines. `None` for non-doc or already single-line docs.
pub(crate) fn doc_attr_lines(attr: &syn::Attribute) -> Option<Vec<String>> {
    if !attr.path().is_ident("doc") {
        return None;
    }
    let syn::Meta::NameValue(nv) = &attr.meta else {
        return None;
    };
    let syn::Expr::Lit(syn::ExprLit {
        lit: syn::Lit::Str(s),
        ..
    }) = &nv.value
    else {
        return None;
    };
    let val = s.value();
    if !val.contains('\n') {
        return None;
    }
    Some(val.split('\n').map(|l| l.to_string()).collect())
}

pub(crate) fn make_doc_attr(style: syn::AttrStyle, line: &str) -> syn::Attribute {
    let lit = syn::LitStr::new(line, proc_macro2::Span::call_site());
    match style {
        syn::AttrStyle::Inner(_) => syn::parse_quote!(#![doc = #lit]),
        syn::AttrStyle::Outer => syn::parse_quote!(#[doc = #lit]),
    }
}

/// Split every multi-line `#[doc]` in `attrs` into one single-line `#[doc]` per
/// line (FR-033). prettyplease renders single-line docs uniformly as `///` /
/// `//!`, but switches a doc string that contains an embedded newline to the
/// `/** */` / `/*! */` block form — and a run that alternates between the two
/// styles splits a doctest's ```` ``` ```` fence across comment kinds, so the
/// example's hidden setup lines (`# struct Foo;`) no longer reach the doctest
/// (E0425). Normalising to one line per doc keeps every example intact.
pub(crate) fn normalize_doc_attrs(attrs: &mut Vec<syn::Attribute>) {
    if !attrs.iter().any(|a| doc_attr_lines(a).is_some()) {
        return;
    }
    let mut out: Vec<syn::Attribute> = Vec::with_capacity(attrs.len());
    for attr in attrs.drain(..) {
        match doc_attr_lines(&attr) {
            Some(lines) => {
                for line in lines {
                    out.push(make_doc_attr(attr.style, &line));
                }
            }
            None => out.push(attr),
        }
    }
    *attrs = out;
}

pub(crate) fn normalize_doc_attrs_in_items(items: &mut [syn::Item]) {
    for it in items.iter_mut() {
        if let Some(attrs) = item_attrs_mut(it) {
            normalize_doc_attrs(attrs);
        }
        match it {
            syn::Item::Mod(m) => {
                if let Some((_, inner)) = &mut m.content {
                    normalize_doc_attrs_in_items(inner);
                }
            }
            syn::Item::Impl(i) => {
                for ii in &mut i.items {
                    if let Some(a) = impl_item_attrs_mut(ii) {
                        normalize_doc_attrs(a);
                    }
                }
            }
            syn::Item::Trait(t) => {
                for ti in &mut t.items {
                    if let Some(a) = trait_item_attrs_mut(ti) {
                        normalize_doc_attrs(a);
                    }
                }
            }
            _ => {}
        }
    }
}

pub fn merge_cluster_files(files: Vec<ClusterFile>) -> Result<String> {
    if files.is_empty() {
        return Ok(String::new());
    }
    let parsed: Vec<(String, String, syn::File)> = files
        .iter()
        .map(|f| f.ast().map(|a| (f.name.clone(), f.cfg_gate.clone(), a)))
        .collect::<Result<_>>()?;

    // Preserve shebang and inner attrs from the first cluster (base).
    let base_ast = &parsed[0].2;
    let merged_items = merge_top_items(&parsed);
    let mut merged = syn::File {
        shebang: base_ast.shebang.clone(),
        attrs: base_ast.attrs.clone(),
        items: merged_items,
    };
    // FR-033: normalise doc comments to one line per `#[doc]` so prettyplease
    // renders them uniformly (no `///`↔`/** */` alternation that breaks docs).
    normalize_doc_attrs(&mut merged.attrs);
    normalize_doc_attrs_in_items(&mut merged.items);
    Ok(prettyplease::unparse(&merged))
}
