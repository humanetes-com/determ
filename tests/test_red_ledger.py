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
from pipeline_core.domain.test_sight import Sight, SightReading, UnseenReason

_NOT_TAKEN = SightReading(taken=False)
_SLICES = {"t": frozenset({"src/a.rs::f", "src/a.rs::g"})}
_REACH = {"src/a.rs::f": frozenset({"F"}), "src/a.rs::g": frozenset({"F", "G"})}


def _reading(ledger: AskLedger, **kw):  # type: ignore[no-untyped-def]
    return due_reading(["t"], slices=_SLICES, reach=_REACH,
                       evidence={"t": "left: 1 right: 2"}, held={},
                       ledger=ledger, **kw)


def _klass(ledger: AskLedger, runs=(), words=None, **kw) -> RedClass:  # type: ignore[no-untyped-def]
    reading = _reading(ledger, **kw)
    (entry,) = red_ledger(["t"], reading=reading, ledger=ledger, runs=runs,
                          words=words, sight=_NOT_TAKEN)
    return entry.klass


def _tier_units(ledger: AskLedger) -> list[str]:
    reading = _reading(ledger)
    out: list[str] = []
    for tr in reading.by_test.values():
        for tier in tr.tiers:
            out.extend(sorted(tier))
        out.extend(sorted(tr.held_by))
    return list(dict.fromkeys(out))


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
            RedClass.UNSEEN, RedClass.NEVER_OFFERED,
            RedClass.EVIDENCE_MISSING} == PIPELINE_CLASSES


def test_an_unseen_test_is_a_measurement_gap_ahead_of_never_offered() -> None:
    """A red test the exit sweep never observed executing closes UNSEEN with
    the reason, not NEVER_OFFERED: the gap is the measurement's, and the
    entry says which. Negative control: the same reading with sight not
    taken closes as before."""
    led = AskLedger()
    reading = due_reading(["orphan"], slices={}, reach={}, evidence={},
                          held={}, ledger=led)
    sight = SightReading({"orphan": Sight(
        "orphan", reason=UnseenReason.NOT_BUILT_IN_PROBED_WORLD,
        detail="failed in nightly; probed stable")})
    (e,) = red_ledger(["orphan"], reading=reading, ledger=led, sight=sight)
    assert e.klass is RedClass.UNSEEN and e.klass in PIPELINE_CLASSES
    assert "not_built_in_probed_world" in e.why and "nightly" in e.why
    (old,) = red_ledger(["orphan"], reading=reading, ledger=led,
                        sight=SightReading(sight.by_test, taken=False))
    assert old.klass is RedClass.NEVER_OFFERED


def test_a_repaired_compile_miss_is_the_models_when_nothing_is_left() -> None:
    """A compile miss the repair saw is not compile loss. With the rungs
    spent and no words supplied, the test is the model's."""
    led = AskLedger()
    _spend(led, land=False, reason="compile")
    for u in _tier_units(led):
        led.note_repair(u, "t", "fp", "saw the diagnostic")
    assert _klass(led) is RedClass.MODEL_LIMIT


def test_one_unrepaired_compile_miss_is_compile_loss_before_it_is_spent() -> None:
    """One offer that did not compile, with no repair, is compile loss
    even while a rung is still owed. The test's words do not hide it."""
    led = AskLedger()
    reading = _reading(led)
    u, fp = next(iter(reading.by_test["t"].owed.items()))
    led.miss(u, "t", fp, "compile")
    assert _klass(led) is RedClass.COMPILE_LOSS
    assert _klass(led, words={"t": "cannot find type `Invented`"}) \
        is RedClass.COMPILE_LOSS


def test_a_real_ask_that_omitted_the_tests_words_is_evidence_missing() -> None:
    """Words the driver has, and that no ask carried, are evidence missing.
    Carrying them, with the rungs spent, is the model's limit. Omitting the
    words map (the caller has no evidence) stays the model's."""
    led = AskLedger()
    _spend(led, land=True)
    assert _klass(led) is RedClass.MODEL_LIMIT
    assert _klass(led, words={"t": "left: 1 right: 2"}) \
        is RedClass.EVIDENCE_MISSING
    fp = _reading(led).fingerprints["t"]
    for u in _tier_units(led):
        led.note_words(u, "t", fp)
    assert _klass(led, words={"t": "left: 1 right: 2"}) is RedClass.MODEL_LIMIT


