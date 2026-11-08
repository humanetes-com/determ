"""One reasoning level per model call, decided once, reaching every transport
method that plans or renders the call.

Before 2026-11-07 the level was one process-wide word at the maximum for every
call (run `336559-eb0222a5`: the behaviour book's prediction calls used 2.7x
Muse's completion tokens per call at the same decode rate). Now
`domain.effort_policy.effort_for` decides per call from the policy, the call's
phase path and the caller's escalation scope, and the controller hands the
answer to `spec`, `count_input_tokens`, `stream` and `reasoning_payload`.
"""
from __future__ import annotations

import threading

import pytest

from pipeline_core import effort
from pipeline_core.adapters import model_conformance as mc
from pipeline_core.domain.effort_policy import EffortPolicy, effort_for
from pipeline_core.domain.model_profile import EffortLevel
from pipeline_core.domain.phase_registry import known_ids
from pipeline_core.llm import Delta, Hooks, LLMController
from pipeline_core.phases import phase_scope
from pipeline_core.threads import ContextPool
from tests.test_llm_controller import SENTINEL, FakeTransport

MED, MAX = EffortLevel.MEDIUM, EffortLevel.MAX


@pytest.fixture(autouse=True)
def _fresh_policy():
    effort.bind_policy(EffortPolicy())
    yield
    effort.bind_policy(None)


def test_the_innermost_configured_phase_wins_and_an_escalation_beats_both():
    pol = EffortPolicy.from_env(
        {"LLM_EFFORT": "medium", "LLM_EFFORT_PHASES": "t0d=medium,t0d.book=max"},
        known_phases=known_ids())
    assert effort_for(("t0d.book", "t0d"), None, pol) is MAX
    assert effort_for(("t0d.probes", "t0d"), None, pol) is MED
    assert effort_for(("t2a.fill", "t2a"), None, pol) is MED
    assert effort_for(("t0d.probes", "t0d"), MAX, pol) is MAX
    assert effort_for((), None, EffortPolicy(default=MAX)) is MAX


def test_a_typo_in_the_policy_refuses_rather_than_falling_back():
    with pytest.raises(ValueError):
        EffortPolicy.from_env({"LLM_EFFORT": "hihg"})
    with pytest.raises(ValueError):
        EffortPolicy.from_env({"LLM_EFFORT_PHASES": "t0d.bok=max"},
                              known_phases=known_ids())
    assert EffortPolicy.from_env({}).default is MED


def test_the_call_reads_its_own_phase_path():
    effort.bind_policy(EffortPolicy(phases=(("t0d.book", MAX),)))
    assert effort.call_effort() is MED
    with phase_scope("t0d") as ph:
        ph.step("t0d.book")
        assert effort.phase_path()[:2] == ("t0d.book", "t0d")
        assert effort.call_effort() is MAX
        ph.step("t0d.rule_probes")
        assert effort.call_effort() is MED


def test_a_lane_thread_sees_its_callers_escalation():
    with effort.effort_scope(MAX), ContextPool(max_workers=2) as pool:
        got = list(pool.map(lambda _i: effort.call_effort(), range(4)))
    assert got == [MAX] * 4


def test_negative_control_a_bare_thread_loses_the_escalation():
    """Why every pool is a `ContextPool` / `context_thread`: a bare thread
    starts on an empty context and would send an escalated run's calls at
    the policy's level."""
    seen: list[EffortLevel] = []
    with effort.effort_scope(MAX):
        th = threading.Thread(target=lambda: seen.append(effort.call_effort()))
        th.start()
        th.join()
    assert seen == [MED]


def test_the_controller_sends_plans_and_records_the_level_it_decided():
    events: list[tuple[str, dict]] = []
    tr = FakeTransport([[Delta(content="ok")], [Delta(content="ok")]], thinking=True)
    ctl = LLMController(Hooks(emit=lambda e, d: events.append((e, d))),
                        default_sentinel=SENTINEL)
    ctl.run(tr, system=None, prompt="p", model="m", max_tokens=100)
    with effort.effort_scope(MAX):
        ctl.run(tr, system=None, prompt="q", model="m", max_tokens=100)
    assert [c["effort"] for c in tr.calls] == [MED, MAX]
    assert tr.spec_effort is MAX
    metas = [d for e, d in events if e == "llm.stream_meta"]
    assert [m["effort"] for m in metas] == ["medium", "max"]
    assert [m["reasoning_payload"]["effort"] for m in metas] == ["medium", "max"]


# ── the launch probe: does the template read the level's word ────────────────


def test_levels_whose_words_render_different_prompts_pass():
    payloads = {"medium": {"enable_thinking": True, "reasoning_effort": "medium"},
                "max": {"enable_thinking": True, "reasoning_effort": "xhigh"}}
    got = mc.check_levels({"medium": [1, 2, 3], "max": [1, 2, 4]}, payloads)
    assert got.ok, got.detail


def test_negative_control_a_template_that_drops_the_word_is_refused():
    """A template without the variable renders both levels alike: a call at
    medium would reason at the default depth and every level reading would
    measure nothing."""
    payloads = {"medium": {"enable_thinking": True, "reasoning_effort": "medium"},
                "max": {"enable_thinking": True, "reasoning_effort": "xhigh"}}
    got = mc.check_levels({"medium": [1, 2, 3], "max": [1, 2, 3]}, payloads)
    assert not got.ok and "drops the level's word" in got.detail


def test_levels_that_send_one_payload_have_nothing_to_tell_apart():
    same = {"enable_thinking": True}
    got = mc.check_levels({"medium": None, "max": None},
                          {"medium": same, "max": same})
    assert got.ok
