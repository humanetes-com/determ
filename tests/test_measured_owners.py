"""Owner-map admission fires when every attributed-or-escalated unit has
been served (or is already unable to pay).

Run 451330-7e8a7ca9 (2026-08-31, cluster stable, iter 5): the coverage
slice listed error.rs::fmt on all three remaining failing tests and the
loop never dispatched it. Dead never-served product members
(context.rs::write_str, error.rs::erase) must count as exempt-from-service;
without that the fire condition is unsatisfiable on that instance.

Run 9872-51b20f30 (2026-09-01) served the whole cohort from iter 1
(never_served=[]) while latest_by_unit FLAT stayed at 10, so the
grade-unanimity fire emitted zero times. Service fire on those records
admits the never-graded owners; the unanimity fire on the same records
stays silent.
"""
from __future__ import annotations

from pipeline_core.domain.escalation_lifetime import escalate_to_measured_owners
from pipeline_core.domain.models import FnLocator

_ALT = "test_altdebug"
_BT_CAUSE = "test_debug_with_captured_backtrace_and_cause"
_BT_SIMPLE = "test_debug_with_captured_backtrace_simple"


def _loc(s: str) -> FnLocator:
    f, _, n = s.partition("::")
    return FnLocator(f, n)


# Live iter-5 owner map from events.jsonl, run 451330-7e8a7ca9.
_OWNERS: dict[str, frozenset[FnLocator]] = {
    _ALT: frozenset(map(_loc, (
        "src/context.rs::context",
        "src/context.rs::ext_context",
        "src/context.rs::fmt",
        "src/error.rs::construct",
        "src/error.rs::construct_from_std",
        "src/error.rs::context",
        "src/error.rs::drop",
        "src/error.rs::error",
        "src/error.rs::fmt",
        "src/error.rs::from",
        "src/error.rs::object_drop",
        "src/error.rs::object_ref",
        "src/fmt.rs::debug",
        "src/kind.rs::anyhow_kind",
        "src/kind.rs::new",
        "src/ptr.rs::as_ptr",
        "src/ptr.rs::boxed",
        "src/ptr.rs::by_ref",
        "src/ptr.rs::deref",
        "src/ptr.rs::from_raw",
        "src/ptr.rs::new",
    ))),
    _BT_CAUSE: frozenset(map(_loc, (
        "src/chain.rs::new",
        "src/chain.rs::next",
        "src/chain.rs::size_hint",
        "src/context.rs::fmt",
        "src/context.rs::source",
        "src/error.rs::as_ref",
        "src/error.rs::chain",
        "src/error.rs::construct",
        "src/error.rs::context",
        "src/error.rs::drop",
        "src/error.rs::error",
        "src/error.rs::fmt",
        "src/error.rs::msg",
        "src/error.rs::object_drop",
        "src/error.rs::object_ref",
        "src/error.rs::source",
        "src/ptr.rs::as_ptr",
        "src/ptr.rs::boxed",
        "src/ptr.rs::by_ref",
        "src/ptr.rs::deref",
        "src/ptr.rs::from_raw",
        "src/ptr.rs::new",
        "src/wrapper.rs::fmt",
    ))),
    _BT_SIMPLE: frozenset(map(_loc, (
        "src/chain.rs::new",
        "src/chain.rs::next",
        "src/chain.rs::size_hint",
        "src/error.rs::chain",
        "src/error.rs::construct",
        "src/error.rs::drop",
        "src/error.rs::error",
        "src/error.rs::fmt",
        "src/error.rs::msg",
        "src/error.rs::object_drop",
        "src/error.rs::object_ref",
        "src/error.rs::source",
        "src/ptr.rs::as_ptr",
        "src/ptr.rs::boxed",
        "src/ptr.rs::by_ref",
        "src/ptr.rs::deref",
        "src/ptr.rs::from_raw",
        "src/ptr.rs::new",
        "src/wrapper.rs::fmt",
    ))),
}

