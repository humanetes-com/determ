"""The run-level LLM cut stop: the share of the process's last calls that
returned no decided answer, and the graceful stop it takes.

The defect it closes, computed on events.jsonl 2026-08-10 → 2026-09-21: 1,810
of 12,002 main-stream calls were cut (15.1%), 132/506 on 08-13 under a
half-sized thinking budget, back to 18/115 on 09-21 as prompts grew. The
per-call grade (a capped reply is UNMEASURED) is correct and is what hid the
rate: every capped call was quietly re-bought, and the class was found by hand
days late each time. Run `450977-5f48b68a` (2026-09-21) is the recorded
positive fixture — 3 of its first 25 calls cut; run `330242-0be2c3e1` is a
216-call run whose rate never reached 4% and must not stop.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline_core.domain import graceful_stop, reasoning_cap
from pipeline_core.domain.fill_context import route_scope
from pipeline_core.domain.reasoning_cap import (
    CallMark,
    CutRate,
    CutStopKnobs,
    bookkeeping_call,
    cut_stop_knobs_from_env,
    is_cut,
    mark_of,
    record_stream_meta,
)

FIXTURES = Path(__file__).parent / "fixtures" / "cut_rate"

OK = {"finish_reason": "stop", "stop": None, "followup": False,
      "content_chars": 900, "prompt_tokens": 30_000}
CAPPED = {"finish_reason": None, "stop": "think_cap", "followup": True,
          "content_chars": 700, "prompt_tokens": 38_000}


@pytest.fixture(autouse=True)
def _clean() -> None:
    graceful_stop.reset_for_tests()
    yield
    graceful_stop.reset_for_tests()


def _rows(run_id: str) -> list[dict]:
    return json.loads((FIXTURES / f"{run_id}.json").read_text())["rows"]


def _first_fire(rows: list[dict], knobs: CutStopKnobs) -> int | None:
    fold = CutRate(knobs.window, knobs.share_pct)
    for i, row in enumerate(rows, 1):
        fold = fold.add(mark_of(row, ""))
        if fold.fired:
            return i
    return None


def test_cut_is_capped_or_empty_and_a_closed_structure_is_not() -> None:
    assert not is_cut(OK)
    assert is_cut(CAPPED)
    assert is_cut({**OK, "finish_reason": "length", "followup": True})
    # A forced followup on an empty main reply: the main call decided nothing.
    assert is_cut({**OK, "followup": True})
    assert is_cut({**OK, "content_chars": 0})
    # The StopDetector ending a finished structure: no finish_reason, content.
    assert not is_cut({**OK, "finish_reason": None, "stop": "closed"})


def test_fold_fires_on_the_count_the_share_names_over_a_full_window() -> None:
    """25% of a 40-call window is 10 cut calls: not at 9, at the 10th,
    wherever the window has got to."""
    fold = CutRate(window=40, share_pct=25)
    assert fold.threshold == 10
    for _ in range(31):
        fold = fold.add(mark_of(OK, "file"))
    for _ in range(9):
        fold = fold.add(mark_of(CAPPED, "file"))
    assert fold.cut == 9 and not fold.fired
    fold = fold.add(mark_of(CAPPED, "session"))     # an OK slides out: 10 of 40
    assert fold.cut == 10 and fold.fired
    assert fold.by_route() == {"file": (9, 39), "session": (1, 1)}


def test_a_single_cut_call_never_fires_the_stop() -> None:
    """The defect this rule replaces: a share compared against a partial
    window has its configured value as its smallest non-zero reading, so at
    4%/100 with a 25-call floor the FIRST cut call stopped the run (run
    `190214-8de7c7a5`, 2026-09-22, `calls=25 (of 25 seen) cut=1
    share=4.0%`)."""
    fold = CutRate(window=100, share_pct=4)
    assert fold.threshold == 4
    for i in range(80):
        fold = fold.add(mark_of(CAPPED if i == 0 else OK, ""))
        assert not fold.fired
    assert fold.cut == 1 and fold.share == 1.25

    # Negative control: the pre-fix verdict — the share over however much of
    # the window has filled, floored at 25 calls — fires at call 25 on these
    # very marks, because 1 of 25 IS 4%.
    def _old_fired(seen: int, cut: int, share_pct: float = 4.0,
                   window: int = 100, min_calls: int = 25) -> bool:
        marks = min(seen, window)
        return seen >= min_calls and 100.0 * cut / marks >= share_pct

    assert _old_fired(seen=25, cut=1)
    assert not _old_fired(seen=24, cut=1)


def test_a_cut_run_is_stopped_earlier_than_the_share_rule_stopped_it() -> None:
    """The count is not a softer stop: on the recorded cut run it fires at
    the 4th cut call, nine calls before the old rule's floor let it look."""
    rows = _rows("450977-5f48b68a")
    assert _first_fire(rows, CutStopKnobs()) == 16


