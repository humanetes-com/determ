"""Phase-C per-fn retrospective prompt, relocated out of pipeline.py.

Pure prompt assembly: given the failed fn's context, produce the diagnosis
prompt that asks the model to classify the root cause and give concrete
"try X instead" guidance for the next attempt. No I/O, no LLM call.
"""

from __future__ import annotations

from typing import Any

from pipeline_core.domain.language_driver import language_driver


def build_phase_c_retro_prompt(
    fn_name: str, sig_text: str, container: str, file_rel: str,
    cluster_name: str, last_body: str, error_excerpt: str, fix_attempts: int,
    prior_retrospective: str, sibling_bodies: str,
    *, failed_body_cap: int, fix_err_cap: int,
    bound_methods_section: str = "",
    pinned_observables: str = "",
    compiled: bool = False,
) -> str:
    """`pinned_observables` is the oracle extraction of the tests the unit
    answers for (`domain.refill_evidence.pinned_observables_from_tests`).
    Rendered as a bound on the prescription: the live
    `src/ensure.rs::*impl*::BothDebugfor(A,B)` record (cluster `stable`,
    2026-08) was written from a compiled attempt that REGRESSED 13 ensure
    tests, and its guidance — "Drop the fixed 40-byte SmallBuf and format
    into an allocating Write … avoids the overflow fallback" — prescribes
    exactly the observable `test_trailer`/`test_whitespace` pin (the 40-byte
    overflow and the whitespace fallback). A diagnosis that never sees the
    pins cannot know that the fallback it removes is the behaviour."""
    pins_section = (
        f"\nThe tests this unit answers for pin: {pinned_observables}. "
        "A prescription may not remove an observable those pins name.\n"
    ) if pinned_observables.strip() else ""
    prior_section = (
        f"\nPrior retrospective for this fn (you said this LAST time it failed; "
        f"if the same approach still failed, escalate to GENUINELY_HARD):\n"
        f"{prior_retrospective}\n"
    ) if prior_retrospective.strip() else ""
    siblings_section = (
        f"\nSibling impls of `{fn_name}` already filled correctly in this file "
        f"(use as reference):\n{sibling_bodies}\n"
    ) if sibling_bodies.strip() else ""
    # BOUND_METHODS surfaces the methods the frozen trait bound makes callable
    # so the model routes through them rather than mis-classifying GENUINELY_HARD.
    bound_section = (
        f"\n<BOUND_METHODS>\n{bound_methods_section}\n</BOUND_METHODS>\n"
    ) if bound_methods_section.strip() else ""
    if compiled:
        attempt_line = (
            f"You are diagnosing why a reconstruction attempt for fn `{fn_name}` "
            f"in `{file_rel}` (impl: `{container}`, cluster: `{cluster_name}`) "
            f"compiled and then crashed or reddened tests after "
            f"{fix_attempts} attempt(s).\n\n")
        body_line = (
            "Last attempted body (compiled; rustc accepted it):\n"
            f"```rust\n{last_body[:failed_body_cap]}\n```\n\n")
        error_line = (
            "Crash / test failure from that attempt:\n"
            f"```\n{error_excerpt[:fix_err_cap]}\n```\n")
        compiled_rule = (
            "rustc accepted this body. Do not prescribe replacing a call it "
            "type-checked with one it has already rejected — that rejection "
            "is a different attempt. Name the runtime failure: the panic or "
            "crash line, and the expression in the body that reached it.\n\n")
    else:
        attempt_line = (
            f"You are diagnosing why a reconstruction attempt for fn `{fn_name}` "
            f"in `{file_rel}` (impl: `{container}`, cluster: `{cluster_name}`) "
            f"could not be made to compile after {fix_attempts} attempt(s).\n\n")
        body_line = (
            "Last attempted body (failed to compile):\n"
            f"```rust\n{last_body[:failed_body_cap]}\n```\n\n")
        error_line = (
            "Compile errors from that attempt:\n"
            f"```\n{error_excerpt[:fix_err_cap]}\n```\n")
        compiled_rule = ""
    return (
        attempt_line
        + f"Signature:\n```rust\n{sig_text}\n```\n\n"
        + body_line
        + error_line
        + compiled_rule
        + f"{prior_section}"
        + f"{siblings_section}"
        + f"{bound_section}"
        + f"{pins_section}\n"
        # The skeleton FREEZES the signature. The fixer can ONLY
        # rewrite the body, so guidance that proposes editing a bound / where /
        # generic / return type is un-actionable → identical re-submit → thrash.
        # State the constraint with WRONG→RIGHT examples (the model acts on
        # examples, not rules) and route an unsatisfiable frozen bound to a skip.
        "HARD CONSTRAINT — you may ONLY change the function BODY. The signature, "
        "generic parameters, the `where` clause, the trait bounds, and the return "
        "type are FROZEN by the skeleton and CANNOT be edited. Never suggest "
        "adding, removing, or relaxing a bound or `where` clause, or changing the "
        "signature. Examples:\n"
        "  WRONG: \"Add `E: std::error::Error` to the where clause so the call "
        "type-checks.\"  (the bound is frozen)\n"
        "  WRONG: \"Change the return type to `Result<T, MyError>`.\"  (the "
        "signature is frozen)\n"
        "  RIGHT: \"In the body, build the value with the in-scope "
        "`Error::new(value)` whose bound the frozen signature already satisfies.\"\n"
        "  RIGHT: \"Call `value.into()` inside the body instead of widening the "
        "generic bound.\"\n"
        # A frozen bound is usually a TRAIT whose own methods ARE the in-scope calls.
        # A constructor erroring E0277 means it demands a DIFFERENT bound — route
        # through the frozen bound's trait method instead. Exception: raw-pointer or
        # vtable-access patterns cannot be satisfied via trait methods → NEEDS_VTABLE_ACCESS.
        "BOUND-MISMATCH RULE: when a free constructor (`Type::make_from(v)`) errors "
        "E0277 because your generic does not satisfy ITS bound, the FROZEN `where` "
        "bound on that generic is itself a trait whose methods you CAN call on the "
        "value. Route through that method (see <BOUND_METHODS>), e.g. "
        "`self.map_err(|v| v.bound_method(arg))` — this is TRAIT_BOUND_VIOLATION "
        "with a concrete fix, NOT GENUINELY_HARD. Only classify GENUINELY_HARD when "
        "the frozen bound provides NO method usable here AND no in-scope value/call "
        "satisfies it. Exception: if the error involves raw-pointer dereference or "
        "vtable-layout access, classify as NEEDS_VTABLE_ACCESS instead.\n\n"
        "Classify the root cause in ONE of these categories:\n"
        "  WRONG_TYPE_CHOICE       — used wrong wrapper/type (e.g. Box vs NonNull vs Own)\n"
        "  WRONG_DEREF_PATTERN     — *ptr vs .as_ref() vs .deref(); name the correct chain\n"
        "  MISSING_UNSAFE_PRIMITIVE— body needs unsafe { Self::X() } or similar; name X\n"
        "  TRAIT_BOUND_VIOLATION   — a BODY value fails a FROZEN bound; name an in-scope "
        "value/call that satisfies it — usually a method the frozen bound's own trait "
        "provides (see <BOUND_METHODS>), NOT a bound to add. Only if truly none → GENUINELY_HARD\n"
        "  API_HALLUCINATION       — model called a method that does not exist; name it\n"
        + language_driver().vtable_category_line() + "\n"
        "  GENUINELY_HARD          — unfixable within the frozen body: a bound/where/sig change "
        "is the ONLY route AND the frozen bound's trait offers no usable method (see "
        "<BOUND_METHODS>), or fundamental layout unknown; flag for skip\n\n"
        "Output EXACTLY two lines:\n"
        "  CATEGORY: <one of the above>\n"
        "  GUIDANCE: <one paragraph: a BODY-ONLY change to try, specific to this fn>\n\n"
        "The GUIDANCE must be a concrete BODY edit (e.g. \"Use "
        "`self.inner.by_ref().as_ref()` to traverse the NonNull wrapper "
        "instead of `*self.inner`\"), never a signature/bound/where change and "
        "never a generic 'try harder' platitude."
    )


