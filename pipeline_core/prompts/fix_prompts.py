"""Pure prompt assembly for the shared compile/test fix loop.

These are the four inner-loop fix prompts `generate_fix_loop` used to build as
inline f-strings: re-fill ONE fn body (`build_fn_fix_prompt`), re-fill a whole
impl block (`build_impl_fix_prompt`), correct a behaviourally-wrong body
(`build_behavior_fix_prompt`), and fill an unimplemented stub that fails tests
(`build_tdd_stub_fill_prompt`).

They are PURE: every dynamic section (signature/where rule, repo-spec block,
retrospective, gotchas, tests) is rendered by the caller and passed in as a
string, so this module has zero I/O and is unit-testable in isolation. The
strings are byte-for-byte the originals (a characterization test pins them).
"""

from __future__ import annotations

from dataclasses import dataclass

# The file/impl view handed to the model can itself contain HTML-escaped Rust —
# rustdoc comments legitimately write `Vec&lt;T&gt;` inside a `<code>` tag — and
# a model that has just read that mirrors the rendering into its own answer.
# `&amp;mut self.x` is not Rust: the body used to be spliced verbatim, rejected
# by the parser, and lost. Say it once, here, where the block format is defined.
FN_LITERAL_RULE = """Write RAW Rust inside the block and in `block=`. Never
XML-escape it, even where the code you were shown does:
WRONG: <FN name="get" block="impl&lt;T&gt; Holder&lt;T&gt;">
RIGHT: <FN name="get" block="impl<T> Holder<T>">
WRONG: match &amp;mut self.slot { Some(v) =&gt; v, None =&gt; return None }
RIGHT: match &mut self.slot { Some(v) => v, None => return None }"""


def build_fn_fix_prompt(
    *,
    thinking_directive: str,
    file_rel: str,
    fn_name: str,
    sig_rule: str,
    sig_text: str,
    cur_body: str,
    errs: str,
    repo_block: str,
    retro_block: str,
    gotchas_block: str,
    tests_block: str,
) -> str:
    """Re-fill ONE fn by body splice (per-fn inner cargo-check loop)."""
    return f"""ONE Rust function does not compile. Return ONLY its corrected body.
{thinking_directive}File: {file_rel} | Fn: {fn_name}
{sig_rule}{sig_text}

Current body (it does NOT compile):
{{
{cur_body}
}}

Compiler errors:
```
{errs}
```
{repo_block}{retro_block}{gotchas_block}{tests_block}
Return the corrected body as EXACTLY one block (statements only — no fn
signature, no outer braces):
<FN name="{fn_name}" block="any">
...corrected body...
</FN>
Output nothing else."""


def build_impl_fix_prompt(
    *,
    thinking_directive: str,
    file_rel: str,
    where_rule: str,
    block_src: str,
    sib_block: str,
    errs: str,
    repo_block: str,
    retro_block: str,
    gotchas_block: str,
    tests_block: str,
) -> str:
    """Re-fill a WHOLE impl block in one prompt (per-impl inner loop)."""
    return f"""A Rust impl block fails to compile. Fix the function BODIES so the
whole block compiles. Change ONLY what the errors require; leave already-correct
functions untouched. {where_rule}
{thinking_directive}File: {file_rel}
Impl block:
```rust
{block_src}
```
{sib_block}
Compiler errors:
```
{errs}
```
{repo_block}{retro_block}{gotchas_block}{tests_block}
For EACH function you change, return one block (body statements only — no fn
signature line, no outer braces). Use the function name verbatim:
<FN name="FUNCTION_NAME" block="impl">
...corrected body...
</FN>
Output nothing else."""


