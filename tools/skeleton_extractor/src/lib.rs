// Skeleton extractor — Phase A of the 4-phase TASK 2 reconstruction.
//
// Reads a pre-strip Rust source tree, emits per-file "skeletons" where:
//   - All type declarations (pub + private) keep their full body.
//   - All function/method bodies are replaced with `unimplemented!()`.
//   - All `#[cfg(test)] mod ...` items are dropped.
//   - Macros, imports, attributes, and consts are preserved verbatim.
//
// The skeleton is the structural ground truth that Phase B/C/D fill in.

use anyhow::{Context as _, Result};
use proc_macro2::TokenStream;
use quote::quote;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use syn::visit_mut::{self, VisitMut};
use syn::{
    parse_file, Block, ImplItem, ImplItemFn, Item, ItemFn, ItemImpl, ItemMod, ItemTrait, TraitItem,
    TraitItemFn,
};

/// Cluster cfg-set: maps predicate strings (as they appear in `#[cfg(...)]`)
/// to their boolean evaluation under the active cluster.
///
/// Examples of keys:
///   - `feature = "std"`     → typically true for stable
///   - `nightly`             → false for stable, true for nightly cluster
///   - `target_os = "linux"` → set by the build target
#[derive(Default, Clone)]
pub struct CfgSet {
    map: HashMap<String, bool>,
}

impl CfgSet {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn set_true(&mut self, pred: impl Into<String>) {
        self.map.insert(pred.into(), true);
    }

    pub fn set_false(&mut self, pred: impl Into<String>) {
        self.map.insert(pred.into(), false);
    }

    /// Evaluate a `#[cfg(...)]` predicate against the cluster cfg-set.
    /// Returns Some(true/false) when the predicate can be fully decided,
    /// or None when the predicate references an unknown key (which we
    /// treat as "keep, don't filter" to be safe).
    pub fn eval(&self, meta: &syn::Meta) -> Option<bool> {
        match meta {
            // Bare ident: `#[cfg(nightly)]`
            syn::Meta::Path(path) => {
                let key = path_to_string(path);
                self.map.get(&key).copied()
            }
            // `#[cfg(not(...))]`, `#[cfg(all(...))]`, `#[cfg(any(...))]`
            syn::Meta::List(list) => {
                let op = path_to_string(&list.path);
                // Parse the nested metas. syn::Meta::List has `tokens`; we
                // re-parse as Punctuated<NestedMeta-like, Comma>.
                // For older/newer syn API: use parse_nested_meta-like manual walk.
                let inner = syn::parse2::<NestedMetaList>(list.tokens.clone()).ok()?;
                match op.as_str() {
                    "not" => {
                        let item = inner.items.first()?;
                        self.eval(item).map(|b| !b)
                    }
                    "all" => {
                        let mut acc = true;
                        for it in &inner.items {
                            match self.eval(it) {
                                Some(b) => acc = acc && b,
                                None => return None,
                            }
                        }
                        Some(acc)
                    }
                    "any" => {
                        let mut acc = false;
                        for it in &inner.items {
                            match self.eval(it) {
                                Some(b) => acc = acc || b,
                                None => return None,
                            }
                        }
                        Some(acc)
                    }
                    _ => None,
                }
            }
            // `#[cfg(feature = "std")]`
            syn::Meta::NameValue(nv) => {
                let key = path_to_string(&nv.path);
                // Render the value side via tokens.
                let value_tokens = nv.value.to_token_stream();
                let full_pred = format!("{} = {}", key, value_tokens);
                // Try a few normalised forms.
                if let Some(b) = self.map.get(&full_pred) {
                    return Some(*b);
                }
                // Strip whitespace inside.
                let compact = full_pred.replace(' ', "");
                for (k, v) in &self.map {
                    if k.replace(' ', "") == compact {
                        return Some(*v);
                    }
                }
                None
            }
        }
    }
}

fn path_to_string(p: &syn::Path) -> String {
    p.segments
        .iter()
        .map(|s| s.ident.to_string())
        .collect::<Vec<_>>()
        .join("::")
}

