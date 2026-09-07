"""Walk: run `1577437-c93d5c26` (2026-09-06, cluster `stable`) — the chain
cohort whose head was re-bought every sweep because every crash-graded key
was exempt from the pass cursor.

The recording: entry at 104, iter 1 at `pass=97 fail=24`, iter 2 at
`pass=104 fail=17` with `ORACLE SHRUNK — counting 16 as failing` (12 named
tests with no verdict, `tests/error_gen_3.rs` dead of `AddressSanitizer:
SEGV`, origin UNATTRIBUTED, no in-crate frame in any stack). Iter 3 synthesized
CHAIN-DERIVED blame over 63 fns (`phase_c.crash.chain_blame`,
`chain_count=63`, `suspect_count=34`; the event lists 50) and the retrigger
fired attempt 2 on `Quoted` off that chain (the aim half, pinned in
`test_walk_chain_blame_aim`). Iter 4 then printed

    63 unit(s) carry crash evidence this sweep and rank ahead of the
    frontier; 12 of them are in this batch: src/ptr.rs::by_ref,
    src/error.rs::construct, src/error.rs::construct_from_adhoc, …

with `attempted_before=0` under the cohort fingerprint iter 2 had opened
(`cohort_fp=3344ccf4d5f4` at both) — the 12 units iter 2 had attempted were
all in the exempt set, so the cursor forgot them. Iter 4's
`phase_c.dispatch.service_order` lists `never_served=[]`: the never-served-
first order that had been advancing the head between iters 2 and 4 was
spent, the cursor was the only traversal left, and it was off. The run was
killed inside iter 4's fill.

What the fix makes REACHABLE (never a score): the chain-only keys are
cursor-bounded (`CrashCohort.cursor_exempt` is the frame-sited keys alone),
so within one pass no chain unit is offered twice, `attempted_before`
advances by the cap each sweep, and the pass COMPLETES (ceil(51/12) = 5
batches) — the two terminators denominated in completed passes can see it.
Site evidence keeps its exemption.

Negative control: the recorded exemption — every crash key, chain included —
injected by widening `CrashCohort.cursor_exempt` to `keys`. `attempted_before`
reads 0 on every sweep (the recorded reading), the pass never completes, the
fifth batch re-offers head units beside the 3 never-served leftovers, and
once every unit has been served the batch is the same 12 forever.

Modelled, beyond what the harness docstring already lists: the chronic shrink
is 16 names past the 121 the recorded rows report (`inventory=137`,
`chronic_vanished=16`), and the chain is the 50 names the event recorded, not
the 63 the log counted.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pipeline_core.domain.models import FnLocator
from tests.walks.harness import load_walk, replay

pytestmark = pytest.mark.walk

_INVENTORY = 137
_CHRONIC = 16
_CAP = 12
_REVERSE_MAP = {"t122": {("src/error.rs", "new")}}
_FIXTURE = (Path(__file__).resolve().parents[1] / "fixtures" / "crash"
            / "chain_blame_1577437-c93d5c26_2026-09-06_iter3.json")


def _recorded_chain() -> list[FnLocator]:
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))["event"]["data"]
    assert data["provenance"] == "chain_derived" and data["chain_count"] == 63
    return [FnLocator(*n.split("::", 1)) for n in data["blamed"]]


def _segv_site():
    """The recorded site (`phase_c.crash.localized`, iters 2, 3 and 4,
    identical): `tests/error_gen_3.rs`, `AddressSanitizer: SEGV`, frames
    EMPTY, origin frames EMPTY, origin-capable — blamed NOBODY."""
    from pipeline_core.domain.crash_site import CrashSite
    return CrashSite(
        binary_label="tests/error_gen_3.rs", test_name="t122",
        signal="AddressSanitizer: SEGV",
        frames=(), origin_frames=(), origin_capable=True,
        raw_report=("==1746084==ERROR: AddressSanitizer: SEGV on unknown "
                    "address 0x000400000124\n    #0 0x55ee8756b443 in "
                    "<alloc::raw_vec::RawVecInner>::non_null::<u8>"))


def _declining_retrigger():
    from pipeline_core.application.retrigger_layout import RetriggerResult

    def _retrig(pd, mod, cl, sd, output, crash_evidence=None, **_kw):
        return RetriggerResult(fired=False,
                               reason="no component matched the evidence")
    return _retrig


def _replay(monkeypatch, tmp_path):
    walk = load_walk("1577437-c93d5c26")
    chain = _recorded_chain()
    fills: list[dict[str, Any]] = []

    def _fill(**kw):
        fills.append(kw)
        from pipeline_core.domain.models import ClusterFillResult
        return ClusterFillResult(attempts=1, fns_filled=0)

    events: list[tuple[str, dict]] = []
    logs = replay(walk, monkeypatch, tmp_path, events=events,
                  retrigger=_declining_retrigger(), reverse_map=_REVERSE_MAP,
                  localize=lambda *a, **k: (_segv_site(),),
                  escalate=lambda src, tname, units, producers_out=None: {
                      k: {tname} for k in chain},
                  fill=_fill, inventory=_INVENTORY, chronic_vanished=_CHRONIC,
                  dispatch_cap=str(_CAP), backstop="9")
    return logs, events, fills, chain


def _chain_batches(fills, chain) -> list[frozenset[FnLocator]]:
    """The dispatched key sets of every fill that carried the chain as crash
    evidence, in order."""
    return [frozenset(kw["tdd_frontier_keys"]) for kw in fills
            if kw.get("crash_evidence")
            and set(chain) & set(kw["crash_evidence"])]


def test_the_chain_cohort_is_traversed_across_batches(
        tmp_path: Path, monkeypatch) -> None:
    logs, events, fills, chain = _replay(monkeypatch, tmp_path)
    assert [d for ev, d in events if ev == "phase_c.crash.chain_blame"], \
        "the frame-blind sweep synthesized nothing"
    batches = _chain_batches(fills, chain)
    assert len(batches) >= 5, "fewer than five chain-evidence dispatches"
    first_pass = batches[:5]
    offered = [k for b in first_pass for k in b]
    # No chain unit offered twice before every one has been offered once.
    assert len(offered) == len(set(offered))
    assert set(chain) <= set(offered)
    assert [len(b) for b in first_pass] == [_CAP, _CAP, _CAP, _CAP, 3]
    iters = [d for ev, d in events if ev == "phase_c.tdd.iter"
             and "attempted_before" in d]
    assert [d["attempted_before"] for d in iters[:5]] == [0, 12, 24, 36, 48]
    assert iters[4]["pass_completed"] is True
    assert not any(d["pass_completed"] for d in iters[:4])
    # The log line says how the cohort is split, and that the chain is
    # cursor-bounded.
    assert any(f"{len(chain)} chain-inferred, cursor-bounded" in ln
               and "0 frame-sited" in ln for ln in logs)


def test_negative_control_the_recorded_exemption_re_buys_the_head(
        tmp_path: Path, monkeypatch) -> None:
    """The recorded rule — every crash-graded key exempt — reproduces the
    recorded reading, `attempted_before=0` on every sweep, and shows what
    it costs once never-served-first has nothing left to advance."""
    from pipeline_core.domain.dispatch_batch import CrashCohort
    monkeypatch.setattr(CrashCohort, "cursor_exempt",
                        property(lambda self: self.keys))
    _logs, events, fills, chain = _replay(monkeypatch, tmp_path)
    batches = _chain_batches(fills, chain)
    assert len(batches) >= 7
    iters = [d for ev, d in events if ev == "phase_c.tdd.iter"
             and "attempted_before" in d]
    assert iters and {d["attempted_before"] for d in iters} == {0}
    assert not any(d["pass_completed"] for d in iters)
    # The fifth batch re-buys head units beside the never-served leftovers…
    head = batches[0]
    assert len(batches[4]) == _CAP and batches[4] & head
    # …and after that the batch is the head, every sweep.
    assert batches[5] == batches[6] == head
