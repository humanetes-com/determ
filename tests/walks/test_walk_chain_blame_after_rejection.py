"""Walk: run `929616-ee20d540` (2026-08-14, cluster `stable`) — the sweep the
frame-blind evidence pack could not reach.

The recording: entry at 79/101 with the deterministic `test_downcast`
heap-use-after-free live. Iter 1's gdb reproduction DID find an in-crate
frame (`src/error.rs::downcast_ref`), so the frame tier decided and the
chain stood down — its ranking contract, and correct. The retrigger then
fired at the 34-name component and was REJECTED (`rejection 2/3 … authored
Error drops member(s) … \\`ptr\\` (29 reads)`), which grants
`RefillGrace.for_failed_attempt` — one iter, nothing re-stubbed.

Iter 2 is what this walk is about. The localizer ran and logged
`AddressSanitizer: heap-use-after-free in tests/test_downcast.rs: no
in-crate frame in the origin stack (27 raw frame(s)) — blaming NOBODY`:
the exact state chain-derived blame exists for, with every site-side
precondition satisfied. It synthesized nothing. `grep -c CHAIN-DERIVED` over
that run's log is 0 and `phase_c.crash.chain_blame` appears 0 times in
`logs/events.jsonl`, because the synthesis was nested inside the
`_vanish_streak >= _crash_retrigger_after` fire gate: iter 1's fire had
reset the streak to 0, and the grace window then short-circuits the branch
that would raise it again. The branch was reachable only on sweeps where a
retrigger fires anyway — and the frame-blind sweep is, by construction, the
sweep after a fire. Those sweeps' fills ran with no crash evidence at all.

What the fix makes REACHABLE (never a score): the EVIDENCE half of the
synthesis — prompt evidence, frontier entry, exhaustion void, dispatch
eligibility — needs no fire, so it is keyed on the crash being deterministic
and on `not _grace.bodies_in_flight` instead. The AIM half (signature,
component choice) stays behind the fire budget.

The two tests below pin the seam from both sides, which is what makes either
of them evidence. The first fails against the pre-fix nesting (a rejected
fire's grace suppressed everything). The second fails against the lazy fix
"arm on every sweep" — a LANDED re-author re-stubs bodies, and until they
re-fill the shrink is the window's own doing, so its sweep may not be read
as crash evidence at all.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pipeline_core.domain.models import FnLocator
from tests.walks.harness import load_walk, replay

pytestmark = pytest.mark.walk

# The run's real inventory and its chronic shrink: 106 expected, 101
# reporting on every intact sweep.
_INVENTORY = 106
_CHRONIC = 5

_CHAIN_PRODUCER = FnLocator("src/error.rs", "construct_from_std")
_REVERSE_MAP = {"t102": {("src/error.rs", "new")}}


def _unattributed_site():
    """The iter-2 localizer output: origin-capable, looked, found no in-crate
    frame. Origin-unattributed sites SURVIVE the localizer — dropping them was
    the starvation the frame-blind class was named for."""
    from pipeline_core.domain.crash_site import CrashSite
    return CrashSite(
        binary_label="tests/test_downcast.rs", test_name="t102",
        signal="AddressSanitizer: heap-use-after-free",
        frames=(), origin_frames=(), origin_capable=True,
        raw_report="freed by thread T0 here:\n    #0 free asan.cpp:52")


def _framed_site():
    """Iter 1's recording: gdb reproduced the fault and DID resolve an in-crate
    frame, `src/error.rs::downcast_ref`. Reproduction of this class is
    launch-dependent — the killed run `635043-5f7c61f5` never got one — and
    with a frame present the chain stands down by its ranking contract."""
    from pipeline_core.domain.crash_site import CrashFrame, CrashSite
    frame = CrashFrame(file_rel="src/error.rs", line=308,
                       fn_name="downcast_ref",
                       symbol="anyhow::Error::downcast_ref")
    return CrashSite(
        binary_label="tests/test_downcast.rs", test_name="t102",
        signal="SIGABRT", frames=(frame,), origin_frames=(frame,),
        origin_capable=False, raw_report="Program received signal SIGABRT")


def _localizer_of_the_recording():
    """Iter 1 finds a frame, every later sweep does not — the run's own
    sequence, and the reason iter 2 is the sweep under test."""
    calls = [0]

    def _localize(*_a, **_k):
        calls[0] += 1
        return (_framed_site(),) if calls[0] == 1 else (_unattributed_site(),)
    return _localize


def _fire_once(*, reverted: int):
    """The recorded schedule: one fire on the first gate arrival, then spent.
    `reverted=0` is the recorded REJECTION (fired, nothing re-stubbed);
    `reverted>0` is a landed re-author, which is the control."""
    from pipeline_core.application.retrigger_layout import RetriggerResult
    seen: list[Any] = []

    def _retrig(pd, mod, cl, sd, output, crash_evidence=None, **_kw):
        seen.append(crash_evidence)
        if len(seen) == 1:
            return RetriggerResult(
                fired=True, succeeded=reverted > 0,
                rejected=reverted == 0, reverted_count=reverted,
                cause="RUNTIME_CRASH",
                reason="" if reverted else "authored Error drops `ptr`")
        return RetriggerResult(fired=False, reason="walk spent")
    return _retrig, seen


def _run(monkeypatch, tmp_path, *, reverted: int):
    walk = load_walk("929616-ee20d540")
    retrig, seen = _fire_once(reverted=reverted)
    fills: list[dict[str, Any]] = []

    def _fill(**kw):
        fills.append(kw)
        from pipeline_core.domain.models import ClusterFillResult
        return ClusterFillResult(attempts=1, fns_filled=2)

    events: list[tuple[str, dict]] = []
    logs = replay(walk, monkeypatch, tmp_path, events=events,
                  retrigger=retrig, reverse_map=_REVERSE_MAP,
                  localize=_localizer_of_the_recording(),
                  escalate=lambda src, tname, units: {
                      _CHAIN_PRODUCER: {tname}},
                  fill=_fill, inventory=_INVENTORY,
                  chronic_vanished=_CHRONIC)
    return logs, events, fills, seen


def test_a_rejected_fire_does_not_suppress_the_next_sweeps_chain_evidence(
        tmp_path: Path, monkeypatch) -> None:
    logs, events, fills, _seen = _run(monkeypatch, tmp_path, reverted=0)

    chain_evs = [d for ev, d in events if ev == "phase_c.crash.chain_blame"]
    assert chain_evs, "the frame-blind sweep synthesized nothing"
    # ITER 2 specifically: the sweep right after the rejected fire, inside the
    # window that used to swallow it. Arming eventually is not the claim.
    assert chain_evs[0]["iter"] == 2
    assert chain_evs[0]["provenance"] == "chain_derived"
    assert "src/error.rs::construct_from_std" in chain_evs[0]["blamed"]
    assert any("CHAIN-DERIVED" in ln for ln in logs)

    # The pack reached the fills — the channel the whole mechanism exists to
    # feed, and the one that was empty on every graced sweep of the run.
    armed = [kw for kw in fills if kw.get("crash_evidence")
             and _CHAIN_PRODUCER in kw["crash_evidence"]]
    assert armed, "the evidence pack never armed"
    text = armed[0]["crash_evidence"][_CHAIN_PRODUCER]
    assert "CHAIN-DERIVED" in text and "freed by thread T0" in text
    assert _CHAIN_PRODUCER in armed[0]["tdd_frontier_keys"]
    # An inference never convicts: it may steer a fill, not immunize a unit.
    assert not armed[0].get("crash_blamed")


def test_negative_control_a_landed_restub_still_suppresses_it(
        tmp_path: Path, monkeypatch) -> None:
    """The other side of the seam. A LANDED re-author reverted bodies to
    `unimplemented!()`; until they re-fill, a stub panic aborts the binary and
    the oracle shrinks REGARDLESS of the tree's quality, so that sweep is not
    evidence about the crash. `bodies_in_flight` is what tells the two windows
    apart — a rejection re-stubbed nothing — and a fix that armed on every
    sweep would synthesize blame from its own re-stub."""
    _logs, events, fills, _seen = _run(monkeypatch, tmp_path, reverted=8)

    chain_evs = [d for ev, d in events if ev == "phase_c.crash.chain_blame"]
    armed_at = {d["iter"] for d in chain_evs}
    # Nothing at iter 2 — where the rejected-fire run armed — nor anywhere else
    # in the window the 8 re-stubbed bodies bought. The suppression ENDS with
    # the window rather than lasting forever: the crash is real and the sweep
    # that measures a re-filled tree is evidence again.
    assert 2 not in armed_at, "synthesized blame from its own re-stub"
    assert armed_at and min(armed_at) > 2
