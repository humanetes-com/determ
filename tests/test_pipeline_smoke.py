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


def test_strip_doc_attrs() -> None:
    """FR-024 fix 1: doc attributes pollute prompt context."""
    import pipeline
    text = (
        '# [doc = " /// Single-line doc"]\n'
        'pub fn foo() -> i32 { 1 }\n'
        '# [doc = " multi-line"]\n'
        '# [doc = " continuation"]\n'
        'pub fn bar() -> i32 { 2 }\n'
        '#[cfg(test)]\nmod tests {}\n'
    )
    out = pipeline._strip_doc_attrs(text)
    assert "doc" not in out, f"doc attrs not stripped:\n{out}"
    assert "pub fn foo" in out
    assert "pub fn bar" in out
    assert "#[cfg(test)]" in out
    print("  OK  _strip_doc_attrs (FR-024)")


def test_condense_doc_attrs() -> None:
    """FR-026: doc attrs condense to a one-line intent, not deleted, not dumped.
    Brackets inside doc content (markdown links/badges) must not desync."""
    import pipeline
    text = (
        '# ! [doc = " [![badge]](http://x)&ensp;"]\n'
        '# ! [doc = " The crate does [a thing](http://y). More detail here."]\n'
        'impl Error { '
        '# [doc = " Create a new error object from any error type."] '
        '# [doc = ""] '
        '# [doc = " ```"] '
        '# [doc = " let x = 1;"] '
        '# [doc = " ```"] '
        '# [cold] pub fn new < E > (e: E) -> Self { unimplemented!() } }\n'
    )
    out = pipeline._condense_doc_attrs(text)
    assert 'doc =' not in out, f"doc attrs survived:\n{out}"
    # crate intent kept as //! , first sentence only, link reduced to text
    assert '//! The crate does a thing.' in out, out
    # fn intent kept as // , code example dropped
    assert '// Create a new error object from any error type.' in out, out
    assert 'let x = 1;' not in out, "code example leaked"
    assert 'badge' not in out, "badge leaked"
    assert 'pub fn new' in out
    print("  OK  _condense_doc_attrs (FR-026)")


def test_cluster_gate_predicate_is_satisfiable() -> None:
    """FR-032: a cluster activated by `--cfg N` must get a gate that's true
    under that cfg. The old `all(N, feature="N", …)` was unsatisfiable (feature
    off) → divergent items vanished from the merge → E0046."""
    import pipeline
    from types import SimpleNamespace
    # no_core cluster: activated by --cfg anyhow_no_core_error, excludes egma
    c = SimpleNamespace(
        active_cfg=["anyhow_no_core_error"],
        excluded_cfg=["error_generic_member_access"],
    )
    g = pipeline._cluster_cfg_predicate_for_gate(c)
    # each cfg is OR'd with its feature form → satisfiable by the bare --cfg
    assert 'any(anyhow_no_core_error, feature = "anyhow_no_core_error")' in g, g
    assert 'not(any(error_generic_member_access' in g, g
    # must NOT require the feature form via a bare AND term
    assert ', feature = "anyhow_no_core_error",' not in g, f"feature AND'd: {g}"
    # default (no active/excluded) → empty gate
    assert pipeline._cluster_cfg_predicate_for_gate(
        SimpleNamespace(active_cfg=[], excluded_cfg=[])) == ""
    print("  OK  _cluster_cfg_predicate_for_gate (FR-032)")


def test_run_scoped_tests() -> None:
    """Plan 10 U1: reusable scoped-test check classifies pass/fail/timeout and
    returns failure detail for prompt feedback."""
    import pipeline
    _orig = pipeline._run_cargo
    try:
        _P = __import__("pathlib").Path(".")
        # all pass → (True, 0, "", set())
        pipeline._run_cargo = lambda *a, **k: (0, "test result: ok. 5 passed; 0 failed")
        ok, n, detail, failed = pipeline._run_scoped_tests("cargo test", _P, {})
        assert ok and n == 0 and detail == "" and failed == set(), (ok, n, detail, failed)

        # failures → (False, count>0, detail tail, failed names set)
        out = "test foo::bar ... FAILED\ntest result: FAILED. 4 passed; 1 failed\nassertion `left == right` failed"
        pipeline._run_cargo = lambda *a, **k: (101, out)
        ok, n, detail, failed = pipeline._run_scoped_tests("cargo test", _P, {})
        assert (not ok) and n == 1 and "assertion" in detail and "foo::bar" in failed, (ok, n, detail, failed)

        # timeout → inconclusive (False, -1, ..., set())
        pipeline._run_cargo = lambda *a, **k: (-1, "")
        ok, n, detail, failed = pipeline._run_scoped_tests("cargo test", _P, {})
        assert (not ok) and n == -1 and failed == set(), (ok, n, detail, failed)
    finally:
        pipeline._run_cargo = _orig
    print("  OK  _run_scoped_tests (Plan 10 U1)")


