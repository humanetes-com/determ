"""No new reader decides "a crash is present" from binary completion.

Isolation (2026-09-19) re-runs every aborted binary one test per process and
rebuilds its section COMPLETE; the abort survives as `BinaryResult.aborted`
/ `aborted_silent`. Two decisions kept asking `not b.completed` and went dead
with every test green: the stale-refill probe and the schedule's
unmeasurable demotion (run `118933-877c43bd`, 2026-09-27). The one
definition is `test_results.native_crashes` (and `unmeasured_failures` for
"verdict never read"); `tests/integration/test_isolated_abort_verdicts.py`
pins it to real cargo in the pre-run gate.

This test is the static half: every read of `.completed` in `pipeline_core`
is listed below with the reason it is not a crash decision. A new one fails
here until it either goes through `native_crashes` or is added with its
reason — which is the moment to ask whether it is the third blind reader.
Keyed by file and enclosing function, so moving code does not fail it.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "pipeline_core"

ALLOWED: dict[tuple[str, str], str] = {
    # The parser and the model that DEFINE completion and crash.
    ("domain/test_results.py", "parse_binary_results"): "defines completed",
    ("domain/test_results.py", "parse_binary_results._Section.__init__"): "defines completed",
    ("domain/test_results.py", "native_crashes"): "the one crash definition",
    ("domain/test_results.py", "still_open_names"): "dangling name of an unclosed section (fallback shape)",
    ("domain/test_results.py", "TestStateReading.passed_names"): "verdict bookkeeping, not a crash decision",
    ("domain/test_results.py", "TestStateReading.keyed"): "verdict bookkeeping per (binary, test), the passed_names rule per key; not a crash decision",
    ("domain/test_results.py", "TestStateReading.judged_names"): "verdict bookkeeping, not a crash decision",
    ("domain/test_results.py", "TestStateReading.crashed"): "unclosed sections for compare_readings' fallback branches",
    ("domain/test_results.py", "compare_readings"): "per-test comparison; unclosed sections are the documented fallback",
    ("domain/test_isolation.py", "read_single_test_run"): "reads ONE test's own process",
    ("domain/test_report.py", "crash_panics"): "stub reading over unclosed sections (fallback shape)",
    ("domain/test_report.py", "stub_abort_reading"): "stub reading over unclosed sections (fallback shape)",
    ("domain/measurement.py", "qualified_verdicts"): "incomplete labels for the measurement record",
    ("domain/reference_floor.py", "pairs_of"): "incomplete labels of the pristine reference",
    ("application/reconstruct/cargo_runner.py", "isolate_aborted_binaries"): "selects the binaries to isolate",
    ("application/reconstruct/crash_localizer.py", "_asan_sites"): "the sanitized sweep is not isolated",
    ("application/reconstruct/crash_settler.py", "_judged_failed"): "verdict bookkeeping (failed in completed binaries, the passed_names rule), not a crash decision",
    ("application/reconstruct/phase_c_tdd.py", "run_phase_c_tdd._reconcile"): "oracle accounting count",
    ("application/emit_behaviour_tests.py", "_verdict_of"): "behaviour-test emission, not the loop",
    ("adapters/coverage_owner.py", "_in_flight_test"): "coverage run of one test",
    ("adapters/coverage_owner.py", "_exact_run"): "coverage run of one test",
    ("adapters/coverage_owner.py", "_owner_map._cut_before_death"): "coverage run of one test",
    ("adapters/coverage_owner.py", "_owner_map._one"): "coverage run of one test",
    ("adapters/coverage_owner.py", "_measure_world"): "coverage runs of one test each",
}


def _visit(node: ast.AST, rel: str, stack: list[str],
           found: set[tuple[str, str]]) -> None:
    scoped = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef))
    if scoped:
        stack.append(node.name)  # type: ignore[attr-defined]
    if isinstance(node, ast.Attribute) and node.attr == "completed":
        found.add((rel, ".".join(stack)))
    if (isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "getattr"
            and len(node.args) > 1
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "completed"):
        found.add((rel, ".".join(stack)))
    for child in ast.iter_child_nodes(node):
        _visit(child, rel, stack, found)
    if scoped:
        stack.pop()


def _sites() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        _visit(ast.parse(path.read_text(encoding="utf-8")), rel, [], found)
    return found


def test_every_completion_reader_is_a_listed_non_crash_decision() -> None:
    new = sorted(s for s in _sites() if s not in ALLOWED)
    assert not new, (
        "new `.completed` reader(s): " + ", ".join(f"{f}::{fn}" for f, fn in new)
        + ". Under test isolation an aborted binary is COMPLETE; to decide "
        "whether a crash is present use test_results.native_crashes (or "
        "unmeasured_failures). If this is not a crash decision, list it in "
        "ALLOWED with the reason.")


def test_the_allow_list_has_no_stale_entries() -> None:
    stale = sorted(set(ALLOWED) - _sites())
    assert not stale, f"ALLOWED entries no longer in the code: {stale}"
