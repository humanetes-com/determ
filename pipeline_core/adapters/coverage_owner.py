"""Instrumented coverage of a filled cluster tree, in a scratch target dir.

`RUSTFLAGS=-C instrument-coverage` invalidates every artefact in a target
dir it touches. A naive in-place run would force the loop to rebuild its
whole suite on the next sweep. This adapter stages a copy and builds with
its own `CARGO_TARGET_DIR`, the same isolation `scripts/coverage_owner_probe.py`
uses as a diagnostic CLI.

Returns None on any failure (missing llvm tools, a tree that does not
compile, an empty export, no fn records).

A binary killed by a signal flushes no profile. The runtime CREATES the
profraw at process start and writes the counters at exit — measured
2026-09-07 on run `1577437-c93d5c26`'s tree: a fixed-name
`LLVM_PROFILE_FILE` exists 5 ms into a `--exact` run of the SIGSEGV test
and is 0 bytes when the process dies, and `llvm-profdata merge` accepts
that file and exports count 0 for every function. So a file on disk is not
a completed measurement; the process's returncode (negative = killed by a
signal) and a NON-EMPTY profile are. A binary that did not complete is
re-run one test per process so the tests that do complete are measured;
the ones that still die are reported as unmeasured, absent from the owner
map, and their bodies are the caller's to exempt from `dead` on the
recorded evidence (`coverage_slice.exempt_recorded_executors`). The crash
localizer stays the signal for the fault itself.

Bodies are named at the extractor's `FnSite`, resolved by LINE against the
records the caller's enumerator returns for the staged tree — the same
enumerator the fill routes key their records with, so coverage and the
session agree on one identity and one cfg world. The adapter does not
locate the extractor binary itself.
"""
from __future__ import annotations

import collections
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.adapters.process import run_bounded
from pipeline_core.domain.attribution import record_site
from pipeline_core.domain.coverage_slice import (
    CountedBody,
    CoverageSlice,
    matching_test_key,
    slice_from_counts,
)
from pipeline_core.domain.models import FnRecord, FnSite
from pipeline_core.domain.test_results import parse_binary_results

_PROF_STEM = re.compile(r"[^A-Za-z0-9_.-]+")
_BIN_HASH = re.compile(r"-[0-9a-f]{16}$")


class SiteIndex:
    """Which extractor site a coverage line belongs to, per file.

    An llvm-cov export names a function by its FIRST region, and for a Rust
    fn that region starts on the SIGNATURE line at the `fn` keyword's column
    (`[8, 5, 8, 61, …]` for `    fn fmt(` at line 8 of the `dual_fmt`
    fixture, computed 2026-09-03), while the extractor's `sig_byte_start` is
    the byte of that keyword. Resolving by byte containment of the line's
    column-0 offset therefore missed every indented fn — every impl method —
    and the first site resolver emptied the owner map for any test that
    entered only methods, which silently disarmed `unreached_by_test` and
    the dispatcher redirect built on it. Lines are the unit both sides
    agree on: a record spans from the line of `sig_byte_start` to the line
    of `body_byte_end`, and a coverage line resolves to the innermost record
    spanning it (a `fn` nested in a body is the inner one)."""

    def __init__(self, records: Iterable[FnRecord],
                 file_texts: Mapping[str, str]) -> None:
        self._spans: dict[str, list[tuple[int, int, FnSite]]] = {}
        for rec in records:
            text = file_texts.get(rec.file)
            if text is None:
                continue
            first = text.count("\n", 0, rec.sig_byte_start) + 1
            last = text.count("\n", 0, rec.body_byte_end) + 1
            self._spans.setdefault(rec.file, []).append(
                (first, last, record_site(rec)))

    def resolve(self, rel: str, line: int) -> tuple[FnSite, int, int] | None:
        """`(site, first_line, last_line)` of the innermost record spanning
        `line` in `rel`, or None when no crate fn does (a generated test, a
        doc-test shim, a file the enumerator did not see)."""
        best: tuple[int, int, FnSite] | None = None
        for first, last, site in self._spans.get(rel, ()):
            if first <= line <= last and (
                    best is None or last - first < best[1] - best[0]):
                best = (first, last, site)
        return None if best is None else (best[2], best[0], best[1])

    def site_at(self, rel: str, line: int) -> FnSite | None:
        hit = self.resolve(rel, line)
        return None if hit is None else hit[0]


