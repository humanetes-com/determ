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

import pytest

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "fixtures" / "tiny"

sys.path.insert(0, str(REPO))

# Cargo-backed coverage tests are slow (~30-60s) and need cargo + a coverage backend
# (tarpaulin/grcov). Opt in with SMOKE_FULL=1; otherwise pytest skips them (and CI, which
# has no coverage toolchain, stays fast). Mirrors the old `--full` gate. The condition is a
# STRING so pytest evaluates it lazily at collection — after main() may have set the env.
needs_cargo = pytest.mark.skipif(
    "os.environ.get('SMOKE_FULL') != '1'", reason="needs cargo + coverage backend; set SMOKE_FULL=1"
)

# Some helpers now parse Rust source through the syn extractor binary (no regex on source).
# Tests of those skip when the binary isn't built (e.g. the CI Python job, which doesn't build
# the Rust crate). Built by `bash setup.sh`.
needs_extractor = pytest.mark.skipif(
    not (REPO / "tools" / "skeleton_extractor" / "target" / "release" / "skeleton-extractor").exists(),
    reason="skeleton-extractor binary not built (bash setup.sh)",
)


def _setup_workspace(name: str = "fixture-tiny-test") -> Path:
    workspace = REPO / "tmp" / name
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE, workspace)
    return workspace


# ─── Pure-Python phase ──────────────────────────────────────────────────────

def test_effective_stalls_passthrough_and_focus(monkeypatch) -> None:
    """Pass-through collapses every module's stall budget except the converge module."""
    import pipeline

    # Mode off → budget unchanged.
    monkeypatch.setattr(pipeline, "_PASS_THROUGH", False)
    assert pipeline._effective_stalls(3, "error") == 3

    # Mode on → non-focus modules collapse to PASS_THROUGH_STALLS.
    monkeypatch.setattr(pipeline, "_PASS_THROUGH", True)
    monkeypatch.setattr(pipeline, "_PASS_THROUGH_STALLS", 1)
    monkeypatch.setattr(pipeline, "_CONVERGE_MODULE", "context")
    assert pipeline._effective_stalls(3, "error") == 1
    assert pipeline._effective_stalls(3, None) == 1
    # Focus module keeps the full budget → converges for real.
    assert pipeline._effective_stalls(3, "context") == 3
    # Collapse never raises the budget above full.
    assert pipeline._effective_stalls(0, "error") == 0


def _fake_run_cargo(rc: int, out: str):
    """Fake `pipeline._run_cargo`: records cmds, returns canned (rc, out)."""
    calls: list[str] = []

    def _fn(cmd, cwd, env, *, timeout=180):
        calls.append(cmd)
        return rc, out

    _fn.calls = calls  # type: ignore[attr-defined]
    return _fn


def test_run_scoped_tests_shim_routes_through_run_cargo(monkeypatch, tmp_path) -> None:
    """Slice-9 seam: the _run_scoped_tests shim MUST go through pipeline._run_cargo
    (the monkeypatchable attr), not ADAPTERS.cargo."""
    import pipeline

    fake = _fake_run_cargo(0, "test a ... ok\ntest b ... ok")
    monkeypatch.setattr(pipeline, "_run_cargo", fake)
    assert pipeline._run_scoped_tests("cargo test", tmp_path, {}) == (True, 0, "", set())
    assert fake.calls == ["cargo test"]  # routed through the seam


def test_u2_test_gate_shim_routes_and_blocks(monkeypatch, tmp_path) -> None:
    """Slice-11 seam: the _u2_test_gate shim composes via pipeline._run_cargo + _guard."""
    import pipeline

    fake = _fake_run_cargo(101, "test b ... FAILED\nassertion failed")
    monkeypatch.setattr(pipeline, "_run_cargo", fake)
    monkeypatch.setattr(pipeline, "_UNIFIED_FILL_LOOP", True)
    res = pipeline._u2_test_gate({"b"}, "cargo test", tmp_path, {})
    assert res is not None
    sig, msg = res
    assert sig == frozenset({"b"}) and "still FAIL" in msg
    assert fake.calls  # routed through the patchable seam
    # gate disabled → accept regardless
    monkeypatch.setattr(pipeline, "_UNIFIED_FILL_LOOP", False)
    assert pipeline._u2_test_gate({"b"}, "cargo test", tmp_path, {}) is None


def test_extract_self_note_shim(monkeypatch) -> None:
    """Slice-12: the _extract_self_note shim runs the ask ladder + parse_self_note."""
    import pipeline

    note = "this is a sufficiently long self-correction note about lifetimes"
    monkeypatch.setattr(pipeline, "ask", lambda *a, **k: note)
    assert pipeline._extract_self_note("failbody", "err excerpt", "fixbody") == note
    # model declines → None
    monkeypatch.setattr(pipeline, "ask", lambda *a, **k: "NONE")
    assert pipeline._extract_self_note("failbody", "err excerpt", "fixbody") is None
    # empty inputs short-circuit before any ask
    assert pipeline._extract_self_note("", "err", "fix") is None


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


