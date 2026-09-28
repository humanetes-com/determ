"""P44 schedule invariant broken, on the recorded fill.

Run `82925-203ed7b8` (2026-09-28) fill 5: the per-fn run holding
`src/error.rs::context` — the first failure of 47 of 49 failing tests — was
deferred `coverage_split` behind the error.rs file and impl runs, which the
same schedule then deferred `rebuy_reverted`. The detector runs the
schedule-invariant checker over the recorded event. Control: today's
admission on that ranking, written back as an event, is not a hit.
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
    # The hold names the re-buy runs the same decision deferred. The reason
    # text is on the blocker line; the holder line names them.
    assert any("holder_not_opened" in h
               and "file src/error.rs [1 re-buy unit(s)]" in h
               and "which did not open" in h for h in hits)
    assert any("blocker_unwritten" in h and "rebuy_reverted" in h for h in hits)


def test_todays_decision_of_the_recorded_ranking_is_not_a_hit() -> None:
    """What today's admission decides on that ranking, not the recorded
    event with its holds deleted."""
    from pipeline_core.domain.fill_schedule import admit_ranked
    from tests.test_schedule_invariants import _ranked_inputs, _recorded

    rec = _recorded()
    sched = admit_ranked(**_ranked_inputs(rec))
    fixed = dict(
        _EVENT,
        runs=[s.to_dict() for s in sched.ordered],
        deferred=[s.to_dict() for s in sched.deferred],
        budget_minutes=sched.budget_minutes)
    assert census.p44_schedule_violations([fixed]) == []


def test_an_unscheduled_fill_is_not_read() -> None:
    assert census.p44_schedule_violations([dict(_EVENT, scheduled=False)]) == []
