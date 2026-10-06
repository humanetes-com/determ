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

Two reasoning-budget shapes (``BudgetSpec.per_call_reasoning``):

- fixed: every thinking call gets the same ``thinking_budget`` reserve and the
  transport's ``thinking_char_cap`` breaks the stream at a fixed reasoning length.
- per call: the request gets the whole room its own prompt leaves,

    request_max = ctx - input - margin      (content keeps ``content_cap`` of it)

  and no fixed character cap: a call is cut when prompt + reasoning fill the
  context, or at the model profile's runaway cap (``per_call_char_cap``) when
  its healthy maximum has been measured. Why: on 2026-09-21 the
  file/impl/pair route's healthy reasoning ran p50 73,962 / p90 109,726 / max
  119,973 chars against a fixed 120,000 cap (14% of 383 calls cut), and the
  cut prompts re-asked with no cap finished naturally at 93,740–159,457 chars
  (scripts/measure_capped_uncapped.py). A fixed cap small enough to leave the
  largest prompt its room cuts the small prompts' healthy calls; a fixed cap
  large enough for them shrinks every prompt, because the planner reserves
  ``thinking_budget`` out of prompt space. In per-call mode ``thinking_budget``
  is only that planner reserve — the floor the LARGEST prompt still gets.
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
    per_call_reasoning: bool = False  # reasoning gets the room the prompt leaves (see module doc)
    # The per-call shape's runaway cap in reasoning chars, from the served
    # model's profile (`domain.model_profile`): None = no cap (Muse, measured
    # 2026-09-21), a number once a model's healthy maximum is measured.
    per_call_char_cap: int | None = None


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
    # The mid-stream reasoning cap THIS call is consumed under. The controller
    # reads it from the plan, never from the spec, so a per-call plan cannot be
    # streamed under the fixed cap.
    thinking_char_cap: int | None = None


def request_max_tokens(
    spec: BudgetSpec,
    state: BudgetState,
    *,
    input_chars: int,
    caller_max_tokens: int,
    default_sentinel: int,
    enable_thinking: bool,
    input_tokens: int | None = None,
) -> Plan:
    """Compute the request's max_tokens (and the pieces the controller reuses).
    ``input_tokens`` is the server's exact count when the transport has one;
    without it the input is estimated from the learned chars-per-token."""
    if input_tokens is not None:
        est_input = max(1, input_tokens)
    else:
        cpt_eff = max(spec.cpt_floor, state.cpt)
        est_input = max(1, int(input_chars / cpt_eff))
    # content_budget overrides ONLY the mis-derived global default; explicit bounds stay.
    if spec.content_budget and caller_max_tokens == default_sentinel:
        content_cap = spec.content_budget
    else:
        content_cap = caller_max_tokens
    return plan_for_input(spec, input_tokens=est_input, content_cap=content_cap,
                          enable_thinking=enable_thinking)


def plan_for_input(spec: BudgetSpec, *, input_tokens: int, content_cap: int,
                   enable_thinking: bool) -> Plan:
    """The plan for a request whose input is `input_tokens` (an estimate, or the
    server's padded count on a retry). The one place both reasoning shapes are
    decided, so the first request and the retry after a 400 cannot disagree."""
    thinking = spec.add_thinking_to_request and enable_thinking
    if spec.per_call_reasoning and thinking and spec.send_max_tokens:
        room = max(512, spec.ctx_len - input_tokens - spec.margin)
        content = min(content_cap, room)
        return Plan(room, input_tokens, content, room - content, spec.per_call_char_cap)
    thinking_reserve = spec.thinking_budget if thinking else 0
    if not spec.send_max_tokens:
        return Plan(None, input_tokens, content_cap, thinking_reserve,
                    spec.thinking_char_cap)
    if spec.cap_by_ctx:
        safe = max(512, spec.ctx_len - input_tokens - thinking_reserve - spec.margin)
        content_cap = min(content_cap, safe)
    return Plan(content_cap + thinking_reserve, input_tokens, content_cap,
                thinking_reserve, spec.thinking_char_cap)
