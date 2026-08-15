//! Source-preserving emission for the cluster merge.
//!
//! The merge decides WHICH item wins; this module decides how that item is
//! written back out. Rendering the merged AST with prettyplease loses every
//! `//` comment in every body, because non-doc comments are not tokens and so
//! never reach the AST at all. Measured on the live `anyhow` trees
//! (2026-08-05): `.skeleton-filled/nightly/src/error.rs` carries 6 line
//! comments and the merged output of that same file carried 0 — among them the
//! note on `object_drop_front` explaining why the `ManuallyDrop<E>` cast is
//! sound, written by the model that got the fn right. Doc comments survived
//! (they are attributes); the reasoning did not.
//!
//! So an item that survives the merge unchanged is written back as the ORIGINAL
//! TEXT its cluster held, located by `Span::byte_range()`. A block whose
//! members came from different clusters is rebuilt from its own header text and
//! its members' text, so only the braces are ours. Anything genuinely
//! synthesised still goes through prettyplease.
//!
//! The splice is self-verifying and that is what makes it safe: a byte range is
//! used only when re-parsing the text it points at yields a token stream EQUAL
//! to the node being emitted. A span that has drifted, a source that does not
//! match, a node the merge rewrote — all fail the comparison and fall back to
//! rendering. There is no path on which a wrong splice reaches the output.

use crate::merge::{ClusterFile, impl_item_attrs_mut, item_attrs_mut, trait_item_attrs_mut};
use proc_macro2::{Span, TokenStream};
use quote::{ToTokens, quote};
use syn::spanned::Spanned;

/// The cluster sources a splice may be taken from.
///
/// Which cluster a merged node came from is not tracked — it does not need to
/// be. The token-equality check is the authority: text that re-parses to the
/// same tokens is a faithful rendering of the node no matter which file it was
/// read out of, so trying each source in turn is sound and needs no provenance
/// plumbing through the recursive merge.
pub(crate) struct Sources<'a> {
    texts: Vec<&'a str>,
}

impl<'a> Sources<'a> {
    pub(crate) fn new(files: &'a [ClusterFile]) -> Self {
        Self {
            texts: files.iter().map(|f| f.source.as_str()).collect(),
        }
    }
}

/// Original text for the node spanning `span`, or None when no source
/// round-trips it into `want`.
///
/// Leading `//` comment lines immediately above the node are included: they are
/// the node's own header comments, and a pure-comment line can never sit inside
/// a preceding item's span (that item's span ends at its closing brace), so
/// taking them cannot steal text another node will also emit.
fn splice(sources: &Sources, span: Span, want: &TokenStream) -> Option<String> {
    let range = span.byte_range();
    if range.start >= range.end {
        return None;
    }
    let want = want.to_string();
    for src in &sources.texts {
        let Some(slice) = src.get(range.start..range.end) else {
            continue;
        };
        if !parses_to(slice, &want) {
            continue;
        }
        let start = extend_over_leading_comments(src, range.start);
        return src.get(start..range.end).map(str::to_string);
    }
    None
}

/// Walk backwards from `start` over whole lines holding nothing but a `//`
/// comment; return the offset that run begins at.
fn extend_over_leading_comments(src: &str, start: usize) -> usize {
    // Only a node that begins its own line can own the comments above it.
    let mut line_start = src[..start].rfind('\n').map(|i| i + 1).unwrap_or(0);
    if !src[line_start..start].trim().is_empty() {
        return start;
    }
    while line_start > 0 {
        let prev_end = line_start - 1; // the '\n' closing the previous line
        let prev_start = src[..prev_end].rfind('\n').map(|i| i + 1).unwrap_or(0);
        let line = src[prev_start..prev_end].trim();
        // `///` and `//!` are attributes; the node already carries them.
        if !line.starts_with("//") || line.starts_with("///") || line.starts_with("//!") {
            break;
        }
        line_start = prev_start;
    }
    line_start
}