def test_gen_tests_origin_cfgs() -> None:
    """A gen_tests decl's origin cluster cfg is recovered from the parent source,
    so post-strip propagation can gate it to the cluster that generated it (not
    the current, always-active cluster) — preventing the nightly→no_core E0658 leak."""
    import pipeline

    nightly_cfg = 'any(error_generic_member_access, feature = "error_generic_member_access")'
    src = (
        "pub fn add(a: i32, b: i32) -> i32 { a + b }\n"
        + pipeline._build_gen_tests_decl("error.gen_tests_1.rs", 1, "#[allow(clippy::all)]")
        + pipeline._build_gen_tests_decl("error.gen_tests_7.rs", 7, "#[allow(clippy::all)]",
                                         cluster_cfg_attr=nightly_cfg)
    )
    origins = pipeline._gen_tests_origin_cfgs(src)
    assert origins[1] == "", "stable-origin (no extra cfg) must map to ''"
    assert origins[7] == nightly_cfg, "nightly-origin must recover the error_generic_member_access gate"

    # Round-trip through the on-disk manifest loader.
    ws = _setup_workspace("smoke-gt-origin")
    skel = ws / ".skeleton"
    skel.mkdir(parents=True, exist_ok=True)
    import json
    (skel / "_gen_tests_origin.json").write_text(json.dumps({
        "error.gen_tests_1": "",
        "error.gen_tests_7": nightly_cfg,
    }))
    loaded = pipeline._load_gen_tests_origin(ws)
    assert loaded["error.gen_tests_7"] == nightly_cfg
    assert loaded["error.gen_tests_1"] == ""
    assert pipeline._load_gen_tests_origin(ws / "nonexistent") == {}, \
        "missing manifest must return {} (older runs fall back to heuristic)"

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
    import tempfile
    from pathlib import Path

    import pipeline

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

    import pipeline

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
    # R7.4b: syn renders operators with a space — `all (…)`, `not (…)`.
    # These were silently undecidable (None) → wrong cluster membership.
    assert e('not (nightly)', true_set, false_set) is False
    assert e('all (nightly, feature = "std")', true_set, false_set) is True
    assert e('any (feature = "alloc", nightly)', true_set, false_set) is True
    assert e('not (all (nightly, feature = "std"))', true_set, false_set) is False
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
    from types import SimpleNamespace

    import pipeline
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
    import threading

    import pipeline
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
    # Stable: error_generic_member_access ∈ cfg_false → t_nightly_only excluded.
    # t_unknown is a CUSTOM `--cfg` flag set in NO cluster → cargo never runs it
    # → excluded (the 2026-06-16 hardcrate over-count fix; was wrongly "kept" via
    # undecidable). t_always counted; t_std_or_default kept (`not(anyhow_no_core_error)`
    # is true — that custom cfg is off by default). → 2.
    n_stable = pipeline._count_test_fns_cfg_aware(
        text,
        cfg_true=set(),
        cfg_false={"error_generic_member_access", "anyhow_no_core_error"},
    )
    assert n_stable == 2, f"stable count expected 2, got {n_stable}"
    # Nightly: error_generic_member_access ∈ cfg_true → t_nightly_only kept;
    # t_unknown still excluded (custom flag, off). → 3.
    n_nightly = pipeline._count_test_fns_cfg_aware(
        text,
        cfg_true={"error_generic_member_access"},
        cfg_false=set(),
    )
    assert n_nightly == 3, f"nightly count expected 3, got {n_nightly}"
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
    import tempfile

    import pipeline
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
    import subprocess
    import tempfile

    import pipeline

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
    import tempfile

    import pipeline
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
    We don't actually call the LLM — we inject a fake `ask` via deps and inspect
    the prompt string the retrospective would have sent. (P3.3: the helper now
    lives in task0c.py; ask is injected, no longer the patchable pipeline global.)"""
    from types import SimpleNamespace

    from pipeline_core.application.tasks.task0c import _engine_retrospective

    captured: dict = {}

    def fake_ask(prompt, **_kw):
        captured['prompt'] = prompt
        return "FUNCTION foo: UNREACHABLE — based on the prior failed attempts.\n"

    deps = SimpleNamespace(
        ask=fake_ask,
        SRC_SNIP_CAP=4000,
        TASK0C_MODEL="m",
        extract_branch_guard=lambda lines, lno: "",
        describe_test_environment=lambda: "Test env: cargo test\n",
    )

    result = _engine_retrospective(
        deps,
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

    prompt = captured['prompt']
    assert "PRIOR FAILED ATTEMPTS" in prompt
    assert "attempt 1" in prompt and "attempt 2" in prompt
    assert "did NOT improve coverage" in prompt
    assert "the classification was\nwrong" in prompt or "wrong" in prompt
    assert "set_var" in prompt, "concrete attempt content should appear in the prompt"
    assert "UNREACHABLE" in result, "fake LLM response should be returned"

    # Without unproductive_blocks, no PRIOR FAILED ATTEMPTS section.
    captured.clear()
    _engine_retrospective(
        deps,
        rel_path="src/foo.rs",
        existing_text="pub fn foo() {}\n",
        fn_bodies="pub fn foo() {}\n",
        old_block="",
        module_name="tiny",
    )
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


def test_cargo_toml_parsed_with_tomllib() -> None:
    """Cargo.toml facts come from tomllib, not regex — so comments, inline tables, and the
    correct [lib] scoping are handled (cases the old regex got wrong)."""
    import pipeline

    toml = (
        "[package]\n"
        'name = "my-crate"   # trailing comment\n'
        "version = \"0.1.0\"\n"
        "[lib]\n"
        "proc-macro = true\n"
        "[features]\n"
        'default = ["std", "backtrace"]\n'
        'std = []\n'
        "[dependencies]\n"
        'serde = { version = "1", features = ["derive"] }\n'
        '# proc-macro = true   ← a commented decoy the regex would have matched\n'
    )
    assert pipeline._cargo_package_name(toml) == "my-crate"
    assert pipeline._cargo_is_proc_macro(toml) is True
    assert set(pipeline._cargo_default_features(toml)) == {"std", "backtrace"}
    # A crate WITHOUT [lib] proc-macro: the commented decoy must not register.
    not_pm = '[package]\nname = "x"\n# proc-macro = true\n[dependencies]\n'
    assert pipeline._cargo_is_proc_macro(not_pm) is False
    # Malformed TOML degrades to empty, never raises.
    assert pipeline._load_cargo_toml("this is not [valid toml") == {}


@needs_extractor
def test_extract_public_symbols_via_syn() -> None:
    """_extract_public_symbols parses Rust source via the syn extractor (no regex on source).
    Confirm it surfaces pub structs (with fields), enums (with variants), traits, fn sigs, and
    consts — and excludes private items."""
    import pipeline

    src = (
        "pub struct Error { pub inner: Box<E>, code: u32 }\n"
        "pub enum Kind { A, B(u32) }\n"
        "pub trait Show { fn show(&self) -> String; }\n"
        "pub fn build(x: u32) -> Error { unimplemented!() }\n"
        "pub const MAX: usize = 10;\n"
        "struct Hidden { x: u8 }\n"          # private — must NOT appear
        "const SECRET: u8 = 1;\n"
    )
    out = pipeline._extract_public_symbols(src)
    assert "pub struct Error" in out and "inner" in out and "code" in out
    assert "pub enum Kind" in out and "A" in out
    assert "pub trait Show" in out
    assert "pub fn build" in out and "-> Error" in out
    assert "pub const MAX" in out
    assert "Hidden" not in out and "SECRET" not in out   # private excluded by syn visibility


@needs_extractor
def test_pub_items_from_tree_via_syn() -> None:
    """_pub_items_from_tree returns (kind, name) for pub struct/enum/trait/fn via syn, excludes
    private items and tests/examples dirs."""
    import tempfile
    from pathlib import Path

    import pipeline

    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "src"
        src.mkdir()
        (src / "lib.rs").write_text(
            "pub struct A; pub enum E { X } pub trait T {} pub fn f() {} struct Priv; pub fn g() {}",
            encoding="utf-8",
        )
        tests = Path(d) / "tests"
        tests.mkdir()
        (tests / "it.rs").write_text("pub fn should_not_appear() {}", encoding="utf-8")
        items = pipeline._pub_items_from_tree(Path(d))
    assert ("struct", "A") in items and ("enum", "E") in items and ("trait", "T") in items
    assert ("fn", "f") in items and ("fn", "g") in items
    assert not any(n == "Priv" for _, n in items)                 # private excluded by syn
    assert ("fn", "should_not_appear") not in items              # tests/ excluded by path


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


def test_monitor_coverage_from_events() -> None:
    """R8.4: the header coverage figure comes from the structured
    coverage.measured event (latest wins, legacy one-decimal shape);
    no event / no pct → "" so the legacy text scrapes take over."""
    import json
    import tempfile

    import monitor
    from pipeline_core.monitor import events_data

    with tempfile.TemporaryDirectory() as td:
        ev = os.path.join(td, "events.jsonl")
        old_log, old_start = events_data.EVENTS_LOG, events_data.pipeline_start_time
        events_data.EVENTS_LOG = ev
        events_data.pipeline_start_time = lambda: 0.0
        try:
            assert monitor._coverage_from_events() == ""  # no file yet
            with open(ev, "w") as fh:
                fh.write(json.dumps({"ts": "2026-06-10T00:00:00+00:00",
                                     "event": "coverage.measured",
                                     "data": {"pct": 83.87096}}) + "\n")
                fh.write("not json\n")  # garbage line tolerated
                fh.write(json.dumps({"ts": "2026-06-10T00:01:00+00:00",
                                     "event": "phase_c.pass.start", "data": {}}) + "\n")
                fh.write(json.dumps({"ts": "2026-06-10T00:02:00+00:00",
                                     "event": "coverage.measured",
                                     "data": {"pct": 97.2704}}) + "\n")
            assert monitor._coverage_from_events() == "97.3%"
            with open(ev, "w") as fh:
                fh.write(json.dumps({"ts": "2026-06-10T00:00:00+00:00",
                                     "event": "coverage.measured",
                                     "data": {}}) + "\n")
            assert monitor._coverage_from_events() == ""  # pct missing → fallback
        finally:
            events_data.EVENTS_LOG = old_log
            events_data.pipeline_start_time = old_start
    print("  OK  monitor coverage from events")


def test_genesis_rules_count() -> None:
    """R13.3: genesis_rules_count totals rules for a scope (module + '*'),
    independent of genesis_rules_read's LIMIT 20 render cap."""
    import tempfile
    from pathlib import Path

    import pipeline

    with tempfile.TemporaryDirectory() as td:
        pd = Path(td)
        assert pipeline.genesis_rules_count(pd, "error") == 0
        for i in range(25):
            pipeline.genesis_rules_write(pd, "error", "build", f"rule {i}")
        pipeline.genesis_rules_write(pd, "*", "build", "global rule")
        pipeline.genesis_rules_write(pd, "other", "build", "other-scope rule")
        # error scope sees its 25 + the global '*' rule, not the other module's.
        assert pipeline.genesis_rules_count(pd, "error") == 26
        assert pipeline.genesis_rules_count(pd, "*") == 1  # '*' scope alone
    print("  OK  genesis_rules_count")


def test_monitor_docs_richness_row() -> None:
    """R13.3: the docs-richness row renders the latest docs.richness event
    (fr/notes/rules + pass delta); no event → "" (row hidden)."""
    import json
    import tempfile

    import monitor
    from pipeline_core.monitor import events_data

    with tempfile.TemporaryDirectory() as td:
        ev = os.path.join(td, "events.jsonl")
        old_log, old_start = events_data.EVENTS_LOG, events_data.pipeline_start_time
        events_data.EVENTS_LOG = ev
        events_data.pipeline_start_time = lambda: 0.0
        try:
            assert monitor._docs_richness_row() == ""  # no file yet
            with open(ev, "w") as fh:
                fh.write(json.dumps({"ts": "2026-06-10T00:00:00+00:00",
                                     "event": "docs.richness",
                                     "data": {"fr_chars": 1200, "notes": 3,
                                              "genesis_rules": 5, "pass_count": 7,
                                              "prev_pass_count": 4,
                                              "improved": True}}) + "\n")
            row = monitor._plain(monitor._docs_richness_row())
            assert "fr=1200c" in row and "notes=3" in row and "rules=5" in row
            assert "4→7" in row
        finally:
            events_data.EVENTS_LOG = old_log
            events_data.pipeline_start_time = old_start
    print("  OK  monitor docs richness row")


