"""Characterization tests for the extracted fix-loop prompts (P3.A).

Pin the byte-output of each builder so the relocation out of
`generate_fix_loop` is provably behaviour-preserving. The literal expected
strings are the originals' rendered form.
"""

from __future__ import annotations

import dataclasses

from pipeline_core.domain.installable_fns import ProducerBypass
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


def test_maximal_and_behavior_prompts_carry_identity_must_as_instruction() -> None:
    """The two builders that handle compiles-but-wrong had no slot. Empty
    keeps them byte-identical; a MUST sits after the signature / file line,
    not in a USAGE_EXAMPLES tag."""
    from pipeline_core.prompts.fix_prompts import build_maximal_impl_prompt
    must = (
        "MUST: `is::<&str>()` / `downcast::<&str>()` on that value is true "
        "— the TypeId a user downcasts to is the payload's type, never a "
        "wrapper's"
    )
    with_must = build_maximal_impl_prompt(
        file_rel="src/error.rs", container="impl Error", stub_names=["msg"],
        file_text="fn msg(){}", tests_block="", errs="e", notes="",
        identity_must=must)
    without = build_maximal_impl_prompt(
        file_rel="src/error.rs", container="impl Error", stub_names=["msg"],
        file_text="fn msg(){}", tests_block="", errs="e", notes="")
    assert must in with_must
    file_at = with_must.index("File: src/error.rs")
    rust_at = with_must.index("```rust")
    assert must in with_must[file_at:rust_at]
    assert must not in without
    assert "<USAGE_EXAMPLES>" not in with_must

    beh_with = build_behavior_fix_prompt(
        thinking_directive="THINK\n", file_rel="src/error.rs", fn_name="msg",
        sig_text="fn msg() -> Self", cur_body="  wrap()", tests_block="t\n",
        errs="panicked", identity_must=must)
    beh_without = build_behavior_fix_prompt(
        thinking_directive="THINK\n", file_rel="src/error.rs", fn_name="msg",
        sig_text="fn msg() -> Self", cur_body="  wrap()", tests_block="t\n",
        errs="panicked")
    sig_at = beh_with.index("fn msg() -> Self")
    body_at = beh_with.index("Current body")
    assert must in beh_with[sig_at:body_at]
    assert must not in beh_without
    assert beh_without == (
        "ONE Rust function compiles but FAILS its tests — "
        "its behaviour is wrong. Correct the body.\n"
        "THINK\nFile: src/error.rs | Fn: msg\n"
        "Signature (do NOT change it):\nfn msg() -> Self\n\n"
        "Current body (wrong — replace it):\n{{\n  wrap()\n}}\n\n"
        "Failing test source (the behaviour the body must satisfy):\n"
        "```\nt\n```\n"
        "Test failure output:\n```\npanicked\n```\n"
        "Return the corrected body as EXACTLY one block "
        "(statements only — no fn signature, no outer braces):\n"
        '<FN name="msg" block="any">\n...body...\n</FN>\n'
        "Output nothing else.")


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
    for tag in ("CLUSTER_INVARIANTS", "RUST_GOTCHAS", "TYPES_IN_SCOPE",
                "RETROSPECTIVE_GUIDANCE"):
        assert tag not in bare