@dataclass(frozen=True, slots=True)
class BlockFillPlan:
    """What ONE impl block needs from the model.

    `stubs` have no body yet; `refills` already compile but their attributed
    test still fails, so the model must REWRITE them. The two are rendered
    separately because they are opposite instructions — listing a refill as a
    stub tells the model to write a body that is already there, and omitting it
    (the older stubs-only plan) rendered a refill-only block as a bare header
    with no fn names at all: an instruction to fill nothing."""

    header: str
    stubs: tuple[str, ...] = ()
    refills: tuple[str, ...] = ()
    # The subset of `refills` the record NAMES and never measured (a traversal
    # step or a name match, no test convicts it — see `domain.refill_evidence`).
    # Listed as "re-check", never as "rewrite": told with certainty that
    # `construct_from_context` failed, the file route replaced its per-type
    # vtable with a `construct_from_std` delegation that made the downcast
    # tests unreachable (run `902902-daee577d`, 2026-08-20).
    inferred: tuple[str, ...] = ()
    # The subset a futility escalation added AFTER measuring that rewriting
    # this fn's consumers moved no verdict. That is the strongest evidence the
    # loop produces about a producer, and it used to render as `inferred` —
    # the weakest instruction in the vocabulary — because the source was one
    # bool. Measured over the 39 file-scope prompts of 2026-08-21: the five
    # `construct_from_*` were writable slots in 3, all `re-check`, while
    # `rewrite` went to `from` in 20 and `new` re-check in 17. The escalation
    # had just declared those accessors futile.
    escalated: tuple[str, ...] = ()
    # Free fns of the file an ESCALATED producer could INSTALL into a
    # fn-pointer table it builds (`domain.installable_fns`). Nothing measured
    # THEM, so they get neither the escalated line (a measurement about a
    # producer) nor the re-check line (which tells the model to leave a right
    # body alone): the instruction is that the table entry is written in the
    # SAME reply as the producer that names it — the two halves of one defect,
    # which across three runs were never writable in one prompt.
    installable: tuple[str, ...] = ()


def _plan_line(p: BlockFillPlan) -> str:
    where = f"in `{p.header.splitlines()[0]}`" if p.header else "free fns"
    parts = []
    if p.stubs:
        parts.append("write: " + ", ".join(p.stubs))
    direct = tuple(r for r in p.refills
                   if r not in p.inferred and r not in p.escalated
                   and r not in p.installable)
    escalated = tuple(r for r in p.refills
                      if r in p.escalated and r not in p.installable)
    installable = tuple(r for r in p.refills if r in p.installable)
    inferred = tuple(r for r in p.refills
                     if r in p.inferred and r not in p.escalated
                     and r not in p.installable)
    if direct:
        parts.append("rewrite (a body exists, but its test still fails): "
                     + ", ".join(direct))
    if escalated:
        # States what was MEASURED and nothing more. The grade it replaces told
        # the model to leave these unchanged, and the model did: six replies
        # across three runs re-emitted `construct_from_context` as
        # `Self::construct_from_std(ContextError { context, error }, backtrace)`
        # byte-for-byte.
        parts.append("REWRITE (the fns that consume what this one produces "
                     "were rewritten over repeated sweeps and no test changed "
                     "its verdict — the defect is not in them, it is in what "
                     "this fn constructs or returns): " + ", ".join(escalated))
    if installable:
        # Nothing was measured about these bodies themselves; what was
        # measured is that rewriting the producer that names them in its table
        # moved no verdict. So the line states the relation and the
        # co-authoring requirement, not a verdict on the body.
        parts.append("REWRITE WITH THE PRODUCER (a fn-pointer table built by "
                     "an escalated producer of this file has a field of this "
                     "fn's signature; the producer was rewritten and no "
                     "verdict moved, so the body the table dispatches to is "
                     "written in the same reply — return it unchanged only if "
                     "it is already right for the type the producer erases): "
                     + ", ".join(installable))
    if inferred:
        parts.append("re-check (a body exists; implicated by NAME only — "
                     "rewrite ONLY if it is wrong for its bounds, else leave it "
                     "unchanged): " + ", ".join(inferred))
    return f"  {where} -> " + "; ".join(parts)