def test_shutdown_on_finish_toggle() -> None:
    """Power-off-on-finish: the monitor-written sentinel ("1"/"0") overrides
    .env SHUTDOWN_ON_FINISH; no sentinel falls back to the env default;
    pipeline._shutdown_on_finish_enabled and monitor._shutdown_armed agree."""
    import tempfile

    import monitor
    import pipeline

    with tempfile.TemporaryDirectory() as td:
        sentinel = os.path.join(td, "shutdown_sentinel")
        old_path, old_mon = pipeline._SHUTDOWN_SENTINEL_PATH, monitor._SHUTDOWN_SENTINEL
        old_script_dir = monitor.SCRIPT_DIR
        old_env = os.environ.get("SHUTDOWN_ON_FINISH")
        pipeline._SHUTDOWN_SENTINEL_PATH = Path(sentinel)
        monitor._SHUTDOWN_SENTINEL = sentinel
        monitor.SCRIPT_DIR = td  # no .env here → monitor falls back to False
        try:
            os.environ["SHUTDOWN_ON_FINISH"] = "1"
            assert pipeline._shutdown_on_finish_enabled()      # env default applies
            monitor._set_shutdown_armed(False)                 # monitor disable wins
            assert not pipeline._shutdown_on_finish_enabled()
            assert not monitor._shutdown_armed()
            monitor._set_shutdown_armed(True)                  # monitor re-arm wins
            assert pipeline._shutdown_on_finish_enabled()
            assert monitor._shutdown_armed()
            os.unlink(sentinel)
            os.environ["SHUTDOWN_ON_FINISH"] = "0"
            assert not pipeline._shutdown_on_finish_enabled()  # back to env default
            with open(os.path.join(td, ".env"), "w") as fh:    # monitor's .env fallback
                fh.write("OTHER=x\nSHUTDOWN_ON_FINISH=1\n")
            assert monitor._shutdown_armed()
        finally:
            pipeline._SHUTDOWN_SENTINEL_PATH = old_path
            monitor._SHUTDOWN_SENTINEL = old_mon
            monitor.SCRIPT_DIR = old_script_dir
            if old_env is None:
                os.environ.pop("SHUTDOWN_ON_FINISH", None)
            else:
                os.environ["SHUTDOWN_ON_FINISH"] = old_env
    print("  OK  shutdown-on-finish toggle (sentinel overrides env)")


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

@needs_cargo
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


