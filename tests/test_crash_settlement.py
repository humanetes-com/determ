"""A crash under a write, settled against an unproven body outside it.

Run `1057870-b427a3a2` (2026-10-15): a constructor kept while its tests could
not run handed `NonNull::dangling()` to an accessor; every correct accessor
body segfaulted and was convicted, and the constructor was never offered again
because its tests stopped in the accessor's stub. The shapes here are
invented: a reader `read` (the write), fabricators `make_a` / `make_b` (kept
bodies outside it), all in `src/lib.rs`. The real-tool walk is
`tests/integration/test_isolated_abort_verdicts.py::test_a_crash_under_a_correct_reader_settles_on_its_callers`.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct.crash_settler import (
    STUB_BODY,
    CrashSettler,
    CrashSettlerDeps,
)
from pipeline_core.domain.crash_settlement import (
    SettlementOutcome,
    occurrence_of_line,
    settle_step,
)
from pipeline_core.domain.crash_site import site_from_frames
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.test_results import TestStateReading, parse_binary_results

READ = FnLocator("src/lib.rs", "read")
MAKE_A = FnLocator("src/lib.rs", "make_a")
MAKE_B = FnLocator("src/lib.rs", "make_b")


def _site(test: str, maker: str, maker_line: int, *, traced: bool = True):
    frames = [
        {"func": "crate::read", "file": "src/lib.rs", "line": 3,
         "ptr_args": [[0x8, 16]]},
        {"func": f"crate::{maker}", "file": "src/lib.rs", "line": maker_line,
         "ptr_args": []},
        {"func": f"t::{test}", "file": "tests/t.rs", "line": 1},
    ]
    return site_from_frames("tests/t.rs", test, "SIGSEGV", frames,
                            fault_addr=0x8 if traced else None)


def _never(_u: FnLocator) -> bool:
    return False


# ---- the decision ----

def test_a_culprit_outside_the_write_and_unproven_is_put_back() -> None:
    step = settle_step([_site("t1", "make_a", 7)], {READ}, _never)
    assert step.outcome is SettlementOutcome.STUB
    assert [f.fn_name for f in step.culprits] == ["make_a"]


def test_a_culprit_inside_the_write_convicts_it() -> None:
    step = settle_step([_site("t1", "make_a", 7)], {READ, MAKE_A}, _never)
    assert step.outcome is SettlementOutcome.OWN


def test_a_proven_culprit_is_never_put_back() -> None:
    step = settle_step([_site("t1", "make_a", 7)], {READ},
                       lambda u: u == MAKE_A)
    assert step.outcome is SettlementOutcome.UNSETTLED


def test_negative_control_an_untraced_fault_convicts_the_reader() -> None:
    # Before the trace the culprit is the faulting frame, the write itself.
    step = settle_step([_site("t1", "make_a", 7, traced=False)], {READ},
                       _never)
    assert step.outcome is SettlementOutcome.OWN


def test_no_site_is_no_crash() -> None:
    assert settle_step([], {READ}, _never).outcome is SettlementOutcome.NO_CRASH


def test_a_frame_names_one_occurrence_of_a_shared_name() -> None:
    text = "fn f() {\n    a\n}\nfn f() {\n    b\n}\n"
    spans = [(0, 15), (16, 31)]
    assert occurrence_of_line(spans, text, 2) == 0
    assert occurrence_of_line(spans, text, 5) == 1


# ---- the loop ----

_LIB = """fn read() {
    real
}

fn make_a() {
    bad_a
}

fn make_b() {
    bad_b
}
"""

_PASS = ("     Running tests/t.rs (target/debug/deps/t-1)\n\nrunning 2 tests\n"
         "test other ... ok\ntest t1 ... FAILED\n\ntest result: FAILED. 1 "
         "passed; 1 failed\n")


def _settler(root: Path, readings: list[str], sites_by_round: list[list],
             proven=_never, recorded: list | None = None) -> CrashSettler:
    rounds = iter(sites_by_round)
    suite = iter(readings)

    def _localize(_cap, _exclude):
        return next(rounds, [])
    return CrashSettler(CrashSettlerDeps(
        localize=_localize, run_suite=lambda: next(suite, None),
        is_proven=proven, src_dir=root,
        record_settled=lambda locs, _s: (recorded.append(list(locs))
                                         if recorded is not None else None),
        log=lambda _m: None))


def _tree(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(_LIB)
    return tmp_path


def _baseline() -> TestStateReading:
    return TestStateReading(parse_binary_results(_PASS))


def test_every_caller_the_suite_shows_goes_back_round_by_round(
        tmp_path: Path) -> None:
    root = _tree(tmp_path)
    recorded: list = []
    s = _settler(root, [_PASS, _PASS],
                 [[_site("t1", "make_a", 6)], [_site("t2", "make_b", 10)], []],
                 recorded=recorded)
    got = s.settle("capture", {READ}, _baseline())
    assert got.outcome is SettlementOutcome.SETTLED, got.reason
    assert got.stubbed_locators == (MAKE_A, MAKE_B)
    text = (root / "src" / "lib.rs").read_text()
    assert "real" in text and "bad_a" not in text and "bad_b" not in text
    assert text.count(STUB_BODY) == 2
    # The refutation is recorded once, for the bodies the stack ran.
    assert recorded == [[MAKE_A, MAKE_B]]


def test_a_settlement_that_loses_a_baseline_pass_is_given_back(
        tmp_path: Path) -> None:
    root = _tree(tmp_path)
    lost = _PASS.replace("test other ... ok", "test other ... FAILED")
    s = _settler(root, [lost], [[_site("t1", "make_a", 6)], []])
    got = s.settle("capture", {READ}, _baseline())
    assert got.outcome is SettlementOutcome.UNSETTLED
    assert (root / "src" / "lib.rs").read_text() == _LIB


def test_a_later_round_that_blames_the_write_gives_every_stub_back(
        tmp_path: Path) -> None:
    root = _tree(tmp_path)
    own = site_from_frames("tests/t.rs", "t3", "SIGSEGV", [
        {"func": "crate::read", "file": "src/lib.rs", "line": 2,
         "ptr_args": [[0x1000, 8]]}], fault_addr=0x9999_0000)
    s = _settler(root, [_PASS], [[_site("t1", "make_a", 6)], [own]])
    got = s.settle("capture", {READ}, _baseline())
    assert got.outcome is SettlementOutcome.OWN
    assert (root / "src" / "lib.rs").read_text() == _LIB
