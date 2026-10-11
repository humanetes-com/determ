"""The held view (`domain/held_stubs`): the one answer to "which stub stops
this test", and the consumers that read it.

Each claim is the plan's sentence; the controls are the readings the view
replaced (the slice intersection, the attribution deferral, the schedule's
"any opened run with the stub in its targets")."""
from __future__ import annotations

from pipeline_core.domain.held_stubs import HeldStubs
from pipeline_core.domain.models import FnLocator, FnSite
from pipeline_core.domain.slice_session import plan_slices
from pipeline_core.domain.test_outcome import StubHold

PROD = FnLocator("src/error.rs", "context")   # still-stubbed producer
CONS = FnLocator("src/lib.rs", "wrap")        # filled consumer of PROD
DROP = FnLocator("src/error.rs", "object_drop")


def test_the_view_is_keyed_by_bare_test_and_dispatch_locator() -> None:
    hold = StubHold({"tests/test_drop.rs::test_drop": frozenset({
        FnSite("src/error.rs", "context", "impl Error")})})
    held = HeldStubs.of(hold)
    assert held.by_test == {"test_drop": frozenset({PROD})}
    assert held.units == frozenset({PROD})
    assert held.holds("tests::test_drop") and not held.holds("test_other")
    assert held.tests_held_by(PROD) == frozenset({"test_drop"})


def test_a_pure_consumer_is_deferred_behind_its_holder() -> None:
    held = HeldStubs({"t_len": frozenset({PROD})})
    active, deferred = held.consumer_deferrals(
        {PROD: {"t_len"}, CONS: {"t_len"}})
    assert PROD in active and CONS not in active
    assert deferred == {CONS: frozenset({PROD})}


def test_a_consumer_with_a_test_no_stub_holds_stays_active() -> None:
    held = HeldStubs({"t_len": frozenset({PROD})})
    active, deferred = held.consumer_deferrals(
        {PROD: {"t_len"}, CONS: {"t_len", "t_own"}})
    assert CONS in active and not deferred


def test_the_holder_is_never_deferred_on_its_own_tests() -> None:
    held = HeldStubs({"t_len": frozenset({PROD})})
    active, deferred = held.consumer_deferrals({PROD: {"t_len"}})
    assert active == {PROD: {"t_len"}} and not deferred


def test_the_consumer_comes_back_the_sweep_nothing_holds_its_test() -> None:
    active, deferred = HeldStubs().consumer_deferrals(
        {PROD: {"t_len"}, CONS: {"t_len"}})
    assert CONS in active and not deferred


def test_a_test_attributed_to_a_stub_but_failing_elsewhere_defers_nothing() -> None:
    """Control: the attribution deferral deferred CONS whenever every test it
    carried was ALSO attributed to a still-stubbed unit. Here `t_len`'s first
    failure is CONS's own assertion; attribution still names PROD. The
    observed holds name nothing, so CONS is dispatched."""
    active, deferred = HeldStubs().consumer_deferrals(
        {PROD: {"t_len"}, CONS: {"t_len"}})
    assert CONS in active and PROD in active and not deferred


def test_a_stub_run_during_unwinding_holds_nothing() -> None:
    """The plan's unwinding case: `test_x` first fails in stub A, and its
    coverage slice also ran stub B, a drop during unwinding. Held by A only.
    Control: a slice intersection (what `hollow_by_test` computed) names both."""
    hold = StubHold({"test_x": frozenset({FnSite("src/error.rs", "context", "")})})
    held = HeldStubs.of(hold)
    assert held.by_test == {"test_x": frozenset({PROD})}
    slice_ = {FnSite("src/error.rs", "context", ""),
              FnSite("src/error.rs", "object_drop", "")}
    stubs = {PROD, DROP}
    slice_reading = {FnLocator(s.file, s.fn_name) for s in slice_} & stubs
    assert slice_reading == {PROD, DROP}


def test_a_session_writes_a_held_stub_only_when_it_aims_at_a_test_it_holds() -> None:
    """A session's targets are its editable set. One that has a held stub as
    a passenger of another test's slice is not asked for that body, so it is
    not the stub's writer; a session aimed at a held test is, and a unit
    route writes what it targets."""
    source = FnLocator("src/wrapper.rs", "source")
    held = HeldStubs({"boxed_error_display_is_forwarded": frozenset({source})})
    editable = frozenset({source, CONS})
    assert not held.writes(source, targets=editable,
                           aimed_tests={"test_debug_alternate"}, session=True)
    assert held.writes(source, targets=editable,
                       aimed_tests={"boxed_error_display_is_forwarded"},
                       session=True)
    assert held.writes(source, targets={source}, aimed_tests=(), session=False)
    assert not held.writes(source, targets={CONS}, aimed_tests=(), session=False)


def test_the_session_planner_leaves_held_tests_and_lists_held_units_first() -> None:
    """A held test is left to the unit routes; a test whose slice merely
    passes through a stub is planned. Control: the slice reading dropped
    both."""
    a = FnSite("src/a.rs", "a", "")
    hub = FnSite("src/error.rs", "context", "")
    slices = {"t_held": {a, hub}, "t_through": {a, hub}}
    plans = plan_slices(slices, held_tests={"t_held"}, held_units={PROD})
    assert [p.tests for p in plans] == [("t_through",)]
    assert plans[0].units[0] == hub
