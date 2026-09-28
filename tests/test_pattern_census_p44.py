"""P44 schedule invariant broken, on the recorded fill.

Run `82925-203ed7b8` (2026-09-28) fill 5: the per-fn run holding
`src/error.rs::context` — the first failure of 47 of 49 failing tests — was
deferred `coverage_split` behind the error.rs file and impl runs, which the
same schedule then deferred `rebuy_reverted`. The detector runs the
schedule-invariant checker over the recorded event. Control: the same event
with the per-fn run moved to the opened list (what the schedule of
2026-09-28 decides on that ranking) is not a hit.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _census():
    path = Path(__file__).resolve().parents[1] / "scripts" / "pattern_census.py"
    spec = importlib.util.spec_from_file_location("pattern_census_p44", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


census = _census()
_EVENT = json.loads((Path(__file__).parent / "fixtures" / "fills"
                     / "82925-203ed7b8_iter5_decided.json").read_text())["event"]


def test_the_recorded_fill_is_a_hit() -> None:
    hits = census.p44_schedule_violations([_EVENT])
    kinds = {h.split(" ", 2)[2].split(":", 1)[0] for h in hits}
    assert kinds == {"holder_not_opened", "blocker_unwritten"}
    assert any("per-fn 1 stub(s), 18 re-fill(s)" in h and "coverage_split" in h
               for h in hits)


def test_the_same_fill_with_the_stub_writer_opened_is_not_a_hit() -> None:
    per_fn = next(r for r in _EVENT["deferred"]
                  if r["label"].startswith("per-fn"))
    rest = [r for r in _EVENT["deferred"] if r is not per_fn
            and not r.get("held_by")]
    fixed = dict(_EVENT, runs=[{**per_fn, "held_by": [], "share_minutes": 0.0},
                               *_EVENT["runs"]],
                 deferred=rest)
    for r in fixed["runs"]:
        r["share_minutes"] = 0.0
    assert census.p44_schedule_violations([fixed]) == []


def test_an_unscheduled_fill_is_not_read() -> None:
    assert census.p44_schedule_violations([dict(_EVENT, scheduled=False)]) == []
