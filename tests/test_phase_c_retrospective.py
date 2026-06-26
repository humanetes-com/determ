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
from pipeline_core.prompts.retrospective import build_phase_c_retro_prompt


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


def _retro_prompt(bound_methods_section: str = "") -> str:
    return build_phase_c_retro_prompt(
        "f", "fn f<E>() where E: Bound", "impl X", "src/x.rs", "c",
        "let x = 1;", "E0277", 3, "", "",
        failed_body_cap=2000, fix_err_cap=2000,
        bound_methods_section=bound_methods_section)


def test_prompt_forbids_signature_edits_body_only():
    # R1 (debt #28): the retro must constrain guidance to BODY-ONLY edits so it
    # never proposes a frozen-signature/bound/where change → no re-submit thrash.
    p = _retro_prompt()
    assert "ONLY change the function BODY" in p
    assert "FROZEN by the skeleton" in p
    assert "`where` clause" in p


def test_prompt_routes_unsatisfiable_bound_to_skip():
    p = _retro_prompt()
    # GENUINELY_HARD is gated on the frozen bound's trait offering no usable
    # method — only then do we skip rather than route through bound methods.
    assert "GENUINELY_HARD" in p
    assert "frozen bound provides NO method usable here" in p


def test_prompt_redirects_bound_mismatch_to_provider_method():
    # A free constructor erroring E0277 must route to the FROZEN bound's own
    # trait method, NOT to a skip (unless vtable/raw-ptr pattern applies).
    p = _retro_prompt()
    assert "BOUND-MISMATCH RULE" in p
    assert "Route through that method" in p
    assert "NOT GENUINELY_HARD" in p


def test_prompt_vtable_access_directs_to_genuinely_hard():
    # NEEDS_VTABLE_ACCESS is a fast-exit to GENUINELY_HARD — vtable layout
    # cannot be recovered via trait methods alone; the skeleton hides the struct.
    p = _retro_prompt()
    assert "NEEDS_VTABLE_ACCESS" in p
    assert "classify as GENUINELY_HARD immediately" in p
    assert "vtable layout cannot be satisfied via trait methods" in p


def test_prompt_vtable_exception_in_bound_mismatch_rule():
    # The BOUND-MISMATCH RULE must name the vtable exception so the model
    # does not attempt bound-routing for pointer/vtable patterns.
    p = _retro_prompt()
    assert "NEEDS_VTABLE_ACCESS" in p
    assert "raw-pointer dereference or" in p


def test_prompt_injects_bound_methods_section_when_present():
    p = _retro_prompt(bound_methods_section="trait `StdError` provides:\n"
                      "    fn ext_context<C>(self, c: C) -> Error")
    # The injected provider-method content is present.
    assert "ext_context" in p
    assert "trait `StdError` provides" in p
    # Absent when empty — the rule TEXT still references <BOUND_METHODS> as a
    # pointer, but the injected method content is gone.
    assert "ext_context" not in _retro_prompt()


def test_prompt_uses_wrong_right_examples():
    # CLAUDE.md: injected guidance must show concrete WRONG→RIGHT, not jargon.
    p = _retro_prompt()
    assert "WRONG:" in p and "RIGHT:" in p


# F/R4 — vtable dispatch fast-exit tests
def _vtable_deps(answer: str = "CATEGORY: X\nGUIDANCE: y") -> PhaseCRetroDeps:
    def ask(_prompt: str) -> str:
        return answer
    return PhaseCRetroDeps(ask_retrospective=ask, log=lambda *_: None,
                           failed_body_cap=2000, fix_err_cap=2000)


def test_vtable_fn_fast_exits_genuinely_hard_at_two_attempts() -> None:
    called: list[str] = []

    def ask(p: str) -> str:
        called.append(p)
        return "CATEGORY: X\nGUIDANCE: y"

    deps = PhaseCRetroDeps(ask_retrospective=ask, log=lambda *_: None,
                           failed_body_cap=2000, fix_err_cap=2000)
    result = diagnose_phase_c(
        "error",
        "unsafe fn error(this: Ref<ErrorImpl>) -> &dyn StdError",
        "impl ErrorImpl",
        "src/error.rs", "stable",
        "let x = 1;", "E0609", 2, deps,
    )
    assert result["category"] == "GENUINELY_HARD"
    assert result["signal"] == "vtable_fast_exit"
    assert not called  # LLM must NOT be called


def test_vtable_fn_does_not_fast_exit_at_one_attempt() -> None:
    called: list[str] = []

    def ask(p: str) -> str:
        called.append(p)
        return "CATEGORY: WRONG_TYPE_CHOICE\nGUIDANCE: try NonNull"

    deps = PhaseCRetroDeps(ask_retrospective=ask, log=lambda *_: None,
                           failed_body_cap=2000, fix_err_cap=2000)
    result = diagnose_phase_c(
        "error",
        "unsafe fn error(this: Ref<ErrorImpl>) -> &dyn StdError",
        "impl ErrorImpl",
        "src/error.rs", "stable",
        "let x = 1;", "E0609", 1, deps,
    )
    assert called  # LLM WAS called
    assert result["signal"] not in ("vtable_fast_exit",)


def test_non_vtable_unsafe_fn_not_fast_exited() -> None:
    # `unsafe fn` with raw-ptr param but no Own/Ref<ErrorImpl> → normal path
    called: list[str] = []

    def ask(p: str) -> str:
        called.append(p)
        return "CATEGORY: WRONG_DEREF_PATTERN\nGUIDANCE: use ptr.read()"

    deps = PhaseCRetroDeps(ask_retrospective=ask, log=lambda *_: None,
                           failed_body_cap=2000, fix_err_cap=2000)
    result = diagnose_phase_c(
        "read_at", "unsafe fn read_at<T: Copy>(p: *const T, i: isize) -> T",
        "impl ReadHelper", "src/lib.rs", "stable",
        "let x = 1;", "E0133", 3, deps,
    )
    assert called
    assert result["signal"] != "vtable_fast_exit"


def test_vtable_fn_with_where_bound_not_fast_exited() -> None:
    # where E: bound opens a normal dispatch path — the fast-exit does not apply
    called: list[str] = []

    def ask(p: str) -> str:
        called.append(p)
        return "CATEGORY: TRAIT_BOUND_VIOLATION\nGUIDANCE: call via bound"

    deps = PhaseCRetroDeps(ask_retrospective=ask, log=lambda *_: None,
                           failed_body_cap=2000, fix_err_cap=2000)
    result = diagnose_phase_c(
        "downcast_ref",
        "unsafe fn downcast_ref<E>(this: Ref<ErrorImpl<E>>) -> Option<&E> where E: Any",
        "impl ErrorImpl",
        "src/error.rs", "stable",
        "let x = 1;", "E0277", 3, deps,
    )
    assert called
    assert result["signal"] != "vtable_fast_exit"