def test_generate_in_parallel() -> None:
    """Plan 8: split-and-merge fans out one concurrent ask() per subset prompt,
    returns responses index-aligned, and survives a failing request."""
    import pipeline, threading
    _orig = pipeline.ask
    seen_lock = threading.Lock()
    calls = []

    def _fake_ask(query, model=None, think=None, **kw):
        with seen_lock:
            calls.append(query)
        if "BOOM" in query:
            raise RuntimeError("simulated request failure")
        return f"resp:{query}"

    pipeline.ask = _fake_ask
    try:
        # empty / single short-circuit.
        assert pipeline.generate_in_parallel([]) == []
        one = pipeline.generate_in_parallel(["Q0"])
        assert one == ["resp:Q0"], one

        # distinct prompts → responses index-aligned to inputs.
        many = pipeline.generate_in_parallel(["A", "B", "C", "D"])
        assert many == ["resp:A", "resp:B", "resp:C", "resp:D"], many

        # a failing request contributes "" without sinking the batch.
        mixed = pipeline.generate_in_parallel(["A", "BOOM", "C"])
        assert mixed == ["resp:A", "", "resp:C"], mixed
    finally:
        pipeline.ask = _orig
    print("  OK  generate_in_parallel (Plan 8 split-and-merge)")


def test_partition_uncovered_lines() -> None:
    """Plan 8: uncovered lines partition into <=N buckets, keeping each
    function's lines together and balancing across buckets."""
    import pipeline
    src = (
        "pub fn a() -> i32 {\n"      # line 1
        "    1\n"                      # 2
        "}\n"                          # 3
        "pub fn b() -> i32 {\n"      # 4
        "    2\n"                      # 5
        "}\n"                          # 6
        "pub fn c() -> i32 {\n"      # 7
        "    3\n"                      # 8
        "}\n"                          # 9
    )
    uncov = {2, 5, 8}
    buckets = pipeline._partition_uncovered_lines(src, uncov, 3)
    assert len(buckets) == 3, buckets
    # every uncovered line lands in exactly one bucket; buckets are disjoint.
    union = set().union(*buckets)
    assert union == uncov, (buckets, union)
    assert sum(len(b) for b in buckets) == len(uncov), buckets
    # fewer functions than groups → never more buckets than functions.
    two = pipeline._partition_uncovered_lines(src, {2, 5}, 4)
    assert len(two) == 2, two
    # n_groups==1 or empty are trivial.
    assert pipeline._partition_uncovered_lines(src, uncov, 1) == [uncov]
    assert pipeline._partition_uncovered_lines(src, set(), 3) == []
    print("  OK  _partition_uncovered_lines (Plan 8 split-and-merge)")


def test_collect_sibling_filled_bodies() -> None:
    """FR-024 fix 3: sibling impl with same fn name surfaces as reference."""
    import pipeline
    text = (
        "impl Foo {\n"
        "    fn as_ref(&self) -> &i32 {\n"
        "        &self.val\n"
        "    }\n"
        "}\n"
        "impl Bar {\n"
        "    fn as_ref(&self) -> &i32 {\n"
        "        ::core::unimplemented!()\n"
        "    }\n"
        "}\n"
    )
    block = pipeline._collect_sibling_filled_bodies(text, "as_ref", "impl Bar")
    assert "&self.val" in block, f"Foo's filled as_ref not surfaced:\n{block}"
    assert "unimplemented" not in block, f"stub leaked into siblings:\n{block}"
    # Calling with Foo as target should exclude Foo and find no fillers.
    block_foo = pipeline._collect_sibling_filled_bodies(text, "as_ref", "impl Foo")
    assert block_foo == "", f"Foo's own impl returned as sibling:\n{block_foo}"
    print("  OK  _collect_sibling_filled_bodies (FR-024)")


