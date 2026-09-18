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
from typing import Any, Protocol

from ..domain.coverage import CoverageMeasurement, PerFileCoverage
from ..domain.enums import CoverageBackend


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
    out_dir = Path("/tmp") / f"tarp_{module_name}"
    out_dir.mkdir(parents=True, exist_ok=True)
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
        json_path = out_dir / "tarpaulin-report.json"
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


def quick_file_coverage_grcov(deps: CoverageDeps, project_dir: Path, module_name: str, rel_path: str) -> tuple[int, int] | None:
    """
    Fast per-file coverage snapshot using grcov + LLVM instrumentation.
    Returns (covered, coverable) for rel_path only, or None on failure.
    """
    if not deps.grcov_bin.exists():
        return None

    profraw_dir = project_dir / "target" / "coverage" / f"{module_name}_quick"
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

        out_json = Path("/tmp") / f"grcov_quick_{module_name}.json"
        cmd = [
            str(deps.grcov_bin),
            str(profraw_dir),
            "--binary-path", str(project_dir / "target" / "debug"),
            "--source-dir", str(project_dir),
            "--output-type", "coveralls",
            "--token", "unused",
            "--branch",
            "--ignore-not-existing",
            "--output-path", str(out_json),
            "--ignore", str(Path.home() / ".cargo" / "registry" / "*"),
            "--ignore", str(Path.home() / ".cargo" / "git" / "*"),
            "--ignore", str(Path.home() / ".rustup" / "*"),
            "--ignore", "*/target/*",
            "--ignore", "*/tests/*",
            "--ignore", "tests/*",
            "--ignore", "build.rs",
            "--ignore", "*.gen_tests_*.rs",   # pipeline-generated test siblings
            "--excl-start", "GRCOV_EXCL_START",
            "--excl-stop",  "GRCOV_EXCL_STOP",
            "--excl-line",  "GRCOV_EXCL_LINE",
        ]
        subprocess.run(cmd, cwd=project_dir, capture_output=True, text=True, timeout=60, check=True)

        data = json.loads(out_json.read_text())
        abs_target = str(project_dir / rel_path)
        for sf in data.get("source_files", []):
            sf_abs = str(project_dir / sf.get("name", ""))
            if sf_abs == abs_target or sf.get("name", "") == rel_path:
                cov_arr = sf.get("coverage", [])
                covered   = sum(1 for c in cov_arr if c is not None and c > 0)
                coverable = sum(1 for c in cov_arr if c is not None)
                return covered, coverable
        return None
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
    profraw_dir = project_dir / "target" / "coverage" / module_name
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

    # Cluster exclude patterns reused from tarpaulin path.
    cluster = deps.cluster()
    if cluster is None or cluster.is_default:
        _exclude_patterns = ["*/nightly.rs"]
    else:
        _exclude_patterns = list(cluster.tarpaulin_exclude_files)

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
        out_json = Path("/tmp") / f"grcov_{module_name}.json"
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
        for pat in _exclude_patterns:
            cmd += ["--ignore", pat]
        subprocess.run(cmd, cwd=project_dir, capture_output=True, text=True,
                       timeout=120, check=True)

        data = json.loads(out_json.read_text())
        source_files = data.get("source_files", [])

        total_covered = total_coverable = 0
        # Build files_data in tarpaulin-compatible format so all downstream
        # code (pragma pass, uncovered-line extraction) works unchanged.
        files_data = []
        for sf in source_files:
            cov_arr = sf.get("coverage", [])
            # null = not coverable, 0 = uncovered, N = hit count
            covered   = sum(1 for c in cov_arr if c is not None and c > 0)
            coverable = sum(1 for c in cov_arr if c is not None)
            total_covered   += covered
            total_coverable += coverable
            # Build traces list matching tarpaulin schema: [{line, stats:{Line:N}}]
            traces = [
                {"line": i + 1, "stats": {"Line": (c if c is not None else 0)}}
                for i, c in enumerate(cov_arr)
                if c is not None
            ]
            abs_path = str(project_dir / sf.get("name", ""))
            files_data.append(PerFileCoverage(
                abs_path=abs_path,
                path=abs_path.split(os.sep),  # tarpaulin-compat schema
                traces=traces,
                covered=covered,
                coverable=coverable,
            ))

        pct = (total_covered / total_coverable * 100.0) if total_coverable > 0 else 0.0
        return CoverageMeasurement(pct, files_data, total_covered, total_coverable)

    except subprocess.TimeoutExpired:
        deps.log(f"  grcov: timeout after {deps.build_timeout}s")
        return CoverageMeasurement(0.0, [], 0, 0)
    except Exception as e:
        deps.log(f"  grcov: error: {e}")
        return CoverageMeasurement(0.0, [], 0, 0)


