"""The held-stub obligation on the recorded fill 5 of run `857717-812ee44c`
(2026-10-11, cluster `stable`).

That fill's schedule had `src/wrapper.rs::source` (3 red tests stop in it)
in no candidate run's targets at all: it was not in the batch, and no session
listed it either. The schedule check said nothing about it, because the
blockers were intersected with the candidates' targets before anything was
checked.

Asserted on the recorded event: the checker now names the stub as unwritten,
and the frozen pre-change check stays silent (the negative control). Then
the fill as the rank leaves it: every held stub is in the batch, so the
per-fn route plans a run that targets it; the schedule opens a writer that
aims at each held stub, and the check passes.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pipeline_core.domain.fill_schedule import RouteKind, RouteRun, schedule
from pipeline_core.domain.held_stubs import HeldStubs
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.schedule_invariants import (
    Decided,
    blamed_from_event,
    decided_from_event,
    decided_from_schedule,
    violations,
)

FIXTURE = (Path(__file__).parent / "fixtures" / "fills"
           / "857717-812ee44c_iter5_schedule.json")
SOURCE = FnLocator("src/wrapper.rs", "source")
DROP_FRONT = FnLocator("src/error.rs", "object_drop_front")

#: `phase_c.crash.stub_holds` iter 5 of the same run: the first-failure holds.
HELD = {
    "boxed_error_debug_alternate_reaches_wrapper_debug": frozenset({SOURCE}),
    "boxed_error_display_is_forwarded": frozenset({SOURCE}),
    "test_from_boxed_roundtrip_preserves_source": frozenset({SOURCE}),
    "test_drop": frozenset({DROP_FRONT}),
}


def _event() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data["event"]


def _loc(s: str) -> FnLocator:
    f, _, fn = s.partition("::")
    return FnLocator(f, fn)


def _pre_change_blockers_unwritten(decided: list[Decided],
                                   hollow_stubs: frozenset[FnLocator],
                                   crash_blamed: frozenset[FnLocator],
                                   ) -> set[FnLocator]:
    """Frozen copy of the deleted check: blockers intersected with the
    candidates' targets; any opened run with the unit in its targets wrote
    it."""
    cand: set[FnLocator] = set()
    for d in decided:
        cand |= d.targets
    blockers = (crash_blamed & cand) | (hollow_stubs & cand)
    written: set[FnLocator] = set()
    for d in decided:
        if d.opened:
            written |= d.targets
    return set(blockers - written)


def test_no_recorded_candidate_targeted_the_held_stub() -> None:
    """The fixture's premise, read off the recorded runs: opened or
    deferred, unit route or session, none targeted the stub."""
    ev = _event()
    writers = [r["label"] for r in ev["runs"] + ev["deferred"]
               if "src/wrapper.rs::source" in (r.get("targets") or [])]
    assert writers == []


def test_the_check_now_names_the_held_stub_and_the_old_one_did_not() -> None:
    ev = _event()
    decided = decided_from_event(ev)
    found = violations(decided, hollow=HELD,
                       crash_blamed=blamed_from_event(ev),
                       budget_minutes=float(ev["budget_minutes"]))
    unwritten = [v.detail for v in found if v.kind == "blocker_unwritten"]
    assert any(d.startswith("src/wrapper.rs::source is written by no opened run")
               for d in unwritten), unwritten
    # NEGATIVE CONTROL: the check as it ran on the fill, silent about it.
    old = _pre_change_blockers_unwritten(
        decided, frozenset().union(*HELD.values()),
        frozenset(blamed_from_event(ev)))
    assert SOURCE not in old


def test_with_every_held_stub_batched_the_schedule_opens_an_aimed_writer() -> None:
    """The fill as the rank leaves it: the held stubs are in the batch, so
    the per-fn route plans them. The recorded candidates are replayed with
    that per-fn run beside them."""
    ev = _event()
    runs: list[RouteRun] = []
    for seq, raw in enumerate(ev["runs"] + ev["deferred"]):
        runs.append(RouteRun(
            kind=RouteKind(str(raw["kind"])), label=str(raw["label"]),
            targets=frozenset(_loc(t) for t in raw.get("targets") or ()),
            target_tests=frozenset(str(t) for t in raw.get("target_tests") or ()),
            stubs_in_targets=frozenset(_loc(t) for t in raw.get("stubs") or ()),
            files=tuple(str(f) for f in raw.get("files") or ()),
            seq=int(raw.get("seq", seq) or seq),
            bodies_sha=str(raw.get("bodies_sha", ""))))
    held = HeldStubs(HELD)
    per_fn = RouteRun(
        kind=RouteKind.PER_FN, label="per-fn held", targets=held.units,
        target_tests=frozenset(HELD), stubs_in_targets=held.units,
        files=tuple(sorted({u.file for u in held.units})), seq=99,
        bodies_sha="h",
        unit_tests=tuple((u, held.tests_held_by(u))
                         for u in sorted(held.units,
                                         key=lambda x: (x.file, x.fn_name))))
    sched = schedule([*runs, per_fn], [], oracle_fp="o", hollow=HELD,
                     budget_minutes=float(ev["budget_minutes"]),
                     crash_blamed=held.units, bodies_sha=lambda _t: "b")
    for u in held.units:
        assert any(held.writes(u, targets=s.run.targets,
                               aimed_tests=s.run.target_tests,
                               session=s.run.kind is RouteKind.SESSION)
                   for s in sched.ordered), f"{u.file}::{u.fn_name} unwritten"
    found = violations(decided_from_schedule(sched), hollow=HELD,
                       crash_blamed=held.units,
                       budget_minutes=float(ev["budget_minutes"]))
    assert not [v for v in found if v.kind == "blocker_unwritten"], [
        v.render() for v in found]