def _fn_records(enumerate_fns: Callable[[Path], Sequence[Any] | None],
                tree: Path) -> list[FnRecord]:
    rows = enumerate_fns(tree) or []
    recs = [FnRecord.from_dict(r) if isinstance(r, dict) else r for r in rows]
    return [r for r in recs if not r.is_test and r.fn_name]


def _run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None,
         *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    """Run ``cmd``. ``timeout`` is set only for a reconstructed test binary;
    llvm-profdata, llvm-cov and ``cargo test --no-run`` omit it (they are
    not this class, and a 120s bound can kill a healthy instrumented
    compile)."""
    if timeout is None:
        return subprocess.run(cmd, cwd=cwd, env=env, text=True,
                              capture_output=True, check=False)
    ran = run_bounded(cmd, cwd=cwd, env=env, timeout=timeout)
    return subprocess.CompletedProcess(
        cmd, ran.returncode, ran.stdout or "", ran.stderr or "")


def _in_flight_test(stdout: str) -> str | None:
    """The libtest name that was running when a timed-out capture was cut.

    Coverage invokes the binary directly, so cargo's ``Running`` labels are
    absent; ``parse_binary_results`` still opens a section on ``running N
    tests`` and puts the dangling in-flight name on it.
    """
    for binary in parse_binary_results(stdout or "", timed_out=True):
        if binary.hung and binary.dangling:
            return binary.dangling
        if not binary.completed and binary.dangling:
            return binary.dangling
    return None


def _suite_timeout() -> int:
    try:
        return int(os.environ.get("PHASE_C_TEST_TIMEOUT", "120"))
    except ValueError:
        return 120


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


def _counted_bodies(
    index: SiteIndex,
    bodies: Mapping[tuple[str, int], int],
    lines: Mapping[tuple[str, int], int],
) -> list[CountedBody]:
    """Name each (file, first-line) body at the extractor site spanning it;
    the record's own last line bounds its instrumented lines."""
    out: list[CountedBody] = []
    for (rel, start), count in bodies.items():
        hit = index.resolve(rel, start)
        if hit is None:
            continue
        site, _first, end = hit
        instrumented = [
            ln for ln in range(start, end + 1) if (rel, ln) in lines]
        zeros = tuple(
            ln for ln in instrumented if lines[(rel, ln)] == 0)
        out.append(CountedBody(
            locator=site,
            start=start,
            count=count,
            zero_lines=zeros,
            span_lines=len(instrumented),
        ))
    return out


def _prof_stem(name: str) -> str:
    """A filesystem-safe unique stem for one test's profile artefacts.

    `--exact` filters on the cargo name (`gen_tests_2::t`), not the leaf.
    Two tests that share a leaf would otherwise write the same
    `one-{leaf}.profraw` and the second would clobber the first.
    """
    return _PROF_STEM.sub("_", name).strip("_") or "t"


def _binary_label(binary: Path) -> str:
    """`error_gen_3-1a679321963fec42` → `error_gen_3`: the cargo target
    name, which is what the sweep's own log names a binary by."""
    return _BIN_HASH.sub("", binary.name)


def _completed(returncode: int, raws: Sequence[Path]) -> bool:
    """A measurement was taken: the process was not killed by a signal
    (POSIX reports that as a negative returncode; a failing test exits 101
    and IS complete) and it left a non-empty profile. The 0-byte file the
    runtime creates at start is not a profile."""
    if returncode < 0:
        return False
    return any(r.exists() and r.stat().st_size > 0 for r in raws)


@dataclass(frozen=True, slots=True)
class ExactRun:
    """One `--exact` process: which binary claimed the test, whether the
    process completed, and the profile it left when it did."""

    name: str
    binary: Path
    returncode: int
    raw: Path
    completed: bool


