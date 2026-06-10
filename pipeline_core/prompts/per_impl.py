"""Per-impl-block prompt builder (slice 8) — fills every stub in a block.

Moved verbatim from pipeline.py. Pure text→text; impure collaborators
come from :mod:`pipeline_core.prompts.runtime`.
"""

from __future__ import annotations

from pathlib import Path

from pipeline_core.prompts.runtime import promoted_error_rules
from pipeline_core.prompts.sections import (
    format_impl_block_for_prompt,
    gotchas_summary,
    tag_block,
    trait_contract_for,
)


def build_per_impl_prompt(
    block: dict,
    related_blocks: list[dict],
    test_blocks: list[tuple[str, str]],
    module_name: str,
    cluster_name: str,
    types_section: str = "",
    invariants_section: str = "",
    notes_section: str = "",
    fr_path: Path | None = None,
    retrospective_guidance: str = "",
    crate_idioms_section: str = "",
    dispatch_family_section: str = "",
    macros_section: str = "",
    siblings_section: str = "",
) -> str:
    """Per-impl-block prompt — fills every stub fn in `block` and (when
    `related_blocks` is non-empty) jointly designs sibling impls of the
    same trait so the model can see all trait-header variants at once.

    `block` is a record from `_collect_impl_blocks`. `related_blocks` are
    other impl blocks in the same file whose trait name matches (used to
    surface the `impl AsRef<T1>` + `impl AsRef<T2>` case that per-fn
    prompts cannot show together).
    """
    file_rel = block.get("file", "")
    container = block.get("container", "")
    stub_members = block.get("stub_members", [])
    target_render = format_impl_block_for_prompt(block, mark_target=True)
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
    invariants_block = tag_block("CLUSTER_INVARIANTS", invariants_section)
    trait_block = tag_block("TRAIT_CONTRACT", trait_contract_for(container))
    recurring_block = tag_block("RECURRING_ERRORS", promoted_error_rules(fr_path))
    retro_block = tag_block(
        "RETROSPECTIVE_GUIDANCE", retrospective_guidance,
        prefix="The previous attempt at this impl block did not reduce the "
               "error count. Apply this strategy differently this time:\n")
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

    # Output protocol example.
    output_example: list[str] = []
    for m in stub_members[:2]:  # show up to 2 worked examples in the
                                # output format
        output_example.append(
            f'<FN name="{m["fn_name"]}" block="{container}">\n'
            "  // body (no outer braces, no signature)\n"
            "</FN>"
        )
    output_example_str = "\n".join(output_example) if output_example else (
        '<FN name="..." block="...">\nbody\n</FN>')

    stub_names = ", ".join(m["fn_name"] for m in stub_members)
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
{retro_block}{siblings_block}{trait_block}{types_block}{invariants_block}{idioms_block}{recurring_block}{notes_block}{dispatch_family_section}{related_section}
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
