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
    # The retry is shown the write's own site, from the one writer.
    assert got.own_sites and "make_a" in got.render()
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


# ---- a stub's cost is bought once per culprit body and baseline ----
#
# Run `1643490-850cf640` (2026-10-29): 21 settlements on one culprit body, each
# a stub and a suite run that cost the same baseline passes and were given
# back. The shapes stay invented: `make_a` is the kept culprit, `other` the
# baseline pass it holds.

_STUB_LOST = _PASS.replace("test other ... ok", "test other ... FAILED")


class _CountingSettler:
    def __init__(self, root: Path, sites: list) -> None:
        self.suite_runs = 0
        self.events: list = []
        self._sites = sites

        def _suite() -> str:
            self.suite_runs += 1
            return _STUB_LOST
        self.settler = CrashSettler(CrashSettlerDeps(
            localize=lambda _c, _e: next(self._round, []),
            run_suite=_suite, is_proven=_never, src_dir=root,
            record_settled=lambda _l, _s: None, log=lambda _m: None,
            emit=lambda e, d: self.events.append((e, dict(d)))))
        self._round = iter(())

    def settle(self, capture: str, baseline: TestStateReading):
        # Each settlement localizes the culprit, then (if it stubs) no crash.
        self._round = iter([list(self._sites), []])
        return self.settler.settle(capture, {READ}, baseline)


def test_a_reused_refusal_reports_the_stored_loss(tmp_path: Path) -> None:
    """The event of a refusal this fill already measured carries the loss
    the reason names. A memo that stored the sentence emits an empty
    `lost`."""
    root = _tree(tmp_path)
    s = _CountingSettler(root, [_site("t1", "make_a", 6)])
    s.settle(_PASS, _baseline())
    s.settle(_PASS, _baseline())
    ev = [d for e, d in s.events if e == "phase_c.crash.settle"]
    assert ev[1]["reused"] is True
    assert ev[1]["lost"] == ["other"]
    assert ev[1]["aborted_green"] == []
    assert ev[1]["unmeasured"] == []
    assert "['other']" in ev[1]["reason"]


_SILENT_OTHER = (
    "     Running tests/t.rs (target/debug/deps/t-1)\n\nrunning 2 tests\n"
    "test other ... FAILED\ntest t1 ... FAILED\n\n"
    "failures:\n\n---- other stdout ----\n"
    "determ-isolation: test process aborted\n\n"
    "test result: FAILED. 0 passed; 2 failed\n")


def test_a_reused_silent_abort_stays_out_of_lost_alone(
        tmp_path: Path) -> None:
    """A baseline pass whose own process died without a panic is in `lost`
    and in `aborted_green`. Writing the union into `lost` alone leaves
    `aborted_green` empty."""
    root = _tree(tmp_path)
    s = _CountingSettler(root, [_site("t1", "make_a", 6)])

    def _suite() -> str:
        s.suite_runs += 1
        return _SILENT_OTHER
    s.settler.deps = _deps_with_suite(s, _suite)
    s.settle(_PASS, _baseline())
    s.settle(_PASS, _baseline())
    ev = [d for e, d in s.events if e == "phase_c.crash.settle"]
    assert ev[1]["reused"] is True
    assert ev[1]["lost"] == ["other"]
    assert ev[1]["aborted_green"] == ["other"]
    assert s.suite_runs == 1


_TWO_BASELINE = (
    "     Running tests/a.rs (target/debug/deps/a-1)\n\nrunning 1 test\n"
    "test other ... ok\n\ntest result: ok. 1 passed; 0 failed; 0 ignored; "
    "0 measured; 0 filtered out\n\n"
    "     Running tests/b.rs (target/debug/deps/b-1)\n\nrunning 1 test\n"
    "test t1 ... FAILED\n\ntest result: FAILED. 0 passed; 1 failed; 0 "
    "ignored; 0 measured; 0 filtered out\n")
