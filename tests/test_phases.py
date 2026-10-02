"""The phase announcer: one way for a phase to say it started and ended."""
from __future__ import annotations

import pytest

from pipeline_core import log as log_mod
from pipeline_core import phases


@pytest.fixture
def wire():
    events: list[tuple[str, dict]] = []
    lines: list[str] = []
    current: list[str] = []
    orig_log, orig_emit = log_mod._log_sink, log_mod._emit_sink
    log_mod.set_sinks(lines.append, lambda ev, **f: events.append((ev, f)))
    phases.bind_current(current.append)
    yield events, lines, current
    log_mod.set_sinks(orig_log, orig_emit)
    phases.bind_current(None)


def _kinds(events):
    return [(e, f["phase"], f.get("outcome")) for e, f in events]


def test_scope_steps_and_exit(wire):
    events, _lines, current = wire
    with phases.phase_scope("bake", module="m") as ph:
        ph.step("bake.preserve")
        ph.step("bake.api")
    assert _kinds(events) == [
        ("phase.enter", "bake", None),
        ("phase.enter", "bake.preserve", None),
        ("phase.exit", "bake.preserve", "ok"),
        ("phase.enter", "bake.api", None),
        ("phase.exit", "bake.api", "ok"),
        ("phase.exit", "bake", "ok"),
    ]
    assert current[0].startswith("bake / ") and "API surface" in current[-1]


def test_exception_is_an_error_exit_and_reraises(wire):
    events, _l, _c = wire
    with pytest.raises(ValueError), phases.phase_scope("strip") as ph:
        ph.step("strip.source")
        raise ValueError("boom")
    assert ("phase.exit", "strip.source", "error") in _kinds(events)
    assert _kinds(events)[-1] == ("phase.exit", "strip", "error")


def test_clean_sys_exit_is_ok_and_nonzero_is_error(wire):
    events, _l, _c = wire
    with pytest.raises(SystemExit), phases.phase_scope("lock"):
        raise SystemExit(0)
    with pytest.raises(SystemExit), phases.phase_scope("lock"):
        raise SystemExit(1)
    outcomes = [o for e, _p, o in _kinds(events) if e == "phase.exit"]
    assert outcomes == ["ok", "error"]


def test_explicit_open_close_and_double_close_is_one_exit(wire):
    events, _l, _c = wire
    ph = phases.phase_scope("setup").open()
    ph.step("setup.cfg_scan")
    ph.close("skipped")
    ph.close("ok")
    exits = [(p, o) for e, p, o in _kinds(events) if e == "phase.exit"]
    assert exits == [("setup.cfg_scan", "skipped"), ("setup", "skipped")]


def test_ambient_step_finds_the_open_parent_scope(wire):
    events, lines, _c = wire
    with phases.phase_scope("bake"):
        phases.step("bake.extract", cluster="stable")
        phases.step("bake.surface", cluster="stable")
    enters = [(f["phase"], f.get("cluster")) for e, f in events if e == "phase.enter"]
    assert ("bake.extract", "stable") in enters and ("bake.surface", "stable") in enters
    assert not lines


def test_book_work_is_reported_on_the_open_book_step_only(wire):
    events, _lines, _c = wire
    phases.note_work(done=1, total=4, calls=1, lanes=2, wave="chunks")
    with phases.phase_scope("bake"):
        phases.step("bake.extract", cluster="stable")
        phases.note_work(done=1, total=4, calls=1, lanes=2, wave="chunks")
        phases.step("bake.book")
        phases.note_work(done=0, total=4, calls=0, lanes=2, wave="chunks")
        phases.note_work(done=4, total=4, calls=9, lanes=2, wave="chunks")
        phases.note_work(done=1, total=2, calls=11, lanes=2, wave="siblings")
        phases.note_work(done=1, total=0, calls=11, lanes=2, wave="chunks")
        phases.note_work(done=1, total=2, calls=11, lanes=2, wave="features")
    progress = [f for e, f in events if e == "phase.progress"]
    assert [p["phase"] for p in progress] == ["bake.book", "bake.book", "bake.book"]
    assert [p["data"] for p in progress] == [
        {"wave": "chunks", "done": 0, "total": 4, "calls": 0, "lanes": 2},
        {"wave": "chunks", "done": 4, "total": 4, "calls": 9, "lanes": 2},
        {"wave": "siblings", "done": 1, "total": 2, "calls": 11, "lanes": 2},
    ]
    assert all(e != "phase.progress" or f["phase"] == "bake.book" for e, f in events)


def test_step_without_its_parent_scope_is_loud_not_silent(wire):
    events, lines, _c = wire
    phases.step("bake.book", cluster="stable")
    assert any("HEALTH" in ln and "bake.book" in ln for ln in lines)
    assert events, "still emitted: observability never drops the reading"


def test_unknown_id_is_flagged_not_fatal(wire):
    events, lines, _c = wire
    with phases.phase_scope("no_such_phase"):
        pass
    assert any("unknown phase id" in ln for ln in lines)
    assert events[0][1]["data"]["unknown"] is True


def test_skipped_phase_is_an_exit_without_an_enter(wire):
    events, _l, _c = wire
    phases.phase_skipped("t0d", "already harvested", cluster="stable")
    assert _kinds(events) == [("phase.exit", "t0d", "skipped")]
    assert events[0][1]["cluster"] == "stable"
