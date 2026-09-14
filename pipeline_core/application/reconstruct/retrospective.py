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
from typing import Any

from ...domain.bound_mismatch import detect_frozen_bound_mismatch
from ...domain.llm_output import parse_retrospective
from ...domain.text import (
    PromptDigest,
    label_value_offset,
    starts_with_label,
    strip_surrounding_asterisks,
)
from ...prompts.retrospective import (
    build_phase_c_retro_prompt,
    build_whole_tree_retro_prompt,
)


def _is_vtable_dispatch_fn(sig_text: str, container: str) -> bool:
    """Bound-driver vtable-dispatch test.

    Catalog keys on Own<ErrorImpl>/Ref<ErrorImpl>. Generic keys on an
    unsafe fn whose parameter is a wrapper of a named type with no
    where-bound that opens a typed path.
    """
    from pipeline_core.domain.language_driver import language_driver
    return language_driver().is_vtable_dispatch(sig_text, container)


@dataclass(frozen=True, slots=True)
class PhaseCRetroDeps:
    """Collaborators the retrospective needs, injected by the composition root."""

    ask_retrospective: Callable[[str], str]
    log: Callable[[str], None]
    failed_body_cap: int
    fix_err_cap: int
    # Telemetry sibling for the model call below. In `llm.stream_meta` a retro
    # call is indistinguishable from a fill call, which is what made the retro
    # round's price unmeasurable: ~50 retro outcomes bought ~6 honoured
    # guidances on run `58819-ee78a1d1` at ordinary big-think call prices
    # (fix-loop median 22,822 prompt tokens), and nothing could split that
    # spend out of the stream. The event joins `llm.stream_meta` on
    # `prompt_sha` — same digest, same function — so the split is one join.
    emit: Callable[..., None] | None = None


def diagnose_phase_c(
    fn_name: str, sig_text: str, container: str, file_rel: str,
    cluster_name: str, last_body: str, error_excerpt: str, fix_attempts: int,
    deps: PhaseCRetroDeps,
    prior_retrospective: str = "", sibling_bodies: str = "",
    bound_methods_section: str = "",
    pinned_observables: str = "",
    compiled: bool = False,
) -> dict[str, str]:
    if not last_body and not error_excerpt:
        return {"category": "", "guidance": "", "signal": "no_data"}
    # If EVERY error is downstream (other files, none in the target),
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
        from pipeline_core.domain.language_driver import language_driver
        return {
            "category": "GENUINELY_HARD",
            "guidance": language_driver().vtable_fast_exit_guidance(),
            "signal": "vtable_fast_exit",
        }

    # A frozen where-bound the compiler proves unsatisfiable cannot be
    # fixed by ANY body — park the fn structurally instead of letting the LLM
    # classifier oscillate TRAIT_BOUND_VIOLATION↔API_HALLUCINATION for 5-6
    # attempts. The cause is the module defining the frozen bound's trait
    # (alias/re-export mismatch), not this fn.
    if fix_attempts >= 2:
        _mm = detect_frozen_bound_mismatch(sig_text, container, _exc)
        if _mm is not None:
            deps.log(f"  [phase-c/retro] {fn_name}: frozen-bound mismatch — "
                     f"`{_mm.param}: {' + '.join(_mm.frozen)}` (frozen) cannot "
                     f"satisfy required `{_mm.required}`; parking fn (fix the "
                     f"module defining `{_mm.frozen[0]}`, not this body).")
            return {
                "category": "FROZEN_BOUND_MISMATCH",
                "guidance": (
                    f"the compiler requires `{_mm.param}: {_mm.required}` but the "
                    f"FROZEN signature only grants `{_mm.param}: "
                    f"{' + '.join(_mm.frozen)}` — no body can satisfy this. The "
                    f"mismatch is in the module that defines "
                    f"`{_mm.frozen[0]}` (it must be, or re-export, "
                    f"`{_mm.required}`). Do not retry this fn."
                ),
                "signal": "frozen_bound_mismatch",
            }

    prompt = build_phase_c_retro_prompt(
        fn_name, sig_text, container, file_rel, cluster_name, last_body,
        error_excerpt, fix_attempts, prior_retrospective, sibling_bodies,
        failed_body_cap=deps.failed_body_cap, fix_err_cap=deps.fix_err_cap,
        bound_methods_section=bound_methods_section,
        pinned_observables=pinned_observables,
        compiled=compiled,
    )
    # Retro runs think=True with a bounded output budget (think=False
    # returns empty content on the vLLM reasoning-parser endpoint). One retry on
    # empty is handled inside the injected ask_retrospective.
    if deps.emit is not None:
        deps.emit("phase_c.retro.call", data={
            "unit": f"{file_rel}::{fn_name}", "cluster": cluster_name,
            "prompt_sha": PromptDigest.of(prompt).sha256,
            "purpose": "retro", "kind": "per_fn"})
    try:
        result = deps.ask_retrospective(prompt)
    except Exception as e:
        deps.log(f"  [phase-c/retro] LLM call failed: {e}")
        return {"category": "", "guidance": "", "signal": "llm_error"}
    if not (result or "").strip():
        deps.log(f"  [phase-c/retro] LLM returned empty content for {fn_name}")
        return {"category": "", "guidance": "", "signal": "empty_response"}
    return parse_retrospective(result)


def whole_tree_retrospective(
    error_excerpt: str,
    history: list[dict[str, Any]],
    *,
    name: str,
    ask_retrospective: Callable[[str], str],
    log: Callable[[str], None],
    fix_err_cap: int,
    emit: Callable[..., None] | None = None,
) -> str:
    """Strategy retrospective for whole-tree fix loops — the tree-wide partner of
    `diagnose_phase_c`. Asks the model what categorically different approach to
    take when the error floor stopped moving; returns ``[CATEGORY] guidance``
    (or just guidance / ``[CATEGORY]``). Empty string on transient LLM failure."""
    if not error_excerpt.strip():
        return ""
    prompt = build_whole_tree_retro_prompt(
        error_excerpt, history, name=name, fix_err_cap=fix_err_cap)
    if emit is not None:
        emit("phase_c.retro.call", data={
            "unit": name, "cluster": "",
            "prompt_sha": PromptDigest.of(prompt).sha256,
            "purpose": "retro", "kind": "whole_tree"})
    try:
        result = ask_retrospective(prompt)
    except Exception as e:
        log(f"  [retro/{name}] LLM call failed: {e}")
        return ""
    if not (result or "").strip():
        log(f"  [retro/{name}] LLM returned empty content")
        return ""
    # Lightweight parse: everything after "GUIDANCE:" up to the next label.
    cat = ""
    guidance_lines: list[str] = []
    in_guidance = False
    for ln in (result or "").splitlines():
        s = ln.strip().lstrip("*-• ").strip()
        s = strip_surrounding_asterisks(s)
        off = label_value_offset(s, "category")
        if off is not None and (parts := s[off:].split()):
            cat = parts[0].strip().rstrip(".,;").upper()
            in_guidance = False
            continue
        off = label_value_offset(s, "guidance")
        if off is not None and s[off:]:
            guidance_lines = [s[off:].strip()]
            in_guidance = True
            continue
        if in_guidance and s:
            if starts_with_label(s, ("category", "guidance", "note")):
                in_guidance = False
            else:
                guidance_lines.append(s)
    guidance = " ".join(guidance_lines).strip()
    if not cat and not guidance:
        guidance = (result or "").strip()[:1200]
    if cat:
        return f"[{cat}] {guidance}" if guidance else f"[{cat}]"
    return guidance
