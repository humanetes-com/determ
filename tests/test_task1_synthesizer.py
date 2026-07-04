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
    """Satisfies Task1LLM/PromptShaping/Progress/Requirements (the chunk cache is a
    pure foundational module imported directly — not a port)."""
    asks: list[str] = field(default_factory=list)
    ask_fn: Callable[[str], str] | None = None

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

    # Progress
    def make_progress(self, total: int) -> _Progress:
        return _Progress()

    # Requirements
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
    return cls(llm=fake, shaping=fake, progress=fake,  # type: ignore[call-arg]
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


def test_chunk_input_too_long_degrades_not_aborts(tmp_path: Path) -> None:
    # debt #32: a chunk that overflows ctx even after the controller replan is
    # SKIPPED (missing from FR, logged) — task1 still succeeds on the rest.
    from pipeline_core.llm import InputTooLong

    def _ask(p: str) -> str:
        if "fn one" in p:
            raise InputTooLong(40172)
        return _FILE_RESP

    genesis, fr = _paths(tmp_path)
    fake = FakeAdapter(ask_fn=_ask)
    ok = _make(StaticTask1Synthesizer, fake, _cfg(single_call_char_limit=10)).synthesize(
        tmp_path, genesis, fr,
        test_files=[("a.rs", "fn one(){}"), ("b.rs", "fn two(){}")],
        readme_text="r")
    assert ok is True
    assert "FR_BODY" in fr.read_text()


def test_group_merge_failure_returns_false(tmp_path: Path) -> None:
    genesis, fr = _paths(tmp_path)
    fake = FakeAdapter(ask_fn=lambda p: "" if "merging a group" in p else "chunk requirement")
    ok = _make(StaticTask1Synthesizer, fake,
               _cfg(single_call_char_limit=10, synthesis_group_size=1)).synthesize(
        tmp_path, genesis, fr,
        test_files=[("a.rs", "fn a(){}"), ("b.rs", "fn b(){}")], readme_text="r")
    assert ok is False


def test_chunked_cache_hit_skips_model(tmp_path: Path) -> None:
    import hashlib

    from pipeline_core.chunk_cache import chunk_hash
    genesis, fr = _paths(tmp_path)
    fake = FakeAdapter()
    tfs = [("a.rs", "fn a(){}"), ("b.rs", "fn b(){}")]
    # Seed the real content-addressed cache so both chunks hit it. The key is
    # chunk_hash(format_test_files([tf]) + sha256(existing_genesis)[:8]); genesis
    # is absent → existing_genesis == "".
    cache_dir = tmp_path / ".chunk_cache"
    cache_dir.mkdir()
    gh = hashlib.sha256(b"").hexdigest()[:8]
    for tf in tfs:
        h = chunk_hash(fake.format_test_files([tf]) + gh)
        (cache_dir / f"{h}.txt").write_text("CACHED requirement entry")
    ok = _make(StaticTask1Synthesizer, fake, _cfg(single_call_char_limit=10)).synthesize(
        tmp_path, genesis, fr, test_files=tfs, readme_text="r")
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


@pytest.mark.parametrize("cls", [StaticTask1Synthesizer, LLMTask1Synthesizer])
def test_genesis_seeded_with_methodology_not_placeholder(
        tmp_path: Path, cls: type[Task1Synthesizer]) -> None:
    # debt #38: the happy path must leave a real methodology document, never
    # the "(populated by TASK 4)" placeholder.
    from pipeline_core.prompts.genesis_seed import GENESIS_SEED
    genesis, fr = _paths(tmp_path)
    fake = FakeAdapter()
    ok = _make(cls, fake, _cfg()).synthesize(
        tmp_path, genesis, fr,
        test_files=[("tests/a.rs", "#[test] fn t() { add(1,2); }")],
        readme_text="readme",
    )
    assert ok is True
    text = genesis.read_text()
    assert text == GENESIS_SEED
    assert "populated by TASK 4" not in text
    assert "reconstruction methodology" in text


@pytest.mark.parametrize("cls", [StaticTask1Synthesizer, LLMTask1Synthesizer])
def test_genesis_existing_content_never_overwritten(
        tmp_path: Path, cls: type[Task1Synthesizer]) -> None:
    genesis, fr = _paths(tmp_path)
    genesis.write_text("# genesis.md\n\ncustom lesson\n")
    fake = FakeAdapter()
    ok = _make(cls, fake, _cfg()).synthesize(
        tmp_path, genesis, fr,
        test_files=[("tests/a.rs", "#[test] fn t() { add(1,2); }")],
        readme_text="readme",
    )
    assert ok is True
    assert genesis.read_text() == "# genesis.md\n\ncustom lesson\n"
