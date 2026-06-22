"""Unit tests for the Phase-C retrospective diagnosis use-case
(`pipeline_core.application.reconstruct.retrospective`) relocated in P6.

Exercises the orchestration with a FAKE injected `ask_retrospective` (no LLM):
the early no-data / downstream-only guards, the llm_error / empty_response
signals, and the happy parse path.
"""
from __future__ import annotations

from pipeline_core.application.reconstruct.retrospective import (
    PhaseCRetroDeps,
    diagnose_phase_c,
)


def _deps(answer=None, raises=False):
    def ask(_prompt: str) -> str:
        if raises:
            raise RuntimeError("boom")
        return answer or ""
    return PhaseCRetroDeps(ask_retrospective=ask, log=lambda *_: None,
                           failed_body_cap=2000, fix_err_cap=2000)


def _call(deps, *, last_body="let x = 1;", error_excerpt="E0277"):
    return diagnose_phase_c("f", "fn f()", "impl X", "src/x.rs", "c",
                            last_body, error_excerpt, 2, deps)


def test_no_data_short_circuits_without_calling_llm():
    assert _call(_deps(), last_body="", error_excerpt="") == {
        "category": "", "guidance": "", "signal": "no_data"}


def test_downstream_only_short_circuits():
    exc = "Errors elsewhere in the crate: lib.rs:1"
    out = _call(_deps(answer="CATEGORY: X"), error_excerpt=exc)
    assert out["signal"] == "downstream_only"
    assert out["category"] == "DOWNSTREAM"


def test_llm_error_signal():
    assert _call(_deps(raises=True))["signal"] == "llm_error"


def test_empty_response_signal():
    assert _call(_deps(answer="   "))["signal"] == "empty_response"


def test_happy_parse():
    out = _call(_deps(answer="CATEGORY: WRONG_TYPE_CHOICE\nGUIDANCE: use NonNull"))
    assert out["category"] == "WRONG_TYPE_CHOICE"
    assert "NonNull" in out["guidance"]
