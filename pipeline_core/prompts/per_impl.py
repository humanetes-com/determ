"""Per-impl-block prompt builder (slice 8) — fills every stub in a block.

Moved verbatim from pipeline.py. Pure text→text; impure collaborators
come from :mod:`pipeline_core.prompts.runtime`.
"""

from __future__ import annotations

from pathlib import Path

from pipeline_core.domain.models import ImplBlock
from pipeline_core.prompts.context import PerImplSections
from pipeline_core.prompts.runtime import promoted_error_rules, repo_spec_section
from pipeline_core.prompts.sections import (
    format_impl_block_for_prompt,
    gotchas_summary,
    tag_block,
    trait_contract_for,
)


def build_per_impl_prompt(
    block: ImplBlock,
    related_blocks: list[ImplBlock],
    test_blocks: list[tuple[str, str]],
    module_name: str,
    cluster_name: str,
    sections: PerImplSections,
    *,
    fr_path: Path | None = None,
) -> str:
    """Per-impl-block prompt — fills every stub fn in `block` and (when
    `related_blocks` is non-empty) jointly designs sibling impls of the
    same trait so the model can see all trait-header variants at once.

    `block` is a record from `_collect_impl_blocks`. `related_blocks` are
    other impl blocks in the same file whose trait name matches (used to
    surface the `impl AsRef<T1>` + `impl AsRef<T2>` case that per-fn
    prompts cannot show together).

    `sections` (P7): every injected section as a frozen no-default record.
    """
    types_section = sections.types
    invariants_section = sections.invariants
    notes_section = sections.notes
    retrospective_guidance = sections.retrospective_guidance
    crate_idioms_section = sections.crate_idioms
    dispatch_family_section = sections.dispatch_family
    macros_section = sections.macros
    siblings_section = sections.siblings
    file_rel = block.file
    container = block.container
    stub_members = block.stub_members
    refill_members = block.refill_members
    target_render = format_impl_block_for_prompt(
        block, mark_target=True,
        refill_fn_names=frozenset(m.fn_name for m in refill_members))
    related_renders = []
    for rb in related_blocks:
        related_renders.append(
            format_impl_block_for_prompt(rb, mark_target=False))
    related_section = ""
    if related_renders:
        related_section = (
            "\n<RELATED_IMPL_BLOCKS>\nThese impl blocks live in the SAME "
            "file as TARGET and implement the same trait name with "
            "DIFFERENT generic parameters. They MUST stay in separate impl "
            "blocks (Rust has no overloading). Design every fn together so "
            "each impl's body matches its trait header's expected return / "
            "argument types verbatim.\n```rust\n"
            + "\n\n".join(related_renders)
            + "\n```\n</RELATED_IMPL_BLOCKS>\n"
        )

    tests_section = ""
    if test_blocks:
        parts = []
        for path, txt in test_blocks:
            parts.append(f'<TEST path="{path}">\n{txt}\n</TEST>')
        tests_section = "\n\n".join(parts)
    else:
        tests_section = ("(no tests directly reference fns in this block "
                         "— they may be invoked indirectly via macros "
                         "(consult MACROS below) or sibling fns)")

    notes_block = tag_block("MODEL_NOTES", notes_section)
    macros_block = tag_block("MACROS", macros_section)
    # FR-024 sibling reference (per-impl variant): already-filled fns
    # elsewhere in this file sharing a stub member's name.
    siblings_block = tag_block(
        "SIBLING_REFERENCE", siblings_section,
        prefix="Already-filled fns in OTHER impl blocks of this file with "
               "the same name as a TARGET stub — follow the same "
               "structural pattern:\n```rust\n",
        suffix="\n```")
    types_block = tag_block("TYPES_IN_SCOPE", types_section)
    # Corrected R2/R3 (debt #28): methods the impl's frozen where-bounds make
    # callable — prefer a bound-provided trait method over a mis-bound free
    # constructor (the E0277 thrash root).
    bound_methods_block = tag_block("BOUND_METHODS", sections.bound_methods)
    invariants_block = tag_block("CLUSTER_INVARIANTS", invariants_section)
    trait_block = tag_block("TRAIT_CONTRACT", trait_contract_for(container))
    recurring_block = tag_block("RECURRING_ERRORS", promoted_error_rules(fr_path))
    blueprint_block = tag_block(
        "DESIGN_BLUEPRINT", sections.blueprint,
        prefix="YOUR crate design, written by you before filling began — every\n"
               "body in this block must agree with it:\n")
    repo_spec_block = tag_block(
        "REPOSITORY_SPECIFICATIONS",
        repo_spec_section(fr_path, file_rel=file_rel, container=container),
        prefix="The crate's own specification documents (functional "
               "requirements, build rules, API ground truth). When your plan "
               "conflicts with them, follow the spec.\n")
    retro_block = tag_block(
        "RETROSPECTIVE_GUIDANCE", retrospective_guidance,
        prefix="The previous attempt at this impl block did not reduce the "
               "error count. Apply this strategy differently this time:\n")
    crash_block = tag_block(
        "CRASH_EVIDENCE", sections.crash_evidence,
        prefix="A test binary CRASHED and fns of this block are on the "
               "faulting or origin stack. This is runtime evidence from the "
               "actual crash, stronger than any failing-test guess:\n")
    # Crate idioms — patterns extracted from already-filled fns in THIS
    # file. Surfaces e.g. anyhow's `Self::construct_from_*(arg, None)`
    # constructor family, its `ContextError { context, error }` wrapper,
    # its vtable fn-pointer dispatch syntax. Generic detectors (no
    # per-crate knowledge); the content the model sees is specific
    # because it's extracted from the live source.
    idioms_block = tag_block(
        "CRATE_IDIOMS", crate_idioms_section,
        prefix="Patterns this file already uses — design new fns to MATCH "
               "these idioms whenever your fn's situation matches one of "
               "them. Do NOT invent a new approach when one of these "
               "patterns clearly applies.\n")
    # debt #46 point 2: members flagged [REFILL] above compile but their
    # attributed test still fails — usually because they disagree with a
    # sibling stub/refill member on a shared private scheme. Rewrite them
    # TOGETHER with the stubs so the whole block agrees.
    refill_block = tag_block(
        "REFILL_TARGETS",
        ", ".join(m.fn_name for m in refill_members),
        prefix="These members are marked [REFILL] in TARGET_IMPL_BLOCK above: "
               "they already COMPILE but their attributed test (see Tests "
               "below) still FAILS. Rewrite their BEHAVIOR — keep the "
               "signature, do not just resubmit the same body. If they share "
               "a private field/scheme with a [STUB] member, design BOTH "
               "consistently in this same response.\n")

    # Output protocol example.
    output_example: list[str] = []
    for m in (*stub_members, *refill_members)[:2]:  # show up to 2 worked
                                # examples in the output format
        output_example.append(
            f'<FN name="{m.fn_name}" block="{container}">\n'
            "  // body (no outer braces, no signature)\n"
            "</FN>"
        )
    output_example_str = "\n".join(output_example) if output_example else (
        '<FN name="..." block="...">\nbody\n</FN>')

    stub_names = ", ".join(m.fn_name for m in (*stub_members, *refill_members))
    return f"""Implement the marked [STUB] fns in this Rust impl block so the
tests pass. The unit of work is the IMPL BLOCK — design every member
together; each fn must respect the impl header's generic parameters,
where-clauses, and trait header. A fn may add a TIGHTER where-clause if
its body needs it (e.g. `where T: Sized` on a fn returning `T` by value
when the impl block declares `T: ?Sized`).

Crate: {module_name} | Cluster: {cluster_name} | File: {file_rel}
Stubs to fill in this response: {stub_names}

<RUST_GOTCHAS>
{gotchas_summary()}
</RUST_GOTCHAS>
{crash_block}{repo_spec_block}{blueprint_block}{retro_block}{refill_block}{siblings_block}{trait_block}{types_block}{bound_methods_block}{invariants_block}{idioms_block}{recurring_block}{notes_block}{dispatch_family_section}{related_section}
<TARGET_IMPL_BLOCK file="{file_rel}">
```rust
{target_render}
```
</TARGET_IMPL_BLOCK>
{macros_block}
Tests (read-only):
{tests_section}

Output protocol — emit ONE `<FN>` block per stub. Match `block` to the
TARGET impl header verbatim so the splicer can route each body to the
correct impl. Multiple `<FN>` blocks per response are required when the
TARGET (or any RELATED impl) has multiple stubs you are filling.

Each `<FN>` body may optionally start with a `where` clause on its own
line to TIGHTEN the impl-level bounds for that fn alone — useful when a
fn returns T by value in an `impl<T: ?Sized>` block.

Format:
{output_example_str}

Rules:
- Body only — no fn signature, no outer braces around the body.
- Use fully-qualified paths (`std::fmt::Write`, `crate::Error`) for
  symbols not in this file's `use` block.
- No `unimplemented!()` / `todo!()` / panic-only bodies. Omit an `<FN>`
  block if you cannot implement that fn; a later pass retries.
- Each fn you emit MUST appear in the TARGET (or a RELATED) block above.
  Emitting an `<FN>` for a fn name not in the block is rejected.
"""
