"""P22 route starvation and P23 declined units.

P22 sentence: a session that opened FIRST in a fill while the plan event
said leftover tests were hollow and no stub route had landed in that fill
is a census row; the same fill with a stub-route landing before the
session, or with the schedule line present, is not. Run 334902-2774708c
iter 11 (2026-09-06 05:23 UTC) is the recorded positive instance.

P23 sentence: a `[phase-c/declined]` line is a row; pre-fix, a reader-loss
line whose reply artefact holds empty `<FN>` tags is a row (declined, not
lost); a reader-loss line whose reply has bodies in every tag is not.
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

_SERVICE = ("  [stable] [phase-c/dispatch] service order: 1 never-served unit(s) "
            "rank before served; 5 last-graded flat sink")
_OPENING = ("  [phase-c/session] session[18 test(s), 32 unit(s)]: opening — "
            "targets test_altdebug, test_debug; editable 32 unit(s) in 9 "
            "file(s); budget 8 turn(s).")
_FILE_FILLED = "  [phase-c/file] src/error.rs: FILE FILLED 16 fn(s) in one pass."
_SCHEDULE = ("  [phase-c/schedule] fill 11: 3 run(s) — 1. file src/error.rs "
             "(never_served; 4 stub(s)); 2. per-fn 4 stub(s), 0 re-fill(s) "
             "(never_served; 4 stub(s)); 3. session[18 test(s), 32 unit(s)] "
             "(never_served); budget unbounded; hollow 24 test(s); ledger 0 row(s)")


def _run(*lines: str, run_id: str = "t") -> object:
    run = census.Run(run_id, "g", "c", "ts", "start:1")
    run.lines = [(f"log:{i}", ln) for i, ln in enumerate(lines, 1)]
    return run


def test_p22_hits_a_session_opened_first_while_tests_were_hollow(monkeypatch) -> None:
    monkeypatch.setattr(census, "_SESSION_PLAN_EVENTS", {
        "t": [{"hollow": 24, "fitted": 3, "sessions": [{"tests": ["a"]}], "ts": ""}]})
    hits = census.detect_p22(_run(_SERVICE, _OPENING), [])
    assert len(hits) == 1
    assert "24 leftover test(s) were hollow" in hits[0]["evidence"]


def test_p22_is_silent_when_a_stub_route_landed_first_or_the_schedule_ran(monkeypatch) -> None:
    monkeypatch.setattr(census, "_SESSION_PLAN_EVENTS", {
        "t": [{"hollow": 24, "fitted": 3, "sessions": [{"tests": ["a"]}], "ts": ""}]})
    assert census.detect_p22(_run(_SERVICE, _FILE_FILLED, _OPENING), []) == []
    assert census.detect_p22(_run(_SERVICE, _SCHEDULE, _OPENING), []) == []
    # No hollow test on the plan: the 91-plateau shape, not this class.
    monkeypatch.setattr(census, "_SESSION_PLAN_EVENTS", {
        "t": [{"hollow": 0, "fitted": 5, "sessions": [{"tests": ["a"]}], "ts": ""}]})
    assert census.detect_p22(_run(_SERVICE, _OPENING), []) == []


_DECLINED = ("  [phase-c/declined] file src/error.rs: declined 3 unit(s) — the "
             "model answered an empty body: context_chain_drop_rest, "
             "context_drop_rest, object_drop_front (reply sha c4eacd9b1437…, "
             "logs/prompts/c4eacd9b1437.txt)")


def test_p23_hits_the_declined_line_and_an_empty_tag_reply(tmp_path: Path, monkeypatch) -> None:
    hits = census.detect_p23(_run(_DECLINED), [])
    assert len(hits) == 1 and "declined 3" in hits[0]["evidence"]
    sha = "a" * 64
    prompts = tmp_path / "logs" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / f"{sha}.txt").write_text(
        '<FN name="ok" block="">\nbody\n</FN>\n<FN name="gone" block="">\n</FN>',
        encoding="utf-8")
    monkeypatch.setattr(census, "EVENTS_PATH", tmp_path / "logs" / "events.jsonl")
    loss = (f"  HEALTH ‼ [reader_loss] file src/x.rs: 2 FN tag(s) in the reply, "
            f"1 record(s) out — 1 answered body(ies) discarded by the READER. "
            f"Reply sha {sha[:12]}… (logs/prompts/{sha}.txt); this is a parser "
            "defect, NOT a model failure.")
    hits2 = census.detect_p23(_run(loss), [])
    assert len(hits2) == 1 and "gone" in hits2[0]["evidence"]
    # Control: every tag carried a body — a reader loss, not a decline.
    (prompts / f"{sha}.txt").write_text(
        '<FN name="ok" block="">\nbody\n</FN>\n<FN name="gone" block="">\nx\n</FN>',
        encoding="utf-8")
    assert census.detect_p23(_run(loss), []) == []
