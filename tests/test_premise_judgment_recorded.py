"""The judgment keeps the dispatch premise — on run 2965-453dd7a4's rows.

Four escalation-only units landed at iter 1 with a recorded convicting
premise (7-19 tests each); the iter-2 sweep ran every binary and returned a
byte-identical vector. Judged on the reverse map alone — empty for a unit
the frontier holds through escalation or the owner map — each was graded
UNMEASURED with its premise erased; judged with the premise joined each is
FLAT, and the FLAT row then excludes the unit from dispatch at iter 3 while
its convictors are a subset of the premise. The fixture is the ledger's
landed rows and the sweep vectors as recorded; no function here reads a
unit name.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from pipeline_core.domain.body_ledger import (
    BodyState,
    LedgerEntry,
    LedgerKey,
    SweepView,
    classify,
    flat_standing_unchanged,
    judgment_attributed,
)
from pipeline_core.domain.dispatch_batch import drop_standing_flat, take_batch
from pipeline_core.domain.models import FnLocator

_FIXTURE = (Path(__file__).resolve().parent / "fixtures" / "body_ledger"
            / "2965_premise_judgment.json")


def _load() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _view(raw: dict) -> SweepView:
    return SweepView(passed=frozenset(raw["passed"]), failed=frozenset(raw["failed"]),
                     intact=bool(raw["reconciled"]), epoch=0)


def _rows(data: dict) -> list[LedgerEntry]:
    return [LedgerEntry(
        key=LedgerKey("anyhow", data["cluster"], r["file"], r["fn"]),
        body_sha=r["body_sha"], bodies=("<recorded>",), sig_sha="", layout_fp="",
        state=BodyState.LANDED, run_id=data["run_id"], iteration=1,
        oracle_fp=r["oracle_fp"], convicting=tuple(r["convicting"]))
        for r in data["landed_iter1"]]


def test_reverse_map_alone_grades_the_recorded_landings_unmeasured() -> None:
    """NEGATIVE CONTROL — the pre-fix read: an escalation-only unit has no
    reverse-map attribution, so `classify` sees no names and says nothing."""
    data = _load()
    before, after = _view(data["sweep_iter1"]), _view(data["sweep_iter2"])
    for row in _rows(data):
        state, delta = classify(row, attributed=frozenset(), before=before,
                                after=after, body_now_sha=row.body_sha)
        assert state is BodyState.UNMEASURED and delta == ()
        assert frozenset() & before.failed == frozenset()   # the erased premise


def test_premise_joined_grades_the_recorded_landings_flat_and_keeps_the_premise() -> None:
    data = _load()
    before, after = _view(data["sweep_iter1"]), _view(data["sweep_iter2"])
    assert after.intact and before.intact
    for row in _rows(data):
        names = judgment_attributed(frozenset(), row)
        assert names == frozenset(row.convicting)
        assert names <= after.ran, "the premise tests ran at iter 2"
        state, delta = classify(row, attributed=names, before=before,
                                after=after, body_now_sha=row.body_sha)
        assert state is BodyState.FLAT and delta == ()
        # The re-stamped premise the driver writes: names ∩ before.failed.
        assert names & before.failed == frozenset(row.convicting)


def test_the_flat_row_then_excludes_the_unit_while_its_convictors_are_in_the_premise() -> None:
    data = _load()
    before, after = _view(data["sweep_iter1"]), _view(data["sweep_iter2"])
    later = _view(data["sweep_iter3"])
    standing: dict[LedgerKey, LedgerEntry] = {}
    convict_now: dict[LedgerKey, frozenset[str]] = {}
    for row in _rows(data):
        names = judgment_attributed(frozenset(), row)
        state, _ = classify(row, attributed=names, before=before, after=after,
                            body_now_sha=row.body_sha)
        judged = replace(row, state=state, iteration=2,
                         convicting=tuple(sorted(names & before.failed)))
        standing[row.key] = judged
        convict_now[row.key] = frozenset(row.convicting) & later.failed
        assert convict_now[row.key], "still convicted at iter 3 by premise tests"
    oracle = data["landed_iter1"][0]["oracle_fp"]
    excluded = flat_standing_unchanged(standing, oracle_fp_now=oracle,
                                       convicting_now=convict_now)
    assert excluded == set(standing)
    # The dispatch-side effect on a cap-12 list that ranks these four first.
    ordered = [(FnLocator(k.file, k.fn_name), frozenset()) for k in standing]
    ordered += [(FnLocator("src/other.rs", f"u{i}"), frozenset()) for i in range(12)]
    flat = {FnLocator(k.file, k.fn_name) for k in excluded}
    batch = take_batch([k for k, _ in drop_standing_flat(ordered, flat)], set(), 12)
    assert not (set(batch.keys) & flat)
    # NEGATIVE CONTROL — the erased-premise rows (UNMEASURED, convicting=())
    # exclude nothing, and the four occupy the head of the batch.
    erased = {k: replace(e, state=BodyState.UNMEASURED, convicting=())
              for k, e in standing.items()}
    assert flat_standing_unchanged(erased, oracle_fp_now=oracle,
                                   convicting_now=convict_now) == set()
    control = take_batch([k for k, _ in ordered], set(), 12)
    assert set(control.keys) >= flat
