"""TASK 4 — failure analysis + genesis.md update (LLM use case).

Extracted from pipeline.py (slice 5b). Body verbatim; infra (log/emit/retry), the pure
budget/parse helpers (domain.text / domain.test_results) and genesis extraction
(domain.llm_output) are imported; the pipeline-bound collaborators (ask, read/write,
the rule-store writer, model ids, char budgets, verbosity guard) are injected via
:class:`Task4Deps`. No import of pipeline.py.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...domain.llm_output import (
    GENESIS_DELIM_END,
    GENESIS_DELIM_START,
    _extract_genesis,
)
from ...domain.test_results import count_passing_tests, extract_test_output_from_file
from ...domain.text import smart_truncate
from ...log import emit, log
from ...retry import with_retry


@dataclass(frozen=True, slots=True)
class Task4Deps:
    """Pipeline-bound collaborators TASK 4 needs, injected by the composition root."""

    ask: Callable[..., str]
    read: Callable[[Any], str]
    write: Callable[[Any, str], None]
    extract_and_store_rules: Callable[..., Any]   # _task4_extract_and_store_rules
    model: str                                    # global MODEL (for the warning message)
    task4_model: str                              # _TASK4_MODEL
    verbosity_guard: str                          # _TASK4_VERBOSITY_GUARD
    intermediate_char_limit: int
    single_call_char_limit: int


def task4(genesis_path, functional_req_path, test_output, iteration, output_dir,
          deps: Task4Deps,
          failure_type: str = "test", failure_log: str = "",
          prev_pass_count: int = 0, prev_test_output: str = "",
          source_context: str = "", api_surface: str = ""):
    """
    Analyze failures and update genesis.md.

    failure_type: "build" (code didn't compile) or "test" (tests failed at runtime).
      - build failures: the model is asked to identify what STRUCTURAL information the
        requirements missed (workspace layout, entry-point files, module declarations).
        genesis.md is updated to require capturing that structural class of information.
      - test failures: the model performs a full reasoning-gap analysis and updates
        genesis.md with general lessons about what categories of test signal were missed.

    failure_log: running record of previous iteration outcomes. Fed in so the model
      can avoid repeating approaches that already failed.

    prev_pass_count / prev_test_output: when provided by the caller (per-module mode),
      used directly for regression comparison instead of reading tests_output_{N-1}.txt.
    """
    # BL-019: map sub-categories back to the two TASK 4 paths.
    _is_build = (failure_type == "build")
    log(f"\nTASK 4: Analyzing failure [{failure_type}], updating genesis.md")
    _t4_t0 = time.monotonic()
    emit("task.start", task="task4",
         attempt=iteration if isinstance(iteration, int) else None,
         data={"failure_type": failure_type, "iteration": iteration,
               "prev_pass_count": prev_pass_count})

    genesis = deps.read(genesis_path)
    functional_req = deps.read(functional_req_path)
    # Fill-down: genesis and functional_req are typically small (10-20k each).
    # Include them in full (up to hard safety caps), then give ALL remaining budget
    # to test_output — the actual failure signal the model must reason about.
    _t4_genesis_cap = deps.intermediate_char_limit
    _t4_req_cap     = deps.single_call_char_limit // 3
    # genesis: head-weighted (methodology intro at top). req: head-weighted (core types first).
    genesis        = smart_truncate(genesis, total_limit=_t4_genesis_cap,
                                    head=2 * _t4_genesis_cap // 3, tail=_t4_genesis_cap // 3)
    functional_req = smart_truncate(functional_req, total_limit=_t4_req_cap,
                                    head=_t4_req_cap // 3, tail=2 * _t4_req_cap // 3)
    # test_output gets everything left after genesis + req + overhead.
    _t4_output_cap = max(10_000,
                         deps.single_call_char_limit - len(genesis) - len(functional_req) - 10_000)
    # test_output: tail-weighted (most recent failures are most actionable).
    truncated_output = smart_truncate(test_output, total_limit=_t4_output_cap,
                                      head=_t4_output_cap // 4, tail=3 * _t4_output_cap // 4)

    # Regression / comparison section.
    # Prefer caller-supplied prev data (per-module mode); fall back to file lookup (legacy mode).
    current_pass = count_passing_tests(test_output)
    comparison_section = ""

    _prev_output = prev_test_output
    _prev_pass = prev_pass_count
    if not _prev_output and isinstance(iteration, int) and iteration > 1:
        prev_file = Path(output_dir) / f"tests_output_{iteration - 1}.txt"
        prev_content = deps.read(prev_file)
        if prev_content:
            _prev_output = extract_test_output_from_file(prev_content)
            _prev_pass = count_passing_tests(_prev_output)

    if _prev_output:
        _t4_used = len(genesis) + len(functional_req) + len(truncated_output)
        _t4_secondary_budget = max(5_000, deps.single_call_char_limit - _t4_used - 10_000)
        _prev_cap = _t4_secondary_budget // 2
        prev_truncated = smart_truncate(_prev_output, total_limit=_prev_cap,
                                        head=_prev_cap // 4, tail=3 * _prev_cap // 4)
        delta = current_pass - _prev_pass
        trend = (f"IMPROVED by {delta} tests" if delta > 0
                 else f"REGRESSED by {abs(delta)} tests" if delta < 0
                 else "NO CHANGE")
        comparison_section = f"""
Previous iteration result: {_prev_pass} tests passing.
Current iteration result: {current_pass} tests passing.
Trend: {trend}.

{"⚠️  The last genesis.md change made things WORSE. You must take a completely different approach — do not refine the previous change, reverse it and try something different." if delta < 0 else ""}
{"⚠️  The last genesis.md change had NO EFFECT. The previous approach is not working. Try a substantively different reasoning strategy." if delta == 0 else ""}

Previous test output (for comparison):
{prev_truncated}
"""

    # failure_log shares the secondary budget with prev_output.
    _t4_used2 = len(genesis) + len(functional_req) + len(truncated_output)
    _t4_log_budget = max(5_000, deps.single_call_char_limit - _t4_used2 - 10_000) // 2
    _t4_failure_log_cap = _t4_log_budget
    failure_log_capped = smart_truncate(
        failure_log, total_limit=_t4_failure_log_cap,
        head=_t4_failure_log_cap // 4, tail=3 * _t4_failure_log_cap // 4,
    )
    failure_log_section = (
        f"\nYou have already attempted to fix this {failure_type} failure multiple times. "
        f"Here is the history of what was tried and what happened each time — "
        f"do NOT repeat any approach that already failed. Each iteration that did not improve "
        f"the pass count means that approach is wrong:\n{failure_log_capped}\n"
        f"Study this history carefully. The next update to genesis.md must take a substantively "
        f"different angle than anything already tried above.\n"
    ) if failure_log.strip() else ""

    # Different prompt depending on whether this is a build or test failure.
    # source_context can be tens of KB on a multi-file module — cap it against
    # whatever budget remains after genesis/req/output/comparison/log.
    if source_context.strip():
        _t4_src_used = (len(genesis) + len(functional_req) + len(truncated_output)
                        + len(comparison_section) + len(failure_log_section))
        _t4_src_cap = max(5_000, deps.single_call_char_limit - _t4_src_used - 10_000)
        _source_truncated = smart_truncate(source_context, total_limit=_t4_src_cap,
                                           head=_t4_src_cap // 3, tail=2 * _t4_src_cap // 3)
        source_section = f"\nGenerated source files (the code that was compiled/tested):\n{_source_truncated}\n"
    else:
        source_section = ""

    # If a pre-strip API surface was captured, TASK 1 received it as structural
    # ground truth. The methodology in genesis.md must reflect that contract:
    # we are NOT asking TASK 1 to derive signatures from tests anymore -- only
    # behaviour. A regression caused by signature drift is a TASK 2 / LLM
    # discipline issue, not a methodology gap that genesis should chase.
    api_surface_section = ""
    if api_surface and api_surface.strip():
        api_surface_section = (
            "\n⚠️  CONTEXT — pipeline contract has changed:\n"
            "TASK 1 received an explicit API surface (signatures + type definitions)\n"
            "captured before strip. That is structural GROUND TRUTH. The methodology\n"
            "you are updating must therefore focus on BEHAVIOURAL extraction from tests\n"
            "(what each function returns, invariants, error paths) — not on rediscovering\n"
            "signatures or struct shapes from test-call patterns. If the failure looks\n"
            "like the LLM ignored the provided API surface (wrong signature, missing\n"
            "pub item, renamed field), the lesson is for the GENERATION step's rule set,\n"
            "not a new test-extraction rule.\n"
        )

    # BL-019: build sub-category hint for the prompt.
    _failure_hint = {
        "assertion": "Tests ran. The failure is an ASSERTION mismatch — the function returned "
                     "a wrong value or the behavior does not match what the test expects. "
                     "Focus on: what was the expected value vs. actual, and what requirement "
                     "was incomplete or incorrect.",
        "panic":     "Tests ran but the code PANICKED (not from an assertion). This typically "
                     "means an unwrap() on None/Err, index out of bounds, or an invariant "
                     "violation. Focus on: what invariant or error condition was not captured "
                     "in the requirements.",
        "timeout":   "The test runner TIMED OUT. This usually means an infinite loop, "
                     "deadlock, or unexpectedly slow operation. Focus on: what algorithmic "
                     "or termination condition was missing from the requirements.",
        "no_tests":  "ZERO tests ran despite test files existing. This is a test-harness "
                     "misconfiguration — check module declarations, feature flags, and "
                     "test discovery conventions.",
    }.get(failure_type, "")

    if _is_build:
        prompt = f"""You are refining a methodology for translating test suites into functional requirements.

The implementation failed to COMPILE this iteration — the tests could not even run.
This is a STRUCTURAL failure: the generated code was missing required files, had wrong
module declarations, or had incorrect workspace layout. This is NOT a logic error.
{api_surface_section}
Your task: update genesis.md so that its methodology explicitly captures the structural
information that leads to correct compilation. The methodology must teach readers how to
derive workspace layout, required entry-point files, and module structure from tests and
README alone — as general rules, not project-specific fixes.

genesis.md (current methodology):
{genesis}

functional_requirements.md (requirements that led to broken code):
{functional_req}
{source_section}
Compiler/build output (what broke):
{truncated_output}
{comparison_section}{failure_log_section}
Analyze in this order:
1. What structural information was absent from functional_requirements.md that caused the build to fail?
2. Which step in genesis.md failed to extract or require that structural information?
3. What general rule should genesis.md add so that any project's workspace structure,
   entry-point files, and module declarations are always captured in requirements?

Produce an updated genesis.md. Rules must be:
- General and language-agnostic (not "always generate src/lib.rs" but "for any module
  system, identify and require every declared entry-point file")
- Never mention specific tests, files, iteration numbers, or this experiment
- More precise and complete than the current version

Output the updated genesis.md between these exact delimiters (required — do not use any other format):
{deps.verbosity_guard}
{GENESIS_DELIM_START}
updated genesis.md content here
{GENESIS_DELIM_END}
"""
    else:
        failure_hint_section = (
            f"\n⚠️  Failure sub-type: {failure_type.upper()}\n{_failure_hint}\n"
        ) if _failure_hint else ""
        prompt = f"""You are refining a general methodology for translating test suites into functional requirements.

genesis.md is not a patch document — it is a project-independent, language-agnostic reasoning
process that, when mature, should work on any test suite without needing iteration. Every update
must move it toward greater generality and precision, not toward narrow fixes for the specific
failures just observed.
{api_surface_section}{failure_hint_section}
genesis.md (the reasoning process followed this iteration):
{genesis}

functional_requirements.md (produced by that process):
{functional_req}
{source_section}
Test output (iteration {iteration}):
{truncated_output}
{comparison_section}{failure_log_section}
Analyze in this order:
1. Which tests failed and what do the failures reveal about what was missing or wrong in
   the functional requirements?
2. Trace each gap back to the reasoning process in genesis.md: which step produced an
   incomplete or incorrect requirement, and why?
3. What is the general CLASS of reasoning error — not "I missed function X" but
   "the process did not account for category Y of test signal"?
4. How should genesis.md be updated to permanently close that class of error for
   any future project, in any language?

Produce an updated genesis.md. It must:
- Remain a general, language-agnostic methodology, not a list of project-specific patches
- Be more precise and complete than the previous version
- Reflect the class of error, not just the symptom
- Never mention specific tests, test file names, test failures, iteration numbers, or
  the iterative process by which it was built

Output the updated genesis.md between these exact delimiters (required — do not use any other format):
{deps.verbosity_guard}
{GENESIS_DELIM_START}
updated genesis.md content here
{GENESIS_DELIM_END}
"""

    def attempt():
        response = deps.ask(prompt, model=deps.task4_model)
        content = _extract_genesis(response)
        if content is None:
            log(f"  WARNING: {deps.model} did not return an updated genesis.md.")
            log(f"  Raw response snippet: {response[:600]}")
            return False
        deps.write(genesis_path, content)
        log("  genesis.md updated.")
        return True

    ok = with_retry(attempt)
    if not ok:
        log("  task4 failed after all retries -- genesis.md unchanged this iteration.")

    # BL-024: write structured rules to the pipeline knowledge store.
    # Extract bullet-point rules from the new genesis.md content.
    if ok:
        new_genesis = deps.read(genesis_path)
        deps.extract_and_store_rules(
            genesis_path.parent, new_genesis,
            module_scope="*",  # genesis rules are global
            failure_type=failure_type,
            iteration=iteration if isinstance(iteration, int) else 0,
        )

    emit("task.done", task="task4",
         attempt=iteration if isinstance(iteration, int) else None,
         duration_ms=int((time.monotonic() - _t4_t0) * 1000),
         data={"ok": ok, "current_pass_count": current_pass,
               "failure_type": failure_type})
    return ok, current_pass