def test_count_test_fns_cfg_aware() -> None:
    """FR-024: cfg-aware test counter must skip #[test] fns gated by a
    #[cfg(...)] whose predicate is in the cluster's cfg_false set."""
    import pipeline
    text = (
        "#[test]\nfn t_always() {}\n"
        "\n"
        "#[cfg(error_generic_member_access)]\n#[test]\nfn t_nightly_only() {}\n"
        "\n"
        "#[test]\n#[cfg(any(feature=\"std\", not(anyhow_no_core_error)))]\n"
        "fn t_std_or_default() {}\n"
        "\n"
        "#[cfg(unknown_predicate)]\n#[test]\nfn t_unknown() {}\n"
    )
    # Stable: error_generic_member_access ∈ cfg_false → t_nightly_only
    # excluded. t_unknown undecidable → kept. t_always always. t_std_or_default
    # has any() with `not(anyhow_no_core_error)` true → kept.
    n_stable = pipeline._count_test_fns_cfg_aware(
        text,
        cfg_true=set(),
        cfg_false={"error_generic_member_access", "anyhow_no_core_error"},
    )
    assert n_stable == 3, f"stable count expected 3, got {n_stable}"
    # Nightly: error_generic_member_access ∈ cfg_true → t_nightly_only kept.
    n_nightly = pipeline._count_test_fns_cfg_aware(
        text,
        cfg_true={"error_generic_member_access"},
        cfg_false=set(),
    )
    assert n_nightly == 4, f"nightly count expected 4, got {n_nightly}"
    print("  OK  _count_test_fns_cfg_aware (FR-024)")


def test_validate_task3_strict() -> None:
    """FR-024 strict per-cluster acceptance criterion."""
    import pipeline
    v = pipeline.validate_task3_output_strict
    # All accounted, no failures → pass.
    s, _ = v(pass_count=10, fail_count=0, ignored_count=2, expected_count=12, cluster_name="x")
    assert s == "pass", s
    # Any failure → fail.
    s, _ = v(pass_count=10, fail_count=1, ignored_count=0, expected_count=11, cluster_name="x")
    assert s == "fail", s
    # Reported fewer than expected → incomplete (binary failed to build).
    s, _ = v(pass_count=5, fail_count=0, ignored_count=0, expected_count=12, cluster_name="x")
    assert s == "incomplete", s
    # Empty corpus and no runs → incomplete.
    s, _ = v(pass_count=0, fail_count=0, ignored_count=0, expected_count=0, cluster_name="x")
    assert s == "incomplete", s
    # Ignored tests count toward the expected total.
    s, _ = v(pass_count=8, fail_count=0, ignored_count=4, expected_count=12, cluster_name="x")
    assert s == "pass", s
    print("  OK  validate_task3_output_strict (FR-024)")


def test_parse_test_counts() -> None:
    """FR-024 metric parser: sum (passed, failed, ignored) across all
    `test result:` lines in cargo test output."""
    import pipeline
    sample = (
        "running 0 tests\n"
        "test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n"
        "\n"
        "running 5 tests\n"
        "test foo ... ok\n"
        "test result: ok. 5 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n"
        "\n"
        "running 3 tests\n"
        "test bar ... FAILED\n"
        "test result: FAILED. 0 passed; 3 failed; 0 ignored; 0 measured; 0 filtered out\n"
        "\n"
        "running 2 tests\n"
        "test result: ok. 1 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out\n"
    )
    passed, failed, ignored = pipeline._parse_test_counts(sample)
    assert (passed, failed, ignored) == (6, 3, 1), \
        f"got ({passed}, {failed}, {ignored}), expected (6, 3, 1)"
    # Empty input → (0, 0, 0).
    assert pipeline._parse_test_counts("") == (0, 0, 0)
    print("  OK  _parse_test_counts (FR-024)")


