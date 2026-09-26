"""A stack overflow is an unbounded recursion: its repeating in-crate frames are
the culprits, jointly — not an innocent reader of memory someone else freed.

The class, from run `38934-2999cc94` (2026-09-26, cluster `stable`, sweeps
5-7): 13 of the last 14 failing tests died of `AddressSanitizer:
stack-overflow` in 11 test binaries. The report held the whole cycle — the
faulting stack alternates two or three in-crate fns 80-120 times — and the
localizer logged `no in-crate frame in the origin stack … blaming NOBODY`,
because `is_memory_fault_signal` read every AddressSanitizer verdict as
invalid memory and an absent origin as unattributed. With nobody blamed the
two units of the cycle were refill targets of no route (`no route attempted`
after fills 5 and 6), and the file-first prompts they did appear in told the
model the frames were innocent and to look for freed memory.

Fixtures: two recorded reports (`tests/fixtures/crash/stack_overflow_*`, the
first report of the sweep's ASAN output, trimmed to 120 frames), a three-member
ring from sweep 5 and a two-member ring from sweep 7. The synthetic shapes
(three-fn ring, a fn that calls itself, a stack that does not repeat) use
invented names.

The plan's sentences, each with its negative control (the pre-fix reading
injected through `fault_class`):
- the recorded cycle is convicted, every member, and is a refill stamp of the
  block-level and per-fn planners;
- the layout retrigger's origin blame does not read it;
- eleven binaries dying of one stack are ONE evidence block, and it says
  what was observed instead of "the frames only read it";
- a use-after-free keeps its origin semantics.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import pytest

from pipeline_core.application.reconstruct.crash_localizer import (
    CrashLocalizerDeps,
    localize_crashes,
)
from pipeline_core.domain import crash_site
from pipeline_core.domain.crash_site import (
    CrashFrame,
    FaultClass,
    blamed_locators,
    crash_evidence_by_locator,
    fault_class,
    faulting_stack_records,
    origin_blamed_locators,
    reader_blamed_locators,
    repeating_cycle,
    site_from_frames,
)
from pipeline_core.domain.language_driver import using_driver
from pipeline_core.domain.models import FnLocator
from pipeline_core.prompts.language_catalog import catalog_driver

_FIX = Path(__file__).parent / "fixtures" / "crash"
_ROOT = Path("/home/user/determ/tmp/anyhow/.skeleton-filled/stable")


@pytest.fixture(autouse=True)
def _catalog_driver() -> Iterator[None]:
    with using_driver(catalog_driver()):
        yield


def _sweep(fixture: str, binaries: list[tuple[str, str]]) -> str:
    """The sanitized sweep's output as the loop saw it: one section per crashed
    binary — `Running <label>`, the test that never reported, then the recorded
    report (its `Running` header line dropped)."""
    lines = (_FIX / fixture).read_text().splitlines()
    report = lines[1:]
    out: list[str] = []
    for label, test in binaries:
        out += [f"    Running {label} (target-asan/debug/deps/x-1)",
                "running 1 test", f"test {test} ... " + report[0]] + report[1:]
    return "\n".join(out) + "\n"


def _sites(fixture: str, binaries: list[tuple[str, str]]):
    deps = CrashLocalizerDeps(
        gdb_localize=lambda *a, **k: None,
        asan_sweep=lambda *a, **k: (101, _sweep(fixture, binaries)),
        log=lambda _m: None)
    # The crashed binaries are found by the sweep output the loop already has.
    sweep = "\n".join(
        f"    Running {label} (target/debug/deps/x-1)\nrunning 1 test\n"
        f"test {test} ...\n" for label, test in binaries)
    return localize_crashes(deps, sweep, "cargo test", _ROOT, {})


_TWO = "stack_overflow_ring2_38934_sweep7.txt"
_THREE = "stack_overflow_ring3_38934_sweep5.txt"
_BINARIES = [("tests/error_gen_2.rs", "t_one"), ("tests/ptr_gen_2.rs", "t_two"),
             ("tests/test_boxed.rs", "t_three")]

_LOC_CFB = FnLocator("src/error.rs", "construct_from_boxed")
_LOC_NEW = FnLocator("src/kind.rs", "new")
_LOC_FROM = FnLocator("src/error.rs", "from_boxed")


# ---- the class is read once, from the report's kind ----


def test_the_sanitizers_stack_overflow_is_exhaustion_not_invalid_memory() -> None:
    assert fault_class("AddressSanitizer: stack-overflow") is FaultClass.EXHAUSTION
    for kind in ("AddressSanitizer: heap-use-after-free", "SIGSEGV",
                 "AddressSanitizer: stack-use-after-return"):
        assert fault_class(kind) is FaultClass.INVALID_MEMORY
    assert fault_class("SIGTRAP") is FaultClass.OTHER


# ---- the recorded cycle is convicted, whole ----


def test_the_recorded_two_member_cycle_is_convicted_and_not_unattributed() -> None:
    sites = _sites(_TWO, _BINARIES)
    assert len(sites) == 3
    for s in sites:
        assert s.fault is FaultClass.EXHAUSTION
        assert s.origin_unattributed is False
        assert s.culprit_locators == {_LOC_CFB, _LOC_NEW}
        assert s.cycle is not None and s.cycle.repeats >= 50
    assert blamed_locators(sites) == {_LOC_CFB, _LOC_NEW}


def test_the_recorded_three_member_cycle_convicts_all_three() -> None:
    sites = _sites(_THREE, _BINARIES[:1])
    assert sites[0].culprit_locators == {_LOC_CFB, _LOC_NEW, _LOC_FROM}
    assert sites[0].cycle is not None and len(sites[0].cycle.frames) == 3


def test_the_convicted_set_is_what_the_stack_repeats() -> None:
    """Independent of the finder: the fns that come round most often in the
    recorded frames are the ones convicted."""
    (site, *_rest) = _sites(_TWO, _BINARIES)
    counts = Counter((f.file_rel, f.fn_name) for f in _all_crate_frames(_TWO))
    repeated = {FnLocator(f, n) for (f, n), c in counts.items() if c >= 50}
    assert site.culprit_locators == repeated


def _all_crate_frames(fixture: str) -> list[CrashFrame]:
    from pipeline_core.domain.crash_site import parse_asan_reports
    text = _sweep(fixture, _BINARIES[:1])
    rep = parse_asan_reports(text)[0]
    out = []
    for r in rep.access:
        f = str(r["file"]).replace(str(_ROOT) + "/", "")
        if f.startswith("src/"):
            out.append(CrashFrame(f, int(r["line"]), crash_site.bare_fn_name(
                str(r["func"])), str(r["func"])))
    return out


def test_negative_control_read_as_invalid_memory_the_cycle_blames_nobody(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crash_site, "fault_class",
                        lambda _sig: FaultClass.INVALID_MEMORY)
    sites = _sites(_TWO, _BINARIES)
    assert all(s.origin_unattributed for s in sites)
    assert all(s.blamed is None and not s.culprit_locators for s in sites)
    assert blamed_locators(sites) == frozenset()


# ---- who reads the cycle ----


def test_the_layout_retrigger_and_the_producer_aim_do_not_read_a_cycle() -> None:
    sites = _sites(_TWO, _BINARIES)
    assert origin_blamed_locators(sites) == frozenset()
    assert reader_blamed_locators(sites) == frozenset()


def _stamped(crash_blamed: frozenset[FnLocator]) -> set[str]:
    """The block planner's own predicate, fed the localizer's blame: a filled
    unit that no attributed test convicts is stamped a refill only when it is
    in `crash_blamed`."""
    from pipeline_core.application.reconstruct import cluster_membership as cm
    from pipeline_core.domain.attribution import FailureFrontier
    from pipeline_core.domain.models import FnRecord, ImplBlock
    blocks = [
        ImplBlock(file=loc.file, container=container, stub_members=(),
                  members=(FnRecord(file=loc.file, fn_name=loc.fn_name,
                                    container=container,
                                    sig_text=f"fn {loc.fn_name}()",
                                    has_unimplemented_body=False),))
        for loc, container in ((_LOC_CFB, "impl Error"), (_LOC_NEW, "impl Boxed"))]
    # The failing tests are statically owned by other units: `tests_for` is
    # empty for both members, as it was on the recorded tree.
    frontier = FailureFrontier.of_direct(
        {FnLocator("src/error.rs", "elsewhere"): {"t_one"}})
    out = cm.collect_impl_refill_targets(
        blocks, tdd_frontier_keys={_LOC_CFB, _LOC_NEW},
        tdd_frontier_tests=frontier, crash_blamed=crash_blamed)
    return {m.fn_name for b in out for m in b.refill_members}


def test_the_cycle_reaches_the_block_planner_as_a_refill_stamp() -> None:
    assert _stamped(blamed_locators(_sites(_TWO, _BINARIES))) == {
        "construct_from_boxed", "new"}


def test_negative_control_the_old_blame_stamped_neither(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crash_site, "fault_class",
                        lambda _sig: FaultClass.INVALID_MEMORY)
    assert _stamped(blamed_locators(_sites(_TWO, _BINARIES))) == set()


# ---- one observation, one block ----


def test_three_binaries_of_one_stack_are_one_evidence_block() -> None:
    sites = _sites(_TWO, _BINARIES)
    text = crash_evidence_by_locator(sites)[_LOC_CFB]
    assert text.count("STACK OVERFLOW") == 1
    assert all(t in text for t in ("t_one", "t_two", "t_three"))
    assert "call each other without a base case" in text
    assert "UNKNOWN" not in text and "freed" not in text.replace(
        "nothing was freed", "")
    assert "pointer/lifetime" not in text


def test_only_the_cycle_carries_the_evidence_not_the_innermost_frame() -> None:
    """The recorded report's innermost crate frame is an inlined callee the
    symbolizer labelled `Boxed::new` at `backtrace.rs:10`; keying it ranked an
    unrelated unit as crash evidence (every scheduler read of the state in
    `scripts/llm_scheduler_probe.py` spent a run on it)."""
    keys = set(crash_evidence_by_locator(_sites(_TWO, _BINARIES)))
    assert keys == {_LOC_CFB, _LOC_NEW}
    assert FnLocator("src/backtrace.rs", "new") not in keys


def test_negative_control_the_old_reading_printed_one_block_per_binary(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crash_site, "fault_class",
                        lambda _sig: FaultClass.INVALID_MEMORY)
    sites = _sites(_TWO, _BINARIES)
    text = crash_evidence_by_locator(sites)[_LOC_CFB]
    assert text.count("The detector did not report an in-crate origin") == 3


# ---- the finder decides on shape, on names it has never seen ----


def _frames(names: list[str]) -> tuple[CrashFrame, ...]:
    return tuple(CrashFrame(f"src/{n[0]}.rs", 10 + i, n, f"crate::{n}")
                 for i, n in enumerate(names))


def test_a_three_fn_ring_and_a_self_call_and_no_repeat() -> None:
    ring = repeating_cycle(_frames(["z"] + ["a", "b", "c"] * 5))
    assert ring is not None and [f.fn_name for f in ring.frames] == ["a", "b", "c"]
    assert ring.repeats == 5
    selfcall = repeating_cycle(_frames(["walk"] * 9))
    assert selfcall is not None and len(selfcall.frames) == 1
    assert repeating_cycle(_frames(["a", "b", "c", "d", "e", "f", "g"])) is None
    assert repeating_cycle(_frames(["a", "b", "a", "b"])) is None    # 2 < 3 repeats


def test_an_overflow_with_no_repeating_crate_frame_convicts_nobody() -> None:
    raw = [{"func": "std::rt::lang_start", "file": "/rustc/x/library/std/rt.rs",
            "line": 1}] * 40 + [
        {"func": "crate::solo", "file": "src/solo.rs", "line": 3}]
    site = site_from_frames("tests/t.rs", "t", "AddressSanitizer: stack-overflow",
                            raw, origin_capable=True)
    assert site.cycle is None and site.blamed is None
    assert site.origin_unattributed is False
    assert "no repeating in-crate frames" in site.blame_label


def test_a_use_after_free_keeps_its_origin_semantics() -> None:
    raw = [{"func": "crate::a::read", "file": "src/a.rs", "line": 4}]
    site = site_from_frames("tests/t.rs", "t", "AddressSanitizer: heap-use-after-free",
                            raw, origin_capable=True)
    assert site.fault is FaultClass.INVALID_MEMORY
    assert site.origin_unattributed is True and site.blamed is None


# ---- an aborted test executed the fns of its own faulting stack ----


def test_an_aborted_test_owns_the_fns_of_its_faulting_stack() -> None:
    """13 of 14 failures of run `38934-2999cc94` were `unmeasured (binary did
    not complete)`: no owner set, so a session planned for them listed none of
    the fns the crash names as executed. The in-flight test of each binary has
    a stack, and the stack is an observation."""
    sites = _sites(_TWO, _BINARIES)
    store = {"t_one": frozenset({FnLocator("src/old.rs", "seen_before")})}
    got = faulting_stack_records(sites, store, ["t_one", "t_two", "t_three"])
    for t in ("t_one", "t_two", "t_three"):
        assert {_LOC_CFB, _LOC_NEW} <= got[t]
    assert FnLocator("src/old.rs", "seen_before") in got["t_one"]   # unioned


def test_a_measured_test_is_never_overwritten_and_control_is_unchanged() -> None:
    sites = _sites(_TWO, _BINARIES)
    got = faulting_stack_records(sites, {}, ["t_one"])       # only t_one unowned
    assert set(got) == {"t_one"}
    store = {"t_one": frozenset({_LOC_CFB})}
    # Negative control: no site, the pre-fix record — the aborted test owns
    # only what the store remembered.
    assert faulting_stack_records((), store, ["t_one"]) == store
