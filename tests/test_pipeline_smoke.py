#!/usr/bin/env python3
"""Smoke tests for pipeline coverage measurement and gen_tests helpers.

Two phases:
  - Pure-Python tests (no cargo): verify gen_tests sibling-file mechanics.
    Fast (sub-second), always run.
  - Cargo coverage test: run the configured COVERAGE_BACKEND on
    fixtures/tiny/ and assert the measurement returns sensible numbers.
    Slow (~30-60s), opt-in via --full or the env var SMOKE_FULL=1.

Invocation:
    .venv/bin/python tests/test_pipeline_smoke.py           # pure-python only
    .venv/bin/python tests/test_pipeline_smoke.py --full    # + cargo phase

Exit non-zero on first failure.
"""
import os
import re  # noqa: F401  — used by individual test bodies
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "fixtures" / "tiny"

sys.path.insert(0, str(REPO))


def _setup_workspace(name: str = "fixture-tiny-test") -> Path:
    workspace = REPO / "tmp" / name
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE, workspace)
    return workspace


# ─── Pure-Python phase ──────────────────────────────────────────────────────

def test_gen_tests_helpers() -> None:
    """Verify gen_tests sibling-file write / read / revert / strip semantics."""
    import pipeline

    workspace = _setup_workspace("smoke-gen-tests")
    src = workspace / "src" / "lib.rs"
    pristine = src.read_text()

    assert pipeline._gen_tests_count(src) == 0, "expected zero siblings initially"
    assert pipeline._next_gen_tests_n(src) == 1, "next-n should be 1 when empty"

    block_1 = """#[cfg(test)]
#[allow(unused)]
mod gen_tests_1 {
    use super::*;
    #[test]
    fn t() { assert_eq!(add(1, 1), 2); }
}"""
    pipeline._write_gen_tests_pass(src, pristine, block_1, "#[allow(clippy::all)]", 1)
    sibling_1 = pipeline._gen_tests_path(src, 1)
    assert sibling_1.exists(), f"expected sibling {sibling_1}"
    assert sibling_1.name == "lib.gen_tests_1.rs"
    body = sibling_1.read_text()
    assert "use super::*;" in body
    assert "fn t" in body
    # Outer mod wrapper stripped from body.
    assert "mod gen_tests_1" not in body
    # Source got the path-mod decl.
    src_text = src.read_text()
    assert 'mod gen_tests_1;' in src_text
    assert '#[path = "lib.gen_tests_1.rs"]' in src_text
    assert "#[allow(clippy::all)]" in src_text
    assert pipeline._gen_tests_count(src) == 1
    assert pipeline._next_gen_tests_n(src) == 2

    # Read-all re-wraps for LLM context.
    all_text = pipeline._read_all_gen_tests(src)
    assert all_text.startswith("#[cfg(test)]\nmod gen_tests_1 {")

    # Add a second pass.
    block_2 = "mod gen_tests_2 {\n    use super::*;\n    #[test] fn u() {}\n}"
    pipeline._write_gen_tests_pass(src, src.read_text(), block_2, "#[allow(clippy::all)]", 2)
    assert pipeline._gen_tests_count(src) == 2
    assert pipeline._next_gen_tests_n(src) == 3

    # Strip decls from the source text (helper used in many places).
    src_text = src.read_text()
    stripped = pipeline._strip_gen_tests_decls(src_text)
    assert "mod gen_tests_1" not in stripped
    assert "mod gen_tests_2" not in stripped
    assert "pub fn add" in stripped, "original source content must remain"

    # Revert second pass: delete sibling, restore src to the pre-pass text.
    src_before_pass_2 = stripped + "\n" + pipeline._build_gen_tests_decl(
        "lib.gen_tests_1.rs", 1, "#[allow(clippy::all)]"
    )
    sibling_2 = pipeline._gen_tests_path(src, 2)
    pipeline._revert_gen_tests_pass(src, src_before_pass_2, sibling_2)
    assert not sibling_2.exists()
    assert pipeline._gen_tests_count(src) == 1

    # Strip everything.
    n = pipeline._strip_all_gen_tests_artifacts(src)
    assert n == 1
    assert pipeline._gen_tests_count(src) == 0
    final = src.read_text()
    assert "mod gen_tests_1" not in final
    assert "pub fn add" in final

    # is_test_file recognises siblings (so coverage loops skip them).
    fake_sibling = workspace / "src" / "foo.gen_tests_3.rs"
    fake_sibling.write_text("// placeholder\n")
    assert pipeline.is_test_file(fake_sibling, workspace), \
        "is_test_file must return True for gen_tests siblings"
    fake_sibling.unlink()

    print("  OK  gen_tests helpers")


def test_merge_fills_preserving_existing() -> None:
    """Phase C merge must (a) fill `unimplemented!()` bodies from model output,
    (b) preserve all non-function content from disk (imports, types, attrs),
    and (c) never overwrite an already-filled body.
    """
    import pipeline

    disk = """\
#![allow(unused)]
use std::fmt::Debug;

pub mod sub;

pub struct Foo {
    pub a: u32,
}

pub fn known() -> u32 { 7 }

pub fn stub_a(x: u32) -> u32 { ::core::unimplemented!() }

pub fn stub_b() -> &'static str { ::core::unimplemented!() }
"""
    # Model returns a thinner file: drops the `use`, drops the `mod sub`, drops
    # the struct, drops `known`, but provides bodies for both stubs.
    model = """\
pub fn stub_a(x: u32) -> u32 { x * 2 }

pub fn stub_b() -> &'static str { "hello" }
"""
    merged = pipeline._merge_fills_preserving_existing(disk, model)
    # Preserved from disk:
    assert "#![allow(unused)]" in merged
    assert "use std::fmt::Debug;" in merged
    assert "pub mod sub;" in merged
    assert "pub struct Foo" in merged
    assert "pub fn known() -> u32 { 7 }" in merged
    # Filled from model:
    assert "x * 2" in merged
    assert '"hello"' in merged
    # unimplemented stubs are gone:
    assert "unimplemented" not in merged
    print("  OK  _merge_fills_preserving_existing (basic)")

    # Already-filled body is never overwritten by the model.
    disk2 = "pub fn already() -> u32 { 42 }\n"
    model2 = "pub fn already() -> u32 { 0 }\n"
    merged2 = pipeline._merge_fills_preserving_existing(disk2, model2)
    assert "42" in merged2
    assert "0 }" not in merged2
    print("  OK  _merge_fills_preserving_existing (preserves filled)")

    # Empty model output returns disk verbatim (we never lose structure).
    disk3 = "pub fn x() -> u32 { ::core::unimplemented!() }\n"
    merged3 = pipeline._merge_fills_preserving_existing(disk3, "")
    assert merged3 == disk3
    print("  OK  _merge_fills_preserving_existing (empty model)")


