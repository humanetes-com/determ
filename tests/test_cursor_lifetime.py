"""The pass cursor's lifetime rule: a code move trims it, it never voids it.

`cursor_mismatch` used to drop the whole cursor whenever `code_id` moved, and
`code_identity` hashes pipeline.py plus every `.py` under `pipeline_core` — so
a docstring edit cost a traversal. The record over 2026-08-17 -> 08-20 is
sixteen code identities in four days, each restarting a 43-unit crawl at 0, in
a week whose entire purpose was the reading a COMPLETED pass produces.

The narrower module hash that suggests itself was measured before it was
written (`scripts/cursor_identity_replay.py`, 41 `code_id` transitions on
`stable`): it would have preserved 4 of 41 and carried zero units, because the
modules it must contain are the modules a fix session edits. So the identity
is not the lever. What a code change actually invalidates is not the cursor's
VALIDITY but a RETRY — and only for the units whose last attempt produced
nothing. That is what `cursor_carry` implements and what these tests pin.
"""
from __future__ import annotations

from dataclasses import replace

from pipeline_core.domain.dispatch_batch import DispatchBatch
from pipeline_core.domain.dispatch_rank import (
    UnitFacts,
    rank,
    streak_after_turn,
    take,
)
from pipeline_core.domain.tdd_floor import (
    TddFloor,
    cursor_carry,
    cursor_mismatch,
    resume_floor,
)

_FLOOR = TddFloor(cluster="stable", best_pass=92, stall=1, tree_sha="20bc7274",
                  oracle_size=105, code_id="bd3a0df7055b")
_KEY = {"cluster": "stable", "tree_sha": "20bc7274", "oracle_size": 105,
        "code_id": "bd3a0df7055b"}


def _floor(attempted: tuple[str, ...], landed: tuple[str, ...] = (),
           declined: tuple[str, ...] = ()) -> TddFloor:
    return replace(_FLOOR, attempted=attempted, landed=landed,
                   declined=declined)


def test_same_code_carries_the_whole_cursor() -> None:
    f = _floor(("a::x", "b::y", "c::z"), landed=("a::x",), declined=("b::y",))
    carry = cursor_carry(f, cluster="stable", code_id=_FLOOR.code_id)
    assert carry.attempted == ("a::x", "b::y", "c::z")
    assert carry.released == ()
    assert carry.carried_all


def test_a_moved_code_id_releases_only_the_units_that_produced_nothing() -> None:
    """The three populations an `attempted` set is made of get three answers.

    `a::x` landed a semantic body change — it was genuinely tried, and another
    model call inside the same pass buys a second lottery ticket on a unit that
    already answered. `b::y` was INFERRED and instructed to return its body
    unchanged, so landing nothing was the instructed outcome. `c::z` was
    dispatched and nothing came back — a routing drop, a splice refusal, a
    compile-failed peel — which is exactly the class a fill/route/prompt fix
    turns into an answer, so it is released for an immediate retry."""
    f = _floor(("a::x", "b::y", "c::z"), landed=("a::x",), declined=("b::y",))
    carry = cursor_carry(f, cluster="stable", code_id="MOVED")
    assert carry.attempted == ("a::x", "b::y")
    assert carry.released == ("c::z",)
    assert carry.landed == ("a::x",) and carry.declined == ("b::y",)
    assert not carry.carried_all
    assert "released for retry" in carry.note


def test_a_different_cluster_still_voids_the_cursor_entirely() -> None:
    """The one population change a carried cursor cannot survive: different
    fns entirely. Everything else — a moved oracle, a changed tree, a new code
    build — leaves the units meaningful."""
    f = _floor(("a::x",), landed=("a::x",))
    assert cursor_mismatch(f, cluster="nightly") is not None
    assert cursor_carry(f, cluster="nightly", code_id="MOVED").attempted == ()
    assert resume_floor(f, **{**_KEY, "cluster": "nightly"}).attempted is None


