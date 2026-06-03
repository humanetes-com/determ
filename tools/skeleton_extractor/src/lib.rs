// Skeleton extractor — Phase A of the 4-phase TASK 2 reconstruction.
//
// Reads a pre-strip Rust source tree, emits per-file "skeletons" where:
//   - All type declarations (pub + private) keep their full body.
//   - All function/method bodies are replaced with `unimplemented!()`.
//   - All `#[cfg(test)] mod ...` items are dropped.
//   - Macros, imports, attributes, and consts are preserved verbatim.
//
// The skeleton is the structural ground truth that Phase B/C/D fill in.

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
    use super::*;
    use quote::quote;
    use syn::parse_file;
    use syn::visit_mut::VisitMut;

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
