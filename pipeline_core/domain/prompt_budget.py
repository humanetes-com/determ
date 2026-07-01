"""Prompt char-budget policy (pure domain value object).

The supplementary-material caps that bound each section of an LLM prompt used to
live as ~13 loose ``_*_CAP`` module globals in pipeline.py, each an inline
``max(floor, single_call_char_limit // N)`` formula. That is one *policy* — "how
many chars each prompt section may take, as a fraction of the call budget" —
scattered as primitive constants. :class:`PromptBudgets` names it: a frozen VO
derived once from the single-call char limit, so the sizing policy is explicit,
tested, and out of the composition root.

Caps scale with the context budget on purpose (a floor so they never collapse on
a tiny window; a fraction so they grow with a large one) — never freeze at values
tuned for an old small context. ``fix_err`` is intentionally NOT here: it is a
Settings passthrough (per-attempt error budget in the fix loop), not a fraction
of the prompt budget.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PromptBudgets:
    """Per-section char caps for LLM prompt assembly, derived from the
    single-call char limit. All values are character counts except
    ``types_count``, which is a runaway-safety count of type records (the char
    cap governs; the count is only a ceiling)."""

    readme: int          # README excerpt (~5% of budget)
    profile: int         # project-profile doc (~3%)
    genesis: int         # genesis.md methodology doc (~5%)
    repo_spec: int       # REPOSITORY_SPECIFICATIONS in per-fn/per-impl prompts
    repo_spec_wide: int  # …in whole-file fix prompts (bigger budget)
    strategy: int        # coverage-strategy doc
    sibling: int         # per-sibling condensed source
    src_snippet: int     # source snippet in task0c prompts
    extra: int           # extra context files (Cargo.toml etc.)
    working: int         # "what worked before" hint
    types_section: int   # type/trait defs in fill prompt
    types_count: int     # safety-net count of type records; chars govern
    failed_body: int     # prior failed body in fix prompt

    @classmethod
    def derive(cls, single_call_char_limit: int) -> PromptBudgets:
        """Compute every cap from the single-call char budget. Each is
        ``max(floor, limit // N)`` — floor keeps a tiny window usable, the
        fraction lets caps grow with a large context window."""
        limit = single_call_char_limit
        return cls(
            readme=max(3_000, limit // 20),
            profile=max(2_000, limit // 30),
            genesis=max(3_000, limit // 20),
            repo_spec=max(2_500, limit // 10),
            repo_spec_wide=max(5_000, limit // 5),
            strategy=max(4_000, limit // 15),
            sibling=max(3_000, limit // 30),
            src_snippet=max(4_000, limit // 10),
            extra=max(3_000, limit // 20),
            working=max(800, limit // 100),
            types_section=max(8_000, limit // 3),
            types_count=max(40, limit // 6000),
            failed_body=max(2_000, limit // 12),
        )