def test_the_carry_survives_the_round_trip_through_disk() -> None:
    """`declined` has to persist or the NEXT relaunch cannot tell a unit that
    was told to change nothing from one that failed to."""
    f = _floor(("a::x", "b::y"), landed=("a::x",), declined=("b::y",))
    back = TddFloor.from_dict(f.to_dict())
    assert back is not None
    assert back.declined == ("b::y",)
    assert cursor_carry(back, cluster="stable", code_id="MOVED").attempted == (
        "a::x", "b::y")
    # An older floor with no `declined` key parses, and every unit that landed
    # nothing is then released — the conservative reading, which is today's
    # behaviour for that half of the cursor.
    raw = f.to_dict()
    raw.pop("declined")
    older = TddFloor.from_dict(raw)
    assert older is not None and older.declined == ()
    assert cursor_carry(older, cluster="stable", code_id="MOVED").released == (
        "b::y",)


def _batch(frontier: tuple[str, ...], streak: dict[str, int],
           traversed: tuple[str, ...], cap: int) -> DispatchBatch[str]:
    ranked = rank({k: UnitFacts(static_index=i, unclaimed=streak.get(k, 0))
                   for i, k in enumerate(frontier)})
    return take(ranked, (), cap, cohort=frontier, traversed=traversed)


def test_negative_control_the_old_rule_re_sends_the_identical_first_batch() -> None:
    """The lock a relaunch must not hit, on the rank.

    `order_frontier` is deterministic, so a relaunch that knows nothing of the
    last process re-sends the same prefix, reads the same score and stops
    again — units 13..43 unreachable without a human editing the env. What
    carries the last process's work into the order now is the unclaimed
    streak, persisted with the floor; the cursor is pass accounting only."""
    frontier = tuple(f"src/f{i:02d}.rs::fn{i:02d}" for i in range(43))
    cap = 12
    first = _batch(frontier, {}, (), cap)
    assert len(first.keys) == cap

    # The stopped process landed 6 of its 12 and declined 2; the other 4 came
    # back with nothing. Every one of the 12 was opened by a route, and none
    # of them is a fault site, so every open counted as a turn.
    landed = first.keys[:6]
    declined = first.keys[6:8]
    streak, _retried = streak_after_turn(
        {}, frontier, first.keys, landed, (), ())
    saved = replace(_floor(tuple(first.keys), landed=tuple(landed),
                           declined=tuple(declined)),
                    unclaimed=tuple(sorted((k, n) for k, n in streak.items()
                                           if n > 0)))
    back = TddFloor.from_dict(saved.to_dict())
    assert back is not None

    # Negative control: a resume that drops the streak re-sends the
    # identical first batch — this is the lock.
    replayed = _batch(frontier, {}, (), cap)
    assert replayed.keys == first.keys

    # The resume: the streak comes back from disk and the batch is the next
    # slice; the pass accounting carries the traversal across the code move.
    resumed_streak = dict(resume_floor(back, **{**_KEY, "code_id": "MOVED"})
                          .unclaimed)
    carry = cursor_carry(back, cluster="stable", code_id="MOVED")
    resumed = _batch(frontier, resumed_streak, carry.attempted, cap)
    assert set(resumed.keys).isdisjoint(first.keys), (
        "a unit a route already gave a turn must not lead the next batch "
        "while units the pass has not offered wait")
    assert resumed.attempted_before == len(carry.attempted)


def test_the_release_is_reported_on_the_resume() -> None:
    """A silent release reads as a cursor that mysteriously shrank."""
    f = _floor(("a::x", "b::y", "c::z"), landed=("a::x",), declined=("b::y",))
    r = resume_floor(f, **{**_KEY, "code_id": "MOVED"})
    assert r.attempted == ("a::x", "b::y")
    assert r.released == ("c::z",)
    assert "released for retry" in r.note