def build_file_fill_prompt(
    *,
    file_rel: str,
    file_text: str,
    fill_plan: list[BlockFillPlan],
    tests_block: str,
    blueprint: str,
    notes: str,
    prior_error: str = "",
    crash_evidence: str = "",
    module_name: str = "",
    cluster_name: str = "",
    gotchas: str = "",
    types_section: str = "",
    invariants_section: str = "",
    refill_evidence: str = "",
) -> str:
    """File-first fill: everything one file needs in ONE prompt, so the
    model designs the file's impls together (the coherence a fresh reply
    naturally has) instead of inventing each block locally. `prior_error`, when
    given, is the previous file-first attempt's compile/test failure — feeding
    it back lets the retry fix the SAME joint design instead of abandoning
    coherence after one miss (a bare one-shot-or-fragment gate throws a
    genuinely co-designed impl cluster to independent per-block fills too
    early).

    The last five arguments are the CRATE-LEVEL context the per-impl prompt has
    always carried and this one did not. Measured over `logs/prompts` on
    2026-08-04: of 956 file-first prompts, **0** named the cluster or carried
    `CLUSTER_INVARIANTS`, `RUST_GOTCHAS` or `TYPES_IN_SCOPE`, while 2 503 other
    prompts carried the invariants and 528 named the compiler cluster. The two
    fill routes' context sections were disjoint, neither a superset — and 318
    of the 326 file-first fills in the log are the `nightly` cluster, so in
    practice the route with less context was the route doing the harder work.

    `invariants_section` is the one that matters most here. The model is shown
    `src/error.rs` with 25 `#[cfg(error_generic_member_access)]` attributes in
    it and no statement anywhere of whether that cfg is on, so which arm of
    `backtrace_if_absent!` is live had to be guessed. A per-impl trace on the
    same crate reads "the cluster is `nightly`, and from the CLUSTER_INVARIANTS,
    `error_generic_member_access` is active"; the file-first traces reason from
    assumptions instead.

    Kept PURE per this module's contract: every section arrives rendered."""
    # `in <header> -> fn, fn`, never `<header>: fn, fn`: a model mirroring the
    # colon shape answers name="Error: root_cause", which resolves to no fn at
    # splice time and loses the body.
    plan_lines = "\n".join(_plan_line(p) for p in fill_plan)
    bp_part = (f"\n<DESIGN_BLUEPRINT>\nYOUR crate design — every body must "
               f"agree with it:\n{blueprint}\n</DESIGN_BLUEPRINT>\n"
               if blueprint.strip() else "")
    notes_part = (f"\n<MODEL_NOTES>\n{notes}\n</MODEL_NOTES>\n"
                  if notes.strip() else "")
    tests_part = (f"\nTests that exercise this file (make them pass):\n"
                  f"{tests_block}\n" if tests_block.strip() else "")
    # What the record supports for each "rewrite"/"re-check" above — DIRECT
    # conviction or inference only, the attributed tests, and what they said
    # (`domain.refill_evidence`); "" when the plan has no re-fills.
    evidence_part = (f"\n<REFILL_EVIDENCE>\nFor the bodies listed as rewrite / "
                     f"re-check above — what the failing tests actually "
                     f"establish:\n{refill_evidence}\n</REFILL_EVIDENCE>\n"
                     if refill_evidence.strip() else "")
    error_part = (f"\nYour last attempt at this file failed:\n{prior_error}\n"
                  "Fix the WHOLE file again, keeping every impl mutually "
                  "consistent.\n" if prior_error.strip() else "")
    crash_part = (f"\n<CRASH_EVIDENCE>\nA test binary CRASHED at runtime inside "
                  f"this file:\n{crash_evidence}\nAn already-written body in the "
                  "file above uses a scheme that is UNSOUND — do NOT copy that "
                  "pattern into the bodies you write, and if a stub you are "
                  "filling participates in the same scheme, implement it the "
                  "sound way.\n</CRASH_EVIDENCE>\n"
                  if crash_evidence.strip() else "")
    gotchas_part = (f"\n<RUST_GOTCHAS>\n{gotchas}\n</RUST_GOTCHAS>\n"
                    if gotchas.strip() else "")
    types_part = (f"\n<TYPES_IN_SCOPE>\n{types_section}\n</TYPES_IN_SCOPE>\n"
                  if types_section.strip() else "")
    invariants_part = (f"\n<CLUSTER_INVARIANTS>\n{invariants_section}\n"
                       "</CLUSTER_INVARIANTS>\n"
                       if invariants_section.strip() else "")
    # `Crate: X | Cluster: Y | File: Z` — the per-impl prompt's own header
    # line, verbatim, so the two routes identify the work the same way. Falls
    # back to the bare `File:` when the caller names neither.
    ident = " | ".join(p for p in (
        f"Crate: {module_name}" if module_name else "",
        f"Cluster: {cluster_name}" if cluster_name else "",
        f"File: {file_rel}") if p)
    return f"""Write EVERY function listed below for this Rust file in one pass —
design the impl blocks together so they stay mutually consistent. Keep every
signature EXACTLY as written; write bodies only.
{ident}
Functions to write (by impl block):
{plan_lines}
```rust
{file_text}
```
{gotchas_part}{invariants_part}{types_part}{crash_part}{bp_part}{notes_part}{evidence_part}{tests_part}{error_part}
For EACH function, return one block (body statements only — no fn signature
line, no outer braces). `name` is the BARE function name; the impl header goes
in `block` and NOWHERE else:
WRONG: <FN name="Error: root_cause" block="impl Error">
WRONG: <FN name="ErrorImpl::provide" block="impl ErrorImpl">
RIGHT: <FN name="root_cause" block="impl Error">
RIGHT: <FN name="provide" block="impl ErrorImpl">
{FN_LITERAL_RULE}
<FN name="FUNCTION_NAME" block="IMPL_HEADER">
...body...
</FN>
Output nothing else."""


