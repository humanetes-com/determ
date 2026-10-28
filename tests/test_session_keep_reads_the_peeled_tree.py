"""A kept session's ledger row reads the tree the session left.

Reviewed 2026-10-28: the session keep recorded `final`, the best judged
turn's reading, which is taken before the peel. When the peel took back the
unit that turned an aimed test green, that test was red on the tree and in
neither the row's newly-green nor its still-failing set, so `aim_left_red`
could not see that the session left its aim red. The claim: after a peel the
row's terms are the peel's comparison; kept whole, they are `final`'s.
Control: `final`'s terms, which the keep used before, list the test as
newly green."""
from __future__ import annotations

from dataclasses import replace

from pipeline_core.application.reconstruct.slice_session import SessionOutcome
from pipeline_core.domain.models import FnSite
from pipeline_core.domain.slice_session import TargetStatus, TurnReading
from pipeline_core.domain.test_results import RegressionComparison

_U = FnSite("src/a.rs", "kept", "")
_FINAL = TurnReading(targets=(TargetStatus("t_aim", True),), pass_delta=2,
                     newly_green=("t_aim", "t_other"), still_failing=())


def _cmp(**kw: object) -> RegressionComparison:
    base: dict[str, object] = {
        "newly_red": frozenset(), "newly_crashed": (), "excluded_crashed": (),
        "not_run": (), "measured": (), "base_pass": 0, "cur_pass": 1,
        "base_executed": 3, "cur_executed": 3}
    base.update(kw)
    return RegressionComparison(**base)  # type: ignore[arg-type]


def _outcome(peeled: RegressionComparison | None) -> SessionOutcome:
    return SessionOutcome(
        outcome="kept", turns=2, model_calls=2, kept=frozenset({_U}),
        reverted=frozenset(), dispatched=frozenset({_U}), notes="",
        final=_FINAL, compile_failures=0, transcript_chars=0,
        peeled_comparison=peeled)


def test_a_peeled_keep_records_the_peeled_tree() -> None:
    peeled = _cmp(newly_green=frozenset({"t_other"}),
                  still_failing=frozenset({"t_aim"}))
    kc = _outcome(peeled).kept_comparison()
    assert kc is not None
    assert kc.still_failing == ("t_aim",)
    assert kc.newly_green == ("t_other",)
    assert kc.delta == peeled.net_pass_delta


def test_control_the_pre_peel_reading_hides_the_red_aim() -> None:
    kc = _outcome(None).kept_comparison()
    assert kc is not None
    assert kc.still_failing == ()
    assert "t_aim" in kc.newly_green
    assert kc.delta == 2


def test_nothing_kept_records_nothing() -> None:
    o = _outcome(None)
    assert replace(o, kept=frozenset()).kept_comparison() is None
