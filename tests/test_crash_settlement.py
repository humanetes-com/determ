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
    JudgedWrite,
    bind,
    withdraw_crashing_bodies,
)
from pipeline_core.domain.crash_settlement import (
    SettlementOutcome,
    held_stub_evidence,
    occurrence_of_frame,
    occurrence_of_line,
    settle_step,
)
from pipeline_core.domain.crash_site import CrashFrame, CrashSite, site_from_frames
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
    # A recovered frame has no line. One occurrence is that body; a shared
    # name stays unresolved. A positive line is unchanged.
    assert occurrence_of_frame([(0, 15)], "fn f() {\n    a\n}\n", 0) == 0
    assert occurrence_of_frame(spans, text, 0) is None
    assert occurrence_of_frame(spans, text, 2) == 0
    assert occurrence_of_frame(spans, text, 5) == 1


def _line0(fn: str) -> CrashSite:
    return CrashSite(
        binary_label="tests/t.rs", test_name="t1", signal="SIGSEGV",
        frames=(CrashFrame("src/lib.rs", 2, "read", "read"),),
        origin_frames=(CrashFrame("src/lib.rs", 0, fn, fn),),
        origin_capable=True)


def test_a_recovered_frame_stubs_the_only_occurrence(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    s = _settler(root, [_PASS], [[_line0("make_a")], []])
    got = s.settle("capture", {READ}, _baseline())
    assert got.outcome is SettlementOutcome.SETTLED, got.reason
    text = (root / "src" / "lib.rs").read_text()
    assert "bad_a" not in text and "bad_b" in text and "real" in text


def test_a_recovered_frame_of_a_shared_name_is_not_put_back(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    path = root / "src" / "lib.rs"
    path.write_text(path.read_text() + "\nfn make_a() {\n    other\n}\n")
    before = path.read_text()
    s = _settler(root, [_PASS], [[_line0("make_a")], []])
    got = s.settle("capture", {READ}, _baseline())
    assert got.outcome is SettlementOutcome.UNSETTLED
    assert "could not be put back" in got.reason
    assert path.read_text() == before


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


# ---- a crash whose culprit is a body of the write ----
#
# Run `757853-e28f1a7d` (2026-10-16, iter 4): a four-body file-first write
# read `pass 59 -> 64` and was restored whole because one of its bodies was
# the double drop the sanitizer named. The shapes are invented: the write
# put `bad_a` into `make_a` and `bad_b` into `make_b`; before it they held
# `old_a` / `old_b`.

_PRIOR = _LIB.replace("bad_a", "old_a").replace("bad_b", "old_b")


def _write(size: int = 2, written=(MAKE_A, MAKE_B)) -> JudgedWrite:
    return JudgedWrite(frozenset(written), {"src/lib.rs": _PRIOR}, size=size)


def test_a_culprit_that_is_part_of_the_write_goes_back_to_its_prior_text(
        tmp_path: Path) -> None:
    root = _tree(tmp_path)
    recorded: list = []
    write = _write()
    s = _settler(root, [_PASS], [[_site("t1", "make_a", 6)], []],
                 recorded=recorded)
    got = s.settle("capture", write, _baseline())
    assert got.outcome is SettlementOutcome.NARROWED, got.reason
    assert got.changed_tree
    text = (root / "src" / "lib.rs").read_text()
    # The culprit holds what it held before the write; the rest stands.
    assert "old_a" in text and "bad_a" not in text and "bad_b" in text
    assert STUB_BODY not in text
    assert write.withdrawn_names == ("make_a",)
    assert write.evidence
    # The refutation is the written body the stack ran.
    assert recorded == [[MAKE_A]]


def test_negative_control_a_write_that_cannot_be_narrowed_is_convicted_whole(
        tmp_path: Path) -> None:
    # The pre-change shape: the settlement is handed bare locators, so the
    # culprit's OWN step convicts the write and nothing is put back.
    root = _tree(tmp_path)
    s = _settler(root, [_PASS], [[_site("t1", "make_a", 6)], []])
    got = s.settle("capture", {MAKE_A, MAKE_B}, _baseline())
    assert got.outcome is SettlementOutcome.OWN
    assert [f.fn_name for f in got.own] == ["make_a"]
    assert (root / "src" / "lib.rs").read_text() == _LIB


def test_a_culprit_that_is_the_whole_write_convicts_it(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    write = _write(size=1, written=(MAKE_A,))
    s = _settler(root, [_PASS], [[_site("t1", "make_a", 6)], []])
    got = s.settle("capture", write, _baseline())
    assert got.outcome is SettlementOutcome.OWN
    assert write.withdrawn == []
    assert (root / "src" / "lib.rs").read_text() == _LIB


def test_a_withdrawn_body_the_crash_still_names_gives_everything_back(
        tmp_path: Path) -> None:
    # The pre-write text crashes the same way: the write did not cause it,
    # and nothing here explains it.
    root = _tree(tmp_path)
    write = _write()
    s = _settler(root, [_PASS],
                 [[_site("t1", "make_a", 6)], [_site("t1", "make_a", 6)]])
    got = s.settle("capture", write, _baseline())
    assert got.outcome is SettlementOutcome.UNSETTLED
    assert "already back" in got.reason
    assert write.withdrawn == []
    assert (root / "src" / "lib.rs").read_text() == _LIB


def test_a_withdrawal_and_a_stub_outside_the_write_settle_together(
        tmp_path: Path) -> None:
    root = _tree(tmp_path)
    write = _write(written=(READ, MAKE_A))
    recorded: list = []
    s = _settler(root, [_PASS, _PASS],
                 [[_site("t1", "make_a", 6)], [_site("t2", "make_b", 10)], []],
                 recorded=recorded)
    got = s.settle("capture", write, _baseline())
    assert got.outcome is SettlementOutcome.SETTLED, got.reason
    text = (root / "src" / "lib.rs").read_text()
    assert "old_a" in text and "bad_b" not in text and text.count(STUB_BODY) == 1
    assert write.withdrawn_names == ("make_a",)
    assert recorded == [[MAKE_B, MAKE_A]]


def test_a_withdrawal_needs_the_bodies_prior_text(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    write = JudgedWrite(frozenset({MAKE_A, MAKE_B}), {}, size=2)
    s = _settler(root, [_PASS], [[_site("t1", "make_a", 6)], []])
    got = s.settle("capture", write, _baseline())
    assert got.outcome is SettlementOutcome.OWN
    assert (root / "src" / "lib.rs").read_text() == _LIB


# ---- before the gates ----

class _Recorder:
    def __init__(self) -> None:
        self.calls: list = []

    def settle(self, capture, written, baseline, *, tag=""):
        self.calls.append(written)
        return None


_CRASHED = ("     Running tests/t.rs (target/debug/deps/t-1)\n\nrunning 2 tests\n"
            "test other ... ok\ntest t1 ... ")


def test_the_pre_gate_reading_settles_only_a_new_crash() -> None:
    rec = _Recorder()
    bind(rec)  # type: ignore[arg-type]
    try:
        write = _write()

        def _measure(text: str):
            def _m(sink: list[str]):
                sink.append(text)
                return TestStateReading(parse_binary_results(text))
            return _m

        assert withdraw_crashing_bodies(
            write, _measure(_PASS), _baseline()) is None
        assert rec.calls == []
        withdraw_crashing_bodies(write, _measure(_CRASHED), _baseline())
        assert rec.calls == [write]
        # A single body's culprit is the write: nothing to narrow, no reading.
        rec.calls.clear()
        withdraw_crashing_bodies(_write(size=1, written=(MAKE_A,)),
                                 _measure(_CRASHED), _baseline())
        assert rec.calls == []
    finally:
        bind(None)



def test_a_stub_the_settlement_made_is_told_what_its_body_did() -> None:
    # Run `757853-e28f1a7d` (2026-10-16): after iter 3 put
    # `into_boxed_dyn_error` back to a stub, the iter-4 prompt said only that
    # the fn was a stub holding three tests, and the reply wrote the same
    # double drop three times. The invented body here frees twice.
    told = held_stub_evidence(3, "let b = self.take();\ndrop(b);\ndrop(b)",
                              "free(): double free detected", 3)
    assert "3 failing test(s) stop" in told
    assert "drop(b)" in told and "double free" in told
    assert "iteration 3" in told and "Do not write that body again." in told
    # Negative control: a stub no settlement made says only that it is one.
    plain = held_stub_evidence(3)
    assert "put back" not in plain and plain.endswith("Give it a real body.")


def test_a_settled_stub_survives_a_restore_of_an_older_pre_image(
        tmp_path: Path) -> None:
    """A route's pre-image of the file, taken before the settlement, used to
    put the culprit's body back on any later restore (the decline, the
    compile peel, a whole-reply revert) while its refutation row stood. The
    stub is a kept edit now. Control: a pre-image taken after the
    settlement restores exactly its bytes."""
    from pipeline_core.application.reconstruct.tree_txn import capture, restore
    root = _tree(tmp_path)
    lib = root / "src" / "lib.rs"
    older = capture(lib, None)
    lib.write_text(_LIB.replace("real", "written"))       # the route's write
    s = _settler(root, [_PASS], [[_site("t1", "make_a", 6)], []])
    assert s.settle("capture", {READ}, _baseline()).outcome is \
        SettlementOutcome.SETTLED
    later = capture(lib, None)
    restore(older)
    text = lib.read_text()
    assert "real" in text and "written" not in text      # the write is gone
    assert "bad_a" not in text and text.count(STUB_BODY) == 1
    lib.write_text(_LIB)
    restore(later)
    assert lib.read_bytes() == later.data


def test_control_without_the_journal_the_older_restore_brings_the_culprit_back(
        tmp_path: Path, monkeypatch) -> None:
    from pipeline_core.application.reconstruct import crash_settler as cs
    from pipeline_core.application.reconstruct.tree_txn import capture, restore
    monkeypatch.setattr(cs, "note_kept_edit", lambda *_a, **_k: None)
    root = _tree(tmp_path)
    lib = root / "src" / "lib.rs"
    older = capture(lib, None)
    s = _settler(root, [_PASS], [[_site("t1", "make_a", 6)], []])
    assert s.settle("capture", {READ}, _baseline()).outcome is \
        SettlementOutcome.SETTLED
    restore(older)
    assert "bad_a" in lib.read_text()


def test_a_trace_that_stops_at_the_reader_outside_the_write_is_not_stubbed(
        ) -> None:
    """Run `14235-bf24f931` iter 3: the faulting accessor received a valid
    pointer and read a stale one out of it, so the trace named it the
    producer and the settlement refuted a correct body. A trace that stops
    at the innermost crate frame is not a producer outside the write.
    Control: 344's shape, where the inner accessor was handed the fabricated
    address and its caller made it, still names the caller and stubs it."""
    reader = site_from_frames("tests/t.rs", "t1", "SIGSEGV", [
        {"func": "crate::make_a", "file": "src/lib.rs", "line": 6,
         "ptr_args": [[0x1000, 8]]}], fault_addr=0x9999_0000)
    assert reader.origin_traced and reader.origin_traced_at_reader
    step = settle_step([reader], {READ}, _never)
    assert step.outcome is SettlementOutcome.UNSETTLED
    assert "trace stops at the frame that read" in step.reason
    fabricated = site_from_frames("tests/t.rs", "t1", "SIGSEGV", [
        {"func": "crate::read", "file": "src/lib.rs", "line": 2,
         "ptr_args": [[0x8, 8]]},
        {"func": "crate::make_a", "file": "src/lib.rs", "line": 6,
         "ptr_args": []}], fault_addr=0x8)
    assert fabricated.origin_traced and not fabricated.origin_traced_at_reader
    step2 = settle_step([fabricated], {FnLocator("src/lib.rs", "other")}, _never)
    assert step2.outcome is SettlementOutcome.STUB
    assert [f.fn_name for f in step2.culprits] == ["make_a"]


# ---- a crash that names nobody beside crashes that name a body ----
#
# Run `79929-5a16b1d7` (2026-10-23): every settlement of the destructor write
# ended `unsettled — tests/test_convert.rs: NOBODY` at the first such site,
# while five sites of the same reading named the two Box conversions the
# destructor had made reachable; the write was reverted on `pass 100 -> 132`.
# The shapes below are the invented `read` / `make_a` / `make_b` crate.

def _nobody(test: str = "t9") -> CrashSite:
    """An origin-capable use-after-free whose stacks hold no crate body."""
    return site_from_frames(
        "tests/u.rs", test, "AddressSanitizer: heap-use-after-free",
        [{"func": f"u::{test}", "file": "tests/u.rs", "line": 17}],
        origin_raw_frames=[{"func": f"u::{test}", "file": "tests/u.rs",
                            "line": 15}],
        origin_capable=True)


def test_a_site_that_names_nobody_does_not_veto_the_culprit_another_names(
) -> None:
    nobody = _nobody()
    assert nobody.origin_unattributed and not nobody.culprit_frames
    step = settle_step([nobody, _site("t1", "make_a", 7)], {READ}, _never)
    assert step.outcome is SettlementOutcome.STUB
    assert [f.fn_name for f in step.culprits] == ["make_a"]
    assert [c.site.test_name for c in step.carried] == ["t9"]
    assert "NOBODY" in step.carried[0].reason


def test_control_a_reading_of_nobody_alone_is_not_settled() -> None:
    """What the rule this replaced protected, kept: a crash nothing explains
    is never settled on its own, and a proven culprit is never put back."""
    step = settle_step([_nobody()], {READ}, _never)
    assert step.outcome is SettlementOutcome.UNSETTLED
    assert "NOBODY" in step.reason
    step = settle_step([_site("t1", "make_a", 7)], {READ},
                       lambda u: u == MAKE_A)
    assert step.outcome is SettlementOutcome.UNSETTLED
    assert [f.fn_name for f in step.culprits] == ["make_a"]


def test_a_culprit_in_the_write_after_a_site_that_names_nobody_convicts_it(
) -> None:
    """The first-site return also hid a later OWN."""
    step = settle_step([_nobody(), _site("t1", "make_a", 7)], {MAKE_A},
                       _never)
    assert step.outcome is SettlementOutcome.OWN
    assert [f.fn_name for f in step.culprits] == ["make_a"]


def test_a_proven_culprit_rides_along_and_the_others_go_back() -> None:
    step = settle_step([_site("t1", "make_a", 7), _site("t2", "make_b", 11)],
                       {READ}, lambda u: u == MAKE_A)
    assert step.outcome is SettlementOutcome.STUB
    assert [f.fn_name for f in step.culprits] == ["make_b"]
    assert "proven" in step.carried[0].reason


def test_a_carried_crash_that_goes_with_the_stubs_settles(
        tmp_path: Path) -> None:
    root = _tree(tmp_path)
    s = _settler(root, [_PASS], [[_nobody(), _site("t1", "make_a", 6)], []])
    got = s.settle("capture", {READ}, _baseline())
    assert got.outcome is SettlementOutcome.SETTLED, got.reason
    assert got.stubbed_locators == (MAKE_A,)
    text = (root / "src" / "lib.rs").read_text()
    assert "bad_a" not in text and "bad_b" in text and "real" in text


def test_a_carried_crash_still_there_after_the_stubs_gives_them_back(
        tmp_path: Path) -> None:
    root = _tree(tmp_path)
    s = _settler(root, [_PASS],
                 [[_nobody(), _site("t1", "make_a", 6)], [_nobody()]])
    got = s.settle("capture", {READ}, _baseline())
    assert got.outcome is SettlementOutcome.UNSETTLED
    assert "NOBODY" in got.reason
    assert (root / "src" / "lib.rs").read_text() == _LIB


def test_an_unlocatable_culprit_is_carried_and_the_others_go_back(
        tmp_path: Path) -> None:
    """A recovered frame of a shared name has no occurrence to put back; the
    other culprit of the same step still goes back, and the settlement holds
    only because the next reading has no crash left."""
    root = _tree(tmp_path)
    path = root / "src" / "lib.rs"
    path.write_text(path.read_text() + "\nfn make_a() {\n    other\n}\n")
    s = _settler(root, [_PASS],
                 [[_line0("make_a"), _site("t2", "make_b", 10)], []])
    got = s.settle("capture", {READ}, _baseline())
    assert got.outcome is SettlementOutcome.SETTLED, got.reason
    assert got.stubbed_locators == (MAKE_B,)
    text = path.read_text()
    assert "bad_b" not in text and "bad_a" in text and "other" in text
