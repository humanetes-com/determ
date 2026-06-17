"""Pins for the second false-SUCCESS layer (2026-06-12 microcrate, post-C9):
task3's strict per-cluster verdict said INCOMPLETE (stable reported 10 of 12
expected) but the run still committed, tagged and printed SUCCESS, because:

  A. the main loop re-derived the verdict from parsed counts via the LENIENT
     validate_task3_output ("24 passed, 0 failed" → pass), discarding task3's
     returned boolean → pinned via resolve_task3_verdict (domain);
  B. task3's reverify recursion returned without filling the outer phase's
     done["data"] → the outer task.done event carried no `passed` key at all
     (audit C9 saw passed=None);
  C. _count_active_tests_for_cluster counts #[test] fns per FILE without
     following the module tree's cfg gates — cap.rs is counted under stable
     although `mod cap;` is extra_cap-gated in lib.rs, so stable expects 12,
     runs 10, and can never pass the strict verdict.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pipeline_core.log as pclog  # noqa: E402
from pipeline_core.domain.models import BuildCluster  # noqa: E402

_FAIL_LINE = "FAILED. 11 passed; 3 failed; 0 ignored; 0 measured; 0 filtered out"


# --- A: strict boolean is the verdict authority --------------------------


def test_resolve_verdict_strict_false_never_pass() -> None:
    """The exact false-pass shape: 24 passed, 0 failed, but task3 said NO
    (a cluster was incomplete). Counts must not overrule the strict gate."""
    from pipeline_core.domain.test_results import resolve_task3_verdict
    status, _reason = resolve_task3_verdict(False, 24, 0, True)
    assert status != "pass"


def test_resolve_verdict_strict_true_is_pass() -> None:
    from pipeline_core.domain.test_results import resolve_task3_verdict
    assert resolve_task3_verdict(True, 26, 0, True)[0] == "pass"


def test_resolve_verdict_failure_subtypes_preserved() -> None:
    """The lenient classifier still subtypes genuine failures for TASK 4
    routing — it just may never upgrade them to pass."""
    from pipeline_core.domain.test_results import resolve_task3_verdict
    assert resolve_task3_verdict(False, 21, 3, True)[0] == "fail"
    assert resolve_task3_verdict(False, 0, 0, False)[0] == "no_files"
    assert resolve_task3_verdict(False, 0, 0, True)[0] == "no_tests"


def test_cluster_failure_actionability() -> None:
    """A non-pass cluster is actionable by the code-fix loop only when its
    output carries a repairable signal — failing tests, or a nonzero exit with
    compiler diagnostics. A clean-exit shortfall (rc==0, fail==0, fewer tests
    reported than expected) is a count/oracle mismatch, NOT a code defect."""
    from pipeline_core.domain.test_results import ClusterStatus
    from pipeline_core.domain.test_results import (
        cluster_failure_is_actionable as act,
    )
    assert act(ClusterStatus.FAIL, 101, 2) is True       # failing tests
    assert act(ClusterStatus.FAIL, 0, 2) is True         # failures even if rc parsed 0
    assert act(ClusterStatus.INCOMPLETE, 101, 0) is True  # binary failed to compile
    assert act(ClusterStatus.INCOMPLETE, 0, 0) is False   # clean shortfall — phantom
    assert act(ClusterStatus.PASS, 0, 0) is False


def test_clean_shortfall_skips_code_fix_retry(tmp_path: Path, monkeypatch) -> None:
    """A cluster that exits cleanly (rc==0, 0 fail) but reports fewer tests than
    expected must NOT be routed to the inline code-fix retry — that loop has no
    signal to act on and would churn (the hardcrate pass=N plateau). The retry
    must be skipped; the run still fails (never a false SUCCESS)."""
    from pipeline_core.application.tasks.task3 import Task3Deps, task3
    monkeypatch.setattr(pclog, "_log_sink", lambda m: None)
    monkeypatch.setattr(pclog, "_emit_sink", lambda e, **f: None)

    @contextmanager
    def fake_phase(kind: str, **kw):
        d: dict = {}
        yield d

    retry_calls: list[dict] = []
    deps = Task3Deps(
        phase=fake_phase,
        load_baked_clusters=lambda p: [SimpleNamespace(
            name="stable", toolchain="", env_extra={})],
        task3_legacy_single=lambda *a, **k: (False, ""),
        # Clean run: rc=0, "2 passed; 0 failed"; but expected=3 → shortfall.
        cargo_env=lambda: {"PATH": "/usr/bin:/bin"},
        parse_cmd=lambda cmd: [
            "sh", "-c", 'echo "test result: ok. 2 passed; 0 failed; 0 ignored"'],
        count_active_tests_for_cluster=lambda p, c: 3,
        store_prompt_content=lambda s: "",
        task3_inline_retry=lambda **k: retry_calls.append(k) or (True, "x"),
        read=lambda p: "",
        write=lambda p, s: None,
        test_timeout=30,
    )
    passed, _out = task3(tmp_path, "cargo test", 1, tmp_path,
                         tmp_path / "genesis.md", deps)
    assert passed is False                 # never a false SUCCESS
    assert retry_calls == []               # the churn-prone retry was skipped


# --- B: every task3 task.done carries the verdict -------------------------


def test_task3_reverify_outer_phase_carries_verdict(tmp_path: Path,
                                                    monkeypatch) -> None:
    """When the inline retry triggers a reverify recursion, the OUTER task3
    phase must still record done["data"]["passed"] — the recursion path
    returned early and left the outer task.done with no verdict at all."""
    from pipeline_core.application.tasks.task3 import Task3Deps, task3
    monkeypatch.setattr(pclog, "_log_sink", lambda m: None)
    monkeypatch.setattr(pclog, "_emit_sink", lambda e, **f: None)

    phases: list[dict] = []

    @contextmanager
    def fake_phase(kind: str, **kw):
        d: dict = {}
        phases.append(d)
        yield d

    # Inline retry claims progress once (forces the reverify recursion),
    # then gives up; the cluster keeps failing identically.
    retry_results = [(True, f"test result: {_FAIL_LINE}"),
                     (False, f"test result: {_FAIL_LINE}")]

    deps = Task3Deps(
        phase=fake_phase,
        load_baked_clusters=lambda p: [SimpleNamespace(
            name="stable", toolchain="", env_extra={})],
        task3_legacy_single=lambda *a, **k: (False, ""),
        cargo_env=lambda: {"PATH": "/usr/bin:/bin", "FAKE_RESULT": _FAIL_LINE},
        parse_cmd=lambda cmd: ["sh", "-c", 'echo "test result: $FAKE_RESULT"'],
        count_active_tests_for_cluster=lambda p, c: 14,
        store_prompt_content=lambda s: "",
        task3_inline_retry=lambda **k: retry_results.pop(0),
        read=lambda p: "",
        write=lambda p, s: None,
        test_timeout=30,
    )
    passed, _out = task3(tmp_path, "cargo test", 1, tmp_path,
                         tmp_path / "genesis.md", deps)
    assert passed is False
    assert len(phases) == 2          # outer + one reverify recursion
    for i, d in enumerate(phases):
        assert "data" in d and "passed" in d["data"], (
            f"task3 phase {i} closed without a verdict in done['data'] — "
            "its task.done event carries no `passed` key")
        assert d["data"]["passed"] is False


# --- C: expected-count follows the module tree's cfg gates ----------------


def _write_workspace(ws: Path) -> None:
    (ws / "src").mkdir(parents=True)
    (ws / "tests").mkdir()
    (ws / "src" / "lib.rs").write_text(
        "mod counter;\n"
        '#[cfg(any(extra_cap, feature = "extra_cap"))]\n'
        "mod cap;\n"
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    #[test]\n"
        "    fn lib_t() {}\n"
        "}\n"
    )
    (ws / "src" / "counter.rs").write_text(
        '#[cfg(any(extra_cap, feature = "extra_cap"))]\n'
        "mod deep;\n"
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    #[test]\n"
        "    fn c1() {}\n"
        "}\n"
    )
    (ws / "src" / "deep.rs").write_text(
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    #[test]\n"
        "    fn d1() {}\n"
        "}\n"
    )
    (ws / "src" / "cap.rs").write_text(
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    #[test]\n"
        "    fn k1() {}\n"
        "    #[test]\n"
        "    fn k2() {}\n"
        "}\n"
    )
    (ws / "tests" / "it.rs").write_text(
        "#[test]\n"
        "fn i1() {}\n"
    )


def _cluster(name: str, active: list[str], excluded: list[str]) -> BuildCluster:
    return BuildCluster(name=name, env_extra={}, tarpaulin_exclude_files=[],
                        excluded_cfg=excluded, active_cfg=active,
                        prompt_nightly_rule="")


def test_expected_count_excludes_cfg_gated_module_files(tmp_path: Path) -> None:
    """Under stable, `mod cap;` (and counter's gated `mod deep;`) never
    compile — their tests must not inflate the expected count. Counting them
    makes the stable cluster permanently 'incomplete' (the 12-vs-10 in both
    microcrate runs)."""
    import pipeline
    _write_workspace(tmp_path)
    stable = _cluster("stable", active=[], excluded=["extra_cap"])
    # lib 1 + counter 1 + tests/it.rs 1 — cap.rs (2) and deep.rs (1) are
    # unreachable under stable.
    assert pipeline._count_active_tests_for_cluster(tmp_path, stable) == 3


def test_expected_count_includes_active_cfg_modules(tmp_path: Path) -> None:
    import pipeline
    _write_workspace(tmp_path)
    extra = _cluster("extra_cap", active=["extra_cap"], excluded=[])
    # lib 1 + counter 1 + cap 2 + deep 1 + tests/it.rs 1.
    assert pipeline._count_active_tests_for_cluster(tmp_path, extra) == 6
