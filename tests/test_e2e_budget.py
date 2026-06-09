"""L1 e2e budget tests — CODE_ARCHITECTURE.md R10.2 (W3 + W4(e)).

Pins the ctx-budget-aware capping invariants that W3/W4 shipped, so a future cap
change is decided by this test, not a 5h live run. `request_max_tokens` is pure —
no model, no I/O.

Invariants under test:
  1. content_budget overrides ONLY the mis-derived global sentinel; an explicit
     caller bound (e.g. the retro's max_tokens=6144) is preserved (W4(e)).
  2. cap_by_ctx shrinks the content cap as input grows (ctx-budget-aware) — the
     "thinking interrupted everywhere" class W3 fixed.
  3. thinking gets its OWN reserve on top of content (request_max = content +
     thinking), so reasoning is never cut to fit content.
"""
from __future__ import annotations

from pipeline_core.llm.budget import BudgetSpec, BudgetState, request_max_tokens

_SENTINEL = 999_999  # the mis-derived global MAX_TOKENS default


def _spec(**over) -> BudgetSpec:
    base = dict(
        ctx_len=32_768, content_budget=16_384, thinking_budget=16_384,
        margin=512, cpt_floor=3.0, adaptive_cpt=False,
        add_thinking_to_request=True, cap_by_ctx=True, send_max_tokens=True,
        thinking_char_cap=None,
    )
    base.update(over)
    return BudgetSpec(**base)


def _state() -> BudgetState:
    return BudgetState(cpt=3.0)


def test_explicit_caller_bound_is_preserved() -> None:
    """W4(e): a deliberate per-call content bound (retro's 6144) is NOT replaced
    by the data-driven content_budget — only the sentinel default is."""
    plan = request_max_tokens(
        _spec(), _state(), input_chars=3_000, caller_max_tokens=6_144,
        default_sentinel=_SENTINEL, enable_thinking=True)
    assert plan.content_cap == 6_144  # explicit bound honored, not 16_384


def test_sentinel_uses_data_driven_content_budget() -> None:
    """The mis-derived global default IS overridden by content_budget. cap_by_ctx
    off here to isolate the sentinel-override from the ctx-shrink (tested separately)."""
    plan = request_max_tokens(
        _spec(cap_by_ctx=False), _state(), input_chars=3_000,
        caller_max_tokens=_SENTINEL, default_sentinel=_SENTINEL, enable_thinking=True)
    assert plan.content_cap == 16_384


def test_content_cap_shrinks_as_input_grows() -> None:
    """W3: cap_by_ctx makes the content cap ctx-budget-aware — a near-full context
    leaves less room, so the cap shrinks below the nominal content_budget."""
    small = request_max_tokens(
        _spec(), _state(), input_chars=3_000, caller_max_tokens=_SENTINEL,
        default_sentinel=_SENTINEL, enable_thinking=True)
    large = request_max_tokens(
        _spec(), _state(), input_chars=30_000, caller_max_tokens=_SENTINEL,
        default_sentinel=_SENTINEL, enable_thinking=True)
    assert large.content_cap < small.content_cap
    assert large.content_cap >= 512  # never collapses below the floor


def test_thinking_gets_its_own_reserve_on_top() -> None:
    """W3: request_max = content + thinking reserve, so reasoning isn't cut to fit
    content. Disabling thinking drops the reserve, not the content cap."""
    on = request_max_tokens(
        _spec(), _state(), input_chars=3_000, caller_max_tokens=6_144,
        default_sentinel=_SENTINEL, enable_thinking=True)
    off = request_max_tokens(
        _spec(), _state(), input_chars=3_000, caller_max_tokens=6_144,
        default_sentinel=_SENTINEL, enable_thinking=False)
    assert on.thinking_reserve == 16_384
    assert off.thinking_reserve == 0
    assert on.request_max == on.content_cap + on.thinking_reserve
    assert off.content_cap == on.content_cap  # content unaffected by thinking