@needs_cargo
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
    import tempfile
    from pathlib import Path

    import pipeline
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
    from pipeline_core.domain.models import FnRecord
    member = FnRecord(fn_name="as_ref", sig_byte_start=src.index(sig),
                      sig_text="fn as_ref(&self) -> &T")
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
    the plain-text fallback parser can't parse colour codes — so re-parsing
    rendered text gave an empty frontier and the inner loop never asked the
    model (0 splices). The structured cargo-JSON path is ANSI-immune."""
    import pipeline
    ansi = ("\x1b[0m\x1b[1m\x1b[38;5;9merror[E0201]\x1b[0m\x1b[1m: dup\x1b[0m\n"
            "  \x1b[38;5;12m-->\x1b[0m src/error.rs:1634:5")
    plain = pipeline._strip_ansi(ansi)
    assert "\x1b[" not in plain and "error[E0201]" in plain, plain
    # The text-fallback parser (parse_rustc_compile_errors, migrated from the old
    # `_COMPILE_ERROR_RE` per R7) needs the `error` keyword at line start, so it
    # matches only the de-ANSI'd text — proving why the rendered-text path
    # silently failed; the structured cargo-JSON frontier below is the ANSI-immune
    # primary path.
    assert not pipeline.parse_rustc_compile_errors(ansi)
    assert pipeline.parse_rustc_compile_errors(plain)
    # Structured-diag frontier never touches rendered text; empty/garbage in →
    # empty out (no crash), and it ignores ANSI entirely.
    assert pipeline._frontier_from_diags([], pipeline.Path(".")) == {}
    # slice 6: diagnostics are typed Diagnostic records now (empty file → skipped → {}).
    assert pipeline._frontier_from_diags(
        [pipeline.Diagnostic(file="", line=0)], pipeline.Path(".")) == {}
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


def test_domain_enums_and_model_caps() -> None:
    """pipeline_core domain: StrEnum coerce + pure model-caps math (strangler slice 1)."""
    from pipeline_core.domain.enums import (
        Backend,
        CoverageBackend,
        FillUnit,
        ModelTier,
    )
    from pipeline_core.domain.model_caps import model_context_chars, model_tier

    # StrEnum members compare equal to their token → drop-in for string literals.
    assert Backend.OLLAMA == "ollama"
    assert FillUnit.IMPL == "impl"

    # coerce: case-insensitive, typed fallback, None → default.
    assert Backend.coerce("VLLM", Backend.OLLAMA) is Backend.VLLM
    assert Backend.coerce("  ollama ", Backend.VLLM) is Backend.OLLAMA
    assert Backend.coerce("nonsense", Backend.OLLAMA) is Backend.OLLAMA
    assert Backend.coerce(None, Backend.VLLM) is Backend.VLLM
    assert CoverageBackend.coerce("llvm-cov", CoverageBackend.TARPAULIN) is CoverageBackend.LLVM_COV

    # backend → context-length env var mapping.
    assert Backend.OLLAMA.max_len_env_var == "OLLAMA_NUM_CTX"
    assert Backend.HUGGINGFACE.max_len_env_var is None

    # model_tier classification (pure).
    assert model_tier("qwen3:32b") is ModelTier.MID
    assert model_tier("random-13b") is ModelTier.WEAK
    assert model_tier("x/llama-3.3-70b") is ModelTier.MID

    # model_context_chars budget math (pure).
    assert model_context_chars("qwen3", Backend.OLLAMA, 32768) == (32768 - 6_000) * 4
    assert model_context_chars("qwen3", Backend.OLLAMA, 6000) == 4_000 * 4  # floor
    assert model_context_chars("x", Backend.OLLAMA, None) == (8_192 - 6_000) * 4

    # Slice 2: module-level knobs are wired to the enum types, not bare strings.
    import pipeline
    assert isinstance(pipeline.COVERAGE_BACKEND, CoverageBackend)

    # Slice 3: one typed Settings singleton is the source of truth for these knobs.
    from pipeline_core.settings import Settings
    assert isinstance(pipeline.SETTINGS, Settings)
    assert pipeline.COVERAGE_BACKEND is pipeline.SETTINGS.coverage_backend
    assert isinstance(pipeline.SETTINGS.backend, Backend)
    assert isinstance(pipeline.SETTINGS.phase_c_fill_unit, FillUnit)

    # Settings owns the derived char budgets; pipeline constants are single-sourced from it.
    from pipeline_core.domain.model_caps import safe_input_char_limit
    s = pipeline.SETTINGS
    assert s.single_call_char_limit == pipeline.SINGLE_CALL_CHAR_LIMIT
    assert s.safe_input_char_limit == pipeline._SAFE_INPUT_CHAR_LIMIT
    assert s.safe_input_char_limit_task0c == pipeline._SAFE_INPUT_CHAR_LIMIT_TASK0C
    assert s.single_call_char_limit == min(s.safe_input_char_limit, s.model_context_chars * 3 // 4)
    assert s.intermediate_char_limit == s.model_context_chars // 2
    assert s.model == pipeline.MODEL
    # pure budget helper: floor + formula
    assert safe_input_char_limit(65536, 0, 16_384) == int((65536 - 512 - 16_384) * 3.0)
    assert safe_input_char_limit(1, 0, 0) == 4_000  # 4k floor

    # per-task model overrides single-sourced from Settings.task_models.
    from pipeline_core.settings import TaskModels
    tm = s.task_models
    assert isinstance(tm, TaskModels)
    assert tm.task0 == pipeline._TASK0_MODEL and tm.task4 == pipeline._TASK4_MODEL
    assert tm.task0c == pipeline._TASK0C_MODEL and tm.task2 == pipeline._TASK2_MODEL
    # default fallback: a task with no env override resolves to the global model.
    import os as _os
    if "TASK1_MODEL" not in _os.environ:
        assert TaskModels.from_env("the-default").task1 == "the-default"
    print("  OK  pipeline_core domain enums + model_caps + Settings budget + task_models (slice 1+2+3)")


def test_adapters_satisfy_ports() -> None:
    """Slice 4a: LLM / Extractor / Cargo adapters structurally satisfy their ports,
    and the composition root builds them on `pipeline.ADAPTERS`."""
    from pathlib import Path

    from pipeline_core.adapters import Adapters, CargoCli, CliExtractor, ModelInteractLLM, build_adapters
    from pipeline_core.adapters.cargo import Process
    from pipeline_core.ports import LLM, Cargo, Extractor, ProcessResult
    from pipeline_core.settings import Settings

    a = build_adapters(Settings.from_env(), extractor_bin=Path("/nonexistent/bin"), model="m")
    assert isinstance(a, Adapters)
    # runtime_checkable Protocols → structural isinstance.
    assert isinstance(a.llm, LLM) and isinstance(a.llm, ModelInteractLLM)
    assert isinstance(a.extractor, Extractor) and isinstance(a.extractor, CliExtractor)
    assert isinstance(a.cargo, Cargo) and isinstance(a.cargo, CargoCli)

    # Missing-binary extractor degrades to None / not-ok, never raises (parity with pipeline).
    assert a.extractor.enumerate_fns(Path("/tmp")) is None
    assert a.extractor.merge_clusters(Path("/tmp/x"), []) is False
    assert a.extractor.skeletonise(Path("/tmp"), Path("/tmp/out"))["ok"] is False

    # Cargo: bogus binary → rc=-1, structured Process satisfying ProcessResult.
    p = a.cargo.run("definitely-not-cargo --version", Path("/tmp"), env={})
    assert isinstance(p, Process) and isinstance(p, ProcessResult)
    assert p.returncode == -1 and "NOT FOUND" in p.stderr

    import pipeline
    assert isinstance(pipeline.ADAPTERS, Adapters)
    print("  OK  pipeline_core adapters satisfy ports (slice 4a)")


def test_coverage_workspace_adapters() -> None:
    """Slice 4b: relocated coverage parse paths + GitWorkspace behave identically,
    and pipeline's shims delegate to the adapters. No cargo needed — exercises the
    transcription-risk pure logic (lcov parse, grcov-json parse, git read/write/commit)."""
    import json as _json
    import os
    import subprocess
    import tempfile
    from pathlib import Path

    from pipeline_core.adapters import coverage as cov
    from pipeline_core.adapters import workspace as ws
    from pipeline_core.domain.enums import CoverageBackend
    from pipeline_core.ports import Coverage, Workspace

    deps = cov.CoverageDeps(
        log=lambda m: None, cargo_env=lambda: {}, cluster=lambda: None,
        tarpaulin_bin=Path("/nonexistent/tarp"), grcov_bin=Path("/nonexistent/grcov"),
        build_timeout=1, ignore_pragma="GRCOV_EXCL_LINE",
    )
    prov = cov.make_coverage_provider(CoverageBackend.LLVM_COV, deps)
    assert isinstance(prov, Coverage)  # measure(...) shape

    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        # default-cluster (None) exclude patterns parity with the old _cluster_exclude_patterns.
        assert cov.cluster_exclude_patterns(deps) == ["*/nightly.rs"]

        # lcov parse: one src file, 2/3 lines covered; pragma line excluded.
        (proj / "src").mkdir()
        (proj / "src" / "f.rs").write_text("a\nb // GRCOV_EXCL_LINE\nc\n")
        lcov = proj / "cov.lcov"
        lcov.write_text("SF:src/f.rs\nDA:1,5\nDA:2,0\nDA:3,0\nend_of_record\n")
        pct, files, covd, total = prov._parse_lcov(lcov, proj)  # type: ignore[attr-defined]
        assert (covd, total) == (1, 2)  # line 2 dropped by pragma; 1 of {1,3} hit
        assert files and files[0]["abs_path"].endswith("src/f.rs")

        # grcov-json parse (load_grcov_json reads /tmp/grcov_<mod>.json).
        mod = f"covtest_{os.getpid()}"
        gj = Path("/tmp") / f"grcov_{mod}.json"
        gj.write_text(_json.dumps({"source_files": [{"name": "src/f.rs", "coverage": [1, 0, None, 2]}]}))
        try:
            pct, files, covd, total = cov.load_grcov_json(proj, mod)
            assert (covd, total) == (2, 3)
        finally:
            gj.unlink(missing_ok=True)

        # GitWorkspace: read/write/commit on a real temp repo.
        subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=proj, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=proj, check=True)
        wsd = ws.WorkspaceDeps(log=lambda m: None, emit=lambda *a, **k: None)
        gw = ws.GitWorkspace(proj, wsd, managed=True)
        assert isinstance(gw, Workspace)
        gw.write("hello.txt", "hi")
        assert gw.read("hello.txt") == "hi"
        assert gw.commit("first") in (None,)  # commit returns None, must not raise
        # second commit with no changes → underlying git_commit returns False (nothing staged)
        assert ws.git_commit(wsd, proj, "noop", managed=True) is False
        assert ws.git_commit(wsd, proj, "skip", managed=False) is False  # unmanaged guard

    # pipeline shims delegate to the adapter (identity of provider + module funcs).
    import pipeline
    assert isinstance(pipeline._coverage_provider, cov.CoverageProvider)
    print("  OK  pipeline_core coverage + workspace adapters (slice 4b)")


def test_slice5a_shims_delegate_to_adapters() -> None:
    """Slice 5a: pipeline's extractor + cargo shims delegate to ADAPTERS (no duplicated
    subprocess logic), and _run_cargo preserves the legacy (rc, text) contract."""
    import types
    from pathlib import Path

    import pipeline
    from pipeline_core.adapters.cargo import Process

    calls: list[tuple] = []

    from pipeline_core.domain.models import FnRecord, ModRecord, TypeRecord

    class _FakeExtractor:
        # slice 6: the adapter contract returns typed records, not dicts.
        def enumerate_fns(self, d, **k): calls.append(("fns", d)); return [FnRecord(fn_name="x")]
        def enumerate_types(self, d, **k): calls.append(("types", d)); return [TypeRecord(name="T")]
        def enumerate_dispatch_families(self, d, **k): calls.append(("disp", d)); return []
        def enumerate_feature_gate_cfgs(self, d, **k): calls.append(("feat", d)); return ["nightly"]
        def enumerate_mods(self, d, **k): calls.append(("mods", d)); return [ModRecord(name="m")]
        def skeletonise(self, i, o, **k): calls.append(("skel", i, o, k)); return {"ok": True, "reports": [], "stderr": ""}

    class _FakeCargo:
        def __init__(self): self.last = None
        def run(self, cmd, cwd, *, env, timeout=180):
            self.last = (cmd, cwd, env, timeout)
            if cmd == "boom":
                return Process(-1, "", "TIMEOUT after 5s")
            return Process(0, "out", "err")

    orig = pipeline.ADAPTERS
    pipeline.ADAPTERS = types.SimpleNamespace(extractor=_FakeExtractor(), cargo=_FakeCargo())
    try:
        d = Path("/tmp/x")
        # fns wrapper re-serialises typed records to wire dicts for the fill engine.
        assert pipeline._enumerate_fns_via_extractor(d) == [FnRecord(fn_name="x").to_dict()]
        # types/mods/dispatch wrappers pass the typed records straight through.
        assert pipeline._enumerate_types_via_extractor(d) == [TypeRecord(name="T")]
        assert pipeline._dispatch_families_via_extractor(d) == []
        assert pipeline._feature_gate_cfgs_via_extractor(d) == ["nightly"]
        assert pipeline._enumerate_mods_via_extractor(d) == [ModRecord(name="m")]
        r = pipeline._extract_skeleton(Path("/in"), Path("/out"))
        assert r["ok"] is True
        assert {c[0] for c in calls} == {"fns", "types", "disp", "feat", "mods", "skel"}

        # _run_cargo: success → (rc, combined stdout+stderr-block); error rc=-1 → bare stderr.
        rc, out = pipeline._run_cargo("ok", Path("/tmp"), {})
        assert rc == 0 and out == "out\n--- stderr ---\nerr"
        rc, out = pipeline._run_cargo("boom", Path("/tmp"), {})
        assert rc == -1 and out == "TIMEOUT after 5s"  # no stderr-block prefix on error
    finally:
        pipeline.ADAPTERS = orig
    print("  OK  pipeline extractor + cargo shims delegate to ADAPTERS (slice 5a)")


def test_slice5b_task0_docs() -> None:
    """Slice 5b: extracted task0 profile/context doc tasks generate + write via fakes,
    and the fence-fallback path uses strip_markdown_fences (no regex in the clean layer)."""
    from pathlib import Path

    from pipeline_core.application.tasks.task0_docs import Task0DocsDeps, task0_project_context, task0_project_profile

    written: dict = {}
    marked: list = []

    def mk(ask_resp: str) -> Task0DocsDeps:
        return Task0DocsDeps(
            phase_valid=lambda d, k: False, mark_phase=lambda *a, **k: marked.append(a),
            ask=lambda prompt, model=None: ask_resp,
            write=lambda p, c: written.__setitem__(Path(p).name, c),
            prompt_budget=lambda *fixed: 10_000,
            task0_model="m", model="m", readme_cap=3000, profile_cap=2000,
            project_context_name="project_context.md",
        )

    tf = [("tests/a.rs", "fn t(){}")]
    # <FILE> path
    r = task0_project_profile(Path("/tmp/zz/project_profile.md"), tf, "readme",
                              mk('<FILE path="project_profile.md">PROFILE BODY</FILE>'))
    assert r == "PROFILE BODY" and written["project_profile.md"] == "PROFILE BODY"
    # fence-fallback path (model ignored <FILE>): strip_markdown_fences kicks in
    written.clear()
    body = "x" * 150
    r = task0_project_context(Path("/tmp/zz/project_context.md"), tf, "readme", "profile",
                              mk(f"```md\n{body}\n```"))
    assert r == body and written["project_context.md"] == body
    print("  OK  task0 doc tasks extracted (slice 5b)")


def test_slice5b_task0b_discovery() -> None:
    """Slice 5b: extracted TASK 0b builds the strategy doc, dedups lines, writes via fakes;
    the fence fallback uses strip_markdown_fences (no regex in the clean layer)."""
    import tempfile
    from pathlib import Path

    from pipeline_core.application.tasks.task0b_discovery import Task0bDeps, task0b_discovery

    written: dict = {}
    deps = Task0bDeps(
        ask=lambda prompt, model=None: '<FILE path="coverage_strategy.md">A\nA\nB</FILE>',
        write=lambda p, c: written.__setitem__("s", c),
        is_test_file=lambda f, d: False, is_gen_tests_file=lambda n: False,
        strip_gen_tests_decls=lambda t: t, condense_rust_to_signatures=lambda raw, filename="": raw,
        manifest_name=".generated_files.txt", sibling_cap=3000,
        single_call_char_limit=120_000, task0_model="m",
    )
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        (proj / "src").mkdir()
        (proj / "src" / "lib.rs").write_text("pub fn x() {}")
        r = task0b_discovery(proj / "coverage_strategy.md", proj, [("tests/a.rs", "fn t(){}")],
                             "readme", "profile", deps)
    # dedup collapsed the repeated "A"
    assert written["s"] == "A\nB" and r == "A\nB"
    print("  OK  task0b discovery extracted (slice 5b)")


def test_slice5b_task1() -> None:
    """Slice 5b: extracted TASK 1 dispatches to the synthesizer, and the proc-macro path
    builds requirements via fakes (Rust-source regex stays injected from pipeline)."""
    from pathlib import Path
    from types import SimpleNamespace

    from pipeline_core.application.tasks.task1 import Task1Deps, task1

    calls: dict = {}

    def mk(synth_ok=True, ask_resp="") -> Task1Deps:
        synth = SimpleNamespace(synthesize=lambda *a, **k: calls.setdefault("synth", True) or synth_ok)
        return Task1Deps(
            load_api_surface=lambda d, m: "", read=lambda p: "",
            ask=lambda prompt, model=None: ask_resp,
            write_fr_preserving_notes=lambda p, c: calls.__setitem__("fr", c),
            synthesizer_factory=lambda: synth,
            filter_proc_macro_test_files=lambda tf, cn: tf,
            proc_macro_fallback_requirements=lambda rel, cn: "FALLBACK",
            format_api_surface_section=lambda s: "", file_format_instruction="FMT",
            single_call_char_limit=120_000, profile_cap=2000, task1_model="m",
        )

    tf = [("tests/a.rs", "#[derive(Foo)] struct S;")]
    # standard path → synthesizer.synthesize drives it
    assert task1(Path("."), Path("g"), Path("r"), tf, "readme", mk(True)) is True
    assert calls.get("synth") is True
    # proc-macro path → writes FR from a <FILE> response
    calls.clear()
    r = task1(Path("."), Path("g"), Path("r"), tf, "readme",
              mk(ask_resp='<FILE path="functional_requirements.md">PM REQS</FILE>'),
              crate_type="proc-macro", module_name="mycrate")
    assert r is True and calls["fr"] == "PM REQS"
    print("  OK  task1 (dispatcher + proc-macro) extracted (slice 5b)")


def test_slice5b_task0c() -> None:
    """Slice 5b: extracted task0c engine — verbatim body behind a rebind-prelude. Exercise the
    early-skip path (augmentor not applicable) with all-stub deps; confirm pipeline builds the
    real _TASK0C_DEPS (which validates all injected names resolve)."""
    from pathlib import Path
    from types import SimpleNamespace

    from pipeline_core.application.tasks.task0c import Task0cDeps, task0c_augment_module

    stub = {f: (lambda *a, **k: None) for f in Task0cDeps.__dataclass_fields__}
    stub["test_augmentor_factory"] = lambda: SimpleNamespace(is_applicable=lambda ct: False)
    stub["get_current_cluster"] = lambda: None
    deps = Task0cDeps(**stub)
    # crate type not applicable → returns (100.0, 0, False, "") before any cargo/LLM work.
    assert task0c_augment_module(Path("."), "m", Path("."), deps, crate_type="proc-macro") == (100.0, 0, False, "")

    import pipeline
    assert isinstance(pipeline._TASK0C_DEPS, Task0cDeps)
    print("  OK  task0c engine extracted behind orchestrator (slice 5b)")


def test_slice6_models() -> None:
    """Slice 6: domain dataclasses for the Rust↔Python extractor seam. Verify tolerant
    from_dict (missing keys default, nested records parse, extra keys ignored) and that
    to_dict round-trips the exact wire shape (the seam is sacred — CODE_ARCHITECTURE.md §4)."""
    from pipeline_core.domain.models import DispatchFamily, FieldRecord, FnRecord, MethodRecord, ModRecord, TypeRecord

    # FnRecord wire dict (every field as the Rust extractor emits it).
    fn_wire = {
        "file": "src/error.rs", "fn_name": "backtrace", "sig_text": "pub fn backtrace(&self) -> &Backtrace",
        "body_text": "unimplemented!()", "sig_byte_start": 10, "body_byte_start": 50, "body_byte_end": 70,
        "cfg_predicates": ["feature = \"std\""], "is_pub": True, "container": "impl Error",
        "has_unimplemented_body": True,
    }
    fr = FnRecord.from_dict(fn_wire)
    assert fr.fn_name == "backtrace" and fr.container == "impl Error" and fr.has_unimplemented_body
    assert fr.cfg_predicates == ("feature = \"std\"",) and fr.sig_byte_start == 10
    assert fr.to_dict() == fn_wire, "FnRecord round-trip must equal the wire dict byte-for-byte"

    # Tolerant: empty dict → all defaults, no crash; extra keys ignored.
    blank = FnRecord.from_dict({"unknown_future_field": 1})
    assert blank.fn_name == "" and blank.cfg_predicates == () and blank.body_byte_end == 0

    # TypeRecord with nested field/method records.
    ty_wire = {
        "file": "src/lib.rs", "kind": "struct", "name": "Error", "generics": "", "definition": "struct Error",
        "fields": [{"name": "inner", "ty": "Own<ErrorImpl>", "is_pub": False}],
        "variants": [], "methods": [{"name": "new", "sig": "fn new() -> Self", "is_pub": True,
                                     "cfg_predicates": ["feature = \"std\""]}], "is_pub": True,
    }
    tr = TypeRecord.from_dict(ty_wire)
    assert isinstance(tr.fields[0], FieldRecord) and tr.fields[0].ty == "Own<ErrorImpl>"
    assert isinstance(tr.methods[0], MethodRecord) and tr.methods[0].sig == "fn new() -> Self"
    assert tr.methods[0].cfg_predicates == ("feature = \"std\"",)
    assert tr.to_dict() == ty_wire

    mod = ModRecord.from_dict({"name": "error", "file": "error.rs", "cfg_predicates": [], "declared_in": "lib.rs"})
    assert mod.name == "error" and mod.declared_in == "lib.rs" and mod.cfg_predicates == ()

    fam_wire = {"method": "kind", "traits": ["AdhocKind", "TraitKind"], "macros": ["anyhow"],
                "trait_files": ["kind.rs"], "macro_defs": ["macro_rules!{}"], "return_types": ["Adhoc"],
                "chained_methods": ["new"], "member_files": ["kind.rs"]}
    fam = DispatchFamily.from_dict(fam_wire)
    assert fam.traits == ("AdhocKind", "TraitKind") and fam.method == "kind"
    assert fam.to_dict() == fam_wire

    # Diagnostic — the cargo↔Python seam (slice 6, 2nd unit).
    from pipeline_core.domain.models import Diagnostic
    diag_wire = {"code": "E0277", "file": "src/error.rs", "line": 42,
                 "message": "the trait bound is not satisfied", "rendered": "error[E0277]..."}
    dg = Diagnostic.from_dict(diag_wire)
    assert dg.code == "E0277" and dg.file == "src/error.rs" and dg.line == 42
    assert dg.to_dict() == diag_wire
    # tolerant: no code / no span.
    blank_dg = Diagnostic.from_dict({"message": "linker error"})
    assert blank_dg.code == "" and blank_dg.line == 0 and blank_dg.message == "linker error"
    print("  OK  domain models for extractor + cargo seams (slice 6)")


def test_slice6_models_seam_wiring() -> None:
    """Slice 6: the Extractor adapter parses JSON into typed records; the pipeline.py fns
    wrapper re-serialises to dicts via to_dict so the dict-based fill engine is unchanged."""
    from pathlib import Path

    from pipeline_core.adapters.extractor import CliExtractor
    from pipeline_core.domain.models import FnRecord

    # Missing binary → None (not a crash), preserved from the dict era.
    ext = CliExtractor(Path("/nonexistent/skeleton-extractor"))
    assert ext.enumerate_fns(Path(".")) is None
    assert ext.enumerate_types(Path(".")) is None

    # The adapter's parse step yields typed records from wire dicts.
    rec = FnRecord.from_dict({"fn_name": "f", "file": "a.rs"})
    # The pipeline wrapper hands the engine dicts (to_dict), not dataclasses.
    assert rec.to_dict()["fn_name"] == "f"
    print("  OK  extractor seam typed; engine still dict-fed (slice 6)")


def test_stub_model() -> None:
    """R3.2: Stub is the per-fn fill unit's input. from_dict is tolerant of BOTH
    a full FnRecord.to_dict() (extra keys ignored) and the hand-built 4-key
    fallback; from_record projects an FnRecord; key is the (file, fn_name)
    identity used for frontier/ordering."""
    from pipeline_core.domain.models import FnRecord, Stub

    # From a full FnRecord wire dict — extra keys (sig_byte_start, body_text, …) ignored.
    fr = FnRecord.from_dict({
        "file": "src/error.rs", "fn_name": "backtrace", "container": "impl Error",
        "sig_text": "pub fn backtrace(&self) -> &Backtrace", "body_text": "unimplemented!()",
        "sig_byte_start": 10, "has_unimplemented_body": True,
    })
    s = Stub.from_dict(fr.to_dict())
    assert s.file == "src/error.rs" and s.fn_name == "backtrace"
    assert s.container == "impl Error" and s.sig_text.startswith("pub fn backtrace")
    assert s.key == ("src/error.rs", "backtrace")
    assert s.to_dict() == {"file": "src/error.rs", "fn_name": "backtrace",
                           "container": "impl Error", "sig_text": "pub fn backtrace(&self) -> &Backtrace"}

    # From the hand-built fallback dict (no container/sig_text) — None coerces to "".
    s2 = Stub.from_dict({"file": "a.rs", "fn_name": "f", "container": None, "sig_text": None})
    assert s2.container == "" and s2.sig_text == "" and s2.key == ("a.rs", "f")

    # from_record projects exactly the four fields.
    assert Stub.from_record(fr) == s
    # tolerant: empty dict → all-default Stub, no crash.
    assert Stub.from_dict({}).key == ("", "")
    print("  OK  Stub model: tolerant from_dict / from_record / key (R3.2)")


def test_impl_block_model() -> None:
    """R3.2: ImplBlock mirrors the _collect_impl_blocks dict. from_dict parses
    members/stub_members as FnRecords (typed threading); to_dict round-trips the
    exact wire shape; id = file::container (the per-block lock key)."""
    from pipeline_core.domain.models import FnRecord, ImplBlock

    # Members are full FnRecord wire dicts (what _collect_impl_blocks groups).
    fmt = FnRecord(file="src/error.rs", fn_name="fmt", container="impl Display for Error",
                   sig_text="fn fmt(&self, f: &mut Formatter) -> Result",
                   has_unimplemented_body=True).to_dict()
    helper = FnRecord(file="src/error.rs", fn_name="helper",
                      container="impl Display for Error").to_dict()
    wire = {
        "file": "src/error.rs",
        "container": "impl Display for Error",
        "members": [fmt, helper],
        "stub_members": [fmt],
        "related_containers": ["impl Debug for Error"],
    }
    blk = ImplBlock.from_dict(wire)
    assert blk.file == "src/error.rs" and blk.container == "impl Display for Error"
    assert isinstance(blk.members[0], FnRecord) and blk.members[0].fn_name == "fmt"
    assert isinstance(blk.stub_members[0], FnRecord) and blk.stub_members[0].has_unimplemented_body
    assert blk.related_containers == ("impl Debug for Error",)
    assert blk.id == "src/error.rs::impl Display for Error"
    assert blk.to_dict() == wire, "ImplBlock round-trip must equal the wire dict"

    # tolerant: empty / missing member lists → all-default block, no crash.
    blank = ImplBlock.from_dict({"file": "a.rs"})
    assert blank.members == () and blank.stub_members == () and blank.related_containers == ()
    print("  OK  ImplBlock model: typed members + round-trip + id (R3.2)")


def test_slice5b_task2() -> None:
    """Slice 5b: extracted TASK 2 dispatcher + inner loop (no-build-check path) + fix loop
    (empty manifest) behave with fakes; generation engine stays injected from pipeline."""
    from pathlib import Path
    from types import SimpleNamespace

    from pipeline_core.application.tasks.task2 import Task2Deps, task2, task2_fix_loop, task2_inner_loop

    gen = SimpleNamespace(generate=lambda *a, **k: True)
    deps = Task2Deps(
        generator_factory=lambda: gen, load_manifest=lambda p: [], read=lambda p: "",
        generate_fix_loop=lambda **k: (True, "ok"), store_prompt_content=lambda s: s,
        is_proc_macro_crate=lambda t: False, count_error_codes=lambda o: 0,
        build_check_retries=3, build_max_stalls=2, build_timeout=1, test_timeout=1,
        task2_model="m", task2_fix_retries=2, single_call_char_limit=120_000, genesis_cap=3000,
    )
    # dispatcher → generator.generate
    assert task2(Path("."), Path("g"), Path("r"), Path("m"), set(), deps) is True
    # inner loop with no build check → True after one generate
    assert task2_inner_loop(Path("."), Path("g"), Path("r"), Path("m"), set(),
                            "prof", "ws", None, deps) is True
    # fix loop with empty manifest → (False, test_output)
    assert task2_fix_loop(Path("."), Path("m"), set(), "OUT", "cargo test",
                          Path("r"), deps) == (False, "OUT")
    print("  OK  task2 (dispatcher + inner/fix loops) extracted (slice 5b)")


def test_slice5b_task3_task4() -> None:
    """Slice 5b: extracted TASK 3 (test runner) + TASK 4 (failure analysis) drive their
    branches correctly with injected fakes — no cargo, no LLM."""
    from contextlib import contextmanager
    from pathlib import Path
    from types import SimpleNamespace

    import pipeline_core.application.tasks.task3 as t3mod
    from pipeline_core.application.tasks.task3 import Task3Deps, task3
    from pipeline_core.application.tasks.task4 import Task4Deps, task4
    from pipeline_core.domain.llm_output import GENESIS_DELIM_END, GENESIS_DELIM_START

    @contextmanager
    def fake_phase(*a, **k):
        yield {}

    # --- TASK 3 legacy fallback: no baked clusters → delegate to task3_legacy_single ---
    d_legacy = Task3Deps(
        phase=fake_phase, load_baked_clusters=lambda p: [],
        task3_legacy_single=lambda *a: ("LEGACY", "out"),
        cargo_env=dict, parse_cmd=lambda c: c.split(),
        count_active_tests_for_cluster=lambda p, c: 0, store_prompt_content=lambda s: s,
        task3_inline_retry=lambda **k: (False, ""), read=lambda p: "", write=lambda p, c: None,
        test_timeout=1,
    )
    assert task3(Path("."), "cargo test", 1, ".", Path("g"), d_legacy) == ("LEGACY", "out")

    # --- TASK 3 all-pass: monkeypatch subprocess in the task3 module ---
    cluster = SimpleNamespace(name="stable", toolchain=None, env_extra={})
    fake_run = lambda *a, **k: SimpleNamespace(
        returncode=0, stdout="test result: ok. 2 passed; 0 failed; 0 ignored", stderr="")
    written: dict = {}
    d_pass = Task3Deps(
        phase=fake_phase, load_baked_clusters=lambda p: [cluster],
        task3_legacy_single=lambda *a: ("X", "X"),
        cargo_env=dict, parse_cmd=lambda c: c.split(),
        count_active_tests_for_cluster=lambda p, c: 2, store_prompt_content=lambda s: len(s),
        task3_inline_retry=lambda **k: (False, ""),
        read=lambda p: "genesis", write=lambda p, c: written.__setitem__("t3", c),
        test_timeout=5,
    )
    orig_run = t3mod.subprocess.run
    t3mod.subprocess.run = fake_run
    try:
        passed, agg = task3(Path("."), "cargo test", 1, ".", Path("g"), d_pass)
    finally:
        t3mod.subprocess.run = orig_run
    assert passed is True and "stable" in agg and "t3" in written

    # --- TASK 4: ask returns a genesis update → written + (True, current_pass) ---
    t4_written: dict = {}
    resp = f"{GENESIS_DELIM_START}\nnew-genesis-methodology\n{GENESIS_DELIM_END}"
    d4 = Task4Deps(
        ask=lambda prompt, model=None: resp,
        read=lambda p: "a" * 300,  # genesis/req content
        write=lambda p, c: t4_written.__setitem__("g", c),
        extract_and_store_rules=lambda *a, **k: None,
        model="m", task4_model="m4", verbosity_guard="",
        intermediate_char_limit=50_000, single_call_char_limit=120_000,
    )
    out = "test result: ok. 3 passed; 1 failed; 0 ignored"
    ok, cur = task4(Path("g"), Path("r"), out, 2, ".", d4, failure_type="test")
    assert ok is True and cur == 3 and t4_written["g"] == "new-genesis-methodology"
    print("  OK  task3 + task4 extracted behind orchestrator (slice 5b)")


def test_slice5b_genesis_audit_task() -> None:
    """Slice 5b: extracted genesis_audit use case behaves per-branch with injected deps,
    and pipeline's shim + deps record are wired."""
    from pathlib import Path

    from pipeline_core.application.tasks.genesis_audit import GenesisAuditDeps, genesis_audit
    from pipeline_core.domain.llm_output import GENESIS_DELIM_END, GENESIS_DELIM_START

    written: dict = {}

    def mk_deps(genesis_text: str, response: str) -> GenesisAuditDeps:
        # log + retry + extract_genesis are now imported (infra / domain.llm_output),
        # not injected. Only ask/read/write/model/prompt-strings remain.
        return GenesisAuditDeps(
            ask=lambda prompt, model=None: response,
            read=lambda p: genesis_text,
            write=lambda p, c: written.__setitem__("c", c),
            model="m", verbosity_guard="",
        )

    long = "x" * 250  # over the 200-char audit threshold

    # too short → False, no write
    assert genesis_audit(Path("g"), mk_deps("short", "whatever")) is False
    # clean → False, no write
    written.clear()
    assert genesis_audit(Path("g"), mk_deps(long, "AUDIT_CLEAN")) is False
    assert "c" not in written
    # contradictions resolved → True, writes content extracted between the real delimiters
    written.clear()
    resp = f"{GENESIS_DELIM_START}\nfixed-genesis-body\n{GENESIS_DELIM_END}"
    assert genesis_audit(Path("g"), mk_deps(long, resp)) is True
    assert written["c"] == "fixed-genesis-body"

    import pipeline
    assert isinstance(pipeline._GENESIS_AUDIT_DEPS, GenesisAuditDeps)

    # 5b foundation: test-result helpers relocated to domain; pipeline re-exports the
    # SAME objects (single source of truth) so call sites + tasks share one definition.
    from pipeline_core.domain import test_results as tr
    assert pipeline._parse_test_counts is tr._parse_test_counts
    assert pipeline.count_passing_tests is tr.count_passing_tests
    assert pipeline.validate_task3_output_strict is tr.validate_task3_output_strict
    assert pipeline.extract_test_output_from_file is tr.extract_test_output_from_file
    assert pipeline.OUTPUT_SEPARATOR == tr.OUTPUT_SEPARATOR == "=== test output ==="

    # 5b foundation: pure text helpers relocated to domain.text; pipeline re-exports them
    # (strip_markdown_fences aliased to the legacy _strip_markdown_fences name).
    from pipeline_core.domain import text as txt
    assert pipeline._strip_markdown_fences is txt.strip_markdown_fences
    assert pipeline.extract_code_content is txt.extract_code_content
    assert pipeline.trim_to_line is txt.trim_to_line
    assert pipeline.smart_truncate is txt.smart_truncate
    assert txt.strip_markdown_fences("```rust\nlet x = 1;\n```") == "let x = 1;"
    assert txt.smart_truncate("abcdef", total_limit=4, head=2, tail=2).startswith("ab")
    assert txt.trim_to_line("a\nbb\nccc", 4) == "a" + "\n// ... (truncated)"

    # 5b infra: log + retry seams. pipeline re-exports with_retry; sinks wired to _log/_emit.
    from pipeline_core import log as pclog
    from pipeline_core import retry as pcretry
    assert pipeline.with_retry is pcretry.with_retry
    assert pclog._log_sink is pipeline._log and pclog._emit_sink is pipeline._emit
    assert pcretry.MAX_RETRIES == pipeline.MAX_RETRIES
    # with_retry contract: returns first non-False result; returns False if all attempts fail.
    assert pcretry.with_retry(lambda: "ok", retries=2) == "ok"
    _n = [0]
    def _always_false():
        _n[0] += 1
        return False
    assert pcretry.with_retry(_always_false, retries=2) is False and _n[0] == 2
    # 5b foundation: <FILE>/genesis extraction relocated to domain.llm_output; re-exported.
    from pipeline_core.domain import llm_output as lo
    assert pipeline.extract_files is lo.extract_files
    assert pipeline._extract_genesis is lo._extract_genesis
    assert pipeline.GENESIS_DELIM_START == lo.GENESIS_DELIM_START == "===GENESIS_START==="
    assert lo.extract_files('<FILE path="a.rs">fn x(){}</FILE>') == {"a.rs": "fn x(){}"}

    # 5b foundation: read/write homed in pipeline_core.io; pipeline re-exports them.
    from pipeline_core import io as pcio
    assert pipeline.read is pcio.read and pipeline.write is pcio.write

    # genesis_audit deps shed everything cross-cutting/derivable (de-ceremony payoff):
    # log/retry/extract_genesis imported, delimiters imported (prompt == parse), leaving
    # only ask/read/write/model/verbosity_guard.
    for gone in ("log", "retry", "extract_genesis", "delim_start", "delim_end"):
        assert gone not in GenesisAuditDeps.__dataclass_fields__
    assert set(GenesisAuditDeps.__dataclass_fields__) == {
        "ask", "read", "write", "model", "verbosity_guard"}
    print("  OK  genesis_audit + test_results + text + infra + llm_output + io (slice 5b)")