def test_build_test_reverse_map() -> None:
    """FR-024 reverse map: each #[test] fn maps to fns it could reach."""
    import pipeline
    import tempfile
    if not pipeline._SKELETON_EXTRACTOR_BIN.exists():
        print("  SKIP _build_test_reverse_map (extractor missing)")
        return
    with tempfile.TemporaryDirectory(prefix="rev_map_") as td:
        root = Path(td)
        src = root / "src"
        src.mkdir()
        (src / "lib.rs").write_text(
            "pub fn alpha() -> i32 { 1 }\n"
            "pub fn beta() -> i32 { 2 }\n"
            "pub fn gamma() -> i32 { 3 }\n"
        )
        tests_dir = root / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_alpha_beta.rs").write_text(
            "use mycrate::{alpha, beta};\n"
            "#[test]\nfn t_one() { assert_eq!(alpha(), 1); }\n"
            "#[test]\nfn t_two() { assert_eq!(beta(), 2); }\n"
        )
        test_files = list(tests_dir.rglob("*.rs"))
        rev = pipeline._build_test_reverse_map(test_files, src)
        # Both test fns should map to alpha and beta (gamma not referenced
        # by name in the test file, but it's in src — coarse reverse map
        # is file-level, so it may or may not appear; either way both
        # named fns must be present).
        assert "t_one" in rev, f"t_one missing from reverse map: {rev}"
        assert "t_two" in rev, f"t_two missing from reverse map: {rev}"
        names = {n for (_f, n) in rev["t_one"]}
        assert "alpha" in names, f"alpha not attributed to t_one: {names}"
        assert "beta" in names, f"beta not attributed to t_one: {names}"
    print("  OK  _build_test_reverse_map (FR-024)")


