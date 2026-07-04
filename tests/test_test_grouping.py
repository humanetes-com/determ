"""Characterization for the Phase-B test-grouping family
(P6 relocation of `pipeline._task2_group_tests` + its helpers).

Pure rules → `domain.test_grouping`; prompt → `prompts.test_grouping`; use-case
→ `application.reconstruct.test_grouping.group_tests` (model/log/loader injected
as fakes — no real LLM, no monkeypatch).
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from pipeline_core.application.reconstruct.test_grouping import group_tests
from pipeline_core.domain.test_grouping import (
    extract_groups_or_none,
    extract_test_assertions,
    fallback_groups_per_file,
    parse_and_validate_groups,
    split_oversized_groups,
)


def test_fallback_one_group_per_file() -> None:
    groups = fallback_groups_per_file([("tests/a.rs", ""), ("tests/b.rs", "")])
    assert [g["name"] for g in groups] == ["file_a", "file_b"]
    assert all(len(g["test_files"]) == 1 for g in groups)


def test_split_oversized_groups_chunks_to_max_size() -> None:
    big = [{"name": "g", "rationale": "r", "test_files": [f"t{i}.rs" for i in range(7)],
            "target_symbols": ["s"]}]
    out = split_oversized_groups(big, max_size=3)
    assert [len(g["test_files"]) for g in out] == [3, 3, 1]
    assert [g["name"] for g in out] == ["g_1of3", "g_2of3", "g_3of3"]
    assert all(g["target_symbols"] == ["s"] for g in out)


def test_validate_rejects_omitted_test_file() -> None:
    resp = json.dumps({"groups": [
        {"name": "g", "rationale": "r", "test_files": ["a.rs"], "target_symbols": []}]})
    groups, err, missing = parse_and_validate_groups(resp, ["a.rs", "b.rs"], set())
    assert groups is None
    assert missing == {"b.rs"}


def test_validate_accepts_full_coverage() -> None:
    resp = json.dumps({"groups": [
        {"name": "g", "rationale": "r", "test_files": ["a.rs", "b.rs"], "target_symbols": []}]})
    groups, err, missing = parse_and_validate_groups(resp, ["a.rs", "b.rs"], set())
    assert groups is not None and err == "ok" and missing == set()


def test_extract_groups_or_none_error_paths() -> None:
    assert extract_groups_or_none("not json {") is None
    assert extract_groups_or_none(json.dumps(["a"])) is None          # not a dict
    assert extract_groups_or_none(json.dumps({"x": 1})) is None       # no groups key
    assert extract_groups_or_none(json.dumps({"groups": "str"})) is None
    assert extract_groups_or_none(json.dumps({"groups": [{"name": "g"}]})) == [{"name": "g"}]


def test_validate_structural_errors() -> None:
    ok = {"name": "g", "rationale": "r", "test_files": ["a.rs"], "target_symbols": []}
    cases = [
        ("{ bad", "not valid JSON"),
        (json.dumps(["x"]), "missing 'groups' key"),
        (json.dumps({"groups": []}), "non-empty list"),
        (json.dumps({"groups": ["str"]}), "not an object"),
        (json.dumps({"groups": [{"name": "g"}]}), "missing key"),
        (json.dumps({"groups": [dict(ok, test_files=[])]}), "empty test_files"),
        (json.dumps({"groups": [ok, dict(ok, name="g2")]}), "multiple groups"),
        (json.dumps({"groups": [dict(ok, test_files=["a.rs", "zz.rs"])]}), "unknown test file"),
    ]
    for resp, want in cases:
        groups, err, missing = parse_and_validate_groups(resp, ["a.rs"], set())
        assert groups is None and want in err, (resp, err)
        assert missing == set()


_TEST_FILE = (
    "fn helper() {}\n"
    "let v = compute();\n"
    "assert_eq!(v, 3);\n"
    "// assert_eq!(commented, out);\n"
    "assert!(v > 0);\n"
)


def test_extract_test_assertions_manifest() -> None:
    out = extract_test_assertions([("tests/a.rs", _TEST_FILE)])
    assert out.startswith("Assertions the generated code must satisfy")
    assert "assert_eq!(v, 3);" in out and "assert!(v > 0);" in out
    assert "commented" not in out
    # `let`-context line rides along above its assertion
    assert "let v = compute();  // context" in out


def test_extract_test_assertions_module_filter_and_cap() -> None:
    files = [("tests/other.rs", _TEST_FILE), ("src/mymod/t.rs", _TEST_FILE)]
    out = extract_test_assertions(files, module_dir_rel="mymod")
    assert out.count("assert_eq!(v, 3);") == 1          # other.rs filtered out
    capped = extract_test_assertions([("a.rs", _TEST_FILE)], max_assertions=1)
    assert capped.count("assert") >= 1 and "assert!(v > 0)" not in capped


def test_extract_test_assertions_empty() -> None:
    assert extract_test_assertions([("a.rs", "fn f() {}\n")]) == ""


def _loader(_d: Path) -> list[tuple[str, str]]:
    return [("lib.rs", "pub fn f() {}")]


def test_group_tests_happy_path_writes_groups_json() -> None:
    resp = json.dumps({"groups": [
        {"name": "g", "rationale": "r", "test_files": ["tests/a.rs"], "target_symbols": ["f"]}]})
    logs: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        sk = Path(td)
        groups = group_tests(
            sk, [("tests/a.rs", "#[test] fn t() {}")], "default", "mod",
            ask=lambda p, model=None: resp, model="m", log=logs.append,
            load_skeleton_files=_loader)
        assert [g["name"] for g in groups] == ["g"]
        assert json.loads((sk / "groups.json").read_text())[0]["name"] == "g"


def test_group_tests_falls_back_on_invalid_json() -> None:
    logs: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        sk = Path(td)
        groups = group_tests(
            sk, [("tests/a.rs", "x")], "default", "mod",
            ask=lambda p, model=None: "not json", model="m", log=logs.append,
            load_skeleton_files=_loader)
        # Two rejected attempts → per-file fallback.
        assert [g["name"] for g in groups] == ["file_a"]
        assert any("falling back" in m for m in logs)


def test_group_tests_auto_recovers_small_omission() -> None:
    # First attempt omits one of two test files (≤2 → recoverable).
    resp = json.dumps({"groups": [
        {"name": "g", "rationale": "r", "test_files": ["tests/a.rs"], "target_symbols": []}]})
    logs: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        sk = Path(td)
        groups = group_tests(
            sk, [("tests/a.rs", "x"), ("tests/b.rs", "y")], "default", "mod",
            ask=lambda p, model=None: resp, model="m", log=logs.append,
            load_skeleton_files=_loader)
        names = {g["name"] for g in groups}
        assert "miscellaneous_auto_added" in names
        covered = {tf for g in groups for tf in g["test_files"]}
        assert covered == {"tests/a.rs", "tests/b.rs"}