_ONE_BINARY = (
    "     Running tests/a.rs (target/debug/deps/a-1)\n\nrunning 1 test\n"
    "test other ... FAILED\n\ntest result: FAILED. 0 passed; 1 failed; 0 "
    "ignored; 0 measured; 0 filtered out\n")


def test_a_reused_refusal_names_a_binary_the_stub_reading_never_started(
        tmp_path: Path) -> None:
    """`not_run` is the baseline binaries absent from the stub reading.
    Recomputing it from the write's capture, which started them, is empty."""
    root = _tree(tmp_path)
    s = _CountingSettler(root, [_site("t1", "make_a", 6)])

    def _suite() -> str:
        s.suite_runs += 1
        return _ONE_BINARY
    s.settler.deps = _deps_with_suite(s, _suite)
    base = TestStateReading(parse_binary_results(_TWO_BASELINE))
    s.settle(_TWO_BASELINE, base)
    s.settle(_TWO_BASELINE, base)
    ev = [d for e, d in s.events if e == "phase_c.crash.settle"]
    assert ev[1]["reused"] is True
    assert any("b.rs" in n for n in ev[1]["not_run"])
    assert ev[1]["lost"] == ["other"]


def test_a_stub_that_cost_the_baseline_is_not_bought_again(
        tmp_path: Path) -> None:
    root = _tree(tmp_path)
    s = _CountingSettler(root, [_site("t1", "make_a", 6)])
    first = s.settle(_PASS, _baseline())
    assert first.outcome is SettlementOutcome.UNSETTLED
    assert s.suite_runs == 1
    second = s.settle(_PASS, _baseline())
    assert second.outcome is SettlementOutcome.UNSETTLED
    assert "other" in second.reason
    assert s.suite_runs == 1, "the same stub was measured twice"
    assert second.sites, "the crash text is this capture's own sites"
    assert (root / "src" / "lib.rs").read_text() == _LIB
    assert [d["reused"] for e, d in s.events
            if e == "phase_c.crash.settle"] == [False, True]


def test_control_a_changed_culprit_body_is_measured_again(
        tmp_path: Path) -> None:
    root = _tree(tmp_path)
    s = _CountingSettler(root, [_site("t1", "make_a", 6)])
    s.settle(_PASS, _baseline())
    path = root / "src" / "lib.rs"
    path.write_text(path.read_text().replace("bad_a", "bad_a2"))
    s.settle(_PASS, _baseline())
    assert s.suite_runs == 2


