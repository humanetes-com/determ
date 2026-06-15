"""Fake-adapter unit tests for the TASK-0a doc use-cases (application layer,
previously only touched incidentally by the smoke). Pins the cached/backfill
short-circuits, the fresh LLM generation path, the no-marker fence fallback, and
the all-retries-failed degradation — driven through Task0DocsDeps, no pipeline.py."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline_core.application.tasks.task0_docs import (
    Task0DocsDeps,
    task0_project_context,
    task0_project_profile,
)


def _deps(responses: list[str], *, valid: bool = False) -> tuple[Task0DocsDeps, dict, list]:
    seq = list(responses)
    written: dict[str, str] = {}
    calls: list[tuple] = []

    def ask(prompt: str, model: Any = None) -> str:
        return seq.pop(0) if seq else ""

    def write(path: Any, content: str) -> None:
        written[Path(path).name] = content
        Path(path).write_text(content, encoding="utf-8")

    deps = Task0DocsDeps(
        phase_valid=lambda pd, ph: valid,
        mark_phase=lambda pd, ph, st, **k: calls.append((ph, st)),
        ask=ask,
        write=write,
        prompt_budget=lambda *a: 4000,
        task0_model="m", model="m",
        readme_cap=2000, profile_cap=2000,
        project_context_name="project_context.md",
    )
    return deps, written, calls


_TESTS = [("tests/a.rs", "fn t() { assert!(true); }")]


def test_profile_cached_short_circuit(tmp_path) -> None:
    (tmp_path / "project_profile.md").write_text("CACHED PROFILE", encoding="utf-8")
    deps, written, _ = _deps(["UNUSED"], valid=True)  # phase valid → no ask
    out = task0_project_profile(tmp_path / "project_profile.md", _TESTS, "", deps)
    assert out == "CACHED PROFILE" and written == {}  # nothing re-asked/written


def test_profile_backfills_existing_artifact(tmp_path) -> None:
    (tmp_path / "project_profile.md").write_text("PRIOR", encoding="utf-8")
    deps, written, calls = _deps([], valid=False)  # not marked done, but file exists
    out = task0_project_profile(tmp_path / "project_profile.md", _TESTS, "", deps)
    assert out == "PRIOR" and ("task0a", "done") in calls and written == {}


def test_profile_fresh_generation(tmp_path) -> None:
    resp = '<FILE path="project_profile.md">Rust + Cargo workspace.</FILE>'
    deps, written, calls = _deps([resp])
    out = task0_project_profile(tmp_path / "project_profile.md", _TESTS, "readme", deps)
    assert out == "Rust + Cargo workspace."
    assert written["project_profile.md"] == "Rust + Cargo workspace."
    assert ("task0a", "done") in calls


def test_profile_fence_fallback_when_no_marker(tmp_path) -> None:
    body = "x" * 150  # >100 chars → fence fallback accepts it
    deps, written, _ = _deps([f"```\n{body}\n```"])
    out = task0_project_profile(tmp_path / "project_profile.md", _TESTS, "", deps)
    assert out == body and written["project_profile.md"] == body


def test_profile_all_retries_fail_returns_empty(tmp_path) -> None:
    deps, written, _ = _deps(["nope"] * 20)  # short junk, no marker → always fails
    out = task0_project_profile(tmp_path / "project_profile.md", _TESTS, "", deps)
    assert out == "" and written == {}


def test_context_fresh_generation(tmp_path) -> None:
    resp = '<FILE path="project_context.md">Behavioral contract notes.</FILE>'
    deps, written, calls = _deps([resp])
    out = task0_project_context(
        tmp_path / "project_context.md", _TESTS, "readme", "profile body", deps)
    assert out == "Behavioral contract notes."
    assert written["project_context.md"] == "Behavioral contract notes."
    assert ("task0a-context", "done") in calls
