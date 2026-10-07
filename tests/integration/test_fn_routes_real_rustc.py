"""Integration: the public routes to an unnameable function, over real rustc output.

Real cargo and rustc (stable, `RUSTC_BOOTSTRAP` scoped to the crate), the real
adapter and extractor, the production reader. No model.

Two things are pinned:
- the committed dumps in `tests/fixtures/fn_routes/expected/` still give the
  routes the fresh ones give (a toolchain that changes the MIR shape fails
  here, not in a run);
- a route is a fact: `tests/routes.rs` in the fixture follows each route the
  expected file states, written from the route line alone, and under llvm-cov
  every routed target executes while the two no-route controls do not.

Gated on `INTEGRATION=1`; `scripts/run_integration.sh` is the launcher.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from pipeline_core.adapters.cargo import cluster_cargo_env
from pipeline_core.adapters.extractor import CliExtractor
from pipeline_core.adapters.fn_reachability import measure_world_reachability
from pipeline_core.domain.attribution import record_site
from pipeline_core.domain.fn_execution import site_label
from pipeline_core.domain.fn_reachability import public_surface, record_first_line
from pipeline_core.domain.fn_routes import world_facts
from pipeline_core.domain.models import BuildCluster, FnRecord

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("INTEGRATION") != "1",
                       reason="real cargo + rustc; set INTEGRATION=1"),
]

_REPO = Path(__file__).resolve().parents[2]
_FIX = _REPO / "tests" / "fixtures" / "fn_routes"
_BIN = _REPO / "tools" / "skeleton_extractor" / "target" / "release" / "skeleton-extractor"
_ROUTED = ("src/lib.rs::fmt [impl fmt::Debug for DisplayError<M>]",
           "src/lib.rs::fmt [impl fmt::Debug for ErrorImpl<E>]",
           "src/lib.rs::leaf", "src/lib.rs::in_closure")
_CONTROLS = ("src/lib.rs::fmt [impl fmt::Debug for Unbuilt]", "src/lib.rs::never_called [impl Error]")


def _scratch(tmp_path: Path) -> Path:
    dst = tmp_path / "crate"
    shutil.copytree(_FIX, dst, ignore=shutil.ignore_patterns("target", "expected"))
    return dst


def _extracted(root: Path) -> tuple[list[FnRecord], list]:
    if not _BIN.exists():
        pytest.skip("extractor release binary missing")
    ex = CliExtractor(_BIN)
    recs = [r if isinstance(r, FnRecord) else FnRecord.from_dict(dict(r))
            for r in ex.enumerate_fns(root) or []]
    return ([r for r in recs if r.file.startswith("src/")],
            [t for t in ex.enumerate_types(root) or [] if t.file.startswith("src/")])


def test_the_committed_dumps_still_give_the_routes_the_toolchain_gives(tmp_path: Path) -> None:
    root = _scratch(tmp_path)
    world = measure_world_reachability(root, BuildCluster("stable", {}, [], [], [], ""),
                                       cluster_env=cluster_cargo_env, timeout=600)
    assert world.measured, world.reason
    recs, types = _extracted(root)
    texts = {"src/lib.rs": (root / "src" / "lib.rs").read_text(encoding="utf-8")}
    fresh_surface = public_surface(world.rustdoc)
    committed_surface = public_surface((_FIX / "expected" / "rustdoc.json").read_text(encoding="utf-8"))
    assert fresh_surface is not None and committed_surface is not None
    fresh = world_facts("stable", mir_text=world.mir, surface=fresh_surface,
                        records=recs, types=types, file_texts=texts)
    committed = world_facts("stable", mir_text=(_FIX / "expected" / "mir.txt").read_text(encoding="utf-8"),
                            surface=committed_surface, records=recs, types=types, file_texts=texts)
    assert fresh.routes == committed.routes
    assert fresh.unnameable == committed.unnameable


def test_following_each_route_executes_its_target(tmp_path: Path) -> None:
    if shutil.which("cargo-llvm-cov") is None:
        pytest.skip("cargo-llvm-cov missing")
    root = _scratch(tmp_path)
    lcov = tmp_path / "routes.lcov"
    run = subprocess.run(["cargo", "llvm-cov", "--lcov", "--output-path", str(lcov), "-q"],
                         cwd=root, capture_output=True, text=True, timeout=1200,
                         env={**os.environ, "CARGO_TARGET_DIR": str(tmp_path / "target")})
    assert run.returncode == 0, run.stdout + run.stderr
    names: dict[str, int] = {}
    counts: dict[int, int] = {}
    for line in lcov.read_text(encoding="utf-8").splitlines():
        if line.startswith("FN:"):
            at, name = line[3:].split(",", 1)
            names[name] = int(at)
        elif line.startswith("FNDA:"):
            n, name = line[5:].split(",", 1)
            if name in names:
                counts[names[name]] = counts.get(names[name], 0) + int(n)
    recs, _ = _extracted(root)
    text = (root / "src" / "lib.rs").read_text(encoding="utf-8")
    by_label = {site_label(record_site(r)): record_first_line(r, text) for r in recs}
    for label in _ROUTED:
        assert counts.get(by_label[label], 0) > 0, (label, by_label[label])
    for label in _CONTROLS:
        assert counts.get(by_label[label], 0) == 0, label
