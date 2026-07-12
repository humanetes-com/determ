//! Skeletonisation: replace fn/method bodies with `unimplemented!()`, drop #[cfg(test)] mods,
//! apply per-cluster cfg filtering. Moved verbatim from lib.rs (slice 7).

use crate::cfg::*;
use crate::error::{ExtractError, Result};
use crate::model::*;
use std::path::{Path, PathBuf};
use syn::visit_mut::{self, VisitMut};
use syn::{
    Block, ImplItem, ImplItemFn, Item, ItemFn, ItemImpl, ItemMod, ItemTrait, TraitItem,
    TraitItemFn, parse_file,
};

/// Parse-check a single Rust source FILE with syn (the structured parser the
/// whole pipeline standardises on). `Ok(())` iff `parse_file` accepts it — the
/// transactional splice gate's signal that a write left the file well-formed.
pub fn parse_check(input_file: &Path) -> Result<()> {
    let source = std::fs::read_to_string(input_file)
        .map_err(|e| ExtractError::read(input_file, e))?;
    parse_file(&source).map_err(|e| ExtractError::parse(input_file, e))?;
    Ok(())
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
    for entry in walkdir::WalkDir::new(input_dir)
        .into_iter()
        .filter_map(|e| e.ok())
    {
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
            // Pipeline scratch directories. If the caller accidentally
            // passes `project_dir` instead of `project_dir/src`, the
            // extractor would otherwise recurse into prior cluster
            // outputs (and prior cluster outputs of prior cluster
            // outputs, etc.) and emit until `mkdir` hits ENAMETOOLONG.
            || rel_str.starts_with(".skeleton")
            || rel_str.contains("/.skeleton")
            || rel_str.starts_with(".phase_c_logs")
            || rel_str.contains("/.phase_c_logs")
            || rel_str.starts_with(".original_structure")
            || rel_str.contains("/.original_structure")
            // task0c-generated test sibling files. They sit in src/ next to the
            // real source but are protected — never include them in skeletons.
            || rel_str.contains(".gen_tests_")
        {
            continue;
        }
        // Inner error already carries the offending path (Read/Parse/Write); just propagate.
        let report = extract_file_with_cfg(path, input_dir, output_dir, cfg_set)?;
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
    let source =
        std::fs::read_to_string(input_path).map_err(|e| ExtractError::read(input_path, e))?;
    let bytes_in = source.len();

    // syn::parse_file accepts a full Rust source file.
    let mut ast = parse_file(&source).map_err(|e| ExtractError::parse(input_path, e))?;

    // Phase 1: drop items whose #[cfg(...)] evaluates to false under cfg_set.
    let mut cfg_filter = CfgFilterVisitor::new(cfg_set);
    cfg_filter.visit_file_mut(&mut ast);

    // Phase 2: stub bodies, strip #[cfg(test)] mods, etc.
    let mut visitor = SkeletonVisitor::default();
    visitor.visit_file_mut(&mut ast);

    // Re-emit the AST as properly-formatted Rust via prettyplease (already a
    // dependency; merge.rs uses the same path). The old naive token dump
    // (`render_tokens` over `quote!`/`to_string`) produced single-line bodies
    // like `{ :: core :: unimplemented ! () }` and `# ! [doc = ...]`; the
    // text-based per-fn splicer miscut those offsets → dropped the model's body,
    // left `{ ::`, "unclosed delimiter" → false GENUINELY_HARD reverts (the
    // phase-c non-convergence root cause). FR-033: normalise multi-line docs
    // first so prettyplease keeps them as `///`/`//!` (no `/** */` switch).
    crate::merge::normalize_doc_attrs(&mut ast.attrs);
    crate::merge::normalize_doc_attrs_in_items(&mut ast.items);
    let rendered = prettyplease::unparse(&ast);

    let rel = input_path.strip_prefix(input_dir).unwrap_or(input_path);
    let out_path: PathBuf = output_dir.join(rel);
    if let Some(parent) = out_path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| ExtractError::write(parent, e))?;
    }
    std::fs::write(&out_path, &rendered).map_err(|e| ExtractError::write(&out_path, e))?;

    Ok(FileReport {
        relative_path: rel.to_string_lossy().into_owned(),
        functions_stubbed: visitor.functions_stubbed,
        test_mods_dropped: visitor.test_mods_dropped,
        bytes_in,
        bytes_out: rendered.len(),
    })
}

/// Visitor that drops items whose `#[cfg(...)]` attributes evaluate to false
/// under the supplied CfgSet. Items whose predicates are absent or
/// undecidable (unknown keys) are kept.
pub(crate) struct CfgFilterVisitor<'a> {
    cfg: &'a CfgSet,
    items_dropped: usize,
}

impl<'a> CfgFilterVisitor<'a> {
    pub(crate) fn new(cfg: &'a CfgSet) -> Self {
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
            if let Some(meta) = inner_meta
                && let Some(b) = self.cfg.eval(&meta)
                && !b
            {
                return false; // explicit false → drop
            }
            // None (undecidable) → keep (safe default)
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

pub(crate) fn item_attrs(item: &Item) -> &[syn::Attribute] {
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
pub(crate) struct SkeletonVisitor {
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
        *node.block = Self::stub_body();
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

        // KEEP `#[cfg(test)] mod ...` — the original inline tests ARE the oracle
        // (FR-018: strip removes implementation AND keeps tests). visit_item_mod_mut
        // guards each test mod from being recursed-into (so its test bodies are
        // never stubbed) and from being dropped.
        visit_mut::visit_file_mut(self, node);
    }

    fn visit_item_mod_mut(&mut self, node: &mut ItemMod) {
        // A `#[cfg(test)]` mod is the original test code — leave it ENTIRELY intact:
        // do not recurse (its test fn bodies must stay real, not `unimplemented!()`)
        // and do not drop it. It survives strip in place as the reconstruction oracle.
        if Self::has_test_cfg(&node.attrs) {
            return;
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

// ---------------------------------------------------------------------------
// enumerate-fns: walk a source tree, emit one JSON record per fn item.
// Used by Python pipeline to replace regex-based fn discovery with the
// canonical syn AST. Catches inline declarations, trait methods, impl
// methods, cfg-attributed fns — everything rustc considers a fn.
// ---------------------------------------------------------------------------

pub(crate) fn body_looks_unimplemented(body: &str) -> bool {
    let s = body.trim();
    // Plain `unimplemented!()` form (possibly with `::core::` prefix and spaces).
    if s.len() < 60 && s.contains("unimplemented") {
        return true;
    }
    // Annotated stub form (P3.12: failed-attempt comment + unimplemented at end).
    s.contains("// SKELETON_STUB_FAILED_ATTEMPT")
}
