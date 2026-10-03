"""build_test_reverse_map must index inline `#[cfg(test)]` unit tests living in
src/ (not just tests/*.rs), so a failing inline test attributes to the production
fn it exercises instead of dying with `no_actionable_refs` (the hardcrate
`extra_cap_limit` → `cap_limit` churn)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.attribution import (
    AttributionDeps,
    attribute_failures,
    build_test_reverse_map,
    expand_frontier_by_calls,
    not_body_repairable_tests,
)
from pipeline_core.domain.models import FnLocator


class _FakeStructure:
    def __init__(self, recs: list[dict[str, Any]]) -> None:
        self._recs = recs

    def enumerate_fns(self, _src_dir: Path) -> list[dict[str, Any]]:
        return self._recs


def _deps(recs: list[dict[str, Any]]) -> AttributionDeps:
    return AttributionDeps(
        structure=_FakeStructure(recs),
        classify=lambda **_k: {},
        locate_fn=lambda *_a, **_k: None,
        run_check_diag=lambda *_a, **_k: (0, "", []),
        derive_check_only_cmd=lambda c: c,
        is_gen_tests_file=lambda _f: False,
        log=lambda _m: None,
    )


def test_inline_cfg_test_maps_to_production_fn(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    body = (
        "pub fn cap_limit(n: u32) -> u32 { n.min(255) }\n"
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    use super::*;\n"
        "    #[test]\n"
        "    fn extra_cap_limit() { assert_eq!(cap_limit(999), 255); }\n"
        "}\n"
    )
    raw = body.encode("utf-8")
    (src / "lib.rs").write_bytes(raw)

    test_start = raw.index(b"    fn extra_cap_limit")
    recs = [
        {"file": "lib.rs", "fn_name": "cap_limit", "cfg_predicates": []},
        {
            "file": "lib.rs", "fn_name": "extra_cap_limit",
            "cfg_predicates": ["test"],
            "sig_byte_start": test_start,
            "body_byte_end": len(raw),
        },
    ]
    rmap = build_test_reverse_map(_deps(recs), [], src)

    assert ("lib.rs", "cap_limit") in rmap["extra_cap_limit"]
    # The test fn is never a candidate (immutable oracle).
    assert ("lib.rs", "extra_cap_limit") not in rmap["extra_cap_limit"]


def test_no_src_tests_yields_empty(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_bytes(b"pub fn f() {}\n")
    recs = [{"file": "lib.rs", "fn_name": "f", "cfg_predicates": []}]
    assert build_test_reverse_map(_deps(recs), [], src) == {}


def test_assertion_panic_falls_through_to_reverse_map(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    """An assertion failure panics at the inline test's OWN line, so path 1
    (runtime panics) resolves to the immutable test fn. The per-path oracle
    strip must drop it and let the chain fall through to the reverse-map,
    attributing the failure to the production fn `cap_limit`."""
    monkeypatch.setenv("PHASE_C_TDD_USE_CLASSIFIER", "0")
    src = tmp_path / "src"
    src.mkdir()
    lines = [
        "pub fn cap_limit(n: u32) -> u32 { n.min(255) }\n",
        "#[cfg(test)]\n",
        "mod tests {\n",
        "    use super::*;\n",
        "    #[test]\n",
        "    fn extra_cap_limit() { assert_eq!(cap_limit(999), 512); }\n",
        "}\n",
    ]
    body = "".join(lines)
    raw = body.encode("utf-8")
    abs_lib = src / "lib.rs"
    abs_lib.write_bytes(raw)

    line6_start = len("".join(lines[:5]))
    recs = [
        {"file": "lib.rs", "fn_name": "cap_limit", "cfg_predicates": [],
         "sig_byte_start": 0, "body_byte_end": len(lines[0])},
        {"file": "lib.rs", "fn_name": "extra_cap_limit",
         "cfg_predicates": ["test"],
         "sig_byte_start": line6_start, "body_byte_end": line6_start + len(lines[5])},
    ]
    output = (
        "running 2 tests\n"
        "test extra_cap_limit ... FAILED\n\n"
        "failures:\n\n"
        f"---- extra_cap_limit stdout ----\n"
        f"thread 'extra_cap_limit' panicked at {abs_lib}:6:9:\n"
        "assertion `left == right` failed\n"
    )
    reverse_map = {"extra_cap_limit": {("lib.rs", "cap_limit")}}

    _res = attribute_failures(
        _deps(recs), output, src, recently_filled=[],
        test_reverse_map=reverse_map, cluster_name="stable")
    frontier = _res.frontier

    assert ("lib.rs", "cap_limit") in frontier
    assert ("lib.rs", "extra_cap_limit") not in frontier


# --- deterministic behavior attribution in the TDD chain ------- #

def test_behavior_path_fires_before_classifier(tmp_path: Path) -> None:
    """Assertion failure with a clean build and test texts available → the
    deterministic behavior path attributes it; the LLM classifier is never
    called (Hard Rule 9a)."""
    from pipeline_core.domain.rust_fn_headers import locate_fn

    def _classify_boom(**_k: Any) -> dict[str, dict[str, Any]]:
        raise AssertionError("classifier must not run when behavior attributes")

    recs = [
        {"file": "src/lib.rs", "fn_name": "cap_limit", "cfg_predicates": [],
         "container": ""},
    ]
    deps = AttributionDeps(
        structure=_FakeStructure(recs),
        classify=_classify_boom,
        locate_fn=locate_fn,
        run_check_diag=lambda *_a, **_k: (0, "", []),
        derive_check_only_cmd=lambda c: c,
        is_gen_tests_file=lambda _f: False,
        log=lambda _m: None,
    )
    test_texts = {
        "tests/cap.rs": ("#[test]\n"
                         "fn t_cap() { assert_eq!(cap_limit(999), 255); }\n"),
    }
    output = ("running 1 test\ntest t_cap ... FAILED\n\nfailures:\n\n"
              "---- t_cap stdout ----\n"
              "assertion `left == right` failed\n")
    _res = attribute_failures(
        deps, output, tmp_path, recently_filled=[],
        test_reverse_map={}, cluster_name="stable", test_texts=test_texts)
    frontier, kind = _res.frontier, _res.kind
    assert kind == "behavior"
    key = FnLocator("src/lib.rs", "cap_limit")
    assert key in frontier and frontier[key] == {"t_cap"}


def test_merges_runtime_and_behavioral_paths(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    """The fix: when a stub still panics (path 1) AND other tests assertion-fail
    (path 3), the frontier must surface BOTH — not just the panicking stub. The
    old first-non-empty chain let exhausted stub-panics starve behavioral
    attribution forever (the anyhow 14-ceiling)."""
    import pipeline_core.application.reconstruct.attribution as attr
    a = FnLocator("src/a.rs", "stub_fn")
    b = FnLocator("src/b.rs", "wrong_fn")
    monkeypatch.setattr(attr, "extract_tdd_frontier", lambda *_a, **_k: {a: {"t_panic"}})
    monkeypatch.setattr(attr, "extract_tdd_compile_frontier", lambda *_a, **_k: {})
    monkeypatch.setattr(attr, "behavior_frontier_from_texts",
                        lambda *_a, **_k: ({b: {"t_assert"}}, {}))
    _res = attribute_failures(
        _deps([]), "out", tmp_path, recently_filled=[], test_reverse_map={},
        cluster_name="stable", test_texts={"tests/x.rs": "…"})
    frontier, kind = _res.frontier, _res.kind
    assert kind == "merged"
    assert frontier[a] == {"t_panic"} and frontier[b] == {"t_assert"}


def test_behavior_does_not_convict_a_test_runtime_located_elsewhere(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    """RUNTIME located test_boxed_str at construct_from_boxed. BEHAVIOR also
    names it on `from` (Error token overlap). Dispatch still lists `from`;
    convicting_for(`from`) does not contain the test — run
    463713-b7c666c8's live mix."""
    import pipeline_core.application.reconstruct.attribution as attr
    boxed = FnLocator("src/error.rs", "construct_from_boxed")
    from_fn = FnLocator("src/error.rs", "from")
    monkeypatch.setattr(
        attr, "extract_tdd_frontier",
        lambda *_a, **_k: {boxed: {"test_boxed_str"}})
    monkeypatch.setattr(attr, "extract_tdd_compile_frontier", lambda *_a, **_k: {})
    monkeypatch.setattr(
        attr, "behavior_frontier_from_texts",
        lambda *_a, **_k: ({from_fn: {
            "test_boxed_str", "message_error_debug_with_string"}}, {}))
    _res = attribute_failures(
        _deps([]), "out", tmp_path, recently_filled=[], test_reverse_map={},
        cluster_name="stable", test_texts={"tests/x.rs": "…"})
    assert "test_boxed_str" in _res.frontier[from_fn]
    assert "test_boxed_str" not in _res.convicting_for(from_fn)
    assert "test_boxed_str" in _res.convicting_for(boxed)
    assert "message_error_debug_with_string" in _res.convicting_for(from_fn)


