"""P54 — a planned session that sent no turn 0.

Plan sentence: a session route line saying the turn 0 at the narrowest
rendering is over capacity and the session is not opened is a census row,
one per run, keyed by the slice labels; both the pre-fix wording (run
`3203804-8c6a0066`, 2026-11-03: `(charged N on the planned slice)`) and the
current one (`is N chars, over capacity … largest sections:`) match. A
session that sent its turn 0 is not this class. Reactions: the packer's
`no session — turn 0 is` report and the fill's `could not open its prompt`
slot release.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _census():
    path = Path(__file__).resolve().parents[1] / "scripts" / "pattern_census.py"
    spec = importlib.util.spec_from_file_location("pattern_census", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


census = _census()

_OLD = ("  [phase-c/session] session[1 test(s), 14 unit(s)]: turn 0 at the narrowest "
        "rendering is over capacity 132,000 (charged 121,729 on the planned "
        "slice) — the session is not opened")
_NEW = ("  [phase-c/session] session[1 test(s), 4 unit(s)]: turn 0 at the narrowest "
        "rendering is 140,112 chars, over capacity 132,000 (planned at "
        "files@18,000, charged 121,121) — the session is not opened; largest "
        "sections: TYPES_IN_SCOPE 37,106")
_SENT = ("  [phase-c/session] session[2 test(s), 3 unit(s)]: turn 0 126,456 chars at "
         "file threshold 18,000 (charged 125,045 on the planned slice, capacity "
         "132,000)")


def _run(*lines: str) -> object:
    run = census.Run("t", "g", "c", "ts", "start:1")
    run.lines = [(f"log:{i}", ln) for i, ln in enumerate(lines, 1)]
    return run


def test_p54_both_wordings_are_the_class() -> None:
    hits = census.detect_p54(_run(_OLD, _SENT, _NEW, _OLD), [])
    assert len(hits) == 1
    assert "3 planned session(s) sent no turn 0" in hits[0]["evidence"]
    assert hits[0]["key"][0] == "session[1 test(s), 14 unit(s)]"
    assert hits[0]["refs"] == ["log:1", "log:3", "log:4"]


def test_p54_a_sent_turn0_is_not_the_class() -> None:
    assert census.detect_p54(_run(_SENT), []) == []


def test_p54_is_registered() -> None:
    assert "P54" in census.DETECTORS and "P54" in census.CLASS_NAMES