def test_task0b_iter_source_rs_prunes_scratch_dirs() -> None:
    """R9.3: the discovery file walk must yield real source only — never the
    pipeline scratch trees (.skeleton / .skeleton-filled, each with its own src/
    + nested target/ build output). Those phantom files exhausted the budget."""
    import tempfile
    from pathlib import Path

    from pipeline_core.application.tasks.task0b_discovery import _iter_source_rs

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        real = [root / "src" / "lib.rs", root / "src" / "error.rs"]
        phantom = [
            root / ".skeleton" / "stable" / "src" / "lib.rs",
            root / ".skeleton-filled" / "nightly" / "src" / "macros.rs",
            root / ".skeleton-filled" / "nightly" / "target" / "debug" / "build" / "x" / "out" / "private.rs",
            root / "target" / "debug" / "build" / "y" / "out" / "gen.rs",
            root / ".git" / "hooks" / "weird.rs",
        ]
        for p in real + phantom:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("fn x() {}\n")

        got = {p.relative_to(root).as_posix() for p in _iter_source_rs(root)}
        assert got == {"src/lib.rs", "src/error.rs"}, got
        print(f"  OK  _iter_source_rs pruned scratch/build dirs ({len(got)} real file(s))")


def test_validate_task4_output_build_skips_identifier_check() -> None:
    """R9.5: the identifier-overlap anti-corruption check is a TEST-failure
    heuristic and must NOT apply to BUILD failures — the build prompt mandates
    general, identifier-free structural rules, so a correct build update shares
    no identifier with the (identifier-heavy) compiler output."""
    import pipeline

    before = "# Methodology\n\nStep 1: read the tests.\n"
    # A general, identifier-free structural rule (what the build prompt asks for).
    general_update = (
        before
        + "\nStep 2: for any module system, identify and require every declared "
        "entry-point file and its module structure before generating code.\n"
    )
    # Compiler output full of project-specific identifiers — zero overlap with the
    # general rule above.
    compiler_output = "error[E0658]: use of unstable library feature; request_ref Backtrace StdError"

    # TEST mode: empty intersection → rejected (existing behaviour preserved).
    ok_test, _ = pipeline.validate_task4_output(
        before, general_update, compiler_output, failure_type="test")
    assert ok_test is False

    # BUILD mode: identifier check skipped → accepted.
    ok_build, reason = pipeline.validate_task4_output(
        before, general_update, compiler_output, failure_type="build")
    assert ok_build is True, reason

    # BUILD mode still catches real corruption: no-change stalls, shrink rejected.
    assert pipeline.validate_task4_output(before, before, compiler_output,
                                          failure_type="build")[0] is False
    assert pipeline.validate_task4_output(before, before[:10], compiler_output,
                                          failure_type="build")[0] is False
    print("  OK  validate_task4_output build-mode skips identifier check (R9.5)")


