"""Reading facts back out of a Claude Code session transcript (``*.jsonl``).

An unattended operator pass must record WHICH model wrote its report, and the
only authoritative source is the transcript the CLI wrote for that session: the
``--model`` flag takes an ALIAS (``opus``) while the assistant records carry the
EXACT id (``claude-opus-5``), and the alias→id mapping moves with releases.

Parsing is pure and tolerant: a transcript is an append-only stream that can end
mid-write, and record shapes vary by CLI version, so unknown/undecodable records
are skipped rather than raising.
"""
from __future__ import annotations

import json
from collections.abc import Iterable


def models_from_transcript(lines: Iterable[str]) -> list[str]:
    """Exact model ids used by the session's own assistant turns, in first-seen
    order, without duplicates. Sidechain (subagent) turns are excluded so a
    delegated helper model never masquerades as the model that ran the pass."""
    seen: list[str] = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "assistant":
            continue
        if rec.get("isSidechain"):
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        model = msg.get("model")
        if isinstance(model, str) and model and model not in seen:
            seen.append(model)
    return seen