def load_grcov_json(project_dir: Path, module_name: str) -> CoverageMeasurement | None:
    """Parse the existing grcov JSON + filecache without re-running grcov.

    Returns (pct, files_data, covered, coverable) if the JSON exists, else None.
    Used on resume to restore coverage state without an expensive re-run.
    """
    out_json = Path("/tmp") / f"grcov_{module_name}.json"
    if not out_json.exists():
        return None
    try:
        main_mtime = out_json.stat().st_mtime
        data = json.loads(out_json.read_text())
        source_files = data.get("source_files", [])
        if not source_files:
            return None

        # Build per-file map from main JSON.
        file_map: dict[str, dict[str, Any]] = {}
        for sf in source_files:
            cov_arr = sf.get("coverage", [])
            covered   = sum(1 for c in cov_arr if c is not None and c > 0)
            coverable = sum(1 for c in cov_arr if c is not None)
            abs_path  = str(project_dir / sf.get("name", ""))
            traces    = [
                {"line": i + 1, "stats": {"Line": (c if c is not None else 0)}}
                for i, c in enumerate(cov_arr) if c is not None
            ]
            file_map[sf.get("name", "")] = {
                "path": abs_path.split(os.sep),
                "abs_path": abs_path,
                "traces": traces,
                "covered": covered,
                "coverable": coverable,
            }

        # Merge filecache entries newer than the main JSON (written by quick-cov).
        filecache = Path("/tmp") / f"grcov_filecache_{module_name}.json"
        if filecache.exists():
            try:
                for rel, d in json.loads(filecache.read_text()).items():
                    if d.get("ts", 0) > main_mtime and d.get("coverable", 0) > 0 and rel in file_map:
                        entry = file_map[rel]
                        entry["covered"]   = d["covered"]
                        entry["coverable"] = d["coverable"]
            except Exception:
                pass

        files_data = [PerFileCoverage.from_dict(v) for v in file_map.values()]
        total_covered   = sum(f.covered   for f in files_data)
        total_coverable = sum(f.coverable for f in files_data)
        pct = (total_covered / total_coverable * 100.0) if total_coverable > 0 else 0.0
        return CoverageMeasurement(pct, files_data, total_covered, total_coverable)
    except Exception:
        return None


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
    def measure_file(self, project_dir: Path, module_name: str, rel_path: str) -> tuple[int, int] | None:
        """Quick per-file snapshot. Returns (covered, coverable) or None on failure."""

    @abstractmethod
    def load_cached(self, project_dir: Path, module_name: str) -> CoverageMeasurement | None:
        """Load previous measurement without re-running. Returns None if unavailable."""


class TarpaulinProvider(CoverageProvider):
    def measure(self, project_dir: Path, module_name: str) -> CoverageMeasurement:
        return run_tarpaulin(self._deps, project_dir, module_name)

    def measure_file(self, project_dir: Path, module_name: str, rel_path: str) -> tuple[int, int] | None:
        return None  # tarpaulin is too slow for per-file quick checks

    def load_cached(self, project_dir: Path, module_name: str) -> CoverageMeasurement | None:
        return None


class GrcovProvider(CoverageProvider):
    def measure(self, project_dir: Path, module_name: str) -> CoverageMeasurement:
        if not self._deps.grcov_bin.exists():
            self._deps.log("  [warn] COVERAGE_BACKEND=grcov but grcov not found — falling back to tarpaulin")
            return run_tarpaulin(self._deps, project_dir, module_name)
        return run_grcov(self._deps, project_dir, module_name)

    def measure_file(self, project_dir: Path, module_name: str, rel_path: str) -> tuple[int, int] | None:
        return quick_file_coverage_grcov(self._deps, project_dir, module_name, rel_path)

    def load_cached(self, project_dir: Path, module_name: str) -> CoverageMeasurement | None:
        return load_grcov_json(project_dir, module_name)


class LlvmCovProvider(CoverageProvider):
    """Coverage backend using cargo-llvm-cov (lcov output, src-based coverage)."""

    def _lcov_path(self, module_name: str, suffix: str = "") -> Path:
        return Path("/tmp") / f"llvmcov_{module_name}{suffix}.lcov"

    def _run(self, project_dir: Path, module_name: str, suffix: str = "") -> CoverageMeasurement:
        lcov_path = self._lcov_path(module_name, suffix)
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

    def measure_file(self, project_dir: Path, module_name: str, rel_path: str) -> tuple[int, int] | None:
        files_data = self._run(project_dir, module_name, suffix="_quick")[1]
        abs_target = str(project_dir / rel_path)
        for fd in files_data:
            if fd.abs_path == abs_target:
                return fd.covered, fd.coverable
        return None

    def load_cached(self, project_dir: Path, module_name: str) -> CoverageMeasurement | None:
        lcov_path = self._lcov_path(module_name)
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