def test_fn_block_extract_and_merge() -> None:
    """P3.2: per-function output format. _extract_fn_blocks parses <FN sig=...>
    blocks; _merge_fn_blocks_into_disk substitutes bodies into disk by sig.
    """
    import pipeline

    # Use double quotes for outer sig and embed single-quote lifetime as-is.
    response = (
        "prose the LLM might emit before...\n\n"
        '<FN sig="pub fn stub_a(x: u32) -> u32">\n'
        "    x.wrapping_add(1)\n"
        "</FN>\n\n"
        "junk between\n\n"
        "<FN sig=\"pub fn stub_b() -> &'static str\">\n"
        '    "hello"\n'
        "</FN>\n"
    )
    blocks = pipeline._extract_fn_blocks(response)
    assert "pub fn stub_a(x: u32) -> u32" in blocks
    assert blocks["pub fn stub_a(x: u32) -> u32"] == "x.wrapping_add(1)"
    assert len(blocks) == 2
    print("  OK  _extract_fn_blocks")

    disk = """\
#![allow(unused)]
use std::fmt;

pub fn stub_a(x: u32) -> u32 { ::core::unimplemented!() }
pub fn already() -> u32 { 7 }
pub fn stub_b() -> &'static str { ::core::unimplemented!() }
"""
    merged = pipeline._merge_fn_blocks_into_disk(disk, blocks)
    assert "#![allow(unused)]" in merged
    assert "use std::fmt;" in merged
    assert "pub fn already() -> u32 { 7 }" in merged
    assert "x.wrapping_add(1)" in merged
    assert '"hello"' in merged
    assert "unimplemented" not in merged
    print("  OK  _merge_fn_blocks_into_disk (basic)")

    # Empty dict returns disk verbatim.
    assert pipeline._merge_fn_blocks_into_disk(disk, {}) == disk
    print("  OK  _merge_fn_blocks_into_disk (empty)")


def test_format_skeleton_index() -> None:
    """P3.3: signatures-only skeleton. Replaces `{...}` bodies with `;`."""
    import pipeline

    files = [(
        "src/lib.rs",
        """\
pub fn foo() -> u32 { ::core::unimplemented!() }
pub struct S { pub a: u32 }
pub fn bar(x: u32) -> u32 {
    let y = x + 1;
    y
}
""",
    )]
    out = pipeline._format_skeleton_index(files)
    assert "src/lib.rs" in out
    assert "mode=\"signatures-only\"" in out
    # Bodies should be replaced with `;`.
    assert "unimplemented" not in out
    assert "pub fn foo() -> u32;" in out or "pub fn foo() -> u32 ;" in out
    # Struct definition preserved.
    assert "pub struct S { pub a: u32 }" in out
    print("  OK  _format_skeleton_index")


def test_prune_redundant_clusters() -> None:
    """Plan 4 + cluster pruning: drop clusters that duplicate defaults or
    gate too few items."""
    import pipeline
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "src"
        src.mkdir()
        # 6 items gated by 'nightly', 1 item by 'rare_cfg'
        (src / "lib.rs").write_text("""
#[cfg(nightly)] fn a() {}
#[cfg(nightly)] fn b() {}
#[cfg(nightly)] fn c() {}
#[cfg(nightly)] fn d() {}
#[cfg(nightly)] fn e() {}
#[cfg(nightly)] fn f() {}
#[cfg(rare_cfg)] fn g() {}
""")
        cargo = Path(td) / "Cargo.toml"
        cargo.write_text('[features]\ndefault = ["std"]\nstd = []\n')

        clusters = [
            pipeline.BuildCluster(name="stable", env_extra={}, tarpaulin_exclude_files=[],
                                  excluded_cfg=[], active_cfg=[], prompt_nightly_rule="",
                                  is_default=True),
            pipeline.BuildCluster(name="std", env_extra={"RUSTFLAGS": "--cfg std"},
                                  tarpaulin_exclude_files=[], excluded_cfg=[],
                                  active_cfg=["std"], prompt_nightly_rule="",
                                  is_default=False),
            pipeline.BuildCluster(name="nightly", env_extra={"RUSTFLAGS": "--cfg nightly"},
                                  tarpaulin_exclude_files=[], excluded_cfg=[],
                                  active_cfg=["nightly"], prompt_nightly_rule="",
                                  is_default=False),
            pipeline.BuildCluster(name="rare", env_extra={"RUSTFLAGS": "--cfg rare_cfg"},
                                  tarpaulin_exclude_files=[], excluded_cfg=[],
                                  active_cfg=["rare_cfg"], prompt_nightly_rule="",
                                  is_default=False),
        ]
        kept = pipeline._prune_redundant_clusters(clusters, Path(td), cargo_toml_path=cargo,
                                                   min_gated_items=5)
        kept_names = {c.name for c in kept}
        # stable (baseline) → kept
        assert "stable" in kept_names
        # std → dropped (cfg is in default features)
        assert "std" not in kept_names
        # nightly → kept (gates 6 items, above threshold)
        assert "nightly" in kept_names
        # rare → dropped (gates only 1 item, below threshold)
        assert "rare" not in kept_names
        print(f"  OK  _prune_redundant_clusters (4 → {len(kept)}: {sorted(kept_names)})")


def test_filter_tests_for_cluster() -> None:
    """Plan 4: top-level #![cfg(...)] filter on test files."""
    import pipeline, dataclasses

    cluster_stable = pipeline.BuildCluster(
        name="stable", env_extra={}, tarpaulin_exclude_files=[],
        excluded_cfg=["nightly"],
        active_cfg=['feature = "std"'],
        prompt_nightly_rule="",
    )
    tests = [
        ("tests/always.rs", "fn t1() {}"),
        ("tests/needs_nightly.rs", "#![cfg(nightly)]\n\nfn t2() {}"),
        ("tests/needs_std.rs", '#![cfg(feature = "std")]\n\nfn t3() {}'),
        ("tests/no_std.rs", '#![cfg(not(feature = "std"))]\n\nfn t4() {}'),
        ("tests/unknown.rs", "#![cfg(some_random)]\n\nfn t5() {}"),
    ]
    filtered = pipeline._filter_tests_for_cluster(tests, cluster_stable)
    rels = {r for r, _ in filtered}
    assert "tests/always.rs" in rels
    assert "tests/needs_nightly.rs" not in rels  # nightly is false in stable
    assert "tests/needs_std.rs" in rels
    assert "tests/no_std.rs" not in rels  # cfg(not(feature="std")) is false
    assert "tests/unknown.rs" in rels  # undecidable → keep
    print("  OK  _filter_tests_for_cluster")


