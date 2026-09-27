"""Every input of the fill schedule is classified, and every measured one has
a contract test that derives it from what the real tool reports.

Why. On 2026-09-27 (run `118933-877c43bd`) two of the schedule's inputs
were found dead for eight days with every test green: test isolation
(2026-09-19) changed what cargo's capture looks like to the pipeline, and
the readers that fed `unmeasurable_tests` and the stale-refill probe kept
asking a question the new capture could never answer. Their tests fed the
schedule hand-written inputs, so nothing could notice. A decision is only as
alive as its inputs; this module makes "has a real-output contract" a
property of every input, checked at the schedule's own signature, so a new
parameter without one fails `full_pre_run.sh`.

The classes:
- ``tool``: read from a real tool's output (cargo/libtest, gdb, the
  extractor). Its contracts are integration tests that derive the input
  the way the loop does, over real output, and assert the decision it feeds.
- ``store``: the pipeline's own persisted records (route ledger, body
  ledger). Written and read by the pipeline alone; a round-trip unit test
  is the contract (the repo conventions: no integration test for a defect
  in the pipeline's own logic).
- ``planner``: produced by the pipeline's own route planners; the walks and
  the scheduler tests exercise it through the real loop.
- ``config``: a knob read once at cluster entry; no reader to go blind.

Gated on `INTEGRATION=1` with the rest of the suite; `full_pre_run.sh` runs it.
"""
from __future__ import annotations

import ast
import inspect
import os
from pathlib import Path

import pytest

from pipeline_core.domain.fill_schedule import schedule

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("INTEGRATION") != "1",
                       reason="part of the integration suite; set INTEGRATION=1"),
]

_TESTS = Path(__file__).resolve().parents[1]

# parameter -> (class, contract test ids "path::function", or a reason)
CONTRACTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "unmeasurable_tests": ("tool", (
        "integration/test_isolated_abort_verdicts.py::test_every_schedule_crash_input_sees_the_isolated_abort",
        "integration/test_isolated_abort_verdicts.py::test_negative_control_the_binary_completion_question_is_blind",
    )),
    "crash_blamed": ("tool", (
        "integration/test_isolated_abort_verdicts.py::test_the_localizer_blames_both_members_of_the_recursion",
    )),
    "hollow": ("tool", (
        "integration/test_stub_hold_under_isolation.py::test_under_isolation_the_stub_holds_the_test_that_stops_in_it",
        "integration/test_stub_hold_under_isolation.py::test_the_stub_hold_puts_the_stub_writer_in_the_blockers_tier",
    )),
    "attributed": ("tool", (
        "integration/test_attribution_feeds_the_schedule.py::test_the_failing_tests_named_body_is_the_attributed_write",
        "integration/test_attribution_feeds_the_schedule.py::test_the_split_run_waits_behind_the_attributed_write",
    )),
    "bodies_sha": ("tool", (
        "integration/test_stub_hold_under_isolation.py::test_a_runs_body_identity_joins_the_extractor_records",
    )),
    "oracle_fp": ("store", (
        "test_body_ledger.py::test_the_oracle_fingerprint_is_order_free_path_bound_and_never_empty",
    )),
    "rows": ("store", (
        "test_fill_scheduler.py::test_rows_are_written_and_a_flat_rebuy_is_deferred_next_fill",
    )),
    "unit_evidence": ("store", (
        "test_body_ledger.py::test_a_row_round_trips_and_a_corrupt_one_costs_one_entry_not_the_run",
        "test_fill_schedule_unit_service.py::test_a_reshuffled_batch_of_bought_units_is_a_rebuy_not_never_served",
    )),
    "runs": ("planner", (
        "test_fill_scheduler.py::test_rows_are_written_and_a_flat_rebuy_is_deferred_next_fill",
        "walks/test_walk_measured_owner_admission.py",
    )),
    "budget_minutes": ("config", ("TDD_FILL_BUDGET_MINUTES",)),
    "today_order": ("config", ("the fixed route order constant",)),
    "readmit_after": ("config", ("RETRO_RESET_MAX_ATTEMPTS-style bound, read at entry",)),
    "max_per_kind": ("config", ("SLICE_SESSION_MAX_PER_FILL",)),
    "unpaid_grace": ("config", ("DEFAULT_UNPAID_GRACE",)),
    "session_min_turns": ("config", ("SLICE_SESSION_MIN_TURNS",)),
}

_CLASSES = {"tool", "store", "planner", "config"}


def _exists(test_id: str) -> bool:
    path, _, fn = test_id.partition("::")
    file = _TESTS / path
    if not file.is_file():
        return False
    if not fn:
        return True
    tree = ast.parse(file.read_text(encoding="utf-8"))
    return any(isinstance(n, ast.FunctionDef) and n.name == fn
               for n in ast.walk(tree))


def test_every_schedule_parameter_is_classified() -> None:
    params = set(inspect.signature(schedule).parameters)
    missing = sorted(params - set(CONTRACTS))
    assert not missing, (
        f"schedule() parameter(s) with no contract: {missing}. A new input "
        "needs a class here and, if it is read from a tool, an integration "
        "test that derives it from real output and asserts the decision it "
        "feeds — or it can go blind with every test green.")
    stale = sorted(set(CONTRACTS) - params)
    assert not stale, f"CONTRACTS names parameters schedule() no longer has: {stale}"


def test_every_named_contract_exists() -> None:
    bad = []
    for param, (cls, refs) in CONTRACTS.items():
        assert cls in _CLASSES, (param, cls)
        if cls == "config":
            continue
        assert refs, f"{param}: a {cls} input needs at least one contract test"
        bad += [f"{param}: {r}" for r in refs if not _exists(r)]
    assert not bad, "contract test(s) not found: " + "; ".join(bad)


def test_every_tool_input_has_a_real_output_contract() -> None:
    # A tool input's contracts must be integration tests: a unit test's
    # capture is written by hand and cannot go stale with the tool.
    for param, (cls, refs) in CONTRACTS.items():
        if cls == "tool":
            assert all(r.startswith("integration/") for r in refs), param