fn parses_to(text: &str, want: &str) -> bool {
    match syn::parse_file(&format!("{text}\n")) {
        Ok(f) if f.items.len() == 1 => f.items[0].to_token_stream().to_string() == *want,
        _ => false,
    }
}

/// Split a leading `#[cfg(...)]` — the gate the merge prepends to mark an item
/// as one cluster's variant — off a node, returning it rendered plus the node
/// as its own cluster's source holds it.
macro_rules! strip_leading_cfg {
    ($node:expr, $attrs_mut:path) => {{
        let mut bare = $node.clone();
        match $attrs_mut(&mut bare) {
            Some(attrs) if attrs.first().is_some_and(|a| a.path().is_ident("cfg")) => {
                let attr = attrs.remove(0);
                Some((quote!(#attr).to_string(), bare))
            }
            _ => None,
        }
    }};
}

/// Write a cluster gate back above spliced text, at that text's own indent.
fn with_gate(cfg_line: &str, text: &str) -> String {
    let indent: String = text
        .chars()
        .take_while(|c| *c == ' ' || *c == '\t')
        .collect();
    format!("{indent}{cfg_line}\n{text}")
}

/// True when the item, or anything nested in it, carries a `#[doc = "…"]`
/// whose string spans several lines.
///
/// Such an item is rendered rather than spliced, so FR-033's normalisation
/// still runs on it: a multi-line doc string is what makes prettyplease switch
/// from `///` to `/** */`, and a run that alternates between the two splits a
/// doctest's ``` fence across comment kinds so the example's hidden setup lines
/// never reach the doctest (E0425). Splicing would keep the multi-line form
/// intact and harmless, but it would also mean the guard no longer covers this
/// item — and the cluster trees are themselves prettyplease output, so they
/// carry one-line `///` docs and lose nothing by this rule.
fn has_multiline_doc(item: &syn::Item) -> bool {
    let any = |attrs: &[syn::Attribute]| {
        attrs
            .iter()
            .any(|a| crate::merge::doc_attr_lines(a).is_some())
    };
    if any(crate::merge::item_attrs(item)) {
        return true;
    }
    match item {
        syn::Item::Impl(i) => i
            .items
            .iter()
            .any(|m| any(crate::merge::impl_item_attrs(m))),
        syn::Item::Trait(t) => t
            .items
            .iter()
            .any(|m| any(crate::merge::trait_item_attrs(m))),
        syn::Item::Mod(m) => m
            .content
            .as_ref()
            .is_some_and(|(_, items)| items.iter().any(has_multiline_doc)),
        _ => false,
    }
}

/// Render one merged top-level item, preferring its original source text.
pub(crate) fn emit_item(item: &syn::Item, sources: &Sources) -> String {
    if has_multiline_doc(item) {
        return render(item);
    }
    if let Some(text) = try_emit_item(item, sources) {
        return text;
    }
    if let Some((cfg_line, bare)) = strip_leading_cfg!(item, item_attrs_mut)
        && let Some(text) = try_emit_item(&bare, sources)
    {
        return with_gate(&cfg_line, &text);
    }
    // Genuinely synthesised — render it. Doc normalisation applies here and
    // only here: it exists to stop prettyplease alternating `///` with `/** */`
    // and splitting a doctest fence across comment kinds, which is a property
    // of rendering. Spliced text was never re-rendered and needs no repair.
    render(item)
}

fn render(item: &syn::Item) -> String {
    let mut owned = item.clone();
    crate::merge::normalize_doc_attrs_in_items(std::slice::from_mut(&mut owned));
    prettyplease::unparse(&syn::File {
        shebang: None,
        attrs: Vec::new(),
        items: vec![owned],
    })
}

/// The item verbatim, or rebuilt from its own header and members' text.
fn try_emit_item(item: &syn::Item, sources: &Sources) -> Option<String> {
    splice(sources, item.span(), &item.to_token_stream()).or_else(|| emit_block(item, sources))
}

/// Header text (up to and including the opening brace) for a block whose body
/// the merge rebuilt, verified by re-parsing it with an empty body.
fn block_header(sources: &Sources, span: Span, open: Span, empty: &TokenStream) -> Option<String> {
    let (start, end) = (span.byte_range().start, open.byte_range().end);
    if start >= end {
        return None;
    }
    let empty = empty.to_string();
    for src in &sources.texts {
        let Some(slice) = src.get(start..end) else {
            continue;
        };
        if !parses_to(&format!("{slice}\n}}"), &empty) {
            continue;
        }
        let from = extend_over_leading_comments(src, start);
        return src.get(from..end).map(str::to_string);
    }
    None
}

/// Assemble `header … members … }` from spliced parts, or None when any part
/// could not be spliced — a partial reconstruction is never emitted.
fn assemble(header: String, members: Vec<Option<String>>) -> Option<String> {
    let mut out = header;
    for m in members {
        out.push('\n');
        out.push_str(&m?);
    }
    out.push_str("\n}");
    Some(out)
}

fn emit_block(item: &syn::Item, sources: &Sources) -> Option<String> {
    match item {
        syn::Item::Impl(i) => {
            let empty = syn::ItemImpl {
                items: Vec::new(),
                ..i.clone()
            };
            let header = block_header(
                sources,
                i.span(),
                i.brace_token.span.open(),
                &empty.to_token_stream(),
            )?;
            let members = i
                .items
                .iter()
                .map(|m| {
                    splice(sources, m.span(), &m.to_token_stream()).or_else(|| {
                        let (cfg, bare) = strip_leading_cfg!(m, impl_item_attrs_mut)?;
                        let text = splice(sources, bare.span(), &bare.to_token_stream())?;
                        Some(with_gate(&cfg, &text))
                    })
                })
                .collect();
            assemble(header, members)
        }
        syn::Item::Trait(t) => {
            let empty = syn::ItemTrait {
                items: Vec::new(),
                ..t.clone()
            };
            let header = block_header(
                sources,
                t.span(),
                t.brace_token.span.open(),
                &empty.to_token_stream(),
            )?;
            let members = t
                .items
                .iter()
                .map(|m| {
                    splice(sources, m.span(), &m.to_token_stream()).or_else(|| {
                        let (cfg, bare) = strip_leading_cfg!(m, trait_item_attrs_mut)?;
                        let text = splice(sources, bare.span(), &bare.to_token_stream())?;
                        Some(with_gate(&cfg, &text))
                    })
                })
                .collect();
            assemble(header, members)
        }
        syn::Item::Mod(m) => {
            let (brace, items) = m.content.as_ref()?;
            let empty = syn::ItemMod {
                content: Some((*brace, Vec::new())),
                ..m.clone()
            };
            let header = block_header(
                sources,
                m.span(),
                brace.span.open(),
                &empty.to_token_stream(),
            )?;
            // Inner items are top-level items again, so they get the full
            // treatment — including their own nested blocks.
            let members = items
                .iter()
                .map(|it| Some(emit_item(it, sources)))
                .collect();
            assemble(header, members)
        }
        _ => None,
    }
}

/// Everything in the base cluster's file ahead of its first item — shebang,
/// inner attributes, and the file's own header comments.
///
/// Trailing `//` comment lines are trimmed off: they sit directly above the
/// first item and [`emit_item`] splices them as that item's header, so leaving
/// them here would emit them twice.
pub(crate) fn file_prefix(base: &ClusterFile, ast: &syn::File) -> String {
    let Some(first) = ast.items.first() else {
        return base.source.clone();
    };
    let start = first.span().byte_range().start;
    let upto = extend_over_leading_comments(&base.source, start);
    base.source.get(..upto).unwrap_or_default().to_string()
}
