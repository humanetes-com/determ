"""Walk: run `263217-d41a5bb5` (2026-08-15, cluster `stable`) — the sweep where
chain-derived blame armed every consumer except the one that aims.

The recording: entry with `best_pass` unset (the persisted floor was dropped on
a code_id change), then two iterations, both flat at `pass=39 fail=40
oracle=79` with 22 expected executions counted as failing. Both localized the
same crash — `unittests src/lib.rs`, `AddressSanitizer: SEGV`, origin
UNATTRIBUTED, and the only two frames in the whole report the crashing TEST's
own (`src/error.gen_tests_4.rs:14` and its `{closure#0}` at :11). At iter 1 the
driver synthesized CHAIN-DERIVED blame over 67 fns and armed the evidence pack;
`phase_c.crash.chain_blame` records `chain_count=67`, `suspect_count=36`, and
the chain holds `src/error.rs::construct`.

What did NOT happen in 308 minutes: a single `[layout-retrigger]` line. No
fire, no refusal, no reason. The driver handed the aim `blamed_fns` =
`crash_texts` minus the chain keys, and what that subtraction LEFT was the two
`gen_tests` locators — non-empty, and naming nothing the crate declares.
`crash_candidate_grade` returned on `blamed_fns` before the chain branch three
lines below it, all 16 non-GATED components graded NONE, `top_blame_tier` came
back NONE, the candidate list emptied, and every refusal that would have logged
lives INSIDE the loop over that list. Control fell to the one exit in the
module that printed nothing.

What the fix makes REACHABLE (never a score): the aim receives the ORIGIN blame
(`blamed_locators`), which for this crash class is EMPTY — so the chain is
consulted, grades OWNS on the construct/vtable component (the chain holds
`construct` and that component declares it), and the walk reaches a component
with an attempt to spend.

Negative control: the evidence object the PRE-FIX driver built — the two
residue locators in `blamed_fns`, the same chain — driven through the same real
`retrigger_layout`. The recorded decision must reproduce exactly: `fired=False`,
`reason="no component matched the evidence"`, nothing charged, nothing
committed, no authoring call. The one thing that does NOT reproduce is the
silence, and deliberately: the declined-walk line now prints the tier and the
evidence shape, which is what makes this outcome diagnosable at iteration 1
instead of at hour five.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pipeline_core.domain.models import FnLocator
from tests.walks.harness import load_walk, replay

pytestmark = pytest.mark.walk

# The cluster's full inventory: 79 reported + 22 counted as failing (19 named
# tests with no verdict, 3 further executions lost to the same 3 crashed
# binaries) — the `ORACLE SHRUNK` line of both recorded iterations.
_INVENTORY = 101

# The crashed test, and the two frames the localizer reported for it. Both are
# in a GENERATED test file: this is the frame-blind class, where the fault
# surfaces in std/alloc drop glue and no in-crate frame is in any stack.
_CRASHED_TEST = "test_reallocate_into_boxed_dyn_error_without_backtrace"
_RESIDUE = frozenset({
    FnLocator("src/error.gen_tests_4.rs", _CRASHED_TEST),
    FnLocator("src/error.gen_tests_4.rs", "{closure#0}"),
})

# The construction chain the driver synthesized, as `phase_c.crash.chain_blame`
# recorded it (the event caps its list at 50 of the 67 keys; the decisive one
# is `src/error.rs::construct`, which the mega component DECLARES).
_CHAIN_NAMES = [
    "src/chain.rs::default", "src/chain.rs::len", "src/chain.rs::new",
    "src/chain.rs::next", "src/chain.rs::next_back", "src/context.rs::context",
    "src/context.rs::ext_context", "src/context.rs::fmt",
    "src/context.rs::source", "src/context.rs::with_context",
    "src/context.rs::write_str", "src/ensure.rs::__dispatch_ensure",
    "src/ensure.rs::as_str", "src/ensure.rs::new", "src/ensure.rs::render",
    "src/ensure.rs::write_str", "src/error.rs::as_ref",
    "src/error.rs::backtrace", "src/error.rs::chain", "src/error.rs::construct",
    "src/error.rs::construct_from_adhoc", "src/error.rs::construct_from_boxed",
    "src/error.rs::construct_from_context",
    "src/error.rs::construct_from_display", "src/error.rs::construct_from_std",
    "src/error.rs::context", "src/error.rs::context_chain_downcast",
    "src/error.rs::context_downcast", "src/error.rs::deref",
    "src/error.rs::deref_mut", "src/error.rs::downcast",
    "src/error.rs::downcast_mut", "src/error.rs::downcast_ref",
    "src/error.rs::drop", "src/error.rs::erase", "src/error.rs::error",
    "src/error.rs::fmt", "src/error.rs::from", "src/error.rs::from_boxed",
    "src/error.rs::is", "src/error.rs::msg", "src/error.rs::new",
    "src/error.rs::no_backtrace", "src/error.rs::object_downcast",
    "src/error.rs::object_ref", "src/error.rs::source", "src/error.rs::vtable",
    "src/fmt.rs::debug", "src/fmt.rs::write_str", "src/kind.rs::anyhow_kind",
]
_CHAIN = frozenset(FnLocator(*n.split("::", 1)) for n in _CHAIN_NAMES)
_CONSTRUCT = FnLocator("src/error.rs", "construct")
# `t1..t101` is the harness's oracle (test identity is MODELLED, see its
# docstring); the reverse map anchors a vanished name to the attributed unit
# exactly as the live map anchors the crashed test to `Error::new`.
_REVERSE_MAP = {f"t{i}": {("src/error.rs", "new")} for i in range(80, 102)}


def _asan_site():
    """The recorded site, field for field (`phase_c.crash.localized`, iters 1
    and 2, identical): frames present, origin frames EMPTY, origin-capable —
    so `blamed` is None, `blamed_locators` is empty, and
    `crash_evidence_by_locator` still keys by the two test frames."""
    from pipeline_core.domain.crash_site import CrashFrame, CrashSite
    return CrashSite(
        binary_label="unittests src/lib.rs",
        test_name=f"error::gen_tests_4::{_CRASHED_TEST}",
        signal="AddressSanitizer: SEGV",
        frames=(CrashFrame(file_rel="src/error.gen_tests_4.rs", line=14,
                           fn_name=_CRASHED_TEST, symbol=_CRASHED_TEST),
                CrashFrame(file_rel="src/error.gen_tests_4.rs", line=11,
                           fn_name="{closure#0}", symbol="{closure#0}")),
        origin_frames=(), origin_capable=True,
        raw_report=("==266134==ERROR: AddressSanitizer: SEGV on unknown "
                    "address 0x00170000017b\n    #0 in "
                    "<alloc::raw_vec::RawVecInner>::non_null::<u8>"))


def _recording_retrigger():
    """The recorded outcome: the walk never fired once in 308 minutes. Every
    `CrashEvidence` it was handed is captured for the assertions below."""
    from pipeline_core.application.retrigger_layout import RetriggerResult
    seen: list[Any] = []

    def _retrig(pd, mod, cl, sd, output, crash_evidence=None, **_kw):
        seen.append(crash_evidence)
        return RetriggerResult(fired=False,
                               reason="no component matched the evidence")
    return _retrig, seen


def _replay(monkeypatch, tmp_path):
    walk = load_walk("263217-d41a5bb5")
    retrig, seen = _recording_retrigger()
    events: list[tuple[str, dict]] = []
    logs = replay(walk, monkeypatch, tmp_path, events=events,
                  retrigger=retrig, reverse_map=_REVERSE_MAP,
                  localize=lambda *a, **k: (_asan_site(),),
                  escalate=lambda src, tname, units: {k: {tname}
                                                      for k in _CHAIN},
                  inventory=_INVENTORY)
    return logs, events, seen


def _sixteen_records(mega_names: frozenset[str]):
    """The cluster's registry as the aim walks it: 16 non-GATED rows with the
    component that declares `construct` LAST, so nothing about registry order
    can be what selects it."""
    from pipeline_core.application.author_layout import (
        LayoutComponentRecord,
        LayoutOutcome,
    )
    others = ["Quoted", "Indented", "Buf", "StdError", "Adhoc", "AdhocKind",
              "Boxed", "BoxedKind", "Display", "MessageError", "Trait",
              "ContextError", "ChainState", "Chain", "Kind"]
    return [LayoutComponentRecord(frozenset({n}), LayoutOutcome.FROZEN)
            for n in others] + [
        LayoutComponentRecord(mega_names, LayoutOutcome.FROZEN)]


def _aim(evidence):
    """Drive the REAL `retrigger_layout` with the evidence the driver built,
    against the live cluster's component registry. Returns (result, fakes)."""
    from pipeline_core.application.retrigger_layout import retrigger_layout
    from tests.test_retrigger_layout import _CRATE, _MEGA_NAMES, _mega_fakes
    f = _mega_fakes(compiles=False)          # authoring stubbed to REJECTED
    f._records = _sixteen_records(_MEGA_NAMES)
    return retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, "",
                            f.deps(), crash_evidence=evidence), f


