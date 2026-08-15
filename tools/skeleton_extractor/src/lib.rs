//! Skeleton extractor — Phase A of the 4-phase TASK 2 reconstruction.
//!
//! Reads a pre-strip Rust source tree and provides two capabilities to the Python pipeline
//! over a JSON CLI (the seam — see `CODE_ARCHITECTURE.md` §4):
//!
//!   - **Skeletonise** ([`extract_tree`]): emit per-file skeletons where every fn/method body
//!     becomes `unimplemented!()`, `#[cfg(test)] mod ...` items are dropped, and types / macros
//!     / imports / attributes / consts are preserved verbatim. The structural ground truth
//!     Phase B/C/D fill in.
//!   - **Enumerate** ([`enumerate_fns_in_tree`], [`enumerate_types_in_tree`], …): emit typed
//!     [`FnRecord`] / [`TypeRecord`] / [`ModRecord`] / [`DispatchFamily`] records.
//!
//! Plus [`merge_cluster_files`] for cross-cluster AST merge. All public functions return
//! [`Result`] ([`ExtractError`]); the binary (`main.rs`) adapts these into `anyhow`.

// Library code returns typed errors (see error.rs) — a stray `.unwrap()` is a latent panic
// at the lib edge. Warn on it so it shows up in review / `clippy -D warnings`. Tests opt out
// (a test SHOULD panic on an unexpected None/Err) via `#![allow(...)]` in the test module.
#![warn(clippy::unwrap_used)]

mod error;
pub use error::{ExtractError, Result};

mod model;
pub use model::*;

mod cfg;
pub use cfg::CfgSet;

