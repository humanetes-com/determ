"""How a test that failed before and after a write moved, read at the keep (pure).

WHY. A keep that leaves a test red can still change what the test says, and
the body it wrote is then the body that produced the new observation. On run
`19493-36a4e056` (2026-11-04, cluster `stable`) a session wrote
`impl Debug for BoxedError` as a literal of one world twin's expected string.
The other twin had died with SIGSEGV inside that body, called from line L of
its own test; after the keep it failed at its assertion on line L with the
literal's text. The move was written nowhere: the ledger's delta named only
the gains, the owed walk restarted at tier 0 on the new failure text and asked
the producing body last, and a later session that rewrote it for that test
recorded no ask.

WHAT. The kind of a move between two failing observations of one test in one
binary, decided only from measured positions of the test's own body:

* ``same_statement`` — the statement that judges the test did not move: both
  readings stop at the same assertion position with a different failure
  text, or the earlier reading died in a crash whose first test-source frame
  is the line the later reading asserts at. The written body is on the path
  to the value now judged.
* ``later`` / ``earlier`` — both readings have an assertion position and it
  moved. The same meaning `assertion_position.compare_positions` gives them.

No position, no kind: a reading with no assertion position and no crash
position (a panic inside crate source, an abort with no localized crash)
yields nothing, never a guessed kind. A crash position compares only for
equality: a later assertion than the crash line says the crashing statement
now returns, not which body the new failure is in.

Nothing here names a crate, test, trait or construct.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum

__all__ = ["MoveKind", "collapse_moves", "move_of", "same_statement_tests"]


class MoveKind(StrEnum):
    SAME_STATEMENT = "same_statement"
    LATER = "later"
    EARLIER = "earlier"


def move_of(*, before_position: int | None, before_fingerprint: str,
            before_crash: int | None, after_position: int | None,
            after_fingerprint: str) -> MoveKind | None:
    """The kind of one test's move in one binary, or None.

    `*_position` is the assertion position of the failure in the test's own
    body (`assertion_position`); `*_fingerprint` the identity of its failure
    text; `before_crash` the position, in the same body, of the first
    test-source frame of a crash the earlier reading died in."""
    if after_position is None:
        return None
    if before_position is not None:
        if after_position > before_position:
            return MoveKind.LATER
        if after_position < before_position:
            return MoveKind.EARLIER
        if (before_fingerprint and after_fingerprint
                and before_fingerprint != after_fingerprint):
            return MoveKind.SAME_STATEMENT
        return None
    if before_crash is not None and before_crash == after_position:
        return MoveKind.SAME_STATEMENT
    return None


def collapse_moves(moves: Iterable[tuple[str, MoveKind]]) -> dict[str, str]:
    """`(test, kind)` per binary -> one kind per test name. A test that moved
    earlier in any binary is `earlier`; else `later` in any is `later`; a
    test is `same_statement` only when no binary moved it by position."""
    rank = {MoveKind.EARLIER: 0, MoveKind.LATER: 1, MoveKind.SAME_STATEMENT: 2}
    best: dict[str, MoveKind] = {}
    for test, kind in moves:
        cur = best.get(test)
        if cur is None or rank[kind] < rank[cur]:
            best[test] = kind
    return {t: str(k) for t, k in sorted(best.items())}


def same_statement_tests(moved: Mapping[str, str] | None) -> frozenset[str]:
    """The tests of a recorded move map whose kind is `same_statement`."""
    return frozenset(t for t, k in (moved or {}).items()
                     if k == MoveKind.SAME_STATEMENT)
