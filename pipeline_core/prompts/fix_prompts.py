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

from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace

from pipeline_core.domain.construction_recipe import (
    construction_recipe_prefix,
    construction_recipes_for,
)
from pipeline_core.domain.language_driver import language_driver
from pipeline_core.domain.coverage_slice import instruction_opener
from pipeline_core.domain.crash_site import (
    family_members_beside_a_crashed_conversion,
    ownership_transfer_must,
)
from pipeline_core.domain.installable_fns import ProducerBypass
from pipeline_core.domain.refill_evidence import typeid_pin_consequence

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
    container: str = "",
) -> str:
    """Re-fill ONE fn by body splice (per-fn inner cargo-check loop).

    ``container`` feeds the stub-time construction recipe. Empty (the
    default) leaves the prompt byte-identical for every unit the recipe
    does not name.
    """
    recipe_part = construction_recipe_prefix(fn_name, container, sig_text)
    return f"""ONE Rust function does not compile. Return ONLY its corrected body.
{thinking_directive}File: {file_rel} | Fn: {fn_name}
{sig_rule}{sig_text}

{recipe_part}Current body (it does NOT compile):
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
    # The subset a runtime fault's own frame named. It is listed FIRST and it
    # outranks `inferred` because the two are produced by the same event: a
    # crashed binary reports no verdict, so the crash strips its own blamed
    # unit of the convicting test that would have graded it `direct`, and the
    # unit arrives here looking like a name match. Run `1519852-5d607329`
    # (2026-08-30) dispatched `src/error.rs` file-first with
    # `src/error.rs::drop` in `inferred` while ASAN was blaming that exact
    # frame for a heap-use-after-free costing 13 unmeasurable tests; the
    # re-check line told it to return the body unchanged and it did.
    crashed: tuple[str, ...] = ()
    # The subset a callee of the faulting frame, carried as evidence and
    # kept out of `crash_blamed`. It outranks `inferred` and `installable`
    # because both of those render "return it unchanged if already right",
    # and run `1911637-d0fab212` listed `object_downcast` on both lines in
    # the same prompt while `test_large_alignment` still SIGABRTed.
    aimed: tuple[str, ...] = ()
    # Free fns of the file an ESCALATED producer could INSTALL into a
    # fn-pointer table it builds (`domain.installable_fns`). Nothing measured
    # THEM, so they get neither the escalated line (a measurement about a
    # producer) nor the re-check line (which tells the model to leave a right
    # body alone): the instruction is that the table entry is written in the
    # SAME reply as the producer that names it — the two halves of one defect,
    # which across three runs were never writable in one prompt.
    installable: tuple[str, ...] = ()
    # Call sites that construct a wrapper an escalated producer erases WITHOUT
    # invoking its typed producer (`domain.installable_fns.producer_bypasses`)
    # — the closure's third population, carried as the domain objects so the
    # renderer reads `erases_generic`/`same_file`/`producer_file` from one
    # source of truth instead of a parallel tuple encoding. `erases_generic`
    # selects the INSTRUCTION: True (the fn's impl erases its own generic
    # parameter) renders the call form — invoke the producer; False renders a
    # table form, which never names the producer and splits again on
    # `same_file` (build the table here vs route to the file that declares
    # the machinery). Run `40938-f4e8e94b` measured what omitting the entries
    # costs (the model's thinking named the `Self::new` →
    # `construct_from_std` bypass in `Error::context`, wrote "Error::context
    # is not listed for rewrite", and complied); run `57025-6ba652f2`
    # measured what the WRONG form costs (told to call the producer on a
    # concrete self, the model complied over the compiler through three
    # identical `E0277`s).
    bypass: tuple[ProducerBypass, ...] = ()
    # Oracle pins extracted from the attributed tests (`pinned_observables`).
    # Empty on a prompt that has no attributed test source. Appended to the
    # ESCALATED clause with `typeid_pin_consequence`: run `2932-cf516410` had
    # the pin in the quoted test and still wrapped (instruction slot empty);
    # run `82157-476ddc16` listed the pins on this line and still wrapped
    # (a call-form list is not the TypeId statement).
    pins: str = ""
    # Whether an ESCALATED member of this block constructs a crate tuple
    # struct inside another call's argument list
    # (`domain.refill_evidence.wraps_crate_tuple_struct`) — the gate on the
    # TypeId sentence. The pins say what the TESTS observe; this says what
    # the BODY does, and the sentence is a claim about the body. The same
    # run's ensure prompt (`logs/prompts/97e9b596….txt` line 6) rendered it
    # on `impl<A, B> BothDebug for (A, B)` -> `__dispatch_ensure`, which
    # builds a `SmallBuf` and wraps nothing.
    wraps_pinned_type: bool = False
    # Identity Then from the behaviour suite (`identity_must_instruction` /
    # `_fmt_outcome`), for first-pass `write:` as well as rewrite. Empty when
    # no IDENTITY spec names this block's units. `typeid_pin_consequence`
    # above fires only on escalated + wraps, which a stub cannot satisfy —
    # there is no body yet. Run `2932-cf516410` had the pin in the quoted
    # test and still wrapped because this slot was empty on `write:`.
    identity_must: str = ""
    # Coverage sentences for this block's units (dead body / unreachable
    # lines). Empty when the sweep has no reading or none of the units
    # speak. Instruction slot, not an evidence section: a fill can carry
    # the profile and still wrap. Bound by the TDD driver for one sweep.
    coverage_must: str = ""
    # Signature text per name, so the generic construction recipe can key
    # on shape. Empty under the catalog driver (it keys on name). Frozen
    # as pairs because this dataclass is frozen.
    sigs: tuple[tuple[str, str], ...] = ()


def restrict_fill_plan(
    plans: Sequence[BlockFillPlan], names: Collection[str],
) -> list[BlockFillPlan]:
    """Drop every planned name not in ``names``; drop plans that then list none.

    File-first peel used to accept the compiling remainder and return, so the
    blamed names never saw a retry with the diagnostic that named them (run
    ``451330-7e8a7ca9``, 2026-08-31: ``src/fmt.rs::debug`` peeled, FILE FILLED
    1, attempt 1 of 3 final). The retry loop rebuilds the prompt from this
    restriction so kept bodies are not re-authorized and the peeled names
    keep the rewrite instruction.
    """
    want = frozenset(names)
    out: list[BlockFillPlan] = []
    for p in plans:
        q = replace(
            p,
            stubs=tuple(n for n in p.stubs if n in want),
            refills=tuple(n for n in p.refills if n in want),
            inferred=tuple(n for n in p.inferred if n in want),
            escalated=tuple(n for n in p.escalated if n in want),
            crashed=tuple(n for n in p.crashed if n in want),
            aimed=tuple(n for n in p.aimed if n in want),
            installable=tuple(n for n in p.installable if n in want),
            bypass=tuple(b for b in p.bypass if b.fn_name in want),
            sigs=tuple((n, s) for n, s in p.sigs if n in want),
        )
        if q.stubs or q.refills:
            out.append(q)
    return out


def _recipe_for_plan(p: BlockFillPlan) -> dict[str, str]:
    """Name → recipe for every stub/refill this plan lists.

    Computed from name + header, never from a sections field: a forgotten
    kwarg cannot omit the instruction the signature already names. Run
    ``1383168-eb151d09``: both units were stubs, ``producer_bypass_offered``
    empty, fill classes ``cashed=0 compile=3``.
    """
    out: dict[str, str] = {}
    for name in (*p.stubs, *p.refills):
        if name in out:
            continue
        from pipeline_core.domain.language_driver import language_driver
        text = language_driver().construction_recipe(
            name, p.header, dict(p.sigs).get(name, ""))
        if text:
            out[name] = text
    return out


def _plan_line(p: BlockFillPlan) -> str:
    where = f"in `{p.header.splitlines()[0]}`" if p.header else "free fns"
    parts = []
    recipes = _recipe_for_plan(p)
    recipe_names = frozenset(recipes)
    stubs_plain = tuple(s for s in p.stubs if s not in recipe_names)
    if stubs_plain:
        parts.append("write: " + ", ".join(stubs_plain))
    seen_recipe: set[str] = set()
    for name in (*p.stubs, *p.refills):
        if name in seen_recipe or name not in recipes:
            continue
        seen_recipe.add(name)
        parts.append(f"{recipes[name]}: {name}")
    bypass_names = frozenset(b.fn_name for b in p.bypass)
    # A conversion-family member AIMED beside a CRASHED family member is
    # graded CRASHED: the family is one ownership contract, and the aimed
    # sentence ("re-derive the erased layout") is the wrong instruction for
    # it — run `1577437-c93d5c26`'s iter-1 error.rs prompt sent
    # `into_boxed_dyn_error` CRASHED with the MUST and `object_boxed` AIMED,
    # and the reply forgot self in one and re-boxed the payload in the other.
    promoted = family_members_beside_a_crashed_conversion(p.crashed, p.aimed)
    crashed_names = frozenset(p.crashed) | promoted
    crashed = tuple(r for r in p.refills if r in crashed_names)
    aimed = tuple(r for r in p.refills
                  if r in p.aimed and r not in recipe_names
                  and r not in crashed_names)
    direct = tuple(r for r in p.refills
                   if r not in p.inferred and r not in p.escalated
                   and r not in p.installable and r not in bypass_names
                   and r not in recipe_names and r not in p.crashed
                   and r not in p.aimed)
    escalated = tuple(r for r in p.refills
                      if r in p.escalated and r not in p.installable
                      and r not in bypass_names and r not in recipe_names
                      and r not in p.crashed and r not in p.aimed)
    installable = tuple(r for r in p.refills
                        if r in p.installable and r not in recipe_names
                        and r not in p.crashed and r not in p.aimed)
    inferred = tuple(r for r in p.refills
                     if r in p.inferred and r not in p.escalated
                     and r not in p.installable and r not in bypass_names
                     and r not in recipe_names and r not in p.crashed
                     and r not in p.aimed)
    if crashed:
        # First in the line, and the only grade whose premise is an observed
        # fault. It states what the tree DID rather than what a verdict
        # implies, because the verdict for these units does not exist: their
        # binary aborted before reporting one.
        crash_clause = (
            "REWRITE — A TEST BINARY CRASHED INSIDE THIS BODY (a "
            "memory fault, with this fn on the faulting frame; the "
            "tests attributed to it report no verdict at all because "
            "the process aborted. It compiles and it is still wrong "
            "— look at what it does with ownership, lifetimes or raw "
            "pointers, and see CRASH_EVIDENCE): " + ", ".join(crashed))
        # Conversion family only. `drop` is a correct destructor; putting
        # this MUST on a drop rewrite re-instructs the innocent body the
        # skip exists to stop aiming at. Run `1790133-2181e509` had
        # `into_boxed_dyn_error` in CRASH_EVIDENCE and still wrote
        # `let own = self.inner` because this slot named drop.
        extra = ownership_transfer_must(crashed)
        if extra:
            crash_clause += " — " + extra
        parts.append(crash_clause)
    if aimed:
        # Consumes the name the way CRASHED does. An aimed producer used to
        # appear twice — REWRITE WITH THE PRODUCER (installable complement)
        # and re-check / leave it unchanged (NAMED) — and the model could
        # pick the one that costs nothing. One name, one instruction.
        parts.append(
            "REWRITE — A TEST BINARY CRASHED and this body is a callee of "
            "the faulting frame (evidence, never blame): the pointer the "
            "blamed frame dereferenced is produced here. Re-derive it from "
            "the types it is generic over — the erased layout must carry the "
            "payload type's alignment and field offsets. Do not return it "
            "unchanged: " + ", ".join(aimed))
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
                     "this fn constructs or returns"
                     + (f" — the attributed tests pin: {p.pins}"
                        + typeid_pin_consequence(
                            p.pins, wraps=p.wraps_pinned_type)
                        if p.pins else "")
                     + "): " + ", ".join(escalated))
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
    for wrapper, producer, producer_file, same_file, erases_generic in sorted(
            {(b.wrapper, b.producer, b.producer_file, b.same_file,
              b.erases_generic) for b in p.bypass}):
        fns = tuple(b.fn_name for b in p.bypass
                    if (b.wrapper, b.producer, b.producer_file, b.same_file,
                        b.erases_generic)
                    == (wrapper, producer, producer_file, same_file,
                        erases_generic)
                    and b.fn_name in p.refills
                    and b.fn_name not in recipe_names)
        if not fns:
            continue
        # The relation is stated, not a verdict on the body: what was measured
        # is that rewriting the producer moved no verdict WHILE this body kept
        # routing `wrapper` values around it, so the per-type table is
        # installed on nothing this body creates. The INSTRUCTION splits on
        # the erased inner type. Call form only where the fn's impl erases its
        # own generic parameter — there the producer call is the correct shape
        # and the model demonstrably executes it. Everywhere else a table
        # form, which states the relation and the goal and NEVER names the
        # producer: named in the instruction, the model complied with the call
        # over the compiler through three identical `E0277`s (run
        # `57025-6ba652f2`), and upstream's own concrete-self body builds the
        # wrapper's chain table inline rather than calling the typed producer
        # even where bounds could be arranged. The table form splits AGAIN on
        # `same_file`, because "build it in this body" is only a possible
        # instruction in the file that declares the machinery: the live
        # table type and chain fns carry no `pub` (`ErrorVTable`
        # error.rs:660, `context_chain_downcast`/`context_chain_drop_rest`
        # error.rs:789/:804), so a context.rs body told to build the table
        # cannot compile — the instructed-impossibility class again. Upstream's
        # own cross-file body is a delegation (`self.context(context)`) for
        # exactly this reason. Neither table form claims what the wrapped
        # value IS: `erases_generic=False` covers the undecidable cases too,
        # and the only claims below are the measured relation and the
        # instruction itself.
        if erases_generic:
            producer_ref = ("declared in this file" if same_file else
                            f"declared in `{producer_file}` — pub(crate), "
                            "callable from here")
            parts.append(f"REWRITE TO INVOKE THE PRODUCER (this body "
                         f"constructs `{wrapper}` and hands it to the generic "
                         f"construction path, so the per-type table its typed "
                         f"producer `{producer}` builds ({producer_ref}) is "
                         f"never installed on the values this body creates; "
                         f"rewrite it to call `{producer}` instead): "
                         + ", ".join(fns))
        elif same_file:
            parts.append(f"BUILD THE PER-TYPE TABLE (this body constructs "
                         f"`{wrapper}` and hands it to the generic "
                         f"construction path, so the per-type table its "
                         f"values need for dispatch is installed on nothing "
                         f"this body creates — the failing downcasts walk "
                         f"that table. A producer-call instruction on a "
                         f"different impl in this prompt does not apply "
                         f"here: build `{wrapper}`'s own table in this body, "
                         f"dispatching through the table fns of this file "
                         f"that fit `{wrapper}`'s types — for a wrapper whose "
                         f"inner value this crate has already erased, those "
                         f"are its CHAIN-walking fns, not the leaf ones. "
                         f"Name the fns; rewrite them only where this prompt "
                         f"lists them as writable): " + ", ".join(fns))
        else:
            parts.append(f"ROUTE TO THE PER-TYPE TABLE (this body constructs "
                         f"`{wrapper}` and hands it to the generic "
                         f"construction path, so the per-type table its "
                         f"values need for dispatch is installed on nothing "
                         f"this body creates — the failing downcasts walk "
                         f"that table. A producer-call instruction on a "
                         f"different impl in this prompt does not apply "
                         f"here, and the machinery that builds `{wrapper}`'s "
                         f"table lives in `{producer_file}`, not in this "
                         f"file: do NOT construct `{wrapper}` in this body — "
                         f"delegate this wrap to the path that file declares "
                         f"for `{wrapper}`'s inner value, so the value "
                         f"carries the table built where it is declared): "
                         + ", ".join(fns))
    if inferred:
        parts.append("re-check (a body exists; implicated by NAME only — "
                     "rewrite ONLY if it is wrong for its bounds, else leave it "
                     "unchanged): " + ", ".join(inferred))
    line = f"  {where} -> " + "; ".join(parts)
    if p.identity_must:
        line = line + " — " + p.identity_must
    if p.coverage_must:
        line = line + " — " + p.coverage_must
    return line


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
    retrospective_guidance: str = "",
    module_name: str = "",
    cluster_name: str = "",
    gotchas: str = "",
    types_section: str = "",
    invariants_section: str = "",
    refill_evidence: str = "",
    prior_regression: str = "",
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

    `prior_regression` is what the previous KEPT landing in this file turned
    red — a net-positive keep forgives a newly-red test at the gate (the
    sweep's rule), so the next prompt for these units must carry it or the
    loss is a silent leftover (`render_prior_regression`; the session and
    pair routes already carried theirs, this route and the per-impl route
    did not).

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
    retro_part = (f"\n<RETROSPECTIVE_GUIDANCE>\nThe previous attempt at a unit "
                  f"in this file did not pay. Apply this strategy differently "
                  f"this time:\n{retrospective_guidance}\n"
                  f"</RETROSPECTIVE_GUIDANCE>\n"
                  if retrospective_guidance.strip() else "")
    tests_part = (f"\nTests that exercise this file (make them pass):\n"
                  f"{tests_block}\n" if tests_block.strip() else "")
    # What the record supports for each "rewrite"/"re-check" above — DIRECT
    # conviction or inference only, the attributed tests, and what they said
    # (`domain.refill_evidence`); "" when the plan has no re-fills.
    evidence_part = (f"\n<REFILL_EVIDENCE>\nFor the bodies listed as rewrite / "
                     f"re-check above — what the failing tests actually "
                     f"establish:\n{refill_evidence}\n</REFILL_EVIDENCE>\n"
                     if refill_evidence.strip() else "")
    regression_part = (f"\n<PRIOR_REGRESSION>\n{prior_regression}\n"
                       "</PRIOR_REGRESSION>\n"
                       if prior_regression.strip() else "")
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
{gotchas_part}{invariants_part}{types_part}{crash_part}{bp_part}{notes_part}{retro_part}{evidence_part}{regression_part}{tests_part}{error_part}
For EACH function, return one block (body statements only — no fn signature
line, no outer braces). `name` is the BARE function name; the impl header goes
in `block` and NOWHERE else:
{language_driver().file_fill_tag_examples()}
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
    identity_must: str = "",
) -> str:
    """Last resort — budgets OFF. After the budgeted per-impl loop
    exhausts, the model gets ONE attempt with the WHOLE current file, every
    referencing test verbatim, and the full focused error (a defect outside
    the block's window is invisible to the budgeted prompt).

    `identity_must` is the same TypeId instruction the budgeted per-impl
    prompt carries after the stub list. Empty (the default) leaves the
    prompt byte-identical to a call that never named the kwarg.

    Construction recipes are derived from ``stub_names`` + ``container``,
    not a sections field: a last-resort prompt for ``Error::context`` or
    ``construct_from_boxed`` must carry the compile-time shape even when
    identity Then is empty.
    """
    header = container.splitlines()[0] if container else "impl"
    names = ", ".join(stub_names)
    tests_part = (f"\nTests that exercise these functions (make them pass):\n"
                  f"{tests_block}\n" if tests_block.strip() else "")
    notes_part = (f"\nNotes from earlier attempts:\n{notes}\n"
                  if notes.strip() else "")
    recipe_text = construction_recipes_for(stub_names, container)
    recipe_part = (f"{recipe_text}\n" if recipe_text else "")
    must_part = (f"{identity_must}\n" if identity_must.strip() else "")
    must_part += instruction_opener(file_rel, stub_names)
    return f"""Previous attempts to implement functions in this Rust file failed.
You now see the ENTIRE current file. Implement or CORRECT the function(s)
{names} of `{header}` so the file compiles and the tests pass — some may
already have a body that compiles but whose test still fails; rewrite its
behavior. Keep every signature EXACTLY as written; change bodies only.
File: {file_rel}
{recipe_part}{must_part}```rust
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
    identity_must: str = "",
    container: str = "",
) -> str:
    """Correct a fn that compiles but FAILS its tests (behaviour wrong).

    `identity_must` is the TypeId instruction the first-pass builders carry.
    A wrap that first-pass missed is exactly this path — the failing test
    source is already in the prompt; without this sentence the TypeId rule
    is evidence the 2026-08-26 constructor fills had and still wrapped.
    Empty (the default) leaves the prompt byte-identical to a call that
    never named the kwarg.

    ``container`` feeds the stub-time construction recipe (same default).
    """
    recipe_part = construction_recipe_prefix(fn_name, container, sig_text)
    must_part = (f"{identity_must}\n\n" if identity_must.strip() else "")
    must_part += instruction_opener(file_rel, (fn_name,))
    return (
        f"ONE Rust function compiles but FAILS its tests — "
        f"its behaviour is wrong. Correct the body.\n"
        f"{thinking_directive}"
        f"File: {file_rel} | Fn: {fn_name}\n"
        f"Signature (do NOT change it):\n{sig_text}\n\n"
        f"{recipe_part}{must_part}"
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
    container: str = "",
) -> str:
    """Fill an unimplemented stub that compiles but fails tests.

    ``container`` feeds the stub-time construction recipe. Empty (the
    default) leaves the prompt byte-identical for every unit the recipe
    does not name.
    """
    recipe_part = construction_recipe_prefix(fn_name, container, sig_text)
    return (
        f"ONE Rust function has an unimplemented body — fill it.\n"
        f"{thinking_directive}"
        f"File: {file_rel} | Fn: {fn_name}\n"
        f"Signature (do NOT change it):\n{sig_text}\n\n"
        f"{recipe_part}"
        f"Current body (unimplemented — replace it):\n{{{{\n{cur_body}\n}}}}\n\n"
        f"Test failure output:\n```\n{errs}\n```\n"
        f"Return the corrected body as EXACTLY one block "
        f"(statements only — no fn signature, no outer braces):\n"
        f'<FN name="{fn_name}" block="any">\n...body...\n</FN>\n'
        f"Output nothing else."
    )
