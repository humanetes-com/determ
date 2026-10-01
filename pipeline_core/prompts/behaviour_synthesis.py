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

from pipeline_core.domain.behaviour import MAY_REPRESENTATION_NOTE
from pipeline_core.prompts.sections import tag_block


def _edges_prompt(module_name: str, cfg_cluster: str,
                  edges: tuple[tuple[str, str, str, str], ...],
                  public_surface: str, example_usages: str) -> str:
    """The ask for COMPOSITIONS (`domain/use_cases.py`): per edge, a value
    built with the first item, then the second item called on it and its
    result rendered. `edges` are (first name, its declaration, second name,
    its declaration). Same output form and rules as the unit and class asks:
    the reply goes through the same parser and the same capture."""
    lines = "".join(
        f"- build with `{p}` (`{p_sig}`), then observe `{c}` (`{c_sig}`)\n"
        for p, p_sig, c, c_sig in edges)
    surface_block = tag_block("PUBLIC_API_SURFACE", public_surface)
    examples_block = tag_block(
        "EXAMPLE_USAGES", example_usages,
        prefix="How this crate's public API is CALLED. Take the calls, never the "
               "shape: these are the crate's own tests, so they assert — your "
               "<GIVEN> and <WHEN> must not.\n```rust\n",
        suffix="\n```")
    return f"""Specify this crate's observable behaviour through its PUBLIC API.

Crate: {module_name} | Cluster: {cfg_cluster}
Goal: write ONE behaviour for each of these compositions of two public items.
<GIVEN> builds a value using the first item (choose its inputs freely).
<WHEN> is the second item called on that value, as ONE expression whose
result is rendered; when the second item's result cannot be printed as it
is, render what it yields.
{lines}
{surface_block}{examples_block}
Output one block per behaviour, EXACTLY this form and nothing else:

  <BEHAVIOUR name="snake_case_name">
  <GIVEN>
  // zero or more Rust statements building the values; no printing, no asserting
  </GIVEN>
  <WHEN>a SINGLE Rust expression</WHEN>
  <OBSERVE>display|debug|alt_debug</OBSERVE>
  </BEHAVIOUR>

Rules:
- PUBLIC API ONLY. You have no access to the crate's internals — treat it as a
  black box. Never guess or transcribe how a function is implemented internally.
- <WHEN> must evaluate to a RENDERED VALUE, never to a comparison (`==`, `!=`,
  `<=`, `>=`, `.contains(` are REJECTED). The ONLY booleans allowed are
  type-identity predicates: `x.is::<T>()` or `x.downcast_ref::<T>().is_some()`.
- <WHEN> is ONE expression: never a statement, never a block, never a
  `println!`, never an `assert`. <GIVEN> may not print and may not assert.
- `{module_name}`'s public items are ALREADY imported (`use {module_name}::*;`
  sits above the `fn main()` your <GIVEN> goes into). Any OTHER import goes at
  the top of <GIVEN> as a normal `use` line.
- You do NOT know the value. Never write it down; the harness prints it.
"""