# Live escalation_sustained additions (28) plus the attributed debug.
_PRODUCT = frozenset(map(_loc, (
    "src/chain.rs::len",
    "src/context.rs::context",
    "src/context.rs::ext_context",
    "src/context.rs::with_context",
    "src/context.rs::write_str",
    "src/ensure.rs::__dispatch_ensure",
    "src/ensure.rs::render",
    "src/ensure.rs::write_str",
    "src/error.rs::as_ref",
    "src/error.rs::construct",
    "src/error.rs::construct_from_adhoc",
    "src/error.rs::construct_from_boxed",
    "src/error.rs::construct_from_context",
    "src/error.rs::construct_from_display",
    "src/error.rs::construct_from_std",
    "src/error.rs::context",
    "src/error.rs::deref",
    "src/error.rs::deref_mut",
    "src/error.rs::downcast",
    "src/error.rs::erase",
    "src/error.rs::from",
    "src/error.rs::from_boxed",
    "src/error.rs::msg",
    "src/error.rs::new",
    "src/fmt.rs::write_str",
    "src/kind.rs::new",
    "src/lib.rs::format_err",
    "src/lib.rs::must_use",
)))
_DEBUG = _loc("src/fmt.rs::debug")
_WRITE_STR = _loc("src/context.rs::write_str")
_ERASE = _loc("src/error.rs::erase")
_FMT = _loc("src/error.rs::fmt")

# Intersection of the three owner lists minus construct (attempted).
_NEVER_GRADED = frozenset(map(_loc, (
    "src/error.rs::drop",
    "src/error.rs::error",
    "src/error.rs::fmt",
    "src/error.rs::object_drop",
    "src/error.rs::object_ref",
    "src/ptr.rs::as_ptr",
    "src/ptr.rs::boxed",
    "src/ptr.rs::by_ref",
    "src/ptr.rs::deref",
    "src/ptr.rs::from_raw",
    "src/ptr.rs::new",
)))


def _units_by_test() -> dict[str, set[FnLocator]]:
    return {
        t: set(_PRODUCT) | ({_DEBUG} if t == _ALT else set())
        for t in _OWNERS
    }


def test_iter5_owner_map_admits_the_never_graded_when_dead_counts() -> None:
    """On 451330's 27-unit product plus debug, with write_str and erase
    dead and never served, the first admission contains all eleven units the
    three tests share and that no landed-change attempted."""
    graded = (_PRODUCT | {_DEBUG}) - {_WRITE_STR, _ERASE}
    unable = {_WRITE_STR, _ERASE}
    already = _PRODUCT | {_DEBUG}
    got = escalate_to_measured_owners(
        owners=_OWNERS,
        units_by_test=_units_by_test(),
        graded=graded,
        unable=unable,
        already_owned=already,
        served=graded,
    )
    assert set(got) >= _NEVER_GRADED
    assert _FMT in got
    assert _OWNERS[_ALT] & _OWNERS[_BT_CAUSE] & _OWNERS[_BT_SIMPLE] - {
        _loc("src/error.rs::construct")} <= set(got)
    assert got[_FMT] == {_ALT, _BT_CAUSE, _BT_SIMPLE}
    assert _DEBUG not in got
    assert _WRITE_STR not in got
    assert _ERASE not in got
    assert _loc("src/error.rs::construct") not in got


def test_without_dead_in_unable_the_fire_does_not_happen() -> None:
    """NEGATIVE CONTROL: unattempted dead product members left unserved
    hold the fire — the as-filed condition that never admitted fmt."""
    graded = (_PRODUCT | {_DEBUG}) - {_WRITE_STR, _ERASE}
    already = _PRODUCT | {_DEBUG}
    got = escalate_to_measured_owners(
        owners=_OWNERS,
        units_by_test=_units_by_test(),
        graded=graded,
        unable=frozenset(),
        already_owned=already,
        served=graded,
    )
    assert got == {}


def test_permanently_exhausted_owners_are_not_re_admitted() -> None:
    graded = (_PRODUCT | {_DEBUG}) - {_WRITE_STR, _ERASE}
    unable = {_WRITE_STR, _ERASE}
    already = _PRODUCT | {_DEBUG}
    got = escalate_to_measured_owners(
        owners=_OWNERS,
        units_by_test=_units_by_test(),
        graded=graded,
        unable=unable,
        already_owned=already,
        permanently_exhausted={_FMT},
        served=graded | unable,
    )
    assert _FMT not in got
    assert set(got) >= _NEVER_GRADED - {_FMT}


