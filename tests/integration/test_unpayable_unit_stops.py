"""Integration: a per-fn unit that coverage proves the failing test never
enters stops on its first failed attempt.

Real pieces: an instrumented coverage reading (`measure_coverage_slice`, llvm
tools, the syn extractor), the pipeline's own fill roles for splice, revert,
cargo, the U2 gate and the regression gate (`pipeline._APP.fill_roles`), and
`FillOneFn`. Scripted pieces: the model's body and the retrospective call,
which count what the loop spends.

The class: the U2 gate's coverage arm and sibling-blocked arm only cleared the
U2 signature. When the kept body then reddened a passing sibling, the
regression gate reverted it and the failure path re-prompted; after the stall
bound the retrospective refused the conviction as coverage-unreached and still
re-entered the loop. Run `33522-b27db63d` (2026-09-17) rewrote one such unit
11 times in 41 minutes. The crate is invented: `label` is executed only by a
passing test, `size` is wrong and its failing test never calls `label`, and
the unit dispatched is `label` with that failing test attributed to it (the
shape a behavioural attribution produces), rewritten to a body that breaks
the passing test.

Gated on `INTEGRATION=1` (real cargo, an instrumented build).
"""
from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path
from typing import Any

import pytest

from pipeline_core.adapters.coverage_owner import ProbeWorld

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("INTEGRATION") != "1",
                       reason="real cargo + coverage; set INTEGRATION=1"),
]

_CLUSTER = "main"
_TEST_CMD = "cargo test --offline --no-fail-fast"

_CARGO_TOML = """[package]
name = "gadgets"
version = "0.1.0"
edition = "2021"
"""

_LIB = """pub struct Gadget(pub u8);

impl Gadget {
    pub fn label(&self) -> String {
        format!("g{}", self.0)
    }

    pub fn size(&self) -> u8 {
        self.0 + 1
    }
}
"""

_TESTS = """use gadgets::Gadget;

#[test]
fn test_label() {
    assert_eq!(Gadget(3).label(), "g3");
}

#[test]
fn test_size() {
    assert_eq!(Gadget(3).size(), 6);
}
"""

_REGRESSING_BODY = 'String::from("wrong")'


class _Tel:
    def __init__(self) -> None:
        self.logs: list[str] = []
        self.events: list[str] = []

    def log(self, msg: str) -> None:
        self.logs.append(msg)

    def emit(self, event: str, *, duration_ms: int | None = None,
             data: dict[str, Any] | None = None) -> None:
        self.events.append(event)

    def check_stop(self, context: str) -> None: ...


