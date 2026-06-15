"""Fake-adapter tests for the TASK-1 synthesizer engine (debt #14 — ports migration).

Drive StaticTask1Synthesizer / LLMTask1Synthesizer over a fake satisfying the 5
`ports.synthesizer` roles + a `Task1Config` VO — no pipeline import, no LLM, no
cargo. Covers the single-call path, the chunked+hierarchical path, the multi-group
Stage-1 merge, the cache-hit / partial-file reuse skips, and the failure/raw-fallback
branches (the configure()-seam suite this replaces). Every `# debt #14` per-method
local-binding preamble is exercised end-to-end (a missed binding = NameError here)."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from pipeline_core.application.generators.task1_synthesizer import (
    LLMTask1Synthesizer,
    StaticTask1Synthesizer,
    Task1Synthesizer,
)
from pipeline_core.domain.generation import Task1Config

_FILE_RESP = '<FILE path="functional_requirements.md">\nFR_BODY\n</FILE>'


class _Progress:
    def __init__(self) -> None:
        self.done = False

    def start_chunk(self) -> None: ...
    def finish_chunk(self, current: int) -> None: ...
    def finish(self) -> None:
        self.done = True


@dataclass
class FakeAdapter:
    """Satisfies Task1LLM/PromptShaping/ChunkCache/Progress/Requirements."""
    asks: list[str] = field(default_factory=list)
    ask_fn: Callable[[str], str] | None = None
    load_fn: Callable[[Path, str], str | None] | None = None

    # Task1LLM
    def ask(self, prompt: str, *, model: str | None = None) -> str:
        self.asks.append(prompt)
        return self.ask_fn(prompt) if self.ask_fn else _FILE_RESP

    # PromptShaping
    def prompt_budget(self, *fixed_parts: str) -> int:
        return 50_000

    def format_test_files(self, test_files: Any) -> str:
        return "\n".join(f"// {n}\n{c}" for n, c in test_files)

    def chunk_test_files_by_file(self, test_files: Any, file_char_limit: int) -> list[Any]:
        return [[t] for t in test_files]

    def format_api_surface_section(self, api_surface: str) -> str:
        return ""

    # ChunkCache
    def chunk_hash(self, chunk_text: str) -> str:
        return str(hash(chunk_text) & 0xffffffff)

    def load(self, cache_dir: Path, chunk_hash: str) -> str | None:
        return self.load_fn(cache_dir, chunk_hash) if self.load_fn else None

    def save(self, cache_dir: Path, chunk_hash: str, content: str) -> None:
        pass

    # Progress
    def make_progress(self, total: int) -> _Progress:
        return _Progress()

    # Requirements
    def extract_type_names(self, text: str) -> set[str]:
        return set()

    def write_fr_preserving_notes(self, path: Path, new_content: str) -> None:
        path.write_text(new_content, encoding="utf-8")

    def type_signals_read(self, project_dir: Path) -> str:
        return ""


def _cfg(**over: Any) -> Task1Config:
    base = Task1Config(
        model="fake-model", task1_model="fake-task1", thinking_directive="",
        file_format_instruction="<<FMT>>", single_call_char_limit=100_000,
        synthesis_group_size=20, readme_cap=2_000, strategy_cap=2_000,
        chunk_cache_dir=".chunk_cache",
    )
    return replace(base, **over)


def _make(cls: type[Task1Synthesizer], fake: FakeAdapter, cfg: Task1Config) -> Task1Synthesizer:
    return cls(llm=fake, shaping=fake, cache=fake, progress=fake,  # type: ignore[call-arg]
               requirements=fake, cfg=cfg)


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "genesis.md", tmp_path / "functional_requirements.md"


@pytest.mark.parametrize("cls", [StaticTask1Synthesizer, LLMTask1Synthesizer])
def test_single_call_path(tmp_path: Path, cls: type[Task1Synthesizer]) -> None:
    genesis, fr = _paths(tmp_path)
    fake = FakeAdapter()
    ok = _make(cls, fake, _cfg()).synthesize(
        tmp_path, genesis, fr,
        test_files=[("tests/a.rs", "#[test] fn t() { add(1,2); }")],
        readme_text="readme", api_surface="fn add(a,b)->i32",
    )
    assert ok is True
    assert "FR_BODY" in fr.read_text()
    assert genesis.exists()  # genesis stub written when absent
    assert len(fake.asks) == 1


@pytest.mark.parametrize("cls", [StaticTask1Synthesizer, LLMTask1Synthesizer])
def test_chunked_path(tmp_path: Path, cls: type[Task1Synthesizer]) -> None:
    genesis, fr = _paths(tmp_path)
    fake = FakeAdapter()
    ok = _make(cls, fake, _cfg(single_call_char_limit=10)).synthesize(
        tmp_path, genesis, fr,
        test_files=[("tests/a.rs", "fn one(){}"), ("tests/b.rs", "fn two(){}")],
        readme_text="readme",
    )
    assert ok is True
    assert "FR_BODY" in fr.read_text()
    assert len(fake.asks) == 3  # 2 chunks + 1 synthesis; single group → no merge


@pytest.mark.parametrize("cls", [StaticTask1Synthesizer, LLMTask1Synthesizer])
def test_chunked_multigroup_hierarchical(tmp_path: Path, cls: type[Task1Synthesizer]) -> None:
    genesis, fr = _paths(tmp_path)
    fake = FakeAdapter()
    ok = _make(cls, fake, _cfg(single_call_char_limit=10, synthesis_group_size=1)).synthesize(
        tmp_path, genesis, fr,
        test_files=[("a.rs", "fn a(){}"), ("b.rs", "fn b(){}"), ("c.rs", "fn c(){}")],
        readme_text="readme",
    )
    assert ok is True
    assert "FR_BODY" in fr.read_text()
    assert len(fake.asks) == 7  # 3 chunk extractions + 3 group merges + 1 final


def test_single_call_failure_returns_false(tmp_path: Path) -> None:
    genesis, fr = _paths(tmp_path)
    fake = FakeAdapter(ask_fn=lambda p: "too short")  # <200 chars, no FILE markers
    ok = _make(StaticTask1Synthesizer, fake, _cfg()).synthesize(
        tmp_path, genesis, fr, test_files=[("a.rs", "fn t(){}")], readme_text="r")
    assert ok is False
    assert not fr.exists()


def test_chunk_extraction_failure_returns_false(tmp_path: Path) -> None:
    genesis, fr = _paths(tmp_path)
    fake = FakeAdapter(ask_fn=lambda p: "")  # empty → retries exhaust
    ok = _make(StaticTask1Synthesizer, fake, _cfg(single_call_char_limit=10)).synthesize(
        tmp_path, genesis, fr, test_files=[("a.rs", "fn a(){}")], readme_text="r")
    assert ok is False


def test_group_merge_failure_returns_false(tmp_path: Path) -> None:
    genesis, fr = _paths(tmp_path)
    fake = FakeAdapter(ask_fn=lambda p: "" if "merging a group" in p else "chunk requirement")
    ok = _make(StaticTask1Synthesizer, fake,
               _cfg(single_call_char_limit=10, synthesis_group_size=1)).synthesize(
        tmp_path, genesis, fr,
        test_files=[("a.rs", "fn a(){}"), ("b.rs", "fn b(){}")], readme_text="r")
    assert ok is False


def test_chunked_cache_hit_skips_model(tmp_path: Path) -> None:
    genesis, fr = _paths(tmp_path)
    fake = FakeAdapter(load_fn=lambda d, h: "CACHED requirement entry")
    ok = _make(StaticTask1Synthesizer, fake, _cfg(single_call_char_limit=10)).synthesize(
        tmp_path, genesis, fr,
        test_files=[("a.rs", "fn a(){}"), ("b.rs", "fn b(){}")], readme_text="r")
    assert ok is True
    assert len(fake.asks) == 1  # 2 chunks from cache → only final synthesis hits ask


def test_chunked_partial_file_reuse(tmp_path: Path) -> None:
    genesis, fr = _paths(tmp_path)
    partials = tmp_path / ".task1_partials"
    partials.mkdir()
    (partials / "chunk_001_of_002.txt").write_text("=== chunk 1 ===\nreq one")
    (partials / "chunk_002_of_002.txt").write_text("=== chunk 2 ===\nreq two")
    fake = FakeAdapter()
    ok = _make(StaticTask1Synthesizer, fake, _cfg(single_call_char_limit=10)).synthesize(
        tmp_path, genesis, fr,
        test_files=[("a.rs", "fn a(){}"), ("b.rs", "fn b(){}")], readme_text="r")
    assert ok is True
    assert len(fake.asks) == 1  # both chunks from disk → only final synthesis hits ask


def test_no_file_markers_raw_fallback(tmp_path: Path) -> None:
    genesis, fr = _paths(tmp_path)
    long_text = "behavioral requirement line. " * 20  # > 200 chars, no FILE markers
    fake = FakeAdapter(ask_fn=lambda p: long_text)
    ok = _make(StaticTask1Synthesizer, fake, _cfg()).synthesize(
        tmp_path, genesis, fr, test_files=[("tests/a.rs", "fn t(){}")], readme_text="r")
    assert ok is True
    assert long_text.strip() in fr.read_text()
