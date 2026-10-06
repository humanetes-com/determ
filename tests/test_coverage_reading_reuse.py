"""A coverage reading is reused only when the sweep asks the same question.

The question is the tree, the bare failing names, the binaries that failed
each name, the binaries that never finished, and the hung labels. A bare
name that gains a second binary, or a trailerless binary beside an unchanged
name, is a different question: pass C unions owners per binary, and the
stored map is the first claimant's.

The captures are the shared-name shape of run `3893442-cb547388`: `t` fails
in `tests/one.rs`, then also in `tests/two.rs`. The tree and the bare name
set stay `t`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.phase_c_tdd import (
    PhaseCTddDeps,
    run_phase_c_tdd,
)
from pipeline_core.domain.attribution import AttributionResult, FailureKind
from pipeline_core.domain.coverage_slice import CoverageSlice
from pipeline_core.domain.models import BuildCluster, ClusterFillResult, FnLocator, FnSite
from pipeline_core.domain.stub_inventory import StubInventory

_A = FnLocator("src/a.rs", "a")

_ONE = """     Running tests/one.rs (target/debug/deps/one-aaa)

running 1 test
test t ... FAILED

failures:

---- t stdout ----

thread 't' panicked at tests/one.rs:3:5:
assertion

failures:
    t

test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s

"""

_TWO = _ONE + """     Running tests/two.rs (target/debug/deps/two-bbb)

running 1 test
test t ... FAILED

failures:

---- t stdout ----

thread 't' panicked at src/lib.rs:6:5:
not implemented

failures:
    t

test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s
"""

_INCOMPLETE = _ONE + """     Running tests/two.rs (target/debug/deps/two-bbb)

running 1 test
test t ...
"""


def _drive(tmp_path: Path, monkeypatch: Any, outputs: list[str]) -> list[dict[str, frozenset[str]]]:
    asked: list[dict[str, frozenset[str]]] = []

    def _fill(**_kw: Any) -> ClusterFillResult:
        return ClusterFillResult(attempts=1, dispatched_fns=(("src/a.rs", "a"),))

    def _coverage(*_a: Any, failing_binaries: dict[str, frozenset[str]] | None = None,
                  **_k: Any) -> CoverageSlice:
        asked.append({n: frozenset(v) for n, v in (failing_binaries or {}).items()})
        return CoverageSlice(owners={"t": frozenset({FnSite(_A.file, _A.fn_name, "")})})

    def _cargo(*_a: Any, **_k: Any) -> tuple[int, str]:
        # The sweep runs before the reading. Later cargo calls in the same
        # iteration (a gate, a re-sweep) keep the output this reading was
        # asked about; the next sweep advances only after a reading.
        return 101, outputs[min(len(asked), len(outputs) - 1)]

    deps = PhaseCTddDeps(
        attribute_failures=lambda *a, **k: AttributionResult(
            {_A: {"t"}}, FailureKind.BEHAVIOR, {FnSite(_A.file, _A.fn_name, ""): {"t"}}),
        build_test_reverse_map=lambda *a, **k: {},
        filter_test_targets=lambda fr, src, **_k: dict(fr),
        check_graceful_stop=lambda *a, **k: None,
        check_auto_operator=lambda *a, **k: None,
        converge_in_cluster=lambda *a, **k: None,
        count_relevant_notes=lambda *a, **k: 0,
        effective_stalls=lambda budget, module: budget,
        emit=lambda *a, **k: None,
        log=lambda *a, **k: None,
        order_frontier=lambda eligible, counts: list(eligible.items()),
        record_tdd_regression=lambda *a, **k: "",
        reset_exhaustion_flags=lambda *a, **k: [],
        run_cargo=_cargo,
        scan_unimplemented_for_cluster=lambda *a, **k: StubInventory.of_units([_A]),
        scan_unimplemented_functions=lambda *a, **k: StubInventory.of_units([_A]),
        task2_fill_fns_one_by_one=_fill,
        measure_coverage_slice=_coverage,
    )
    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "0")
    monkeypatch.setenv("TDD_MAX_STALLS", "0")
    monkeypatch.setenv("TDD_NO_FRONTIER_STOP", "1")
    monkeypatch.setenv("TDD_FLAT_SCORE_REPEATS", "0")
    monkeypatch.setenv("PHASE_C_TDD_BACKSTOP", "4")
    run_phase_c_tdd(
        deps, project_dir=tmp_path, target_src_dir=tmp_path, tests_dir_files=[],
        cluster_tests=[("tests/one.rs", "fn t() {}")], module="m",
        cluster=BuildCluster(name="c", env_extra={}, tarpaulin_exclude_files=[],
                             excluded_cfg=[], active_cfg=[], prompt_nightly_rule=""),
        check_cmd="cargo check", cluster_env={},
        leaf_counts=None, fr_path=None,
        test_cmd_builder=lambda stems, include_lib=False: "cargo test",
        tag="[c]")
    return asked


def test_a_second_binary_of_the_same_name_is_measured_again(
        tmp_path: Path, monkeypatch: Any) -> None:
    asked = _drive(tmp_path, monkeypatch, [_ONE, _TWO])
    assert {"t": frozenset({"one"})} in asked
    assert {"t": frozenset({"one", "two"})} in asked, asked


def test_a_trailerless_binary_beside_the_same_name_is_measured_again(
        tmp_path: Path, monkeypatch: Any) -> None:
    asked = _drive(tmp_path, monkeypatch, [_ONE, _INCOMPLETE])
    # Both questions name `t` in `one` only. The second sweep's unfinished
    # `two` is still a different question, so the reading is paid again.
    assert asked.count({"t": frozenset({"one"})}) >= 2, asked
