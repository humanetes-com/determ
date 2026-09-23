"""The exemption replay cuts the body ledger where the fill ranked, and joins
a standing flat to the body's sha.

The sweep grades a landing under the iteration the fill then shares, and the
fill writes its own landings under that same iteration. Dropping every row at
the iteration drops the grades the schedule just received. A standing-flat
name joined to the empty string matches no landing sha.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


def _replay():
    path = Path(__file__).resolve().parents[1] / "scripts" / "exemption_replay.py"
    spec = importlib.util.spec_from_file_location("exemption_replay_cut", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


replay = _replay()


def _row(run: str, iteration: int, state: str, sha: str, fn: str = "f"):
    return SimpleNamespace(
        run_id=run, iteration=iteration, state=state, body_sha=sha,
        key=SimpleNamespace(file="src/a.rs", fn_name=fn))


def test_the_sweep_grade_at_the_fill_iteration_stays_visible() -> None:
    rows = [
        _row("r", 1, "landed", "sha"),
        _row("r", 2, "flat", "sha"),       # the sweep, before the fill
        _row("r", 2, "landed", "new"),     # this fill, after the schedule
        _row("other", 9, "landed", "x"),
    ]
    visible = replay.rows_visible_at_schedule(
        rows, run_id="r", iteration=2, body_rows=None)
    assert [r.body_sha for r in visible] == ["sha", "sha", "x"]


def test_a_recorded_row_count_is_the_cut() -> None:
    rows = [_row("r", 1, "landed", "a"), _row("r", 2, "flat", "b")]
    visible = replay.rows_visible_at_schedule(
        rows, run_id="r", iteration=2, body_rows=1)
    assert [r.body_sha for r in visible] == ["a"]


def test_a_flat_name_is_joined_to_the_body_sha() -> None:
    rows = [_row("r", 2, "flat", "sha-real")]
    got = replay.standing_flat_at({}, ["src/a.rs::f"], rows)
    assert got[replay._loc("src/a.rs::f")] == "sha-real"


def test_a_recorded_standing_map_is_the_ranking_s_own() -> None:
    got = replay.standing_flat_at(
        {"standing_flat": {"src/a.rs::f": "from-the-event"}},
        ["src/a.rs::f"],
        [_row("r", 2, "flat", "from-the-ledger")])
    assert got[replay._loc("src/a.rs::f")] == "from-the-event"