def test_share_zero_never_fires() -> None:
    fold = CutRate(window=10, share_pct=0)
    for _ in range(10):
        fold = fold.add(mark_of(CAPPED, ""))
    assert not fold.fired
    assert fold.threshold == 0


def test_recorded_run_450977_is_stopped_and_the_disabled_stop_never_is() -> None:
    rows = _rows("450977-5f48b68a")
    assert _first_fire(rows, CutStopKnobs()) is not None
    # Negative control — the knob off: no stop at all.
    assert _first_fire(rows, CutStopKnobs(share_pct=0)) is None


def test_recorded_healthy_run_is_not_stopped() -> None:
    rows = _rows("330242-0be2c3e1")
    assert len(rows) == 216
    assert _first_fire(rows, CutStopKnobs()) is None


def test_knobs_parse_and_a_typo_keeps_the_default(monkeypatch) -> None:
    monkeypatch.setenv("LLM_CUT_STOP_SHARE", "4")
    monkeypatch.setenv("LLM_CUT_STOP_WINDOW", "100")
    monkeypatch.setenv("LLM_CUT_STOP_WINDOW", "oops")
    k = cut_stop_knobs_from_env()
    assert (k.share_pct, k.window, k.threshold) == (4.0, 100, 4)
    assert k.describe() == "cut-stop=4%/100 (4 cut call(s))"
    # A MIN_CALLS left in .env is ignored, not read back into the verdict.
    monkeypatch.setenv("LLM_CUT_STOP_MIN_CALLS", "25")
    assert cut_stop_knobs_from_env() == k
    monkeypatch.setenv("LLM_CUT_STOP_SHARE", "0")
    assert cut_stop_knobs_from_env().describe() == "cut-stop=off"


def test_every_recorded_row_folds_bookkeeping_excluded_route_tagged() -> None:
    reasoning_cap.arm_cut_stop(CutStopKnobs(share_pct=50, window=10))
    with route_scope("session", "session[1,2] #0"):
        record_stream_meta(CAPPED)
    with bookkeeping_call():
        record_stream_meta({**OK, "content_chars": 0})
    record_stream_meta(OK)
    reading = reasoning_cap.health_reading()
    assert reading is not None and reading.seen == 2
    assert reading.by_route() == {"session": (1, 1), "other": (0, 1)}


def test_unarmed_process_records_nothing() -> None:
    record_stream_meta(CAPPED)
    assert reasoning_cap.health_reading() is None


def test_fire_logs_emits_and_takes_the_graceful_stop() -> None:
    logged: list[str] = []
    events: list[tuple[str, dict]] = []
    # 50% of a 4-call window: the 2nd cut call fires it.
    reasoning_cap.arm_cut_stop(
        CutStopKnobs(share_pct=50, window=4),
        log=logged.append,
        emit=lambda name, data: events.append((name, data)),
        context={"cluster": "c"})
    record_stream_meta(OK)
    record_stream_meta(OK)
    graceful_stop.check_graceful_stop(logged.append, "unit a")   # not yet
    record_stream_meta(CAPPED)
    record_stream_meta(CAPPED)              # fires once, not twice
    assert sum("HEALTH ‼ LLM_CUT_STOP" in line for line in logged) == 1
    assert [n for n, _ in events] == ["llm.cut_rate_stop"]
    assert events[0][1]["cut"] == 2 and events[0][1]["cluster"] == "c"
    assert events[0][1]["cut_threshold"] == 2
    with pytest.raises(SystemExit):
        graceful_stop.check_graceful_stop(logged.append, "unit b")
    assert "[graceful] LLM cut-rate stop at unit b" in logged[-1]
    assert not graceful_stop.sentinel_path().exists()


def test_rearm_keeps_the_process_window() -> None:
    reasoning_cap.arm_cut_stop(CutStopKnobs(share_pct=0))
    record_stream_meta(CAPPED)
    reasoning_cap.arm_cut_stop(CutStopKnobs(share_pct=4, window=100))
    reading = reasoning_cap.health_reading()
    assert reading is not None and reading.seen == 1 and reading.cut == 1


def test_call_mark_is_what_the_breakdown_prints() -> None:
    fold = CutRate(100, 4).add(CallMark("per-fn", True, True, 41_000))
    assert "cut=1/4 empty=1 share=100.0% [per-fn 1/1] largest-prompt=41000 tok" \
        in fold.describe()