def test_the_aim_is_handed_the_origin_blame_and_the_chain(
        tmp_path: Path, monkeypatch) -> None:
    """The seam itself. The localizer's report keys two `gen_tests` locators
    into the crash EVIDENCE — that is unchanged and correct, they are the
    access path — but what reaches the aim as BLAME is `blamed_locators`,
    which for an unattributed origin is empty."""
    logs, events, seen = _replay(monkeypatch, tmp_path)

    chain_evs = [d for ev, d in events if ev == "phase_c.crash.chain_blame"]
    assert chain_evs, "the frame-blind sweep synthesized nothing"
    assert chain_evs[0]["provenance"] == "chain_derived"
    assert "src/error.rs::construct" in chain_evs[0]["blamed"]
    assert any("CHAIN-DERIVED" in ln for ln in logs)

    handed = [e for e in seen if e is not None]
    assert handed, "the fire gate was never reached"
    ev = handed[0]
    # The fix, in one assertion: the crashing test's own frames are NOT blame.
    assert ev.blamed_fns == frozenset()
    assert not (ev.blamed_fns & _RESIDUE)
    assert _CONSTRUCT in ev.chain_blamed


def test_the_captured_evidence_reaches_the_construct_component(
        tmp_path: Path, monkeypatch) -> None:
    """Reachability, which is all a walk may assert: the evidence the driver
    now builds, handed to the real action over the real 16-row registry,
    grades OWNS and charges an attempt at the component the chain implicates —
    the one the run's 308 minutes never reached."""
    _logs, _events, seen = _replay(monkeypatch, tmp_path)
    evidence = next(e for e in seen if e is not None)

    res, f = _aim(evidence)
    assert res.fired, "the aim declined the evidence it was built for"
    assert any("the observed fault is inside code 1 component(s) declare" in ln
               for ln in f.logs)
    assert f.committed and "attempt 1" in f.committed[0]
    assert "object_reallocate_boxed" in f.committed[0]   # the mega component
    assert not any("no component matched the evidence" in ln for ln in f.logs)


