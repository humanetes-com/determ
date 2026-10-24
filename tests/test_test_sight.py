"""Whether each owed red test was seen this sweep (`domain/test_sight.py`).

Run `1947461-b4eb65c3` (2026-10-24, cluster `stable` judged in `stable` and
`nightly`): from iter 13 the last owed red test, `doc_error_example_6`, was
built only under the nightly cfg, the owner probe ran in the home world, and
the test had no observation at all. The iteration line printed `unowned=0`
because a recently-filled guess counted as an owner, and fills 13 and 14
spent every run on units off the failing path. The shapes below are invented;
none names that crate.
"""
from __future__ import annotations

from pipeline_core.domain.coverage_slice import CoverageSlice
from pipeline_core.domain.crash_site import CrashFrame, CrashSite
from pipeline_core.domain.models import FnLocator, FnSite
from pipeline_core.domain.test_sight import (
    Observations,
    SightSource,
    UnseenReason,
    sight_reading,
)

_A = FnSite("src/a.rs", "alpha", "impl A")
_B = FnSite("src/b.rs", "beta", "")


def _obs(*, coverage: CoverageSlice | None = None, recorded=None,  # type: ignore[no-untyped-def]
         crash=(), sites=None, pairs=(), home: str = "home") -> Observations:
    return Observations.of(
        coverage=coverage, positive_owners=None, recorded=recorded or {},
        crash_sites=crash, failure_sites=sites or {}, failed_pairs=pairs,
        home=home)


def _crash(test: str, frames: tuple[CrashFrame, ...] = ()) -> CrashSite:
    return CrashSite(binary_label="tests/t.rs", test_name=test,
                     signal="SIGSEGV", frames=frames)


def _frame(file: str, fn: str) -> CrashFrame:
    return CrashFrame(file_rel=file, line=3, fn_name=fn, symbol=fn)


def test_measured_owners_make_a_test_seen_with_those_units() -> None:
    cov = CoverageSlice(owners={"t": frozenset({_A})}, failing=frozenset({"t"}),
                        worlds=frozenset({"home"}))
    s = sight_reading(["t"], _obs(coverage=cov)).by_test["t"]
    assert s.seen and s.sources == {SightSource.MEASURED} and s.units == {_A}


def test_a_test_failed_only_in_a_world_the_probe_did_not_run() -> None:
    """The class of the run above: the reading names the gap, not a guess."""
    cov = CoverageSlice(failing=frozenset({"t"}), worlds=frozenset({"home"}))
    obs = _obs(coverage=cov, pairs=[("other::tests/g.rs", "t")])
    s = sight_reading(["t"], obs).by_test["t"]
    assert not s.seen
    assert s.reason is UnseenReason.NOT_BUILT_IN_PROBED_WORLD
    assert "other" in s.detail and "home" in s.detail


def test_probed_where_it_failed_but_no_binary_claimed_it_is_unclaimed() -> None:
    cov = CoverageSlice(failing=frozenset({"t"}), worlds=frozenset({"home"}))
    s = sight_reading(["t"], _obs(coverage=cov,
                                  pairs=[("tests/g.rs", "t")])).by_test["t"]
    assert s.reason is UnseenReason.UNCLAIMED


def test_no_coverage_reading_is_probe_unavailable() -> None:
    s = sight_reading(["t"], _obs()).by_test["t"]
    assert s.reason is UnseenReason.PROBE_UNAVAILABLE


def test_measured_and_entered_no_body() -> None:
    cov = CoverageSlice(owners={"t": frozenset()}, failing=frozenset({"t"}),
                        worlds=frozenset({"home"}))
    s = sight_reading(["t"], _obs(coverage=cov)).by_test["t"]
    assert s.reason is UnseenReason.ENTERS_NO_BODY


def test_a_crash_with_no_crate_frame_is_unseen_and_named() -> None:
    cov = CoverageSlice(failing=frozenset({"t"}), unmeasured_tests=frozenset({"t"}),
                        worlds=frozenset({"home"}))
    s = sight_reading(["t"], _obs(coverage=cov, crash=[_crash("t")])).by_test["t"]
    assert s.reason is UnseenReason.CRASH_NO_CRATE_FRAME


def test_every_crash_site_is_read_not_the_first() -> None:
    """A first site that names nobody does not make the test unseen when a
    later site of the same test has a crate frame."""
    cov = CoverageSlice(failing=frozenset({"t"}), unmeasured_tests=frozenset({"t"}),
                        worlds=frozenset({"home"}))
    obs = _obs(coverage=cov, crash=[
        _crash("t"), _crash("t", (_frame("src/b.rs", "beta"),))])
    s = sight_reading(["t"], obs).by_test["t"]
    assert s.sources == {SightSource.CRASH_STACK}
    assert FnSite("src/b.rs", "beta", "") in s.units


def test_a_run_that_died_with_nothing_recorded() -> None:
    cov = CoverageSlice(failing=frozenset({"t"}), unmeasured_tests=frozenset({"t"}),
                        worlds=frozenset({"home"}))
    s = sight_reading(["t"], _obs(coverage=cov)).by_test["t"]
    assert s.reason is UnseenReason.DID_NOT_COMPLETE_NO_RECORD