def build_maximal_impl_prompt(
    *,
    file_rel: str,
    container: str,
    stub_names: list[str],
    file_text: str,
    tests_block: str,
    errs: str,
    notes: str,
) -> str:
    """Last resort — budgets OFF. After the budgeted per-impl loop
    exhausts, the model gets ONE attempt with the WHOLE current file, every
    referencing test verbatim, and the full focused error (a defect outside
    the block's window is invisible to the budgeted prompt)."""
    header = container.splitlines()[0] if container else "impl"
    names = ", ".join(stub_names)
    tests_part = (f"\nTests that exercise these functions (make them pass):\n"
                  f"{tests_block}\n" if tests_block.strip() else "")
    notes_part = (f"\nNotes from earlier attempts:\n{notes}\n"
                  if notes.strip() else "")
    return f"""Previous attempts to implement functions in this Rust file failed.
You now see the ENTIRE current file. Implement or CORRECT the function(s)
{names} of `{header}` so the file compiles and the tests pass — some may
already have a body that compiles but whose test still fails; rewrite its
behavior. Keep every signature EXACTLY as written; change bodies only.
File: {file_rel}
```rust
{file_text}
```
{tests_part}Compiler errors from the last attempt:
```
{errs}
```
{notes_part}For EACH function you implement, return one block (body statements only — no
fn signature line, no outer braces). Use the function name verbatim:
<FN name="FUNCTION_NAME" block="{header}">
...body...
</FN>
Output nothing else."""


def build_behavior_fix_prompt(
    *,
    thinking_directive: str,
    file_rel: str,
    fn_name: str,
    sig_text: str,
    cur_body: str,
    tests_block: str,
    errs: str,
) -> str:
    """Correct a fn that compiles but FAILS its tests (behaviour wrong)."""
    return (
        f"ONE Rust function compiles but FAILS its tests — "
        f"its behaviour is wrong. Correct the body.\n"
        f"{thinking_directive}"
        f"File: {file_rel} | Fn: {fn_name}\n"
        f"Signature (do NOT change it):\n{sig_text}\n\n"
        f"Current body (wrong — replace it):\n{{{{\n{cur_body}\n}}}}\n\n"
        f"Failing test source (the behaviour the body must satisfy):\n"
        f"```\n{tests_block}```\n"
        f"Test failure output:\n```\n{errs}\n```\n"
        f"Return the corrected body as EXACTLY one block "
        f"(statements only — no fn signature, no outer braces):\n"
        f'<FN name="{fn_name}" block="any">\n...body...\n</FN>\n'
        f"Output nothing else."
    )


def build_tdd_stub_fill_prompt(
    *,
    thinking_directive: str,
    file_rel: str,
    fn_name: str,
    sig_text: str,
    cur_body: str,
    errs: str,
) -> str:
    """Fill an unimplemented stub that compiles but fails tests."""
    return (
        f"ONE Rust function has an unimplemented body — fill it.\n"
        f"{thinking_directive}"
        f"File: {file_rel} | Fn: {fn_name}\n"
        f"Signature (do NOT change it):\n{sig_text}\n\n"
        f"Current body (unimplemented — replace it):\n{{{{\n{cur_body}\n}}}}\n\n"
        f"Test failure output:\n```\n{errs}\n```\n"
        f"Return the corrected body as EXACTLY one block "
        f"(statements only — no fn signature, no outer braces):\n"
        f'<FN name="{fn_name}" block="any">\n...body...\n</FN>\n'
        f"Output nothing else."
    )
