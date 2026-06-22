"""Phase-C per-fn retrospective prompt (P6 engine-relocation series).

Pure prompt assembly: given the failed fn's context, produce the diagnosis
prompt that asks the model to classify the root cause and give concrete
"try X instead" guidance for the next attempt. No I/O, no LLM call.
"""

from __future__ import annotations


def build_phase_c_retro_prompt(
    fn_name: str, sig_text: str, container: str, file_rel: str,
    cluster_name: str, last_body: str, error_excerpt: str, fix_attempts: int,
    prior_retrospective: str, sibling_bodies: str,
    *, failed_body_cap: int, fix_err_cap: int,
) -> str:
    prior_section = (
        f"\nPrior retrospective for this fn (you said this LAST time it failed; "
        f"if the same approach still failed, escalate to GENUINELY_HARD):\n"
        f"{prior_retrospective}\n"
    ) if prior_retrospective.strip() else ""
    siblings_section = (
        f"\nSibling impls of `{fn_name}` already filled correctly in this file "
        f"(use as reference):\n{sibling_bodies}\n"
    ) if sibling_bodies.strip() else ""
    return (
        f"You are diagnosing why a reconstruction attempt for fn `{fn_name}` "
        f"in `{file_rel}` (impl: `{container}`, cluster: `{cluster_name}`) "
        f"could not be made to compile after {fix_attempts} attempt(s).\n\n"
        f"Signature:\n```rust\n{sig_text}\n```\n\n"
        f"Last attempted body (failed to compile):\n```rust\n{last_body[:failed_body_cap]}\n```\n\n"
        f"Compile errors from that attempt:\n```\n{error_excerpt[:fix_err_cap]}\n```\n"
        f"{prior_section}"
        f"{siblings_section}\n"
        "Classify the root cause in ONE of these categories:\n"
        "  WRONG_TYPE_CHOICE       — used wrong wrapper/type (e.g. Box vs NonNull vs Own)\n"
        "  WRONG_DEREF_PATTERN     — *ptr vs .as_ref() vs .deref(); name the correct chain\n"
        "  MISSING_UNSAFE_PRIMITIVE— body needs unsafe { Self::X() } or similar; name X\n"
        "  TRAIT_BOUND_VIOLATION   — generic bounds or where-clause mismatch; name the bound\n"
        "  API_HALLUCINATION       — model called a method that does not exist; name it\n"
        "  NEEDS_VTABLE_ACCESS     — requires reading the ErrorVTable; layout is needed\n"
        "  GENUINELY_HARD          — multiple categories or fundamental layout unknown; flag for skip\n\n"
        "Output EXACTLY two lines:\n"
        "  CATEGORY: <one of the above>\n"
        "  GUIDANCE: <one paragraph: what to try instead, specific to this fn>\n\n"
        "The GUIDANCE should be a concrete instruction (e.g. \"Use "
        "`self.inner.by_ref().as_ref()` to traverse the NonNull wrapper "
        "instead of `*self.inner`\"), NOT a generic 'try harder' platitude."
    )
