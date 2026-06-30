"""CargoAugmentEngine — the TASK 0c cluster coverage-augmentation work (P6).

Satisfies :class:`pipeline_core.ports.augment.AugmentEngine`. Bundles the four
engine operations the `ClusterAugmentor` use-case drives: the per-cluster ceiling
estimate (cfg-excluded source lines), the base-library pre-check (compile under
the cluster env, install nightly on E0554), the current test-file set, and the
per-module augment driver. Collaborators are constructor-injected — the cargo env
builder, logger, timeout, the `setup.sh` path, the test-file collector, and the
`augment_module` driver (which the composition root binds to the Task0c deps web).
Relocated verbatim from the pipeline.py free fns + the `_AugmentAdapter` binding."""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..domain.cfg import CfgSet
from ..domain.models import BuildCluster
from ..ports.augment import TestAugmentor


class StaticRustTestAugmentor(TestAugmentor):
    """Rust coverage augmentor using cargo + cargo-tarpaulin. The cargo env
    builder, build timeout, and coverage measurer are constructor-injected
    (composition root binds them); `is_applicable` is pure."""

    def __init__(
        self, *,
        cargo_env: Callable[[], dict[str, str]],
        build_timeout: int,
        measure_coverage: Callable[[Path, str], tuple[float, list[Any], int, int]],
    ) -> None:
        self._cargo_env = cargo_env
        self._build_timeout = build_timeout
        self._measure_coverage = measure_coverage

    def lib_compiles(self, project_dir: Path, module_name: str) -> tuple[bool, str]:
        result = subprocess.run(
            shlex.split(f"cargo build -p {module_name} --lib"),
            cwd=project_dir, capture_output=True, text=True,
            timeout=self._build_timeout, env=self._cargo_env(),
        )
        return result.returncode == 0, result.stdout + result.stderr

    def test_harness_compiles(self, project_dir: Path, module_name: str) -> tuple[bool, str]:
        result = subprocess.run(
            shlex.split(f"cargo test -p {module_name} --no-run"),
            cwd=project_dir, capture_output=True, text=True,
            timeout=self._build_timeout, env=self._cargo_env(),
        )
        return result.returncode == 0, result.stdout + result.stderr

    def measure_coverage(
        self, project_dir: Path, module_name: str, module_dir: Path,
    ) -> tuple[float, list[Any], int, int]:
        return self._measure_coverage(project_dir, module_name)

    def is_applicable(self, crate_type: str) -> bool:
        return "proc-macro" not in crate_type.lower()


class CargoAugmentEngine:
    def __init__(
        self, *,
        log: Callable[[str], None],
        cargo_env: Callable[[], dict[str, str]],
        build_timeout: int,
        default_setup: Path,
        collect_test_files: Callable[[Path], list[tuple[str, str]]],
        augment_module: Callable[..., tuple[float, int, bool, str]],
    ) -> None:
        self._log = log
        self._cargo_env = cargo_env
        self._build_timeout = build_timeout
        self._default_setup = default_setup
        self._collect_test_files = collect_test_files
        self._augment_module = augment_module

    def estimate_ceiling(self, project_dir: Path, cluster: BuildCluster) -> tuple[float, int, int]:
        """Max coverage % achievable in this cluster, using source line count as a
        proxy for grcov region count (approximate but directionally correct)."""
        if not cluster.excluded_cfg:
            return 100.0, 0, 0
        total = 0
        excluded = 0
        src_dir = project_dir / "src"
        if not src_dir.exists():
            return 100.0, 0, 0
        for rs in sorted(src_dir.rglob("*.rs")):
            try:
                text = rs.read_text(encoding="utf-8")
            except OSError:
                continue
            lines = text.splitlines()
            n = len(lines)
            # #20: one CfgSet evaluates BOTH sides (excluded-X and active-not(X)) so the
            # estimate matches the runtime filter (task0c subtracts both).
            ctx = CfgSet.of(cfg_true=cluster.cfg_true, cfg_false=cluster.cfg_false)
            excl = ctx.excluded_uncovered_lines(text, set(range(1, n + 1)))
            total += n
            excluded += len(excl)
        if total == 0:
            return 100.0, 0, 0
        ceiling = (1.0 - excluded / total) * 100.0
        return ceiling, excluded, total

    def pre_check(self, project_dir: Path, module_name: str, cluster: BuildCluster) -> bool:
        """Verify the base library (no gen_tests) compiles under the cluster env. On
        E0554 (nightly feature on stable) install nightly via setup.sh and retry;
        return False only when the cluster is genuinely unbuildable."""
        result = subprocess.run(
            ["cargo", "build", "-p", module_name],
            cwd=project_dir, capture_output=True, text=True,
            timeout=self._build_timeout, env=self._cargo_env(),
        )
        if result.returncode == 0:
            return True

        err = result.stderr + result.stdout
        if "E0554" not in err and "may not be used on the stable release channel" not in err:
            return True   # some other compile error — let the normal flow handle it

        self._log(f"  cluster/{cluster.name}: E0554 detected — nightly toolchain required.")
        setup = project_dir.parent.parent / "setup.sh"   # determ/setup.sh
        if not setup.exists():
            setup = self._default_setup
        if setup.exists():
            self._log(f"  cluster/{cluster.name}: running setup.sh rust nightly ...")
            r = subprocess.run(
                ["bash", str(setup), "rust", "nightly"],
                capture_output=False, text=True, timeout=600,
            )
            if r.returncode != 0:
                self._log(f"  cluster/{cluster.name}: setup.sh rust nightly failed — skipping cluster.")
                return False
        else:
            self._log(f"  cluster/{cluster.name}: setup.sh not found — cannot install nightly. Skipping cluster.")
            return False

        cluster.requires_nightly = True   # _cargo_env now sets RUSTUP_TOOLCHAIN=nightly
        result2 = subprocess.run(
            ["cargo", "build", "-p", module_name],
            cwd=project_dir, capture_output=True, text=True,
            timeout=self._build_timeout, env=self._cargo_env(),
        )
        if result2.returncode != 0:
            self._log(f"  cluster/{cluster.name}: still broken after nightly install — skipping cluster.")
            self._log(f"    {(result2.stderr + result2.stdout)[:300]}")
            return False
        self._log(f"  cluster/{cluster.name}: nightly toolchain ready.")
        return True

    def collect_test_rels(self, project_dir: Path) -> set[str]:
        return {rel for rel, _ in self._collect_test_files(project_dir)}

    def augment_module(self, project_dir: Path, module_name: str, module_dir: Path,
                       crate_type: str, *, existing_test_files: set[str],
                       project_context: str, coverage_strategy: str) -> tuple[float, int, bool, str]:
        return self._augment_module(
            project_dir, module_name, module_dir, crate_type,
            existing_test_files=existing_test_files,
            project_context=project_context, coverage_strategy=coverage_strategy)