def _exact_run(tree: Path, binary: Path, name: str, raw: Path,
               env: dict[str, str], *, timeout: float) -> ExactRun | None:
    """Run `name` alone on `binary`; None when the binary does not claim it
    (`running 1 test` is printed before the test starts, so a claim is
    readable even when the process then dies)."""
    raw.unlink(missing_ok=True)
    e = dict(env)
    e["LLVM_PROFILE_FILE"] = str(raw)
    out = _run([str(binary), "--exact", name, "--test-threads=1"],
               cwd=tree, env=e, timeout=timeout)
    if "running 1 test" not in (out.stdout or ""):
        return None
    return ExactRun(name=name, binary=binary, returncode=out.returncode,
                    raw=raw, completed=_completed(out.returncode, [raw]))


def _listed_tests(tree: Path, binary: Path, env: dict[str, str],
                  prof: Path, *, timeout: float) -> list[str]:
    """The tests a binary carries, as it names them (`--list` prints
    `name: test`, module-qualified for unit tests)."""
    e = dict(env)
    e["LLVM_PROFILE_FILE"] = str(prof / "list.profraw")
    out = _run([str(binary), "--list"], cwd=tree, env=e, timeout=timeout)
    suffix = ": test"
    return [ln[:-len(suffix)] for ln in (out.stdout or "").splitlines()
            if ln.endswith(suffix)]


def _name_matches(name: str, skip: Iterable[str]) -> bool:
    leaves = {n.rsplit("::", 1)[-1] for n in skip}
    return name in set(skip) or name.rsplit("::", 1)[-1] in leaves


def _recover_binary(
    tree: Path, binary: Path, env: dict[str, str], prof: Path,
    *, timeout: float, skip: Iterable[str] = (),
) -> dict[str, ExactRun]:
    """A binary whose whole-suite process did not complete, re-run one
    test per process. The runs that complete carry the counts the merge
    lost with the crash; the ones that still die are the tests the reading
    cannot measure. Keyed by the listed name. ``skip`` is the hung test
    the caller already named — re-running it is a second unbounded wait
    on the same test (run `3500465-67c40e78`: kill of pass A handed
    `lib_gen_1` to recovery, which `--exact`'d the hung name)."""
    runs: dict[str, ExactRun] = {}
    for name in _listed_tests(tree, binary, env, prof, timeout=timeout):
        if _name_matches(name, skip):
            continue
        raw = prof / f"one-{_prof_stem(name)}.profraw"
        run = _exact_run(tree, binary, name, raw, env, timeout=timeout)
        if run is not None:
            runs[name] = run
    return runs


def _entered_sites(export: dict[str, Any], tree: Path,
                   index: SiteIndex) -> frozenset[FnSite]:
    return frozenset(
        site for (rel, start), count in _body_counts(export, tree).items()
        if count > 0
        for site in [index.site_at(rel, start)]
        if site is not None)


def _owner_map(
    tree: Path, bins: list[Path], prof: Path, env: dict[str, str],
    tests: Iterable[str], profdata_bin: str, cov_bin: str,
    index: SiteIndex,
    prior: Mapping[str, ExactRun] | None = None,
    *, timeout: float,
) -> tuple[dict[str, frozenset[FnSite]], frozenset[str]]:
    """Which crate bodies does each named test enter, one profile per test
    — and which named tests could not be measured because their process
    did not complete.

    Pass C of `scripts/coverage_owner_probe.py`, promoted into the loop.
    The whole-suite merge cannot answer this: it says a body ran, not
    which test ran it, so it can only convict a body nothing executes.
    A test is profiled alone under `--exact` on whichever binary reports
    `running 1 test` for it; the filter is the cargo name the sweep
    parsed (`gen_tests_2::t`), not the leaf — `--exact t` does not match
    a module-qualified name, and a name no binary claims is left OUT of
    the mapping rather than mapped to the empty set, because unmeasured
    and measured-empty license different answers (`convictors_never_enter`).

    A claimed test whose process did not complete is ALSO left out, and
    returned in the second half: the file its runtime created at start is
    0 bytes and exports as "entered nothing" (run `1577437-c93d5c26`, iter
    4: 6 of 13 red tests read as empty owner sets and the log said
    `owner set for 13/13`). `prior` holds runs the crashed-binary recovery
    already made, reused here (exact name, then unique leaf) instead of
    re-spending the process.

    Cost is one binary invocation per test per candidate binary until the
    owning one answers, plus a merge and an export. Measured on the
    2026-08-30 stable tree: sub-second per test against a 6 s
    uninstrumented build of the same suite, which is why this runs on the
    failing set only and never on the whole oracle.
    """
    owners: dict[str, frozenset[FnSite]] = {}
    unmeasured: set[str] = set()
    prior = dict(prior or {})
    for name in dict.fromkeys(tests):
        key = matching_test_key(name, prior)
        run: ExactRun | None = prior[key] if key is not None else None
        if run is None:
            raw = prof / f"one-{_prof_stem(name)}.profraw"
            for b in bins:
                run = _exact_run(tree, b, name, raw, env, timeout=timeout)
                if run is not None:
                    break
        if run is None:
            continue
        if not run.completed:
            unmeasured.add(name)
            continue
        export = _merge_export([run.raw], [run.binary],
                               prof / f"one-{_prof_stem(name)}.json",
                               profdata_bin, cov_bin)
        if export is None:
            continue
        owners[name] = _entered_sites(export, tree, index)
    return owners, frozenset(unmeasured)


