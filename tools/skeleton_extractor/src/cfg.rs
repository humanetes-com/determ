//! Cfg predicate logic — the `CfgSet` evaluator + cfg-attribute parsing.
//!
//! Moved verbatim from `lib.rs`. Owns cluster cfg-set evaluation and the helpers that
//! pull predicate strings out of `#[cfg(...)]` attributes. No regex — syn token walks only.

use quote::{ToTokens, quote};
use std::collections::HashMap;

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
                            // Bind first, combine second. `acc = acc && self.eval(it)?`
                            // reads the same but is NOT the same: `&&` short-circuits, so
                            // once acc is false the remaining operands would never be
                            // evaluated and an unresolvable one among them would yield
                            // Some(false) where this yields None. Every operand of a cfg
                            // predicate has to be resolvable for the predicate to be.
                            let b = self.eval(it)?;
                            acc = acc && b;
                        }
                        Some(acc)
                    }
                    "any" => {
                        let mut acc = false;
                        for it in &inner.items {
                            let b = self.eval(it)?;
                            acc = acc || b;
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
pub(crate) fn path_to_string(p: &syn::Path) -> String {
    p.segments
        .iter()
        .map(|s| s.ident.to_string())
        .collect::<Vec<_>>()
        .join("::")
}
/// Helper: parse a `Meta::List`'s inner tokens as a comma-separated list of
/// nested `Meta`. syn 2.x doesn't expose a built-in helper for this in all
/// configurations, so we hand-roll using `Punctuated`.
pub(crate) struct NestedMetaList {
    pub(crate) items: Vec<syn::Meta>,
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
pub(crate) fn extract_cfg_predicates(attrs: &[syn::Attribute]) -> Vec<String> {
    let mut out = Vec::new();
    for attr in attrs {
        if attr.path().is_ident("cfg") {
            // attr.meta is Meta::List(MetaList { tokens, ... })
            if let syn::Meta::List(list) = &attr.meta {
                out.push(list.tokens.to_string().trim().to_string());
            }
        }
    }
    out
}
/// Collect the leaf atoms of a `#[cfg(...)]`-style condition meta as structured
/// (key, value) pairs, descending through `all(...)`/`any(...)`/`not(...)`. A
/// bare ident (`nightly`) yields `(nightly, None)`; a `feature = "std"` yields
/// `(feature, Some("\"std\""))` with spaces stripped so it matches the Python
/// domain's normalised form. Unknown list forms are ignored. Used by
/// `--enumerate-cfgs` to derive compiler clusters without regex.
pub(crate) fn cfg_meta_atoms(meta: &syn::Meta, out: &mut Vec<crate::model::CfgAtomWire>) {
    use crate::model::CfgAtomWire;
    match meta {
        syn::Meta::Path(p) => out.push(CfgAtomWire {
            key: path_to_string(p),
            value: None,
        }),
        syn::Meta::NameValue(nv) => out.push(CfgAtomWire {
            key: path_to_string(&nv.path),
            value: Some(nv.value.to_token_stream().to_string().replace(' ', "")),
        }),
        syn::Meta::List(list) => {
            let name = path_to_string(&list.path);
            if matches!(name.as_str(), "all" | "any" | "not")
                && let Ok(nested) = syn::parse2::<NestedMetaList>(list.tokens.clone())
            {
                for m in &nested.items {
                    cfg_meta_atoms(m, out);
                }
            }
        }
    }
}

/// Collect the leaf cfg predicates of a `#[cfg(...)]`-style condition meta
/// (e.g. `error_generic_member_access`, `feature = "std"`), descending through
/// `all(...)`/`any(...)`/`not(...)`. Used to learn which cfgs, when active,
/// switch on a nightly `feature(...)` attribute.
pub(crate) fn cfg_meta_leaves(meta: &syn::Meta, out: &mut std::collections::BTreeSet<String>) {
    match meta {
        syn::Meta::Path(p) => {
            out.insert(path_to_string(p));
        }
        syn::Meta::NameValue(_) => {
            out.insert(quote!(#meta).to_string());
        }
        syn::Meta::List(list) => {
            let name = list
                .path
                .get_ident()
                .map(|i| i.to_string())
                .unwrap_or_default();
            if matches!(name.as_str(), "all" | "any" | "not") {
                if let Ok(nested) = syn::parse2::<NestedMetaList>(list.tokens.clone()) {
                    for m in &nested.items {
                        cfg_meta_leaves(m, out);
                    }
                }
            } else {
                out.insert(quote!(#meta).to_string());
            }
        }
    }
}