def test_propagate_or_quarantine_orphan_sibling() -> None:
    """R9.6: the origin manifest is authoritative. A sibling on disk but absent
    from a NON-empty manifest is an undeclared orphan → quarantined (.rs.broken),
    not propagated under a guessed gate. Empty manifest → legacy pass-through."""
    import tempfile
    from pathlib import Path

    import pipeline

    with tempfile.TemporaryDirectory() as td:
        src = Path(td)
        orphan = src / "error.gen_tests_5.rs"
        declared = src / "error.gen_tests_1.rs"
        orphan.write_text("fn t() {}\n")
        declared.write_text("fn t() {}\n")
        manifest = {"error.gen_tests_1": "", "context.gen_tests_2": ""}

        # Orphan (gen_tests_5 not in manifest) → quarantined, returns False.
        assert pipeline._propagate_or_quarantine_sibling(
            orphan, manifest, "error", 5, log_fn=None, tag="t") is False
        assert not orphan.exists()
        assert (src / "error.gen_tests_5.rs.broken").exists()

        # Declared sibling → propagate, returns True, untouched.
        assert pipeline._propagate_or_quarantine_sibling(
            declared, manifest, "error", 1, log_fn=None, tag="t") is True
        assert declared.exists()

        # Empty manifest (pre-bake/legacy) → always propagate, no quarantine.
        legacy = src / "lib.gen_tests_9.rs"
        legacy.write_text("fn t() {}\n")
        assert pipeline._propagate_or_quarantine_sibling(
            legacy, {}, "lib", 9, log_fn=None, tag="t") is True
        assert legacy.exists()
        print("  OK  _propagate_or_quarantine_sibling quarantines orphans (R9.6)")


