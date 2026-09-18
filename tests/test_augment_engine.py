"""CargoAugmentEngine pure bits + CallableCommitGate delegation."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.adapters.augment_engine import CargoAugmentEngine
from pipeline_core.adapters.commit_gate import CallableCommitGate
from pipeline_core.domain.models import BuildCluster


def _engine(**over: object) -> CargoAugmentEngine:
    deps: dict = {
        "log": lambda _m: None, "cargo_env": dict, "build_timeout": 1,
        "default_setup": Path("/nonexistent/setup.sh"),
        "collect_test_files": lambda _p: [("tests/a.rs", ""), ("src/lib.rs", "")],
        "augment_module": lambda *a, **k: (0.0, 0, False, ""),
    }
    deps.update(over)
    return CargoAugmentEngine(**deps)  # type: ignore[arg-type]


def _cluster(excluded: list[str]) -> BuildCluster:
    return BuildCluster(
        name="stable", env_extra={}, tarpaulin_exclude_files=[],
        excluded_cfg=excluded, active_cfg=[], prompt_nightly_rule="", is_default=True)


def test_estimate_ceiling_no_excluded_is_100() -> None:
    assert _engine().estimate_ceiling(Path("/whatever"), _cluster([])) == (100.0, 0, 0)


def test_estimate_ceiling_drops_excluded_cfg_block(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(
        "fn a() {}\n"                                    # 1  agnostic
        "#[cfg(error_generic_member_access)]\n"          # 2
        "fn egma() {\n"                                  # 3
        "    x();\n"                                      # 4
        "}\n"                                            # 5  excluded block 2..5
    )
    ceiling, excluded, total = _engine().estimate_ceiling(
        tmp_path, _cluster(["error_generic_member_access"]))
    assert total == 5 and excluded == 4 and ceiling == (1.0 - 4 / 5) * 100.0


def test_collect_test_rels() -> None:
    assert _engine().collect_test_rels(Path("/p")) == {"tests/a.rs", "src/lib.rs"}


def test_commit_gate_forwards_all_args() -> None:
    seen: dict = {}
    gate = CallableCommitGate(
        lambda *a, **k: seen.update(args=a, kwargs=k))
    gate.commit_and_mark(Path("/p"), "task3", "done",
                         commit_message="m", detail="d", source_hash="h")
    assert seen["args"] == (Path("/p"), "task3", "done")
    assert seen["kwargs"] == {"commit_message": "m", "detail": "d", "source_hash": "h"}


def test_behaviour_methods_unwired_read_nothing_and_pass_nothing() -> None:
    """A composition root that predates the wiring must not crash the cluster
    loop: the engine reads 0/0 (the loop then skips the rounds) and passes False."""
    logs: list[str] = []
    eng = _engine(log=logs.append)
    assert eng.behaviour_coverage(Path("/p"), _cluster([]), "m", Path("/p")) == (0, 0)
    assert eng.behaviour_pass(Path("/p"), _cluster([]), "m", Path("/p"),
                              project_context="", coverage_strategy="") is False
    assert any("not wired" in m for m in logs)


def test_behaviour_methods_forward_to_the_bound_callables() -> None:
    seen: list = []
    eng = _engine(
        behaviour_coverage=lambda pd, m, md: seen.append(("cov", pd, m, md)) or (2, 5),
        behaviour_pass=lambda pd, m, md, *, project_context, coverage_strategy:
            seen.append(("pass", m, project_context, coverage_strategy)) or True)
    assert eng.behaviour_coverage(Path("/p"), _cluster([]), "m", Path("/p/m")) == (2, 5)
    assert eng.behaviour_pass(Path("/p"), _cluster([]), "m", Path("/p/m"),
                              project_context="ctx", coverage_strategy="strat") is True
    assert seen == [("cov", Path("/p"), "m", Path("/p/m")), ("pass", "m", "ctx", "strat")]