def test_eval_cfg_pred() -> None:
    """Plan 4: cfg predicate evaluator handles all/any/not."""
    import pipeline
    true_set = {'feature="std"', 'nightly'}
    false_set = {'feature="alloc"'}
    e = pipeline._eval_cfg_pred
    assert e('feature = "std"', true_set, false_set) is True
    assert e('feature = "alloc"', true_set, false_set) is False
    assert e('nightly', true_set, false_set) is True
    assert e('not(nightly)', true_set, false_set) is False
    assert e('all(nightly, feature = "std")', true_set, false_set) is True
    assert e('any(feature = "alloc", nightly)', true_set, false_set) is True
    assert e('not(feature = "alloc")', true_set, false_set) is True
    assert e('something_unknown', true_set, false_set) is None
    assert e('all(nightly, something_unknown)', true_set, false_set) is None
    print("  OK  _eval_cfg_pred")


def test_file_cfg_predicate_adjacent_mod_decls() -> None:
    """Regression: when two `mod foo;` decls sit on adjacent lines and the
    FIRST one has a `#[cfg(...)]` attribute, `_file_cfg_predicate` must
    return the predicate ONLY for the gated mod, not for its unrelated
    neighbour below. The old regex-based scan walked backward over lines
    and greedily harvested any `#[cfg(...)]` token it found, conflating
    the two mod decls.

    Real case caught in the wild: anyhow's `lib.rs` has
        # [cfg (error_generic_member_access)] mod nightly ;
        mod ptr ;
    on consecutive lines. The bug labeled `src/ptr.rs` as gated by
    `error_generic_member_access`, which evaluated false in the stable
    cluster, so all 19 unimplemented methods in `ptr.rs` were silently
    filtered out of the per-fn fill loop. 3 boxed-error tests panicked
    forever at `unimplemented!()` because the pipeline never queued
    those fns.
    """
    import pipeline
    import tempfile
    import os
    with tempfile.TemporaryDirectory(prefix="cfg_pred_test_") as td:
        root = Path(td)
        src = root / "src"
        src.mkdir()
        # Reproduce the anyhow lib.rs pattern: gated mod, then ungated
        # mod on the very next line, no blank line between them. Spaces
        # around the cfg tokens match how the skeleton extractor emits
        # them in real workspaces.
        (src / "lib.rs").write_text(
            " # [cfg (gated_feature)] mod nightly ;\n"
            " mod ptr ;\n"
            " mod wrapper ;\n"
        )
        (src / "nightly.rs").write_text("")
        (src / "ptr.rs").write_text("")
        (src / "wrapper.rs").write_text("")

        # AST path (requires the extractor binary). Skip if not built.
        if pipeline._SKELETON_EXTRACTOR_BIN.exists():
            pipeline._invalidate_mod_records_cache()
            assert pipeline._file_cfg_predicate(root, "src/nightly.rs") == \
                "gated_feature", "AST path: nightly should be gated"
            assert pipeline._file_cfg_predicate(root, "src/ptr.rs") is None, \
                "AST path: ptr must NOT inherit neighbour's cfg"
            assert pipeline._file_cfg_predicate(root, "src/wrapper.rs") is None, \
                "AST path: wrapper must NOT inherit any cfg"
            pipeline._invalidate_mod_records_cache()

        # Regex fallback path: force it by pointing _SKELETON_EXTRACTOR_BIN
        # at a non-existent file. The fallback must also resist conflation
        # via its "stop on declaration keyword" guard.
        orig = pipeline._SKELETON_EXTRACTOR_BIN
        try:
            pipeline._SKELETON_EXTRACTOR_BIN = Path("/nonexistent/extractor")
            pipeline._invalidate_mod_records_cache()
            assert pipeline._file_cfg_predicate(root, "src/nightly.rs") == \
                "gated_feature", "fallback: nightly should be gated"
            assert pipeline._file_cfg_predicate(root, "src/ptr.rs") is None, \
                "fallback: ptr must NOT inherit neighbour's cfg"
            assert pipeline._file_cfg_predicate(root, "src/wrapper.rs") is None, \
                "fallback: wrapper must NOT inherit any cfg"
        finally:
            pipeline._SKELETON_EXTRACTOR_BIN = orig
            pipeline._invalidate_mod_records_cache()
    print("  OK  _file_cfg_predicate adjacent mod decls (AST + fallback)")


def test_cluster_extractor_args() -> None:
    """Plan 4: cluster → CLI args translation."""
    import pipeline

    cluster = pipeline.BuildCluster(
        name="stable", env_extra={}, tarpaulin_exclude_files=[],
        excluded_cfg=["nightly"],
        active_cfg=['feature = "std"'],
        prompt_nightly_rule="",
    )
    args = pipeline._cluster_extractor_args(cluster)
    assert "--cfg-true" in args
    assert 'feature = "std"' in args
    assert "--cfg-false" in args
    assert "nightly" in args
    # None → empty.
    assert pipeline._cluster_extractor_args(None) == []
    print("  OK  _cluster_extractor_args")


def test_buildcluster_features_toolchain() -> None:
    """Plan 4: BuildCluster derives features and toolchain from cfg lists."""
    import pipeline

    c = pipeline.BuildCluster(
        name="nightly", env_extra={}, tarpaulin_exclude_files=[],
        excluded_cfg=[],
        active_cfg=['feature = "std"', 'feature = "backtrace"', "nightly"],
        prompt_nightly_rule="",
        requires_nightly=True,
    )
    feats = c.features
    assert "std" in feats
    assert "backtrace" in feats
    assert c.toolchain == "nightly"
    c2 = pipeline.BuildCluster(
        name="stable", env_extra={}, tarpaulin_exclude_files=[],
        excluded_cfg=["nightly"], active_cfg=[],
        prompt_nightly_rule="",
    )
    assert c2.toolchain is None
    assert c2.features == []
    print("  OK  BuildCluster.features / .toolchain")