def test_revert_gen_tests_pass_reconciles_all_siblings() -> None:
    """R9.6 atomicity: reverting to a snapshot that drops MULTIPLE decls (split/
    retry added extra siblings) must delete every sibling whose decl the restore
    removed — not just the one named sibling — so no orphan file is left behind."""
    import tempfile
    from pathlib import Path

    import pipeline

    with tempfile.TemporaryDirectory() as td:
        src_dir = Path(td)
        parent = src_dir / "error.rs"
        sib1 = src_dir / "error.gen_tests_1.rs"
        sib2 = src_dir / "error.gen_tests_2.rs"
        snapshot = "fn real() {}\n"  # pre-attempt text: NO gen_tests decls
        # Current state: both siblings on disk, both declared in the parent.
        parent.write_text(snapshot + "\n#[cfg(test)]\nmod gen_tests_1;\n"
                          "#[cfg(test)]\nmod gen_tests_2;\n")
        sib1.write_text("fn t1() {}\n")
        sib2.write_text("fn t2() {}\n")

        # Revert naming only sib1 — sib2's decl is also dropped by the snapshot.
        pipeline._revert_gen_tests_pass(parent, snapshot, sib1)

        assert parent.read_text() == snapshot
        assert not sib1.exists()
        assert not sib2.exists(), "sib2 orphaned — reconciliation missed it"
        print("  OK  _revert_gen_tests_pass reconciles all dropped siblings (R9.6)")


