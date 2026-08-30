"""Instrumented coverage of a filled cluster tree, in a scratch target dir.

`RUSTFLAGS=-C instrument-coverage` invalidates every artefact in a target
dir it touches. A naive in-place run would force the loop to rebuild its
whole suite on the next sweep. This adapter stages a copy and builds with
its own `CARGO_TARGET_DIR`, the same isolation `scripts/coverage_owner_probe.py`
uses as a diagnostic CLI.

Returns None on any failure (missing llvm tools, a tree that does not
compile, an empty export). A crashing binary writes no profile, so a
crash-only suite under-reports; the crash localizer stays the signal
there. This is a re-fill-phase complement, not a replacement.
"""
from __future__ import annotations

import collections
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from pipeline_core.domain.coverage_slice import (
    CountedBody,
    CoverageSlice,
    slice_from_counts,
)
from pipeline_core.domain.models import FnLocator

_FN_DECL = re.compile(r"\bfn\s+([A-Za-z_]\w*)")


def _run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None,
         ) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, env=env, text=True,
                          capture_output=True, check=False)


def _stage(project: Path, cluster: str, work: Path) -> Path:
    filled = project / ".skeleton-filled" / cluster
    dst = work / "tree"
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copytree(filled / "src", dst / "src", dirs_exist_ok=True)
    for name in ("Cargo.toml", "Cargo.lock"):
        if (filled / name).exists():
            shutil.copy2(filled / name, dst / name)
    tests = project / "tests"
    if tests.exists():
        shutil.copytree(tests, dst / "tests", dirs_exist_ok=True, symlinks=False)
    if (project / "rust-toolchain.toml").exists():
        shutil.copy2(project / "rust-toolchain.toml", dst / "rust-toolchain.toml")
    return dst


def _cov_env(tree: Path, profile: str) -> dict[str, str]:
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = str(tree / "target-cov")
    env["RUSTFLAGS"] = "-C instrument-coverage"
    env["LLVM_PROFILE_FILE"] = profile
    return env


def _test_binaries(tree: Path, cluster: str = "") -> list[Path]:
    """Executables in the scratch deps dir, scoped to this cluster's oracle.

    A body executed only by a nightly test would otherwise count as live when
    ranking the stable cluster. Skipping nightly-named binaries on a
    non-nightly cluster is the cluster's own oracle; the other direction
    (counting a nightly-only body as dead on stable) would demote a unit
    the nightly cluster still needs. When `cluster` names nightly, keep them.
    """
    deps = tree / "target-cov" / "debug" / "deps"
    out: list[Path] = []
    if not deps.is_dir():
        return out
    skip_nightly = "nightly" not in cluster.lower()
    for p in sorted(deps.iterdir()):
        if p.suffix in (".d", ".rlib", ".rmeta", ".so") or not p.is_file():
            continue
        if skip_nightly and "nightly" in p.name.lower():
            continue
        if os.access(p, os.X_OK):
            out.append(p)
    return out


def _merge_export(prof_files: list[Path], objects: list[Path], dest: Path,
                  profdata_bin: str, cov_bin: str) -> dict[str, Any] | None:
    if not prof_files:
        return None
    merged = dest.with_suffix(".profdata")
    r = _run([profdata_bin, "merge", "-sparse", *map(str, prof_files),
              "-o", str(merged)], cwd=dest.parent)
    if r.returncode != 0 or not merged.exists():
        return None
    cmd = [cov_bin, "export", f"--instr-profile={merged}"]
    for o in objects:
        cmd += ["-object", str(o)]
    cmd += ["--format=text", "--skip-expansions"]
    r = _run(cmd, cwd=dest.parent)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        loaded: Any = json.loads(r.stdout)
    except ValueError:
        return None
    if not isinstance(loaded, dict):
        return None
    return loaded


def _body_counts(export: dict[str, Any], src_root: Path,
                 ) -> dict[tuple[str, int], int]:
    marker = f"{src_root.name}{os.sep}src{os.sep}"
    best: dict[tuple[str, int], int] = collections.defaultdict(int)
    for exp in export.get("data", []):
        for fn in exp.get("functions", []):
            files = fn.get("filenames") or []
            if not files or marker not in files[0]:
                continue
            rel = "src/" + files[0].split(marker, 1)[1]
            regions = fn.get("regions") or []
            if not regions:
                continue
            key = (rel, min(r[0] for r in regions))
            best[key] = max(best[key], fn.get("count", 0))
    return dict(best)


