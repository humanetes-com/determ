"""Ctx-budget-aware output sizing — the *one* place output/thinking caps are computed.

Every backend used to re-derive ``max_tokens`` from its own constants (num_ctx-8192,
a hard 8192, VLLM_THINKING_BUDGET, …). Those scattered hard caps are exactly what
silently truncated reasoning. Here a transport declares its shape once via
:class:`BudgetSpec`; :func:`request_max_tokens` turns (input size, caller intent,
learned chars-per-token) into the request's ``max_tokens`` consistently:

  request_max = min(content_budget, ctx - input - thinking_reserve - margin) + thinking_reserve

``content_budget`` only overrides the caller's value when the caller used the global
default sentinel (a mis-derived cap); a deliberate per-call bound is honored as-is,
because thinking is a *separate* reserve and never eats the caller's content room.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BudgetSpec:
    ctx_len: int                      # server context window, tokens
    content_budget: int               # data-driven content floor (0 ⇒ always use caller's value)
    thinking_budget: int              # reasoning reserve, tokens (0 ⇒ none)
    margin: int                       # safety tokens left free (KV-cache headroom)
    cpt_floor: float                  # chars/token floor for input estimation
    adaptive_cpt: bool                # learn chars/token from the server's usage stats
    add_thinking_to_request: bool     # request_max = content + thinking (vLLM) vs content only
    cap_by_ctx: bool                  # apply the ctx-budget safe() cap (False ⇒ pass content as-is)
    send_max_tokens: bool             # send max_tokens at all (False ⇒ let the server decide, Ollama-native)
    thinking_char_cap: int | None     # mid-stream reasoning char cap before forced followup (None ⇒ none)


@dataclass(slots=True)
class BudgetState:
    """Per-transport learned chars-per-token (EWM). Persists across calls."""

    cpt: float = 2.0
    alpha: float = 0.15

    def update(self, msg_chars: int, real_tokens: int) -> tuple[float, float]:
        """Fold one observation into the EWM; return (previous_cpt, new_cpt)."""
        prev = self.cpt
        if real_tokens > 0 and msg_chars > 0:
            measured = msg_chars / real_tokens
            self.cpt = (1 - self.alpha) * self.cpt + self.alpha * measured
        return prev, self.cpt


@dataclass(frozen=True, slots=True)
class Plan:
    request_max: int | None   # None ⇒ do not send max_tokens
    est_input_tokens: int
    content_cap: int
    thinking_reserve: int


def request_max_tokens(
    spec: BudgetSpec,
    state: BudgetState,
    *,
    input_chars: int,
    caller_max_tokens: int,
    default_sentinel: int,
    enable_thinking: bool,
) -> Plan:
    """Compute the request's max_tokens (and the pieces the controller reuses)."""
    cpt_eff = max(spec.cpt_floor, state.cpt)
    est_input = max(1, int(input_chars / cpt_eff))
    thinking_reserve = spec.thinking_budget if (spec.add_thinking_to_request and enable_thinking) else 0

    # content_budget overrides ONLY the mis-derived global default; explicit bounds stay.
    if spec.content_budget and caller_max_tokens == default_sentinel:
        content_cap = spec.content_budget
    else:
        content_cap = caller_max_tokens

    if not spec.send_max_tokens:
        return Plan(None, est_input, content_cap, thinking_reserve)

    if spec.cap_by_ctx:
        safe = max(512, spec.ctx_len - est_input - thinking_reserve - spec.margin)
        content_cap = min(content_cap, safe)

    return Plan(content_cap + thinking_reserve, est_input, content_cap, thinking_reserve)
