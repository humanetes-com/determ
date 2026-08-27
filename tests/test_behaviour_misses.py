"""The miss sidecar: what the capture pass could NOT observe, and why.

WHY THESE EXIST. A probe that failed to compile, printed nothing, or did not
reproduce used to leave one log line and nothing else — no event, no stored
program, and `materialized_probe` unlinks the source in a `finally`, so even
the program was gone. The set of things we know we cannot observe was
invisible, which is the silent divergence the capture route exists to remove.
The store is separate from the suite on purpose: a miss admitted as a
`then=None` spec would be classified OTHER, ranked last by the authoring cap
and unpinnable — the same hole under another name.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline_core.adapters import behaviour_misses as mod
from pipeline_core.adapters.behaviour_misses import (
    load_behaviour_misses,
    misses_path,
    save_behaviour_misses,
)


@pytest.fixture(autouse=True)
def _quiet(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    lines: list[str] = []
    monkeypatch.setattr(mod, "log", lines.append)
    monkeypatch.setattr(mod, "emit", lambda _e, **_f: None)
    return lines


def test_a_pass_round_trips_its_misses_and_names_them_by_reason(
        tmp_path: Path, _quiet: list[str]) -> None:
    misses = [{"unit": "a", "reason": "failed: error[E0599]", "when": "e.x()"},
              {"unit": "b", "reason": "failed: timeout", "when": "e.y()"},
              {"unit": "c", "reason": "not reproducible", "when": "e.z()"}]
    p = save_behaviour_misses(tmp_path, "stable", misses)
    assert p == misses_path(tmp_path, "stable")
    assert load_behaviour_misses(tmp_path, "stable") == misses
    # The summary counts by the reason's HEAD, so two different compile errors
    # read as one class rather than as two singletons.
    assert any("failed 2, not reproducible 1" in m for m in _quiet)


def test_a_pass_that_missed_nothing_still_writes_the_record(
        tmp_path: Path, _quiet: list[str]) -> None:
    """The file states what THIS pass could not capture. Writing it only when
    the list is non-empty would leave the previous pass's file standing as if
    it were current."""
    save_behaviour_misses(tmp_path, "stable", [{"unit": "a", "reason": "no stdout"}])
    save_behaviour_misses(tmp_path, "stable", [])
    assert load_behaviour_misses(tmp_path, "stable") == []
    assert any("every attempted behaviour was captured" in m for m in _quiet)


def test_no_file_is_quiet_and_an_unreadable_one_is_loud(
        tmp_path: Path, _quiet: list[str]) -> None:
    """An empty list and a corrupt file mean opposite things about whether
    capture was attempted, so they must not read the same."""
    assert load_behaviour_misses(tmp_path, "stable") == []
    assert _quiet == []

    p = misses_path(tmp_path, "nightly")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    assert load_behaviour_misses(tmp_path, "nightly") == []
    assert any("miss store unreadable" in m for m in _quiet)

    _quiet.clear()
    p.write_text('{"cfg_cluster": "nightly", "misses": {"a": 1}}', encoding="utf-8")
    assert load_behaviour_misses(tmp_path, "nightly") == []
    assert any("misses is not a list" in m for m in _quiet)