def test_cluster_filter_type_methods_drops_cfg_dead_methods():
    """TYPES_IN_SCOPE must advertise only methods that compile in the cluster
    (Hard Rule 8). In a no_core cluster, a constructor gated
    `any(feature="std", not(anyhow_no_core_error))` must be dropped, while an
    ungated one and an undecidable-cfg one are kept."""
    import pipeline
    from pipeline_core.domain.models import MethodRecord, TypeRecord
    err = TypeRecord(name="Error", kind="struct", methods=(
        MethodRecord(name="construct_from_display", sig="fn construct_from_display(...)"),
        MethodRecord(name="new", sig="fn new(...)",
                     cfg_predicates=("any(feature = \"std\", not(anyhow_no_core_error))",)),
        MethodRecord(name="provide", sig="fn provide(...)",
                     cfg_predicates=("error_generic_member_access",)),  # undecidable → keep
    ))
    cluster = pipeline.BuildCluster(
        name="anyhow_no_core_error", env_extra={}, tarpaulin_exclude_files=[],
        excluded_cfg=["std"], active_cfg=["anyhow_no_core_error"],
        prompt_nightly_rule="")
    [out] = pipeline._cluster_filter_type_methods([err], cluster)
    kept = {m.name for m in out.methods}
    assert kept == {"construct_from_display", "provide"}, kept
    # None cluster → unchanged identity.
    assert pipeline._cluster_filter_type_methods([err], None) == [err]


# ─── Driver ─────────────────────────────────────────────────────────────────

def main() -> int:
    """Back-compat CLI shim: delegate to pytest so there is ONE source of truth for which
    tests exist (pytest collection), not a hand-maintained call list that silently drifts.
        python tests/test_pipeline_smoke.py          # fast suite (cargo tests skipped)
        python tests/test_pipeline_smoke.py --full   # + cargo coverage tests
    """
    if "--full" in sys.argv:
        os.environ["SMOKE_FULL"] = "1"
    return pytest.main([__file__, "-q"])


if __name__ == "__main__":
    sys.exit(main())


def test_tag_block_shapes():
    # slice 8a: the one wrapper for optional prompt sections — pins the exact
    # legacy rendering (leading/trailing newlines, prefix/suffix placement).
    import pipeline as p
    assert p._tag_block("X", "") == ""
    assert p._tag_block("X", "c") == "\n<X>\nc\n</X>\n"
    assert (p._tag_block("X", "c", prefix="h:\n", suffix="\n```")
            == "\n<X>\nh:\nc\n```\n</X>\n")


def test_per_impl_prompt_macros_and_siblings_sections():
    # Per-impl/per-fn prompt parity: MACROS + SIBLING_REFERENCE render when
    # provided and vanish when empty (the per-impl builder lacked both).
    import pipeline as p
    from pipeline_core.domain.models import FnRecord, ImplBlock
    from pipeline_core.prompts.context import PerImplSections
    members = (FnRecord(file="lib.rs", fn_name="kind", container="impl Foo",
                        sig_text="fn kind (& self) -> u32", has_unimplemented_body=True),)
    block = ImplBlock(file="lib.rs", container="impl Foo",
                      members=members, stub_members=members)
    kw = {"block": block, "related_blocks": [], "test_blocks": [],
          "module_name": "m", "cluster_name": "t"}

    def _impl_sections(**over):
        base = {"types": "", "invariants": "", "notes": "", "crate_idioms": "",
                "dispatch_family": "", "macros": "", "siblings": "",
                "retrospective_guidance": ""}
        base.update(over)
        return PerImplSections(**base)

    full = p._build_per_impl_prompt(
        sections=_impl_sections(macros="macro_rules! mk { () => {} }",
                                siblings="fn kind(&self) -> u32 { 7 }"), **kw)
    assert "<MACROS>" in full and "macro_rules! mk" in full
    assert "<SIBLING_REFERENCE>" in full and "{ 7 }" in full
    bare = p._build_per_impl_prompt(sections=_impl_sections(), **kw)
    assert "<MACROS>" not in bare and "<SIBLING_REFERENCE>" not in bare


def test_fn_belongs_to_cluster_record_cfg_path(tmp_path):
    # R7.4a: callers holding extractor records pass the record's
    # cfg_predicates — syn-accurate, per-record. The legacy regex
    # backward-window runs ONLY when no record data is given, and its
    # over-capture (preceding fn's cfg bleeding onto an ungated neighbour
    # when fns are not blank-line separated) is exactly why.
    import pipeline
    cluster = pipeline.BuildCluster(
        name="no_std", env_extra={}, tarpaulin_exclude_files=[],
        excluded_cfg=['feature = "std"'], active_cfg=[],
        prompt_nightly_rule="",
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_text(
        '#[cfg(feature = "std")]\n'
        'pub fn gated() {}\n'
        'pub fn open_fn() {}\n'
    )
    b = pipeline._fn_belongs_to_cluster
    # Record path: extractor says open_fn is ungated → in every cluster.
    assert b(src, "lib.rs", "open_fn", cluster, fn_cfg_predicates=[]) is True
    # Record path: gated() carries the std cfg → excluded from no_std.
    assert b(src, "lib.rs", "gated", cluster,
             fn_cfg_predicates=['feature = "std"']) is False
    # Legacy fallback (no record data): the window over-captures gated()'s
    # cfg for open_fn → wrongly excluded. Documents the bug the record
    # path fixes; if the fallback regex is ever fixed, update this line.
    assert b(src, "lib.rs", "open_fn", cluster) is False


def test_per_fn_prompts_carry_crate_idioms():
    # Parity: CRATE_IDIOMS (patterns mined from already-filled fns) was
    # per-impl-only; both per-fn builders must render it when provided
    # and omit it when empty.
    import pipeline
    from pipeline_core.prompts.context import PerFnFixSections, PerFnSections
    common = {
        "file_text": "fn target() { unimplemented!() }",
        "fn_name": "target", "sig_text": "fn target()",
        "test_blocks": [], "module_name": "m", "cluster_name": "t",
        "file_rel": "lib.rs",
    }
    base = {"macros": "", "notes": "", "container": "", "types": "",
            "invariants": "", "siblings": ""}

    def _fn(crate_idioms=""):
        return PerFnSections(crate_idioms=crate_idioms, **base)

    def _fix(crate_idioms=""):
        return PerFnFixSections(crate_idioms=crate_idioms, retrospective_guidance="", **base)

    idiom = "# Self::construct_from_display family\nSelf::construct_from_display(arg, None)"
    full = pipeline._build_per_fn_prompt(sections=_fn(idiom), **common)
    assert "<CRATE_IDIOMS>" in full and "construct_from_display" in full
    assert "<CRATE_IDIOMS>" not in pipeline._build_per_fn_prompt(sections=_fn(), **common)
    fix = pipeline._build_per_fn_fix_prompt(
        prev_body="1", error_excerpt="E0308", sections=_fix(idiom), **common)
    assert "<CRATE_IDIOMS>" in fix and "construct_from_display" in fix
    assert "<CRATE_IDIOMS>" not in pipeline._build_per_fn_fix_prompt(
        prev_body="1", error_excerpt="E0308", sections=_fix(), **common)