/// Helper: parse a `Meta::List`'s inner tokens as a comma-separated list of
/// nested `Meta`. syn 2.x doesn't expose a built-in helper for this in all
/// configurations, so we hand-roll using `Punctuated`.
struct NestedMetaList {
    items: Vec<syn::Meta>,
}

impl syn::parse::Parse for NestedMetaList {
    fn parse(input: syn::parse::ParseStream) -> syn::Result<Self> {
        let punct: syn::punctuated::Punctuated<syn::Meta, syn::Token![,]> =
            input.parse_terminated(syn::Meta::parse, syn::Token![,])?;
        Ok(NestedMetaList {
            items: punct.into_iter().collect(),
        })
    }
}

use quote::ToTokens;

/// Result of skeletonising a single file.
#[derive(Debug, Clone, serde::Serialize)]
pub struct FileReport {
    pub relative_path: String,
    pub functions_stubbed: usize,
    pub test_mods_dropped: usize,
    pub bytes_in: usize,
    pub bytes_out: usize,
}

/// Walk `input_dir`, skeletonise every .rs file, mirror under `output_dir`.
/// Backwards-compatible wrapper that uses an empty CfgSet (no filtering).
pub fn extract_tree(input_dir: &Path, output_dir: &Path) -> Result<Vec<FileReport>> {
    extract_tree_with_cfg(input_dir, output_dir, &CfgSet::default())
}

/// Walk `input_dir`, skeletonise every .rs file, mirror under `output_dir`.
/// Items whose `#[cfg(...)]` predicates evaluate to false under `cfg_set`
/// are dropped before body-stubbing.
pub fn extract_tree_with_cfg(
    input_dir: &Path,
    output_dir: &Path,
    cfg_set: &CfgSet,
) -> Result<Vec<FileReport>> {
    let mut reports = Vec::new();
    for entry in walkdir::WalkDir::new(input_dir).into_iter().filter_map(|e| e.ok()) {
        let path = entry.path();
        if !path.is_file() {
            continue;
        }
        if path.extension().and_then(|s| s.to_str()) != Some("rs") {
            continue;
        }
        // Skip directories we never want to skeletonise.
        let rel = path.strip_prefix(input_dir).unwrap_or(path);
        let rel_str = rel.to_string_lossy();
        if rel_str.starts_with("target/")
            || rel_str.contains("/target/")
            || rel_str.starts_with(".git/")
            || rel_str.starts_with("tests/")
            || rel_str.contains("/tests/")
            || rel_str.starts_with("examples/")
            || rel_str.contains("/examples/")
            || rel_str.starts_with("benches/")
            || rel_str.contains("/benches/")
            // task0c-generated test sibling files. They sit in src/ next to the
            // real source but are protected — never include them in skeletons.
            || rel_str.contains(".gen_tests_")
        {
            continue;
        }
        let report = extract_file_with_cfg(path, input_dir, output_dir, cfg_set)
            .with_context(|| format!("skeletonising {}", path.display()))?;
        reports.push(report);
    }
    Ok(reports)
}

/// Skeletonise one file. Writes the skeleton to the mirrored path under `output_dir`.
/// Backwards-compatible wrapper.
pub fn extract_file(input_path: &Path, input_dir: &Path, output_dir: &Path) -> Result<FileReport> {
    extract_file_with_cfg(input_path, input_dir, output_dir, &CfgSet::default())
}