mod emit;
mod enumerate;
mod merge;
mod skeleton;
pub use enumerate::*;
pub use merge::*;
pub use skeleton::*;

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)] // tests should panic on unexpected None/Err
    use super::*;
    use syn::parse_file;
    use syn::visit_mut::VisitMut;

    fn skeletonise_str(source: &str) -> String {
        let mut ast: syn::File = parse_file(source).unwrap();
        let mut v = SkeletonVisitor::default();
        v.visit_file_mut(&mut ast);
        prettyplease::unparse(&ast)
    }

    /// `const _: () = { … };` — the idiom for scoping trait impls and
    /// compile-time assertions. Shaped after `anyhow`'s `src/nightly.rs`, whose
    /// build probe puts three impl fns inside one.
    const CONST_BLOCK_SRC: &str = r#"
        #[cfg(probe)]
        const _: () = {
            use core::fmt::{self, Debug, Display};
            struct MyError;
            impl Debug for MyError {
                fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
                    write!(f, "debug")
                }
            }
            impl Display for MyError {
                fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
                    write!(f, "display")
                }
            }
        };
        pub fn top_level() -> u8 { 1 }
    "#;

    #[test]
    fn fns_inside_a_const_block_are_enumerated() {
        // The skeletoniser is a VisitMut and recurses into the const's
        // initializer, so it HOLLOWS these. The enumerator's walk is
        // hand-rolled and its catch-all arm used to swallow `Item::Const`
        // whole, so the same fns were never reported: measured on
        // `tmp/anyhow/src/nightly.rs` (2026-08-06), a file of SEVEN fns
        // enumerated as FOUR, omitting `Debug::fmt`, `Display::fmt` and
        // `Error::provide` on the build probe's `MyError`.
        let recs = enumerate_fns_in_source(CONST_BLOCK_SRC, "t.rs").unwrap();
        let names: Vec<_> = recs.iter().map(|r| r.fn_name.as_str()).collect();
        assert_eq!(names, vec!["fmt", "fmt", "top_level"], "got {names:?}");
        // Same name twice in one file: the container is what disambiguates
        // them, and a splice that cannot tell them apart refuses on
        // `ambiguous_name`.
        let containers: Vec<_> = recs
            .iter()
            .filter(|r| r.fn_name == "fmt")
            .map(|r| r.container.as_str())
            .collect();
        assert_eq!(
            containers,
            vec!["impl Debug for MyError", "impl Display for MyError"]
        );
        // The enclosing const's cfg has to reach them, or the completion gate
        // cannot tell "unreachable under every baked cfg-set" from "never
        // attempted" — which is the difference between exit 0 and exit 1.
        for r in recs.iter().filter(|r| r.fn_name == "fmt") {
            assert_eq!(r.cfg_predicates, vec!["probe".to_string()]);
        }
    }

    #[test]
    fn every_body_the_skeletoniser_hollows_is_reported_hollow() {
        // THE invariant the asymmetry violated: a body the strip half writes
        // must be one the enumerate half can name again. Every fill route keys
        // on FnRecord — frontier, container index, attribution reverse-map,
        // leaf-first ordering — so a hollowed fn with no record is not hard to
        // fill, it is unaddressable; and both completion stub gates filter
        // records, so it could never be reported either. On the live `anyhow`
        // tree the counts were 3 on disk against 0 reported.
        let stubbed = skeletonise_str(CONST_BLOCK_SRC);
        let on_disk = stubbed.matches("unimplemented!()").count();
        let recs = enumerate_fns_in_source(&stubbed, "t.rs").unwrap();
        let reported = recs.iter().filter(|r| r.has_unimplemented_body).count();
        assert_eq!(on_disk, 3, "skeletoniser hollowed {on_disk}, expected 3");
        assert_eq!(
            on_disk, reported,
            "{on_disk} hollow bodies on disk but {reported} reported by the enumerator"
        );
    }

    #[test]
    fn a_static_initializer_is_walked_too() {
        let src = r#"
            static _X: () = {
                impl Foo {
                    fn hidden(&self) -> u8 { 7 }
                }
            };
        "#;
        let recs = enumerate_fns_in_source(src, "t.rs").unwrap();
        assert_eq!(recs.len(), 1);
        assert_eq!(recs[0].fn_name, "hidden");
    }

    #[test]
    fn a_plain_const_is_not_mistaken_for_a_block() {
        // Only a block initializer holds items; a value initializer must not
        // be walked into, and nothing about ordinary consts may change.
        let recs = enumerate_fns_in_source("const N: usize = 3; pub fn f() -> usize { N }", "t.rs")
            .unwrap();
        assert_eq!(recs.len(), 1);
        assert_eq!(recs[0].fn_name, "f");
    }

    #[test]
    fn visibility_is_the_most_public_declaration_of_a_name() {
        // Type records are keyed by bare name tree-wide, so a crate that
        // declares one twice gets ONE record. anyhow declares `Chain` as `pub`
        // in lib.rs and `pub(crate)` in chain.rs; whichever file the walk
        // reached last used to decide, and on the reconstructed tree that was
        // chain.rs — so the API-conformance check reported `struct: Chain`
        // missing from a tree that declares it publicly.
        let mut types = std::collections::BTreeMap::new();
        let a: syn::File = parse_file("pub(crate) struct Chain<'a> { x: u8 }").unwrap();
        let b: syn::File = parse_file("pub struct Chain<'a> { x: u8 }").unwrap();
        collect_types_and_methods(&a.items, "chain.rs", &mut types);
        collect_types_and_methods(&b.items, "lib.rs", &mut types);
        let rec = types.get("Chain").unwrap();
        assert!(rec.is_exactly_pub, "a pub declaration must not be hidden");

        // …and the other order must give the same answer.
        let mut types2 = std::collections::BTreeMap::new();
        collect_types_and_methods(&b.items, "lib.rs", &mut types2);
        collect_types_and_methods(&a.items, "chain.rs", &mut types2);
        assert!(
            types2.get("Chain").unwrap().is_exactly_pub,
            "walk order decided"
        );
    }

    #[test]
    fn a_traits_visibility_is_also_raised_across_declarations() {
        // `StdError`: a private trait in lib.rs, a `pub trait` in context.rs.
        // The trait branch keeps the FIRST record, so this is the mirror image
        // of the struct case and needs the same rule.
        let priv_t: syn::File = parse_file("trait StdError { }").unwrap();
        let pub_t: syn::File = parse_file("pub trait StdError { }").unwrap();
        let mut types = std::collections::BTreeMap::new();
        collect_types_and_methods(&priv_t.items, "lib.rs", &mut types);
        collect_types_and_methods(&pub_t.items, "context.rs", &mut types);
        assert!(types.get("StdError").unwrap().is_exactly_pub);
    }

    #[test]
    fn type_definition_locatable_in_skeleton_rendering() {
        // The t1.5 layout splice locates a frozen definition inside a skeleton
        // file (prettyplease output) by whitespace-tolerant text match. A
        // token-stream rendering turns `/// doc` into `# [doc = "..."]`, which
        // never matches — every doc-commented private type froze silently.
        let src = "/// A doc'd counter.\n\
                   #[derive(Debug, Default)]\n\
                   pub struct Counter { value: u32 }\n\
                   mod inner {\n\
                       /// Inner doc'd enum.\n\
                       pub enum Reason { A, B }\n\
                   }";
        let ast: syn::File = parse_file(src).unwrap();
        let rendered = prettyplease::unparse(&ast); // what skeletonise writes
        let mut out = std::collections::BTreeMap::new();
        collect_types_and_methods(&ast.items, "lib.rs", &mut out);
        let strip = |s: &str| s.chars().filter(|c| !c.is_whitespace()).collect::<String>();
        for name in ["Counter", "Reason"] {
            let def = &out[name].definition;
            assert!(def.contains("///"), "{name}: doc comment lost:\n{def}");
            assert!(
                !def.contains("doc ="),
                "{name}: token-soup doc attr:\n{def}"
            );
            assert!(
                strip(&rendered).contains(&strip(def)),
                "{name}: definition not locatable in skeleton rendering:\n{def}\n---\n{rendered}"
            );
        }
    }

    #[test]
    fn is_exactly_pub_excludes_restricted_visibility() {
        let src = "pub struct Fully; pub(crate) struct CrateOnly; struct Private;";
        let ast: syn::File = parse_file(src).unwrap();
        let mut out = std::collections::BTreeMap::new();
        collect_types_and_methods(&ast.items, "lib.rs", &mut out);
        assert!(out["Fully"].is_pub && out["Fully"].is_exactly_pub);
        assert!(out["CrateOnly"].is_pub && !out["CrateOnly"].is_exactly_pub);
        assert!(!out["Private"].is_pub && !out["Private"].is_exactly_pub);
    }

    #[test]
    fn fn_record_is_exactly_pub_excludes_restricted_visibility() {
        use std::fs;
        let tmp = std::env::temp_dir().join("skel_extractor_fn_pub_test");
        if tmp.exists() {
            fs::remove_dir_all(&tmp).unwrap();
        }
        fs::create_dir_all(&tmp).unwrap();
        fs::write(
            tmp.join("lib.rs"),
            "pub fn fully() {} pub(crate) fn crate_only() {} fn private() {}",
        )
        .unwrap();
        let recs = enumerate_fns_in_file(&tmp.join("lib.rs"), &tmp).unwrap();
        let by_name: std::collections::BTreeMap<_, _> =
            recs.iter().map(|r| (r.fn_name.as_str(), r)).collect();
        assert!(by_name["fully"].is_pub && by_name["fully"].is_exactly_pub);
        assert!(by_name["crate_only"].is_pub && !by_name["crate_only"].is_exactly_pub);
        assert!(!by_name["private"].is_pub && !by_name["private"].is_exactly_pub);
    }

    #[test]
    fn method_record_is_exactly_pub_excludes_restricted_visibility() {
        let src = "struct S; impl S { pub fn fully(&self) {} pub(crate) fn crate_only(&self) {} fn private(&self) {} }";
        let ast: syn::File = parse_file(src).unwrap();
        let mut out = std::collections::BTreeMap::new();
        collect_types_and_methods(&ast.items, "lib.rs", &mut out);
        let methods = &out["S"].methods;
        let by_name: std::collections::BTreeMap<_, _> =
            methods.iter().map(|m| (m.name.as_str(), m)).collect();
        assert!(by_name["fully"].is_pub && by_name["fully"].is_exactly_pub);
        assert!(by_name["crate_only"].is_pub && !by_name["crate_only"].is_exactly_pub);
        assert!(!by_name["private"].is_pub && !by_name["private"].is_exactly_pub);
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
    fn keeps_cfg_test_mod_unstubbed() {
        // A #[cfg(test)] mod is the original tests, which ARE the reconstruction's
        // oracle — kept in place, its bodies NOT stubbed; only the real impl fn
        // is stubbed. Stubbing the oracle would let any skeleton pass it.
        let src = "pub fn a() -> u32 { 1 } \
                   #[cfg(test)] mod tests { #[test] fn t() { assert_eq!(super::a(), 1); } }";
        let out = skeletonise_str(src);
        assert!(out.contains("mod tests"), "test mod kept: {out}");
        assert!(
            out.contains("assert_eq"),
            "test body intact (not stubbed): {out}"
        );
        assert!(
            out.contains("unimplemented"),
            "impl fn `a` IS stubbed: {out}"
        );
    }

    #[test]
    fn detects_test_attributes() {
        use syn::{ItemFn, parse_str};
        let cases = [
            ("#[test] fn t() {}", true),
            ("#[tokio::test] async fn t() {}", true),
            ("#[async_std::test] async fn t() {}", true),
            ("#[rstest] fn t() {}", true),
            ("#[serial_test::serial] fn t() {}", true),
            ("#[test_case] fn t() {}", true),
            ("fn t() {}", false),
            ("#[inline] fn t() {}", false),
            ("#[cfg(test)] fn t() {}", false),
        ];
        for (src, want) in cases {
            let f: ItemFn = parse_str(src).unwrap();
            assert_eq!(crate::enumerate::attrs_have_test(&f.attrs), want, "{src}");
        }
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
        prettyplease::unparse(&ast)
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
        assert!(
            !out.contains("nightly_only"),
            "nightly_only should be dropped"
        );
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
    fn merge_keeps_impls_distinct_by_trait_generic_args() {
        // FR-025: two `impl AsRef<...> for Error` blocks that differ only in
        // the trait's generic argument must NOT collapse into one impl (which
        // would produce E0201 duplicate + E0053 mismatch).
        let src = r#"
            impl AsRef<dyn StdError + Send + Sync> for Error {
                fn as_ref(&self) -> &(dyn StdError + Send + Sync + 'static) { unimplemented!() }
            }
            impl AsRef<dyn StdError> for Error {
                fn as_ref(&self) -> &(dyn StdError + 'static) { unimplemented!() }
            }
        "#;
        let out = merge_cluster_files(vec![ClusterFile {
            name: "stable".into(),
            source: src.into(),
            cfg_gate: String::new(),
        }])
        .unwrap();
        // Both impl headers survive as separate blocks.
        assert!(out.contains("impl AsRef<dyn StdError + Send + Sync> for Error"));
        assert!(out.contains("impl AsRef<dyn StdError> for Error"));
        // Exactly two impls, each with exactly one as_ref.
        assert_eq!(out.matches("fn as_ref").count(), 2);
        assert_eq!(out.matches("impl AsRef").count(), 2);
    }

    #[test]
    fn merge_keeps_impls_distinct_by_where_clause() {
        // FR-025: same trait/self-type but divergent where-clause → distinct.
        let src = r#"
            impl<'a, T> Mut<'a, T> {
                pub unsafe fn read(self) -> T { unimplemented!() }
            }
            impl<'a, T> Mut<'a, T> where T: ?Sized {
                pub fn as_ptr(self) -> *const T { unimplemented!() }
            }
        "#;
        let out = merge_cluster_files(vec![ClusterFile {
            name: "stable".into(),
            source: src.into(),
            cfg_gate: String::new(),
        }])
        .unwrap();
        assert_eq!(out.matches("impl<'a, T> Mut<'a, T>").count(), 2);
        // `read` must not be fused into the `where T: ?Sized` block.
        assert!(out.contains("fn read"));
        assert!(out.contains("fn as_ptr"));
    }

    #[test]
    fn merge_keeps_cfg_variant_fns_distinct() {
        // Regression (hardcrate cap trap, 2026-06-16): two fns sharing name+sig
        // but differing by their OWN #[cfg] are DISTINCT. The merge must keep two
        // separate gated defs — each with the FILLED body from the cluster where
        // its cfg is live — never collapse them into one with cfgs OR-combined
        // (`any(x, not(x))`), bodies swapped, and a leftover `unimplemented!()`.
        let stable = r#"
            #[cfg(any(extra_cap, feature = "extra_cap"))]
            pub fn cap_limit() -> usize { ::core::unimplemented!() }
            #[cfg(not(any(extra_cap, feature = "extra_cap")))]
            pub fn cap_limit() -> usize { 64 }
        "#;
        let extra = r#"
            #[cfg(any(extra_cap, feature = "extra_cap"))]
            pub fn cap_limit() -> usize { 255 }
            #[cfg(not(any(extra_cap, feature = "extra_cap")))]
            pub fn cap_limit() -> usize { ::core::unimplemented!() }
        "#;
        let out = merge_cluster_files(vec![
            ClusterFile {
                name: "stable".into(),
                source: stable.into(),
                cfg_gate: "not(any(extra_cap, feature = \"extra_cap\"))".into(),
            },
            ClusterFile {
                name: "extra_cap".into(),
                source: extra.into(),
                cfg_gate: "any(extra_cap, feature = \"extra_cap\")".into(),
            },
        ])
        .unwrap();
        // Exactly two cap_limit defs survive.
        assert_eq!(
            out.matches("fn cap_limit").count(),
            2,
            "two cfg-variant defs: {out}"
        );
        // The filled body from each live cluster is kept; no stub survives.
        assert!(out.contains("255"), "extra_cap body kept: {out}");
        assert!(out.contains("64"), "stable body kept: {out}");
        assert!(
            !out.contains("unimplemented"),
            "no stub may survive merge: {out}"
        );
        // No OR-combined garbage cfg, and the correct per-variant gates are present.
        assert!(
            !out.replace(' ', "").contains("any(any("),
            "no any(x, not(x)) garbage: {out}"
        );
        assert!(
            out.contains("not(any(extra_cap"),
            "not-variant gate kept: {out}"
        );
    }

    #[test]
    fn merge_preserves_cfg_gated_test_bodies() {
        // FR-018 oracle immutability: cfg-gated #[test] fns (the reconstruction
        // oracle) must survive merge byte-for-byte — never reconciled/overwritten
        // (the hardcrate run corrupted `default_cap_limit`'s body to `255`).
        let tests_src = r#"
            #[cfg(test)]
            mod tests {
                #[cfg(not(any(extra_cap, feature = "extra_cap")))]
                #[test]
                fn default_cap_limit() { assert_eq!(cap_limit(), 64); }
                #[cfg(any(extra_cap, feature = "extra_cap"))]
                #[test]
                fn extra_cap_limit() { assert_eq!(cap_limit(), 255); }
            }
        "#;
        let out = merge_cluster_files(vec![
            ClusterFile {
                name: "stable".into(),
                source: tests_src.into(),
                cfg_gate: "not(any(extra_cap, feature = \"extra_cap\"))".into(),
            },
            ClusterFile {
                name: "extra_cap".into(),
                source: tests_src.into(),
                cfg_gate: "any(extra_cap, feature = \"extra_cap\")".into(),
            },
        ])
        .unwrap();
        let flat = out.replace([' ', '\n'], "");
        assert!(
            flat.contains("assert_eq!(cap_limit(),64)"),
            "default oracle intact: {out}"
        );
        assert!(
            flat.contains("assert_eq!(cap_limit(),255)"),
            "extra_cap oracle intact: {out}"
        );
        assert_eq!(out.matches("fn default_cap_limit").count(), 1);
        assert_eq!(out.matches("fn extra_cap_limit").count(), 1);
    }

    #[test]
    fn merge_normalizes_multiline_doc_to_uniform_line_docs() {
        // FR-033: a doc example must not render with mixed /// and /** */ —
        // that splits the ``` fence and drops `# struct Foo;` from the doctest.
        let src = "#[doc = \" ```\\n # struct Foo;\\n let _ = Foo;\\n ```\"]\npub fn f() {}\n";
        let out = merge_cluster_files(vec![ClusterFile {
            name: "stable".into(),
            source: src.into(),
            cfg_gate: String::new(),
        }])
        .unwrap();
        assert!(!out.contains("/**"), "block doc leaked:\n{out}");
        assert!(!out.contains("*/"), "block doc close leaked:\n{out}");
        assert!(
            out.contains("struct Foo;"),
            "hidden setup line lost:\n{out}"
        );
        // every doc line is a `///` line-doc
        assert_eq!(out.matches("///").count(), 4, "{out}");
    }

    #[test]
    fn fn_cfg_predicates_are_not_duplicated() {
        use std::fs;
        let tmp = std::env::temp_dir().join("skel_cfgdup_test");
        if tmp.exists() {
            fs::remove_dir_all(&tmp).unwrap();
        }
        fs::create_dir_all(tmp.join("src")).unwrap();
        fs::write(
            tmp.join("src").join("lib.rs"),
            "#[cfg(feature = \"std\")]\npub fn foo() {}\npub fn plain() {}\n",
        )
        .unwrap();
        let fns = enumerate_fns_in_tree(&tmp).unwrap();
        let foo = fns.iter().find(|f| f.fn_name == "foo").unwrap();
        assert_eq!(
            foo.cfg_predicates.len(),
            1,
            "cfg must not be double-counted"
        );
        let plain = fns.iter().find(|f| f.fn_name == "plain").unwrap();
        assert!(
            plain.cfg_predicates.is_empty(),
            "uncfg'd fn has no predicates"
        );
    }

    #[test]
    fn enumerates_pub_consts_and_statics() {
        use std::fs;
        let tmp = std::env::temp_dir().join("skel_consts_test");
        if tmp.exists() {
            fs::remove_dir_all(&tmp).unwrap();
        }
        fs::create_dir_all(tmp.join("src")).unwrap();
        fs::write(
            tmp.join("src").join("lib.rs"),
            r#"
            pub const MAX: usize = 10;
            pub static NAME: &str = "x";
            const PRIVATE: u8 = 1;
            pub mod inner { pub const NESTED: i32 = 2; }
            "#,
        )
        .unwrap();
        let cs = enumerate_consts_in_tree(&tmp).unwrap();
        let pubs: Vec<_> = cs
            .iter()
            .filter(|c| c.is_pub)
            .map(|c| c.name.as_str())
            .collect();
        assert!(pubs.contains(&"MAX") && pubs.contains(&"NAME") && pubs.contains(&"NESTED"));
        assert!(cs.iter().any(|c| c.name == "PRIVATE" && !c.is_pub));
        assert_eq!(cs.iter().find(|c| c.name == "MAX").unwrap().ty, "usize");
    }

    #[test]
    fn detects_dispatch_family_and_excludes_spurious() {
        // FR-027: shared method + macro dispatch = family; shared method
        // WITHOUT a macro that calls it = not a family.
        use std::fs;
        let tmp = std::env::temp_dir().join("skel_dispatch_test");
        if tmp.exists() {
            fs::remove_dir_all(&tmp).unwrap();
        }
        fs::create_dir_all(tmp.join("src")).unwrap();
        fs::write(
            tmp.join("src").join("lib.rs"),
            r#"
            pub struct Adhoc;
            pub struct Trait;
            pub trait AdhocKind { fn kind(&self) -> Adhoc; }
            pub trait TraitKind { fn kind(&self) -> Trait; }
            impl Adhoc { fn new(self) -> Error { unimplemented!() } }
            impl Trait { fn new(self) -> Error { unimplemented!() } }
            // spurious: shared method `other` but no macro dispatches it
            pub trait A { fn other(&self) -> u8; }
            pub trait B { fn other(&self) -> u8; }
            macro_rules! make { ($e:expr) => { ($e).kind().new() } }
            "#,
        )
        .unwrap();
        let fams = enumerate_dispatch_families_in_tree(&tmp).unwrap();
        let kind = fams
            .iter()
            .find(|f| f.method == "kind")
            .expect("kind family");
        assert_eq!(kind.traits, vec!["AdhocKind", "TraitKind"]);
        assert_eq!(kind.macros, vec!["make"]);
        // chain-following: `.kind().new()` pulls the tag types + `new` in
        assert_eq!(kind.return_types, vec!["Adhoc", "Trait"]);
        assert_eq!(kind.chained_methods, vec!["new"]);
        assert!(
            fams.iter().all(|f| f.method != "other"),
            "spurious `other` leaked"
        );
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
        assert!(
            reports
                .iter()
                .all(|r| !r.relative_path.contains(".gen_tests_"))
        );
    }

    #[test]
    fn mod_record_captures_own_visibility() {
        use std::fs;
        let tmp = std::env::temp_dir().join("skel_extractor_mod_vis_test");
        if tmp.exists() {
            fs::remove_dir_all(&tmp).unwrap();
        }
        fs::create_dir_all(&tmp).unwrap();
        fs::write(
            tmp.join("lib.rs"),
            "pub mod pub_mod; pub(crate) mod crate_mod; mod private_mod;",
        )
        .unwrap();
        let recs = enumerate_mods_in_file(&tmp.join("lib.rs"), &tmp).unwrap();
        let by_name: std::collections::BTreeMap<_, _> = recs
            .iter()
            .map(|m| (m.name.as_str(), m.vis.as_str()))
            .collect();
        assert_eq!(by_name["pub_mod"], "public");
        assert_eq!(by_name["crate_mod"], "restricted");
        assert_eq!(by_name["private_mod"], "private");
    }

    #[test]
    fn tree_walk_follows_a_symlinked_tests_dir_and_skips_artifact_roots() {
        // A per-cluster tree does not own its tests: the pipeline symlinks
        // `tests` into each one. WalkDir does not follow links by default, so
        // this walk returned a cluster tree's src fns and NOT one of the fns in
        // its own tests/ — measured on the live anyhow tree 2026-08-01, 186 src
        // records and 0 of the 105 test records. The sweep inventory indexes
        // test fns by crate-relative path, so every tests/ file fell back to a
        // scan that counts but cannot NAME, and the crash localizer lost the
        // vanished names it aims with.
        //
        // `target-asan` is the second cargo artifact root the ASAN sweep builds;
        // it is not named `target`, so the walk used to descend into it and
        // yield trybuild scratch sources as if they were crate code.
        use std::fs;
        let tmp = std::env::temp_dir().join("skel_extractor_symlink_walk_test");
        if tmp.exists() {
            fs::remove_dir_all(&tmp).unwrap();
        }
        let tree = tmp.join("tree");
        let real_tests = tmp.join("real_tests");
        fs::create_dir_all(tree.join("src")).unwrap();
        fs::create_dir_all(tree.join("target-asan")).unwrap();
        fs::create_dir_all(&real_tests).unwrap();
        fs::write(tree.join("src/lib.rs"), "pub fn in_src() {}").unwrap();
        fs::write(tree.join("target-asan/scratch.rs"), "fn in_artifacts() {}").unwrap();
        fs::write(real_tests.join("t.rs"), "#[test]\nfn in_tests() {}").unwrap();
        std::os::unix::fs::symlink(&real_tests, tree.join("tests")).unwrap();

        let recs = enumerate_fns_in_tree(&tree).unwrap();
        let names: std::collections::BTreeSet<_> =
            recs.iter().map(|r| r.fn_name.as_str()).collect();
        assert!(names.contains("in_src"), "src fn missing: {names:?}");
        assert!(
            names.contains("in_tests"),
            "symlinked tests/ fn missing — the walk is not following links: {names:?}"
        );
        assert!(
            !names.contains("in_artifacts"),
            "walked a cargo artifact root: {names:?}"
        );
        // Paths stay crate-relative through the link, which is what the
        // inventory's per-file index is keyed on.
        let files: std::collections::BTreeSet<_> = recs.iter().map(|r| r.file.as_str()).collect();
        assert!(
            files.contains("tests/t.rs"),
            "unexpected rel paths: {files:?}"
        );
    }

    // ── the cluster axis, the anonymous const, the comments and the preference ──

    const NG: &str = "any(egma, feature = \"egma\")";
    const ST: &str = "not(any(egma, feature = \"egma\"))";

    fn two_clusters(nightly: &str, stable: &str) -> Vec<ClusterFile> {
        vec![
            ClusterFile {
                name: "nightly".into(),
                source: nightly.into(),
                cfg_gate: NG.into(),
            },
            ClusterFile {
                name: "stable".into(),
                source: stable.into(),
                cfg_gate: ST.into(),
            },
        ]
    }

    #[test]
    fn merge_keeps_both_bodies_when_the_items_own_cfg_is_off_the_cluster_axis() {
        // `#[cfg(any(feature = "std", not(no_core_error)))]` mentions no atom
        // the clusters differ on, so the item is LIVE IN BOTH and each cluster
        // filled it. Emitting one body silently discards the other cluster's
        // answer — measured on `anyhow` 2026-08-05 for `context_downcast`,
        // `error_mut`, `new`, `object_reallocate_boxed` and `root_cause`, all
        // resolved by nothing but `"nightly" < "stable"`.
        let mk = |body: &str| {
            format!(
                "#[cfg(any(feature = \"std\", not(no_core_error)))]\n\
                 fn root_cause() -> u8 {{ {body} }}\n"
            )
        };
        let out = merge_cluster_files(two_clusters(&mk("1"), &mk("2"))).unwrap();
        assert_eq!(out.matches("fn root_cause").count(), 2, "both kept:\n{out}");
        assert!(out.contains('1') && out.contains('2'), "{out}");
        assert!(
            out.contains("egma"),
            "each variant gated by cluster:\n{out}"
        );
    }

    #[test]
    fn merge_still_collapses_a_variant_whose_own_cfg_is_on_the_cluster_axis() {
        // The original premise, now checked rather than assumed: `egma` IS the
        // axis, so the item is live in one cluster and stubbed in the other and
        // exactly one body is right. Stacking a cluster gate here would yield
        // `any(x, not(x))` garbage.
        let out = merge_cluster_files(two_clusters(
            "#[cfg(egma)]\nfn provide() -> u8 { 7 }\n",
            "#[cfg(egma)]\nfn provide() -> u8 { ::core::unimplemented!() }\n",
        ))
        .unwrap();
        assert_eq!(out.matches("fn provide").count(), 1, "one def:\n{out}");
        assert!(out.contains('7'), "filled body kept:\n{out}");
        assert!(!out.contains("unimplemented"), "stub survived:\n{out}");
    }

    #[test]
    fn merge_keeps_every_anonymous_const_in_a_file() {
        // `const _` has the literal ident `_`, so keying by ident put both in
        // one bucket and emitted one. On `anyhow` the loss was
        // `option_env!("RUSTC_BOOTSTRAP")` from src/nightly.rs — present
        // identically in BOTH inputs, emitted zero times.
        let src = "#[cfg(probe)]\nconst _: () = { let _x = 1; };\n\
                   #[cfg(probe)]\nconst _: Option<&str> = option_env!(\"BOOTSTRAP\");\n";
        let out = merge_cluster_files(two_clusters(src, src)).unwrap();
        assert_eq!(
            out.matches("const _").count(),
            2,
            "both consts kept:\n{out}"
        );
        assert!(out.contains("BOOTSTRAP"), "{out}");
    }

    #[test]
    fn merge_preserves_line_comments_in_bodies_and_above_items() {
        // Non-doc comments are not tokens, so a syn round-trip drops them. The
        // note this test carries is the shape of the one actually lost on
        // `anyhow`: the reason an unsafe cast is sound, written by the model
        // that got the fn right.
        let src = "// why this fn exists\n\
                   fn drop_front() {\n    \
                   // Cast to ManuallyDrop<E> to suppress the double-free.\n    \
                   let _ = 1;\n}\n\
                   impl Foo {\n    \
                   fn m(&self) {\n        // member comment\n        let _ = 2;\n    }\n}\n";
        let out = merge_cluster_files(two_clusters(src, src)).unwrap();
        assert!(out.contains("// why this fn exists"), "header:\n{out}");
        assert!(out.contains("suppress the double-free"), "body:\n{out}");
        assert!(out.contains("// member comment"), "impl member:\n{out}");
        // Still parses — a splice that broke the file would be worse than the loss.
        syn::parse_file(&out).expect("merged output must parse");
    }

    #[test]
    fn merge_prefers_the_named_clusters_body_for_an_off_axis_divergence() {
        // `stable` was frozen 2026-07-30 with `test_downcast` failing and can
        // never be re-swept; `nightly` fixed it on 08-04. Gating by provenance
        // ships the frozen body to the default build, which is why the merged
        // tree cored while both cluster trees were the best they had ever been.
        let mk = |body: &str| format!("fn object_downcast() -> u8 {{ {body} }}\n");
        let opts = MergeOptions {
            prefer_cluster: Some("nightly".into()),
        };
        let out = merge_cluster_files_with(two_clusters(&mk("1"), &mk("2")), &opts).unwrap();
        assert_eq!(
            out.text.matches("fn object_downcast").count(),
            1,
            "{}",
            out.text
        );
        assert!(out.text.contains('1'), "nightly's body:\n{}", out.text);
        assert!(
            !out.text.contains('2'),
            "stable's body survived:\n{}",
            out.text
        );
        assert_eq!(out.preferred, vec!["fn::object_downcast -> nightly"]);
    }

    #[test]
    fn merge_never_prefers_a_declaration_only_a_body() {
        // `anyhow`'s `TraitKind::anyhow_kind` is required in nightly's tree and
        // defaulted in stable's, and stable's `impl TraitKind for E` is empty
        // because it leans on that default. Preferring nightly's DECLARATION
        // kept the requirement and left the impl satisfying it gated to the
        // other cluster: E0046, the crate stopped compiling.
        let nightly = "trait TraitKind {\n    fn anyhow_kind(&self) -> u8;\n}\n";
        let stable = "trait TraitKind {\n    fn anyhow_kind(&self) -> u8 { 0 }\n}\n";
        let opts = MergeOptions {
            prefer_cluster: Some("nightly".into()),
        };
        let out = merge_cluster_files_with(two_clusters(nightly, stable), &opts).unwrap();
        assert!(
            out.preferred.is_empty(),
            "declaration preferred: {:?}",
            out.preferred
        );
        assert!(
            out.text.contains("egma"),
            "both variants gated:\n{}",
            out.text
        );
    }

    #[test]
    fn merge_reports_item_counts_so_a_drop_is_visible() {
        // items_in_max is the RICHEST single input, not the sum: an item every
        // cluster carries is counted once per cluster going in and once coming
        // out, so `items_out < items_in_max` means something was dropped.
        // Against the sum the comparison is vacuous and catches nothing.
        let src = "fn a() {}\nimpl S {\n    fn b(&self) {}\n    fn c(&self) {}\n}\n";
        let out =
            merge_cluster_files_with(two_clusters(src, src), &MergeOptions::default()).unwrap();
        assert_eq!(out.items_in_max, 4, "fn a + impl S + two members");
        assert!(
            out.items_out >= out.items_in_max,
            "merge dropped items: in={} out={}",
            out.items_in_max,
            out.items_out
        );
    }
}