def test_format_skeleton_index_elides_macros() -> None:
    """P3.11: macro_rules! bodies must be elided from the skeleton index."""
    import pipeline

    files = [(
        "src/macros.rs",
        """\
#[macro_export]
macro_rules! shout {
    ($e:expr) => {
        {
            let s = format!("{}", $e);
            s.to_uppercase()
        }
    };
}

pub fn helper() { ::core::unimplemented!() }
""",
    )]
    out = pipeline._format_skeleton_index(files)
    # Macro name must remain visible.
    assert "macro_rules! shout" in out
    # Macro body must be gone.
    assert "to_uppercase" not in out
    assert "macro body elided" in out
    # Function body also elided.
    assert "unimplemented" not in out
    assert "pub fn helper();" in out or "pub fn helper() ;" in out
    print("  OK  _format_skeleton_index elides macros")


def test_split_oversized_groups() -> None:
    """P3.6: groups with >max_size test files get split into chunks."""
    import pipeline

    groups = [
        {"name": "small", "rationale": "r1", "test_files": ["a.rs", "b.rs"],
         "target_symbols": ["x"]},
        {"name": "big", "rationale": "r2",
         "test_files": ["a.rs", "b.rs", "c.rs", "d.rs", "e.rs", "f.rs", "g.rs"],
         "target_symbols": ["y", "z"]},
    ]
    split = pipeline._split_oversized_groups(groups, max_size=3)
    # small is untouched.
    assert any(g["name"] == "small" for g in split)
    # big becomes 3 chunks (7 tests / 3 = 3 chunks).
    big_chunks = [g for g in split if g["name"].startswith("big_")]
    assert len(big_chunks) == 3
    assert sum(len(g["test_files"]) for g in big_chunks) == 7
    # All chunks inherit target_symbols.
    assert all(g["target_symbols"] == ["y", "z"] for g in big_chunks)
    print("  OK  _split_oversized_groups")


def test_describe_test_environment() -> None:
    """_describe_test_environment should describe what cargo sees: the env
    vars set, the env vars notably NOT set, the cluster cfg state, and the
    test runner. Used as prompt context so the LLM stops proposing strategies
    that depend on env vars the pipeline doesn't actually set."""
    import pipeline

    desc = pipeline._describe_test_environment()
    # Header is recognisable so prompt assembly can identify the section.
    assert "Test environment" in desc
    # Always-present env keys.
    assert "RUSTFLAGS" in desc
    # The pipeline deliberately does NOT set RUST_BACKTRACE; the description
    # must surface that absence so env-dependent strategies are flagged.
    assert "RUST_BACKTRACE" in desc
    assert "NOT SET" in desc
    # Should mention the test runner so the LLM knows the invocation shape.
    assert "cargo test" in desc

    # When a cluster is active, the cluster's cfg state should appear.
    original_cluster = pipeline._current_cluster
    try:
        pipeline._current_cluster = pipeline.BuildCluster(
            name="nightly",
            env_extra={},
            tarpaulin_exclude_files=[],
            excluded_cfg=["target_has_atomic_load_store"],
            active_cfg=["error_generic_member_access"],
            prompt_nightly_rule="",
        )
        desc2 = pipeline._describe_test_environment()
        assert "active cfg predicates" in desc2
        assert "error_generic_member_access" in desc2
        assert "excluded cfg predicates" in desc2
    finally:
        pipeline._current_cluster = original_cluster

    print("  OK  _describe_test_environment")


def test_retrospective_prompt_includes_failed_attempts() -> None:
    """When unproductive_blocks is non-empty, the retrospective prompt must
    surface them as concrete evidence so the LLM can revise a TESTABLE call.
    We don't actually call the LLM — we monkey-patch `ask` and inspect the
    prompt string the retrospective would have sent."""
    import pipeline

    captured: dict = {}

    def fake_ask(prompt, **_kw):
        captured['prompt'] = prompt
        return "FUNCTION foo: UNREACHABLE — based on the prior failed attempts.\n"

    original_ask = pipeline.ask
    pipeline.ask = fake_ask
    try:
        result = pipeline._task0c_retrospective(
            rel_path="src/foo.rs",
            existing_text="pub fn foo() {\n    // !! UNCOVERED\n}\n",
            fn_bodies="pub fn foo() {\n    // !! UNCOVERED\n}\n",
            old_block="mod gen_tests_1 { #[test] fn t() { foo(); } }",
            module_name="tiny",
            unproductive_blocks=[
                'mod gen_tests_1 { #[test] fn a() { std::env::set_var("X", "1"); } }',
                'mod gen_tests_2 { #[test] fn b() { std::env::set_var("X", "1"); } }',
            ],
        )
    finally:
        pipeline.ask = original_ask

    prompt = captured['prompt']
    assert "PRIOR FAILED ATTEMPTS" in prompt
    assert "attempt 1" in prompt and "attempt 2" in prompt
    assert "did NOT improve coverage" in prompt
    assert "the classification was\nwrong" in prompt or "wrong" in prompt
    assert "set_var" in prompt, "concrete attempt content should appear in the prompt"
    assert "UNREACHABLE" in result, "fake LLM response should be returned"

    # Without unproductive_blocks, no PRIOR FAILED ATTEMPTS section.
    captured.clear()
    pipeline.ask = fake_ask
    try:
        pipeline._task0c_retrospective(
            rel_path="src/foo.rs",
            existing_text="pub fn foo() {}\n",
            fn_bodies="pub fn foo() {}\n",
            old_block="",
            module_name="tiny",
        )
    finally:
        pipeline.ask = original_ask
    assert "PRIOR FAILED ATTEMPTS" not in captured['prompt']

    print("  OK  retrospective_prompt_includes_failed_attempts")


