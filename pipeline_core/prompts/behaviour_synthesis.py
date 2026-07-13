"""Behaviour-synthesis prompt (F10 tier 3 / M7).

Ask the model to author NEW public-API usage programs for the units the harvest
missed. Legality (Hard Rule 11 / plan_1 caveat #2): the ONLY inputs are the
PUBLIC API surface + existing tests + the in-scope symbol inventory — the model's
already-allowed set, with no path to stripped bodies. A synthesized program that
does not compile+run against the REAL lib is discarded (self-filtering), so a
hallucinated composition can never become oracle. The prompt forbids transcribing
any internal call-sequence — synthesis is black-box usage, not implementation.
"""
from __future__ import annotations

from pipeline_core.prompts.sections import tag_block


def build_behaviour_synthesis_prompt(
    *,
    module_name: str,
    cfg_cluster: str,
    target_units: tuple[str, ...],
    public_surface: str,
    symbol_inventory: str,
    example_usages: str = "",
) -> str:
    """Prompt the model to write self-contained public-API programs exercising
    `target_units`. Each program prints an observable and/or asserts — the
    harness runs it against the real lib to capture the golden Then."""
    targets = ", ".join(target_units) or "(the crate's public API)"
    surface_block = tag_block("PUBLIC_API_SURFACE", public_surface)
    symbols_block = tag_block("IN_SCOPE_SYMBOLS", symbol_inventory)
    examples_block = tag_block(
        "EXAMPLE_USAGES", example_usages,
        prefix="Existing usages to imitate the STYLE of (do not copy verbatim):\n"
               "```rust\n", suffix="\n```")
    return f"""Author Rust programs that USE this crate's public API.

Crate: {module_name} | Cluster: {cfg_cluster}
Goal: exercise these units through PUBLIC API only: {targets}

{surface_block}{symbols_block}{examples_block}
Rules:
- PUBLIC API ONLY. You have no access to the crate's internals — treat it as a
  black box. Never guess or transcribe how a function is implemented internally.
- Each program is self-contained: a `fn main()` (or a `#[test]`) that constructs
  values via the public API, exercises the target unit, and OBSERVES the result
  (print the Display/Debug, assert an equality, or trigger a documented panic).
- Observe OUTPUTS, never internal call-order. The observable IS the behaviour.
- Prefer a structured observable (`println!("{{}}", x)` / `assert_eq!`) over raw
  formatting so the captured value is deterministic.

Output one or more blocks:
  <BEHAVIOUR unit="UNIT_ID">
  // self-contained program
  </BEHAVIOUR>
"""
