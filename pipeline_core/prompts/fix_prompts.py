"""Pure prompt assembly for the shared compile/test fix loop (P3.A).

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


def _plan_line(p: BlockFillPlan) -> str:
    where = f"in `{p.header.splitlines()[0]}`" if p.header else "free fns"
    parts = []
    if p.stubs:
        parts.append("write: " + ", ".join(p.stubs))
    if p.refills:
        parts.append("rewrite (a body exists, but its test still fails): "
                     + ", ".join(p.refills))
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
) -> str:
    """P5 file-first fill: everything one file needs in ONE prompt, so the
    model designs the file's impls together (the coherence a fresh reply
    naturally has) instead of inventing each block locally. `prior_error`, when
    given, is the previous file-first attempt's compile/test failure — feeding
    it back lets the retry fix the SAME joint design instead of abandoning
    coherence after one miss (a bare one-shot-or-fragment gate throws a
    genuinely co-designed impl cluster to independent per-block fills too
    early)."""
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
    return f"""Write EVERY function listed below for this Rust file in one pass —
design the impl blocks together so they stay mutually consistent. Keep every
signature EXACTLY as written; write bodies only.
File: {file_rel}
Functions to write (by impl block):
{plan_lines}
```rust
{file_text}
```
{crash_part}{bp_part}{notes_part}{tests_part}{error_part}
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
    """P6 last resort — budgets OFF. After the budgeted per-impl loop
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