/// Skeletonise one file with cfg-aware item filtering.
pub fn extract_file_with_cfg(
    input_path: &Path,
    input_dir: &Path,
    output_dir: &Path,
    cfg_set: &CfgSet,
) -> Result<FileReport> {
    let source = std::fs::read_to_string(input_path)
        .with_context(|| format!("reading {}", input_path.display()))?;
    let bytes_in = source.len();

    // syn::parse_file accepts a full Rust source file.
    let mut ast = parse_file(&source)
        .with_context(|| format!("parsing {} as Rust source", input_path.display()))?;

    // Phase 1: drop items whose #[cfg(...)] evaluates to false under cfg_set.
    let mut cfg_filter = CfgFilterVisitor::new(cfg_set);
    cfg_filter.visit_file_mut(&mut ast);

    // Phase 2: stub bodies, strip #[cfg(test)] mods, etc.
    let mut visitor = SkeletonVisitor::default();
    visitor.visit_file_mut(&mut ast);

    // Re-emit the AST. quote! preserves doc-comments and most attribute text;
    // it does NOT preserve original spacing/comments. The skeleton's purpose
    // is compilability + structural signal, not byte-for-byte fidelity.
    let tokens: TokenStream = quote!(#ast);
    let rendered = render_tokens(&tokens);

    let rel = input_path.strip_prefix(input_dir).unwrap_or(input_path);
    let out_path: PathBuf = output_dir.join(rel);
    if let Some(parent) = out_path.parent() {
        std::fs::create_dir_all(parent)
            .with_context(|| format!("mkdir {}", parent.display()))?;
    }
    std::fs::write(&out_path, &rendered)
        .with_context(|| format!("writing {}", out_path.display()))?;

    Ok(FileReport {
        relative_path: rel.to_string_lossy().into_owned(),
        functions_stubbed: visitor.functions_stubbed,
        test_mods_dropped: visitor.test_mods_dropped,
        bytes_in,
        bytes_out: rendered.len(),
    })
}

/// Render a TokenStream to a String. We add line breaks between top-level
/// items so the output is at least readable; we do not invoke `rustfmt`
/// here because it would be one more required tool. The pipeline's
/// `cargo check` is the real validator.
fn render_tokens(tokens: &TokenStream) -> String {
    // Naive but works: insert a blank line after every closing brace at column 0.
    // The output compiles regardless — formatting is best-effort.
    let raw = tokens.to_string();
    let mut out = String::with_capacity(raw.len() + raw.len() / 4);
    let mut depth: i32 = 0;
    let mut last_was_brace = false;
    for ch in raw.chars() {
        match ch {
            '{' => {
                depth += 1;
                out.push(ch);
                last_was_brace = false;
            }
            '}' => {
                depth -= 1;
                out.push(ch);
                if depth == 0 {
                    out.push('\n');
                    out.push('\n');
                    last_was_brace = true;
                } else {
                    last_was_brace = false;
                }
            }
            ';' => {
                out.push(ch);
                if depth == 0 {
                    out.push('\n');
                }
                last_was_brace = false;
            }
            _ => {
                out.push(ch);
                last_was_brace = false;
            }
        }
    }
    let _ = last_was_brace;
    out
}

/// Visitor that drops items whose `#[cfg(...)]` attributes evaluate to false
/// under the supplied CfgSet. Items whose predicates are absent or
/// undecidable (unknown keys) are kept.
struct CfgFilterVisitor<'a> {
    cfg: &'a CfgSet,
    items_dropped: usize,
}

impl<'a> CfgFilterVisitor<'a> {
    fn new(cfg: &'a CfgSet) -> Self {
        Self {
            cfg,
            items_dropped: 0,
        }
    }

    /// Returns true if the item should be KEPT, false if it should be dropped.
    fn keep_attrs(&self, attrs: &[syn::Attribute]) -> bool {
        for a in attrs {
            if !a.path().is_ident("cfg") {
                continue;
            }
            // Inner of `cfg(...)` — parse the single inner Meta.
            let inner_meta: Option<syn::Meta> = match &a.meta {
                syn::Meta::List(list) => syn::parse2::<syn::Meta>(list.tokens.clone()).ok(),
                _ => None,
            };
            if let Some(meta) = inner_meta {
                if let Some(b) = self.cfg.eval(&meta) {
                    if !b {
                        return false; // explicit false → drop
                    }
                }
                // None (undecidable) → keep (safe default)
            }
        }
        true
    }
}

impl<'a> VisitMut for CfgFilterVisitor<'a> {
    fn visit_file_mut(&mut self, node: &mut syn::File) {
        let before = node.items.len();
        node.items.retain(|item| self.keep_attrs(item_attrs(item)));
        self.items_dropped += before - node.items.len();
        visit_mut::visit_file_mut(self, node);
    }