class _Delegate:
    """The live role, with the model-facing calls scripted and counted."""

    def __init__(self, inner: Any, **overrides: Any) -> None:
        self._inner = inner
        for k, v in overrides.items():
            setattr(self, k, v)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _workspace(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    cluster = project / ".skeleton-filled" / _CLUSTER
    (cluster / "src").mkdir(parents=True)
    (cluster / "Cargo.toml").write_text(_CARGO_TOML)
    (cluster / "src" / "lib.rs").write_text(_LIB)
    (project / "tests").mkdir()
    (project / "tests" / "gadgets.rs").write_text(_TESTS)
    (cluster / "tests").symlink_to(project / "tests")
    (project / ".skeleton" / "_failures").mkdir(parents=True)
    return project


def _structure() -> Any:
    from pipeline_core.adapters.extractor import CliExtractor
    from pipeline_core.adapters.structure import CliStructureReader
    exe = (Path(__file__).resolve().parents[2] / "tools" / "skeleton_extractor"
           / "target" / "release" / "skeleton-extractor")
    assert exe.exists(), f"{exe} missing — bash pre_run.sh builds it"
    return CliStructureReader(CliExtractor(exe))


def _run_unit(project: Path, monkeypatch: pytest.MonkeyPatch
              ) -> tuple[Any, _Tel, dict[str, int], str]:
    import pipeline
    from pipeline_core.adapters.coverage_owner import measure_coverage_slice
    from pipeline_core.application.reconstruct.context import PerFnPassCtx
    from pipeline_core.application.reconstruct.fill_one_fn import (
        FillCaps,
        FillOneFn,
    )
    from pipeline_core.domain.attribution import FailureFrontier
    from pipeline_core.domain.coverage_slice import bind, unreached_by
    from pipeline_core.domain.models import FnLocator, Stub

    cluster = project / ".skeleton-filled" / _CLUSTER
    env = dict(os.environ)
    env.pop("RUSTFLAGS", None)
    env["CARGO_TARGET_DIR"] = str(cluster / "target")
    structure = _structure()

    reading = measure_coverage_slice(
        project, _CLUSTER, failing_tests={"test_size"},
        worlds=(ProbeWorld("", env),),
        enumerate_fns=structure.enumerate_fns, timeout=300, workers=1)
    assert reading is not None, "the coverage reading could not be taken"
    unit = FnLocator("src/lib.rs", "label")
    bind(reading)
    try:
        # Premise: the reading must prove the failing test never enters the
        # unit, or this is not the shape under test.
        assert unreached_by(unit, ["test_size"]), (
            f"coverage no longer proves test_size misses label: {reading.owners}")

        counts = {"asks": 0, "diagnoses": 0}
        roles = pipeline._APP.fill_roles

        def ask(*_a: Any, **_k: Any) -> tuple[str | None, str]:
            counts["asks"] += 1
            return _REGRESSING_BODY, _REGRESSING_BODY

        def diagnose(**_k: Any) -> dict[str, Any]:
            counts["diagnoses"] += 1
            return {"signal": "", "category": "WRONG_TYPE_CHOICE",
                    "guidance": "try again"}

        tel = _Tel()
        fill = FillOneFn(
            tel=tel, mut=roles.mut, cargo=roles.cargo,
            prompt=_Delegate(roles.prompt, ask_per_fn_body=ask),
            retros=_Delegate(roles.retros, diagnose=diagnose,
                             extract_self_note=lambda *a, **k: None),
            tests=roles.tests, stall=roles.stall,
            caps=FillCaps(per_fn_fix_hard_cap=8, per_fn_fix_max_stalls=2,
                          fix_err_cap=4000))
        ctx = PerFnPassCtx(
            project_dir=project, target_src_dir=cluster, module_name="gadgets",
            cluster_name=_CLUSTER, check_cmd="cargo check --offline --tests",
            cluster_env=env, fr_path=None, tests_dir_files=[],
            macros_section="", invariants_section="", crate_type_records=[],
            src_for_macros=cluster / "src",
            failures_dir=project / ".skeleton" / "_failures",
            tdd_frontier_tests=FailureFrontier.of_direct({unit: {"test_size"}}),
            tdd_test_cmd=_TEST_CMD, splice_check_lock=threading.Lock(),
            retros_cache={}, idiom_blocks=[], pass_n=1, total=1,
            cluster_cfg_set=None, still_stubbed=frozenset())
        monkeypatch.setenv("PER_FN_RETRO_ROUNDS_MAX", "3")
        rec = Stub(file="src/lib.rs", fn_name="label", container="impl Gadget",
                   refill=True)
        result = fill.run(1, rec, ctx=ctx)
    finally:
        bind(None)
    return result, tel, counts, (cluster / "src" / "lib.rs").read_text()


def test_unreached_unit_whose_rewrite_regresses_a_sibling_stops(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if shutil.which("llvm-profdata") is None or shutil.which("llvm-cov") is None:
        pytest.fail("llvm-profdata / llvm-cov not on PATH — the coverage "
                    "reading this class rests on cannot be taken")
    project = _workspace(tmp_path)
    result, tel, counts, lib_after = _run_unit(project, monkeypatch)
    assert any("regressed sibling test(s)" in m and "test_label" in m
               for m in tel.logs), (
        "premise: the rewrite must redden the passing sibling\n"
        + "\n".join(tel.logs))
    assert not result.filled
    assert counts["asks"] == 1, "\n".join(tel.logs)
    assert counts["diagnoses"] == 0
    assert "phase_c.fn.unpayable_stop" in tel.events
    assert "phase_c.fn.retro_retry" not in tel.events
    assert 'format!("g{}", self.0)' in lib_after, "the passing body is restored"


def test_negative_control_a_unit_the_test_enters_is_retried(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same run with the failing test attributed to the body it DOES
    execute (`size`) and a rewrite that reddens the sibling: real work, so the
    loop re-prompts."""
    import pipeline_core.domain.coverage_slice as cs
    project = _workspace(tmp_path)
    real_unreached = cs.unreached_by
    # The only change is the coverage answer for the dispatched unit: it now
    # reads as entered, so neither the U2 arm nor the retrospective arm fires.
    monkeypatch.setattr(
        "pipeline_core.application.reconstruct.fill_one_fn.coverage_unreached_by",
        lambda unit, tests: None)
    monkeypatch.setattr(cs, "unreached_by", lambda unit, tests: (
        real_unreached(unit, tests) if unit.fn_name == "label" else None))
    result, tel, counts, _ = _run_unit(project, monkeypatch)
    assert not result.filled
    assert counts["asks"] > 1, "\n".join(tel.logs)
    assert "phase_c.fn.unpayable_stop" not in tel.events