def measure_coverage_slice(
    project_dir: Path,
    cluster: str,
    *,
    failing_tests: Iterable[str] = (),
    log: Callable[[str], None] | None = None,
    cluster_env: Mapping[str, str] | None = None,
    enumerate_fns: Callable[[Path], Sequence[Any] | None],
    timeout: int | None = None,
    hung: Mapping[str, str] | None = None,
) -> CoverageSlice | None:
    """Pass A of the owner probe, plus pass C over `failing_tests`.

    `enumerate_fns` is the loop's own fn enumerator (the extractor port the
    composition root binds); it runs on the staged copy, so its records
    carry `src/…` paths exactly as the export's do. Required: without
    records no body can be named and the reading is not taken.

    Returns a slice of dead locators, dead-region sentences and the
    per-test owner sets, plus the binaries and tests whose process did not
    complete (`unmeasured_binaries`, `unmeasured_tests`), or None when the
    reading cannot be taken. `dead` is computed over the COMPLETED
    processes only; the caller subtracts what an unmeasured test is
    recorded to execute (`coverage_slice.exempt_recorded_executors`),
    because this adapter measures the tree and holds no record. Never
    writes into the loop's target dir.

    `failing_tests` is the sweep's failing set. Empty leaves `owners`
    empty, and every consumer of it then declines to refuse anything —
    the reading degrades to what it was before the owner map existed.

    `cluster_env` is the same mapping the sweep's cargo uses. None keeps
    the pre-inheritance replace of RUSTFLAGS (instrumentation only).

    `timeout` bounds every reconstructed-binary invocation (pass A,
    `--list`, `--exact`). None reads `PHASE_C_TEST_TIMEOUT` (default 120).
    Never unbounded: run `3500465-67c40e78` waited 89 minutes inside pass A
    of `lib_gen_1`. llvm-profdata / llvm-cov / `cargo test --no-run` are
    not this bound.

    `hung` maps a binary label the caller's own reading already saw in
    flight to that test name. Pass A skips those binaries; recovery and
    pass C do not re-run the named test.
    """
    bound = float(timeout if timeout is not None else _suite_timeout())
    hung_map = dict(hung or {})
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
        # Pass A, one process per binary. A profile is a measurement only
        # when the process completed; a binary that did not is re-run one
        # test per process below, so its completing tests still count.
        completed_raws: list[Path] = []
        unmeasured_bins: list[tuple[Path, int]] = []
        hung_tests: set[str] = set()
        recovered: dict[str, ExactRun] = {}
        hung_skip: dict[Path, set[str]] = {}
        for b in bins:
            d = run_a / b.name
            d.mkdir()
            label = _binary_label(b)
            if label in hung_map:
                unmeasured_bins.append((b, -1))
                hung_name = hung_map[label]
                hung_tests.add(f"{label}::{hung_name}")
                hung_skip.setdefault(b, set()).add(hung_name)
                recovered[hung_name] = ExactRun(
                    name=hung_name, binary=b, returncode=-1,
                    raw=d / "hung.profraw", completed=False)
                continue
            e = dict(env)
            e["LLVM_PROFILE_FILE"] = str(d / "%p-%m.profraw")
            ran = _run([str(b), "--test-threads=1"], cwd=tree, env=e,
                       timeout=bound)
            raws = sorted(d.glob("*.profraw"))
            if _completed(ran.returncode, raws):
                completed_raws.extend(raws)
                continue
            unmeasured_bins.append((b, ran.returncode))
            dangling = _in_flight_test(ran.stdout or "")
            if dangling:
                hung_tests.add(f"{label}::{dangling}")
                hung_skip.setdefault(b, set()).add(dangling)
                recovered[dangling] = ExactRun(
                    name=dangling, binary=b, returncode=ran.returncode,
                    raw=d / "hung.profraw", completed=False)
        for b, _rc in unmeasured_bins:
            recovered.update(_recover_binary(
                tree, b, env, prof, timeout=bound,
                skip=hung_skip.get(b, ())))
        completed_raws.extend(
            r.raw for r in recovered.values() if r.completed)
        if unmeasured_bins and log:
            still = sorted(n for n, r in recovered.items() if not r.completed)
            hung_bits = [
                f"{item} (killed after {int(bound)}s)"
                for item in sorted(hung_tests)]
            log("  [coverage] "
                f"{len(unmeasured_bins)} binary(ies) did not complete "
                "(killed by a signal or no profile written): "
                + ", ".join(f"{_binary_label(b)} (rc={rc})"
                            for b, rc in unmeasured_bins)
                + (("; hung inside " + ", ".join(hung_bits)) if hung_bits
                   else "")
                + f"; {len(recovered)} test(s) re-run alone, "
                f"{len(recovered) - len(still)} measured, "
                f"{len(still)} still unmeasured"
                + (": " + ", ".join(still[:8]) if still else ""))
        export = _merge_export(completed_raws, bins,
                               prof / "all.json", profdata_bin, cov_bin)
        if export is None:
            if log:
                log("  [coverage] no whole-suite export — "
                    "dead-body demotion skipped")
            return None
        bodies = _body_counts(export, tree)
        lines = _region_counts(export, tree)
        fn_records = _fn_records(enumerate_fns, tree)
        if not fn_records:
            if log:
                log("  [coverage] the enumerator returned no fn record for "
                    "the staged tree — no body can be named; reading skipped")
            return None
        file_texts: dict[str, str] = {}
        for rel in {k[0] for k in bodies} | {k[0] for k in lines}:
            try:
                file_texts[rel] = (tree / rel).read_text(encoding="utf-8")
            except OSError:
                file_texts[rel] = ""
        index = SiteIndex(fn_records, file_texts)
        asked = tuple(dict.fromkeys(failing_tests))
        owners, unmeasured_asked = _owner_map(
            tree, bins, prof, env, asked, profdata_bin, cov_bin, index,
            prior=recovered, timeout=bound)
        # The crashed binaries' tests that still died alone, under the name
        # the binary lists, unless an asked name already covers them.
        unmeasured_tests = set(unmeasured_asked)
        for name, run in recovered.items():
            if not run.completed and matching_test_key(
                    name, unmeasured_asked) is None:
                unmeasured_tests.add(name)
        slice_ = slice_from_counts(
            _counted_bodies(index, bodies, lines),
            owners=owners, failing=asked,
            unmeasured_binaries=[_binary_label(b) for b, _ in unmeasured_bins],
            unmeasured_tests=unmeasured_tests,
            hung_tests=hung_tests)
        if log:
            log(f"  [coverage] {len(slice_.dead)} dead body(ies), "
                f"{len(slice_.dead_regions)} body(ies) with unreachable lines, "
                f"owner set for {len(owners)}/{len(asked)} failing test(s), "
                f"{len(unmeasured_asked)} unmeasured (binary did not complete) "
                "(staged, own target dir)")
        return slice_
    except (OSError, subprocess.SubprocessError) as exc:
        if log:
            log(f"  [coverage] probe failed ({type(exc).__name__}) — "
                "dead-body demotion skipped")
        return None
    finally:
        shutil.rmtree(work, ignore_errors=True)
