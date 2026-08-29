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
    whole_tree_retrospective,
)
from pipeline_core.prompts.retrospective import build_phase_c_retro_prompt


def _wtr(answer: str, *, raises: bool = False) -> str:
    def ask(_p: str) -> str:
        if raises:
            raise RuntimeError("boom")
        return answer
    return whole_tree_retrospective(
        "error[E0277]: bad", [{"iter": 1, "summary": "x", "error_sig": "E0277"}],
        name="gfl", ask_retrospective=ask, log=lambda _m: None, fix_err_cap=2000)


def test_whole_tree_retro_parses_category_and_guidance() -> None:
    assert _wtr("CATEGORY: UNSIZED_RETURN\nGUIDANCE: return &T instead") == (
        "[UNSIZED_RETURN] return &T instead")


def test_whole_tree_retro_empty_excerpt_short_circuits() -> None:
    assert whole_tree_retrospective(
        "   ", [], name="x", ask_retrospective=lambda _p: "X",
        log=lambda _m: None, fix_err_cap=10) == ""


def test_whole_tree_retro_llm_error_and_empty() -> None:
    assert _wtr("", raises=True) == ""        # exception → ""
    assert _wtr("   ") == ""                    # empty content → ""


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


def test_same_file_other_impl_is_not_downstream_only():
    """A windowed LAST_ERROR that moved a same-file diagnostic out of the
    target bucket must not skip the retro — that is other impls, not other
    files (run-shaped e2e: diag on line 1, impl starts on line 3)."""
    exc = (
        "# Errors elsewhere in this file "
        "(other impls / items, not this block):\n"
        "error[E0308]: mismatched types\n --> src/lib.rs:1"
    )
    out = _call(_deps(answer="CATEGORY: WRONG_TYPE_CHOICE\nGUIDANCE: x"),
                error_excerpt=exc)
    assert out["signal"] != "downstream_only"
    assert out["category"] == "WRONG_TYPE_CHOICE"


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


def test_prompt_carries_the_pins_the_unit_answers_for():
    """The live `src/ensure.rs::*impl*::BothDebugfor(A,B)` retrospective
    prescribed away the 40-byte overflow fallback `test_trailer` pins; a
    diagnosis that sees the pins is told it may not."""
    pins = 'is::<&str>, "Condition failed: `(|| 1)() == 2` (1 vs 2)"'
    p = build_phase_c_retro_prompt(
        "f", "fn f<E>() where E: Bound", "impl X", "src/x.rs", "c",
        "let x = 1;", "E0277", 3, "", "",
        failed_body_cap=2000, fix_err_cap=2000, pinned_observables=pins)
    assert (f"The tests this unit answers for pin: {pins}. A prescription "
            "may not remove an observable those pins name.") in p
    # NEGATIVE CONTROL: no pins, no section — and a blank string is no pins.
    assert "answers for pin" not in _retro_prompt()
    assert "answers for pin" not in build_phase_c_retro_prompt(
        "f", "fn f()", "", "src/x.rs", "c", "", "E0277", 1, "", "",
        failed_body_cap=10, fix_err_cap=10, pinned_observables="  ")


def test_prompt_forbids_signature_edits_body_only():
    # the retro must constrain guidance to BODY-ONLY edits so it
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


def test_a_retro_call_is_tagged_in_telemetry_and_joins_on_prompt_sha() -> None:
    """In `llm.stream_meta` a retro call is indistinguishable from a fill
    call, which is what made the retro round's price unmeasurable (~6 honoured
    guidances from ~50 outcomes at big-think prices, run `58819-ee78a1d1`, and
    no way to split the spend out of the stream). The sibling event carries
    purpose=retro and the SAME PromptDigest sha the stream event is keyed on,
    so the split is one join."""
    from pipeline_core.domain.text import PromptDigest
    seen_prompts: list[str] = []
    events: list[tuple[str, dict]] = []

    def ask(p: str) -> str:
        seen_prompts.append(p)
        return "CATEGORY: X\nGUIDANCE: y"

    deps = PhaseCRetroDeps(
        ask_retrospective=ask, log=lambda *_: None,
        failed_body_cap=2000, fix_err_cap=2000,
        emit=lambda ev, **k: events.append((ev, k.get("data") or {})))
    diagnose_phase_c(
        "construct", "fn construct()", "impl Error", "src/error.rs",
        "stable", "let x = 1;", "error[E0308]: mismatched", 1, deps)
    rows = [d for ev, d in events if ev == "phase_c.retro.call"]
    assert rows and rows[0]["purpose"] == "retro"
    assert rows[0]["unit"] == "src/error.rs::construct"
    assert seen_prompts and rows[0]["prompt_sha"] == \
        PromptDigest.of(seen_prompts[0]).sha256


def test_a_whole_tree_retro_call_is_tagged_too() -> None:
    events: list[tuple[str, dict]] = []
    whole_tree_retrospective(
        "error[E0277]: bad", [], name="gfl",
        ask_retrospective=lambda _p: "CATEGORY: X\nGUIDANCE: y",
        log=lambda _m: None, fix_err_cap=2000,
        emit=lambda ev, **k: events.append((ev, k.get("data") or {})))
    rows = [d for ev, d in events if ev == "phase_c.retro.call"]
    assert rows and rows[0]["kind"] == "whole_tree"
