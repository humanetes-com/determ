"""Walk: run `3368-753757e6` (2026-08-15, cluster `stable`) — the graced
sweeps a landed fire's own window starved of crash evidence.

The recording: entry at 39 with the deterministic `unittests src/lib.rs`
SEGV live (`AddressSanitizer: SEGV ... no in-crate frame in the origin stack
(0 raw frame(s)) — blaming NOBODY`). The crash retrigger fired its second
and last `['StdError']` attempt (25 bodies reverted), which opened a
`RefillGrace` window of ceil(25/3) = 9 iterations. Every graced sweep then
printed the SAME 3 binaries `already crashed at baseline` — a reading taken
on the pre-revert tree, which the re-stub cannot explain — while the
`_grace.active` short-circuit kept `_crash_deterministic` unset and the
evidence gate's `not bodies_in_flight` term blocked the synthesis:
`grep -c CHAIN-DERIVED` over the run is 0 and `phase_c.crash.chain_blame`
appears 0 times in `logs/events.jsonl` for this run. Iter 4 measured what
that starvation bought: 156 fills, pass 39 -> 39, oracle 87 -> 79.

What the fix makes REACHABLE (never a score): a graced sweep whose vanished
set adds nothing beyond the reading the fire itself acted on
(`RefillGrace.explains_vanish` is False) arms `_crash_deterministic` and
satisfies the evidence gate, so chain-derived blame synthesizes on the FIRST
graced sweep — the fills the window protects run with the evidence pack.
The fire budget stays suppressed: the vanish streak is untouched inside the
window, so the walk asserts exactly one re-fill window ever opens.

Negative control: the pre-fix semantics — a window explains every vanish —
injected by forcing `explains_vanish` to True. The recorded outcome must
reproduce: zero chain_blame events and zero CHAIN-DERIVED lines across the
whole replay, the starvation the run actually lived.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pipeline_core.domain.models import FnLocator
from tests.walks.harness import load_walk, replay

pytestmark = pytest.mark.walk

# The run's real inventory and its chronic shrink: 107 expected, 87 reporting
# on the intact sweeps (the 3 crashed binaries hide 20 names).
_INVENTORY = 107
_CHRONIC = 20

_CHAIN_PRODUCER = FnLocator("src/error.rs", "construct_from_std")
# The reverse map anchors a chronically-vanished name, as the real map
# anchors `test_reallocate_into_boxed_dyn_error_without_backtrace`.
_REVERSE_MAP = {"t106": {("src/error.rs", "new")},
                "t107": {("src/error.rs", "new")}}


def _unattributed_site():
    """The graced sweeps' localizer output: origin-capable, looked, zero
    in-crate frames — blamed NOBODY."""
    from pipeline_core.domain.crash_site import CrashSite
    return CrashSite(
        binary_label="unittests src/lib.rs",
        test_name="t107",
        signal="AddressSanitizer: SEGV",
        frames=(), origin_frames=(), origin_capable=True,
        raw_report="SEGV on unknown address\n    #0 0x0 (<unknown module>)")


def _framed_site():
    """The pre-fire sweeps: the frame tier resolved `src/error.rs::drop`
    (the run's `crash:drop` blame), which aimed the recorded `['StdError']`
    fire — with a frame present the chain stands down by its ranking
    contract, so nothing synthesizes before the window opens."""
    from pipeline_core.domain.crash_site import CrashFrame, CrashSite
    frame = CrashFrame(file_rel="src/error.rs", line=560, fn_name="drop",
                       symbol="anyhow::Error::drop")
    return CrashSite(
        binary_label="unittests src/lib.rs", test_name="t107",
        signal="AddressSanitizer: SEGV", frames=(frame,),
        origin_frames=(frame,), origin_capable=False,
        raw_report="SEGV in anyhow::Error::drop")


def _localizer_of_the_recording():
    """The fire-aiming frame exists on the two pre-window sweeps (entry and
    the fire's own iteration); every graced sweep after blames NOBODY —
    the run's own sequence."""
    calls = [0]

    def _localize(*_a, **_k):
        calls[0] += 1
        return (_framed_site(),) if calls[0] <= 2 else (_unattributed_site(),)
    return _localize


def _run(monkeypatch, tmp_path):
    walk = load_walk("3368-753757e6")
    fills: list[dict[str, Any]] = []

    def _fill(**kw):
        fills.append(kw)
        from pipeline_core.domain.models import ClusterFillResult
        return ClusterFillResult(attempts=1, fns_filled=2)

    events: list[tuple[str, dict]] = []
    logs = replay(walk, monkeypatch, tmp_path, events=events,
                  reverse_map=_REVERSE_MAP,
                  localize=_localizer_of_the_recording(),
                  escalate=lambda src, tname, units: {
                      _CHAIN_PRODUCER: {tname}},
                  fill=_fill, inventory=_INVENTORY,
                  chronic_vanished=_CHRONIC)
    return logs, events, fills


def test_chain_blame_arms_on_the_first_graced_sweep(
        tmp_path: Path, monkeypatch) -> None:
    logs, events, fills = _run(monkeypatch, tmp_path)

    # Exactly one window opened (the recorded 25-body fire): arming the
    # evidence must not have re-armed the fire budget.
    assert sum(1 for ln in logs if "re-fill window open" in ln) == 1

    chain_evs = [d for ev, d in events if ev == "phase_c.crash.chain_blame"]
    assert chain_evs, "the graced sweeps synthesized nothing"
    # Loop iter 3 is the first sweep AFTER the fire (loop iter 2 = walk
    # iter 1, swallowed by the retrigger's continue) — armed inside the
    # window, not after it expires.
    assert chain_evs[0]["iter"] == 3
    assert chain_evs[0]["provenance"] == "chain_derived"
    assert any("CHAIN-DERIVED" in ln for ln in logs)
    # The refined gate said why, on the record.
    assert any("crash predates the re-stub" in ln for ln in logs)

    # The pack reached the graced fills — the channel that was empty on
    # every graced sweep of the recording.
    armed = [kw for kw in fills if kw.get("crash_evidence")
             and _CHAIN_PRODUCER in kw["crash_evidence"]]
    assert armed, "the evidence pack never armed"
    assert "CHAIN-DERIVED" in armed[0]["crash_evidence"][_CHAIN_PRODUCER]
    assert not armed[0].get("crash_blamed")


def test_negative_control_prefix_semantics_reproduce_the_starvation(
        tmp_path: Path, monkeypatch) -> None:
    from pipeline_core.domain import layout_retrigger as lr
    # Pre-fix: the window explains every vanish, unconditionally.
    monkeypatch.setattr(lr.RefillGrace, "explains_vanish",
                        lambda self, vanished: True)
    logs, events, fills = _run(monkeypatch, tmp_path)

    # The recorded outcome, reproduced: nothing synthesizes while the window
    # HOLDS (the recording's iters 3-4 both starved; at the run's ~3h/iter
    # that was ~20h of GPU) — pre-fix semantics only arm once it expires.
    # (Frame-derived evidence from the pre-fire sweeps is not the claim; only
    # the chain tier is under test.)
    #
    # THE WINDOW'S EXTENT IS READ FROM THE RUN, not asserted as 9 iterations.
    # It was `range(3, 3 + 9)` — the size the grant computes from ceil(25/3) —
    # and that stopped being the window's length on 2026-08-17, when the
    # re-fill window gained a second and third denominator: a wall-clock
    # ceiling and an early close once the re-climb regains the high-water it
    # was re-based from. This recording hits the second one. Its score dips
    # below 39 and recovers to exactly 39, which IS the mark the fire re-based
    # from, so the window closes at loop iter 5 with 8 of its 9 iterations
    # unspent and iters 5+ are simply not graced. Hard-coding the size made
    # this control assert a fact about the grant arithmetic; what it is FOR is
    # the fact that a graced sweep starves under pre-fix semantics, and that
    # claim has to be scoped to the sweeps that are actually graced or it
    # convicts the wrong mechanism the next time the window's length moves.
    armed_at = {d["iter"]
                for ev, d in events if ev == "phase_c.crash.chain_blame"}
    _closed = [d["iter"] for ev, d in events
               if ev == "phase_c.tdd.refill_window_closed"]
    _opened = [d["iter"] for ev, d in events
               if ev == "phase_c.tdd.refill_window_open"]
    assert len(_opened) == 1, "the walk's premise is one window"
    # Open from the grant's iteration up to and including the one that closed
    # it; unclosed means it ran to the end of the replay.
    window_iters = set(range(
        _opened[0] + 1, (_closed[0] if _closed else max(armed_at or {0})) + 1))
    assert window_iters, "no graced sweep — the control cannot convict"
    assert not (armed_at & window_iters), (
        f"pre-fix semantics armed inside the window: {sorted(armed_at)} "
        f"against graced sweeps {sorted(window_iters)}")
