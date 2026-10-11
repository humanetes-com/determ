"""A failing test that stops inside a still-stubbed fn names that fn as the
unit to fill, whether or not its process died.

Fixture: the sweep capture of the iter-3 checkpoint of run
`3453127-045b05b1` (2026-09-19, cluster `stable`, task2a checkpoint commit
`566d06aa3`), re-run through the pipeline's own runner (`run_cargo` over
`CargoCli`, `--test-threads=1`) with test isolation on (the live setting) and
off; backtrace frames trimmed, every outcome, section and abort reading equal
to the untrimmed capture's. The loop dispatched a batch of 12 at iterations 3
and 4 without `src/error.rs::vtable` — the only stub left, where 82 of 141
failing tests stopped — and filled it at iteration 5; the next sweep went
38 → 105. Two owners missed it: the stub-abort reading (it read unfinished
sections and vanished tests, and isolation leaves neither) and the service
order (a stamp from an earlier process put the stub in the served bucket,
behind 20 never-served units).

The locator below is the tree's own answer for each crate panic line (the
release extractor's records of that checkpoint), not a list chosen for the
test.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.domain.attribution import locator_of
from pipeline_core.domain.dispatch_rank import UnitFacts, rank, service_of, take
from pipeline_core.domain.held_stubs import HeldStubs
from pipeline_core.domain.models import FnLocator, FnSite
from pipeline_core.domain.test_outcome import StubHold, outcomes_of, stub_hold_of
from pipeline_core.domain.test_report import stub_abort_reading
from pipeline_core.domain.test_results import parse_binary_results

_FIX = Path(__file__).parent / "fixtures"
_ISOLATED = (_FIX / "stub_hold_sweep_stable_3453127_iter3_isolated.txt").read_text(
    encoding="utf-8")
_SHARED = (_FIX / "stub_hold_sweep_stable_3453127_iter3_shared.txt").read_text(
    encoding="utf-8")

_VTABLE = FnSite("src/error.rs", "vtable", "")
_RECORDED_LINES = {
    ("src/error.rs", 733): _VTABLE,
    ("src/error.rs", 351): FnSite("src/error.rs", "backtrace", "impl Error"),
    ("src/error.rs", 773): FnSite("src/error.rs", "backtrace", "impl ErrorImpl"),
}
_STUBS = frozenset({_VTABLE})  # the extractor's `has_unimplemented_body` set


def _locate(f: str, ln: int) -> FnSite | None:
    return _RECORDED_LINES.get((f, ln))


def _is_stub(s: FnSite) -> bool:
    return s in _STUBS


def test_under_isolation_the_stub_holds_the_tests_that_stop_in_it() -> None:
    hold = stub_hold_of(_ISOLATED, _locate, _is_stub)
    assert hold.blockers == {_VTABLE}
    # 82 names whose first-parsed copy stops at the stub, plus 2 whose copy
    # in ANOTHER binary does (`test_drop`, `test_unsuccessful_downcast`:
    # one name, two failing tests), which the first-section-wins parse hid
    # until 2026-09-19.
    assert len(hold.held_by(_VTABLE)) == 84
    # Every held test is failing and some copy of it first fails at the
    # stub's line.
    oc = outcomes_of(_ISOLATED)
    assert all(not oc[t].passed and any(
        (c.first_file, c.first_line) == ("src/error.rs", 733)
        for c in (oc[t], *oc[t].others)) for t in hold.held_by(_VTABLE))
    assert {t for t in hold.held_by(_VTABLE)
            if (oc[t].first_file, oc[t].first_line) != ("src/error.rs", 733)
            } == {"test_drop", "test_unsuccessful_downcast"}


def test_negative_control_the_binary_reading_is_silent_under_isolation() -> None:
    # Every section closed: the abort reading this replaces has nothing to read.
    assert all(b.completed for b in parse_binary_results(_ISOLATED))
    assert stub_abort_reading(_ISOLATED, _locate).by_binary == {}


def test_premise_the_binary_reading_named_the_stub_without_isolation() -> None:
    r = stub_abort_reading(_SHARED, _locate)
    assert r.blockers == {_VTABLE}


def test_the_shared_process_fallback_still_holds_vanished_tests() -> None:
    # Tests the shared capture has no outcome for are the fallback's input.
    iso, shared = outcomes_of(_ISOLATED), outcomes_of(_SHARED)
    vanished = sorted(set(iso) - set(shared))
    files = {}
    for b in parse_binary_results(_ISOLATED):
        for t in b.passed | b.failed:
            files[t] = b.label
    hold = stub_hold_of(_SHARED, _locate, _is_stub, vanished=vanished,
                        test_files=files)
    assert hold.blockers == {_VTABLE}
    via_fallback = set(hold.held_by(_VTABLE)) & set(vanished)
    assert via_fallback
    # Without the vanished tests only the outcome half speaks.
    alone = stub_hold_of(_SHARED, _locate, _is_stub)
    assert not set(alone.held_by(_VTABLE)) & set(vanished)


def test_a_filled_fn_at_the_first_site_holds_nothing() -> None:
    assert stub_hold_of(_ISOLATED, _locate, lambda s: False).by_test == {}


# --- the batch the sweep would have cut, on the recorded iteration ---

# `phase_c.dispatch.service_order` iter 3 of the same run: the never-served
# units ranked ahead of every served one.
_NEVER_SERVED_ITER3 = [
    "src/error.rs::error", "src/nightly.rs::fmt", "src/ptr.rs::boxed",
    "src/context.rs::source", "src/chain.rs::next", "src/chain.rs::new",
    "src/ptr.rs::new", "src/chain.rs::len", "src/ensure.rs::new",
    "src/chain.rs::next_back", "src/error.rs::drop", "src/chain.rs::size_hint",
    "src/error.rs::as_ref", "src/context.rs::write_str",
    "src/ensure.rs::write_str", "src/ptr.rs::by_ref", "src/ptr.rs::deref",
    "src/ptr.rs::as_ptr", "src/ptr.rs::from_raw", "src/ensure.rs::as_str"]


def _loc(s: str) -> FnLocator:
    f, fn = s.split("::")
    return FnLocator(f, fn)


def _batch(hold: StubHold, hollow: frozenset[FnLocator]) -> list[FnLocator]:
    """The batch the rank takes on the recorded iteration's frontier."""
    stub = locator_of(_VTABLE)
    served_rest = [FnLocator("src/rest.rs", f"f{i}") for i in range(30)]
    # Frontier order before ranking: the stub LAST, the worst position.
    static = [_loc(k) for k in _NEVER_SERVED_ITER3] + served_rest + [stub]
    held = HeldStubs.of(hold)
    served = {stub, *served_rest}  # the stale stamp the floor carried
    facts = {k: UnitFacts(
        static_index=i, held_tests=len(held.tests_held_by(k)),
        service=service_of(k, served=served, flat=(), hollow=hollow))
        for i, k in enumerate(static)}
    ranked = rank(facts)
    return list(take(ranked, held.units, 12, cohort=None, traversed=()).keys)


def test_the_held_stub_opens_the_batch() -> None:
    hold = stub_hold_of(_ISOLATED, _locate, _is_stub)
    batch = _batch(hold, frozenset({locator_of(_VTABLE)}))
    assert batch[0] == locator_of(_VTABLE)


def test_the_hold_opens_the_batch_whatever_the_stale_stamp_says() -> None:
    # The observed hold alone: the floor's stale service stamp reads the stub
    # as served and nothing marks it hollow, and it still leads the batch.
    hold = stub_hold_of(_ISOLATED, _locate, _is_stub)
    batch = _batch(hold, frozenset())
    assert batch[0] == locator_of(_VTABLE)


def test_negative_control_the_recorded_cut() -> None:
    # Today's inputs on iter 3: no hold (isolation silenced it) and the stale
    # stamp read as service — the stub falls behind the 20 never-served.
    batch = _batch(StubHold({}), frozenset())
    assert locator_of(_VTABLE) not in batch
