"""One reading of "a body landed this fill" (`landed_change.written_units`).

Reviewed 2026-10-28: the served stamp and the starvation streak read the
raw body move, so a unit a crash settlement put back to a stub, with no
route opening it, was stamped served and had its streak reset; the
post-fill ledger writer wrote a row grading the stub. Only the wave ledger
writer dropped such units. The claim: a unit whose body moved to a stub is
not landed, for every reader, and a unit whose body moved to a body is.
Control: the raw move (`changed_units`), which the readers used before,
counts the restubbed unit."""
from __future__ import annotations

from pipeline_core.domain.landed_change import (
    changed_units,
    served_units,
    written_units,
)
from pipeline_core.domain.models import FnLocator

_FILLED = FnLocator("src/a.rs", "filled")
_RESTUBBED = FnLocator("src/a.rs", "restubbed")
_SAME = FnLocator("src/a.rs", "same")

_BEFORE = {_FILLED: ("unimplemented!()",),
           _RESTUBBED: ("self.inner.len()",),
           _SAME: ("1",)}
_AFTER = {_FILLED: ("self.inner.is_empty()",),
          _RESTUBBED: ("unimplemented!()",),
          _SAME: ("1",)}


def test_a_unit_put_back_to_a_stub_landed_nothing() -> None:
    assert written_units(_BEFORE, _AFTER) == {_FILLED}
    # Served is what opened plus what landed: nothing opened here.
    assert served_units((), written_units(_BEFORE, _AFTER)) == {_FILLED}


def test_control_the_raw_move_counts_the_restubbed_unit() -> None:
    assert changed_units(_BEFORE, _AFTER) == {_FILLED, _RESTUBBED}
    assert _RESTUBBED in served_units((), changed_units(_BEFORE, _AFTER))
