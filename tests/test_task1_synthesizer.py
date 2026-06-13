"""Fakes-deps tests for the relocated TASK 1 synthesizer engine (P4).

Drive StaticTask1Synthesizer / LLMTask1Synthesizer through configure()-injected
fakes — no pipeline import, no LLM, no cargo. Covers the single-call path, the
chunked+hierarchical path, and the no-FILE-markers raw fallback.
"""
from __future__ import annotations

import pytest

from pipeline_core.application.generators import task1_synthesizer as t1
from pipeline_core.application.generators.task1_synthesizer import (
    LLMTask1Synthesizer,
    StaticTask1Synthesizer,
)

_INJECTED = [
    "ask", "prompt_budget", "format_test_files", "chunk_test_files_by_file",
    "_chunk_hash", "_load_cached_chunk", "_save_cached_chunk", "ChunkProgress",
    "_extract_type_names_from_requirements", "_format_api_surface_section",
    "_write_fr_preserving_notes", "type_signals_read", "MODEL",
    "SINGLE_CALL_CHAR_LIMIT", "SYNTHESIS_GROUP_SIZE", "_README_CAP", "_STRATEGY_CAP",
    "_TASK1_MODEL", "_TASK1_THINKING_DIRECTIVE", "CHUNK_CACHE_DIR",
    "FILE_FORMAT_INSTRUCTION",
]


class _Progress:
    def __init__(self, total: int = 0) -> None: ...
    def start_chunk(self) -> None: ...
    def finish_chunk(self, i: int) -> None: ...
    def finish(self) -> None: ...


@pytest.fixture
def configured(monkeypatch):
    """Snapshot the module's injected globals, install fakes, restore after."""
    saved = {n: getattr(t1, n) for n in _INJECTED}

    calls: dict[str, list] = {"ask": []}

    def fake_ask(prompt, system=None, model=None, **k):
        calls["ask"].append(prompt)
        return '<FILE path="functional_requirements.md">\nFR_BODY\n</FILE>'

    t1.configure(
        ask=fake_ask,
        prompt_budget=lambda *a: 50_000,
        format_test_files=lambda tf: "\n".join(f"// {n}\n{c}" for n, c in tf),
        chunk_test_files_by_file=lambda tf, lim: [[t] for t in tf],
        _chunk_hash=lambda s: str(hash(s)),
        _load_cached_chunk=lambda d, h: None,
        _save_cached_chunk=lambda d, h, c: None,
        ChunkProgress=_Progress,
        _extract_type_names_from_requirements=lambda s: set(),
        _format_api_surface_section=lambda s: "",
        _write_fr_preserving_notes=lambda p, c: t1.write(p, c),
        type_signals_read=lambda d: "",
        MODEL="fake-model",
        SINGLE_CALL_CHAR_LIMIT=100_000,
        SYNTHESIS_GROUP_SIZE=20,
        _README_CAP=2_000,
        _STRATEGY_CAP=2_000,
        _TASK1_MODEL="fake-task1",
        _TASK1_THINKING_DIRECTIVE="",
        CHUNK_CACHE_DIR=".chunk_cache",
        FILE_FORMAT_INSTRUCTION="<<FMT>>",
    )
    yield calls
    t1.configure(**saved)


def _paths(tmp_path):
    return tmp_path / "genesis.md", tmp_path / "functional_requirements.md"


@pytest.mark.parametrize("cls", [StaticTask1Synthesizer, LLMTask1Synthesizer])
def test_single_call_path(configured, tmp_path, cls):
    genesis, fr = _paths(tmp_path)
    ok = cls().synthesize(
        tmp_path, genesis, fr,
        test_files=[("tests/a.rs", "#[test] fn t() { add(1,2); }")],
        readme_text="readme", api_surface="fn add(a,b)->i32",
    )
    assert ok is True
    assert "FR_BODY" in fr.read_text()
    assert genesis.exists()  # genesis stub written when absent
    assert len(configured["ask"]) == 1


@pytest.mark.parametrize("cls", [StaticTask1Synthesizer, LLMTask1Synthesizer])
def test_chunked_path(configured, tmp_path, cls):
    # Force the chunked branch: tiny single-call budget so total_chars exceeds it.
    t1.configure(SINGLE_CALL_CHAR_LIMIT=10)
    genesis, fr = _paths(tmp_path)
    ok = cls().synthesize(
        tmp_path, genesis, fr,
        test_files=[("tests/a.rs", "fn one(){}"), ("tests/b.rs", "fn two(){}")],
        readme_text="readme",
    )
    assert ok is True
    assert "FR_BODY" in fr.read_text()
    # one ask per chunk (2) + final synthesis (1); single group → no stage-1 merge.
    assert len(configured["ask"]) == 3


