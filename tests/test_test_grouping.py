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