    fn visit_item_mod_mut(&mut self, node: &mut ItemMod) {
        if let Some((_, items)) = node.content.as_mut() {
            let before = items.len();
            items.retain(|item| self.keep_attrs(item_attrs(item)));
            self.items_dropped += before - items.len();
        }
        visit_mut::visit_item_mod_mut(self, node);
    }

    fn visit_item_impl_mut(&mut self, node: &mut ItemImpl) {
        let before = node.items.len();
        node.items.retain(|item| match item {
            ImplItem::Const(c) => self.keep_attrs(&c.attrs),
            ImplItem::Fn(f) => self.keep_attrs(&f.attrs),
            ImplItem::Type(t) => self.keep_attrs(&t.attrs),
            ImplItem::Macro(m) => self.keep_attrs(&m.attrs),
            _ => true,
        });
        self.items_dropped += before - node.items.len();
        visit_mut::visit_item_impl_mut(self, node);
    }

    fn visit_item_trait_mut(&mut self, node: &mut ItemTrait) {
        let before = node.items.len();
        node.items.retain(|item| match item {
            TraitItem::Const(c) => self.keep_attrs(&c.attrs),
            TraitItem::Fn(f) => self.keep_attrs(&f.attrs),
            TraitItem::Type(t) => self.keep_attrs(&t.attrs),
            TraitItem::Macro(m) => self.keep_attrs(&m.attrs),
            _ => true,
        });
        self.items_dropped += before - node.items.len();
        visit_mut::visit_item_trait_mut(self, node);
    }
}

fn item_attrs(item: &Item) -> &[syn::Attribute] {
    match item {
        Item::Const(c) => &c.attrs,
        Item::Enum(e) => &e.attrs,
        Item::ExternCrate(e) => &e.attrs,
        Item::Fn(f) => &f.attrs,
        Item::ForeignMod(f) => &f.attrs,
        Item::Impl(i) => &i.attrs,
        Item::Macro(m) => &m.attrs,
        Item::Mod(m) => &m.attrs,
        Item::Static(s) => &s.attrs,
        Item::Struct(s) => &s.attrs,
        Item::Trait(t) => &t.attrs,
        Item::TraitAlias(t) => &t.attrs,
        Item::Type(t) => &t.attrs,
        Item::Union(u) => &u.attrs,
        Item::Use(u) => &u.attrs,
        _ => &[],
    }
}


#[derive(Default)]
struct SkeletonVisitor {
    functions_stubbed: usize,
    test_mods_dropped: usize,
}

impl SkeletonVisitor {
    fn stub_body() -> Block {
        // Parse a trivial body that contains the `unimplemented!()` expression.
        syn::parse_quote! {{ ::core::unimplemented!() }}
    }

    fn has_test_cfg(attrs: &[syn::Attribute]) -> bool {
        attrs.iter().any(|a| {
            // Match `#[cfg(test)]` exactly. We don't try to match `#[cfg(any(test, ...))]`
            // — those are rare in idiomatic Rust and erring on the side of keeping
            // them is safer.
            if !a.path().is_ident("cfg") {
                return false;
            }
            let mut is_test = false;
            let _ = a.parse_nested_meta(|meta| {
                if meta.path.is_ident("test") {
                    is_test = true;
                }
                Ok(())
            });
            is_test
        })
    }
}

impl VisitMut for SkeletonVisitor {
    fn visit_item_fn_mut(&mut self, node: &mut ItemFn) {
        // Free functions: replace body with `unimplemented!()`.
        node.block = Box::new(Self::stub_body());
        self.functions_stubbed += 1;
    }

    fn visit_impl_item_fn_mut(&mut self, node: &mut ImplItemFn) {
        // Inherent and trait-impl methods: replace body.
        node.block = Self::stub_body();
        self.functions_stubbed += 1;
    }

    fn visit_trait_item_fn_mut(&mut self, node: &mut TraitItemFn) {
        // Trait method default implementations: stub if present, else leave (signature only).
        if let Some(block) = node.default.as_mut() {
            *block = Self::stub_body();
            self.functions_stubbed += 1;
        }
    }