@pytest.mark.parametrize("cls", [StaticTask1Synthesizer, LLMTask1Synthesizer])
def test_chunked_multigroup_hierarchical(configured, tmp_path, cls):
    # group size 1 + 3 chunks → 3 stage-1 group merges, then final synthesis.
    t1.configure(SINGLE_CALL_CHAR_LIMIT=10, SYNTHESIS_GROUP_SIZE=1)
    genesis, fr = _paths(tmp_path)
    ok = cls().synthesize(
        tmp_path, genesis, fr,
        test_files=[("a.rs", "fn a(){}"), ("b.rs", "fn b(){}"), ("c.rs", "fn c(){}")],
        readme_text="readme",
    )
    assert ok is True
    assert "FR_BODY" in fr.read_text()
    # 3 chunk extractions + 3 group merges + 1 final synthesis.
    assert len(configured["ask"]) == 7


def test_single_call_failure_returns_false(configured, tmp_path):
    t1.configure(ask=lambda *a, **k: "too short")  # <200 chars, no FILE markers
    genesis, fr = _paths(tmp_path)
    ok = StaticTask1Synthesizer().synthesize(
        tmp_path, genesis, fr,
        test_files=[("a.rs", "fn t(){}")], readme_text="r",
    )
    assert ok is False
    assert not fr.exists()


def test_chunk_extraction_failure_returns_false(configured, tmp_path):
    t1.configure(SINGLE_CALL_CHAR_LIMIT=10, ask=lambda *a, **k: "")  # empty → retries exhaust
    genesis, fr = _paths(tmp_path)
    ok = StaticTask1Synthesizer().synthesize(
        tmp_path, genesis, fr,
        test_files=[("a.rs", "fn a(){}")], readme_text="r",
    )
    assert ok is False


def test_group_merge_failure_returns_false(configured, tmp_path):
    def picky_ask(prompt, *a, **k):
        return "" if "merging a group" in prompt else "chunk requirement"

    t1.configure(SINGLE_CALL_CHAR_LIMIT=10, SYNTHESIS_GROUP_SIZE=1, ask=picky_ask)
    genesis, fr = _paths(tmp_path)
    ok = StaticTask1Synthesizer().synthesize(
        tmp_path, genesis, fr,
        test_files=[("a.rs", "fn a(){}"), ("b.rs", "fn b(){}")], readme_text="r",
    )
    assert ok is False


def test_chunked_cache_hit_skips_model(configured, tmp_path):
    t1.configure(SINGLE_CALL_CHAR_LIMIT=10,
                 _load_cached_chunk=lambda d, h: "CACHED requirement entry")
    genesis, fr = _paths(tmp_path)
    ok = StaticTask1Synthesizer().synthesize(
        tmp_path, genesis, fr,
        test_files=[("a.rs", "fn a(){}"), ("b.rs", "fn b(){}")], readme_text="r",
    )
    assert ok is True
    # 2 chunks served from cache → only the final synthesis call hits ask().
    assert len(configured["ask"]) == 1


def test_chunked_partial_file_reuse(configured, tmp_path):
    # First run writes per-chunk partial files; second run reads them back.
    t1.configure(SINGLE_CALL_CHAR_LIMIT=10)
    genesis, fr = _paths(tmp_path)
    args = {"test_files": [("a.rs", "fn a(){}"), ("b.rs", "fn b(){}")], "readme_text": "r"}
    assert StaticTask1Synthesizer().synthesize(tmp_path, genesis, fr, **args) is True
    n_first = len(configured["ask"])
    assert StaticTask1Synthesizer().synthesize(tmp_path, genesis, fr, **args) is True
    # Second run reuses both partial files → only the final synthesis call is new.
    assert len(configured["ask"]) == n_first + 1


def test_no_file_markers_raw_fallback(configured, tmp_path):
    long_text = "behavioral requirement line. " * 20  # > 200 chars, no FILE markers

    def raw_ask(prompt, system=None, model=None, **k):
        return long_text

    t1.configure(ask=raw_ask)
    genesis, fr = _paths(tmp_path)
    ok = StaticTask1Synthesizer().synthesize(
        tmp_path, genesis, fr,
        test_files=[("tests/a.rs", "fn t(){}")], readme_text="r",
    )
    assert ok is True
    assert long_text.strip() in fr.read_text()