def test_file_fill_omits_empty_retrospective_and_carries_a_honoured_one() -> None:
    """File-first had no retro slot; a screened BothDebug record now reaches
    the same builder the live constructor call uses. Empty (the default)
    leaves the prompt byte-identical to a call that never named the kwarg."""
    plan = [BlockFillPlan("impl Error", (), ("construct_from_adhoc",))]
    without = build_file_fill_prompt(
        file_rel="src/error.rs", file_text="fn a() {}", fill_plan=plan,
        tests_block="", blueprint="", notes="")
    empty = build_file_fill_prompt(
        file_rel="src/error.rs", file_text="fn a() {}", fill_plan=plan,
        tests_block="", blueprint="", notes="",
        retrospective_guidance="  \n")
    assert without == empty
    assert "RETROSPECTIVE_GUIDANCE" not in without
    guided = build_file_fill_prompt(
        file_rel="src/error.rs", file_text="fn a() {}", fill_plan=plan,
        tests_block="", blueprint="", notes="",
        retrospective_guidance=(
            "Drop the fixed 40-byte SmallBuf and format into an allocating Write"))
    assert "<RETROSPECTIVE_GUIDANCE>" in guided
    assert "40-byte SmallBuf" in guided
    assert without != guided


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
        bypass=(ProducerBypass(
            fn_name="ext_context", container="impl<E> StdError for E",
            wrapper="ContextError", producer="construct_from_context",
            producer_file="src/error.rs", same_file=False,
            erases_generic=True),))])
    line = next(ln for ln in out.splitlines()
                if ln.startswith("  in `impl<E> StdError for E`"))
    assert "REWRITE TO INVOKE THE PRODUCER" in line
    assert "rewrite it to call `construct_from_context`" in line
    assert "declared in `src/error.rs` — pub(crate), callable from here" in line
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
        bypass=(ProducerBypass(
            fn_name="context", container="impl Error",
            wrapper="ContextError", producer="construct_from_context",
            producer_file="src/error.rs", same_file=True,
            erases_generic=False),))])
    line = next(ln for ln in out.splitlines()
                if ln.startswith("  in `impl Error`"))
    assert "BUILD THE PER-TYPE TABLE" in line
    assert "construct_from_context" not in line
    assert "REWRITE TO INVOKE" not in line
    assert "ContextError" in line
    assert "CHAIN-walking" in line
    assert "different impl" in line
    # Unconditional "candidate fns are writable in this prompt" is false
    # for context.rs (chain fns live in error.rs). Writability is only a
    # condition: name them, do not rewrite unless listed.
    assert "the candidate fns are writable in this prompt" not in line
    assert line.rstrip().endswith("context")


def test_file_fill_plan_call_and_table_forms_in_one_prompt_stay_split() -> None:
    """context.rs writes both ext_context impls in one prompt. The generic
    impl's call form names the producer; the Error-impl's table form must
    not, and must say that sibling instruction does not apply — otherwise
    the model copies the named call onto the concrete self (run
    57025-6ba652f2, the generic body correct, the Error-impl the same
    E0277, peel dropped both).

    The Error-impl entry is CROSS-FILE (the producer and the table machinery
    live in src/error.rs), so its form is ROUTE, not BUILD: `ErrorVTable`,
    `context_chain_downcast` and `context_chain_drop_rest` carry no `pub`
    (live tree error.rs:660/:789/:804), so a context.rs body instructed to
    build the table cannot compile — the instructed-impossibility class
    again. Upstream's own body here is the delegation `self.context(context)`."""
    out = _file_fill([
        BlockFillPlan(
            "impl StdError for E", (), ("ext_context",),
            bypass=(ProducerBypass(
                fn_name="ext_context", container="impl StdError for E",
                wrapper="ContextError", producer="construct_from_context",
                producer_file="src/error.rs", same_file=False,
                erases_generic=True),)),
        BlockFillPlan(
            "impl StdError for Error", (), ("ext_context",),
            bypass=(ProducerBypass(
                fn_name="ext_context", container="impl StdError for Error",
                wrapper="ContextError", producer="construct_from_context",
                producer_file="src/error.rs", same_file=False,
                erases_generic=False),)),
    ])
    call = next(ln for ln in out.splitlines()
                if ln.startswith("  in `impl StdError for E`"))
    table = next(ln for ln in out.splitlines()
                 if ln.startswith("  in `impl StdError for Error`"))
    assert "REWRITE TO INVOKE THE PRODUCER" in call
    assert "construct_from_context" in call
    assert "ROUTE TO THE PER-TYPE TABLE" in table
    assert "construct_from_context" not in table
    assert "different impl" in table
    assert "delegate" in table and "`src/error.rs`" in table
    # The cross-file table form must NOT instruct building the table here.
    assert "do NOT construct `ContextError` in this body" in table
    assert "BUILD THE PER-TYPE TABLE" not in table


def test_file_fill_plan_bypass_outranks_the_escalated_grade() -> None:
    """A fn both ESCALATED and a bypass gets the bypass line: the wrapper
    relation is the half of the instruction the plain REWRITE grade lacks."""
    out = _file_fill([BlockFillPlan(
        "impl Error", (), ("context", "construct_from_std"),
        escalated=("context", "construct_from_std"),
        bypass=(ProducerBypass(
            fn_name="context", container="impl Error",
            wrapper="ContextError", producer="construct_from_context",
            producer_file="src/error.rs", same_file=True,
            erases_generic=False),))])
    line = next(ln for ln in out.splitlines()
                if ln.startswith("  in `impl Error`"))
    assert "BUILD THE PER-TYPE TABLE" in line
    # the other escalated member keeps the measured-grade line
    assert "REWRITE (the fns that consume" in line
    assert line.split("REWRITE (the fns that consume")[1].startswith(
        " what this one produces")


