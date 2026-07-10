"""PRIOR_ATTEMPTS error excerpts must break at line boundaries — a rustc
diagnostic sliced mid-line ("- th...") hides the span arrows and
expected/found detail the model needs (same rule as focus_diagnostics)."""
from __future__ import annotations


def test_prior_attempt_errors_cut_at_line_boundary() -> None:
    import pipeline  # noqa: F401  — configures the prompts runtime facade
    from pipeline_core.prompts.context import PerFnFixSections
    from pipeline_core.prompts.per_fn import build_per_fn_fix_prompt

    long_diag = "\n".join(
        [f"error: unexpected closing delimiter: `}}` line {i} " + "x" * 60
         for i in range(20)])
    fix = build_per_fn_fix_prompt(
        file_text="fn parse() { unimplemented!() }", fn_name="parse",
        sig_text="fn parse()", test_blocks=[], module_name="mycrate",
        cluster_name="stable", file_rel="src/lib.rs", fr_path=None,
        prev_body="1", error_excerpt="E",
        attempt_history=[{"attempt": 1, "body": "b",
                          "error_sig": long_diag, "outcome": "compile_fail"}],
        sections=PerFnFixSections(
            macros="", notes="", container="", types="", invariants="",
            siblings="", crate_idioms="", bound_methods="",
            retrospective_guidance=""),
    )
    start = fix.index("<PRIOR_ATTEMPTS>")
    end = fix.index("</PRIOR_ATTEMPTS>")
    block = fix[start:end]
    # Truncated (ends with ellipsis) but every kept line is whole.
    assert "..." in block
    for ln in block.splitlines():
        if ln.startswith("error:"):
            assert ln.endswith("x") or ln.endswith("...")
