"""Coverage adapter — the I/O layer for coverage measurement (tarpaulin / grcov / llvm-cov).

Relocated verbatim from pipeline.py. The bodies are unchanged; the only edits
re-point former module-level free variables (``_log``, ``_cargo_env``, ``_current_cluster``,
``TARPAULIN_BIN``/``GRCOV_BIN``/``BUILD_TIMEOUT``/``COVERAGE_IGNORE_PRAGMA``) to a
:class:`CoverageDeps` injected by the composition root. This keeps the dependency rule
one-directional: the adapter imports only stdlib + ports-shaped collaborators, never
``pipeline.py``.

``cluster`` is a *callable* returning the currently-active build cluster (or None) because
the active cluster is reassigned across a run — resolving it at call time preserves the
original ``_current_cluster`` global-lookup semantics. The cluster object is duck-typed
(``.is_default``, ``.tarpaulin_exclude_files``, ``.name``); no application type is imported.

All providers return files_data in the tarpaulin-compatible schema so downstream
(pragma pass, uncovered-line extraction) is unchanged:
  {"abs_path": str, "path": list[str], "traces": [{"line": N, "stats": {"Line": N}}],
   "covered": int, "coverable": int}
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..domain.coverage import CoverageMeasurement, PerFileCoverage
from ..domain.enums import CoverageBackend
from ..domain.workspace_layout import COVERAGE_SCRATCH_DIR


class ClusterView(Protocol):
    """The structural slice of the active build cluster the coverage I/O reads."""

    is_default: bool
    tarpaulin_exclude_files: list[str]
    name: str


@dataclass(frozen=True, slots=True)
class CoverageDeps:
    """Collaborators the coverage I/O needs, injected by the composition root."""

    log: Callable[[str], None]
    cargo_env: Callable[[], dict[str, str]]
    cluster: Callable[[], ClusterView | None]
    tarpaulin_bin: Path
    grcov_bin: Path
    build_timeout: int
    ignore_pragma: str


def glob_to_filename_regex(pat: str) -> str:
    """Convert a simple glob (*/foo.rs) to a regex fragment for --ignore-filename-regex."""
    return re.escape(os.path.basename(pat.lstrip("*/")))


def coverage_output(project_dir: Path, name: str) -> Path:
    """A coverage tool's output path in the workspace's scratch dir, deleted
    first. The output belongs to this workspace (a `/tmp/<tool>_<module>` path
    was shared with every other process on the machine, the test suite
    included). Deleting it before the tool runs is what lets "the file exists"
    mean "this run wrote it": a run that fails without writing used to leave
    the PREVIOUS measurement in place, and the reader parsed it as fresh."""
    out = project_dir / COVERAGE_SCRATCH_DIR / name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    return out


def cluster_exclude_patterns(deps: CoverageDeps) -> list[str]:
    cluster = deps.cluster()
    if cluster is None or cluster.is_default:
        return ["*/nightly.rs"]
    return list(cluster.tarpaulin_exclude_files)


def run_tarpaulin(deps: CoverageDeps, project_dir: Path, module_name: str) -> CoverageMeasurement:
    """
    Run cargo tarpaulin for one module.
    Returns (coverage_pct 0-100, files_list, covered_lines, coverable_lines).
    Returns (0.0, [], 0, 0) on any failure.
    """
    json_path = coverage_output(project_dir, f"tarp_{module_name}/tarpaulin-report.json")
    out_dir = json_path.parent
    # Exclude files based on current cluster:
    # - stable/default: exclude nightly.rs (cfg-excluded, not in coverage denominator)
    # - non-default cluster: use cluster's own exclusion list (may be empty to include nightly.rs)
    cluster = deps.cluster()
    if cluster is None or cluster.is_default:
        _exclude_patterns = ["*/nightly.rs"]
    else:
        _exclude_patterns = list(cluster.tarpaulin_exclude_files)
    cmd = [
        str(deps.tarpaulin_bin), "tarpaulin",
        "-p", module_name,
        "--tests",         # run integration tests (tests/ dir) but exclude from coverage denominator
        "--no-fail-fast",  # produce report even if some tests fail at runtime
        "--out", "Json",
        "--output-dir", str(out_dir),
        "--skip-clean",
        "--exclude-files", "*/tests/*",   # measure src/ coverage only
        "--exclude-files", "build.rs",    # build scripts run at compile time, not under test
    ]
    for pat in _exclude_patterns:
        cmd += ["--exclude-files", pat]
    try:
        result = subprocess.run(
            cmd, cwd=project_dir, capture_output=True, text=True,
            timeout=deps.build_timeout, env=deps.cargo_env(),
        )
        if not json_path.exists():
            deps.log(f"  tarpaulin: no JSON report produced. stderr: {result.stderr[:300]}")
            return CoverageMeasurement(0.0, [], 0, 0)
        data = json.loads(json_path.read_text())
        return CoverageMeasurement(
            data.get("coverage", 0.0),
            [PerFileCoverage.from_dict(d) for d in data.get("files", [])],
            data.get("covered", 0),
            data.get("coverable", 0),
        )
    except subprocess.TimeoutExpired:
        deps.log(f"  tarpaulin: timeout after {deps.build_timeout}s")
        return CoverageMeasurement(0.0, [], 0, 0)
    except Exception as e:
        deps.log(f"  tarpaulin: error: {e}")
        return CoverageMeasurement(0.0, [], 0, 0)


def _grcov_cmd(deps: CoverageDeps, profraw_dir: Path, project_dir: Path,
               out_json: Path) -> list[str]:
    """The grcov invocation both grcov readings share, so a quick reading is
    the same measurement as a full one (same files, same exclusions). The
    quick one used to skip the cluster exclusions and `*/build.rs`, which
    did not matter while it was read for one file and does once its whole
    map is the reading."""
    cmd = [
        str(deps.grcov_bin),
        str(profraw_dir),
        "--binary-path", str(project_dir / "target" / "debug"),
        "--source-dir", str(project_dir),
        "--output-type", "coveralls",
        "--token", "unused",   # required by grcov 0.10+ for coveralls output
        "--branch",
        "--ignore-not-existing",
        "--output-path", str(out_json),
        # Exclude dependency and toolchain sources — only measure project code.
        "--ignore", str(Path.home() / ".cargo" / "registry" / "*"),
        "--ignore", str(Path.home() / ".cargo" / "git" / "*"),
        "--ignore", str(Path.home() / ".rustup" / "*"),
        "--ignore", "*/target/*",
        "--ignore", "*/build.rs",  # build scripts run at compile time, not under test
        "--ignore", "build.rs",    # root-level build.rs (no parent dir prefix)
        "--ignore", "*/tests/*",   # integration test files — measure src/ coverage only
        "--ignore", "tests/*",     # relative-path variant (grcov * doesn't cross /)
        "--ignore", "*.gen_tests_*.rs",  # pipeline-generated test siblings
        "--excl-start", "GRCOV_EXCL_START",
        "--excl-stop",  "GRCOV_EXCL_STOP",
        "--excl-line",  "GRCOV_EXCL_LINE",
    ]
    for pat in cluster_exclude_patterns(deps):
        cmd += ["--ignore", pat]
    return cmd


def _parse_coveralls(out_json: Path, project_dir: Path) -> CoverageMeasurement:
    """grcov's coveralls JSON -> a measurement in the tarpaulin-compatible
    schema, so all downstream code (pragma pass, uncovered-line extraction)
    works unchanged. ``coverage[i]`` is null (not coverable), 0 (uncovered)
    or the hit count of line i+1."""
    data = json.loads(out_json.read_text())
    files_data: list[PerFileCoverage] = []
    for sf in data.get("source_files", []):
        cov_arr = sf.get("coverage", [])
        abs_path = str(project_dir / sf.get("name", ""))
        files_data.append(PerFileCoverage(
            abs_path=abs_path,
            path=abs_path.split(os.sep),  # tarpaulin-compat schema
            traces=[{"line": i + 1, "stats": {"Line": c}}
                    for i, c in enumerate(cov_arr) if c is not None],
            covered=sum(1 for c in cov_arr if c is not None and c > 0),
            coverable=sum(1 for c in cov_arr if c is not None),
        ))
    total_covered = sum(f.covered for f in files_data)
    total_coverable = sum(f.coverable for f in files_data)
    pct = (total_covered / total_coverable * 100.0) if total_coverable > 0 else 0.0
    return CoverageMeasurement(pct, files_data, total_covered, total_coverable)


def quick_coverage_grcov(deps: CoverageDeps, project_dir: Path,
                         module_name: str) -> CoverageMeasurement | None:
    """
    Fast whole-crate coverage snapshot using grcov + LLVM instrumentation
    (incremental build, no clean). Every file is measured either way, so the
    whole measurement is returned and the caller picks the file it asked
    about. None when grcov is absent or the run produced nothing.
    """
    if not deps.grcov_bin.exists():
        return None

    profraw_dir = project_dir / COVERAGE_SCRATCH_DIR / f"{module_name}_quick"
    profraw_dir.mkdir(parents=True, exist_ok=True)
    for f in profraw_dir.glob("*.profraw"):
        f.unlink(missing_ok=True)

    env = deps.cargo_env()
    # Add -C instrument-coverage; do NOT set CARGO_INCREMENTAL=0 so incremental
    # artifacts from the compile-check step are reused where possible.
    env["RUSTFLAGS"] = (env.get("RUSTFLAGS", "") + " -C instrument-coverage").strip()
    env["LLVM_PROFILE_FILE"] = str(profraw_dir / "%p-%m.profraw")

    try:
        subprocess.run(
            ["cargo", "test", "-p", module_name, "--no-fail-fast", "--", "--test-threads=1"],
            cwd=project_dir, capture_output=True, text=True,
            timeout=deps.build_timeout, env=env,
        )
        if not list(profraw_dir.glob("*.profraw")):
            return None
        out_json = coverage_output(project_dir, f"grcov_quick_{module_name}.json")
        subprocess.run(_grcov_cmd(deps, profraw_dir, project_dir, out_json),
                       cwd=project_dir, capture_output=True, text=True, timeout=60, check=True)
        return _parse_coveralls(out_json, project_dir)
    except Exception:
        return None


def run_grcov(deps: CoverageDeps, project_dir: Path, module_name: str) -> CoverageMeasurement:
    """
    Run grcov (LLVM source-based coverage) for one module.
    Returns (coverage_pct 0-100, files_list compatible with tarpaulin format, covered, coverable).
    Returns (0.0, [], 0, 0) on any failure.

    Requires:
      - rustup component add llvm-tools-preview
      - cargo install grcov
      - COVERAGE_BACKEND=grcov in .env
    """
    profraw_dir = project_dir / COVERAGE_SCRATCH_DIR / module_name
    profraw_dir.mkdir(parents=True, exist_ok=True)
    # Clean stale profraw files so old runs don't pollute results.
    for f in profraw_dir.glob("*.profraw"):
        f.unlink(missing_ok=True)

    env = deps.cargo_env()
    env["RUSTFLAGS"] = (env.get("RUSTFLAGS", "") + " -C instrument-coverage").strip()
    env["LLVM_PROFILE_FILE"] = str(profraw_dir / "%p-%m.profraw")
    # Required for correct coverage: incremental compilation can reuse object files
    # compiled without -C instrument-coverage, causing integration test binaries to
    # produce no profraw data.
    env["CARGO_INCREMENTAL"] = "0"

    try:
        # Step 1: run tests with instrumentation.
        result = subprocess.run(
            ["cargo", "test", "-p", module_name, "--no-fail-fast", "--", "--test-threads=1", "--no-capture"],
            cwd=project_dir, capture_output=True, text=True,
            timeout=deps.build_timeout, env=env,
        )
        if result.returncode != 0 and not list(profraw_dir.glob("*.profraw")):
            deps.log(f"  grcov: cargo test failed and produced no profraw: {result.stderr[:200]}")
            return CoverageMeasurement(0.0, [], 0, 0)

        # Step 2: run grcov to produce coveralls JSON.
        out_json = coverage_output(project_dir, f"grcov_{module_name}.json")
        subprocess.run(_grcov_cmd(deps, profraw_dir, project_dir, out_json),
                       cwd=project_dir, capture_output=True, text=True,
                       timeout=120, check=True)
        return _parse_coveralls(out_json, project_dir)

    except subprocess.TimeoutExpired:
        deps.log(f"  grcov: timeout after {deps.build_timeout}s")
        return CoverageMeasurement(0.0, [], 0, 0)
    except Exception as e:
        deps.log(f"  grcov: error: {e}")
        return CoverageMeasurement(0.0, [], 0, 0)


def load_grcov_json(project_dir: Path, module_name: str) -> CoverageMeasurement | None:
    """The workspace's last FULL grcov measurement, without re-running grcov.

    None if there is none (or it holds no file). Used on resume to restore
    coverage state without an expensive re-run. Quick readings are not merged
    in: the llvm-cov provider restores its last full lcov the same way, and the
    per-file cache that used to carry quick values here was a `/tmp` file any
    process could write (a test's `microcrate` row once stood in a live run's
    overview)."""
    out_json = project_dir / COVERAGE_SCRATCH_DIR / f"grcov_{module_name}.json"
    if not out_json.exists():
        return None
    try:
        measured = _parse_coveralls(out_json, project_dir)
    except Exception:
        return None
    return measured if measured.files else None


# ── Coverage provider interface ───────────────────────────────────────────────

class CoverageProvider(ABC):
    """Pluggable coverage measurement backend.

    All implementations return files_data entries in the tarpaulin-compatible schema:
      {"abs_path": str, "path": list[str], "traces": [{"line": N, "stats": {"Line": N}}],
       "covered": int, "coverable": int}
    """

    def __init__(self, deps: CoverageDeps) -> None:
        self._deps = deps
        self.ignore_pragma: str = deps.ignore_pragma

    @abstractmethod
    def measure(self, project_dir: Path, module_name: str) -> CoverageMeasurement:
        """Run instrumented tests. Returns (pct, files_data, covered, coverable)."""

    @abstractmethod
    def measure_quick(self, project_dir: Path, module_name: str) -> CoverageMeasurement | None:
        """Fast whole-crate snapshot (no clean rebuild). None when the backend
        has no quick path. Whole, not one file: every backend measures every
        file anyway, and a test written for one file can move another."""

    @abstractmethod
    def load_cached(self, project_dir: Path, module_name: str) -> CoverageMeasurement | None:
        """Load previous measurement without re-running. Returns None if unavailable."""


class TarpaulinProvider(CoverageProvider):
    def measure(self, project_dir: Path, module_name: str) -> CoverageMeasurement:
        return run_tarpaulin(self._deps, project_dir, module_name)

    def measure_quick(self, project_dir: Path, module_name: str) -> CoverageMeasurement | None:
        return None  # tarpaulin is too slow for quick checks

    def load_cached(self, project_dir: Path, module_name: str) -> CoverageMeasurement | None:
        return None


class GrcovProvider(CoverageProvider):
    def measure(self, project_dir: Path, module_name: str) -> CoverageMeasurement:
        if not self._deps.grcov_bin.exists():
            self._deps.log("  [warn] COVERAGE_BACKEND=grcov but grcov not found — falling back to tarpaulin")
            return run_tarpaulin(self._deps, project_dir, module_name)
        return run_grcov(self._deps, project_dir, module_name)

    def measure_quick(self, project_dir: Path, module_name: str) -> CoverageMeasurement | None:
        return quick_coverage_grcov(self._deps, project_dir, module_name)

    def load_cached(self, project_dir: Path, module_name: str) -> CoverageMeasurement | None:
        return load_grcov_json(project_dir, module_name)


class LlvmCovProvider(CoverageProvider):
    """Coverage backend using cargo-llvm-cov (lcov output, src-based coverage)."""

    @staticmethod
    def lcov_path(project_dir: Path, module_name: str, suffix: str = "") -> Path:
        """Where a measurement's lcov lands: the workspace's coverage scratch."""
        return project_dir / COVERAGE_SCRATCH_DIR / f"llvmcov_{module_name}{suffix}.lcov"

    def _run(self, project_dir: Path, module_name: str, suffix: str = "") -> CoverageMeasurement:
        lcov_path = coverage_output(project_dir, f"llvmcov_{module_name}{suffix}.lcov")
        env = self._deps.cargo_env()
        # llvm-cov uses LLVM's regex (POSIX-ERE-ish): no `\d`, use `[0-9]` instead.
        ignore_patterns = [
            r"tests[/\\]",
            r"build\.rs",
            r"\.gen_tests_[0-9]+\.rs$",   # pipeline-generated test siblings
        ]
        ignore_patterns += [glob_to_filename_regex(p) for p in cluster_exclude_patterns(self._deps)]
        cmd = [
            "cargo", "llvm-cov",
            "--lcov",
            "--output-path", str(lcov_path),
            "-p", module_name,
            # Keep generating the coverage report even if some individual tests
            # fail. Without this, a single failing test (e.g. a buggy gen_tests
            # block) zeroes out the entire measurement. cargo-llvm-cov rejects
            # --no-fail-fast and --ignore-run-fail together; --ignore-run-fail
            # already covers the "report even on failure" case we need here.
            "--ignore-run-fail",
            "--ignore-filename-regex", "|".join(ignore_patterns),
        ]
        cmd += ["--", "--test-threads=1", "--no-capture"]
        try:
            result = subprocess.run(
                cmd, cwd=project_dir, capture_output=True, text=True,
                timeout=self._deps.build_timeout, env=env,
            )
            if result.returncode != 0 and not lcov_path.exists():
                # Capture more of stderr (the relevant error usually appears in
                # the LAST kilobyte after pages of "Compiling …" messages).
                _err_tail = result.stderr[-1500:] if len(result.stderr) > 1500 else result.stderr
                self._deps.log(f"  llvm-cov: failed (exit={result.returncode}):\n{_err_tail}")
                return CoverageMeasurement(0.0, [], 0, 0)
        except subprocess.TimeoutExpired:
            self._deps.log(f"  llvm-cov: timeout after {self._deps.build_timeout}s")
            return CoverageMeasurement(0.0, [], 0, 0)
        except Exception as e:
            self._deps.log(f"  llvm-cov: error: {e}")
            return CoverageMeasurement(0.0, [], 0, 0)
        return self._parse_lcov(lcov_path, project_dir)

    def _parse_lcov(self, lcov_path: Path, project_dir: Path) -> CoverageMeasurement:
        ignore_pragma = self._deps.ignore_pragma
        try:
            text = lcov_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return CoverageMeasurement(0.0, [], 0, 0)
        files_data: list[PerFileCoverage] = []
        current_file: str | None = None
        line_hits: dict[int, int] = {}
        # `FN:<line>,<name>` / `FNDA:<count>,<name>` — the function records the
        # line reading used to drop. One record per function per BINARY (the
        # lib's copy and each test binary's copy carry different crate hashes
        # in the mangled name, measured on an invented crate 2026-09-17) and
        # one per generic instantiation, so the map keeps the max per
        # `(file, first line)`, the rule `domain.fn_execution.executed_by_key`
        # states. Names follow the FIRST comma; the ignore pragma is not
        # applied here — a pragma is a line-level classification, and no
        # classification removes a function from the execution criterion.
        fn_lines: dict[str, int] = {}
        fn_hits: dict[str, int] = {}
        functions: dict[tuple[str, int], int] = {}
        project_str = str(project_dir)
        for raw in text.splitlines():
            if raw.startswith("SF:"):
                current_file = raw[3:]
                line_hits = {}
                fn_lines = {}
                fn_hits = {}
            elif raw.startswith("FN:") and current_file is not None:
                ln_s, _, name = raw[3:].partition(",")
                if ln_s.strip().isdigit():
                    fn_lines[name] = int(ln_s)
            elif raw.startswith("FNDA:") and current_file is not None:
                ct_s, _, name = raw[5:].partition(",")
                if ct_s.strip().isdigit():
                    fn_hits[name] = max(fn_hits.get(name, 0), int(ct_s))
            elif raw.startswith("DA:") and current_file is not None:
                parts = raw[3:].split(",")
                if len(parts) >= 2:
                    try:
                        ln = int(parts[0])
                        ct = int(parts[1])
                        line_hits[ln] = max(line_hits.get(ln, 0), ct)
                    except ValueError:
                        pass
            elif raw == "end_of_record" and current_file is not None:
                abs_path = current_file if os.path.isabs(current_file) else str(project_dir / current_file)
                rel = os.path.relpath(abs_path, project_str)
                if rel.startswith("src" + os.sep) or rel.startswith("src/"):
                    rel_key = rel.replace(os.sep, "/")
                    for name, fn_line in fn_lines.items():
                        key = (rel_key, fn_line)
                        functions[key] = max(functions.get(key, 0),
                                             fn_hits.get(name, 0))
                    if ignore_pragma:
                        try:
                            src_lines = Path(abs_path).read_text(encoding="utf-8", errors="replace").splitlines()
                            pragma_lnos = {i + 1 for i, ln in enumerate(src_lines) if ignore_pragma in ln}
                            if pragma_lnos:
                                line_hits = {ln: ct for ln, ct in line_hits.items() if ln not in pragma_lnos}
                        except Exception:
                            pass
                    covered   = sum(1 for c in line_hits.values() if c > 0)
                    coverable = len(line_hits)
                    traces    = [{"line": ln, "stats": {"Line": ct}} for ln, ct in sorted(line_hits.items())]
                    files_data.append(PerFileCoverage(
                        abs_path=abs_path,
                        path=abs_path.split(os.sep),
                        traces=traces,
                        covered=covered,
                        coverable=coverable,
                    ))
                current_file = None
                line_hits = {}
                fn_lines = {}
                fn_hits = {}
        total_covered   = sum(f.covered   for f in files_data)
        total_coverable = sum(f.coverable for f in files_data)
        pct = (total_covered / total_coverable * 100.0) if total_coverable > 0 else 0.0
        # No FN record in any src/ file is a report WITHOUT function records
        # (None), not a report that every function counted 0.
        return CoverageMeasurement(pct, files_data, total_covered, total_coverable,
                                   functions or None)

    def measure(self, project_dir: Path, module_name: str) -> CoverageMeasurement:
        return self._run(project_dir, module_name)

    def measure_quick(self, project_dir: Path, module_name: str) -> CoverageMeasurement | None:
        # The same `cargo llvm-cov` as `measure`, into its own lcov so the
        # last FULL measurement stays the one `load_cached` restores.
        measured = self._run(project_dir, module_name, suffix="_quick")
        return measured if measured.files else None

    def load_cached(self, project_dir: Path, module_name: str) -> CoverageMeasurement | None:
        lcov_path = self.lcov_path(project_dir, module_name)
        if not lcov_path.exists():
            return None
        try:
            return self._parse_lcov(lcov_path, project_dir)
        except Exception:
            return None


def make_coverage_provider(backend: CoverageBackend, deps: CoverageDeps) -> CoverageProvider:
    """Select a provider for the configured coverage backend (domain enum, by identity)."""
    if backend is CoverageBackend.GRCOV:
        return GrcovProvider(deps)
    if backend is CoverageBackend.LLVM_COV:
        return LlvmCovProvider(deps)
    return TarpaulinProvider(deps)