def test_a_repaired_compile_that_omitted_the_words_is_evidence_missing() -> None:
    led = AskLedger()
    _spend(led, land=False, reason="compile")
    for u in _tier_units(led):
        led.note_repair(u, "t", "fp", "saw")
    assert _klass(led, words={"t": "left: 1"}) is RedClass.EVIDENCE_MISSING


def test_a_joint_repair_does_not_stick_as_a_refused_gain() -> None:
    led = AskLedger()
    _spend(led, land=True)
    run = RunFacts(label="pair a+b", reverted=frozenset({"src/a.rs::f"}),
                   newly_red=frozenset({"sibling"}),
                   newly_green=frozenset({"tests/x.rs::t"}),
                   joint_repaired=True)
    assert _klass(led, runs=[run]) is RedClass.MODEL_LIMIT


def test_a_session_that_cannot_be_built_is_never_offered_ahead_of_compile_and_words() -> None:
    """``route_absent`` with no session landing is never offered, even when
    a unit rung compiled and the test's words were not carried."""
    led = AskLedger()
    reading = _reading(led)
    for u, fp in reading.by_test["t"].owed.items():
        led.record(u, "t", fp, rung="unit")
        led.miss(u, "t", fp, "compile", rung="unit")
        led.note_absent(u, "t", fp, "session")
        assert led.reason(u, "t", fp) == "compile"
        assert led.route_absent(u, "t")
    assert _klass(led, words={"t": "left: 1"}) is RedClass.NEVER_OFFERED


def test_a_bare_miss_with_no_stored_reason_is_a_real_ask() -> None:
    """A miss that stored no reason is still a run. Spent, with no words
    map, it is the model's limit, the same as a landing."""
    led = AskLedger()
    _spend(led, land=False)
    assert _klass(led) is RedClass.MODEL_LIMIT


def test_an_unread_test_and_a_test_outside_the_reading_are_never_offered() -> None:
    led = AskLedger()
    reading = due_reading(["orphan"], slices={}, reach={}, evidence={},
                          held={}, ledger=led)
    (unread,) = red_ledger(["orphan"], reading=reading, ledger=led,
                           sight=_NOT_TAKEN)
    assert unread.klass is RedClass.NEVER_OFFERED
    assert unread.why == "no function could be attributed to it"
    (missing,) = red_ledger(["not-in-reading"], reading=reading, ledger=led,
                            sight=_NOT_TAKEN)
    assert missing.klass is RedClass.NEVER_OFFERED
    assert missing.why == "no owed reading was taken for it"


def test_a_route_row_reads_as_run_facts() -> None:
    row = SimpleNamespace(label="file src/a.rs", reverted=(("src/a.rs", "f"),),
                          newly_red=("s",), newly_green=("t",))
    facts = RunFacts.of_row(row)
    assert facts.reverted == {"src/a.rs::f"} and facts.newly_green == {"t"}


def test_task3_prints_the_stored_ledger(tmp_path: Path) -> None:
    led = AskLedger()
    _spend(led, land=True)
    entries = red_ledger(["t"], reading=_reading(led), ledger=led,
                         sight=_NOT_TAKEN)
    save_red_ledger(tmp_path, "stable", [e.to_dict() for e in entries],
                    run_id="r", close_kind="cluster_failed")
    lines = red_ledger_lines(load_red_ledger(tmp_path, "stable"))
    assert "model_limit 1" in lines[0] and "cluster_failed" in lines[0]
    assert any("model_limit: t" in line for line in lines)
    assert red_ledger_lines(load_red_ledger(tmp_path, "nightly")) == [
        "red ledger: none written by t2a for this cluster"]


def test_a_test_still_held_in_a_stub_is_not_evidence_missing() -> None:
    """Its only words are the stub's own `not implemented`: there were no
    words about a body to carry. It read evidence_missing, a pipeline
    defect, for every stub-held test at a cluster's end."""
    sl = {"t": frozenset({"a::f", "a::stub"})}
    held = {"t": frozenset({"a::stub"})}
    led = AskLedger()
    r = due_reading(["t"], slices=sl, reach={}, evidence={}, held=held, ledger=led)
    led.record("a::stub", "t", r.fingerprints["t"], rung="unit")
    r = due_reading(["t"], slices=sl, reach={}, evidence={}, held=held, ledger=led)
    (e,) = red_ledger(["t"], reading=r, ledger=led, sight=_NOT_TAKEN,
                      words={"t": "thread 't' panicked at src/a.rs:3:5: not implemented"})
    assert e.klass is not RedClass.EVIDENCE_MISSING
