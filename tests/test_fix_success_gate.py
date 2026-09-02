"""Pins for the post-task3 fix-loop success gate (2026-06-12 microcrate false
SUCCESS): task3 failed per-cluster (extra_cap 3 failed, stable incomplete) but
task2_fix_loop validated with the single default-cluster command — its first
check instantly "passed" and the run printed SUCCESS and exited 0.

Contract pinned here:
  - task3's per-cluster execution is extracted as `run_clusters_strict`
    (single source of truth for the "all tests pass" verdict).
  - `generate_fix_loop` accepts `check_fn` overriding the shell `check_cmd`.
  - `task2_fix_loop` threads a per-cluster check (Task2Deps.cluster_check_factory)
    into generate_fix_loop as check_fn — never the bare module test command.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _cluster(name: str, env_extra: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(name=name, toolchain="", env_extra=env_extra or {})


def _fake_task3_deps(expected: dict[str, int]):
    """Task3Deps whose subprocess seam echoes a cargo `test result:` line taken
    from $FAKE_RESULT — per-cluster env_extra overrides it, exactly the channel
    a real cluster uses for RUSTFLAGS/cfg."""
    from pipeline_core.application.tasks.task3 import Task3Deps
    return Task3Deps(
        phase=None,
        load_baked_clusters=lambda p: [],
        task3_legacy_single=lambda *a, **k: (False, ""),
        cargo_env=lambda: {
            "PATH": os.environ.get("PATH", ""),
            "FAKE_RESULT": "ok. 5 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out",
        },
        parse_cmd=lambda cmd: ["sh", "-c", 'echo "test result: $FAKE_RESULT"'],
        count_active_tests_for_cluster=lambda p, c: expected[c.name],
        store_prompt_content=lambda s: "",
        task3_inline_retry=lambda **k: (False, ""),
        read=lambda p: "",
        write=lambda p, s: None,
        test_timeout=30,
    )


def test_run_clusters_strict_flags_failing_cfg_cluster(tmp_path: Path) -> None:
    """Default cluster green, cfg-gated cluster red → verdict is NOT pass, and
    both clusters' outputs are in the aggregate (this is the exact shape of the
    false-SUCCESS run: stable 5/5, extra_cap 11/14)."""
    from pipeline_core.application.tasks.task3 import run_clusters_strict
    deps = _fake_task3_deps({"stable": 5, "extra_cap": 14})
    clusters = [
        _cluster("stable"),
        _cluster("extra_cap", {"FAKE_RESULT":
                 "FAILED. 11 passed; 3 failed; 0 ignored; 0 measured; 0 filtered out"}),
    ]
    all_pass, output, results = run_clusters_strict(tmp_path, clusters, deps)
    assert all_pass is False
    assert "stable" in output and "extra_cap" in output
    by_name = {r["cluster"]: r for r in results}
    assert by_name["stable"]["status"] == "pass"
    assert by_name["extra_cap"]["status"] == "fail"


def test_run_clusters_strict_flags_incomplete_cluster(tmp_path: Path) -> None:
    """rc=0 with fewer tests reported than expected (a binary silently didn't
    run) must NOT pass — the stable cluster reported 10 of 12 in the false-
    SUCCESS run."""
    from pipeline_core.application.tasks.task3 import run_clusters_strict
    deps = _fake_task3_deps({"stable": 12})
    clusters = [_cluster("stable", {"FAKE_RESULT":
                "ok. 10 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out"})]
    all_pass, _output, results = run_clusters_strict(tmp_path, clusters, deps)
    assert all_pass is False
    assert results[0]["status"] == "incomplete"


def test_run_clusters_strict_all_green_passes(tmp_path: Path) -> None:
    from pipeline_core.application.tasks.task3 import run_clusters_strict
    deps = _fake_task3_deps({"stable": 5, "extra_cap": 5})
    clusters = [_cluster("stable"), _cluster("extra_cap")]
    all_pass, _output, results = run_clusters_strict(tmp_path, clusters, deps)
    assert all_pass is True
    assert all(r["status"] == "pass" for r in results)


def test_run_clusters_strict_uses_the_injected_cluster_env(tmp_path: Path) -> None:
    """When cluster_env is bound, env_extra is not merged by dict.update on
    cargo_env() — that overlay is what replaced RUSTFLAGS and skipped the
    toolchain. cargo_env() here returns a failing FAKE_RESULT and env_extra
    would too; only the injected mapping is green."""
    from pipeline_core.application.tasks.task3 import Task3Deps, run_clusters_strict
    seen: list[str] = []

    def cluster_env(c: SimpleNamespace) -> dict[str, str]:
        seen.append(c.name)
        return {
            "PATH": os.environ.get("PATH", ""),
            "FAKE_RESULT": "ok. 5 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out",
        }

    deps = Task3Deps(
        phase=None,
        load_baked_clusters=lambda p: [],
        task3_legacy_single=lambda *a, **k: (False, ""),
        cargo_env=lambda: {
            "PATH": os.environ.get("PATH", ""),
            "FAKE_RESULT": "FAILED. 0 passed; 5 failed; 0 ignored; 0 measured; 0 filtered out",
        },
        parse_cmd=lambda cmd: ["sh", "-c", 'echo "test result: $FAKE_RESULT"'],
        count_active_tests_for_cluster=lambda p, c: 5,
        store_prompt_content=lambda s: "",
        task3_inline_retry=lambda **k: (False, ""),
        read=lambda p: "",
        write=lambda p, s: None,
        test_timeout=30,
        cluster_env=cluster_env,
    )
    all_pass, _output, results = run_clusters_strict(
        tmp_path,
        [_cluster("stable", {"FAKE_RESULT":
                  "FAILED. 0 passed; 5 failed; 0 ignored; 0 measured; 0 filtered out"})],
        deps,
    )
    assert all_pass is True
    assert seen == ["stable"]
    assert results[0]["status"] == "pass"


def test_generate_fix_loop_honors_check_fn_over_cmd(tmp_path: Path) -> None:
    """check_fn is the verdict authority: when it reports pass, the loop
    returns success WITHOUT running check_cmd (`false` here would fail rc=1
    and drag the loop into LLM fix attempts)."""
    import pipeline
    calls: list[int] = []

    def check_fn() -> tuple[bool, str]:
        calls.append(1)
        return True, "=== cluster: stable pass ===\n=== cluster: extra_cap pass ==="

    passed, output = pipeline.generate_fix_loop(
        project_dir=tmp_path,
        check_cmd="false",
        source_paths=[],
        existing_test_files=set(),
        check_fn=check_fn,
    )
    assert passed is True
    assert calls == [1]
    assert "extra_cap" in output


def test_task2_fix_loop_threads_cluster_check(tmp_path: Path) -> None:
    """task2_fix_loop must hand generate_fix_loop the per-cluster check built
    by Task2Deps.cluster_check_factory — validating the fix with the bare
    single-cluster test command is the false-SUCCESS bug."""
    from pipeline_core.application.tasks.task2 import Task2Deps, task2_fix_loop

    captured: dict = {}

    def fake_generate_fix_loop(**kwargs) -> tuple[bool, str]:
        captured.update(kwargs)
        return False, "still failing"

    def sentinel_check() -> tuple[bool, str]:
        return False, "cluster 'extra_cap': 3 failed"

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("fn x() {}\n")

    deps = Task2Deps(
        generator_factory=lambda: None,
        load_manifest=lambda p: ["src/lib.rs"],
        read=lambda p: "",
        generate_fix_loop=fake_generate_fix_loop,
        store_prompt_content=lambda s: "",
        is_proc_macro_crate=lambda s: False,
        count_error_codes=lambda s: 0,
        build_check_retries=1,
        build_max_stalls=1,
        build_timeout=10,
        test_timeout=10,
        task2_model="m",
        task2_fix_retries=1,
        single_call_char_limit=100_000,
        genesis_cap=10_000,
        cluster_check_factory=lambda project_dir: sentinel_check,
    )
    passed, _out = task2_fix_loop(
        tmp_path, tmp_path / "manifest.json", set(), "test output",
        "cargo test", tmp_path / "fr.md", deps,
    )
    assert passed is False
    assert captured.get("check_fn") is sentinel_check
