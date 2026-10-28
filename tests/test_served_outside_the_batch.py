"""A unit a run of the fill wrote outside the dispatch batch was served.

The driver read served and landed through the batch: the opened set was the
dispatched units intersected with the batch, and the landed set the bodies
of the batch that changed. A schedule run writes units the dispatch did not
batch (a session's editable set, a file run's other members, a run opened on
a measured blocker), so such a unit kept its starvation streak and its
never-served rank and took a batch slot on the next sweep (`HEALTH ‼
EVIDENCED_STARVED`, 4 fires on the 2026-10-22 full read, 3 of them on
`391506-3403a6a7`). Plan sentence: the streak and the served stamp read every
unit the fill opened or landed. NEGATIVE CONTROL: the batch-scoped sets keep
the out-of-batch unit's streak climbing. Invented names.
"""
from __future__ import annotations

from pipeline_core.domain.dispatch_rank import streak_after_turn
from pipeline_core.domain.landed_change import opened_in_fill
from pipeline_core.domain.models import FnLocator

_IN = FnLocator("src/a.rs", "inside")
_OUT = FnLocator("src/a.rs", "outside")
_LOST = FnLocator("src/a.rs", "lost")


def test_a_unit_opened_outside_the_batch_resets_its_streak() -> None:
    ranked = [_IN, _OUT]
    streak = {_IN: 2, _OUT: 3}
    opened = opened_in_fill({_IN, _OUT, _LOST}, [_LOST], fallback=[_IN])
    assert opened == {_IN, _OUT}
    after, _ = streak_after_turn(streak, ranked, opened, frozenset(),
                                 frozenset(), frozenset())
    assert after.get(_OUT, 0) == 0
    # Control: the batch-scoped reading the driver used before.
    batch_only = {_IN, _OUT, _LOST} & {_IN}
    before, _ = streak_after_turn(streak, ranked, batch_only, frozenset(),
                                  frozenset(), frozenset())
    assert before[_OUT] == 4


def test_a_landed_passenger_counts_as_served() -> None:
    """A body the fill changed was written, including one no route opened.
    The served stamp is that union. The opened set stays the routes' own."""
    from pipeline_core.domain.landed_change import served_units
    assert served_units([_IN], [_OUT]) == {_IN, _OUT}
    assert _OUT not in opened_in_fill([_IN], [], fallback=[_IN])


def test_a_lost_answer_is_not_service_and_no_report_reads_the_batch() -> None:
    assert _LOST not in opened_in_fill({_LOST, _IN}, [_LOST], fallback=[])
    assert opened_in_fill(None, [], fallback=[_IN]) == {_IN}