    fn visit_file_mut(&mut self, node: &mut syn::File) {
        // Strip crate-level deny/forbid lints. They're meant for real code; the
        // skeleton's `unimplemented!()` bodies trip every "unused" lint at the
        // edges. The full implementation phase will re-introduce real usage.
        node.attrs.retain(|attr| {
            let path = attr.path();
            !(path.is_ident("deny") || path.is_ident("forbid") || path.is_ident("warn"))
        });
        // Inject `#![allow(unused, dead_code, non_snake_case, clippy::all)]` so
        // the skeleton compiles cleanly regardless of which lints downstream
        // wrappers happen to deny.
        let allow_attr: syn::Attribute =
            syn::parse_quote!(#![allow(unused, dead_code, non_snake_case, clippy::all)]);
        node.attrs.insert(0, allow_attr);

        // Drop `#[cfg(test)] mod ...` items at file level.
        let before = node.items.len();
        node.items.retain(|item| match item {
            Item::Mod(m) => !Self::has_test_cfg(&m.attrs),
            _ => true,
        });
        self.test_mods_dropped += before - node.items.len();
        visit_mut::visit_file_mut(self, node);
    }

    fn visit_item_mod_mut(&mut self, node: &mut ItemMod) {
        // Drop nested `#[cfg(test)] mod ...` items too.
        if let Some((_, items)) = node.content.as_mut() {
            let before = items.len();
            items.retain(|item| match item {
                Item::Mod(m) => !Self::has_test_cfg(&m.attrs),
                _ => true,
            });
            self.test_mods_dropped += before - items.len();
        }
        visit_mut::visit_item_mod_mut(self, node);
    }

    // Const initializers: keep as-is. Type defs, impls, traits, macros: leave alone
    // (the visitor only mutates function bodies + drops test mods).
    // syn's default visitor walks into impls/traits/etc. automatically.
    fn visit_item_impl_mut(&mut self, node: &mut ItemImpl) {
        visit_mut::visit_item_impl_mut(self, node);
    }

    fn visit_item_trait_mut(&mut self, node: &mut ItemTrait) {
        visit_mut::visit_item_trait_mut(self, node);
    }

    fn visit_trait_item_mut(&mut self, node: &mut TraitItem) {
        visit_mut::visit_trait_item_mut(self, node);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn skeletonise_str(source: &str) -> String {
        let mut ast: syn::File = parse_file(source).unwrap();
        let mut v = SkeletonVisitor::default();
        v.visit_file_mut(&mut ast);
        render_tokens(&quote!(#ast))
    }

    #[test]
    fn stubs_free_function() {
        let out = skeletonise_str("pub fn answer() -> u32 { 42 }");
        assert!(out.contains("unimplemented"));
        assert!(out.contains("answer"));
    }

    #[test]
    fn keeps_struct_fields() {
        let out = skeletonise_str("pub struct Foo { pub a: u32, b: String }");
        assert!(out.contains("a : u32") || out.contains("a: u32"));
        assert!(out.contains("b : String") || out.contains("b: String"));
    }

    #[test]
    fn stubs_methods_in_impl() {
        let src = "pub struct S; impl S { pub fn m(&self) -> u32 { 1 } }";
        let out = skeletonise_str(src);
        assert!(out.contains("unimplemented"));
        assert!(out.contains("fn m"));
    }

    #[test]
    fn drops_cfg_test_mod() {
        let src = "pub fn a() {} #[cfg(test)] mod tests { fn t() {} }";
        let out = skeletonise_str(src);
        assert!(!out.contains("mod tests"));
        assert!(out.contains("fn a"));
    }

    #[test]
    fn preserves_const_value() {
        let src = "pub const X: u32 = 42;";
        let out = skeletonise_str(src);
        assert!(out.contains("42"));
    }

    #[test]
    fn preserves_macro_rules() {
        let src = "macro_rules! greet { () => { println!(\"hi\") } }";
        let out = skeletonise_str(src);
        assert!(out.contains("macro_rules"));
        assert!(out.contains("greet"));
    }

    fn skeletonise_with_cfg(source: &str, cfg: &CfgSet) -> String {
        let mut ast: syn::File = parse_file(source).unwrap();
        CfgFilterVisitor::new(cfg).visit_file_mut(&mut ast);
        SkeletonVisitor::default().visit_file_mut(&mut ast);
        render_tokens(&quote!(#ast))
    }

    #[test]
    fn cfg_drops_items_with_false_predicate() {
        let mut cfg = CfgSet::new();
        cfg.set_true("feature = \"std\"");
        cfg.set_false("nightly");
        let src = r#"
            pub fn always() {}
            #[cfg(nightly)]
            pub fn nightly_only() {}
            #[cfg(not(nightly))]
            pub fn stable_only() {}
            #[cfg(feature = "std")]
            pub fn needs_std() {}
        "#;
        let out = skeletonise_with_cfg(src, &cfg);
        assert!(out.contains("always"));
        assert!(out.contains("stable_only"));
        assert!(out.contains("needs_std"));
        assert!(!out.contains("nightly_only"), "nightly_only should be dropped");
    }

    #[test]
    fn cfg_evaluates_all_and_any() {
        let mut cfg = CfgSet::new();
        cfg.set_true("feature = \"std\"");
        cfg.set_false("nightly");
        let src = r#"
            #[cfg(all(feature = "std", not(nightly)))]
            pub fn stable_std_only() {}
            #[cfg(any(nightly, feature = "std"))]
            pub fn either_one() {}
            #[cfg(all(nightly, feature = "std"))]
            pub fn both_required() {}
        "#;
        let out = skeletonise_with_cfg(src, &cfg);
        assert!(out.contains("stable_std_only"));
        assert!(out.contains("either_one"));
        assert!(!out.contains("both_required"));
    }

    #[test]
    fn cfg_unknown_predicate_keeps_item() {
        // Safe default: predicates we can't decide → keep.
        let cfg = CfgSet::new();
        let src = r#"
            #[cfg(some_random_thing)]
            pub fn maybe() {}
        "#;
        let out = skeletonise_with_cfg(src, &cfg);
        assert!(out.contains("maybe"));
    }

    #[test]
    fn cfg_drops_methods_in_impl_block() {
        let mut cfg = CfgSet::new();
        cfg.set_false("nightly");
        let src = r#"
            pub struct S;
            impl S {
                pub fn always(&self) {}
                #[cfg(nightly)]
                pub fn nightly_only(&self) {}
            }
        "#;
        let out = skeletonise_with_cfg(src, &cfg);
        assert!(out.contains("always"));
        assert!(!out.contains("nightly_only"));
    }

    #[test]
    fn extract_tree_skips_gen_tests_siblings() {
        // Build a tiny in-temp fixture with both a real source file and a
        // gen_tests sibling; confirm only the real file is emitted.
        use std::fs;
        let tmp = std::env::temp_dir().join("skel_extractor_filter_test");
        if tmp.exists() {
            fs::remove_dir_all(&tmp).unwrap();
        }
        fs::create_dir_all(tmp.join("src")).unwrap();
        fs::write(
            tmp.join("src").join("lib.rs"),
            "pub fn answer() -> u32 { 42 }",
        )
        .unwrap();
        fs::write(
            tmp.join("src").join("lib.gen_tests_1.rs"),
            "#[cfg(test)] mod gen_tests_1 { #[test] fn t() {} }",
        )
        .unwrap();
        let out_dir = std::env::temp_dir().join("skel_extractor_filter_out");
        if out_dir.exists() {
            fs::remove_dir_all(&out_dir).unwrap();
        }
        fs::create_dir_all(&out_dir).unwrap();
        let reports = extract_tree(&tmp, &out_dir).unwrap();
        assert!(out_dir.join("src/lib.rs").exists());
        assert!(!out_dir.join("src/lib.gen_tests_1.rs").exists());
        assert!(reports.iter().all(|r| !r.relative_path.contains(".gen_tests_")));
    }
}
