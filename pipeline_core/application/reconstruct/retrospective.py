"""Phase-C per-fn retrospective diagnosis use-case (P6 engine-relocation series).

When the per-fn fix loop exhausts on a fn, this diagnoses WHY — returning a
structured `{category, guidance, signal}` dict that shapes the next attempt's
prompt with concrete "try X instead" hints rather than a blind retry.

Pure decisions (the early no-data / downstream-only guards) and prompt assembly
live in domain/prompts; this use-case orchestrates the injected LLM call and the
domain parse. `signal` values: no_data / downstream_only / llm_error /
empty_response, else the parsed category/guidance.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ...domain.llm_output import parse_retrospective
from ...prompts.retrospective import build_phase_c_retro_prompt


def _is_vtable_dispatch_fn(sig_text: str, container: str) -> bool:
    """True when the fn requires ErrorVTable layout access that the skeleton
    does not expose: `unsafe fn` with `Own<ErrorImpl>` or `Ref<ErrorImpl>`
    param and no `where E:` bound (which would open a normal dispatch path)."""
    if "unsafe fn" not in sig_text:
        return False
    combined = sig_text + " " + container
    if "Own<ErrorImpl" not in combined and "Ref<ErrorImpl" not in combined:
        return False
    return "where E:" not in sig_text


@dataclass(frozen=True, slots=True)
class PhaseCRetroDeps:
    """Collaborators the retrospective needs, injected by the composition root."""

    ask_retrospective: Callable[[str], str]
    log: Callable[[str], None]
    failed_body_cap: int
    fix_err_cap: int


def diagnose_phase_c(
    fn_name: str, sig_text: str, container: str, file_rel: str,
    cluster_name: str, last_body: str, error_excerpt: str, fix_attempts: int,
    deps: PhaseCRetroDeps,
    prior_retrospective: str = "", sibling_bodies: str = "",
    bound_methods_section: str = "",
) -> dict[str, str]:
    if not last_body and not error_excerpt:
        return {"category": "", "guidance": "", "signal": "no_data"}
    # FR-037: if EVERY error is downstream (other files, none in the target),
    # this fn is probably fine and the crate is broken elsewhere. A target retro
    # here misattributes the cause and writes a WRONG anti-hallucination note —
    # skip it; let the blocking fn's own loop fix it.
    _exc = error_excerpt or ""
    if "Errors elsewhere in the crate" in _exc and "Errors in target file" not in _exc:
        return {"category": "DOWNSTREAM", "guidance": "", "signal": "downstream_only"}

    # F/R4: unsafe vtable-dispatch fns cannot be reconstructed without the
    # ErrorVTable struct layout. Skip the LLM call and mark GENUINELY_HARD
    # immediately at ≥2 attempts — saves ~32 wasted model calls per anyhow run.
    if fix_attempts >= 2 and _is_vtable_dispatch_fn(sig_text, container):
        return {
            "category": "GENUINELY_HARD",
            "guidance": (
                "fn requires ErrorVTable layout access via Own<ErrorImpl> or "
                "Ref<ErrorImpl> without a where-bound. The vtable struct is not "
                "exposed by the skeleton — do not retry."
            ),
            "signal": "vtable_fast_exit",
        }

    prompt = build_phase_c_retro_prompt(
        fn_name, sig_text, container, file_rel, cluster_name, last_body,
        error_excerpt, fix_attempts, prior_retrospective, sibling_bodies,
        failed_body_cap=deps.failed_body_cap, fix_err_cap=deps.fix_err_cap,
        bound_methods_section=bound_methods_section,
    )
    # FR-028: retro runs think=True with a bounded output budget (think=False
    # returns empty content on the vLLM reasoning-parser endpoint). One retry on
    # empty is handled inside the injected ask_retrospective.
    try:
        result = deps.ask_retrospective(prompt)
    except Exception as e:
        deps.log(f"  [phase-c/retro] LLM call failed: {e}")
        return {"category": "", "guidance": "", "signal": "llm_error"}
    if not (result or "").strip():
        deps.log(f"  [phase-c/retro] LLM returned empty content for {fn_name}")
        return {"category": "", "guidance": "", "signal": "empty_response"}
    return parse_retrospective(result)
