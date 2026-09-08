"""The slice session keeps its best judged state — pinned on the record.

Run `3014175-d98ed09e` (2026-09-08, cluster `stable`, iter 3), replayed
through the real `run_slice_session` by `tests/sessions/harness.py` (which
names what it models). The plan's sentences, asserted as written:

- session #1 (targets `test_altdisplay`, `test_anyhow_interpolated_multiple_
  args`, `test_backtrace_reachable`, `test_deref_mut_via_downcast_mut`)
  ends on turn 1's tree with `kept` = its five units and the row reads
  `net +5`;
- session #4 (`test_debug_backtrace_captured_*`, `test_debug_format_with_
  context`) ends on turn 5's tree — `context` and `msg` from turns 4–5,
  `construct_from_adhoc` from turn 1, the `display` rewrites of turns 2, 3,
  6 and 7 put back — and the row reads `net +1`.

Negative control: the same series under the pre-fix rule (the opening
suite as the only reference, `keep_best=False`) end `kept 1 / net -4` and
on turn 7's tree at `net +0` — the recorded rows. (The record's `kept 3`
for #4 excludes a `display` body byte-identical to its baseline, which the
events do not carry; the replay counts it as changed.)
"""
from __future__ import annotations

from pathlib import Path

from tests.sessions.harness import load_sessions, replay

FIX = "3014175-d98ed09e_iter3"


def _session(index: int) -> dict:
    fix = load_sessions(FIX)
    s = fix["sessions"][index]
    assert s["index"] == index
    return s


def _fns(units) -> set[str]:
    return {u.fn_name for u in units}


def test_session_1_ends_on_turn_1s_tree_with_its_five_units_at_net_plus_5(tmp_path: Path):
    s = _session(1)
    assert s["row"]["net_pass_delta"] == -4 and s["row"]["outcome"] == "flat"
    rep = replay(s, tmp_path / "fix")
    out = rep.outcome
    assert out.best_turn == 1
    assert out.outcome == "kept"
    assert _fns(out.kept) == {"downcast_mut", "fmt", "is", "msg", "no_backtrace"}
    assert out.reverted == frozenset()
    assert rep.row_delta == 5
    assert out.final is not None and out.final.pass_delta == 5
    # Turn 3 (the `fmt` rewrite that reddened seven passengers) was put back.
    assert out.restored_turns == 1
    assert all(rep.final_bodies[u] == "turn1" for u in out.kept)
    judged = [d for name, d in rep.events
              if name == "phase_c.session.turn" and d["kind"] == "judged"]
    assert [(d["turn"], d["restored_to"], d["best_turn"]) for d in judged] == [
        (1, None, 1), (3, 1, 1)]
    # The transcript was told what turn 3 lost and where the tree is.
    told = [m for m in rep.user_turns if "RESTORED:" in m]
    assert len(told) == 1
    assert "reddened" in told[0] and "back at turn 1's bodies" in told[0]
    # A restored turn still consumed one of the eight.
    assert out.turns == 8 and out.model_calls == 8
    # The clock was told the tree's payoff, not the restored turn's.
    assert rep.deltas_noted == [5, 5]


def test_session_1_under_the_baseline_reference_ends_kept_1_at_net_minus_4(tmp_path: Path):
    s = _session(1)
    rep = replay(s, tmp_path / "control", keep_best=False)
    out = rep.outcome
    assert out.restored_turns == 0
    assert _fns(out.kept) == {"downcast_mut"}
    assert _fns(out.reverted) == {"fmt", "is", "msg", "no_backtrace"}
    assert rep.row_delta == -4
    assert {u["fn"] for u in s["end"]["kept"]} == _fns(out.kept)
    assert {u["fn"] for u in s["end"]["reverted"]} == _fns(out.reverted)


def test_session_4_ends_on_turn_5s_tree_at_net_plus_1(tmp_path: Path):
    s = _session(4)
    assert s["row"]["net_pass_delta"] == 0
    rep = replay(s, tmp_path / "fix")
    out = rep.outcome
    assert out.best_turn == 5
    assert out.outcome == "kept"
    assert _fns(out.kept) == {"construct_from_adhoc", "context", "msg"}
    body = {u.fn_name: b for u, b in rep.final_bodies.items()}
    assert body["construct_from_adhoc"] == "turn1"
    assert body["context"] == "turn4"
    assert body["msg"] == "turn5"
    assert body["display"] == "BASE"
    assert rep.row_delta == 1
    assert out.restored_turns == 4          # turns 2, 3, 6, 7
    assert out.final is not None
    assert "test_debug_format_with_context" in out.final.green_targets
    told = [m for m in rep.user_turns if "RESTORED:" in m]
    assert len(told) == 4
    assert all("lost target(s) test_debug_format_with_context" in m for m in told)
    assert "back at turn 1's bodies" in told[0]
    assert "back at turn 5's bodies" in told[-1]


def test_session_4_under_the_baseline_reference_ends_on_turn_7_at_net_0(tmp_path: Path):
    s = _session(4)
    rep = replay(s, tmp_path / "control", keep_best=False)
    out = rep.outcome
    assert out.best_turn == 7
    assert rep.row_delta == 0
    body = {u.fn_name: b for u, b in rep.final_bodies.items()}
    assert body["display"] == "turn7"
    assert out.final is not None
    assert "test_debug_format_with_context" not in out.final.green_targets
    assert {"construct_from_adhoc", "context", "msg"} <= _fns(out.kept)


def test_every_recorded_session_replays_to_its_recorded_end_under_the_baseline_reference(
        tmp_path: Path):
    """The model's coverage of the record: under the pre-fix rule every
    session of the fill ends with the recorded outcome, kept fns and
    reverted fns (the `display`-at-baseline case of #4 excepted, named in
    the module docstring)."""
    fix = load_sessions(FIX)
    for s in fix["sessions"]:
        if s["end"] is None:
            continue
        rep = replay(s, tmp_path / f"s{s['index']}", keep_best=False)
        out = rep.outcome
        assert out.outcome == s["end"]["outcome"], s["index"]
        if s["index"] == 4:
            continue
        assert _fns(out.kept) == {u["fn"] for u in s["end"]["kept"]}, s["index"]
        assert _fns(out.reverted) == {u["fn"] for u in s["end"]["reverted"]}, s["index"]
