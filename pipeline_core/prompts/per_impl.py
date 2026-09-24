"""Per-impl-block prompt builder — fills every stub in a block.

Moved verbatim from pipeline.py. Pure text→text; impure collaborators
come from :mod:`pipeline_core.prompts.runtime`.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from pipeline_core.domain.construction_recipe import construction_recipes_for
from pipeline_core.domain.coverage_slice import instruction_opener
from pipeline_core.domain.crash_site import crash_evidence_prefix
from pipeline_core.domain.language_driver import language_driver
from pipeline_core.domain.last_error import (
    LastErrorKind,
    last_error_prefix,
    resolve_kind,
)
from pipeline_core.domain.last_error import (
    faulted_clause as _faulted_clause,
)
from pipeline_core.domain.models import ImplBlock
from pipeline_core.domain.pair_fill import extra_quoted_members, extra_write_members
from pipeline_core.domain.prompt_holdings import FileView, file_block
from pipeline_core.prompts.context import PerImplSections
from pipeline_core.prompts.fix_prompts import FN_LITERAL_RULE
from pipeline_core.prompts.runtime import promoted_error_rules, repo_spec_section
from pipeline_core.prompts.sections import (
    format_impl_block_for_prompt,
    tag_block,
)

SIBLING_FOLLOW_PREFIX = (
    "Already-filled fns in OTHER impl blocks of this file with the same name "
    "as a TARGET stub — follow the same structural pattern:\n```rust\n")


def last_error_is_compiling_crash(
        last_error: str, kind: LastErrorKind | None = None) -> bool:
    """True when LAST_ERROR is a rustc-accepted body that then FAULTED.

    A compile excerpt (`error[E…]`) is not this class, and neither is a
    run that merely lost tests — that one gives no call to point away
    from. Used to pick the joint-write opener and the LAST_ERROR prefix:
    the unentered-arm sentence is how a compiling crash is resubmitted
    (run `642983-68b474a4` fills 1–2: LAST_ERROR already said do not
    resubmit the same approach; pair_part said call the implementer on
    this arm; the thinking quoted pair_part).

    ``kind`` is the verdict the code that MEASURED the failure recorded,
    and it decides. Reading it out of the prose is the fallback, and only
    the fallback, because that is what failed: this function's substring
    list wanted `binaries crashed` while the renderer writes
    `test binaries [...] CRASH before reporting results`, so it answered
    False on every crash for as long as both existed. Run
    `681204-b695ab3f` (2026-09-14) launched on the fix that reads this
    predicate and its first pair prompt still carried the compile
    wording and the unentered-arm sentence.
    """
    return resolve_kind(kind, last_error).has_faulting_call


def pair_joint_write_paragraph(last_error: str,
                               kind: LastErrorKind | None = None) -> str:
    """PAIRED WRITE opener when both halves stay refill.

    After a compiling fault, do not tell the model the implementer is
    unentered because the dispatcher does not yet call it: that call is
    what LAST_ERROR shows faulted. Both halves still refill (a leftover
    that already enters the implementer still fails). Control: empty or
    compile-only LAST_ERROR keeps the unentered-arm sentence, which is
    still the first-write instruction.
    """
    resolved = resolve_kind(kind, last_error)
    if resolved.has_faulting_call:
        did, _below, that = _faulted_clause(resolved)
        return (
            "PAIRED WRITE — this response fills BOTH target blocks, in two "
            "different files, as ONE design. The first block dispatches a "
            "format trait; the second is the implementer it delegates to. "
            "They are accepted or rejected TOGETHER, so design the "
            "delegation and the implementation to agree: neither half is "
            "correct alone. LAST_ERROR below is a body rustc accepted that "
            f"then {did}. Do not emit {that}. "
            "The implementer is still a rewrite: a leftover that "
            "already enters it still fails. Reach the implementer from the "
            f"arm that {did} by a call that is not that expression. A "
            "pinned output substring in REFILL_TARGETS is emitted by this "
            "pair. Debug or Display of a field cannot produce a substring "
            "that field's own formatter does not print — write the literal "
            "in the implementer.\n\n"
        )
    return (
        "PAIRED WRITE — this response fills BOTH target blocks, in two "
        "different files, as ONE design. The first block dispatches a "
        "format trait; the second is the implementer it delegates to. "
        "They are accepted or rejected TOGETHER, so design the "
        "delegation and the implementation to agree: neither half is "
        "correct alone. The implementer is a rewrite even when coverage "
        "of the current tree shows the failing tests do not enter it: "
        "they do not enter it because the dispatcher does not yet call "
        "it on this arm, which is the other half of this write. A "
        "pinned output substring in REFILL_TARGETS is emitted by this "
        "pair. Debug or Display of a field cannot produce a substring "
        "that field's own formatter does not print — write the literal "
        "in the implementer.\n\n"
    )


def sibling_reference_prefix(rejected: tuple[str, ...]) -> str:
    """The SIBLING_REFERENCE lead-in. With no rejected expression in the
    sibling (attempt 1, or a sibling that does not carry what LAST_ERROR
    names) it endorses the pattern; otherwise it names the contradiction —
    the sibling is shown so the model can see what THIS block must do
    differently, not as a template. The sibling stays in the prompt either
    way: the same body is visible as a RELATED block, so dropping the section
    would remove the place the contradiction is named, not the pattern."""
    if not rejected:
        return SIBLING_FOLLOW_PREFIX
    exprs = ", ".join(f"`{e}`" for e in rejected)
    return (
        "Already-filled fns in OTHER impl blocks of this file with the same "
        f"name as a TARGET stub. CAUTION — this sibling's body carries {exprs}: "
        "the expression your previous attempt wrote and LAST_ERROR above "
        "rejected under THIS block's bounds. The sibling compiles only because "
        "its impl header bounds its types differently. Do NOT follow its "
        "structural pattern here — reach the same result by a path THIS "
        "block's bounds allow (TYPES_IN_SCOPE and BOUND_METHODS name the "
        "fields and methods available):\n```rust\n")


def sibling_rejected_note(rejected: tuple[str, ...]) -> str:
    """The same contradiction as :func:`sibling_reference_prefix`, for a
    retry turn: the SIBLING_REFERENCE above was introduced as a pattern to
    follow, and it carries the expression this result rejected."""
    if not rejected:
        return ""
    exprs = ", ".join(f"`{e}`" for e in rejected)
    return (f"CAUTION — the SIBLING_REFERENCE above carries {exprs}: the "
            "expression your previous reply wrote and this result rejected "
            "under THIS block's bounds. The sibling compiles only because its "
            "impl header bounds its types differently. Do NOT follow its "
            "structural pattern here — reach the same result by a path THIS "
            "block's bounds allow (TYPES_IN_SCOPE and BOUND_METHODS name the "
            "fields and methods available).")


def build_per_impl_prompt(
    block: ImplBlock,
    related_blocks: list[ImplBlock],
    test_blocks: list[tuple[str, str]],
    module_name: str,
    cluster_name: str,
    sections: PerImplSections,
    *,
    fr_path: Path | None = None,
    partner_block: ImplBlock | None = None,
    extra_blocks: Sequence[ImplBlock] = (),
) -> str:
    """Per-impl-block prompt — fills every stub fn in `block` and (when
    `related_blocks` is non-empty) jointly designs sibling impls of the
    same trait so the model can see all trait-header variants at once.

    `block` is a record from `_collect_impl_blocks`. `related_blocks` are
    other impl blocks in the same file whose trait name matches (used to
    surface the `impl AsRef<T1>` + `impl AsRef<T2>` case that per-fn
    prompts cannot show together).

    `sections`: every injected section as a frozen no-default record.

    `partner_block` is the pair route's two-block write list: a SECOND
    target block, from ANOTHER file, rendered as its own
    `<TARGET_IMPL_BLOCK file=…>` and named in the write list — the
    co-dependent half of a dispatcher/implementer transaction (a format
    dispatcher's delegation arm and the sibling-file implementer it
    delegates to are each rejected by the revert gate alone; the pair
    lands them under one judgment). None leaves the prompt byte-identical
    to the single-block render.

    `extra_blocks` are same-file table slots a compiling crash-revert
    recovered. Stub and refill extras are rewritten with the pair;
    refill extras are tagged aimed (callee of the faulting formatter),
    never "its test below FAILS" — those tests often still pass.
    Members-only extras (remembered, current crash extract did not
    name them) are quoted, not rewritten. Empty (the default) leaves
    the two-block render.
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
    _refill_names = frozenset(m.fn_name for m in refill_members)
    _quoted_names = frozenset(
        m.fn_name for m in extra_quoted_members(block))
    target_render = format_impl_block_for_prompt(
        block, mark_target=True,
        refill_fn_names=_refill_names,
        # Only the cluster-active stubs are asked for; a stub the cluster's
        # cfg never compiles is rendered as one the model must NOT answer
        # (see `format_impl_block_for_prompt`).
        fill_fn_names=frozenset(m.fn_name for m in stub_members),
        companion_fn_names=_quoted_names,
        inferred_fn_names=sections.inferred_refill_names,
        crashed_fn_names=sections.crashed_refill_names,
        aimed_fn_names=sections.aimed_refill_names)
    related_renders = []
    for rb in related_blocks:
        related_renders.append(
            format_impl_block_for_prompt(
                rb, mark_target=False,
                # A same-named member that is ALSO a re-fill target for the
                # same failing tests is the competing candidate: shown in
                # full, not as a settled `/* FILLED */` preview.
                candidate_fn_names=_refill_names & frozenset(
                    m.fn_name for m in rb.refill_members)))
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
    # Sibling reference (per-impl variant): already-filled fns elsewhere in
    # this file sharing a stub member's name. When the sibling body carries an
    # expression LAST_ERROR just rejected, the two sections contradict each
    # other and the worked example wins over the diagnostic (run
    # `902902-daee577d`, 2026-08-20: attempt 2 of `ContextError<C, Error>::
    # source` re-wrote the sibling's `Some(&self.error)` into the same E0277)
    # — so the prefix names the contradiction instead of endorsing the pattern.
    siblings_block = tag_block(
        "SIBLING_REFERENCE", siblings_section,
        prefix=sibling_reference_prefix(sections.sibling_rejected),
        suffix="\n```")
    types_block = tag_block("TYPES_IN_SCOPE", types_section)
    # Methods the impl's frozen where-bounds make callable — prefer a
    # bound-provided trait method over a mis-bound free constructor (the
    # E0277 thrash root).
    bound_methods_block = tag_block("BOUND_METHODS", sections.bound_methods)
    # The previous attempt's own compiler/test verdict, verbatim. Without it
    # a retry is blind: the error text reached only the TYPES_IN_SCOPE
    # re-selection, so an error naming no new types rebuilt attempt 1's
    # prompt byte-for-byte and attempt 2 was a temperature resample (both
    # E0277 units of run `275772-425da59c`, 2026-08-19, same prompt sha on
    # both attempts while other units' attempt-2 prompts differed).
    last_error_block = tag_block(
        "LAST_ERROR", sections.last_error,
        prefix=last_error_prefix(sections.last_error,
                                 sections.last_error_kind))
    invariants_block = tag_block("CLUSTER_INVARIANTS", invariants_section)
    trait_block = tag_block(
        "TRAIT_CONTRACT", language_driver().trait_contract(container))
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
        prefix=crash_evidence_prefix(
            "A test binary CRASHED and fns of this block are on the "
            "faulting or origin stack. This is runtime evidence from the "
            "actual crash, stronger than any failing-test guess:\n",
            sections.crashed_refill_names))
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
    # Members flagged [REFILL] above compile but their attributed test still
    # fails — usually because they disagree with a sibling stub/refill member
    # on a shared private scheme. Rewrite them TOGETHER with the stubs so the
    # whole block agrees.
    # With evidence (`domain.refill_evidence`) the section states, per
    # member, what the record supports — DIRECT conviction or inference only,
    # the attributed tests, and what they said — instead of asserting that a
    # test the prompt did not carry "still FAILS".
    if sections.refill_evidence:
        refill_block = tag_block(
            "REFILL_TARGETS",
            sections.refill_evidence,
            prefix="Members marked [REFILL] / [REFILL?] / [REFILL!] in "
                   "TARGET_IMPL_BLOCK "
                   "above already COMPILE. What the record supports for each, "
                   "and the failing tests' own words (their source is under "
                   "Tests below). If a member shares a private field/scheme "
                   "with a [STUB] member, design BOTH consistently in this "
                   "same response.\n")
    else:
        refill_block = tag_block(
            "REFILL_TARGETS",
            ", ".join(m.fn_name for m in refill_members),
            prefix="These members are marked [REFILL] in TARGET_IMPL_BLOCK "
                   "above: they already COMPILE but their attributed test "
                   "(see Tests below) still FAILS. Rewrite their BEHAVIOR — "
                   "keep the signature, do not just resubmit the same body. "
                   "If they share a private field/scheme with a [STUB] member, "
                   "design BOTH consistently in this same response.\n")

    # Output protocol example.
    output_example: list[str] = []
    for m in (*stub_members, *refill_members)[:2]:  # show up to 2 worked
                                # examples in the output format
        output_example.append(
            f'<FN name="{m.fn_name}" block="{container}">\n'
            "  // body (no outer braces, no signature)\n"
            "</FN>"
        )
    if partner_block is not None:
        for m in (*partner_block.stub_members,
                  *partner_block.refill_members)[:1]:
            output_example.append(
                f'<FN name="{m.fn_name}" block="{partner_block.container}">\n'
                "  // body (no outer braces, no signature)\n"
                "</FN>"
            )
    for eb in extra_blocks:
        for m in (*eb.stub_members, *eb.refill_members)[:1]:
            eblock = eb.container or "free fns"
            output_example.append(
                f'<FN name="{m.fn_name}" block="{eblock}">\n'
                "  // body (no outer braces, no signature)\n"
                "</FN>"
            )
    output_example_str = (("\n".join(output_example) if output_example else
                           '<FN name="..." block="...">\nbody\n</FN>')
                          + "\n" + FN_LITERAL_RULE)

    partner_section = ""
    pair_part = ""
    partner_names = ""
    partner_file = ""
    if partner_block is not None:
        partner_file = partner_block.file
        _p_refills = frozenset(
            m.fn_name for m in partner_block.refill_members)
        partner_render = format_impl_block_for_prompt(
            partner_block, mark_target=True,
            refill_fn_names=_p_refills,
            fill_fn_names=frozenset(
                m.fn_name for m in partner_block.stub_members),
            inferred_fn_names=sections.inferred_refill_names,
            crashed_fn_names=sections.crashed_refill_names,
            aimed_fn_names=sections.aimed_refill_names)
        partner_section = (
            f"\n<TARGET_IMPL_BLOCK file=\"{partner_block.file}\">\n"
            "```rust\n"
            f"{partner_render}\n"
            "```\n</TARGET_IMPL_BLOCK>\n")
        partner_names = ", ".join(
            m.fn_name for m in (*partner_block.stub_members,
                                *partner_block.refill_members))
        if extra_write_members(block):
            pair_part = pair_joint_write_paragraph(
                sections.last_error, sections.last_error_kind)
        else:
            pair_part = (
                "PAIRED WRITE — coverage shows failing tests already enter "
                "the implementer through the current dispatcher. This "
                "response rewrites the implementer only. The dispatcher is "
                "marked FILLED: do NOT emit an <FN> for it. Wiring a "
                "dispatcher arm that does not yet call the implementer in "
                "the same keep is how a compiling pair crash-reverts — "
                "that arm is a later write, after this one keeps. A pinned "
                "output substring in REFILL_TARGETS is emitted by the "
                "implementer. Debug or Display of a field cannot produce a "
                "substring that field's own formatter does not print — "
                "write the literal in the implementer.\n\n")
    extra_section = ""
    extra_names = ""
    if extra_blocks:
        extra_renders: list[str] = []
        extra_name_bits: list[str] = []
        extra_quote_bits: list[str] = []
        for eb in extra_blocks:
            _e_refills = frozenset(m.fn_name for m in eb.refill_members)
            _e_fills = frozenset(m.fn_name for m in eb.stub_members)
            _e_quoted = frozenset(
                m.fn_name for m in extra_quoted_members(eb))
            # Crash-named extras arrive as refill because the panic stem
            # matched their return, not because their attributed tests
            # fail. The FAILS tag is a lie for them; aimed is the
            # callee-of-the-faulting-formatter instruction that already
            # exists. Union, do not replace: a slot the evidence already
            # aimed stays aimed.
            extra_renders.append(
                f'<TARGET_IMPL_BLOCK file="{eb.file}">\n'
                "```rust\n"
                + format_impl_block_for_prompt(
                    eb, mark_target=True,
                    refill_fn_names=_e_refills,
                    fill_fn_names=_e_fills,
                    companion_fn_names=_e_quoted,
                    inferred_fn_names=sections.inferred_refill_names,
                    crashed_fn_names=sections.crashed_refill_names,
                    aimed_fn_names=sections.aimed_refill_names | _e_refills)
                + "\n```\n</TARGET_IMPL_BLOCK>")
            extra_name_bits.extend(
                m.fn_name for m in extra_write_members(eb))
            extra_quote_bits.extend(sorted(_e_quoted))
        extra_names = ", ".join(extra_name_bits)
        extra_section = "\n" + "\n".join(extra_renders) + "\n"
        if extra_name_bits:
            pair_part += (
                "TABLE SLOTS — this response also rewrites the same-file "
                "fns listed below as [STUB]/[REFILL]. A previous joint "
                "write compiled and crash-reverted through fn-pointer "
                "fields these fns match. They are accepted or rejected "
                "WITH the pair; rewriting the pair without them is the "
                "split this write exists to close.\n\n")
        if extra_quote_bits:
            pair_part += (
                "TABLE SLOTS — the same-file fns marked FILLED below are "
                "the current bodies a previous joint write called through "
                "fn-pointer fields. They compile and their tests pass. "
                "Call them from the pair; do NOT emit an <FN> for them. "
                "Rewriting them is how a joint write reddens tests those "
                "bodies already pay. Coverage showing they are unentered "
                "is expected: the current pair members do not call them.\n\n")
    stub_names = ", ".join(m.fn_name for m in (*stub_members, *refill_members))
    if partner_names:
        stub_names = (f"{stub_names} ({file_rel}); "
                      f"{partner_names} ({partner_file})")
    if extra_names:
        stub_names = f"{stub_names}; {extra_names} (table slots)"
    _recipe_members = (*stub_members, *refill_members)
    recipe_text = construction_recipes_for(
        (m.fn_name for m in _recipe_members), container,
        sigs={m.fn_name: (m.sig_text or "") for m in _recipe_members})
    recipe_part = (f"{recipe_text}\n\n" if recipe_text else "")
    identity_part = (
        f"{sections.identity_must}\n\n" if sections.identity_must.strip() else "")
    coverage_part = instruction_opener(
        file_rel, (m.fn_name for m in (*stub_members, *refill_members)))
    own_file_block = (
        "\nThe whole file this block lives in, as this cluster compiles it — "
        "the TARGET_IMPL_BLOCK below is part of it; read the other items "
        "here:\n" + file_block(file_rel, sections.own_file, view=FileView.WHOLE)
        + "\n") if sections.own_file.strip() else ""
    return f"""Implement the marked [STUB] fns in this Rust impl block so the
tests pass. The unit of work is the IMPL BLOCK — design every member
together; each fn must respect the impl header's generic parameters,
where-clauses, and trait header. A fn may add a TIGHTER where-clause if
its body needs it (e.g. `where T: Sized` on a fn returning `T` by value
when the impl block declares `T: ?Sized`).

Crate: {module_name} | Cluster: {cluster_name} | File: {file_rel}
Stubs to fill in this response: {stub_names}

{pair_part}{recipe_part}{identity_part}{coverage_part}<RUST_GOTCHAS>
{language_driver().gotchas_summary()}
</RUST_GOTCHAS>
{crash_block}{repo_spec_block}{blueprint_block}{retro_block}{last_error_block}{refill_block}{siblings_block}{trait_block}{types_block}{bound_methods_block}{invariants_block}{idioms_block}{recurring_block}{notes_block}{dispatch_family_section}{related_section}{own_file_block}
<TARGET_IMPL_BLOCK file="{file_rel}">
```rust
{target_render}
```
</TARGET_IMPL_BLOCK>
{partner_section}{extra_section}{macros_block}
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
- Each fn you emit MUST appear in a TARGET (or RELATED) block above.
  Emitting an `<FN>` for a fn name not in those blocks is rejected.
"""
