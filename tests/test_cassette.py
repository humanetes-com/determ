"""L2 cassette tests — CODE_ARCHITECTURE.md R10.3.

Offline: synthesizes a tiny events.jsonl + content-addressed prompts dir (NOT the
370M live logs) so the builder/replay logic is CI-safe.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

import pipeline
from tests.cassette import CassetteMiss, _sha, build_cassette, install_cassette


def _seed(td: str, events: list[dict], bodies: dict[str, str]) -> tuple[Path, Path]:
    root = Path(td)
    prompts = root / "prompts"
    prompts.mkdir()
    for sha, text in bodies.items():
        (prompts / f"{sha}.txt").write_text(text, encoding="utf-8")
    ev = root / "events.jsonl"
    ev.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return ev, prompts


def test_build_from_explicit_prompt_sha() -> None:
    """R10.0 path: response carries prompt_sha → exact pairing."""
    p_sha, r_sha = _sha("PROMPT-A"), "rrr1"
    events = [
        {"product": "anyhow", "pipeline_sha": "abc", "event": "llm.request",
         "data": {"prompt": {"sha256": p_sha}}},
        {"product": "anyhow", "pipeline_sha": "abc", "event": "llm.response",
         "data": {"prompt_sha": p_sha, "response": {"sha256": r_sha}}},
    ]
    with tempfile.TemporaryDirectory() as td:
        ev, prompts = _seed(td, events, {r_sha: "RESPONSE-A"})
        cassette = build_cassette(ev, prompts, product="anyhow", pipeline_sha="abc")
    assert cassette == {p_sha: "RESPONSE-A"}


def test_build_falls_back_to_order_pairing() -> None:
    """Pre-R10.0 recording (no prompt_sha on response) → emission-order pairing."""
    p_sha, r_sha = _sha("PROMPT-B"), "rrr2"
    events = [
        {"event": "llm.request", "data": {"prompt": {"sha256": p_sha}}},
        {"event": "llm.response", "data": {"response": {"sha256": r_sha}}},
    ]
    with tempfile.TemporaryDirectory() as td:
        ev, prompts = _seed(td, events, {r_sha: "RESPONSE-B"})
        cassette = build_cassette(ev, prompts)
    assert cassette == {p_sha: "RESPONSE-B"}


def test_filter_drops_other_runs() -> None:
    """Product/pipeline_sha filter isolates one run from the multi-run log."""
    pa, pb = _sha("A"), _sha("B")
    events = [
        {"product": "anyhow", "pipeline_sha": "v1", "event": "llm.request",
         "data": {"prompt": {"sha256": pa}}},
        {"product": "anyhow", "pipeline_sha": "v1", "event": "llm.response",
         "data": {"prompt_sha": pa, "response": {"sha256": "ra"}}},
        {"product": "other", "pipeline_sha": "v2", "event": "llm.request",
         "data": {"prompt": {"sha256": pb}}},
        {"product": "other", "pipeline_sha": "v2", "event": "llm.response",
         "data": {"prompt_sha": pb, "response": {"sha256": "rb"}}},
    ]
    with tempfile.TemporaryDirectory() as td:
        ev, prompts = _seed(td, events, {"ra": "RA", "rb": "RB"})
        cassette = build_cassette(ev, prompts, product="anyhow", pipeline_sha="v1")
    assert cassette == {pa: "RA"}


def test_replay_hit_and_miss(monkeypatch) -> None:
    """Replay returns the recorded body for a known prompt; an altered prompt
    misses (the blast-radius signal) — recorded strict and lenient."""
    cassette = {_sha("hello"): "WORLD"}

    misses: list[str] = []
    install_cassette(monkeypatch, cassette, strict=True, misses=misses)
    assert pipeline._ask_raw("hello", None) == "WORLD"
    with pytest.raises(CassetteMiss):
        pipeline._ask_raw("changed-prompt", None)
    assert misses == [_sha("changed-prompt")]

    install_cassette(monkeypatch, cassette, strict=False)
    assert pipeline._ask_raw("still-missing") == ""