def test_api_surface_conformance() -> None:
    """Capture api_surface, mutate the regenerated source, and verify the
    conformance check reports the missing pub items."""
    import pipeline

    workspace = _setup_workspace("smoke-api-conformance")
    module_name = "tiny"
    pipeline._capture_api_surface(workspace, workspace, module_name)

    # All pub items present → ok.
    ok, report = pipeline.validate_task2_api_surface_conformance(
        workspace, module_name, workspace
    )
    assert ok, f"unmodified fixture should conform: {report}"
    assert "ok" in report.lower()

    # Drop a public function from the source. Conformance should flag it.
    src = workspace / "src" / "lib.rs"
    text = src.read_text()
    # Remove the never_called fn entirely.
    text_mutated = re.sub(
        r'pub fn never_called\(\) -> i32 \{\s*42\s*\}\s*',
        '',
        text,
    )
    assert text_mutated != text, "test setup: expected to remove never_called"
    src.write_text(text_mutated)

    ok, report = pipeline.validate_task2_api_surface_conformance(
        workspace, module_name, workspace
    )
    assert not ok, "expected drift report after removing pub fn"
    assert "never_called" in report
    assert "fn:" in report

    # Missing api_surface → check is a no-op.
    workspace2 = _setup_workspace("smoke-api-conformance-no-api")
    ok, report = pipeline.validate_task2_api_surface_conformance(
        workspace2, module_name, workspace2
    )
    assert ok
    assert "no api_surface" in report.lower()

    print("  OK  validate_task2_api_surface_conformance")


def test_examples_collector() -> None:
    """collect_examples picks up examples/*.rs files for TASK 1 usage signal."""
    import pipeline

    workspace = _setup_workspace("smoke-examples")
    examples = pipeline.collect_examples(workspace)
    rels = [rel for rel, _ in examples]
    assert any("examples/usage.rs" in r for r in rels), \
        f"expected examples/usage.rs in collected examples, got: {rels}"

    # Sanity: content is the actual example body, not empty.
    _, content = examples[0]
    assert "fn main" in content
    assert "add(" in content

    # collect_test_files does NOT pick up examples (they aren't tests).
    test_files = pipeline.collect_test_files(workspace)
    assert not any("examples/" in rel for rel, _ in test_files), \
        "examples/ files must NOT appear in collect_test_files output"

    print("  OK  collect_examples")


def test_decl_only_src_noise_filter() -> None:
    """Stripped src files containing only gen_tests decls should be skipped by
    TASK 1's input collector. Regression for the post-strip noise issue where
    src/lib.rs would survive strip as a 4-line decl stub and confuse TASK 1."""
    import pipeline

    decl_only = (
        '// auto-generated stub after strip\n'
        '\n'
        '#[cfg(test)]\n'
        '#[allow(clippy::all)]\n'
        '#[path = "lib.gen_tests_1.rs"]\n'
        'mod gen_tests_1;\n'
        '\n'
        '#[cfg(test)]\n'
        '#[path = "lib.gen_tests_2.rs"]\n'
        'mod gen_tests_2;\n'
    )
    assert pipeline._is_gen_tests_decl_only_src(decl_only) is True

    decl_only_no_comments = (
        '#[cfg(test)]\n'
        '#[path = "lib.gen_tests_1.rs"]\n'
        'mod gen_tests_1;\n'
    )
    assert pipeline._is_gen_tests_decl_only_src(decl_only_no_comments) is True

    # A real source file with actual code is NOT decl-only.
    has_code = (
        'pub fn add(a: i32, b: i32) -> i32 { a + b }\n'
        '#[cfg(test)]\n'
        'mod inline_tests { #[test] fn t() {} }\n'
    )
    assert pipeline._is_gen_tests_decl_only_src(has_code) is False

    # A stripped file with one inline test mod (not a path-mod decl) is NOT decl-only.
    has_inline_tests = (
        '#[cfg(test)]\n'
        'mod tests {\n'
        '    #[test] fn t() {}\n'
        '}\n'
    )
    assert pipeline._is_gen_tests_decl_only_src(has_inline_tests) is False

    # Empty / whitespace counts as decl-only (no semantic content).
    assert pipeline._is_gen_tests_decl_only_src("") is True
    assert pipeline._is_gen_tests_decl_only_src("\n\n  \n") is True

    print("  OK  _is_gen_tests_decl_only_src")


def test_has_gen_tests_decl() -> None:
    """The guard used by task2 reattach: detects whether src contains
    `mod gen_tests_<n>;`. Regression coverage for the orphan-siblings bug
    where the previous guard checked only sibling-file existence."""
    import pipeline

    src = (
        'pub fn add(a: i32, b: i32) -> i32 { a + b }\n'
        '#[cfg(test)]\n'
        '#[allow(clippy::all)]\n'
        '#[path = "lib.gen_tests_1.rs"]\n'
        'mod gen_tests_1;\n'
        '#[cfg(test)]\n'
        '#[path = "lib.gen_tests_3.rs"]\n'
        'mod gen_tests_3;\n'
    )
    assert pipeline._has_gen_tests_decl(src, 1) is True
    assert pipeline._has_gen_tests_decl(src, 3) is True
    assert pipeline._has_gen_tests_decl(src, 2) is False, "missing decl must report False"
    assert pipeline._has_gen_tests_decl("", 1) is False
    assert pipeline._has_gen_tests_decl("// no decls here\n", 1) is False

    print("  OK  _has_gen_tests_decl")


def test_api_surface_capture() -> None:
    """Capture pre-strip API surface from the fixture and validate the artifact."""
    import pipeline

    workspace = _setup_workspace("smoke-api-surface")
    module_name = "tiny"
    out = pipeline._capture_api_surface(workspace, workspace, module_name)
    assert out is not None, "API capture must return a path for the fixture"
    assert out.exists()
    assert out.parent.name == pipeline.ORIGINAL_STRUCTURE_DIR

    text = out.read_text()
    # Must include each public function as a signature, with body elided.
    assert "fn add" in text, "missing add() signature"
    assert "fn divide" in text, "missing divide() signature"
    assert "fn never_called" in text, "missing never_called() signature"
    # Function bodies should be elided in the condensed view (no implementation).
    assert "a + b" not in text, "function body leaked into API surface"
    # Header / role text must be present so the LLM knows what this is.
    assert "API surface" in text
    assert "ground truth" in text.lower()

    # load_api_surface round-trip.
    loaded = pipeline.load_api_surface(workspace, module_name)
    assert loaded == text

    # _format_api_surface_section wraps it with the prompt header and truncates.
    section = pipeline._format_api_surface_section(loaded)
    assert "Public API surface" in section
    assert "ground truth" in section.lower()
    # Empty input → empty section (no prompt noise).
    assert pipeline._format_api_surface_section("") == ""

    print("  OK  api_surface capture + load + format")