def test_single_deterministic_path_keeps_its_kind(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    import pipeline_core.application.reconstruct.attribution as attr
    a = FnLocator("src/a.rs", "stub_fn")
    monkeypatch.setattr(attr, "extract_tdd_frontier", lambda *_a, **_k: {a: {"t"}})
    monkeypatch.setattr(attr, "extract_tdd_compile_frontier", lambda *_a, **_k: {})
    monkeypatch.setattr(attr, "behavior_frontier_from_texts", lambda *_a, **_k: ({}, {}))
    _res = attribute_failures(
        _deps([]), "out", tmp_path, recently_filled=[], test_reverse_map={},
        cluster_name="stable", test_texts={"tests/x.rs": "…"})
    frontier, kind = _res.frontier, _res.kind
    assert kind == "runtime" and a in frontier


def test_behavior_path_skipped_without_texts(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    """No test_texts (fix-loop callers unchanged) → chain behaves as before:
    falls through to the reverse map."""
    monkeypatch.setenv("PHASE_C_TDD_USE_CLASSIFIER", "0")
    recs = [{"file": "src/lib.rs", "fn_name": "cap_limit", "cfg_predicates": []}]
    output = ("running 1 test\ntest t_cap ... FAILED\n\nfailures:\n")
    _res = attribute_failures(
        _deps(recs), output, tmp_path, recently_filled=[],
        test_reverse_map={"t_cap": {FnLocator("src/lib.rs", "cap_limit")}},
        cluster_name="stable")
    frontier, kind = _res.frontier, _res.kind
    assert kind == "reverse_map"
    assert FnLocator("src/lib.rs", "cap_limit") in frontier


def test_vanished_tests_merge_into_nonempty_frontier(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    """A vanished test (its binary segfaulted → NO line in the output) must
    reach the frontier via the reverse map even when other tests' stub panics
    keep paths 1-3 non-empty — otherwise the crashing filled body is never
    re-dispatched (the anyhow vtable-segfault plateau: fallbacks only ran on
    an empty merged frontier)."""
    monkeypatch.setenv("PHASE_C_TDD_USE_CLASSIFIER", "0")
    src = tmp_path / "src"
    src.mkdir()
    body = "pub fn stub_fn() { unimplemented!() }\npub fn crashy() {}\n"
    (src / "lib.rs").write_text(body, encoding="utf-8")
    recs = [
        {"file": "lib.rs", "fn_name": "stub_fn", "cfg_predicates": [],
         "sig_byte_start": 0, "body_byte_end": body.index("\n") + 1},
        {"file": "lib.rs", "fn_name": "crashy", "cfg_predicates": []},
    ]
    # Path 1 evidence: a stub panic from a DIFFERENT, still-reporting test.
    output = (
        "running 1 test\n"
        "test test_stub ... FAILED\n\n"
        "---- test_stub stdout ----\n"
        "thread 'test_stub' panicked at src/lib.rs:1:20:\n"
        "not implemented\n"
    )
    reverse_map = {"test_vanished": {("lib.rs", "crashy")}}
    _res = attribute_failures(
        _deps(recs), output, src, recently_filled=[],
        test_reverse_map=reverse_map, cluster_name="stable",
        extra_failed=frozenset({"test_vanished"}))
    frontier = _res.frontier
    assert ("lib.rs", "crashy") in frontier
    assert frontier[("lib.rs", "crashy")] == {"test_vanished"}


# --- conviction scope: only direct evidence may revert a fill --------------- #

def test_reverse_map_vanished_dispatches_without_convicting(
    tmp_path: Path,
) -> None:
    """A vanished test routed through the static reverse map says "this fn could
    reach it", never "this fn broke it". It must dispatch the fn and stay OUT of
    the conviction set, or a compiling fill is reverted for a test it has no
    observed link to — on every attempt, forever."""
    src = tmp_path / "src"
    src.mkdir()
    body = "pub fn stub_fn() { unimplemented!() }\npub fn crashy() {}\n"
    (src / "lib.rs").write_text(body, encoding="utf-8")
    recs = [
        {"file": "lib.rs", "fn_name": "stub_fn", "cfg_predicates": [],
         "sig_byte_start": 0, "body_byte_end": body.index("\n") + 1},
        {"file": "lib.rs", "fn_name": "crashy", "cfg_predicates": []},
    ]
    output = (
        "running 1 test\n"
        "test test_stub ... FAILED\n\n"
        "---- test_stub stdout ----\n"
        "thread 'test_stub' panicked at lib.rs:1:20:\n"
        "not implemented\n"
    )
    _res = attribute_failures(
        _deps(recs), output, src, recently_filled=[],
        test_reverse_map={"test_vanished": {("lib.rs", "crashy")}},
        cluster_name="stable", extra_failed=frozenset({"test_vanished"}))
    # Dispatch sees both; conviction sees only the located stub panic.
    assert ("lib.rs", "crashy") in _res.frontier
    assert not _res.convicting_for(FnLocator("lib.rs", "crashy"))
    assert _res.convicting_for(FnLocator("lib.rs", "stub_fn")) == {"test_stub"}


def test_classifier_fallback_convicts_nobody(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    """The LLM classifier's opinion dispatches but never convicts — a
    non-deterministic guess must not be able to revert a compiling body."""
    import pipeline_core.application.reconstruct.attribution as attr
    monkeypatch.setattr(attr, "extract_tdd_frontier", lambda *_a, **_k: {})
    monkeypatch.setattr(attr, "extract_tdd_compile_frontier", lambda *_a, **_k: {})
    monkeypatch.setattr(attr, "parse_failed_tests", lambda _o: {"t_x"})
    deps = AttributionDeps(
        structure=_FakeStructure([]),
        classify=lambda **_k: {
            "t_x": {"category": "BAD_FILL", "fn": "src/a.rs::wrong_fn"}},
        locate_fn=lambda *_a, **_k: None,
        run_check_diag=lambda *_a, **_k: (0, "", []),
        derive_check_only_cmd=lambda c: c,
        is_gen_tests_file=lambda _f: False,
        log=lambda _m: None,
    )
    _res = attribute_failures(
        deps, "out", tmp_path, recently_filled=[], test_reverse_map={},
        cluster_name="stable")
    assert _res.kind == "classifier"
    assert _res.frontier[FnLocator("src/a.rs", "wrong_fn")] == {"t_x"}
    assert _res.convicting == {}


# ── the compile-fail harness, and the callee walk ────────────────────────────

def test_a_trybuild_harness_convicts_nothing(tmp_path: Path) -> None:
    """`tests/compiletest.rs` is 7 lines and its only crate-name tokens are the
    `new` of `TestCases::new()` and the `not` of `#[rustversion::attr(not(
    nightly), …)]`. On the 2026-08-05 merged tree that convicted five unrelated
    `new` fns and `lib.rs::not`, each at a ~2-minute model call, for a test no
    body rewrite can satisfy."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "kind.rs").write_text("fn new() {}\n")
    (src / "lib.rs").write_text("fn not() {}\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    ct = tests / "compiletest.rs"
    ct.write_text(
        "#[rustversion::attr(not(nightly), ignore = \"requires nightly\")]\n"
        "#[test]\n"
        "fn ui() {\n"
        "    let t = trybuild::TestCases::new();\n"
        "    t.compile_fail(\"tests/ui/*.rs\");\n"
        "}\n")
    deps = _deps([{"file": "kind.rs", "fn_name": "new"},
                  {"file": "lib.rs", "fn_name": "not"}])
    assert build_test_reverse_map(deps, [ct], src) == {}
    assert not_body_repairable_tests([ct]) == frozenset({"ui"})


def test_an_ordinary_test_file_is_untouched_by_the_harness_check(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "kind.rs").write_text("fn new() {}\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    tf = tests / "test_boxed.rs"
    tf.write_text("#[test]\nfn test_boxed_str() { new(); }\n")
    deps = _deps([{"file": "kind.rs", "fn_name": "new"}])
    assert build_test_reverse_map(deps, [tf], src) == {
        "test_boxed_str": {("kind.rs", "new")}}
    assert not_body_repairable_tests([tf]) == frozenset()


def test_the_harness_is_excluded_from_the_path_that_CONVICTS(tmp_path: Path) -> None:
    """The reverse map is not the only route a compile-fail harness can convict
    through, and it is not the dangerous one.

    The behavioral path reads the failing test's OWN body and intersects its
    identifiers with the crate's fn records — and it is a CONVICTING kind, so its
    frontier may revert a unit. `ui`'s body is `trybuild::TestCases::new()` plus
    a glob, so a crate with a free `new` is named by it on identifier
    coincidence alone. The first cut of the exclusion filtered only inside the
    helper the fallback paths call, which left this one uncovered.
    """
    from pipeline_core.domain.rust_fn_headers import locate_fn

    recs = [{"file": "src/kind.rs", "fn_name": "new", "container": ""}]
    deps = AttributionDeps(
        structure=_FakeStructure(recs),
        classify=lambda **_k: {},
        locate_fn=locate_fn,
        run_check_diag=lambda *_a, **_k: (0, "", []),
        derive_check_only_cmd=lambda c: c,
        is_gen_tests_file=lambda _f: False,
        log=lambda _m: None,
    )
    texts = {"tests/compiletest.rs": (
        "#[test]\n"
        "fn ui() {\n"
        "    let t = trybuild::TestCases::new();\n"
        "    t.compile_fail(\"tests/ui/*.rs\");\n"
        "}\n")}
    output = ("running 1 test\ntest ui ... FAILED\n\nfailures:\n    ui\n")

    unguarded = attribute_failures(
        deps, output, tmp_path, recently_filled=[], test_reverse_map={},
        cluster_name="nightly", test_texts=texts)
    assert unguarded.convicting_for(FnLocator("src/kind.rs", "new")) == {"ui"}, \
        "without the exclusion the harness convicts by coincidence"

    guarded = attribute_failures(
        deps, output, tmp_path, recently_filled=[], test_reverse_map={},
        cluster_name="nightly", test_texts=texts,
        unrepairable_tests=frozenset({"ui"}))
    assert guarded.frontier == {}
    assert guarded.convicting == {}


def test_an_unrepairable_vanished_test_dispatches_nobody(tmp_path: Path) -> None:
    """The vanished-test merge reads `extra_failed` directly, so it is a third
    way in. A harness test that stopped reporting is still not repairable by
    writing a body."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_text("pub fn crashy() {}\n", encoding="utf-8")
    recs = [{"file": "lib.rs", "fn_name": "crashy"}]
    _res = attribute_failures(
        _deps(recs), "", src, recently_filled=[],
        test_reverse_map={"ui": {("lib.rs", "crashy")}},
        cluster_name="nightly", extra_failed=frozenset({"ui"}),
        unrepairable_tests=frozenset({"ui"}))
    assert _res.frontier == {}


def test_the_callee_walk_reaches_a_fn_the_test_never_names(tmp_path: Path) -> None:
    """The shape of the anyhow defect: the test names `downcast_ref`, and the
    body that is wrong is two calls further in."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "error.rs").write_text(
        "fn downcast_ref() { from_boxed() }\n"
        "fn from_boxed() { construct_from_boxed() }\n"
        "fn construct_from_boxed() { WRONG }\n")
    text = (src / "error.rs").read_text()

    def _span(name: str) -> tuple[int, int]:
        s = text.index("{", text.index(f"fn {name}"))
        return s, text.index("}", s) + 1

    recs = [{"file": "error.rs", "fn_name": n,
             "body_byte_start": _span(n)[0], "body_byte_end": _span(n)[1]}
            for n in ("downcast_ref", "from_boxed", "construct_from_boxed")]
    seed = {("error.rs", "downcast_ref"): {"test_boxed_str"}}
    out, hops = expand_frontier_by_calls(_deps(recs), seed, src, max_hops=2)
    assert ("error.rs", "construct_from_boxed") in out, \
        "the fn holding the defect is unreachable from the failing test"
    assert hops[("error.rs", "construct_from_boxed")] == 2
    assert hops[("error.rs", "downcast_ref")] == 0
    assert out[("error.rs", "construct_from_boxed")] == {"test_boxed_str"}, \
        "a callee inherits the evidence that led to it"


def test_the_callee_walk_respects_its_cap_and_can_be_switched_off(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.rs").write_text("fn a() { b() }\nfn b() { c() }\nfn c() { }\n")
    text = (src / "a.rs").read_text()

    def _span(name: str) -> tuple[int, int]:
        s = text.index("{", text.index(f"fn {name}"))
        return s, text.index("}", s) + 1

    recs = [{"file": "a.rs", "fn_name": n, "body_byte_start": _span(n)[0],
             "body_byte_end": _span(n)[1]} for n in ("a", "b", "c")]
    seed = {("a.rs", "a"): {"t"}}
    capped, _ = expand_frontier_by_calls(_deps(recs), seed, src, max_hops=2, cap=2)
    assert len(capped) == 2
    off, hops = expand_frontier_by_calls(_deps(recs), seed, src, max_hops=0)
    assert off == seed and hops == {("a.rs", "a"): 0}


def test_line_to_fn_record_rejects_bad_lines_and_uses_col() -> None:
    from pipeline_core.domain.attribution import line_to_fn_record
    recs = [{"sig_byte_start": 0, "body_byte_end": 20, "fn_name": "a"}]
    text = "fn a() { x }\nfn b() {}\n"
    assert line_to_fn_record(text, 0, recs) is None
    assert line_to_fn_record(text, 99, recs) is None
    hit = line_to_fn_record(text, 1, recs, col_no=4)
    assert hit is not None and hit["fn_name"] == "a"