def test_permanently_exhausted_cohort_members_do_not_hold_the_fire() -> None:
    """A cap-exhausted unit that is no longer counted as served must not
    freeze owner-map admission the way an unattempted dead member used to."""
    graded = (_PRODUCT | {_DEBUG}) - {_WRITE_STR, _ERASE}
    already = _PRODUCT | {_DEBUG}
    got = escalate_to_measured_owners(
        owners=_OWNERS,
        units_by_test=_units_by_test(),
        graded=graded,
        unable={_ERASE},
        already_owned=already,
        permanently_exhausted={_WRITE_STR},
        served=graded,
    )
    assert _FMT in got
    assert _WRITE_STR not in got


# Run 9872-51b20f30 (2026-09-01) iter 1, computed from events.jsonl:
# phase_c.dispatch.service_order never_served=[], flat=10, dead includes
# context.rs::write_str and error.rs::erase. Owner maps byte-identical to
# the 451330 iter-5 maps above. Grade unanimity cannot fire; service can.
_9872_FLAT = frozenset(map(_loc, (
    "src/error.rs::construct",
    "src/error.rs::construct_from_context",
    "src/error.rs::construct_from_display",
    "src/error.rs::construct_from_std",
    "src/error.rs::context",
    "src/error.rs::downcast",
    "src/error.rs::erase",
    "src/error.rs::from",
    "src/error.rs::msg",
    "src/lib.rs::must_use",
)))
_9872_DEAD_IN_COHORT = {_WRITE_STR, _ERASE}


def test_9872_service_fire_admits_owners_grade_unanimity_does_not() -> None:
    """The live zero-emission: iter 1 already had never_served=[], but
    escalate_to_measured_owners keyed on FLAT and stayed silent. Service
    fire on the same records admits the never-graded measured owners."""
    already = _PRODUCT | {_DEBUG}
    # NEGATIVE CONTROL = the shipped unanimity fire (only FLAT counts as
    # served). 18 cohort members hold it — the live zero-emission.
    silent = escalate_to_measured_owners(
        owners=_OWNERS,
        units_by_test=_units_by_test(),
        graded=_9872_FLAT,
        unable=_9872_DEAD_IN_COHORT,
        already_owned=already,
        served=_9872_FLAT,
    )
    assert silent == {}
    got = escalate_to_measured_owners(
        owners=_OWNERS,
        units_by_test=_units_by_test(),
        graded=_9872_FLAT,
        unable=_9872_DEAD_IN_COHORT,
        already_owned=already,
        served=_PRODUCT | {_DEBUG},
    )
    assert set(got) >= _NEVER_GRADED
    assert _FMT in got
    assert got[_FMT] == {_ALT, _BT_CAUSE, _BT_SIMPLE}
    assert _DEBUG not in got
    assert _WRITE_STR not in got
    assert _ERASE not in got


def test_a_paired_test_does_not_admit_coverage_owners() -> None:
    """A leftover whose attribution already resolved a hop pair does not
    widen to coverage-spine owners after the pair members were served.
    The pair is that test's fill. Control: the same maps without
    paired_tests still admit the spine unit."""
    disp = FnLocator("src/d.rs", "fmt")
    impl = FnLocator("src/i.rs", "debug")
    spine = FnLocator("src/d.rs", "drop")
    owners = {"t_fmt": frozenset({disp, impl, spine})}
    units = {"t_fmt": {disp, impl}}
    served = {disp, impl}
    got = escalate_to_measured_owners(
        owners=owners,
        units_by_test=units,
        graded=frozenset(),
        unable=frozenset(),
        already_owned=served,
        served=served,
        paired_tests={"t_fmt"},
    )
    assert got == {}
    control = escalate_to_measured_owners(
        owners=owners,
        units_by_test=units,
        graded=frozenset(),
        unable=frozenset(),
        already_owned=served,
        served=served,
    )
    assert spine in control
    assert control[spine] == {"t_fmt"}
    assert disp not in control
    assert impl not in control