def test_module_augment_state() -> None:
    """Cover every transition in the augmentation state machine."""
    import pipeline

    # 1. Done — coverage hits 100.
    s = pipeline.ModuleAugmentState("m", "stable")
    s.record_pass(100.0, 50, False)
    assert s.status("lib") == "done"

    # 2. Done — proc-macro crates short-circuit regardless of coverage.
    s = pipeline.ModuleAugmentState("m", "stable")
    s.record_pass(12.0, 1, False)
    assert s.status("proc-macro") == "done"

    # 3. Ceiling — requires CEILING_CONFIRM (2) consecutive at_ceiling passes.
    s = pipeline.ModuleAugmentState("m", "stable")
    s.record_pass(45.0, 12, True)
    assert s.status("lib") == "retry", "single at_ceiling should not yet trigger ceiling"
    s.record_pass(45.0, 12, True)
    assert s.status("lib") == "ceiling", "two consecutive at_ceiling passes should trigger ceiling"

    # 3b. Ceiling counter resets on a non-ceiling pass.
    s = pipeline.ModuleAugmentState("m", "stable")
    s.record_pass(45.0, 12, True)
    s.record_pass(45.0, 12, False)   # resets ceiling_passes
    s.record_pass(45.0, 12, True)
    assert s.status("lib") == "retry", "ceiling counter should reset on non-ceiling pass"
    s.record_pass(45.0, 12, True)
    assert s.status("lib") == "ceiling"

    # 4. Retry — first non-improving pass below ceiling, still under stall limit.
    s = pipeline.ModuleAugmentState("m", "stable")
    s.record_pass(80.0, 80, False)   # baseline
    s.record_pass(80.0, 80, False)   # no improvement; stall=1
    assert s.status("lib") == "retry"
    assert s.stall_passes == 1
    assert s.best_covered_lines == 80

    # 5. Stall counter resets on improvement.
    s.record_pass(82.0, 82, False)   # improved
    assert s.stall_passes == 0
    assert s.best_covered_lines == 82

    # 6. Hard block — STALL_LIMIT consecutive no-improvement passes.
    s = pipeline.ModuleAugmentState("m", "stable")
    s.record_pass(80.0, 80, False)
    for _ in range(pipeline.ModuleAugmentState.STALL_LIMIT):
        s.record_pass(80.0, 80, False)
    assert s.stall_passes == pipeline.ModuleAugmentState.STALL_LIMIT
    assert s.status("lib") == "hard_block"

    # 7. Coverage reaching 100 wins over hard_block (done takes precedence).
    s = pipeline.ModuleAugmentState("m", "stable")
    for _ in range(pipeline.ModuleAugmentState.STALL_LIMIT + 1):
        s.record_pass(99.0, 99, False)
    assert s.status("lib") == "hard_block"
    s.record_pass(100.0, 100, False)
    assert s.status("lib") == "done"

    print("  OK  ModuleAugmentState transitions")


def test_model_context_chars() -> None:
    """_model_context_chars returns sensible values for different env configs."""
    import importlib
    import pipeline

    orig_env = {k: os.environ.get(k) for k in ("BACKEND", "OLLAMA_NUM_CTX", "VLLM_MAX_MODEL_LEN", "MODEL")}

    def _call(**env_overrides):
        for k, v in env_overrides.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        result = pipeline._model_context_chars()
        return result

    try:
        # Ollama with explicit context window.
        v = _call(BACKEND="ollama", OLLAMA_NUM_CTX="32768")
        assert v == (32768 - 6_000) * 4, f"ollama 32768 → {v}"

        # Ollama with very small context — floor at 4_000 tokens budget.
        v = _call(BACKEND="ollama", OLLAMA_NUM_CTX="6000")
        assert v == max((6000 - 6_000), 4_000) * 4, f"ollama 6000 floor → {v}"

        # Claude backend → 200k token budget.
        v = _call(BACKEND="claude", OLLAMA_NUM_CTX=None)
        assert v == (200_000 - 6_000) * 4, f"claude backend → {v}"

        # Unknown backend, no env var → safe fallback 8192.
        v = _call(BACKEND="unknown_backend", OLLAMA_NUM_CTX=None, VLLM_MAX_MODEL_LEN=None)
        assert v == (8_192 - 6_000) * 4, f"fallback → {v}"

        # vLLM backend.
        v = _call(BACKEND="vllm", VLLM_MAX_MODEL_LEN="65536", OLLAMA_NUM_CTX=None)
        assert v == (65536 - 6_000) * 4, f"vllm 65536 → {v}"

    finally:
        for k, v in orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print("  OK  _model_context_chars")


def test_context_guard_section_removal() -> None:
    """ask() context guard drops sections in priority order before falling back to middle-trim."""
    import pipeline

    limit = pipeline.MODEL_CONTEXT_CHARS
    margin = 1_000

    # Build a prompt with 2 TESTFILE sections that together exceed the limit.
    # Each block is ~60% of (limit-margin) so one fits but two don't.
    section_size = (limit - margin) * 3 // 5
    padding = "x" * section_size

    testfile_block = f'\n<TESTFILE path="tests/foo.rs">{padding}</TESTFILE>'
    base_prompt = "A" * 200 + testfile_block + testfile_block

    # Confirm it's actually over limit (so the guard fires).
    assert len(base_prompt) > limit - margin, (
        f"test precondition failed: prompt {len(base_prompt)} not > limit {limit - margin}"
    )

    # Monkey-patch _ask_raw to capture the trimmed prompt.
    captured: list[str] = []
    orig_ask = pipeline._ask_raw

    def fake_ask(prompt, system=None, **kw):
        captured.append(prompt)
        return "ok"

    pipeline._ask_raw = fake_ask
    try:
        pipeline.ask(base_prompt)
    finally:
        pipeline._ask_raw = orig_ask

    assert captured, "ask() never called model_interact.ask"
    sent = captured[0]
    assert len(sent) <= limit - margin, (
        f"guard failed to trim: sent {len(sent)} chars, limit {limit - margin}"
    )
    # At least one TESTFILE block was dropped.
    assert sent.count("<TESTFILE") < base_prompt.count("<TESTFILE"), (
        "guard should have dropped at least one TESTFILE block"
    )
    print("  OK  context_guard section removal")