def test_a_record_stands_in_only_for_a_run_that_started() -> None:
    """RECORDED is the store's set for a run that started and did not
    complete. A test no probed binary claimed did not start, and its old
    record is not an observation of this tree."""
    rec = {"t": frozenset({FnLocator("src/a.rs", "alpha")})}
    died = CoverageSlice(failing=frozenset({"t"}), unmeasured_tests=frozenset({"t"}),
                         worlds=frozenset({"home"}))
    s = sight_reading(["t"], _obs(coverage=died, recorded=rec)).by_test["t"]
    assert s.sources == {SightSource.RECORDED}
    unclaimed = CoverageSlice(failing=frozenset({"t"}), worlds=frozenset({"home"}))
    s2 = sight_reading(["t"], _obs(coverage=unclaimed, recorded=rec)).by_test["t"]
    assert not s2.seen and s2.reason is UnseenReason.UNCLAIMED


def test_a_first_failure_inside_a_crate_body_is_seen() -> None:
    s = sight_reading(["t"], _obs(sites={"t": frozenset({_B})})).by_test["t"]
    assert s.sources == {SightSource.FAILURE_SITE} and s.units == {_B}


def test_the_frontier_edges_of_an_unseen_test_leave_and_a_seen_tests_stay() -> None:
    """An unseen test's edges leave. A seen test keeps the edge whose unit
    the sight names (same file and function), and a stub with no test edge
    stays. Negative control: sight not taken removes nothing."""
    cov = CoverageSlice(owners={"seen": frozenset({_A})},
                        failing=frozenset({"seen", "blind"}),
                        worlds=frozenset({"home"}))
    reading = sight_reading(["seen", "blind"], _obs(coverage=cov))
    assert reading.unseen == {"blind"} and not reading.all_unseen
    guess, shared, stub = (FnLocator("src/c.rs", "g"), FnLocator("src/a.rs", "alpha"),
                           FnLocator("src/d.rs", "s"))
    frontier = {guess: {"blind"}, shared: {"seen", "blind"}, stub: set()}
    assert reading.edges_without_unseen(frontier) == {shared: {"seen"}, stub: set()}
    not_taken = sight_reading(["seen", "blind"], _obs(coverage=cov), taken=False)
    assert not_taken.edges_without_unseen(frontier) == frontier
    assert not not_taken.unseen and not not_taken.all_unseen


def test_a_seen_test_drops_a_unit_the_sight_did_not_observe() -> None:
    """A guess aimed at a test this sweep did see is not dispatched. The
    unit the sight names stays, and is added when the frontier did not
    already hold it. ``keep`` preserves one more edge and does not save
    an unseen test's edge. A path that differs only by a leading ``src/``
    reuses the key. Sight not taken ignores ``keep`` and adds nothing."""
    cov = CoverageSlice(owners={"seen": frozenset({_A})},
                        failing=frozenset({"seen", "blind"}),
                        worlds=frozenset({"home"}))
    reading = sight_reading(["seen", "blind"], _obs(coverage=cov))
    guess = FnLocator("src/c.rs", "g")
    shared = FnLocator("src/a.rs", "alpha")
    extra = FnLocator("src/e.rs", "extra")
    stub = FnLocator("src/d.rs", "s")
    frontier = {
        guess: {"seen", "blind"},
        shared: {"seen", "blind"},
        extra: {"seen"},
        stub: set(),
    }
    out = reading.edges_without_unseen(
        frontier, keep=lambda loc, _t: loc == extra)
    assert "blind" not in {t for ts in out.values() for t in ts}
    assert guess not in out
    assert out[shared] == {"seen"}
    assert out[extra] == {"seen"}
    assert out[stub] == set()
    lone = reading.edges_without_unseen({guess: {"seen"}})
    assert lone == {FnLocator("src/a.rs", "alpha"): {"seen"}}
    bare = FnSite("a.rs", "alpha", "impl A")
    cov2 = CoverageSlice(owners={"seen": frozenset({bare})},
                         failing=frozenset({"seen"}),
                         worlds=frozenset({"home"}))
    reading2 = sight_reading(["seen"], _obs(coverage=cov2))
    reused = reading2.edges_without_unseen(
        {shared: {"seen"}, guess: {"seen"}})
    assert set(reused) == {shared} and reused[shared] == {"seen"}
    not_taken = sight_reading(["seen", "blind"], _obs(coverage=cov), taken=False)
    assert not_taken.edges_without_unseen(
        frontier, keep=lambda *_a: False) == frontier


def test_all_unseen_is_the_stop_condition_and_needs_an_owed_test() -> None:
    cov = CoverageSlice(failing=frozenset({"t"}), worlds=frozenset({"home"}))
    obs = _obs(coverage=cov, pairs=[("other::tests/g.rs", "t")])
    assert sight_reading(["t"], obs).all_unseen
    assert not sight_reading([], obs).all_unseen
    assert not sight_reading(["t"], obs, taken=False).all_unseen


def test_a_nobody_crash_beside_a_failure_site_is_the_mixed_case_not_a_stop() -> None:
    """One test crashed with no crate frame, another first failed inside a
    crate body: the first is unseen and named, the second keeps the loop
    going, and the stop's condition is not met."""
    cov = CoverageSlice(failing=frozenset({"blind", "held"}),
                        unmeasured_tests=frozenset({"blind"}),
                        owners={"held": frozenset()},
                        worlds=frozenset({"home"}))
    reading = sight_reading(["blind", "held"], _obs(
        coverage=cov, crash=[_crash("blind")],
        sites={"held": frozenset({_B})}))
    assert reading.unseen == {"blind"}
    assert reading.by_test["blind"].reason is UnseenReason.CRASH_NO_CRATE_FRAME
    assert reading.by_test["held"].sources == {SightSource.FAILURE_SITE}
    assert not reading.all_unseen
