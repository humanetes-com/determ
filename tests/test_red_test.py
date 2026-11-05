"""The owner record (`domain/red_test.py`) on the recorded inputs of run
`1604111-bb3716ef` (2026-11-05, cluster `stable`, iter 9).

The two owed tests' binary aborted (`rc=-6`), so coverage listed them as
unmeasured. The positive owner map taken before `withhold_unmeasured` held
13 sites for `test_downcast_low` (the `phase_c.coverage.owner_map` event,
pasted below with the two advanced-past units the sweep had removed put
back). The session slicer read the post-withhold map, where the test is
absent, so its slice was the one attributed site (`drop`); `merge_slices`
took that as primary and the walk owed `[[drop]]`.

The plan's sentence these tests assert: the owed slice and the session
sites of an aborted test contain its positive owners, with the container
the coverage reading carried. Negative control: the pre-record composition
(post-withhold owners into the session slicer, `merge_slices` without the
owner view) gives `{drop}`.
"""
from __future__ import annotations

from pipeline_core.domain.candidate_tiers import AskLedger, due_reading, merge_slices
from pipeline_core.domain.coverage_slice import CoverageSlice, withhold_unmeasured
from pipeline_core.domain.models import FnLocator, FnSite
from pipeline_core.domain.red_test import (
    OwnerSource,
    owners_off_tiers,
    read_red_owners,
)
from pipeline_core.domain.slice_session import (
    expand_locator_sites,
    session_slices_for_failing,
)
from pipeline_core.domain.test_sight import Sight, SightReading, SightSource

T = "test_downcast_low"

# phase_c.coverage.owner_map, iter 9, owners[test_downcast_low] (after
# owning_view), plus the two units premise_advance had taken off it.
_OWNERS_ITER9 = [
    ("src/context.rs", "context", "impl Context<T,E>for Result<T,E>"),
    ("src/context.rs", "ext_context", "impl StdError for E"),
    ("src/context.rs", "ext_context", "impl StdError for Error"),
    ("src/error.rs", "construct", "impl Error"),
    ("src/error.rs", "construct_from_context", "impl Error"),
    ("src/error.rs", "context", "impl Error"),
    ("src/error.rs", "context_downcast", ""),
    ("src/error.rs", "context_drop_rest", ""),
    ("src/error.rs", "drop", "impl Drop for Error"),
    ("src/error.rs", "object_drop", ""),
    ("src/ptr.rs", "as_ptr", "impl Ref<'a,T>"),
    ("src/ptr.rs", "from_raw", "impl Ref<'a,T>"),
    ("src/ptr.rs", "new", "impl Own<T>"),
]
_ADVANCED = frozenset({FnLocator("src/error.rs", "downcast"),
                       FnLocator("src/error.rs", "downcast_ref")})
POSITIVE = {T: frozenset(FnSite(*x) for x in _OWNERS_ITER9)
            | frozenset({FnSite("src/error.rs", "downcast", "impl Error"),
                         FnSite("src/error.rs", "downcast_ref", "impl Error")})}
DROP = FnSite("src/error.rs", "drop", "impl Drop for Error")
ATTRIBUTED_SITES = {T: frozenset({DROP})}
SEEN = SightReading({T: Sight(T, frozenset({SightSource.MEASURED}),
                              frozenset(POSITIVE[T]))})


def _record():
    return read_red_owners(
        positive=POSITIVE, recorded={}, owed=[T],
        advanced={T: _ADVANCED}, owned_elsewhere=[T],
        frontier_sites=[DROP])


def test_an_aborted_tests_session_sites_carry_its_positive_owners() -> None:
    rec = _record()
    sites = rec.session_owners()[T]
    assert FnSite("src/error.rs", "context_drop_rest", "") in sites
    assert FnSite("src/error.rs", "context_downcast", "") in sites
    assert FnSite("src/ptr.rs", "from_raw", "impl Ref<'a,T>") in sites
    # owning_view: the advanced-past units no longer own the test.
    assert not {s.fn_name for s in sites} & {"downcast", "downcast_ref"}
    assert rec.by_test[T].source is OwnerSource.POSITIVE
    slices = session_slices_for_failing(
        [T], owners=rec.session_owners(), units_by_test=ATTRIBUTED_SITES)
    assert len(slices[T]) == 13


