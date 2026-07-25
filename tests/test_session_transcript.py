"""Pins for session-transcript parsing — the source of the exact model id that
the operator-pass report header must name (operator_run.sh writes it from
tools/session_model.py, which delegates to this parser)."""
from __future__ import annotations

import json

from pipeline_core.domain.session_transcript import models_from_transcript


def _rec(**kw) -> str:
    return json.dumps(kw)


def test_assistant_model_in_first_seen_order_without_duplicates():
    lines = [
        _rec(type="user", message={"role": "user", "content": "hi"}),
        _rec(type="assistant", message={"role": "assistant", "model": "claude-opus-5"}),
        _rec(type="assistant", message={"role": "assistant", "model": "claude-opus-5"}),
        _rec(type="assistant", message={"role": "assistant", "model": "claude-fable-5"}),
    ]
    assert models_from_transcript(lines) == ["claude-opus-5", "claude-fable-5"]


def test_sidechain_subagent_model_excluded():
    """A delegated helper (e.g. the Haiku `validate` agent) must never be
    reported as the model that ran the pass."""
    lines = [
        _rec(type="assistant", message={"role": "assistant", "model": "claude-opus-5"}),
        _rec(
            type="assistant",
            isSidechain=True,
            message={"role": "assistant", "model": "claude-haiku-4-5-20251001"},
        ),
    ]
    assert models_from_transcript(lines) == ["claude-opus-5"]


def test_tolerates_truncated_and_unknown_records():
    """Transcripts are append-only and can end mid-write; a partial last line
    must not lose the ids already parsed."""
    lines = [
        "",
        "not json at all",
        _rec(type="file-history-snapshot", message=False),
        _rec(type="assistant", message={"role": "assistant"}),
        _rec(type="assistant", message={"role": "assistant", "model": "claude-opus-5"}),
        '{"type": "assistant", "message": {"model": "claude-op',
    ]
    assert models_from_transcript(lines) == ["claude-opus-5"]


def test_no_assistant_turns_yields_empty():
    assert models_from_transcript([_rec(type="user", message={"role": "user"})]) == []
