"""Ranking on the owner map, on run 2651653-7d5bce0f's recorded iteration 1.

The plan sentences this pins, in the plan's words rather than the code's:

  1. With the escalated cohort exempt, the reading demotes exactly ONE unit
     the dead set does not already cover, and it is `src/lib.rs::Ok`.
  2. Without that exemption the same reading demotes nine LIVE escalated
     units, five of them in the iteration's own never-served head — so the
     unexempted rule is not a stricter version of the right one, it guts the
     batch.
  3. On the recorded ranking `src/lib.rs::Ok` is inside the 12-unit cap
     before the demotion (that is what the live run dispatched) and outside
     it after.

The fixture is the run's own records: the owner map measured by
scripts/coverage_owner_probe.py on its .skeleton-filled/stable tree for the
seven tests its sweep reported failing, and the bucket lists verbatim from
its phase_c.dispatch.service_order / phase_c.attribution.escalation_sustained
events. Nothing here is a hand-made list of units chosen to make the rule
pass.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.domain.coverage_slice import (
    RED_DEAD_BODY,
    CoverageSlice,
    bind,
    instruction_for,
    red_dead_units,
)
from pipeline_core.domain.dispatch_batch import promote_by_service, take_batch
from pipeline_core.domain.models import FnLocator

_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "coverage"
    / "2651653_stable_red_dead.json"
)

_OK = FnLocator("src/lib.rs", "Ok")


def _loc(s: str) -> FnLocator:
    file, fn = s.rsplit("::", 1)
    return FnLocator(file, fn)


def _load() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _owners(data: dict) -> dict[str, frozenset[FnLocator]]:
    return {t: frozenset(_loc(u) for u in us)
            for t, us in data["owners"].items()}


def _ranked(data: dict) -> list[tuple[FnLocator, frozenset[str]]]:
    """The ranked units, reconstructed from the two events: the never-served
    head in its recorded order, then the escalated additions those buckets do
    not name (served-live, so they bucket between never-served and flat and
    cannot displace a never-served unit), then the flat sink, then the dead.

    39 units against the iteration's 33-unit frontier — the events over-cover
    it, and a superset only makes the 12-unit cap tighter than the live one,
    so it cannot manufacture the claim that `Ok` leaves the cap. Values are
    the attributed test sets the frontier carries; ranking never reads them,
    so they stay empty."""
    named = data["never_served"] + data["flat"] + data["dead"]
    extra = [s for s in data["escalated"] if s not in named]
    names = data["never_served"] + extra + data["flat"] + data["dead"]
    return [(_loc(s), frozenset()) for s in names]


def test_exempt_rule_adds_exactly_one_unit_to_the_dead_demotion() -> None:
    data = _load()
    ranked = _ranked(data)
    dead = {_loc(s) for s in data["dead"]}
    demoted = red_dead_units(
        (k for k, _ in ranked), data["failing"], _owners(data),
        exempt={_loc(s) for s in data["escalated"]})
    beyond_dead = demoted - dead
    assert beyond_dead == {_OK}, sorted(
        f"{k.file}::{k.fn_name}" for k in beyond_dead)


def test_without_the_exemption_the_rule_sinks_the_escalated_cohort() -> None:
    """The negative control that decides the shape of the rule: the same
    reading, no exemption. `src/error.rs::construct_from_boxed` and
    `::construct_from_display` are the constructors the producer-first
    promotion exists to lift, and they are unentered precisely because the
    chain breaks upstream of them."""
    data = _load()
    ranked = _ranked(data)
    dead = {_loc(s) for s in data["dead"]}
    escalated = {_loc(s) for s in data["escalated"]}
    blanket = red_dead_units(
        (k for k, _ in ranked), data["failing"], _owners(data))
    live_escalated_sunk = (blanket & escalated) - dead
    assert len(live_escalated_sunk) == 9, sorted(
        f"{k.file}::{k.fn_name}" for k in live_escalated_sunk)
    assert {_loc("src/error.rs::construct_from_boxed"),
            _loc("src/error.rs::construct_from_display")} <= live_escalated_sunk
    never_served = {_loc(s) for s in data["never_served"]}
    assert len(live_escalated_sunk & never_served) == 5


def test_ok_leaves_the_cap_and_the_pre_fix_ranking_keeps_it() -> None:
    data = _load()
    ranked = _ranked(data)
    dead = {_loc(s) for s in data["dead"]}
    flat = {_loc(s) for s in data["flat"]}
    served = {k for k, _ in ranked} - {_loc(s) for s in data["never_served"]}
    cap = data["dispatch_cap"]

    before = promote_by_service(ranked, served, flat, dead)
    before_batch = take_batch([k for k, _ in before], set(), cap).keys
    assert _OK in set(before_batch), "negative control: the live run bought it"

    demoted = red_dead_units(
        (k for k, _ in ranked), data["failing"], _owners(data),
        exempt={_loc(s) for s in data["escalated"]})
    after = promote_by_service(ranked, served, flat, dead, demoted)
    after_batch = set(take_batch([k for k, _ in after], set(), cap).keys)
    assert _OK not in after_batch
    # The demotion must not cost the batch its earned never-served units.
    for name in ("src/lib.rs::format_err", "src/lib.rs::must_use",
                 "src/error.rs::from_boxed", "src/chain.rs::len"):
        assert _loc(name) in after_batch, name


def test_the_reading_declines_on_ignorance() -> None:
    """Unmeasured, empty and no-red-set all leave the ranking alone: a
    crashed binary writes no profile, and 'empty' is what a crash produces."""
    data = _load()
    units = [_loc("src/lib.rs::Ok")]
    owners = _owners(data)
    assert red_dead_units(units, data["failing"], {}) == frozenset()
    assert red_dead_units(units, [], owners) == frozenset()
    crashed = {t: frozenset() for t in data["failing"]}
    assert red_dead_units(units, data["failing"], crashed) == frozenset()
    partial = {**owners, "test_trailer": frozenset()}
    assert red_dead_units(units, data["failing"], partial) == frozenset()


def test_the_prompt_sentence_fires_for_an_exempt_escalated_producer() -> None:
    """The demotion exempts the escalated cohort, so those units are still
    dispatched — and on this iteration nine of them are entered by no failing
    test. The prompt has to say so; before this the model saw only the write
    list. `src/error.rs::construct` (entered by all seven) must stay silent."""
    data = _load()
    reading = CoverageSlice(
        dead=frozenset(_loc(s) for s in data["dead"]),
        owners=_owners(data),
        failing=frozenset(data["failing"]),
    )
    try:
        bind(reading)
        assert RED_DEAD_BODY in instruction_for(
            "src/error.rs", "construct_from_display")
        assert RED_DEAD_BODY in instruction_for("src/error.rs", "deref_mut")
        assert instruction_for("src/error.rs", "construct") == ""
        # A dead body keeps the stronger sentence, not both.
        dead_text = instruction_for("src/error.rs", "context_backtrace")
        assert RED_DEAD_BODY not in dead_text
        assert dead_text
    finally:
        bind(None)


def test_the_prompt_sentence_declines_without_the_red_set() -> None:
    """A slice built before the red set was carried says nothing new: the
    positive-evidence rule needs the set the map was ASKED for."""
    data = _load()
    try:
        bind(CoverageSlice(owners=_owners(data)))
        assert RED_DEAD_BODY not in instruction_for(
            "src/error.rs", "construct_from_display")
    finally:
        bind(None)
