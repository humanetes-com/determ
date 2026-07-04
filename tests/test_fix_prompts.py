"""Characterization tests for the extracted fix-loop prompts (P3.A).

Pin the byte-output of each builder so the relocation out of
`generate_fix_loop` is provably behaviour-preserving. The literal expected
strings are the originals' rendered form.
"""

from __future__ import annotations

from pipeline_core.prompts.fix_prompts import (
    build_behavior_fix_prompt,
    build_fn_fix_prompt,
    build_impl_fix_prompt,
    build_tdd_stub_fill_prompt,
)


def test_fn_fix_prompt_exact() -> None:
    got = build_fn_fix_prompt(
        thinking_directive="THINK\n", file_rel="src/a.rs", fn_name="foo",
        sig_rule="Signature (do NOT change it):\n", sig_text="fn foo() -> i32",
        cur_body="    0", errs="E0277", repo_block="<REPO/>",
        retro_block="<RETRO/>", gotchas_block="<G/>", tests_block="<T/>")
    assert got == (
        "ONE Rust function does not compile. Return ONLY its corrected body.\n"
        "THINK\nFile: src/a.rs | Fn: foo\n"
        "Signature (do NOT change it):\nfn foo() -> i32\n\n"
        "Current body (it does NOT compile):\n{\n    0\n}\n\n"
        "Compiler errors:\n```\nE0277\n```\n"
        "<REPO/><RETRO/><G/><T/>\n"
        "Return the corrected body as EXACTLY one block (statements only — no fn\n"
        "signature, no outer braces):\n"
        '<FN name="foo" block="any">\n...corrected body...\n</FN>\n'
        "Output nothing else."
    )


def test_impl_fix_prompt_exact() -> None:
    got = build_impl_fix_prompt(
        thinking_directive="THINK\n", file_rel="src/a.rs",
        where_rule="Do NOT change any signature.\n", block_src="impl A {}",
        sib_block="\nsibs\n", errs="E0053", repo_block="<REPO/>",
        retro_block="<RETRO/>", gotchas_block="<G/>", tests_block="<T/>")
    assert got == (
        "A Rust impl block fails to compile. Fix the function BODIES so the\n"
        "whole block compiles. Change ONLY what the errors require; leave already-correct\n"
        "functions untouched. Do NOT change any signature.\n\n"
        "THINK\nFile: src/a.rs\n"
        "Impl block:\n```rust\nimpl A {}\n```\n"
        "\nsibs\n\n"
        "Compiler errors:\n```\nE0053\n```\n"
        "<REPO/><RETRO/><G/><T/>\n"
        "For EACH function you change, return one block (body statements only — no fn\n"
        "signature line, no outer braces). Use the function name verbatim:\n"
        '<FN name="FUNCTION_NAME" block="impl">\n...corrected body...\n</FN>\n'
        "Output nothing else."
    )


def test_behavior_fix_prompt_exact() -> None:
    got = build_behavior_fix_prompt(
        thinking_directive="THINK\n", file_rel="src/a.rs", fn_name="foo",
        sig_text="fn foo()", cur_body="  bad()", tests_block="assert_eq!(1,2);\n",
        errs="panicked")
    assert got == (
        "ONE Rust function compiles but FAILS its tests — its behaviour is wrong. "
        "Correct the body.\n"
        "THINK\nFile: src/a.rs | Fn: foo\n"
        "Signature (do NOT change it):\nfn foo()\n\n"
        "Current body (wrong — replace it):\n{{\n  bad()\n}}\n\n"
        "Failing test source (the behaviour the body must satisfy):\n"
        "```\nassert_eq!(1,2);\n```\n"
        "Test failure output:\n```\npanicked\n```\n"
        "Return the corrected body as EXACTLY one block "
        "(statements only — no fn signature, no outer braces):\n"
        '<FN name="foo" block="any">\n...body...\n</FN>\n'
        "Output nothing else."
    )


def test_tdd_stub_fill_prompt_exact() -> None:
    got = build_tdd_stub_fill_prompt(
        thinking_directive="THINK\n", file_rel="src/a.rs", fn_name="foo",
        sig_text="fn foo()", cur_body="  unimplemented!()", errs="panicked")
    assert got == (
        "ONE Rust function has an unimplemented body — fill it.\n"
        "THINK\nFile: src/a.rs | Fn: foo\n"
        "Signature (do NOT change it):\nfn foo()\n\n"
        "Current body (unimplemented — replace it):\n{{\n  unimplemented!()\n}}\n\n"
        "Test failure output:\n```\npanicked\n```\n"
        "Return the corrected body as EXACTLY one block "
        "(statements only — no fn signature, no outer braces):\n"
        '<FN name="foo" block="any">\n...body...\n</FN>\n'
        "Output nothing else."
    )


def test_build_maximal_impl_prompt_carries_everything() -> None:
    from pipeline_core.prompts.fix_prompts import build_maximal_impl_prompt
    p = build_maximal_impl_prompt(
        file_rel="src/ensure.rs",
        container="impl Buf\nwhere\n    T: ?Sized,",
        stub_names=["write_str", "as_str"],
        file_text="struct Buf; /*whole file*/",
        tests_block="#[test] fn t(){}",
        errs="error[E0133]: unsafe",
        notes="I tried X; wrong.")
    assert "/*whole file*/" in p and "#[test] fn t(){}" in p
    assert "error[E0133]" in p and "I tried X" in p
    assert "write_str, as_str" in p
    assert '<FN name="FUNCTION_NAME" block="impl Buf">' in p


def test_build_maximal_impl_prompt_omits_empty_sections() -> None:
    from pipeline_core.prompts.fix_prompts import build_maximal_impl_prompt
    p = build_maximal_impl_prompt(
        file_rel="a.rs", container="impl A", stub_names=["f"],
        file_text="x", tests_block="", errs="e", notes=" ")
    assert "Tests that exercise" not in p and "Notes from earlier" not in p
