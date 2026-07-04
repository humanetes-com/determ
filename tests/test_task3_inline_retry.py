"""Characterization for the extracted task3 inline-retry use-case (chunk H).

Proves `run_task3_inline_retry` works through CONSTRUCTOR-injected fakes (no
monkeypatch of module globals) — the DI rail the pipeline.py shim wires the
resident collaborators into. The end-to-end monkeypatch path stays covered by
tests/test_e2e_build_fix.py + test_e2e_routing.py.
"""

from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct.per_fn_retry import (
    RetryDeps,
    Task3Attribution,
    Task3Cargo,
    Task3Enumerate,
    Task3RetryDeps,
    run_task3_inline_retry,
)


def _failed(n: int) -> str:
    rows = "".join(f"test t_{i} ... FAILED\n" for i in range(n))
    return f"running {n} tests\n{rows}\ntest result: FAILED. 0 passed; {n} failed\n"


def _deps(*, run=None, check_diag=None) -> Task3RetryDeps:
    def _unused(*_a, **_k):  # noqa: ANN002, ANN003
        raise AssertionError("collaborator should not be called on this path")
    return Task3RetryDeps(
        cargo=Task3Cargo(
            env=lambda: {},
            run=run or _unused,
            check_diag=check_diag or _unused,
        ),
        attribution=Task3Attribution(
            attribute_failures=_unused, build_test_reverse_map=_unused,
            frontier_from_diags=_unused, filter_test_targets=_unused),
        enumerator=Task3Enumerate(
            cluster_filter_type_methods=lambda recs, cl: [],
            enumerate_types=lambda root: [], fn_container_index=lambda root: {}),
        retry_fix=RetryDeps(
            locate_fn=_unused, gather_fix_context=_unused,
            build_per_fn_fix_prompt=_unused, ask_per_fn_body=_unused,
            splice_fn_body=_unused, find_tests_for_fn=_unused,
            focus_cargo_errors=_unused, is_gen_tests_file=lambda n: False,
            log=lambda *_: None),
        log=lambda *_: None,
        test_timeout=10,
    )


def test_build_mode_no_reproducible_diags_bails(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    initial = "error: could not compile (no FAILED test lines)\n"
    deps = _deps(check_diag=lambda *a, **k: (1, "", []))  # build mode, no diags
    passed, out = run_task3_inline_retry(
        tmp_path, "cargo test", 1, initial, None,
        max_stalls=3, hard_cap=10, deps=deps)
    assert passed is False
    assert out == initial


def test_env_gate_disables_retry(tmp_path: Path, monkeypatch) -> None:
    """TASK3_INLINE_RETRY=0 short-circuits before touching any collaborator
    (the gate moved from the pipeline.py shim into the use-case, S27)."""
    monkeypatch.setenv("TASK3_INLINE_RETRY", "0")
    initial = _failed(1)
    passed, out = run_task3_inline_retry(
        tmp_path, "cargo test", 1, initial, None, deps=_deps())
    assert passed is False and out == initial


def test_env_default_stall_knobs(tmp_path: Path, monkeypatch) -> None:
    """Omitted max_stalls/hard_cap read TASK3_INLINE_* per call."""
    monkeypatch.setenv("TASK3_INLINE_MAX_STALLS", "1")
    (tmp_path / "src").mkdir()
    initial = "error: could not compile (no FAILED test lines)\n"
    deps = _deps(check_diag=lambda *a, **k: (1, "", []))  # build mode, no diags
    passed, out = run_task3_inline_retry(
        tmp_path, "cargo test", 1, initial, None, deps=deps)
    assert passed is False and out == initial


def test_test_mode_passes_immediately(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    initial = _failed(2)  # has FAILED lines → TEST mode
    ok_out = "test result: ok. 2 passed; 0 failed\n"
    deps = _deps(run=lambda *a, **k: (0, ok_out))  # first check → pass
    passed, out = run_task3_inline_retry(
        tmp_path, "cargo test", 1, initial, None,
        max_stalls=3, hard_cap=10, deps=deps)
    assert passed is True
    assert out == ok_out
