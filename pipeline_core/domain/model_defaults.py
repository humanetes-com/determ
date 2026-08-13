"""Per-model operating defaults, applied only where the env is silent.

`MODEL_CAPS` keys the request dialect on the model string and `model_tier`
keys the prompt tier; this module is the third member of that family — the
knob VALUES a model has been measured to work best under. Precedence is
strict and pinned by test: an explicit env setting always wins, the model
default fills in when the env is unset, and an unknown model string falls
back to the global default, byte-identical to the behavior before this
module existed.

Only measured values belong here. The evidence for the current table, all
2026-08-12, complexity-7 hardcrate + microcrate, ctx 131072, parallelism 4:

- Muse-Glimmer -> FILE_PRIMARY. Flag-on halved its hardcrate calls (30 vs
  59, runs `143454-1d3cf2ee` vs `63595-67b8afb1`) and every cluster closed
  in a single fill sweep, including the 14-fn `sprawl.rs` block in one call;
  microcrate 15 vs 21 calls (runs `135969-5b7f709c` vs `53678-337e9f7e`).
- Qwen3.6 -> DEFAULT. The same flag regressed its hardcrate (55.0 vs 32.2
  min, both clusters EXHAUSTED on the cross-file `link_a`/`link_b` contract,
  task3 104/106 before the retry; run `182955-0c302bf2` vs
  `4182269-1ad665b5`) — at file granularity it ping-pongs coupled cross-file
  pairs, rewriting one side per call against the other's stale version.

Sampling parameters are deliberately ABSENT: the measurement that would
justify a Muse top_p pin (its card ships no sampling defaults, so vLLM
serves it at temperature 1.0 with no nucleus truncation) has not been run,
and this table only carries values a run has validated.
"""

from __future__ import annotations

from pipeline_core.domain.enums import RouteOrder

# Matched case-insensitively as substrings of the served model id, so a repo
# prefix or quantization suffix ("RedHatAI/Muse-Glimmer-30B-FP8-block")
# resolves the same as the family name.
_ROUTE_ORDER_DEFAULTS: tuple[tuple[str, RouteOrder], ...] = (
    ("muse-glimmer", RouteOrder.FILE_PRIMARY),
    ("qwen", RouteOrder.DEFAULT),
)


def default_route_order(model: str) -> RouteOrder:
    """The validated route order for `model`; DEFAULT when nothing matches."""
    low = model.lower()
    for needle, order in _ROUTE_ORDER_DEFAULTS:
        if needle in low:
            return order
    return RouteOrder.DEFAULT


def resolve_route_order(env_value: str | None, model: str) -> RouteOrder:
    """Env wins; the model default fills in only when the env is silent.

    An explicitly set env var — even one naming the same value as the model
    default — resolves through `RouteOrder.coerce` exactly as before, so an
    experiment can force any combination and a garbled value degrades to
    DEFAULT loudly-in-effect rather than silently changing meaning."""
    if env_value is not None and env_value.strip():
        return RouteOrder.coerce(env_value, RouteOrder.DEFAULT)
    return default_route_order(model)
