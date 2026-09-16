"""A session ends on the best state its own keep gate accepts — pinned on
the record.

Run `504793-5774c226` (2026-09-16, cluster `nightly`, iter 1), the session
aimed at `test_whitespace`: 8 turns, 32 minutes, one unit
(`src/ensure.rs::__dispatch_ensure`). Turns 1, 3, 5 and 7 turned the target
green and reddened 13 passengers (net -12); turns 4 and 8 left the target
red and measured net +1 with nothing newly red. The session's preference
order ranks its targets first, so 7 was the state it kept building on and 4
and 8 were put back as worse; the end-of-session gate ranks the net first
(`TurnReading.regressed`), refused 7, peeled the one unit and ended
`kept 0`. Fourteen minutes later the next fill opened a session on
`test_too_long` over the same unit and bought +1 in 8.8 minutes.

The plan's sentences, asserted as written:

- the record holds a judged state the gate accepts (+1, nothing newly red)
  and the session ended on one it refuses;
- replayed through the real `run_slice_session`, the session ends on turn
  8's state, `kept` = its one unit, and the row reads `net +1`;
- the session's preference order is unchanged: the turn it built on stays
  turn 7 and the transcript still says so.

Negative control: the same series under the pre-fix rule
(`end_on_keepable=False` — the preferred state alone, the peel from it the
only way back) ends `reverted / kept 0 / net 0` on turn 7's state, which is
the recorded row.
"""
from __future__ import annotations

from pathlib import Path

from tests.sessions.harness import load_sessions, replay

FIX = "504793-5774c226_iter1"


def _session() -> dict:
    return load_sessions(FIX)["sessions"][0]


def test_the_record_holds_a_keepable_state_the_session_threw_away():
    s = _session()
    assert s["tests"] == ["test_whitespace"]
    judged = {int(t["turn"]): t for t in s["turns"] if t["kind"] == "judged"}
    # What the end gate accepts: a strict net gain, nothing newly red or
    # crashed. The session measured it twice.
    keepable = [n for n, t in judged.items()
                if t["pass_delta"] > 0 and not t["newly_red"] and not t["crashed"]]
    assert keepable == [4, 8]
    assert all(judged[n]["pass_delta"] == 1 for n in keepable)
    # The state it ended on instead (turn 7, the last judged turn the
    # targets-first order preferred): the target green, 13 passengers red.
    assert max(judged) == 8 and judged[7]["targets_failing"] == []
    assert judged[7]["pass_delta"] == -12 and len(judged[7]["newly_red"]) == 13
    # No judged turn is both: a target green costs the 13, so the order that
    # picks the state and the gate that keeps it cannot agree on this slice.
    assert not [n for n, t in judged.items()
                if not t["targets_failing"] and t["pass_delta"] > 0]
    # And so the fill's row bought nothing.
    assert s["end"]["outcome"] == "reverted" and s["end"]["kept"] == []
    assert s["row"]["net_pass_delta"] == 0
    assert [u["fn"] for u in s["end"]["reverted"]] == ["__dispatch_ensure"]


def test_it_ends_on_turn_8s_state_kept_at_net_plus_1(tmp_path: Path):
    rep = replay(_session(), tmp_path / "fix")
    out = rep.outcome
    assert out.outcome == "kept"
    assert out.best_turn == 8
    assert {u.fn_name for u in out.kept} == {"__dispatch_ensure"}
    assert out.reverted == frozenset()
    assert rep.row_delta == 1
    assert out.final is not None and out.final.pass_delta == 1
    assert out.final.newly_red == ()
    assert all(b == "turn8" for u, b in rep.final_bodies.items() if u in out.kept)


def test_the_preference_order_is_unchanged_and_the_transcript_still_says_so(
        tmp_path: Path):
    """The fix is the END of the session, not what the model builds on: the
    targets-first order still chose turn 7, and every worse turn was still
    put back to it."""
    rep = replay(_session(), tmp_path / "fix")
    out = rep.outcome
    assert out.preferred_turn == 7
    assert out.restored_turns == 3          # turns 2, 4, 8
    # Two of the three were told: turn 8 was the last of the budget, so no
    # further user turn was written.
    told = [m for m in rep.user_turns if "RESTORED:" in m]
    assert len(told) == 2
    assert all("lost target(s) test_whitespace" in m for m in told)
    assert "back at turn 3's bodies" in told[-1]
    named = [ln for ln in rep.log if "keep gate refuses" in ln]
    assert len(named) == 1
    assert "turn 7's state (-12, 13 newly red)" in named[0]
    assert "ended on turn 8's state instead (+1, no regression)" in named[0]


def test_under_the_pre_fix_rule_it_ends_reverted_at_net_0(tmp_path: Path):
    s = _session()
    rep = replay(s, tmp_path / "control", end_on_keepable=False)
    out = rep.outcome
    assert out.outcome == "reverted"
    assert out.best_turn == 7
    assert out.kept == frozenset()
    assert {u.fn_name for u in out.reverted} == {"__dispatch_ensure"}
    assert rep.row_delta == 0
    assert out.final is not None and out.final.pass_delta == -12
    # The recorded row, reproduced.
    assert out.outcome == s["end"]["outcome"]
    assert {u.fn_name for u in out.reverted} == {u["fn"] for u in s["end"]["reverted"]}
    assert rep.row_delta == s["row"]["net_pass_delta"]