def test_control_a_moved_baseline_is_measured_again(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    s = _CountingSettler(root, [_site("t1", "make_a", 6)])
    s.settle(_PASS, _baseline())
    moved = TestStateReading(parse_binary_results(
        _PASS.replace("test t1 ... FAILED", "test t1 ... ok")))
    s.settle(_PASS, moved)
    assert s.suite_runs == 2


def test_control_a_write_that_regressed_on_its_own_teaches_nothing(
        tmp_path: Path) -> None:
    """The write's own reading already lost `other`: the stub's reading
    losing it too is the write's cost, not the culprit's, so nothing is
    remembered and the next write's settlement runs."""
    root = _tree(tmp_path)
    s = _CountingSettler(root, [_site("t1", "make_a", 6)])
    s.settle(_STUB_LOST, _baseline())
    s.settle(_PASS, _baseline())
    assert s.suite_runs == 2


def test_control_a_new_settler_measures_again(tmp_path: Path) -> None:
    """The memory is the bound settler's, one per fill: the next fill's
    settler measures the stub again."""
    root = _tree(tmp_path)
    s1 = _CountingSettler(root, [_site("t1", "make_a", 6)])
    s1.settle(_PASS, _baseline())
    s2 = _CountingSettler(root, [_site("t1", "make_a", 6)])
    s2.settle(_PASS, _baseline())
    assert s1.suite_runs == 1 and s2.suite_runs == 1


# A capture scoped to the written unit never runs `other`. The full suite
# after the stub does, and judges it failed. That loss is the stub's.
_SCOPED = ("     Running tests/t.rs (target/debug/deps/t-1)\n\nrunning 1 test\n"
           "test t1 ... FAILED\n\ntest result: FAILED. 0 passed; 1 failed\n")

# The binary opened and died before it reported `other`: the after-reading
# never judged that baseline pass. It is unmeasured, not the stub's cost.
_ABORTED = ("     Running tests/t.rs (target/debug/deps/t-1)\n\nrunning 2 tests\n"
            "test t1 ... FAILED\n")

# `other` printed FAILED and the binary never closed. The comparator every
# keep reads (`assertion_position.outcomes_of`) counts a failure in any
# binary, so this is a lost baseline pass. Until 2026-11-02 the settlement
# alone read only completed binaries here.
_ABORTED_AFTER_FAILING = (
    "     Running tests/t.rs (target/debug/deps/t-1)\n\nrunning 2 tests\n"
    "test other ... FAILED\n")


def test_a_scoped_capture_that_never_ran_the_lost_test_still_teaches(
        tmp_path: Path) -> None:
    root = _tree(tmp_path)
    s = _CountingSettler(root, [_site("t1", "make_a", 6)])
    first = s.settle(_SCOPED, _baseline())
    assert first.outcome is SettlementOutcome.UNSETTLED
    assert s.suite_runs == 1
    second = s.settle(_SCOPED, _baseline())
    assert second.outcome is SettlementOutcome.UNSETTLED
    assert "other" in second.reason
    assert s.suite_runs == 1
    assert [d["reused"] for _e, d in s.events
            if _e == "phase_c.crash.settle"] == [False, True]


def test_control_an_unjudged_baseline_pass_teaches_nothing(
        tmp_path: Path) -> None:
    """The binary started and never reported `other`: no verdict, so the
    hold names it unmeasured and nothing is remembered; the next settlement
    measures."""
    root = _tree(tmp_path)
    s = _CountingSettler(root, [_site("t1", "make_a", 6)])

    def _suite() -> str:
        s.suite_runs += 1
        return _ABORTED
    s.settler.deps = _deps_with_suite(s, _suite)
    first = s.settle(_PASS, _baseline())
    assert first.outcome is SettlementOutcome.UNSETTLED
    assert first.reason == "not measured after the stubs: ['other']"
    s.settle(_PASS, _baseline())
    assert s.suite_runs == 2
    assert (root / "src" / "lib.rs").read_text() == _LIB
    ev = [d for e, d in s.events if e == "phase_c.crash.settle"][0]
    assert ev["unmeasured"] == ["other"] and ev["lost"] == []


def test_a_failure_printed_before_the_abort_is_a_lost_pass(
        tmp_path: Path) -> None:
    root = _tree(tmp_path)
    s = _CountingSettler(root, [_site("t1", "make_a", 6)])

    def _suite() -> str:
        s.suite_runs += 1
        return _ABORTED_AFTER_FAILING
    s.settler.deps = _deps_with_suite(s, _suite)
    first = s.settle(_PASS, _baseline())
    assert first.reason == "the stubs cost baseline passes: ['other']"


def _deps_with_suite(counter: _CountingSettler, suite):
    return CrashSettlerDeps(
        localize=counter.settler.deps.localize,
        run_suite=suite, is_proven=_never, src_dir=counter.settler.deps.src_dir,
        record_settled=lambda _l, _s: None, log=lambda _m: None,
        emit=counter.settler.deps.emit)


def test_a_carried_crash_does_not_rebuy_a_known_stub(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    s = _CountingSettler(root, [_site("t1", "make_a", 6)])
    s.settle(_PASS, _baseline())
    assert s.suite_runs == 1
    s._sites = [_nobody(), _site("t1", "make_a", 6)]
    second = s.settle(_PASS, _baseline())
    assert second.outcome is SettlementOutcome.UNSETTLED
    assert "other" in second.reason
    assert s.suite_runs == 1


def test_a_second_culprit_still_remembers_the_body_measured_alone(
        tmp_path: Path) -> None:
    """The first stub's reading already lost the baseline pass. A later
    settlement of that body alone, and one of both bodies together, does
    not buy either set again."""
    root = _tree(tmp_path)
    script = [
        [_site("t1", "make_a", 6)],
        [_site("t2", "make_b", 10)],
        [],
        [_site("t1", "make_a", 6)],
        [_site("t1", "make_a", 6), _site("t2", "make_b", 10)],
    ]
    runs = {"n": 0}

    def _suite() -> str:
        runs["n"] += 1
        return _STUB_LOST
    settler = CrashSettler(CrashSettlerDeps(
        localize=lambda _c, _e: script.pop(0) if script else [],
        run_suite=_suite, is_proven=_never, src_dir=root,
        record_settled=lambda _l, _s: None, log=lambda _m: None))
    first = settler.settle(_PASS, {READ}, _baseline())
    assert first.outcome is SettlementOutcome.UNSETTLED
    assert runs["n"] == 2
    second = settler.settle(_PASS, {READ}, _baseline())
    assert second.outcome is SettlementOutcome.UNSETTLED
    assert "other" in second.reason
    both = settler.settle(_PASS, {READ}, _baseline())
    assert both.outcome is SettlementOutcome.UNSETTLED
    assert runs["n"] == 2


def test_crash_text_is_the_writes_own_crash_only() -> None:
    """Run `3808231-81b847f7` (2026-10-29, fill 3): a pair over a Debug
    dispatcher and its implementer was shown, as its previous attempt's
    failure, a layout-dependent use-after-free produced in a downcast body
    outside its write, with a MUST to fix that body. The crash text now
    holds only sites whose faulting or origin stack ran through a body of
    the write (or of the caller's wider scope). Negative control: the same
    settlement with the origin body in the write renders it."""
    from pipeline_core.application.reconstruct.crash_settler import (
        Settlement,
        crash_text_of,
    )
    from pipeline_core.domain.crash_settlement import SettlementOutcome
    from pipeline_core.domain.crash_site import CrashFrame, CrashSite
    from pipeline_core.domain.models import FnLocator

    origin = CrashFrame("src/store.rs", 41, "take_out", "take_out")
    site = CrashSite("tests/t.rs", "t_take", "SIGSEGV",
                     frames=(CrashFrame("tests/t.rs", 19, "t_take", "t_take"),),
                     origin_frames=(origin,))
    nobody = CrashSite("tests/t.rs", "t_other", "SIGSEGV", frames=())
    pair = frozenset({FnLocator("src/show.rs", "fmt"),
                      FnLocator("src/show.rs", "render")})
    foreign = Settlement(SettlementOutcome.UNSETTLED, sites=(site, nobody),
                         written=pair)
    assert crash_text_of(foreign) == ""
    # The caller's wider scope (a session's editable set) admits it.
    assert "src/store.rs:41" in crash_text_of(
        foreign, pair | {FnLocator("src/store.rs", "take_out")})
    own = Settlement(SettlementOutcome.UNSETTLED, sites=(site,),
                     written=pair | {FnLocator("src/store.rs", "take_out")})
    assert "src/store.rs:41" in crash_text_of(own)
    # A settlement that changed the tree still names the bodies it stubbed.
    stubbed = Settlement(SettlementOutcome.SETTLED, sites=(site,),
                         stubbed=(origin,), written=pair)
    text = crash_text_of(stubbed)
    assert "src/store.rs:41" not in text.split("Before the verdicts")[0]
    assert "src/store.rs::take_out" in text


# ---- the hold reads one test per binary, never a count or a bare name ----
#
# Two test files each hold a test named `t`: it passes in `tests/a.rs` and is
# red in `tests/b.rs` at the baseline (the `test_drop` shape of run
# `14495-a26ad4ea`). Invented names.

def _two(a: str, b: str) -> str:
    return ("     Running tests/a.rs (target/debug/deps/a-1)\n\nrunning 1 test\n"
            f"test t ... {a}\n\ntest result: ok. 1 passed; 0 failed\n"
            "     Running tests/b.rs (target/debug/deps/b-1)\n\nrunning 1 test\n"
            f"test t ... {b}\n\ntest result: FAILED. 0 passed; 1 failed\n")


_TWO_BASE = _two("ok", "FAILED")


def _old_stub_own_cost(capture: str, stubbed: str,
                       baseline: TestStateReading) -> list[str]:
    """The bare-name rule this batch replaced, kept as the negative control."""
    def _judged_failed(r: TestStateReading) -> frozenset[str]:
        out: set[str] = set()
        for b in r.binaries:
            if b.completed:
                out |= b.failed
        return frozenset(out)
    cap = _judged_failed(TestStateReading(parse_binary_results(capture)))
    aft = _judged_failed(TestStateReading(parse_binary_results(stubbed)))
    return sorted((baseline.passed_names & aft) - cap)


def test_a_name_red_in_another_file_is_not_the_stubs_cost() -> None:
    from pipeline_core.application.reconstruct.crash_settler import _stub_own_cost
    base = TestStateReading(parse_binary_results(_TWO_BASE))
    assert _stub_own_cost(_SCOPED, _TWO_BASE, base) == []
    # Negative control: the bare-name rule charged it.
    assert _old_stub_own_cost(_SCOPED, _TWO_BASE, base) == ["t"]


def test_the_same_name_lost_in_its_own_file_is_the_stubs_cost() -> None:
    from pipeline_core.application.reconstruct.crash_settler import _stub_own_cost
    base = TestStateReading(parse_binary_results(_TWO_BASE))
    lost = _two("FAILED", "FAILED")
    assert _stub_own_cost(_SCOPED, lost, base) == ["t"]


def _hold(reading: str, baseline: str):
    s = CrashSettler(CrashSettlerDeps(
        localize=lambda _c, _e: [], run_suite=lambda: None,
        is_proven=_never, src_dir=Path("."),
        record_settled=lambda _l, _s: None, log=lambda _m: None))
    return s._holds(reading, TestStateReading(parse_binary_results(baseline)))


def test_the_hold_judges_per_file() -> None:
    assert _hold(_TWO_BASE, _TWO_BASE)[0] is SettlementOutcome.SETTLED
    out, why = _hold(_two("FAILED", "FAILED"), _TWO_BASE)
    assert out is SettlementOutcome.UNSETTLED
    assert why == "the stubs cost baseline passes: ['t']"


def test_a_binary_that_never_started_decides_nothing() -> None:
    """Only `tests/a.rs` ran after the stubs: `tests/b.rs` has no section,
    which is not a reading of its tests."""
    base = _two("ok", "ok").replace("0 passed; 1 failed", "1 passed; 0 failed")
    only_a = base.split("     Running tests/b.rs")[0]
    assert _hold(only_a, base)[0] is SettlementOutcome.SETTLED


def _isolated(name: str, status: str, other: str = "passed") -> str:
    from pipeline_core.domain.test_isolation import (
        IsolatedStatus,
        IsolatedTest,
        render_isolated_section,
    )
    tests = [IsolatedTest("other", IsolatedStatus(other)),
             IsolatedTest(name, IsolatedStatus(status),
                          body="" if status == "passed" else "boom")]
    return ("     Running tests/t.rs (target/debug/deps/t-1)\n\n"
            + render_isolated_section(tests) + "\n")


def test_an_isolated_abort_of_a_baseline_pass_unsettles() -> None:
    base = _isolated("t1", "passed")
    out, why = _hold(_isolated("t1", "aborted"), base)
    assert out is SettlementOutcome.UNSETTLED
    assert "['t1']" in why


def test_an_isolated_abort_of_a_test_red_at_the_baseline_does_not() -> None:
    """The settlement has no gain to weigh, and the keep's abort rule
    (`AbortPartition.refuses`) is not read here: a red test that now dies
    lost nothing that passed."""
    base = _isolated("t1", "failed")
    assert _hold(_isolated("t1", "aborted"), base)[0] is SettlementOutcome.SETTLED