def test_file_fill_plan_line_carries_the_oracle_pins_on_escalated() -> None:
    """Run `2932-cf516410`: the constructor prompt's `_plan_line` stated the
    measurement and never the pin. The attributed tests already named
    `downcast::<&str>`; putting that on the instruction is the slot the
    live fill actually read."""
    pins = 'downcast::<&str>, is::<&str>, assert_eq!(EXPECTED_ALTDEBUG_F, format!("{:#?}", …))'
    out = _file_fill([BlockFillPlan(
        "impl Error", (), ("construct_from_adhoc",),
        escalated=("construct_from_adhoc",), pins=pins)])
    line = next(ln for ln in out.splitlines()
                if ln.startswith("  in `impl Error`"))
    assert "the attributed tests pin: downcast::<&str>" in line
    assert "is::<&str>" in line
    assert "EXPECTED_ALTDEBUG_F" in line
    # NEGATIVE CONTROL: today's `_plan_line` (no pins field) contains none.
    bare = _file_fill([BlockFillPlan(
        "impl Error", (), ("construct_from_adhoc",),
        escalated=("construct_from_adhoc",))])
    bare_line = next(ln for ln in bare.splitlines()
                     if ln.startswith("  in `impl Error`"))
    assert "the attributed tests pin:" not in bare_line
    assert "downcast::<&str>" not in bare_line
    assert "EXPECTED_ALTDEBUG_F" not in bare_line


# Recorded constructor `_plan_line` from run `82157-476ddc16`
# (`logs/prompts/49385be….txt` line 7). Pins listed; TypeId consequence
# absent. The FILE FILLED 21 reply left `construct_from_adhoc` wrapping
# before the generic constructor.
_LIVE_82157_PINS = (
    "downcast::<Box<dyn StdError + Send + Sync>>, downcast::<&str>, "
    "downcast::<String>, downcast::<io::Error>, is::<&str>, "
    "is::<<str as ToOwned>::Owned>, "
    'assert_eq!(EXPECTED_ALTDEBUG_F, format!("{:#?}", …)), '
    'assert_eq!(EXPECTED_ALTDEBUG_G, format!("{:#?}", …)), '
    'assert_eq!(EXPECTED_ALTDEBUG_H, format!("{:#?}", …))'
)
_LIVE_82157_CONSTRUCTORS = (
    "new", "msg", "construct_from_std", "construct_from_adhoc",
    "construct_from_display", "construct_from_context",
    "construct_from_boxed", "construct", "context",
)
_LIVE_82157_PLAN_LINE = (
    "  in `impl Error` -> REWRITE (the fns that consume what this one "
    "produces were rewritten over repeated sweeps and no test changed "
    "its verdict — the defect is not in them, it is in what this fn "
    "constructs or returns — the attributed tests pin: "
    + _LIVE_82157_PINS
    + "): " + ", ".join(_LIVE_82157_CONSTRUCTORS)
)


def test_plan_line_states_typeid_consequence_of_downcast_pins() -> None:
    """Run `82157-476ddc16`: the constructor `_plan_line` listed
    `downcast::<&str>` and the reply still wrapped. The claim is that the
    same pins, on a block whose escalated body WRAPS (`construct_from_adhoc`
    builds `MessageError(message)` inside `construct_from_std(…)`), now
    produce a line that states wrapping the pinned type before the generic
    constructor stores a different TypeId."""
    out = _file_fill([BlockFillPlan(
        "impl Error", (), _LIVE_82157_CONSTRUCTORS,
        escalated=_LIVE_82157_CONSTRUCTORS, pins=_LIVE_82157_PINS,
        wraps_pinned_type=True)])
    line = next(ln for ln in out.splitlines()
                if ln.startswith("  in `impl Error`"))
    assert "the attributed tests pin:" in line
    assert "downcast::<&str>" in line
    assert "stores a different TypeId" in line
    assert "downcast::<T>()" in line
    # NEGATIVE CONTROL: the recorded prompt had the pin list and not the
    # TypeId sentence. That is the pre-fix instruction the fill actually
    # read.
    assert "the attributed tests pin:" in _LIVE_82157_PLAN_LINE
    assert "downcast::<&str>" in _LIVE_82157_PLAN_LINE
    assert "stores a different TypeId" not in _LIVE_82157_PLAN_LINE
    # ALTDEBUG-only pins must not grow a TypeId sentence, wrap or no wrap.
    alt = _file_fill([BlockFillPlan(
        "impl Error", (), ("debug",),
        escalated=("debug",),
        pins='assert_eq!(EXPECTED_ALTDEBUG_F, format!("{:#?}", …))',
        wraps_pinned_type=True)])
    alt_line = next(ln for ln in alt.splitlines()
                    if ln.startswith("  in `impl Error`"))
    assert "stores a different TypeId" not in alt_line
    assert "EXPECTED_ALTDEBUG_F" in alt_line


