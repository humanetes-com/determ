"""Integration: the unreachable-by-construction proof, over real rustc output.

Real cargo and rustc (stable, `RUSTC_BOOTSTRAP` scoped to the crate), the real
adapter, the real extractor and the production reader. No model.

Three things are pinned:
- the fixture crate's real MIR and rustdoc JSON give the verdicts
  `expected/verdicts.json` states (so the oracle works on what the toolchain
  prints today, not only on the committed text);
- the committed dumps in `expected/` still give the same verdicts as the fresh
  ones (the dump's SHAPE is what a toolchain update would change, and this is
  where that surfaces, not in a run);
- the soundness reading: no function the oracle proves is executed by the
  integration tests of the fixture, `microcrate` or `hardcrate`
  (`scripts/measure_fn_reachability.py --measure-executed`).

Gated on `INTEGRATION=1`; `scripts/run_integration.sh` is the launcher.
"""
from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline_core.adapters.cargo import cluster_cargo_env
from pipeline_core.adapters.extractor import CliExtractor
from pipeline_core.adapters.fn_reachability import measure_world_reachability
from pipeline_core.application.tasks.fn_criterion import FnCriterionInputs, read_verdicts
from pipeline_core.domain.fn_execution import site_label
from pipeline_core.domain.fn_reachability import public_surface, read_world
from pipeline_core.domain.models import BuildCluster, FnRecord

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("INTEGRATION") != "1",
                       reason="real cargo + rustc; set INTEGRATION=1"),
]

_REPO = Path(__file__).resolve().parents[2]
_FIX = _REPO / "tests" / "fixtures" / "fn_reachability"
_BIN = _REPO / "tools" / "skeleton_extractor" / "target" / "release" / "skeleton-extractor"
_EXPECTED = json.loads((_FIX / "expected" / "verdicts.json").read_text(encoding="utf-8"))


def _scratch(tmp_path: Path) -> Path:
    dst = tmp_path / "crate"
    shutil.copytree(_FIX, dst, ignore=shutil.ignore_patterns("target", "expected"))
    return dst


def _inputs() -> FnCriterionInputs:
    if not _BIN.exists():
        pytest.skip("extractor release binary missing")
    ex = CliExtractor(_BIN)
    return FnCriterionInputs(
        enumerate_fns=ex.enumerate_fns, enumerate_mods=ex.enumerate_mods,
        measure_reachability=functools.partial(
            measure_world_reachability, cluster_env=cluster_cargo_env, timeout=600))


def _cluster() -> BuildCluster:
    return BuildCluster("stable", {}, [], [], [], "")


def test_the_real_compiler_output_gives_the_fixtures_verdicts(tmp_path: Path) -> None:
    verdicts, reason = read_verdicts(_scratch(tmp_path), _cluster(), _inputs())
    assert reason == ""
    got = {site_label(s): v for s, v in verdicts.items()}
    assert sorted(k for k, v in got.items() if v.proof) == sorted(_EXPECTED["flagged"])
    for label, why in _EXPECTED["kept"].items():
        assert not got[label].proof and why in got[label].kept, (label, got[label].kept)


def test_the_committed_dumps_still_say_what_the_toolchain_says(tmp_path: Path) -> None:
    """A rustc update that changes the MIR or rustdoc shape fails HERE: the
    committed text and the fresh text must yield one verdict set."""
    root = _scratch(tmp_path)
    world = measure_world_reachability(root, _cluster(), cluster_env=cluster_cargo_env, timeout=600)
    assert world.measured, world.reason
    ex = CliExtractor(_BIN) if _BIN.exists() else pytest.skip("extractor release binary missing")
    recs = [r if isinstance(r, FnRecord) else FnRecord.from_dict(dict(r))
            for r in (ex.enumerate_fns(root) or []) if r.file.startswith("src/")]
    texts = {"src/lib.rs": (root / "src" / "lib.rs").read_text(encoding="utf-8")}
    fresh = read_world(recs, mir_text=world.mir, surface=public_surface(world.rustdoc), file_texts=texts)
    committed = read_world(
        recs, mir_text=(_FIX / "expected" / "mir.txt").read_text(encoding="utf-8"),
        surface=public_surface((_FIX / "expected" / "rustdoc.json").read_text(encoding="utf-8")),
        file_texts=texts)
    assert {s: bool(v.proof) for s, v in fresh.items()} == {s: bool(v.proof) for s, v in committed.items()}


@pytest.mark.parametrize("workspace", [
    "tests/fixtures/fn_reachability", "tests/fixtures/microcrate", "tests/fixtures/hardcrate"])
def test_no_proved_function_is_executed_by_the_integration_tests(workspace: str) -> None:
    if shutil.which("cargo-llvm-cov") is None:
        pytest.skip("cargo-llvm-cov missing")
    run = subprocess.run(
        [sys.executable, str(_REPO / "scripts" / "measure_fn_reachability.py"), workspace,
         "--measure-executed"], capture_output=True, text=True, timeout=1800, cwd=_REPO)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "integration tests (llvm-cov):" in run.stdout and "0 counterexample(s)" in run.stdout
    assert "!!" not in run.stdout
