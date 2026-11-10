"""A forced followup that finished its reply is a decided answer, not a cut.

Run `2833915-23e0c633` (2026-11-09, Qwen3.8-27B-FP8 at `medium`, cluster
`stable`) was stopped by `HEALTH ‼ LLM_CUT_STOP calls=100 (of 112 seen)
cut=4/4` at 376 of 424 passing. All four "cut" calls had a main stream that
ended `finish_reason=stop`, 16,918-24,867 completion tokens against a
78,973-90,528 request max, with no content: the model ended its turn inside
its reasoning. Each forced followup finished on its own and wrote the reply
the pipeline then served (three context requests, one fn body). The cut rule
read the followup's FIRING as the failure; the fact the stop exists for is
whether a decided answer reached the record.

The recorded fixture is that run's 112 main-stream rows joined to their
followup rows (`tests/fixtures/cut_rate/2833915-23e0c633.json`). The negative
control is the same rows read without the join — the pre-fix rule — which
fires at call 112 exactly as the live process did.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pipeline_core.domain.reasoning_cap import (
    CutRate,
    CutStopKnobs,
    answer_decided,
    is_cut,
    mark_of,
    pair_call_rows,
)
from pipeline_core.llm import Delta, Hooks, LLMController
from tests.test_llm_controller import SENTINEL, FakeTransport, _spec

FIXTURE = Path(__file__).parent / "fixtures" / "cut_rate" / "2833915-23e0c633.json"

EMPTY_MAIN = {"finish_reason": "stop", "stop": None, "followup": True,
              "content_chars": 45, "prompt_tokens": 40_032}
ANSWERED = {"finish_reason": "stop", "stop": None, "content_chars": 45,
            "salvaged_chars": 0, "fn_blocks": 0}


def _first_fire(rows: list[dict[str, Any]], *, joined: bool) -> int | None:
    k = CutStopKnobs()
    fold = CutRate(k.window, k.share_pct)
    for i, r in enumerate(rows, 1):
        fold = fold.add(mark_of(r["stream"], "", r["followup"] if joined else None))
        if fold.fired:
            return i
    return None


def test_the_recorded_run_is_not_stopped_by_four_answered_followups() -> None:
    rows = json.loads(FIXTURE.read_text())["rows"]
    assert len(rows) == 112
    fu = [r for r in rows if r["stream"]["followup"]]
    assert len(fu) == 4 and all(r["followup"] is not None for r in fu)
    # The premise: none of the four main streams hit a budget.
    assert all(r["stream"]["finish_reason"] == "stop" and r["stream"]["stop"] is None
               for r in fu)
    assert _first_fire(rows, joined=True) is None
    # Negative control: without the followup's outcome, the pre-fix rule —
    # the live process's stop, at the live call index.
    assert _first_fire(rows, joined=False) == 112


def test_a_followup_decides_the_call_only_when_it_finished_and_wrote() -> None:
    assert answer_decided(EMPTY_MAIN, ANSWERED)
    assert not is_cut(EMPTY_MAIN, ANSWERED)
    # A followup cut by the server budget or the think cap: still a cut, even
    # with a salvaged closed prefix (the budget lost the element in flight).
    assert is_cut(EMPTY_MAIN, {**ANSWERED, "finish_reason": "length",
                               "salvaged_chars": 40})
    assert is_cut(EMPTY_MAIN, {**ANSWERED, "finish_reason": None, "stop": "think_cap"})
    # A transport failure on the followup, or a followup that wrote nothing.
    assert is_cut(EMPTY_MAIN, {**ANSWERED, "finish_reason": "error", "content_chars": 0})
    assert is_cut(EMPTY_MAIN, {**ANSWERED, "content_chars": 0})
    # No followup row to vouch for it: undecided (the pre-stamp rule).
    assert is_cut(EMPTY_MAIN)


def test_a_main_stream_decides_alone_and_a_followup_row_cannot_rescue_it() -> None:
    ok = {"finish_reason": "stop", "stop": None, "followup": False, "content_chars": 900}
    assert not is_cut(ok)
    assert not is_cut({**ok, "finish_reason": None, "stop": "closed"})
    assert is_cut({**ok, "finish_reason": "length"})
    assert is_cut({**ok, "content_chars": 0})
    # A stale followup row passed beside a call that ran none is ignored.
    assert is_cut({**ok, "content_chars": 0}, ANSWERED)


def test_the_stamp_wins_over_the_rows() -> None:
    assert not is_cut({**EMPTY_MAIN, "decided": True})
    assert is_cut({**EMPTY_MAIN, "decided": False}, ANSWERED)


def test_the_join_pairs_a_followup_with_its_own_call_only() -> None:
    evs = [
        {"event": "llm.followup_meta", "run_id": "r", "data": {**ANSWERED, "prompt_sha": "a"}},
        {"event": "llm.request", "run_id": "r", "data": {"prompt_sha": "b"}},
        {"event": "llm.stream_meta", "run_id": "r", "data": {**EMPTY_MAIN, "prompt_sha": "a"}},
        # Same prompt in another run, no followup row of its own.
        {"event": "llm.stream_meta", "run_id": "s", "data": {**EMPTY_MAIN, "prompt_sha": "a"}},
        # A call that ran no followup consumes nothing and is given nothing.
        {"event": "llm.followup_meta", "run_id": "r", "data": {**ANSWERED, "prompt_sha": "c"}},
        {"event": "llm.stream_meta", "run_id": "r",
         "data": {"prompt_sha": "c", "followup": False, "content_chars": 9,
                  "finish_reason": "stop"}},
        {"event": "llm.stream_meta", "run_id": "r", "data": {**EMPTY_MAIN, "prompt_sha": "c"}},
    ]
    pairs = [(ev["run_id"], ev["data"]["prompt_sha"], fu) for ev, fu in pair_call_rows(evs)]
    assert [(r, p, fu is not None) for r, p, fu in pairs] == [
        ("r", "a", True), ("s", "a", False), ("r", "c", False), ("r", "c", False)]


def test_the_breakdown_prints_the_answered_followups() -> None:
    fold = CutRate(100, 4).add(mark_of(EMPTY_MAIN, "session", ANSWERED))
    assert fold.describe().endswith("cut=0/4 empty=0 share=0.0% [session 0/1] "
                                    "largest-prompt=40032 tok followup-answered=1")


# --------------------------------------------------------------------------- #
# the writer: the controller stamps the verdict from the followup's own row
# --------------------------------------------------------------------------- #
def _stream_row(transport: FakeTransport) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    ctl = LLMController(
        Hooks(emit=lambda name, data: events.append({"event": name, **data})),
        default_sentinel=SENTINEL)
    ctl.run(transport, system="sys", prompt="p", model="m", max_tokens=SENTINEL)
    rows = [e for e in events if e["event"] == "llm.stream_meta"]
    assert len(rows) == 1
    return rows[0]


def _spec_live():
    return _spec(content_budget=8192, thinking_budget=16384)


def test_controller_stamps_an_empty_main_answered_by_its_followup_as_decided() -> None:
    row = _stream_row(FakeTransport(
        [[Delta(reasoning="r" * 500)], [Delta(content='<REQUEST kind="file" arg="x"/>')]],
        spec=_spec_live(), finish_reasons=["stop", "stop"]))
    assert row["followup"] is True and row["finish_reason"] == "stop"
    assert row["decided"] is True and not is_cut(row)


def test_controller_stamps_a_followup_cut_at_length_as_undecided() -> None:
    row = _stream_row(FakeTransport(
        [[Delta(reasoning="r" * 500)], [Delta(content="<FN a>x</FN><FN b>par")]],
        spec=_spec_live(), finish_reasons=["length", "length"]))
    assert row["followup"] is True and row["decided"] is False and is_cut(row)


def test_controller_stamps_a_failed_followup_as_undecided() -> None:
    from pipeline_core.llm.events import TransportTimeout
    row = _stream_row(FakeTransport(
        [[Delta(reasoning="r" * 100)], TransportTimeout("hung")],
        spec=_spec_live(), finish_reasons=["stop", None]))
    assert row["decided"] is False and is_cut(row)


def test_controller_stamps_a_plain_reply_as_decided() -> None:
    row = _stream_row(FakeTransport([[Delta(content="<FN a>x</FN>")]]))
    assert row["followup"] is False and row["decided"] is True