def test_event_log() -> None:
    """_emit writes JSONL with fixed schema; _store_prompt_content content-addresses."""
    import json as _json
    import tempfile
    import pipeline

    with tempfile.TemporaryDirectory() as td:
        events_path = Path(td) / "events.jsonl"
        prompts_dir = Path(td) / "prompts"

        # Redirect event sinks at module-level.
        orig_log = pipeline._EVENTS_LOG
        orig_dir = pipeline._PROMPTS_DIR
        orig_ctx = dict(pipeline._RUN_CONTEXT)
        pipeline._EVENTS_LOG = str(events_path)
        pipeline._PROMPTS_DIR = prompts_dir
        prompts_dir.mkdir()
        pipeline._RUN_CONTEXT["product"] = "test-product"
        pipeline._RUN_CONTEXT["pipeline_sha"] = "abc123"

        try:
            # Content addressing: same text → same sha, file written once.
            m1 = pipeline._store_prompt_content("hello world")
            m2 = pipeline._store_prompt_content("hello world")
            assert m1["sha256"] == m2["sha256"]
            assert m1["char_count"] == 11
            assert (prompts_dir / f"{m1['sha256']}.txt").exists()

            # Empty input returns empty sha — and writes nothing.
            empty = pipeline._store_prompt_content("")
            assert empty == {"sha256": "", "char_count": 0}

            # Emit two events.
            pipeline._emit("test.one", task="task0c", attempt=3, data={"k": "v"})
            pipeline._emit("test.two", duration_ms=42)

            lines = events_path.read_text().splitlines()
            assert len(lines) == 2, f"expected 2 events, got {len(lines)}"
            e1 = _json.loads(lines[0])
            e2 = _json.loads(lines[1])

            # Schema must include fixed fields.
            for required in ("ts", "product", "pipeline_sha", "event"):
                assert required in e1, f"missing {required} in {e1}"
            assert e1["product"] == "test-product"
            assert e1["pipeline_sha"] == "abc123"
            assert e1["event"] == "test.one"
            assert e1["task"] == "task0c"
            assert e1["attempt"] == 3
            assert e1["data"] == {"k": "v"}
            assert "duration_ms" not in e1, "None fields must be omitted"

            assert e2["event"] == "test.two"
            assert e2["duration_ms"] == 42

            # _emit must never raise even if write fails.
            pipeline._EVENTS_LOG = "/nonexistent/dir/foo.jsonl"
            pipeline._emit("test.three")  # should not raise

        finally:
            pipeline._EVENTS_LOG = orig_log
            pipeline._PROMPTS_DIR = orig_dir
            pipeline._RUN_CONTEXT.clear()
            pipeline._RUN_CONTEXT.update(orig_ctx)

    print("  OK  event log (_emit, _store_prompt_content)")


def test_monitor_task_pipeline_parser() -> None:
    """_parse_task_pipeline reconstructs task progression from log lines."""
    import monitor

    # Build a synthetic log that walks the full pipeline.
    log_lines = [
        "PIPELINE STARTED — 2026-05-23 11:00:00 UTC",
        "TASK 0a: Identifying project structure",
        "TASK 0b (discovery): Analyzing project for coverage strategy...",
        "  [git] committed: pipeline: task0b done",
        "TASK 0c [stable]: Augmenting 'anyhow' to 100% coverage...",
        "  [git] committed: pipeline: task0c/stable — anyhow:99.5%",
        "TASK 0c [nightly]: Augmenting 'anyhow' to 100% coverage...",
        "  [git] committed: pipeline: task0c/nightly — anyhow:99.5%",
        "  [git] committed: pipeline: stripped — anyhow",
        "TASK 1: Analyzing tests + README, building functional_requirements.md",
        "  TASK 1 output validated OK.",
        "  [git] committed: pipeline: task1 — anyhow",
        "TASK 1.5: 100 use-paths in tests, 6 missing from requirements.",
        "==================================================",
        "MODULE anyhow — ITERATION 1",
        "==================================================",
        "TASK 2: Implementing source code (static file list)",
    ]
    tp = monitor._parse_task_pipeline(log_lines)
    assert tp["module"] == "anyhow"
    assert tp["iteration"] == 1
    assert tp["task0c_clusters_done"] == 2
    assert tp["states"]["task0a"]  == "done"
    assert tp["states"]["task0b"]  == "done"
    assert tp["states"]["task0c"]  == "done"
    assert tp["states"]["strip"]   == "done"
    assert tp["states"]["task1"]   == "done"
    assert tp["states"]["task2"]   == "in_progress"
    assert tp["states"]["task3"]   == "pending"
    assert tp["states"]["task4"]   == "pending"
    assert tp["current"] == "task2"

    # Test PIPELINE STARTED reset.
    log_lines.extend([
        "PIPELINE STARTED — 2026-05-23 12:00:00 UTC",
        "TASK 0a: Identifying project structure",
    ])
    tp2 = monitor._parse_task_pipeline(log_lines)
    assert tp2["states"]["task0a"] == "in_progress"
    assert tp2["states"]["task0c"] == "pending", "PIPELINE STARTED must reset all phases"
    assert tp2["module"] is None
    assert tp2["task0c_clusters_done"] == 0

    # Test test PASS/FAIL recording.
    log_lines2 = [
        "PIPELINE STARTED — 2026-05-23 13:00:00 UTC",
        "TASK 3: Running tests (iteration 1)",
        "  Tests PASSED. Output -> /tmp/foo.txt",
    ]
    tp3 = monitor._parse_task_pipeline(log_lines2)
    assert tp3["states"]["task3"] == "done"
    assert tp3["task3_pass"] is True

    log_lines3 = [
        "PIPELINE STARTED — 2026-05-23 14:00:00 UTC",
        "TASK 3: Running tests (iteration 1)",
        "  Tests FAILED. Output -> /tmp/foo.txt",
    ]
    tp4 = monitor._parse_task_pipeline(log_lines3)
    assert tp4["task3_pass"] is False

    # Iteration boundary resets task2/3/4 but not task1.
    log_lines4 = [
        "PIPELINE STARTED — 2026-05-23 15:00:00 UTC",
        "TASK 1: Analyzing tests",
        "  [git] committed: pipeline: task1 — anyhow",
        "TASK 2: Implementing source code",
        "MODULE anyhow — ITERATION 2",
        "TASK 2: Implementing source code",
    ]
    tp5 = monitor._parse_task_pipeline(log_lines4)
    assert tp5["states"]["task1"] == "done", "task1 must persist across iterations"
    assert tp5["states"]["task2"] == "in_progress"

    print("  OK  monitor task pipeline parser")