def _pre_fix_payload(seen) -> Any:
    """The `CrashEvidence` the PRE-FIX driver built from this same sweep:
    `crash_texts` minus the chain keys, which for a site whose only frames are
    the crashed test's own is exactly the two `gen_tests` locators."""
    from dataclasses import replace as dc_replace

    from pipeline_core.domain.crash_site import crash_evidence_by_locator
    # Proved from the recorded site rather than assumed: the evidence keys of
    # this crash ARE the two test frames, and the chain keys are disjoint from
    # them, so `{k for k in crash_texts if k not in _chain_added}` is `_RESIDUE`.
    assert frozenset(crash_evidence_by_locator((_asan_site(),))) == _RESIDUE
    assert not (_RESIDUE & _CHAIN)
    post_fix = next(e for e in seen if e is not None)
    pre_fix = dc_replace(post_fix, blamed_fns=_RESIDUE)
    assert pre_fix.chain_blamed == post_fix.chain_blamed
    return pre_fix


def _pre_fix_grade(evidence, fn_records, component_names):
    """`crash_candidate_grade` as it stood before this fix, verbatim: a
    non-empty `blamed_fns` RETURNS, so the chain branch below it is
    unreachable whenever any frame residue survives."""
    from pipeline_core.domain.layout_retrigger import (
        BlameGrade,
        crash_blame_grade,
    )
    if not evidence.vanished:
        return BlameGrade.NONE
    if evidence.blamed_fns:
        return crash_blame_grade(
            evidence.blamed_fns, fn_records, component_names)
    if evidence.chain_blamed:
        return crash_blame_grade(
            evidence.chain_blamed, fn_records, component_names)
    if not evidence.suspect_fns:
        return BlameGrade.NONE
    return crash_blame_grade(
        evidence.suspect_fns, fn_records, component_names)


def test_negative_control_the_pre_fix_walk_reproduces_the_silent_decline(
        tmp_path: Path, monkeypatch) -> None:
    """The recorded run, in miniature: the pre-fix PAYLOAD graded by the
    pre-fix CODE. Both halves are needed — the residue returns NONE for every
    one of the 16 rows, `top_blame_tier` is NONE, the candidate list empties,
    and the walk takes the exit that never printed. Nothing charged, nothing
    committed, no authoring call: 308 GPU-minutes of exactly this.

    The one recorded property that does NOT reproduce is the silence itself,
    deliberately — the declined walk now names the route, the tier and the
    evidence shape, which is the diagnosis the run went five hours without."""
    import pipeline_core.application.retrigger_layout as _rl
    _logs, _events, seen = _replay(monkeypatch, tmp_path)
    monkeypatch.setattr(_rl, "crash_candidate_grade", _pre_fix_grade)

    res, f = _aim(_pre_fix_payload(seen))
    assert not res.fired
    assert res.reason == "no component matched the evidence"
    assert f.committed == []          # no attempt charged, no checkpoint
    assert f.prompts == []            # no layout-authoring model call spent
    assert f.reverted_calls == []
    declined = [ln for ln in f.logs if "no component matched the evidence" in ln]
    assert len(declined) == 1
    assert "blame tier NONE" in declined[0]
    assert "0 of 16 non-GATED record(s)" in declined[0]
    assert "2 blamed frame(s)" in declined[0]


def test_the_domain_fall_through_alone_rescues_the_pre_fix_payload(
        tmp_path: Path, monkeypatch) -> None:
    """The two halves are separable, and this is the seam-hardening one. Feed
    the PRE-FIX payload to the CURRENT grade: a frame that implicates nothing
    while a chain inference exists no longer decides, so the same evidence
    that silenced the run for 308 minutes now reaches the component the chain
    names. The driver fix removes the residue; this makes a residue harmless
    if one ever returns."""
    _logs, _events, seen = _replay(monkeypatch, tmp_path)
    res, f = _aim(_pre_fix_payload(seen))
    assert res.fired
    assert any("the observed fault is inside code 1 component(s) declare" in ln
               for ln in f.logs)
