"""A binary that aborts on stub panics alone names the stubs that must fill.

Fixture: the sweep capture of cluster `nightly` in run `119837-09e3ac57`
(2026-09-15), reproduced on a copy of that tree with the sweep's own command
(`cargo +nightly test --no-fail-fast -- --test-threads=1`, `--cfg
error_generic_member_access`), backtrace frames and the build's warnings
trimmed; every per-binary reading equals the untrimmed capture's. Twenty
binaries aborted. The loop forgave the drop "until the stubs fill" and then
dispatched none of them: the crash cohort only knew localizer blame (none —
a double panic is not a native fault), and the schedule parked any run
aimed at those binaries' tests behind a pair and graded it an unmeasurable
re-buy.

The locator below is the recorded tree's own answer for each panic line (the
extractor's records at the capture), not a list chosen for the test.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.domain.attribution import locator_of
from pipeline_core.domain.dispatch_batch import (
    CrashCohort,
    promote_crash_evidence,
    take_batch,
)
from pipeline_core.domain.fill_schedule import (
    RouteKind,
    RouteRun,
    run_blocks,
    run_is_unmeasurable,
)
from pipeline_core.domain.models import FnLocator, FnSite
from pipeline_core.domain.test_report import StubAbortReading, stub_abort_reading

_CAPTURE = (Path(__file__).parent / "fixtures"
            / "stub_abort_sweep_nightly_119837_2026-09-15.txt").read_text(encoding="utf-8")

_RECORDED_LINES = {
    ("src/error.rs", 660): FnSite("src/error.rs", "object_drop", ""),
    ("src/error.rs", 669): FnSite("src/error.rs", "object_ref", ""),
    ("src/error.rs", 779): FnSite("src/error.rs", "error_mut", "impl ErrorImpl"),
    ("src/error.rs", 850): FnSite(
        "src/error.rs", "as_ref", "impl AsRef < dyn StdError + Send + Sync > for Error"),
    ("src/nightly.rs", 35): FnSite("src/nightly.rs", "request_ref", ""),
}


def _locate(f: str, ln: int) -> FnSite | None:
    return _RECORDED_LINES.get((f, ln))


def test_the_recorded_aborts_name_the_four_drop_path_stubs() -> None:
    r = stub_abort_reading(_CAPTURE, _locate)
    assert len(r.by_binary) == 20
    assert {(s.file, s.fn_name) for s in r.blockers} == {
        ("src/error.rs", "object_drop"), ("src/error.rs", "object_ref"),
        ("src/error.rs", "error_mut"), ("src/error.rs", "as_ref")}
    # The nightly request helper panics only in binaries that COMPLETED: an
    # ordinary stub failure attribution already owns, never an abort blocker.
    assert all(s.fn_name != "request_ref" for s in r.blockers)


def test_an_unlocatable_site_leaves_that_binary_out() -> None:
    def partial(f: str, ln: int) -> FnSite | None:
        return None if ln == 850 else _locate(f, ln)
    r = stub_abort_reading(_CAPTURE, partial)
    assert "tests/test_boxed.rs" not in r.by_binary
    assert "tests/context_gen_3.rs" in r.by_binary


_RUSTC = "/rustc/0000/library/core/src/panicking.rs:233:5"
_STABLE_245 = (
    # The stable shape (run `2585062-494e630e`): one binary aborts on a
    # stub panic plus the runtime's destructor line, one aborts after an
    # ASSERTION, one completes with a stub failure of its own.
    "running 3 tests\n"
    "test ctx_a ... \n"
    "thread 'ctx_a' panicked at src/error.rs:669:67:\n"
    "not implemented\n"
    "\n"
    "running 2 tests\n"
    "test eg_a ... \n"
    "thread 'eg_a' panicked at tests/error_gen_3.rs:50:5:\n"
    "assertion failed: inner.is_some()\n"
    "\n"
    "running 2 tests\n"
    "test lib_a ... ok\n"
    "test lib_b ... FAILED\n"
    "\n"
    "failures:\n"
    "\n"
    "---- lib_b stdout ----\n"
    "thread 'lib_b' panicked at src/fmt.rs:8:5:\n"
    "not implemented\n"
    "\n"
    "test result: FAILED. 1 passed; 1 failed; 0 ignored\n"
    "\n--- stderr ---\n"
    "     Running tests/context_gen_1.rs (target/debug/deps/context_gen_1-aaaa)\n"
    "\n"
    f"thread 'ctx_a' panicked at {_RUSTC}:\n"
    "panic in a destructor during cleanup\n"
    "thread caused non-unwinding panic. aborting.\n"
    "     Running tests/error_gen_3.rs (target/debug/deps/error_gen_3-bbbb)\n"
    "\n"
    f"thread 'eg_a' panicked at {_RUSTC}:\n"
    "panic in a destructor during cleanup\n"
    "thread caused non-unwinding panic. aborting.\n"
    "     Running tests/lib_gen_1.rs (target/debug/deps/lib_gen_1-cccc)\n"
)


def test_an_assertion_beside_the_abort_is_the_body_speaking() -> None:
    sites = {("src/error.rs", 669): FnSite("src/error.rs", "vtable", ""),
             ("src/fmt.rs", 8): FnSite("src/fmt.rs", "display", "")}
    r = stub_abort_reading(_STABLE_245, lambda f, ln: sites.get((f, ln)))
    assert set(r.by_binary) == {"tests/context_gen_1.rs"}
    assert r.blockers == {FnSite("src/error.rs", "vtable", "")}


def test_hollow_places_tests_by_their_binary() -> None:
    drop = FnSite("src/error.rs", "object_drop", "")
    ref = FnSite("src/error.rs", "object_ref", "")
    r = StubAbortReading({"tests/a.rs": frozenset({drop}),
                          "unittests src/lib.rs": frozenset({ref})})
    files = {"t_int": "tests/a.rs", "t_lib": "src/error.gen_tests_1.rs",
             "t_other": "tests/b.rs"}
    assert r.hollow(["t_int", "t_lib", "t_other", "t_unplaced"], files) == {
        "t_int": frozenset({drop}), "t_lib": frozenset({ref})}


# --- what the reading changes downstream, on the recorded iteration ---

_LOGGED_BATCH = [  # iter 1's `12 batch unit(s)` of the same run, in order
    "src/context.rs::context", "src/context.rs::ext_context",
    "src/context.rs::with_context", "src/error.rs::construct_from_adhoc",
    "src/error.rs::construct_from_context", "src/error.rs::construct_from_display",
    "src/error.rs::construct_from_std", "src/error.rs::context", "src/error.rs::from",
    "src/error.rs::msg", "src/error.rs::new", "src/kind.rs::new"]


def _loc(s: str) -> FnLocator:
    f, fn = s.split("::")
    return FnLocator(f, fn)


def _batch(sited: frozenset[FnLocator], blockers: frozenset[FnLocator]) -> list[FnLocator]:
    # The recorded batch first, the rest of the 75-unit frontier behind it, the
    # blockers LAST — the worst position the order could have left them in.
    ordered = ([(_loc(k), set()) for k in _LOGGED_BATCH]
               + [(FnLocator("src/rest.rs", f"f{i}"), set()) for i in range(59)]
               + [(b, set()) for b in sorted(blockers)])
    cohort = CrashCohort(blamed=frozenset(), sited=sited, chain=frozenset())
    o = promote_crash_evidence(ordered, cohort.grades)
    return list(take_batch([k for k, _ in o], set(), 12,
                           cohort=tuple(k for k, _ in o),
                           exempt=cohort.cursor_exempt).keys)


def test_the_blockers_make_the_first_batch_and_today_they_do_not() -> None:
    r = stub_abort_reading(_CAPTURE, _locate)
    blockers = frozenset(locator_of(s) for s in r.blockers)
    assert blockers <= set(_batch(blockers, blockers))
    # Negative control: no site evidence, the batch the log recorded.
    assert _batch(frozenset(), blockers) == [_loc(k) for k in _LOGGED_BATCH]


def test_a_run_holding_the_blockers_is_neither_parked_nor_a_rebuy() -> None:
    r = stub_abort_reading(_CAPTURE, _locate)
    blockers = frozenset(locator_of(s) for s in r.blockers)
    aimed = frozenset({"t1", "t2", "t3"})
    hollow = dict.fromkeys(aimed, blockers)
    pair = RouteRun(RouteKind.PAIR, "pair", frozenset(map(_loc, [
        "src/error.rs::context_backtrace", "src/error.rs::fmt",
        "src/error.rs::no_backtrace", "src/fmt.rs::debug"])), target_tests=aimed)
    stub_run = RouteRun(RouteKind.PER_FN, "per-fn", blockers, target_tests=aimed,
                        stubs_in_targets=blockers)
    assert not run_blocks(pair, stub_run, hollow, blockers)
    assert not run_is_unmeasurable(stub_run, aimed, blockers)
    # Negative control: the inputs the schedule had — no hollow relation for an
    # aborted binary, no crash-named locator.
    assert run_blocks(pair, stub_run, {}, frozenset())
    assert run_is_unmeasurable(stub_run, aimed, frozenset())
