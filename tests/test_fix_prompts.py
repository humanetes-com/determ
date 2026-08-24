"""Characterization tests for the extracted fix-loop prompts (P3.A).

Pin the byte-output of each builder so the relocation out of
`generate_fix_loop` is provably behaviour-preserving. The literal expected
strings are the originals' rendered form.
"""

from __future__ import annotations

from pipeline_core.prompts.fix_prompts import (
    BlockFillPlan,
    build_behavior_fix_prompt,
    build_file_fill_prompt,
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


def _file_fill(plan: list[BlockFillPlan]) -> str:
    return build_file_fill_prompt(
        file_rel="src/error.rs", file_text="fn a() {}", fill_plan=plan,
        tests_block="", blueprint="", notes="")


def test_file_fill_carries_the_crate_level_context_the_per_impl_prompt_has() -> None:
    """The file-first prompt never told the model which cluster it was filling.

    Measured over `logs/prompts` 2026-08-04: of 956 file-first prompts, 0 named
    the cluster or carried `CLUSTER_INVARIANTS` / `RUST_GOTCHAS` /
    `TYPES_IN_SCOPE`, while 2 503 other prompts carried the invariants and 528
    named the compiler cluster. The two fill routes' context sections were
    disjoint, and 318 of the 326 file-first fills in the log are `nightly` — so
    the route with less context was doing the harder work.

    The invariants are the load-bearing part: `src/error.rs` is shown to the
    model with 25 `#[cfg(error_generic_member_access)]` attributes and nothing
    anywhere saying whether that cfg is on, so which arm of
    `backtrace_if_absent!` is live had to be guessed."""
    p = build_file_fill_prompt(
        file_rel="src/error.rs", file_text="fn a() {}",
        fill_plan=[BlockFillPlan("impl Error", (), ("chain",))],
        tests_block="", blueprint="", notes="",
        module_name="anyhow", cluster_name="nightly",
        gotchas="- E0599 → read TYPES_IN_SCOPE",
        types_section="struct ErrorImpl { _error: E }",
        invariants_section="backtrace_if_absent!(...) [cfg=error_generic_member_access]")
    assert "Crate: anyhow | Cluster: nightly | File: src/error.rs" in p
    assert "<CLUSTER_INVARIANTS>" in p and "error_generic_member_access" in p
    assert "<RUST_GOTCHAS>" in p and "<TYPES_IN_SCOPE>" in p
    # Omitted sections leave no empty tags, and the header degrades to the
    # bare `File:` a caller that names no cluster used to get.
    bare = _file_fill([BlockFillPlan("impl Error", (), ("chain",))])
    assert bare.splitlines()[3] == "File: src/error.rs"
    for tag in ("CLUSTER_INVARIANTS", "RUST_GOTCHAS", "TYPES_IN_SCOPE"):
        assert tag not in bare


def test_file_fill_plan_names_the_refill_members() -> None:
    # Live loss (run 990528): the plan rendered `stub_members` only, so a block
    # in the fill set purely to CORRECT a wrong-but-compiling body arrived as a
    # bare header with no fn names — 5 of 6 plan lines were empty.
    out = _file_fill([BlockFillPlan("impl ErrorImpl", (), ("provide",))])
    assert "in `impl ErrorImpl` -> rewrite" in out
    assert "provide" in out
    # a block needing both gets both instructions, and they stay distinguished.
    both = _file_fill([BlockFillPlan("impl Error", ("msg",), ("chain",))])
    line = next(ln for ln in both.splitlines() if ln.startswith("  in `impl Error`"))
    assert line.index("write: msg") < line.index("rewrite")
    assert "chain" in line.split("rewrite")[1]


def test_file_fill_plan_never_renders_a_header_with_no_functions() -> None:
    # The prompt asks for work per block; a line naming a block but no fn is an
    # instruction to fill nothing, and the model answers for whatever it likes.
    for plan in ([BlockFillPlan("impl Error", ("msg",))],
                 [BlockFillPlan("impl Error", (), ("chain",))],
                 [BlockFillPlan("", ("free_one",))]):
        for line in _file_fill(plan).splitlines():
            if line.startswith("  in `") or line.startswith("  free fns"):
                assert line.rstrip().endswith(("msg", "chain", "free_one")), line


def test_file_fill_plan_line_avoids_the_name_colon_shape() -> None:
    # `<header>: fn` invited name="Error: root_cause", which splices nowhere.
    out = _file_fill([BlockFillPlan("impl Error", ("root_cause",))])
    assert "impl Error: root_cause" not in out
    assert "in `impl Error` -> write: root_cause" in out
    assert 'RIGHT: <FN name="root_cause" block="impl Error">' in out


def test_file_fill_states_inference_as_re_check_and_carries_the_evidence() -> None:
    """A re-fill the frontier named by inference only is listed as
    "re-check … else leave it unchanged", never as "rewrite (… its test still
    fails)"; the evidence section carries what the record supports. Told
    with certainty, the file route replaced `construct_from_context`'s
    per-type vtable with a `construct_from_std` delegation that made the
    downcast tests unreachable (run `902902-daee577d`, 2026-08-20)."""
    p = build_file_fill_prompt(
        file_rel="src/error.rs", file_text="fn a() {}",
        fill_plan=[BlockFillPlan(
            "impl Error", (), ("construct_from_adhoc", "construct_from_context"),
            inferred=("construct_from_context",))],
        tests_block="", blueprint="", notes="",
        refill_evidence="`construct_from_context` — INFERRED: …")
    plan = next(ln for ln in p.splitlines() if ln.strip().startswith("in `impl Error`"))
    assert "rewrite (a body exists, but its test still fails): construct_from_adhoc" in plan
    assert "re-check (a body exists; implicated by NAME only" in plan
    assert "construct_from_context" in plan.split("re-check", 1)[1]
    assert "construct_from_context" not in plan.split("re-check", 1)[0]
    assert "<REFILL_EVIDENCE>" in p and "INFERRED" in p
    # Without evidence: no empty tag, and the legacy wording stands.
    bare = _file_fill([BlockFillPlan("impl Error", (), ("chain",))])
    assert "<REFILL_EVIDENCE>" not in bare
    assert "rewrite (a body exists, but its test still fails): chain" in bare


def test_file_fill_plan_renders_the_call_form_only_for_a_generic_self() -> None:
    """The bypass instruction's call form — invoke the typed producer, by
    name — is rendered ONLY where the fn's impl erases its own generic
    parameter (`erases_generic=True`, the live `impl<E> StdError for E`
    `ext_context`): there the producer call is the correct shape, and reply
    sha `96b99782…` executed it verbatim. The entry still lands under its own
    impl header — the information the FILE FILLED 20 reply lacked when its
    thinking wrote "Error::context is not listed for rewrite" and complied
    (run `40938-f4e8e94b`)."""
    out = _file_fill([BlockFillPlan(
        "impl<E> StdError for E", (), ("ext_context",),
        bypass=(("ext_context", "ContextError", "construct_from_context",
                 "declared in `src/error.rs` — pub(crate), callable from here",
                 True),))])
    line = next(ln for ln in out.splitlines()
                if ln.startswith("  in `impl<E> StdError for E`"))
    assert "REWRITE TO INVOKE THE PRODUCER" in line
    assert "rewrite it to call `construct_from_context`" in line
    assert "ContextError" in line
    assert line.rstrip().endswith("ext_context")
    # NEGATIVE CONTROL: without the bypass entry the same refill renders as
    # the plain rewrite line — the pre-fix shape the thinking complied with.
    bare = _file_fill([BlockFillPlan("impl<E> StdError for E", (),
                                     ("ext_context",))])
    bare_line = next(ln for ln in bare.splitlines()
                     if ln.startswith("  in `impl<E> StdError for E`"))
    assert "PRODUCER" not in bare_line


def test_file_fill_plan_concrete_self_gets_the_table_form_and_no_producer_name() -> None:
    """A concrete-self bypass (`Error::context` — `erases_generic=False`)
    renders the TABLE form: the measured relation and the goal, and the
    producer's name NOWHERE in it. The call form for exactly this entry is
    what the model executed over the compiler three times (run
    `57025-6ba652f2`, identical `E0277` at 17:45/17:55/18:05 UTC — the
    recorded pre-fix control: prompt sha `09a55899…` carries "rewrite it to
    call `construct_from_context`" for `context`); and upstream's own body
    here builds the wrapper's chain vtable inline rather than calling the
    typed producer, so the call would be wrong even where bounds could be
    arranged."""
    out = _file_fill([BlockFillPlan(
        "impl Error", (), ("context",),
        bypass=(("context", "ContextError", "construct_from_context",
                 "declared in this file", False),))])
    line = next(ln for ln in out.splitlines()
                if ln.startswith("  in `impl Error`"))
    assert "BUILD THE PER-TYPE TABLE" in line
    assert "construct_from_context" not in line
    assert "REWRITE TO INVOKE" not in line
    assert "ContextError" in line and "writable in this prompt" in line
    assert line.rstrip().endswith("context")


def test_file_fill_plan_bypass_outranks_the_escalated_grade() -> None:
    """A fn both ESCALATED and a bypass gets the bypass line: the wrapper
    relation is the half of the instruction the plain REWRITE grade lacks."""
    out = _file_fill([BlockFillPlan(
        "impl Error", (), ("context", "construct_from_std"),
        escalated=("context", "construct_from_std"),
        bypass=(("context", "ContextError", "construct_from_context",
                 "declared in this file", False),))])
    line = next(ln for ln in out.splitlines()
                if ln.startswith("  in `impl Error`"))
    assert "BUILD THE PER-TYPE TABLE" in line
    # the other escalated member keeps the measured-grade line
    assert "REWRITE (the fns that consume" in line
    assert line.split("REWRITE (the fns that consume")[1].startswith(
        " what this one produces")