def test_the_owed_slice_of_an_aborted_test_contains_its_owners() -> None:
    rec = _record()
    primary = {T: frozenset({"src/error.rs::drop"})}
    sl = merge_slices(primary, {}, [T], sight=SEEN, owners=rec.walk_owners())
    assert "src/error.rs::context_drop_rest" in sl[T]
    assert len(sl[T]) == 12  # 13 sites, `ext_context` twice as one key
    reading = due_reading([T], slices=sl, reach={}, evidence={T: "x"},
                          held={}, ledger=AskLedger())
    on = {u for tier in reading.by_test[T].tiers for u in tier}
    assert "src/error.rs::context_drop_rest" in on
    assert owners_off_tiers(rec.walk_owners(),
                            {T: reading.by_test[T].tiers}) == {}


def test_negative_control_the_pre_record_composition_owes_drop_alone() -> None:
    reading = withhold_unmeasured(CoverageSlice(
        owners=POSITIVE, unmeasured_tests=frozenset({T})))[0]
    assert T not in reading.owners
    old_session = session_slices_for_failing(
        [T], owners=dict(reading.owners), units_by_test=ATTRIBUTED_SITES)
    assert old_session[T] == {DROP}
    primary = {T: frozenset({"src/error.rs::drop"})}
    old = merge_slices(primary, {}, [T], sight=SEEN)
    assert old == {T: frozenset({"src/error.rs::drop"})}
    rec = _record()
    off = owners_off_tiers(rec.walk_owners(), {T: [old[T]]})
    assert "src/error.rs::context_drop_rest" in off[T]


def test_a_withheld_pair_test_takes_no_owner_in_the_walk() -> None:
    rec = _record()
    assert T not in rec.walk_owners(exclude=[f"tests/x.rs::{T}"])


def test_a_recorded_homonym_stays_out_of_the_session_and_in_the_walk() -> None:
    """A recorded `(file, fn)` with two declarations and no frontier site
    is not a session site (an empty container would be expanded into every
    same-named body, run `110993-6efa23be`), and stays a walk key."""
    rec = read_red_owners(
        positive={}, recorded={"t": frozenset({FnLocator("src/fmt.rs", "fmt"),
                                               FnLocator("src/a.rs", "free")})},
        owed=["t"], advanced={},
        frontier_sites=[FnSite("src/a.rs", "free", "impl A")])
    assert rec.by_test["t"].source is OwnerSource.RECORDED
    assert rec.session_owners()["t"] == {FnSite("src/a.rs", "free", "impl A")}
    assert rec.walk_owners()["t"] == {"src/fmt.rs::fmt", "src/a.rs::free"}
    tree = [FnSite("src/fmt.rs", "fmt", "impl Debug for E"),
            FnSite("src/fmt.rs", "fmt", "impl Display for E"),
            FnSite("src/a.rs", "free", "impl A")]
    expanded = expand_locator_sites(rec.session_owners(), tree)
    assert expanded["t"] == {FnSite("src/a.rs", "free", "impl A")}


def test_a_positive_free_fn_site_survives_the_sessions_expand_unchanged() -> None:
    rec = _record()
    tree = [FnSite("src/error.rs", "context_drop_rest", ""),
            FnSite("src/error.rs", "drop", "impl Drop for Error")]
    expanded = expand_locator_sites(rec.session_owners(), tree)
    assert FnSite("src/error.rs", "context_drop_rest", "") in expanded[T]


def test_a_recorded_set_stands_in_only_where_no_positive_owner_exists() -> None:
    rec = read_red_owners(
        positive=POSITIVE, recorded={T: frozenset({FnLocator("src/z.rs", "z")})},
        owed=[T], advanced={})
    assert "src/z.rs::z" not in rec.walk_owners()[T]
    assert rec.by_test[T].source is OwnerSource.POSITIVE