def test_context_guard_middle_trim() -> None:
    """ask() falls back to middle-trim when section removal is insufficient."""
    import pipeline

    limit = pipeline.MODEL_CONTEXT_CHARS
    margin = 1_000

    # Build a prompt with no recognisable sections — forces blind middle-trim.
    big_prompt = "B" * (limit + 10_000)

    captured: list[str] = []
    orig_ask = pipeline._ask_raw

    def fake_ask(prompt, system=None, **kw):
        captured.append(prompt)
        return "ok"

    pipeline._ask_raw = fake_ask
    try:
        pipeline.ask(big_prompt)
    finally:
        pipeline._ask_raw = orig_ask

    assert captured
    sent = captured[0]
    assert len(sent) <= limit - margin, (
        f"middle-trim failed: {len(sent)} > {limit - margin}"
    )
    assert "trimmed by context-guard" in sent, "expected context-guard ellipsis in trimmed output"
    print("  OK  context_guard middle-trim fallback")


def test_extract_gen_tests_body() -> None:
    import pipeline

    block_with_attrs = """#[cfg(test)]
#[allow(unused)]
mod gen_tests_5 {
    use super::*;

    #[test]
    fn x() {
        assert!(true);
    }
}"""
    body = pipeline._extract_gen_tests_body(block_with_attrs)
    assert body.startswith("use super::*;"), f"body starts wrong: {body[:80]!r}"
    assert "#[test]" in body
    assert "mod gen_tests" not in body
    # Body should be dedented relative to the original 4-space inner indent.
    assert "fn x()" in body
    assert "    fn x()" not in body, "body should be dedented to col 0"

    # Bare functions (no mod wrapper): returned as-is.
    bare = "#[test]\nfn x() { assert!(true); }"
    body = pipeline._extract_gen_tests_body(bare)
    assert "#[test]" in body and "fn x()" in body

    print("  OK  _extract_gen_tests_body")


# ─── Cargo phase (slow, opt-in) ─────────────────────────────────────────────

def test_coverage_measurement() -> None:
    """Run the configured coverage provider on the fixture; assert sensible output."""
    import pipeline

    workspace = _setup_workspace("smoke-coverage")
    cargo_text = (workspace / "Cargo.toml").read_text()
    m = re.search(r'name\s*=\s*"([^"]+)"', cargo_text)
    assert m, "no [package] name in fixture Cargo.toml"
    module_name = m.group(1)

    print(f"  ... running {pipeline.COVERAGE_BACKEND} on {workspace.relative_to(REPO)}")
    pct, files_data, covered, coverable = pipeline._coverage_provider.measure(
        workspace, module_name
    )

    print(f"      coverage: {pct:.1f}% ({covered}/{coverable})")
    assert coverable > 0, "fixture must report coverable lines > 0"
    assert covered > 0, "fixture has tests — covered must be > 0"
    assert covered < coverable, "fixture has uncovered code — partial coverage expected"
    assert 10.0 < pct < 95.0, f"expected 10%-95% coverage, got {pct:.1f}%"

    paths = [fd.get("abs_path") or fd.get("path") or "" for fd in files_data]
    has_lib = any("lib.rs" in (str(p) if not isinstance(p, list) else "/".join(p))
                  for p in paths)
    assert has_lib, f"lib.rs not in measured files_data: {paths}"

    print("  OK  coverage measurement")


def test_coverage_ignores_siblings() -> None:
    """Write a synthetic sibling that would inflate coverage if measured;
    confirm the coverage provider's ignore patterns exclude it."""
    import pipeline

    workspace = _setup_workspace("smoke-cov-sibling")
    src = workspace / "src" / "lib.rs"

    # Plant a sibling with intentionally-uncovered code. If the ignore pattern
    # is wrong, the sibling's lines would show up in `coverable`.
    block = """#[cfg(test)]
mod gen_tests_1 {
    use super::*;
    #[test]
    fn t() {
        let _ = add(0, 0);
        let _ = divide(0, 0);
    }
}"""
    pipeline._write_gen_tests_pass(
        src, src.read_text(), block, "#[allow(clippy::all)]", 1
    )

    cargo_text = (workspace / "Cargo.toml").read_text()
    m = re.search(r'name\s*=\s*"([^"]+)"', cargo_text)
    assert m
    module_name = m.group(1)

    pct, files_data, covered, coverable = pipeline._coverage_provider.measure(
        workspace, module_name
    )
    print(f"  ... with-sibling coverage: {pct:.1f}% ({covered}/{coverable})")

    # The sibling file must not appear in measured files.
    paths = [
        (str(fd.get("abs_path") or fd.get("path") or ""))
        if not isinstance(fd.get("path"), list)
        else "/".join(fd.get("path", []))
        for fd in files_data
    ]
    sibling_seen = any("gen_tests_1" in p for p in paths)
    assert not sibling_seen, (
        f"sibling file leaked into coverage measurement: {paths}"
    )
    print("  OK  coverage ignores siblings")


# ─── Driver ─────────────────────────────────────────────────────────────────

def main() -> int:
    full = "--full" in sys.argv or os.getenv("SMOKE_FULL") == "1"

    print("Pure-Python phase:")
    test_gen_tests_helpers()
    test_extract_gen_tests_body()
    test_examples_collector()
    test_decl_only_src_noise_filter()
    test_has_gen_tests_decl()
    test_api_surface_capture()
    test_api_surface_conformance()
    test_merge_fills_preserving_existing()
    test_fn_block_extract_and_merge()
    test_format_skeleton_index()
    test_format_skeleton_index_elides_macros()
    test_prune_redundant_clusters()
    test_filter_tests_for_cluster()
    test_eval_cfg_pred()
    test_file_cfg_predicate_adjacent_mod_decls()
    test_cluster_extractor_args()
    test_buildcluster_features_toolchain()
    test_split_oversized_groups()
    test_describe_test_environment()
    test_retrospective_prompt_includes_failed_attempts()
    test_module_augment_state()
    test_model_context_chars()
    test_context_guard_section_removal()
    test_context_guard_middle_trim()
    test_event_log()
    test_monitor_task_pipeline_parser()

    if full:
        print("Cargo phase (--full):")
        test_coverage_measurement()
        test_coverage_ignores_siblings()
    else:
        print("Skipping cargo phase (pass --full or SMOKE_FULL=1 to enable).")

    print("\nAll smoke tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