def _region_counts(export: dict[str, Any], src_root: Path,
                   ) -> dict[tuple[str, int], int]:
    marker = f"{src_root.name}{os.sep}src{os.sep}"
    best: dict[tuple[str, int], int] = collections.defaultdict(int)
    for exp in export.get("data", []):
        for fn in exp.get("functions", []):
            files = fn.get("filenames") or []
            if not files or marker not in files[0]:
                continue
            rel = "src/" + files[0].split(marker, 1)[1]
            for r in fn.get("regions", []):
                for ln in range(r[0], r[2] + 1):
                    best[(rel, ln)] = max(best[(rel, ln)], r[4])
    return dict(best)


def _fn_at(tree: Path, rel: str, line: int) -> str:
    p = tree / rel
    try:
        src = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "?"
    for i in range(min(line, len(src)) - 1, max(-1, line - 40), -1):
        m = _FN_DECL.search(src[i])
        if m:
            return m.group(1)
    return "?"


def _body_end(tree: Path, rel: str, start: int) -> int:
    """Last source line of the body that starts at `start`, brace-heuristic."""
    try:
        src = (tree / rel).read_text(encoding="utf-8").splitlines()
    except OSError:
        return start
    end = start
    for i in range(start, len(src) + 1):
        if i > len(src):
            break
        stripped = src[i - 1].rstrip()
        if stripped in ("    }", "}"):
            end = i
            break
    return end


def _counted_bodies(
    tree: Path,
    bodies: Mapping[tuple[str, int], int],
    lines: Mapping[tuple[str, int], int],
) -> list[CountedBody]:
    """Name each (file, first-line) body so `slice_from_counts` can collapse."""
    out: list[CountedBody] = []
    for (rel, start), count in bodies.items():
        fn = _fn_at(tree, rel, start)
        if fn == "?":
            continue
        end = _body_end(tree, rel, start)
        instrumented = [
            ln for ln in range(start, end + 1) if (rel, ln) in lines]
        zeros = tuple(
            ln for ln in instrumented if lines[(rel, ln)] == 0)
        out.append(CountedBody(
            locator=FnLocator(rel, fn),
            start=start,
            count=count,
            zero_lines=zeros,
            span_lines=len(instrumented),
        ))
    return out


def measure_coverage_slice(
    project_dir: Path,
    cluster: str,
    *,
    log: Callable[[str], None] | None = None,
) -> CoverageSlice | None:
    """Pass A of the owner probe: whole-suite profile on a staged copy.

    Returns a slice of dead locators and dead-region sentences, or None
    when the reading cannot be taken. Never writes into the loop's
    target dir.
    """
    profdata_bin = os.environ.get("LLVM_PROFDATA", "llvm-profdata")
    cov_bin = os.environ.get("LLVM_COV", "llvm-cov")
    if shutil.which(profdata_bin) is None or shutil.which(cov_bin) is None:
        if log:
            log("  [coverage] llvm-profdata/llvm-cov not on PATH — "
                "dead-body demotion skipped")
        return None
    filled = project_dir / ".skeleton-filled" / cluster / "src"
    if not filled.is_dir():
        return None
    work = Path(tempfile.mkdtemp(prefix="coverage_slice_"))
    try:
        tree = _stage(project_dir, cluster, work)
        prof = work / "prof"
        prof.mkdir()
        env = _cov_env(tree, str(prof / "build-%p.profraw"))
        built = _run(["cargo", "test", "--no-run", "--offline"], cwd=tree,
                     env=env)
        if built.returncode != 0:
            if log:
                log("  [coverage] staged tree does not build under "
                    "instrumentation — dead-body demotion skipped")
            return None
        bins = _test_binaries(tree, cluster)
        if not bins:
            return None
        run_a = prof / "a"
        run_a.mkdir()
        for b in bins:
            e = dict(env)
            e["LLVM_PROFILE_FILE"] = str(run_a / "%p-%m.profraw")
            _run([str(b), "--test-threads=1"], cwd=tree, env=e)
        export = _merge_export(sorted(run_a.glob("*.profraw")), bins,
                               prof / "all.json", profdata_bin, cov_bin)
        if export is None:
            if log:
                log("  [coverage] no whole-suite export — "
                    "dead-body demotion skipped")
            return None
        bodies = _body_counts(export, tree)
        lines = _region_counts(export, tree)
        slice_ = slice_from_counts(_counted_bodies(tree, bodies, lines))
        if log:
            log(f"  [coverage] {len(slice_.dead)} dead body(ies), "
                f"{len(slice_.dead_regions)} body(ies) with unreachable lines "
                f"(staged, own target dir)")
        return slice_
    except (OSError, subprocess.SubprocessError) as exc:
        if log:
            log(f"  [coverage] probe failed ({type(exc).__name__}) — "
                "dead-body demotion skipped")
        return None
    finally:
        shutil.rmtree(work, ignore_errors=True)
