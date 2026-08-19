"""Pins for the plateau-exit budget semantics (`domain/plateau.py`).

A non-positive stall budget DISABLES the plateau stop. Before the predicate
existed both exits compared `stall >= max_stalls` raw, so `TDD_MAX_STALLS=0`
(or `POST_MERGE_MAX_STALLS=0`) stopped a cluster on its FIRST non-improving
sweep — the opposite of "no plateau budget".
"""

from pathlib import Path

from pipeline_core.domain.plateau import plateau_exit_disabled, plateau_exit_due

_REPO = Path(__file__).resolve().parents[1]


def test_zero_budget_disables_the_exit_at_any_stall() -> None:
    for stall in (0, 1, 3, 10_000):
        assert not plateau_exit_due(stall, 0)
    assert plateau_exit_disabled(0)


def test_negative_budget_is_disabled_not_instant() -> None:
    for stall in (0, 1, 3, 10_000):
        assert not plateau_exit_due(stall, -1)
    assert plateau_exit_disabled(-1)


def test_positive_budget_fires_at_the_budget_and_not_before() -> None:
    assert not plateau_exit_disabled(3)
    assert not plateau_exit_due(0, 3)
    assert not plateau_exit_due(2, 3)
    assert plateau_exit_due(3, 3)
    assert plateau_exit_due(4, 3)


def test_both_loops_route_their_exit_through_the_predicate() -> None:
    # The raw `stall >= max_stalls` comparison is the defect shape this
    # module exists to remove; both plateau exits must go through
    # plateau_exit_due so 0-disables holds everywhere at once.
    tdd = (_REPO / "pipeline_core/application/reconstruct/"
                   "phase_c_tdd.py").read_text()
    post_merge = (_REPO / "pipeline_core/application/generators/"
                          "task2_generator.py").read_text()
    for src in (tdd, post_merge):
        assert "plateau_exit_due(stall, max_stalls)" in src
        assert "if stall >= max_stalls:" not in src
