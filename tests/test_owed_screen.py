"""An owed tier never names a unit the sweep's coverage proves the test
never enters.

Run `3076-051d385a` iter 3 (2026-10-13): a red test's slice was its coverage
owners plus the units attribution named, ranked by reach, and tier 1 was
`[src/chain.rs::len, src/error.rs::as_ref]` on the sweep whose own service
order listed `len` as executed by no failing test. The fill planned a file
run on `len` alone and a per-fn run on it. Computed on that sweep's recorded
owner map and tiers with `screen_unentered`: `len` leaves that test's slice
and tier 1 becomes `[as_ref]`; another test loses a name-attributed `from`.

Asserted on an invented shape: the screen removes a proven-unentered unit
from its test's tier 1, keeps an unmeasured test's slice whole, and never
empties a slice. Negative control: today's reading (no screen) puts the
unentered unit in tier 1.
"""
from __future__ import annotations

from pipeline_core.domain.candidate_tiers import screen_unentered, tiers_of

_SLICE = frozenset({"src/q.rs::narrow", "src/q.rs::wide", "src/r.rs::named"})
#: Reach: `named` is exercised by fewest examples, so it is tier 1 unscreened.
_REACH = {"src/r.rs::named": frozenset({"e1"}),
          "src/q.rs::narrow": frozenset({"e1", "e2"}),
          "src/q.rs::wide": frozenset({"e1", "e2", "e3", "e4"})}


def _entered(test: str, unit: str) -> bool:
    return not (test == "measured_red" and unit == "src/r.rs::named")


def test_a_proven_unentered_unit_leaves_its_tests_tiers() -> None:
    slices = {"measured_red": _SLICE, "unmeasured_red": _SLICE}
    screened, removed = screen_unentered(
        slices, lambda u, t: not _entered(t, u))
    assert removed == {"measured_red": frozenset({"src/r.rs::named"})}
    tiers = tiers_of(screened["measured_red"], _REACH, {})
    assert "src/r.rs::named" not in tiers[0]
    # An unmeasured test (the predicate answers False) keeps its slice.
    assert screened["unmeasured_red"] == _SLICE
    # Negative control: unscreened, the unentered unit is tier 1.
    assert tiers_of(_SLICE, _REACH, {})[0] == {"src/r.rs::named"}


def test_the_screen_never_empties_a_slice() -> None:
    screened, removed = screen_unentered({"t": _SLICE}, lambda _u, _t: True)
    assert screened["t"] == _SLICE and removed == {}
