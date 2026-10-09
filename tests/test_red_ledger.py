"""Every cluster end says, per red test, whether the pipeline or the model
held it (`domain/red_ledger`).

Run `39786-fd38e72f` (2026-10-09) left `stable` at 137/165 with 28 red tests
and no record of which had been offered to a writer, which bodies a gate
refused, or which never compiled."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pipeline_core.adapters.red_ledger_store import load_red_ledger, save_red_ledger
from pipeline_core.application.tasks.task3 import red_ledger_lines
from pipeline_core.domain.candidate_tiers import AskLedger, due_reading
from pipeline_core.domain.red_ledger import (
    PIPELINE_CLASSES,
    RedClass,
    RunFacts,
    red_ledger,
)

_SLICES = {"t": frozenset({"src/a.rs::f", "src/a.rs::g"})}
_REACH = {"src/a.rs::f": frozenset({"F"}), "src/a.rs::g": frozenset({"F", "G"})}


def _reading(ledger: AskLedger, **kw):  # type: ignore[no-untyped-def]
    return due_reading(["t"], slices=_SLICES, reach=_REACH,
                       evidence={"t": "left: 1 right: 2"}, held={},
                       ledger=ledger, **kw)


def _klass(ledger: AskLedger, runs=(), **kw) -> RedClass:  # type: ignore[no-untyped-def]
    reading = _reading(ledger, **kw)
    (entry,) = red_ledger(["t"], reading=reading, ledger=ledger, runs=runs)
    return entry.klass


def _spend(ledger: AskLedger, *, land: bool, reason: str = "") -> None:
    for _ in range(6):
        r = _reading(ledger)
        if r.exhausted:
            return
        for t, tr in r.by_test.items():
            for u, fp in tr.owed.items():
                if land:
                    ledger.record(u, t, fp)
                else:
                    for _m in range(3):
                        ledger.miss(u, t, fp, reason)


def test_nothing_asked_or_offered_is_never_offered() -> None:
    assert _klass(AskLedger()) is RedClass.NEVER_OFFERED


def test_landed_and_still_red_with_nothing_left_is_the_models() -> None:
    led = AskLedger()
    _spend(led, land=True)
    assert _klass(led) is RedClass.MODEL_LIMIT


def test_every_offer_failing_to_compile_is_compile_loss() -> None:
    led = AskLedger()
    _spend(led, land=False, reason="compile")
    assert _klass(led) is RedClass.COMPILE_LOSS


def test_offers_no_route_planned_are_never_offered() -> None:
    led = AskLedger()
    _spend(led, land=False, reason="not_planned")
    assert _klass(led) is RedClass.NEVER_OFFERED


def test_a_reverted_run_that_turned_it_green_is_a_refused_gain() -> None:
    led = AskLedger()
    _spend(led, land=True)
    run = RunFacts(label="pair a+b", reverted=frozenset({"src/a.rs::f"}),
                   newly_red=frozenset({"sibling"}),
                   newly_green=frozenset({"tests/x.rs::t"}))
    assert _klass(led, runs=[run]) is RedClass.REFUSED_GAIN
    kept = RunFacts(label="kept", reverted=frozenset(),
                    newly_red=frozenset(), newly_green=frozenset({"t"}))
    assert _klass(led, runs=[kept]) is RedClass.MODEL_LIMIT, \
        "a run that was kept refused nothing"


def test_withdrawn_wins_and_open_is_what_is_still_owed() -> None:
    assert _klass(AskLedger(), withdrawn={"t"}) is RedClass.WITHDRAWN
    led = AskLedger()
    r = _reading(led)
    for t, tr in r.by_test.items():
        for u, fp in tr.owed.items():
            led.record(u, t, fp)
    assert _klass(led) is RedClass.OPEN, "one tier asked, another still owed"


def test_the_pipeline_classes_are_named() -> None:
    assert {RedClass.REFUSED_GAIN, RedClass.COMPILE_LOSS,
                                RedClass.NEVER_OFFERED} == PIPELINE_CLASSES


def test_a_route_row_reads_as_run_facts() -> None:
    row = SimpleNamespace(label="file src/a.rs", reverted=(("src/a.rs", "f"),),
                          newly_red=("s",), newly_green=("t",))
    facts = RunFacts.of_row(row)
    assert facts.reverted == {"src/a.rs::f"} and facts.newly_green == {"t"}


def test_task3_prints_the_stored_ledger(tmp_path: Path) -> None:
    led = AskLedger()
    _spend(led, land=True)
    entries = red_ledger(["t"], reading=_reading(led), ledger=led)
    save_red_ledger(tmp_path, "stable", [e.to_dict() for e in entries],
                    run_id="r", close_kind="cluster_failed")
    lines = red_ledger_lines(load_red_ledger(tmp_path, "stable"))
    assert "model_limit 1" in lines[0] and "cluster_failed" in lines[0]
    assert any("model_limit: t" in line for line in lines)
    assert red_ledger_lines(load_red_ledger(tmp_path, "nightly")) == [
        "red ledger: none written by t2a for this cluster"]
