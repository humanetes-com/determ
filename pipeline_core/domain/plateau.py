"""Plateau-exit decision for the pass_count stall budget.

Two loops terminate on "pass_count has not moved for `max_stalls` charged
sweeps": the per-cluster TDD outer loop (`TDD_MAX_STALLS`, default 3) and the
post-merge fill loop (`POST_MERGE_MAX_STALLS`, default 3). Both compared
`stall >= max_stalls` directly, so a 0 in the environment fired the stop on
the FIRST sweep (`stall >= 0` is vacuously true) — the opposite of what "no
plateau budget" means. This predicate is the one place that owns the
semantics: a non-positive budget disables the plateau exit.

Disabling the plateau exit does not unbound a run: the TDD loop keeps its
iteration backstop (`PHASE_C_TDD_BACKSTOP`) and the flat-score GPU guard
(`domain/flat_score.py`, a process-stop on repeated identical readings), and
the post-merge loop keeps its hard cap.
"""

from __future__ import annotations


def plateau_exit_disabled(max_stalls: int) -> bool:
    """A non-positive stall budget means "never stop on a plateau"."""
    return max_stalls <= 0


def plateau_exit_due(stall: int, max_stalls: int) -> bool:
    """True when a loop's plateau stop should fire this sweep."""
    return not plateau_exit_disabled(max_stalls) and stall >= max_stalls