def test_ast_cluster_merge_preserves_trait_structure() -> None:
    """Regression: the AST-based cluster merger must keep cfg-divergent
    items at their natural AST position. The previous regex-based splicer
    emitted divergent trait/impl method bodies OUTSIDE their trait/impl
    block, producing orphan declarations with stray `}` braces — concrete
    case observed in anyhow's `ensure.rs`:

        pub trait BothDebug { #[cfg(...)] fn __dispatch_ensure(...); }
        impl BothDebug for (A,B) { fn __dispatch_ensure(...) { body_X } }
        #[cfg(...)] fn __dispatch_ensure(...) ; }   ← orphan with stray `}`

    This test feeds three cluster fixtures where the same trait method's
    impl body differs and asserts that the merged output keeps the impl
    bodies INSIDE the impl block (no orphan items) AND compiles cleanly.
    """
    import pipeline
    import subprocess
    import tempfile

    if not pipeline._SKELETON_EXTRACTOR_BIN.exists():
        print("  SKIP _ast_cluster_merge (extractor binary missing)")
        return

    base = (
        "pub trait T { fn dispatch(self, m: &'static str) -> i32; }\n"
        "impl T for (i32, i32) {\n"
        "    fn dispatch(self, m: &'static str) -> i32 { self.0 + self.1 }\n"
        "}\n"
        "fn helper() -> i32 { 0 }\n"
    )
    variant = (
        "pub trait T { fn dispatch(self, m: &'static str) -> i32; }\n"
        "impl T for (i32, i32) {\n"
        "    fn dispatch(self, m: &'static str) -> i32 { self.0 * self.1 }\n"
        "}\n"
        "fn helper() -> i32 { 0 }\n"
    )
    with tempfile.TemporaryDirectory(prefix="ast_merge_") as td:
        root = Path(td)
        (root / "a.rs").write_text(base)
        (root / "b.rs").write_text(variant)
        out = root / "merged.rs"
        r = subprocess.run(
            [str(pipeline._SKELETON_EXTRACTOR_BIN), "--merge-clusters",
             "--merge-output", str(out),
             "--cluster", f"cluster_a:{root}/a.rs:feature_a",
             "--cluster", f"cluster_b:{root}/b.rs:feature_b"],
            capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 0, f"extractor failed: {r.stderr}"
        merged = out.read_text()
        # The trait declaration appears exactly once (identical across clusters).
        assert merged.count("pub trait T") == 1, \
            f"trait collapsed wrong; merged:\n{merged}"
        # Two impl-method bodies appear, gated, INSIDE the impl block.
        assert merged.count("impl T for (i32, i32)") == 1, \
            f"impl block duplicated; merged:\n{merged}"
        assert "self.0 + self.1" in merged
        assert "self.0 * self.1" in merged
        # No orphan fn appears with a `}` directly preceding it at top level.
        # Crude check: the count of top-level `}` mid-file should be balanced.
        # A definitive check: the merged file must parse + compile.
        crate = root / "compile_test"
        (crate).mkdir()
        (crate / "Cargo.toml").write_text(
            '[package]\nname="m"\nversion="0.0.1"\nedition="2021"\n'
        )
        src = crate / "src"
        src.mkdir()
        (src / "lib.rs").write_text(
            '#![allow(unused, dead_code)]\nmod merged;\n'
        )
        (src / "merged.rs").write_text(merged)
        # Build under each cluster cfg in turn — exactly one impl method
        # must be active per build (the trait must be fully implemented).
        # If the merge had misplaced the methods outside the impl block,
        # the trait would be empty and cargo would either err on a stub
        # impl or on the orphan fn syntax.
        for feat in ("feature_a", "feature_b"):
            cc = subprocess.run(
                ["cargo", "check"], cwd=crate, capture_output=True, text=True,
                timeout=60,
                env={**os.environ, "RUSTFLAGS": f"--cfg={feat}"},
            )
            assert cc.returncode == 0, (
                f"merged output did not compile under {feat}:\n"
                f"--- merged.rs ---\n{merged}\n"
                f"--- cargo stderr ---\n{cc.stderr}"
            )
            # cargo caches per-RUSTFLAGS. Wipe target between builds so
            # both feats are exercised cleanly.
            subprocess.run(["cargo", "clean"], cwd=crate,
                            capture_output=True, timeout=30)
    print("  OK  AST cluster merge preserves trait/impl structure")


def test_strip_preserves_gen_tests_verbatim() -> None:
    """FR-023: strip must preserve `*.gen_tests_N.rs` siblings byte-for-byte.

    These files are task0c output — pure test code with imports and helper
    types. Running `extract_rust_tests` on them drops the imports and
    helpers, leaving `#[test]` fns that reference undefined symbols. The
    gen_test then fails to compile when Phase C runs cargo test, the TDD
    compile-frontier sees unresolved-name errors that don't map to any
    reconstruction target, and the gen_test is silently disabled.
    """
    import subprocess
    import sys
    import tempfile
    strip_script = Path(__file__).resolve().parent.parent / "strip_to_tests.py"
    if not strip_script.exists():
        print("  SKIP _strip_preserves_gen_tests_verbatim (script missing)")
        return
    fixture = (
        "use super::*;\n"
        "use std::io;\n"
        "use std::error::Error as StdError;\n"
        "use std::fmt::{self, Debug, Display};\n"
        "\n"
        "#[derive(Debug)]\n"
        "struct MyTestErr(&'static str);\n"
        "\n"
        "impl Display for MyTestErr {\n"
        "    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {\n"
        "        f.write_str(self.0)\n"
        "    }\n"
        "}\n"
        "\n"
        "impl StdError for MyTestErr {}\n"
        "\n"
        "#[test]\n"
        "fn test_with_helper() {\n"
        "    let e = MyTestErr(\"hi\");\n"
        "    assert_eq!(e.to_string(), \"hi\");\n"
        "    let _: &dyn StdError = &e;\n"
        "    let _io = io::Error::new(io::ErrorKind::Other, \"x\");\n"
        "}\n"
    )
    with tempfile.TemporaryDirectory(prefix="strip_gentests_") as td:
        root = Path(td)
        (root / "Cargo.toml").write_text(
            '[package]\nname = "x"\nversion = "0.1.0"\nedition = "2021"\n'
        )
        src = root / "src"
        src.mkdir()
        (src / "lib.rs").write_text(
            "#[cfg(test)]\n#[path = \"foo.gen_tests_1.rs\"]\n"
            "mod gen_tests_1;\nfn _x() {}\n"
        )
        gen_path = src / "foo.gen_tests_1.rs"
        gen_path.write_text(fixture)

        r = subprocess.run(
            [sys.executable, str(strip_script), str(root)],
            capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 0, f"strip_to_tests.py failed: {r.stderr}"
        survivor = gen_path.read_text()
        # Must keep helper struct + Display + StdError impl.
        assert "struct MyTestErr" in survivor, \
            "gen_tests helper struct dropped by strip"
        assert "impl Display for MyTestErr" in survivor, \
            "gen_tests Display impl dropped by strip"
        assert "impl StdError for MyTestErr" in survivor, \
            "gen_tests StdError impl dropped by strip"
        # Must keep imports the tests rely on.
        assert "use super::*;" in survivor, "use super::*; dropped by strip"
        assert "use std::io;" in survivor, "use std::io; dropped by strip"
        # Byte-for-byte is the strongest property.
        assert survivor == fixture, (
            "gen_tests file modified by strip; expected byte-for-byte "
            "preservation per FR-023."
        )
    print("  OK  strip preserves gen_tests verbatim (FR-023)")


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


def test_derive_check_only_cmd() -> None:
    """per_fn fix strategy: the inner-loop check command is the caller's
    test/build command rewritten to `cargo check` (codegen + harness dropped)."""
    import pipeline
    cases = {
        "cargo test -p anyhow --no-run": "cargo check -p anyhow",
        "cargo +nightly test -p x -- --test-threads=1": "cargo +nightly check -p x",
        "cargo test --no-fail-fast": "cargo check",
        "cargo check -p x": "cargo check -p x",
        "cargo build -p x": "cargo check -p x",
    }
    for src, want in cases.items():
        got = pipeline._derive_check_only_cmd(src)
        assert got == want, f"{src!r} → {got!r}, expected {want!r}"
    print("  OK  _derive_check_only_cmd (per_fn inner check)")


def test_splice_fn_body_allow_nonstub() -> None:
    """per_fn fix strategy: re-fill an already-filled (wrong) fn body. Default
    splice refuses a non-stub; allow_nonstub=True overwrites it."""
    import pipeline, tempfile
    from pathlib import Path
    src = (
        "impl Foo {\n"
        "    fn as_ref(&self) -> u32 {\n"
        "        self.wrong_field\n"
        "    }\n"
        "}\n"
    )
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "f.rs"
        p.write_text(src, encoding="utf-8")
        # Default: refuses to clobber a non-stub body.
        ok, _ = pipeline._splice_fn_body(p, "as_ref", "self.right_field")
        assert not ok, "default splice must refuse a non-stub body"
        assert "wrong_field" in p.read_text(encoding="utf-8")
        # allow_nonstub: overwrites it.
        ok, delta = pipeline._splice_fn_body(
            p, "as_ref", "self.right_field", allow_nonstub=True)
        assert ok, "allow_nonstub splice must succeed"
        out = p.read_text(encoding="utf-8")
        assert "right_field" in out and "wrong_field" not in out, out
        # Signature + surrounding impl preserved.
        assert "fn as_ref(&self) -> u32" in out and out.count("impl Foo") == 1, out
    print("  OK  _splice_fn_body allow_nonstub (per_fn re-fill)")


def test_render_impl_block() -> None:
    """per_impl fix unit: render the whole impl block (header + where-clause +
    all bodies) verbatim from the byte range containing the first member."""
    import pipeline
    src = (
        "use std::fmt;\n"
        "\n"
        "impl<T> AsRef<T> for Wrap<T> where T: ?Sized {\n"
        "    fn as_ref(&self) -> &T {\n"
        "        &self.0\n"
        "    }\n"
        "}\n"
        "\n"
        "fn free() {}\n"
    )
    sig = "    fn as_ref(&self) -> &T {"
    member = {"fn_name": "as_ref", "sig_byte_start": src.index(sig),
              "sig_text": "fn as_ref(&self) -> &T"}
    rendered = pipeline._render_impl_block(src, [member])
    assert rendered.startswith("impl<T> AsRef<T> for Wrap<T> where T: ?Sized"), rendered
    assert "fn as_ref" in rendered and rendered.rstrip().endswith("}"), rendered
    # The where-clause (the impl-level constraint per-fn prompts can't show) is
    # part of the rendered block — the whole point of the per_impl unit.
    assert "where T: ?Sized" in rendered, rendered
    # Does not bleed into the free fn outside the block.
    assert "fn free" not in rendered, rendered
    # Empty members → empty string (caller falls back).
    assert pipeline._render_impl_block(src, []) == ""
    print("  OK  _render_impl_block (per_impl fix unit)")


def test_strip_ansi_and_diag_frontier() -> None:
    """Inner fix-loop check must read STRUCTURED diags, not ANSI-rendered text.
    Regression: _run_cargo_check_diag emits json-diagnostic-rendered-ansi, and
    the plain-text _COMPILE_ERROR_RE can't parse colour codes — so re-parsing
    rendered text gave an empty frontier and the inner loop never asked the
    model (0 splices). The structured path is ANSI-immune."""
    import pipeline
    ansi = ("\x1b[0m\x1b[1m\x1b[38;5;9merror[E0201]\x1b[0m\x1b[1m: dup\x1b[0m\n"
            "  \x1b[38;5;12m-->\x1b[0m src/error.rs:1634:5")
    plain = pipeline._strip_ansi(ansi)
    assert "\x1b[" not in plain and "error[E0201]" in plain, plain
    # The plain-text regex matches only the de-ANSI'd text — proving why the
    # rendered-text path silently failed.
    assert not pipeline._COMPILE_ERROR_RE.search(ansi)
    assert pipeline._COMPILE_ERROR_RE.search(plain)
    # Structured-diag frontier never touches rendered text; empty/garbage in →
    # empty out (no crash), and it ignores ANSI entirely.
    assert pipeline._frontier_from_diags([], pipeline.Path(".")) == {}
    assert pipeline._frontier_from_diags(
        [{"file": "", "line": 0}], pipeline.Path(".")) == {}
    print("  OK  _strip_ansi + _frontier_from_diags (ANSI-immune inner check)")


def test_sig_tighten_guard_and_where_parse() -> None:
    """Fix-loop signature tightening: the model may add a leading `where` clause
    (parsed by extract_fn_bodies) but only PRIVATE fns may be touched
    (_is_pub_fn guard) — public signatures are the test surface, never changed."""
    import pipeline
    src = (
        "impl<T> Mut<T> {\n"
        "    fn read(self) -> T {\n"
        "        unimplemented!()\n"
        "    }\n"
        "    pub fn get(&self) -> &T {\n"
        "        unimplemented!()\n"
        "    }\n"
        "}\n"
    )
    # API-surface guard: private fn may be tightened, pub fn may not.
    assert pipeline._is_pub_fn(src, "read") is False
    assert pipeline._is_pub_fn(src, "get") is True
    # The model expresses a bound as a leading `where` line in the FN body;
    # extract_fn_bodies lifts it into where_clause and removes it from the body.
    resp = ('<FN name="read" block="impl">\n'
            'where T: Sized\n'
            'self.0\n'
            '</FN>')
    recs = pipeline.extract_fn_bodies(resp)
    assert len(recs) == 1, recs
    assert recs[0]["where_clause"] == "where T: Sized", recs[0]
    assert "where" not in recs[0]["body"] and "self.0" in recs[0]["body"], recs[0]
    print("  OK  sig-tighten guard (_is_pub_fn) + where-clause parse")


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
    test_strip_doc_attrs()
    test_condense_doc_attrs()
    test_cluster_gate_predicate_is_satisfiable()
    test_run_scoped_tests()
    test_generate_in_parallel()
    test_partition_uncovered_lines()
    test_collect_sibling_filled_bodies()
    test_count_test_fns_cfg_aware()
    test_validate_task3_strict()
    test_parse_test_counts()
    test_build_test_reverse_map()
    test_ast_cluster_merge_preserves_trait_structure()
    test_strip_preserves_gen_tests_verbatim()
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
    test_derive_check_only_cmd()
    test_splice_fn_body_allow_nonstub()
    test_render_impl_block()
    test_strip_ansi_and_diag_frontier()
    test_sig_tighten_guard_and_where_parse()

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