def build_whole_tree_retro_prompt(
    error_excerpt: str, history: list[dict[str, Any]], *, name: str, fix_err_cap: int,
) -> str:
    """Tree-wide compile-error retrospective prompt: asks the model why fix-loop
    ``name`` stopped reducing the rustc error count and what categorically
    different approach to try next. Pure assembly (mirrors the per-fn prompt at
    the whole-tree level)."""
    recent = history[-3:] if history else []
    history_block = ""
    for h in recent:
        summary = h.get("summary") or h.get("fix_summary") or ""
        err = (h.get("error_sig") or "")[:600]
        history_block += (
            f"\nIter {h.get('iter','?')}: {summary}\n"
            f"  metric_before={h.get('metric','?')} → "
            f"metric_after={h.get('post_fix_metric','?')}\n"
            f"  error_sig: {err}\n"
        )
    prior_retro = ""
    for h in reversed(recent):
        if h.get("retro"):
            prior_retro = h["retro"]
            break
    prior_block = (
        f"\nPrior retrospective said:\n{prior_retro}\n"
        "If that guidance did not move the error floor, escalate to a\n"
        "DIFFERENT strategy this time.\n"
    ) if prior_retro else ""
    return (
        f"You are diagnosing why fix-loop `{name}` has stopped reducing\n"
        f"the rustc error count across multiple attempts.\n\n"
        f"Recent attempts:\n{history_block}\n"
        f"Current error excerpt:\n```\n{error_excerpt[:fix_err_cap]}\n```\n"
        f"{prior_block}\n"
        "Output EXACTLY two lines:\n"
        "  CATEGORY: <one short tag — e.g. WRONG_FILE_TARGETED, "
        "TRAIT_HEADER_MISMATCH, UNSIZED_RETURN, DUPLICATE_IMPL, "
        "GENUINELY_HARD>\n"
        "  GUIDANCE: <one paragraph — what concretely different approach to "
        "try next iter. Reference specific symbols, fns, or rustc error codes.>\n\n"
        "GUIDANCE must be actionable, not 'try harder'. If two iters in a row\n"
        "with non-trivial strategy changes both failed to move the floor,\n"
        "set CATEGORY: GENUINELY_HARD."
    )