def build_behaviour_synthesis_prompt(
    *,
    module_name: str,
    cfg_cluster: str,
    target_units: tuple[str, ...],
    public_surface: str,
    symbol_inventory: str,
    example_usages: str = "",
    target_classes: tuple[tuple[str, str], ...] = (),
    target_edges: tuple[tuple[str, str, str, str], ...] = (),
) -> str:
    """Prompt the model for behaviours in the CONSTRAINED shape: setup
    statements, ONE expression, and the render kind. The harness prints the
    expression against the real lib and records what it printed as the Then, so
    the model never guesses a value — and the same three parts render back as an
    asserting `#[test]` for replay.

    Measured 2026-08-27 on this crate and model: the previous instruction ("a
    self-contained `fn main()` … PRINTS the observable") produced 0 of 6
    mechanically-assertable captures — every reply was a multi-`println!` blob
    stored as NORMALIZED_STDOUT and classified OTHER, which no generated test can
    pin and which t1.5's cap discards for ranking last. The constrained shape
    produced 7 of 7 across two variants.

    `target_classes` is `(class, description)` pairs: when given, the request is
    for a BEHAVIOUR of that class rather than for a unit, because behaviours are
    transversal and a unit-shaped ask cannot reach one. The class is stated TO
    the model and never read back from the reply — measured 0 of 3 self-assigned
    classes correct against 4 of 4 when supplied, and `classified()` re-derives
    the class from kind and text at storage time regardless."""
    if target_edges:
        return _edges_prompt(module_name, cfg_cluster, target_edges, public_surface, example_usages)
    targets = ", ".join(target_units) or "(the crate's public API)"
    class_lines = "".join(f"- {k}: {d}\n" for k, d in target_classes)
    # Asked by CLASS, the model still needs CALLABLE NAMES: a class names what
    # to observe, not what to call. `symbol_inventory` is a constant "" on
    # every live run (the same empty collaborator t1.5 receives), and
    # `tag_block` omits an empty tag. Filling it from PUBLIC_API_SURFACE would
    # dump that block twice. The leftover unit names below are the inventory.
    goal = (
        f"Goal: write ONE behaviour for each of these classes, choosing "
        f"whatever public API exercises it:\n{class_lines}"
        f"\nPublic units available to call (nothing here is required; use "
        f"whatever the behaviour needs): {targets}\n"
        if target_classes
        else f"Goal: exercise these units through PUBLIC API only: {targets}\n"
    )
    surface_block = tag_block("PUBLIC_API_SURFACE", public_surface)
    symbols_block = tag_block("IN_SCOPE_SYMBOLS", symbol_inventory)
    # NOT "imitate the STYLE of". The only worked example this prompt carries
    # is a harvested `given`, which is a whole `#[test]` item INCLUDING its
    # `assert_eq!` — computed on the live suite, the first one is
    # `fn test_boxed_anyhow() { … assert_eq!("oh no!", …); }`. Labelled as the
    # style to imitate it instructs the exact shape the two rules below refuse,
    # which is the same defect as the free-form `fn main()` this prompt was
    # rewritten to stop asking for, one level down. It is here for the CALLS.
    examples_block = tag_block(
        "EXAMPLE_USAGES",
        example_usages,
        prefix="How this crate's public API is CALLED. Take the calls, never the "
        "shape: these are the crate's own tests, so they assert — your "
        "<GIVEN> and <WHEN> must not.\n```rust\n",
        suffix="\n```",
    )
    return f"""Specify this crate's observable behaviour through its PUBLIC API.

Crate: {module_name} | Cluster: {cfg_cluster}
{goal}
{surface_block}{symbols_block}{examples_block}
Output one block per behaviour, EXACTLY this form and nothing else:

  <BEHAVIOUR name="snake_case_name">
  <GIVEN>
  // zero or more Rust statements building the values; no printing, no asserting
  </GIVEN>
  <WHEN>a SINGLE Rust expression</WHEN>
  <OBSERVE>display|debug|alt_debug</OBSERVE>
  </BEHAVIOUR>

Rules:
- PUBLIC API ONLY. You have no access to the crate's internals — treat it as a
  black box. Never guess or transcribe how a function is implemented internally.
- <WHEN> must evaluate to a RENDERED VALUE, never to a comparison. Write
  `err.chain().nth(1).unwrap().to_string()`, NOT `... == "inner"`. A `when`
  containing `==`, `!=`, `<=`, `>=` or `.contains(` is REJECTED: the harness
  records what it prints, so a boolean discards the value being specified.
- The ONLY booleans allowed are type-identity predicates: `x.is::<T>()` or
  `x.downcast_ref::<T>().is_some()`. For those the type IS the pin.
- <WHEN> is ONE expression: never a statement, never a block, never a
  `println!`, never an `assert`.
- <GIVEN> may not print and may not assert. The pair (<GIVEN>, <WHEN>) must
  compile on its own inside `fn main()`.
- `{module_name}`'s public items are ALREADY imported for you (`use
  {module_name}::*;` sits above the `fn main()` your <GIVEN> goes into, so the
  macros and types are in scope). Any OTHER import your setup needs — a `std`
  trait such as `std::error::Error`, an alias — goes at the top of <GIVEN> as a
  normal `use` line.
- You do NOT know the value. Never write it down; the harness prints it.
- Observe OUTPUTS, never internal call-order. The observable IS the behaviour.
- Do NOT state a class. It is already known.
- {MAY_REPRESENTATION_NOTE}
"""