# Recorded `_plan_line` from the SAME run's ensure prompt
# (`logs/prompts/97e9b596….txt` line 6, 2026-08-26): the file-wide pin list
# — `test_downcast`'s pins — on `impl<A, B> BothDebug for (A, B)` ->
# `__dispatch_ensure`, a body that builds a local `SmallBuf` and wraps
# nothing. Pins listed, no TypeId sentence: the pre-fix line, verbatim.
_LIVE_97E9_PINS = (
    "downcast::<Box<dyn StdError + Send + Sync>>, downcast::<&str>, "
    "downcast::<String>, downcast::<io::Error>, is::<&str>, "
    "is::<<str as ToOwned>::Owned>"
)
_LIVE_97E9_PLAN_LINE = '  in `impl<A, B> BothDebug for (A, B)` -> REWRITE (the fns that consume what this one produces were rewritten over repeated sweeps and no test changed its verdict — the defect is not in them, it is in what this fn constructs or returns — the attributed tests pin: downcast::<Box<dyn StdError + Send + Sync>>, downcast::<&str>, downcast::<String>, downcast::<io::Error>, is::<&str>, is::<<str as ToOwned>::Owned>): __dispatch_ensure'


def test_typeid_sentence_is_withheld_from_a_block_whose_body_wraps_nothing() -> None:
    """The gate is the BODY, not the pins. The recorded BothDebug plan
    (downcast pins present, body constructs no crate type) must not grow
    the sentence; the same plan with `wraps_pinned_type` flipped grows it
    — that flip is the control showing the flag, and only the flag, is
    what withholds it."""
    plan = BlockFillPlan(
        "impl<A, B> BothDebug for (A, B)", (), ("__dispatch_ensure",),
        escalated=("__dispatch_ensure",), pins=_LIVE_97E9_PINS)
    assert plan.wraps_pinned_type is False
    out = _file_fill([plan])
    line = next(ln for ln in out.splitlines()
                if ln.startswith("  in `impl<A, B> BothDebug"))
    # Byte-identical to the recorded line: the pins are still stated, the
    # sentence the run rendered here is gone, and nothing else moved.
    assert line == _LIVE_97E9_PLAN_LINE
    assert "downcast::<&str>" in line
    assert "stores a different TypeId" not in line
    # CONTROL: flip the flag on the identical plan -> the sentence renders.
    flipped = _file_fill([dataclasses.replace(plan, wraps_pinned_type=True)])
    flipped_line = next(ln for ln in flipped.splitlines()
                        if ln.startswith("  in `impl<A, B> BothDebug"))
    assert "stores a different TypeId" in flipped_line
    assert flipped_line.startswith(_LIVE_97E9_PLAN_LINE.split("): __dispatch")[0])


def test_file_fill_first_pass_write_carries_identity_must() -> None:
    """Run `2932-cf516410`: first-pass `write:` had the pin in the quoted
    test and no TypeId sentence on the plan line (`typeid_pin_consequence`
    fires only on escalated + wraps). The identity Then belongs on `write:`
    before any body exists."""
    must = (
        "MUST: `is::<&str>()` / `downcast::<&str>()` on that value is true "
        "— the TypeId a user downcasts to is the payload's type, never a "
        "wrapper's"
    )
    out = _file_fill([BlockFillPlan(
        "impl Error", ("construct_from_adhoc", "msg"), (),
        identity_must=must)])
    line = next(ln for ln in out.splitlines()
                if ln.startswith("  in `impl Error`"))
    assert line.startswith(
        "  in `impl Error` -> write: construct_from_adhoc, msg")
    assert "never a wrapper's" in line
    assert "`is::<&str>()`" in line
    # NEGATIVE CONTROL: today's `_plan_line` (no identity_must field) is a
    # write: list and nothing else — the instruction the first-pass fill
    # actually read.
    bare = _file_fill([BlockFillPlan(
        "impl Error", ("construct_from_adhoc", "msg"), ())])
    bare_line = next(ln for ln in bare.splitlines()
                     if ln.startswith("  in `impl Error`"))
    assert bare_line == "  in `impl Error` -> write: construct_from_adhoc, msg"
    assert "never a wrapper" not in bare_line
    assert "MUST:" not in bare_line
