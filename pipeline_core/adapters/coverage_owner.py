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
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from pipeline_core.domain.coverage_slice import (
    CountedBody,
    CoverageSlice,
    slice_from_counts,
)
from pipeline_core.domain.models import FnLocator

_FN_DECL = re.compile(r"\bfn\s+([A-Za-z_]\w*)")
_PROF_STEM = re.compile(r"[^A-Za-z0-9_.-]+")


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


_INSTRUMENT_FLAG = "-C instrument-coverage"


def _cov_env(
    tree: Path,
    profile: str,
    cluster_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Cargo env for the staged coverage tree.

    Isolation is load-bearing: CARGO_TARGET_DIR and LLVM_PROFILE_FILE are
    always the scratch paths. A cluster env that names the loop's target
    dir must not leak instrumentation into it.

    Cluster cargo (cfg flags, RUSTUP_TOOLCHAIN) is inherited when
    `cluster_env` is passed, and instrumentation is APPENDED onto
    RUSTFLAGS the same way apply_cluster_env appends a cluster's flags.
    A replace of RUSTFLAGS dropped `--cfg error_generic_member_access`,
    so cfg-gated tests never matched in the owner map (run
    216855-03b2a21b, 2026-09-02, nightly coverage listed ui only of
    three leftovers). None keeps today's replace, which is the
    negative control the existing test pins.
    """
    env = dict(os.environ)
    if cluster_env:
        env.update(cluster_env)
        flags = env.get("RUSTFLAGS", "").strip()
        if "instrument-coverage" not in flags:
            env["RUSTFLAGS"] = (
                f"{flags} {_INSTRUMENT_FLAG}".strip() if flags
                else _INSTRUMENT_FLAG)
    else:
        env["RUSTFLAGS"] = _INSTRUMENT_FLAG
    env["CARGO_TARGET_DIR"] = str(tree / "target-cov")
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


def _prof_stem(name: str) -> str:
    """A filesystem-safe unique stem for one test's profile artefacts.

    `--exact` filters on the cargo name (`gen_tests_2::t`), not the leaf.
    Two tests that share a leaf would otherwise write the same
    `one-{leaf}.profraw` and the second would clobber the first.
    """
    return _PROF_STEM.sub("_", name).strip("_") or "t"


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


def _owner_map(
    tree: Path, bins: list[Path], prof: Path, env: dict[str, str],
    tests: Iterable[str], profdata_bin: str, cov_bin: str,
) -> dict[str, frozenset[FnLocator]]:
    """Which crate bodies does each named test enter, one profile per test.

    Pass C of `scripts/coverage_owner_probe.py`, promoted into the loop.
    The whole-suite merge cannot answer this: it says a body ran, not
    which test ran it, so it can only convict a body nothing executes.
    A test is profiled alone under `--exact` on whichever binary reports
    `running 1 test` for it; the filter is the cargo name the sweep
    parsed (`gen_tests_2::t`), not the leaf — `--exact t` does not match
    a module-qualified name, and a name no binary claims is left OUT of
    the mapping rather than mapped to the empty set, because unmeasured
    and measured-empty license different answers (`convictors_never_enter`).

    Cost is one binary invocation per test per candidate binary until the
    owning one answers, plus a merge and an export. Measured on the
    2026-08-30 stable tree: sub-second per test against a 6 s
    uninstrumented build of the same suite, which is why this runs on the
    failing set only and never on the whole oracle.
    """
    owners: dict[str, frozenset[FnLocator]] = {}
    for name in dict.fromkeys(tests):
        stem = _prof_stem(name)
        raw = prof / f"one-{stem}.profraw"
        hit: Path | None = None
        for b in bins:
            raw.unlink(missing_ok=True)
            e = dict(env)
            e["LLVM_PROFILE_FILE"] = str(raw)
            out = _run([str(b), "--exact", name, "--test-threads=1"],
                       cwd=tree, env=e)
            if "running 1 test" in (out.stdout or ""):
                hit = b
                break
        if hit is None or not raw.exists():
            continue
        export = _merge_export([raw], [hit], prof / f"one-{stem}.json",
                               profdata_bin, cov_bin)
        if export is None:
            continue
        entered = {
            FnLocator(rel, _fn_at(tree, rel, start))
            for (rel, start), count in _body_counts(export, tree).items()
            if count > 0
        }
        owners[name] = frozenset(
            loc for loc in entered if loc.fn_name != "?")
    return owners


def measure_coverage_slice(
    project_dir: Path,
    cluster: str,
    *,
    failing_tests: Iterable[str] = (),
    log: Callable[[str], None] | None = None,
    cluster_env: Mapping[str, str] | None = None,
) -> CoverageSlice | None:
    """Pass A of the owner probe, plus pass C over `failing_tests`.

    Returns a slice of dead locators, dead-region sentences and the
    per-test owner sets, or None when the reading cannot be taken. Never
    writes into the loop's target dir.

    `failing_tests` is the sweep's failing set. Empty leaves `owners`
    empty, and every consumer of it then declines to refuse anything —
    the reading degrades to what it was before the owner map existed.

    `cluster_env` is the same mapping the sweep's cargo uses. None keeps
    the pre-inheritance replace of RUSTFLAGS (instrumentation only).
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
        env = _cov_env(tree, str(prof / "build-%p.profraw"),
                       cluster_env=cluster_env)
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
        asked = tuple(dict.fromkeys(failing_tests))
        owners = _owner_map(tree, bins, prof, env, asked,
                            profdata_bin, cov_bin)
        slice_ = slice_from_counts(_counted_bodies(tree, bodies, lines),
                                   owners=owners, failing=asked)
        if log:
            log(f"  [coverage] {len(slice_.dead)} dead body(ies), "
                f"{len(slice_.dead_regions)} body(ies) with unreachable lines, "
                f"owner set for {len(owners)}/{len(asked)} failing test(s) "
                "(staged, own target dir)")
        return slice_
    except (OSError, subprocess.SubprocessError) as exc:
        if log:
            log(f"  [coverage] probe failed ({type(exc).__name__}) — "
                "dead-body demotion skipped")
        return None
    finally:
        shutil.rmtree(work, ignore_errors=True)
