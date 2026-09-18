"""Walk: run `635043-5f7c61f5` (2026-08-14, cluster `stable`) — the closed
cause-C walk against the frame-blind `test_downcast` heap-use-after-free.

The recording: entry at 79, two landed cause-C fires (`Quoted` reverted 3,
then a 0-body fire), and the walk then CLOSED — `the last 2 landed cause-C
re-author(s) each left this exact crash reproducing … A changed crash or a
new localizer blame changes the signature and clears it.` The unlock the
exit line names was unsatisfiable: every ASAN stack of the fault resolves to
std/test frames (`logs/asan/asan-20260814T025642115609.txt`, report at line
3877), so no frame parse could ever produce the "new localizer blame", the
evidence pack never armed, and ~5 of the 106-test oracle stayed locked out
on every run of the tree.

What the fix makes REACHABLE (never a score): with the localizer returning
the origin-unattributed site and the crash deterministic, the driver
synthesizes a CHAIN-DERIVED blame — and by the iteration after the second
recorded fire the evidence pack arms (dispatch carries the chain keys with
the raw report) and the retrigger receives a CrashEvidence whose signature
has moved off the one the walk's refutations were booked under.

Negative control: the pre-fix driver input — the old localizer dropped the
frames-empty site, so the driver saw empty localization. Replaying that
grading must reproduce the recorded outcome: no synthesis, no armed
evidence, one unchanged signature at every gate arrival (the walk stays
closed).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pipeline_core.domain.models import FnLocator
from tests.walks.harness import load_walk, replay

pytestmark = pytest.mark.walk

# The run's real inventory and its chronic shrink: 106 expected, 101
# reporting on every intact sweep ("capped at 101 by design").
_INVENTORY = 106
_CHRONIC = 5

_CHAIN_PRODUCER = FnLocator("src/error.rs", "construct_from_std")
_ATTRIBUTED = FnLocator("src/error.rs", "new")
# t102..t106 never report; the reverse map anchors one of them, as the real
# map anchors `test_drop` to `Error::new`.
_REVERSE_MAP = {"t102": {("src/error.rs", "new")}}


def _unattributed_site():
    from pipeline_core.domain.crash_site import CrashSite
    return CrashSite(
        binary_label="tests/test_downcast.rs", test_name="t102",
        signal="AddressSanitizer: heap-use-after-free",
        frames=(), origin_frames=(), origin_capable=True,
        raw_report="freed by thread T0 here:\n    #0 free asan.cpp:52")


def _recording_retrigger(reverted_by_fire: list[int]):
    """The recorded firing schedule (fire on the first N gate arrivals, then
    the walk is spent), recording every CrashEvidence it is handed."""
    from pipeline_core.application.retrigger_layout import RetriggerResult
    evidences: list[Any] = []

    def _retrig(pd, mod, cl, sd, output, crash_evidence=None, **_kw):
        evidences.append(crash_evidence)
        fired_so_far = len(evidences) - 1
        if crash_evidence is not None and fired_so_far < len(reverted_by_fire):
            return RetriggerResult(
                fired=True, succeeded=True,
                reverted_count=reverted_by_fire[fired_so_far],
                cause="RUNTIME_CRASH")
        return RetriggerResult(fired=False, reason="walk closed")
    return _retrig, evidences


def test_chain_blame_lands_and_the_evidence_pack_arms(
        tmp_path: Path, monkeypatch) -> None:
    from pipeline_core.domain.layout_retrigger import crash_signature
    walk = load_walk("635043-5f7c61f5")
    fires = [it["retrigger"]["reverted"]
             for it in walk["iterations"] if "retrigger" in it]
    retrig, evidences = _recording_retrigger(fires)
    fills: list[dict[str, Any]] = []

    def _fill(**kw):
        fills.append(kw)
        from pipeline_core.domain.models import ClusterFillResult
        return ClusterFillResult(attempts=1, fns_filled=2)

    events: list[tuple[str, dict]] = []
    logs = replay(walk, monkeypatch, tmp_path, events=events,
                  retrigger=retrig, reverse_map=_REVERSE_MAP,
                  localize=lambda *a, **k: (_unattributed_site(),),
                  escalate=lambda src, tname, units, producers_out=None: {
                      _CHAIN_PRODUCER: {tname}},
                  fill=_fill, inventory=_INVENTORY,
                  chronic_vanished=_CHRONIC)

    # The blame landed, marked as the inference it is.
    chain_evs = [d for ev, d in events if ev == "phase_c.crash.chain_blame"]
    assert chain_evs, "chain-derived synthesis never fired"
    assert chain_evs[0]["provenance"] == "chain_derived"
    assert "src/error.rs::construct_from_std" in chain_evs[0]["blamed"]
    assert any("CHAIN-DERIVED" in ln for ln in logs)

    # Both recorded fires happened; the signature the retrigger sees with
    # the chain landed differs from the blameless one those fires (and the
    # walk's refutations) were booked under — the exit line's own unlock.
    assert len(evidences) >= len(fires) + 1
    with_chain = [e for e in evidences if e is not None and e.chain_blamed]
    assert with_chain, "no CrashEvidence carried the chain"
    from dataclasses import replace as _dc_replace
    blameless = _dc_replace(with_chain[0], chain_blamed=frozenset())
    assert crash_signature(with_chain[0]) != crash_signature(blameless)

    # The evidence pack ARMED after the walk's recorded fires were spent:
    # a dispatch carried the chain key, its CHAIN-DERIVED text and the raw
    # report — while the conviction channel stayed empty.
    armed = [kw for kw in fills if kw.get("crash_evidence")
             and _CHAIN_PRODUCER in kw["crash_evidence"]]
    assert armed, "the evidence pack never armed"
    text = armed[0]["crash_evidence"][_CHAIN_PRODUCER]
    assert "CHAIN-DERIVED" in text and "freed by thread T0" in text
    assert _CHAIN_PRODUCER in armed[0]["tdd_frontier_keys"]
    assert not armed[0].get("crash_blamed")


def test_negative_control_old_grading_keeps_the_walk_closed(
        tmp_path: Path, monkeypatch) -> None:
    from pipeline_core.domain.layout_retrigger import crash_signature
    walk = load_walk("635043-5f7c61f5")
    fires = [it["retrigger"]["reverted"]
             for it in walk["iterations"] if "retrigger" in it]
    retrig, evidences = _recording_retrigger(fires)
    fills: list[dict[str, Any]] = []

    def _fill(**kw):
        fills.append(kw)
        from pipeline_core.domain.models import ClusterFillResult
        return ClusterFillResult(attempts=1, fns_filled=2)

    events: list[tuple[str, dict]] = []
    replay(walk, monkeypatch, tmp_path, events=events,
           retrigger=retrig, reverse_map=_REVERSE_MAP,
           localize=lambda *a, **k: (),   # the pre-fix driver input
           escalate=lambda src, tname, units, producers_out=None: {_CHAIN_PRODUCER: {tname}},
           fill=_fill, inventory=_INVENTORY, chronic_vanished=_CHRONIC)

    # The recorded outcome, reproduced: no synthesis, nothing armed, and one
    # unchanged signature at every gate arrival — exactly the constancy the
    # zero-payoff closure keys on, so the walk stays closed.
    assert not [d for ev, d in events if ev == "phase_c.crash.chain_blame"]
    assert all(e is None or not e.chain_blamed for e in evidences)
    sigs = {crash_signature(e) for e in evidences if e is not None}
    assert len(sigs) == 1
    assert not any(kw.get("crash_evidence") for kw in fills)
