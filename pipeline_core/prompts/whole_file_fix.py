"""Pure prompt assembly for the legacy whole-file-rewrite fix strategy (chunk H).

`generate_fix_loop._do_fix` used to build this prompt inline. It is the
straggler of the P3.A inline-fix-prompt set (the other four live in
`fix_prompts.py`); unlike those, it also owns the char-budget math (it caps the
error/source/spec/extra-context sections to fit `single_call_char_limit`), so it
lives in its own module and depends on `domain.text.smart_truncate`.

PURE: every dynamic section (source block, repo-spec, gotchas) is rendered by
the caller and passed in as a string. Zero I/O. The output is byte-for-byte the
original inline f-string (a characterization test pins it).
"""

from __future__ import annotations

from ..domain.text import smart_truncate


def build_whole_file_fix_prompt(
    *,
    thinking_directive: str,
    file_format_instruction: str,
    verbosity_guard: str,
    single_call_char_limit: int,
    output: str,
    history: list[dict[str, object]],
    retro: str,
    source_block: str,
    extra_context: str,
    functional_req: str,
    gotchas_brief: str,
    gotchas_specific: str,
    repo_block: str,
) -> str:
    """Whole-file rewrite fix prompt + section char-budget truncation."""
    # Running narrative of prior fix attempts (from improvement-loop history).
    _attempt_lines: list[str] = []
    for rec in history:
        iter_n = rec.get("iter", "?")
        chg = rec.get("files_changed_str", "no files changed")
        err_prev = rec.get("error_preview", "")
        _attempt_lines.append(
            f"Attempt {iter_n}: changed [{chg}] → still failing.\n"
            f"Error was:\n{err_prev}"
        )
    _history_section = ""
    if _attempt_lines:
        n = len(_attempt_lines)
        _history_section = (
            f"You have already made {n} fix attempt{'s' if n != 1 else ''} in this loop. "
            f"Here is what happened — do NOT repeat the same changes:\n\n"
            + "\n\n".join(_attempt_lines)
            + "\n\nNow make a different fix that avoids every approach above.\n"
        )

    # Strategy retrospective injected when prior iter didn't improve.
    _retro_block = (
        f"\n<RETROSPECTIVE_GUIDANCE>\nThe previous attempt did not reduce "
        "the compile error count. Apply this strategy instead of repeating "
        f"the prior approach:\n{retro}\n</RETROSPECTIVE_GUIDANCE>\n"
        if retro.strip() else ""
    )

    _gotchas_block = f"\n<RUST_GOTCHAS>\n{gotchas_brief}\n</RUST_GOTCHAS>\n"
    if gotchas_specific:
        _gotchas_block += (
            f"\n<RELEVANT_GOTCHAS_DETAIL>\n{gotchas_specific}\n"
            "</RELEVANT_GOTCHAS_DETAIL>\n"
        )
    _spec_header = "\nFunctional requirements (the contract code must satisfy):\n" if functional_req else ""
    _fl_scaffold = f"""Code generation produced files that fail the check below.
{thinking_directive}Fix ONLY the files causing the failure — do NOT regenerate correct files.
{_history_section}{_spec_header}{_retro_block}{_gotchas_block}
Error output:
```

```

Current source files:


{file_format_instruction}
{verbosity_guard}"""
    _fl_overhead = len(_fl_scaffold)
    _fl_min_err  = 10_000
    _fl_min_src  = 30_000
    _fl_min_spec = 8_000 if functional_req else 0
    _extra_budget = max(0,
        single_call_char_limit - _fl_overhead - _fl_min_err - _fl_min_src - _fl_min_spec)
    _extra_ctx_capped = smart_truncate(extra_context, total_limit=_extra_budget,
                                       head=_extra_budget // 3, tail=2 * _extra_budget // 3)
    _after_extra = max(_fl_min_err + _fl_min_src + _fl_min_spec,
                       single_call_char_limit - _fl_overhead - len(_extra_ctx_capped))
    if functional_req:
        _spec_cap = _after_extra // 4
        _remaining = _after_extra - _spec_cap
        _err_cap = _remaining // 3
        _src_cap = _remaining * 2 // 3
        _spec_capped = smart_truncate(functional_req, total_limit=_spec_cap,
                                      head=_spec_cap // 3, tail=2 * _spec_cap // 3)
        _spec_block = f"\nFunctional requirements (the contract code must satisfy):\n{_spec_capped}\n"
    else:
        _err_cap = _after_extra // 3
        _src_cap = _after_extra * 2 // 3
        _spec_block = ""
    _last_output_capped = smart_truncate(output, total_limit=_err_cap,
                                         head=_err_cap // 4, tail=3 * _err_cap // 4)
    _source_block_capped = smart_truncate(source_block, total_limit=_src_cap,
                                          head=_src_cap // 3, tail=2 * _src_cap // 3)
    return f"""Code generation produced files that fail the check below.
{thinking_directive}Fix ONLY the files causing the failure — do NOT regenerate correct files.
{_history_section}{_spec_block}{repo_block}{_retro_block}{_gotchas_block}
Error output:
```
{_last_output_capped}
```

Current source files:
{_source_block_capped}
{_extra_ctx_capped}
{file_format_instruction}
{verbosity_guard}"""
