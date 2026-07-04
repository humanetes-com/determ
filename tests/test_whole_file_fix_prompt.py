"""Characterization test for the extracted whole-file-rewrite fix prompt (chunk H).

Pins the byte-output of `build_whole_file_fix_prompt` so the relocation out of
`generate_fix_loop._do_fix` is provably behaviour-preserving, including the
char-budget truncation math.
"""

from __future__ import annotations

from pipeline_core.prompts.whole_file_fix import build_whole_file_fix_prompt


def _build(**over: object) -> str:
    kw: dict = {
        "thinking_directive": "THINK\n",
        "file_format_instruction": "<FORMAT>",
        "verbosity_guard": "<GUARD>",
        "single_call_char_limit": 1_000_000,
        "output": "error[E0277]: boom",
        "history": [],
        "retro": "",
        "source_block": '\n<FILE path="src/a.rs">\nfn a() {}\n</FILE>\n',
        "extra_context": "",
        "functional_req": "",
        "gotchas_brief": "brief gotchas",
        "gotchas_specific": "",
        "repo_block": "<REPO/>",
    }
    kw.update(over)
    return build_whole_file_fix_prompt(**kw)  # type: ignore[arg-type]


def test_no_functional_req_no_history() -> None:
    got = _build()
    assert got == (
        "Code generation produced files that fail the check below.\n"
        "THINK\nFix ONLY the files causing the failure — do NOT regenerate correct files.\n"
        "<REPO/>\n<RUST_GOTCHAS>\nbrief gotchas\n</RUST_GOTCHAS>\n\n"
        "Error output:\n```\nerror[E0277]: boom\n```\n\n"
        "Current source files:\n"
        '\n<FILE path="src/a.rs">\nfn a() {}\n</FILE>\n\n'
        "\n"
        "<FORMAT>\n<GUARD>"
    )


def test_history_and_retro_and_specific_gotchas() -> None:
    got = _build(
        history=[{"iter": 1, "files_changed_str": "src/a.rs", "error_preview": "E0277"}],
        retro="try the other approach",
        gotchas_specific="detail here",
    )
    assert "You have already made 1 fix attempt in this loop." in got
    assert "Attempt 1: changed [src/a.rs] → still failing.\nError was:\nE0277" in got
    assert "<RETROSPECTIVE_GUIDANCE>" in got
    assert "try the other approach" in got
    assert "<RELEVANT_GOTCHAS_DETAIL>\ndetail here\n</RELEVANT_GOTCHAS_DETAIL>" in got


def test_functional_req_renders_spec_block() -> None:
    got = _build(functional_req="the contract")
    assert "Functional requirements (the contract code must satisfy):\nthe contract\n" in got


def test_budget_truncates_oversized_sections() -> None:
    big = "x" * 500_000
    got = _build(output=big, single_call_char_limit=60_000)
    assert len(got) < 80_000
    assert "chars omitted ...]" in got


def test_gotcha_unsafe_block_triggers_on_e0133() -> None:
    from pipeline_core.prompts.sections import gotchas_for_error
    out = gotchas_for_error(
        "error[E0133]: call to unsafe function `MaybeUninit::<T>::assume_init`"
        " is unsafe and requires unsafe block")
    assert "unsafe { self.slot.assume_init() }" in out


def test_gotcha_no_core_format_triggers_on_e0433() -> None:
    from pipeline_core.prompts.sections import gotchas_for_error
    out = gotchas_for_error("error[E0433]: cannot find `format` in `core`")
    assert "core::format!" in out and "core::fmt::Write" in out
