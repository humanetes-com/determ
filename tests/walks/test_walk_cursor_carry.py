"""Walk `2773540-e47c3754` — a code fix must not re-spend the traversal it
could not have affected, and must still re-offer the attempts it could.

The recorded run is the flat-score guard's one-batch-per-relaunch crawl: it
reads, dispatches a slice, stops. Across 2026-08-17 -> 08-20 that crawl was
restarted at 0 sixteen times in four days, because every session shipped a fix
and `cursor_mismatch` dropped the whole cursor on any `code_id` move — and
`code_identity` hashes pipeline.py plus every `.py` under `pipeline_core`.

What a walk may assert: reachability, never the score. Here the decision is
which units the FIRST batch after a code move contains. Under the shipped rule
it is the units the stopped process never reached plus the ones whose attempt
produced nothing; under the pre-fix rule (the negative control below, injected
and observed to fail) it is byte-for-byte the batch the previous process
already sent — the lock.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline_core.domain.models import ClusterFillResult, FnLocator
from tests.walks.harness import load_walk, replay

pytestmark = pytest.mark.walk

_WALK = "2773540-e47c3754"
_U1 = FnLocator("src/a.rs", "f")        # dispatched first; its body will move
_U2 = FnLocator("src/b.rs", "g")        # dispatched second; nothing comes back
_U3 = FnLocator("src/c.rs", "h")        # never reached by the stopped process
_FRONTIER = {_U1: {"t2"}, _U2: {"t3"}, _U3: {"t4"}}


def _process(monkeypatch, tmp_path: Path, floor: dict,
             ) -> tuple[list[str], list[tuple[FnLocator, ...]]]:
    """One process over the recorded readings, dispatch cap 1 so the pass is
    a crawl and the cursor is a partial traversal — the shape the stop leaves
    behind on the live run."""
    batches: list[tuple[FnLocator, ...]] = []
    landed: set[FnLocator] = set()

    def _fill(**kw):
        keys = tuple(kw.get("tdd_frontier_keys") or ())
        batches.append(keys)
        # Only `_U1` answers. `_U2` is the routing-drop / splice-refusal class:
        # dispatched, a model call spent, and the on-disk body unchanged.
        if _U1 in keys:
            landed.add(_U1)
        return ClusterFillResult(attempts=1, fns_filled=len(keys))

    def _enumerate(_src, **_kw):
        """The extractor's records, so the loop's body comparison can read a
        landed change. `body_text` moves for `_U1` once it has been filled."""
        return [
            {"file": k.file, "fn_name": k.fn_name,
             "body_text": ("filled()" if k in landed else "unimplemented!()")}
            for k in (_U1, _U2, _U3)]

    logs = replay(load_walk(_WALK), monkeypatch, tmp_path, events=[],
                  gpu_guard="2", max_stalls="9", floor=floor,
                  dispatch_cap="1", frontier=dict(_FRONTIER),
                  enumerate_fns=_enumerate, fill=_fill)
    return logs, batches


def _stopped_process(monkeypatch, tmp_path: Path) -> dict:
    """Process 1: the recorded run. It crawls a batch at a time and the guard
    stops it mid-pass, leaving a cursor on disk."""
    floor: dict = {}
    logs, batches = _process(monkeypatch, tmp_path, floor)
    assert any("GPU_GUARD_STOP" in ln for ln in logs), (
        "the walk's own readings must still stop this process mid-pass")
    assert batches and batches[0] == (_U1,), (
        f"cap 1 means one unit a batch, leaf-first: {batches}")
    row = floor.get("row") or {}
    assert row.get("attempted") == ["src/a.rs::f"], (
        f"the stop must leave a cursor on disk: {row.get('attempted')}")
    assert row.get("landed") == ["src/a.rs::f"], (
        f"only the unit whose body moved is landed: {row.get('landed')}")
    # This walk's readings stop the process after ONE batch — the guard fires
    # at iter 2, before its dispatch — so the recorded cursor holds one unit.
    # A crawl that got two batches in leaves the same row with a second
    # attempted unit and no landed record for it: dispatched, a model call
    # spent, nothing back. That second unit is added here rather than
    # simulated by a second reading, because the walk is a FIXED observation
    # sequence and lengthening it would be inventing readings the run never
    # produced. What is asserted on it is a dispatch decision, not a score.
    row["attempted"] = ["src/a.rs::f", "src/b.rs::g"]
    # The relaunch runs a DIFFERENT build — that is the whole transition under
    # test, and it is the one the record shows sixteen times in four days.
    row["code_id"] = "MOVED-BY-A-FIX"
    floor["row"] = row
    return floor


def test_a_code_move_carries_the_tried_units_and_re_offers_the_rest(
        monkeypatch, tmp_path: Path) -> None:
    """THE FIX, on the recorded readings: the resumed process does not re-send
    the unit that already answered, and does not defer the unit that answered
    with nothing."""
    floor = _stopped_process(monkeypatch, tmp_path)
    attempted_before = set(floor["row"]["attempted"])
    logs, batches = _process(monkeypatch, tmp_path, floor)

    assert batches, "the resumed process dispatched nothing"
    assert _U1 not in batches[0], (
        f"a unit whose body already moved must not be re-sent in the same "
        f"pass: {batches[0]}")
    assert batches[0] == (_U2,), (
        f"the released unit — dispatched, nothing came back — is offered "
        f"again immediately rather than deferred to the next pass: {batches[0]}")
    assert any("released" in ln and "for retry" in ln for ln in logs), (
        "a release that is not reported reads as a cursor that shrank")
    assert "src/b.rs::g" in attempted_before, (
        "sanity: the released unit really was on the persisted cursor")


def test_negative_control_the_old_rule_re_sends_the_identical_batch(
        monkeypatch, tmp_path: Path) -> None:
    """The pre-fix rule injected into the same replay: `code_id` moved, so the
    cursor is dropped whole, `order_frontier` is deterministic, and the
    relaunch re-sends the batch the stopped process had already sent. This is
    the lock the fix removes, and it must reproduce here or the test above has
    proved nothing."""
    import pipeline_core.domain.tdd_floor as _floor_mod
    from pipeline_core.domain.tdd_floor import CursorCarry

    floor = _stopped_process(monkeypatch, tmp_path)

    def _pre_fix_carry(saved, *, cluster: str, code_id: str) -> CursorCarry:
        if saved.cluster != cluster or saved.code_id != code_id:
            return CursorCarry(carried_all=False)
        return CursorCarry(attempted=saved.attempted, landed=saved.landed,
                           declined=saved.declined)

    monkeypatch.setattr(_floor_mod, "cursor_carry", _pre_fix_carry)
    # Service ranking is a different lock: never-served-first would itself
    # skip `f` (it is in the floor's served record) and the first batch
    # would be `g` under BOTH the old cursor rule and the new one — so this
    # control could not fail. Identity-ranking restores the isolation the
    # cursor rule needs.
    def _identity(ordered, served, flat, dead=()):  # noqa: ARG001
        return list(ordered)

    monkeypatch.setattr(
        "pipeline_core.application.reconstruct.phase_c_tdd.promote_by_service",
        _identity,
    )
    logs, batches = _process(monkeypatch, tmp_path, floor)
    assert batches and batches[0] == (_U1,), (
        f"negative control: the old rule re-sends the identical first batch, "
        f"got {batches[0] if batches else None}")
    assert not any("released" in ln and "for retry" in ln for ln in logs)
