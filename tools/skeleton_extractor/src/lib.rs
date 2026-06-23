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
        // FR-018: a #[cfg(test)] mod is the original tests (oracle) — kept in place,
        // its bodies NOT stubbed; only the real impl fn is stubbed.
        let src = "pub fn a() -> u32 { 1 } \
                   #[cfg(test)] mod tests { #[test] fn t() { assert_eq!(super::a(), 1); } }";
        let out = skeletonise_str(src);
        assert!(out.contains("mod tests"), "test mod kept: {out}");
        assert!(out.contains("assert_eq"), "test body intact (not stubbed): {out}");
        assert!(out.contains("unimplemented"), "impl fn `a` IS stubbed: {out}");
    }

    #[test]
    fn detects_test_attributes() {
        use syn::{parse_str, ItemFn};
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
        assert_eq!(out.matches("fn cap_limit").count(), 2, "two cfg-variant defs: {out}");
        // The filled body from each live cluster is kept; no stub survives.
        assert!(out.contains("255"), "extra_cap body kept: {out}");
        assert!(out.contains("64"), "stable body kept: {out}");
        assert!(!out.contains("unimplemented"), "no stub may survive merge: {out}");
        // No OR-combined garbage cfg, and the correct per-variant gates are present.
        assert!(!out.replace(' ', "").contains("any(any("), "no any(x, not(x)) garbage: {out}");
        assert!(out.contains("not(any(extra_cap"), "not-variant gate kept: {out}");
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
            ClusterFile { name: "stable".into(), source: tests_src.into(),
                          cfg_gate: "not(any(extra_cap, feature = \"extra_cap\"))".into() },
            ClusterFile { name: "extra_cap".into(), source: tests_src.into(),
                          cfg_gate: "any(extra_cap, feature = \"extra_cap\")".into() },
        ])
        .unwrap();
        let flat = out.replace([' ', '\n'], "");
        assert!(flat.contains("assert_eq!(cap_limit(),64)"), "default oracle intact: {out}");
        assert!(flat.contains("assert_eq!(cap_limit(),255)"), "extra_cap oracle intact: {out}");
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
}
