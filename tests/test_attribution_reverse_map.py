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

    frontier, _kind = attribute_failures(
        _deps(recs), output, src, recently_filled=[],
        test_reverse_map=reverse_map, cluster_name="stable")

    assert ("lib.rs", "cap_limit") in frontier
    assert ("lib.rs", "extra_cap_limit") not in frontier


# --- s117 lever: deterministic behavior attribution in the TDD chain ------- #

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
    frontier, kind = attribute_failures(
        deps, output, tmp_path, recently_filled=[],
        test_reverse_map={}, cluster_name="stable", test_texts=test_texts)
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
    frontier, kind = attribute_failures(
        _deps([]), "out", tmp_path, recently_filled=[], test_reverse_map={},
        cluster_name="stable", test_texts={"tests/x.rs": "…"})
    assert kind == "merged"
    assert frontier[a] == {"t_panic"} and frontier[b] == {"t_assert"}


def test_single_deterministic_path_keeps_its_kind(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    import pipeline_core.application.reconstruct.attribution as attr
    a = FnLocator("src/a.rs", "stub_fn")
    monkeypatch.setattr(attr, "extract_tdd_frontier", lambda *_a, **_k: {a: {"t"}})
    monkeypatch.setattr(attr, "extract_tdd_compile_frontier", lambda *_a, **_k: {})
    monkeypatch.setattr(attr, "behavior_frontier_from_texts", lambda *_a, **_k: ({}, {}))
    frontier, kind = attribute_failures(
        _deps([]), "out", tmp_path, recently_filled=[], test_reverse_map={},
        cluster_name="stable", test_texts={"tests/x.rs": "…"})
    assert kind == "runtime" and a in frontier


def test_behavior_path_skipped_without_texts(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    """No test_texts (fix-loop callers unchanged) → chain behaves as before:
    falls through to the reverse map."""
    monkeypatch.setenv("PHASE_C_TDD_USE_CLASSIFIER", "0")
    recs = [{"file": "src/lib.rs", "fn_name": "cap_limit", "cfg_predicates": []}]
    output = ("running 1 test\ntest t_cap ... FAILED\n\nfailures:\n")
    frontier, kind = attribute_failures(
        _deps(recs), output, tmp_path, recently_filled=[],
        test_reverse_map={"t_cap": {FnLocator("src/lib.rs", "cap_limit")}},
        cluster_name="stable")
    assert kind == "reverse_map"
    assert FnLocator("src/lib.rs", "cap_limit") in frontier
