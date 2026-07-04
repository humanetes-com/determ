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


def build_file_fill_prompt(
    *,
    file_rel: str,
    file_text: str,
    stub_map: list[tuple[str, list[str]]],
    tests_block: str,
    blueprint: str,
    notes: str,
) -> str:
    """P5 file-first fill: ALL stubs of one file in ONE prompt, so the model
    designs the file's impls together (the coherence a fresh reply naturally
    has) instead of inventing each block locally. `stub_map` is
    [(container_header, [stub fn names])]."""
    plan_lines = "\n".join(
        f"  {header.splitlines()[0] if header else 'free fns'}: "
        + ", ".join(names)
        for header, names in stub_map)
    bp_part = (f"\n<DESIGN_BLUEPRINT>\nYOUR crate design — every body must "
               f"agree with it:\n{blueprint}\n</DESIGN_BLUEPRINT>\n"
               if blueprint.strip() else "")
    notes_part = (f"\n<MODEL_NOTES>\n{notes}\n</MODEL_NOTES>\n"
                  if notes.strip() else "")
    tests_part = (f"\nTests that exercise this file (make them pass):\n"
                  f"{tests_block}\n" if tests_block.strip() else "")
    return f"""Implement EVERY unimplemented function of this Rust file in one pass —
design the impl blocks together so they stay mutually consistent. Keep every
signature EXACTLY as written; write bodies only.
File: {file_rel}
Stubs to fill (by impl block):
{plan_lines}
```rust
{file_text}
```
{bp_part}{notes_part}{tests_part}
For EACH function, return one block (body statements only — no fn signature
line, no outer braces). `block` must name the function's OWN impl header:
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
You now see the ENTIRE current file. Implement the still-unimplemented
function(s) {names} of `{header}` so the file compiles and the tests pass.
Keep every signature EXACTLY as written; change bodies only.
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
