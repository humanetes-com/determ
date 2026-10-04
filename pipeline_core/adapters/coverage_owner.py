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
the ones that still die are reported as unmeasured, and the caller withholds
every negative claim while any run is unmeasured
(`coverage_slice.withhold_unmeasured`). The crash
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
import threading
import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.adapters.process import run_bounded
from pipeline_core.domain.coverage_slice import (
    CountedBody,
    CoverageSlice,
    matching_test_key,
    slice_from_counts,
)
from pipeline_core.domain.fn_execution import SiteIndex as SiteIndex
from pipeline_core.domain.models import FnRecord, FnSite
from pipeline_core.domain.test_results import parse_binary_results
from pipeline_core.threads import ContextPool

_PROF_STEM = re.compile(r"[^A-Za-z0-9_.-]+")
_BIN_HASH = re.compile(r"-[0-9a-f]{16}$")


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


def _premise_cut_enabled() -> bool:
    """`PHASE_C_COVERAGE_PREMISE` (default 1): cut each failing test's owner
    set at its premise boundary. `0` restores the full profile as ownership
    (the pre-cut reading) for an A/B."""
    return os.getenv("PHASE_C_COVERAGE_PREMISE", "1") != "0"


def _suite_timeout() -> int:
    try:
        return int(os.environ.get("PHASE_C_TEST_TIMEOUT", "120"))
    except ValueError:
        return 120


def _coverage_workers() -> int:
    """How many reconstructed-binary processes one coverage step runs at
    once: `PHASE_C_COVERAGE_WORKERS`, else `PIPELINE_PARALLELISM`, else 4.

    The step's cost on a tree that carries a spinning body is the bound
    paid once per binary that never returns and once per single-test
    re-run that never returns, and run `3698228-901de95e` (2026-09-08/09)
    paid it serially: ten coverage steps of 12.1 min each, 121 of the
    fill's 364 minutes, on the same seven binaries every turn. The
    processes are independent — each writes its own profile file under its
    own directory and each is its own process group — so the serial loop
    was the shape of the original `subprocess.run` call, not a contract.
    A spinning binary holds one core; the model is on the GPU."""
    for var in ("PHASE_C_COVERAGE_WORKERS", "PIPELINE_PARALLELISM"):
        raw = os.environ.get(var, "").strip()
        if not raw:
            continue
        try:
            return max(1, int(raw))
        except ValueError:
            continue
    return 4


def _exact_timeout(bound: float) -> float:
    """The bound on one single-test (`--exact`) process:
    `PHASE_C_EXACT_TEST_TIMEOUT` when set, else the suite bound.

    A test that runs alone for the whole suite bound is a hang, but the
    number that licenses a shorter bound is the longest COMPLETING
    single-test run the step has measured (`_Longest`, printed on the
    `[coverage]` line) — the same rule every other bound here follows:
    sized on a reading, never guessed. Unset means no behaviour change."""
    raw = os.environ.get("PHASE_C_EXACT_TEST_TIMEOUT", "").strip()
    if not raw:
        return bound
    try:
        return max(1.0, float(raw))
    except ValueError:
        return bound


class _Longest:
    """The longest completing bounded run in one coverage step, and what it
    ran — the reading that sizes `PHASE_C_EXACT_TEST_TIMEOUT`."""

    def __init__(self) -> None:
        self.seconds = 0.0
        self.label = ""
        self._lock = threading.Lock()

    def note(self, seconds: float, label: str) -> None:
        with self._lock:
            if seconds > self.seconds:
                self.seconds = seconds
                self.label = label

    def render(self) -> str:
        with self._lock:
            if not self.label:
                return "none completed"
            return f"{self.seconds:.1f}s ({self.label})"


def _map_concurrently[T, R](fn: Callable[[T], R], items: Sequence[T],
                            workers: int) -> list[R]:
    """`fn` over `items`, up to `workers` at a time, results in item order.
    One worker (or one item) is the plain serial map — the negative control
    the concurrency test runs against."""
    if workers <= 1 or len(items) <= 1:
        return [fn(it) for it in items]
    with ContextPool(max_workers=min(workers, len(items))) as pool:
        return list(pool.map(fn, items))


def _run_timed(cmd: list[str], cwd: Path, env: dict[str, str] | None = None,
               *, timeout: float | None = None,
               ) -> tuple[subprocess.CompletedProcess[str], float]:
    t0 = time.monotonic()
    out = _run(cmd, cwd=cwd, env=env, timeout=timeout)
    return out, time.monotonic() - t0


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
               env: dict[str, str], *, timeout: float,
               longest: _Longest | None = None) -> ExactRun | None:
    """Run `name` alone on `binary`; None when the binary does not claim it
    (`running 1 test` is printed before the test starts, so a claim is
    readable even when the process then dies). A completing run's duration
    is noted on `longest` — the reading that sizes the single-test bound."""
    raw.unlink(missing_ok=True)
    e = dict(env)
    e["LLVM_PROFILE_FILE"] = str(raw)
    out, secs = _run_timed([str(binary), "--exact", name, "--test-threads=1"],
                           cwd=tree, env=e, timeout=timeout)
    if "running 1 test" not in (out.stdout or ""):
        return None
    run = ExactRun(name=name, binary=binary, returncode=out.returncode,
                   raw=raw, completed=_completed(out.returncode, [raw]))
    if run.completed and longest is not None:
        longest.note(secs, f"{_binary_label(binary)}::{name}")
    return run


# -- the premise cut ---------------------------------------------------------
#
# A failing test's full profile includes everything its FAILURE REPORT runs:
# `Result::unwrap` on an `Err` formats the error's Debug inside the panic
# machinery, and a test returning `Err` has its error formatted by the test
# harness after the body returned. Those bodies run after the premise failed
# and cannot change whether it holds. Measured 2026-09-21 on run
# `450977-5f48b68a`'s tree (scratch copy, `test_downcast_low`): full profile
# 39 crate fns, cut at the panic handler's entry 15; `Chain::len`,
# `Chain::next`, `ErrorImpl::debug`, `Indented::write_str` all 0% at the cut.
# A panic hook cannot make this cut: std formats the payload before any hook.
#
# The boundaries, resolved from the test binary's own symbol table:
#  - the panic handler (`rust_begin_unwind`, v0-mangled `…17rust_begin_unwind`);
#  - the harness's result check (`test::assert_test_result::<T>`), entered
#    after the test body returned and before its `Err` is reported.
# The profile is flushed at EVERY boundary hit (to `LLVM_PROFILE_FILE`, then
# copied aside by the debugger — the runtime links no set-filename call in a
# test binary) and the LAST copy is the premise: a panic the test catches (`catch_unwind`) is followed
# by the one that fails it. A hit with a boundary frame already on the stack
# is a panic raised while reporting the first (a stubbed Debug impl panicking
# inside the message) and is not a boundary. A process that dies before any
# boundary (SIGSEGV, abort) or a debugger that cannot run leaves no cut; the
# caller then keeps the full profile and says so.
_BOUNDARY_SUFFIXES = ("rust_begin_unwind",)
_BOUNDARY_SUBSTRINGS = ("assert_test_result",)

_GDB_SCRIPT = r"""
import shutil
import gdb
BASE = {base!r}
SYMS = {syms!r}
state = {{"k": 0, "names": set(), "hit": False}}


class _Cut(gdb.Breakpoint):
    # Decide only; the flush happens in the loop below. Calling into the
    # inferior from `stop` resumes it inside the callback and hangs gdb.
    def stop(self):
        here = gdb.newest_frame()
        state["names"].add(here.name() or "")
        f = here.older()
        while f is not None:
            if (f.name() or "") in state["names"]:
                return False
            f = f.older()
        state["hit"] = True
        return True


gdb.execute("set pagination off")
gdb.execute("set confirm off")
gdb.execute("set language c")
for s in SYMS:
    _Cut("'" + s + "'", internal=True)
gdb.execute("run")
while state["hit"] and gdb.selected_inferior().pid:
    state["hit"] = False
    state["k"] += 1
    gdb.execute("call (int)__llvm_profile_write_file()", to_string=True)
    shutil.copyfile(BASE, "%s.cut%d" % (BASE, state["k"]))
    gdb.execute("continue")
"""


def _boundary_symbols(binary: Path) -> tuple[str, ...]:
    """The linkage names of the premise boundaries in `binary`; empty when
    `nm` is missing or finds none (the caller falls back and says so)."""
    nm = shutil.which("nm")
    if nm is None:
        return ()
    r = _run([nm, "--defined-only", str(binary)], cwd=binary.parent)
    if r.returncode != 0:
        return ()
    out: list[str] = []
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[1] not in ("T", "t"):
            continue
        name = parts[2]
        if (name.endswith(_BOUNDARY_SUFFIXES)
                or any(sub in name for sub in _BOUNDARY_SUBSTRINGS)):
            out.append(name)
    return tuple(dict.fromkeys(out))


@dataclass(frozen=True, slots=True)
class PremiseRun:
    """The profile flushed at a failing test's last premise boundary, or
    why there is none (`reason`, printed on the `[coverage]` line)."""

    raw: Path | None
    reason: str = ""
    #: The flush at the FIRST boundary: the premise of a run whose process
    #: then died (`_owner_map`), where every later boundary is raised while
    #: the first panic's unwind is being cleaned up.
    first: Path | None = None


def _premise_run(tree: Path, binary: Path, name: str, base: Path,
                 env: dict[str, str], *, timeout: float,
                 symbols: tuple[str, ...]) -> PremiseRun:
    """Re-run `name` alone under gdb, flushing the profile at each premise
    boundary (see the block comment above), and return the last flush."""
    gdb = shutil.which("gdb")
    if gdb is None:
        return PremiseRun(None, "gdb not on PATH")
    if not symbols:
        return PremiseRun(None, "no premise boundary symbol in the binary")
    for old in base.parent.glob(base.name + "*"):
        old.unlink(missing_ok=True)
    script = base.with_suffix(".gdb.py")
    script.write_text(_GDB_SCRIPT.format(base=str(base), syms=list(symbols)),
                      encoding="utf-8")
    e = dict(env)
    e["LLVM_PROFILE_FILE"] = str(base)
    _run([gdb, "-nx", "-batch", "-x", str(script), "--args", str(binary),
          "--exact", name, "--test-threads=1"], cwd=tree, env=e,
         timeout=int(max(1, timeout)))
    cuts = sorted(base.parent.glob(base.name + ".cut*"),
                  key=lambda p: int(p.name.rsplit(".cut", 1)[-1] or 0))
    cuts = [c for c in cuts if c.stat().st_size > 0]
    if not cuts:
        return PremiseRun(None, "no boundary was reached")
    return PremiseRun(cuts[-1], first=cuts[0])


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


def _recover_binaries(
    tree: Path, items: Sequence[tuple[Path, Iterable[str]]],
    env: dict[str, str], prof: Path,
    *, timeout: float, exact_timeout: float | None = None,
    workers: int = 1, longest: _Longest | None = None,
) -> dict[tuple[str, str], ExactRun]:
    """Every binary whose whole-suite process did not complete, re-run one
    test per process. The runs that complete carry the counts the merge
    lost with the crash; the ones that still die are the tests the reading
    cannot measure. Keyed by `(binary label, listed name)`, in listing
    order: a name two binaries share is two tests. Keyed by the name alone
    the later binary's run replaced the earlier one, and the owner map
    then answered a test with the other binary's execution (run
    `3893442-cb547388`, 2026-09-19: `test_downcast_ref` fails in both
    `test_context` and `test_downcast`; the session got the latter's slice
    and the former's panic).

    Each item is `(binary, skip)`: ``skip`` is the hung test the caller
    already named for that binary — re-running it is a second wait on the
    same test (run `3500465-67c40e78`: kill of pass A handed `lib_gen_1` to
    recovery, which `--exact`'d the hung name).

    The listings are serial (`--list` runs no test); the single-test
    processes run `workers` at a time across every binary, each under
    `exact_timeout` (the suite bound when None). The profile file carries
    the binary's stem as well as the test's, so two binaries listing the
    same name never write one file concurrently."""
    bound = float(timeout if exact_timeout is None else exact_timeout)
    tasks: list[tuple[Path, str, Path]] = []
    for binary, skip in items:
        skip_names = tuple(skip)
        for name in _listed_tests(tree, binary, env, prof, timeout=timeout):
            if _name_matches(name, skip_names):
                continue
            raw = prof / (f"one-{_prof_stem(_binary_label(binary))}"
                          f"-{_prof_stem(name)}.profraw")
            tasks.append((binary, name, raw))

    def _one(task: tuple[Path, str, Path]) -> tuple[
            tuple[str, str], ExactRun | None]:
        binary, name, raw = task
        return (_binary_label(binary), name), _exact_run(
            tree, binary, name, raw, env, timeout=bound, longest=longest)

    runs: dict[tuple[str, str], ExactRun] = {}
    for key, run in _map_concurrently(_one, tasks, workers):
        if run is not None:
            runs[key] = run
    return runs


def _recover_binary(
    tree: Path, binary: Path, env: dict[str, str], prof: Path,
    *, timeout: float, skip: Iterable[str] = (),
) -> dict[tuple[str, str], ExactRun]:
    """One binary through `_recover_binaries`, serially — the pre-concurrency
    shape, kept for callers that recover a single binary."""
    return _recover_binaries(tree, [(binary, skip)], env, prof,
                             timeout=timeout, workers=1)


def _entered_sites(export: dict[str, Any], tree: Path,
                   index: SiteIndex) -> frozenset[FnSite]:
    return frozenset(
        site for (rel, start), count in _body_counts(export, tree).items()
        if count > 0
        for site in [index.site_at(rel, start)]
        if site is not None)


@dataclass(frozen=True, slots=True)
class OwnerReading:
    """What `_owner_map` measured: `owners` (premise sets), the names whose
    process did not complete, the names measured on several binaries, the
    after-the-failure sets (`rendered`) and the names whose premise could
    not be cut (`premise_fallbacks`: name -> reason; their owner set is the
    full profile)."""

    owners: dict[str, frozenset[FnSite]]
    unmeasured: frozenset[str]
    multi: frozenset[str]
    rendered: dict[str, frozenset[FnSite]]
    premise_fallbacks: dict[str, str]
    #: Unmeasured names whose owner set was still read, up to their first
    #: panic, from the debugger's flush before the process died.
    cut_before_death: frozenset[str] = frozenset()


def _owner_map(
    tree: Path, bins: list[Path], prof: Path, env: dict[str, str],
    tests: Iterable[str], profdata_bin: str, cov_bin: str,
    index: SiteIndex,
    prior: Mapping[tuple[str, str], ExactRun] | None = None,
    *, timeout: float, workers: int = 1, longest: _Longest | None = None,
    failing_binaries: Mapping[str, Collection[str]] | None = None,
    premise: bool = True,
) -> OwnerReading:
    """Which crate bodies does each named test enter BEFORE its failure is
    reported, one profile per test — and which named tests could not be
    measured because their process did not complete.

    A run that failed (non-zero exit, completed) is re-run under the
    debugger and cut at its last premise boundary (`_premise_run`): the
    owner set is what ran before the panic handler or the harness's result
    check, and what ran only after it — message rendering, unwind drops —
    is `rendered`, evidence and never ownership. A run with no cut (no
    debugger, no boundary symbol, no boundary reached) keeps its full
    profile as its owner set and is named in `premise_fallbacks` with the
    reason, so the `[coverage]` line says the reading is the union, never
    silently. A passing run has no failure to report: its full profile is
    its owner set. `premise=False` is the pre-cut reading (full profile as
    ownership), the negative control.

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

    A name the sweep saw fail in several binaries (`failing_binaries`:
    name -> the cargo labels of the binaries that failed it) is several
    failing tests. Each is run on its own binary and the owner set of the
    name is the UNION of what they entered: the slice built from it must
    hold every body the quoted failures executed, and a binary where the
    name passes contributes nothing. A test that died on any of them makes
    the name unmeasured. The third return value is those names. Without
    `failing_binaries` (or when none of its labels is built here), the
    first binary that claims the name answers, as before.

    Cost is one binary invocation per test per candidate binary until the
    owning one answers, plus a merge and an export. Measured on the
    2026-08-30 stable tree: sub-second per test against a 6 s
    uninstrumented build of the same suite, which is why this runs on the
    failing set only and never on the whole oracle. The tests are
    independent (own profile, own export file), so they run `workers` at a
    time; `timeout` here is the single-test bound.
    """
    owners: dict[str, frozenset[FnSite]] = {}
    rendered: dict[str, frozenset[FnSite]] = {}
    fallbacks: dict[str, str] = {}
    unmeasured: set[str] = set()
    multi: set[str] = set()
    prior = dict(prior or {})
    by_label = {_binary_label(b): b for b in bins}
    fb = {k: frozenset(v) for k, v in (failing_binaries or {}).items()}
    syms: dict[Path, tuple[str, ...]] = {}
    syms_lock = threading.Lock()

    def _symbols(b: Path) -> tuple[str, ...]:
        with syms_lock:
            if b not in syms:
                syms[b] = _boundary_symbols(b)
            return syms[b]

    def _sites_of(raw: Path, binary: Path, stem: str) -> frozenset[FnSite] | None:
        export = _merge_export([raw], [binary], prof / f"{stem}.json",
                               profdata_bin, cov_bin)
        return None if export is None else _entered_sites(export, tree, index)

    def _prior_run(label: str, name: str) -> ExactRun | None:
        keys = [n for (lab, n) in prior if lab == label]
        key = matching_test_key(name, keys)
        return prior[(label, key)] if key is not None else None

    def _cut_before_death(runs: Sequence[ExactRun],
                          name: str) -> frozenset[FnSite] | None:
        """What a name's runs entered up to their first panic, when a
        process died and left no profile of its own.

        A process that aborts writes no profile, so its test had no owner
        set at all — not even the positive evidence a run truncated inside
        the crate keeps. Run `497151-a6fa205b` (2026-09-22, iter 4): once
        `construct` had a body, 92 failing tests aborted even alone (the
        first panic unwinds into a still-stubbed drop path, which panics
        during cleanup), and 91 of 150 failing tests had no owner set. The
        debugger flushes the profile at the panic handler, before the
        abort; the first flush is the premise, since every later one is
        raised while that panic's unwind is being cleaned up. The name
        stays unmeasured: its run did not reach its end, so every negative
        claim stays withheld (`coverage_slice.withhold_unmeasured`), exactly
        as for a run truncated inside the crate. None when any run of the
        name reached no boundary, or cut is off. A run the bound cut (-1)
        is never re-run: that is a second wait on the same hang."""
        if not premise or any(not r.completed and r.returncode >= -1
                              for r in runs):
            return None
        sites: set[FnSite] = set()
        for r in runs:
            if r.completed:
                stem = (f"one-{_prof_stem(_binary_label(r.binary))}"
                        f"-{_prof_stem(name)}")
                full = _sites_of(r.raw, r.binary, stem)
                if full is None:
                    return None
                sites |= full
                continue
            stem = (f"one-{_prof_stem(_binary_label(r.binary))}"
                    f"-{_prof_stem(name)}-died")
            try:
                cut = _premise_run(
                    tree, r.binary, name, prof / f"{stem}.profraw", env,
                    timeout=timeout * 2 + 30, symbols=_symbols(r.binary))
            except (OSError, subprocess.SubprocessError):
                return None
            if cut.first is None:
                return None
            before = _sites_of(cut.first, r.binary, f"{stem}-first")
            if before is None:
                return None
            sites |= before
        return frozenset(sites)

    def _one(name: str) -> tuple[str, frozenset[FnSite] | None, bool, int,
                                 frozenset[FnSite], str]:
        labels = fb.get(name) or fb.get(name.rsplit("::", 1)[-1]) or frozenset()
        failing = [by_label[lab] for lab in sorted(labels) if lab in by_label]
        # Every binary that failed the name, each measured; otherwise the
        # first binary that claims it.
        candidates, every = (failing, True) if failing else (list(bins), False)
        runs: list[ExactRun] = []
        for b in candidates:
            label = _binary_label(b)
            run = _prior_run(label, name)
            if run is None:
                raw = prof / (f"one-{_prof_stem(label)}"
                              f"-{_prof_stem(name)}.profraw")
                run = _exact_run(tree, b, name, raw, env, timeout=timeout,
                                 longest=longest)
            if run is None:
                continue
            runs.append(run)
            if not every:
                break
        if not runs:
            return name, None, False, 0, frozenset(), ""
        if any(not r.completed for r in runs):
            return (name, _cut_before_death(runs, name), True, len(runs),
                    frozenset(), "")
        sites: set[FnSite] = set()
        after: set[FnSite] = set()
        why: list[str] = []
        measured = 0
        for r in runs:
            stem = (f"one-{_prof_stem(_binary_label(r.binary))}"
                    f"-{_prof_stem(name)}")
            full = _sites_of(r.raw, r.binary, stem)
            if full is None:
                continue
            measured += 1
            if not premise or r.returncode <= 0:
                # Passed (0): no failure to report. Killed by a signal (<0):
                # no report ran either, and no boundary can be reached.
                sites |= full
                continue
            try:
                cut = _premise_run(
                    tree, r.binary, name, prof / f"{stem}-premise.profraw",
                    env, timeout=timeout * 2 + 30, symbols=_symbols(r.binary))
            except (OSError, subprocess.SubprocessError) as exc:
                # A premise that cannot be taken falls back to the full
                # profile; it must never cost the whole coverage reading.
                cut = PremiseRun(None, f"premise run failed "
                                       f"({type(exc).__name__})")
            before = (_sites_of(cut.raw, r.binary, f"{stem}-premise")
                      if cut.raw is not None else None)
            if before is None:
                sites |= full
                why.append(f"{_binary_label(r.binary)}: "
                           f"{cut.reason or 'premise profile did not export'}")
                continue
            sites |= before
            after |= full - before
        if measured < len(runs):
            # One of the name's failing tests left no export: a union
            # missing it is the one-binary answer this rule replaces.
            return name, None, False, len(runs), frozenset(), ""
        return (name, frozenset(sites), False, len(runs),
                frozenset(after - sites), "; ".join(why))

    cut_before_death: set[str] = set()
    for name, sites, died, n_runs, after, why in _map_concurrently(
            _one, list(dict.fromkeys(tests)), workers):
        if n_runs > 1:
            multi.add(name)
        if died:
            unmeasured.add(name)
            if sites is not None:
                owners[name] = sites
                cut_before_death.add(name)
        elif sites is not None:
            owners[name] = sites
            if after:
                rendered[name] = after
            if why:
                fallbacks[name] = why
    return OwnerReading(owners=owners, unmeasured=frozenset(unmeasured),
                        multi=frozenset(multi), rendered=rendered,
                        premise_fallbacks=fallbacks,
                        cut_before_death=frozenset(cut_before_death))


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
    workers: int | None = None,
    exact_timeout: float | None = None,
    failing_binaries: Mapping[str, Collection[str]] | None = None,
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
    processes only; the caller withholds it while any run is unmeasured
    (`coverage_slice.withhold_unmeasured`), because a run that stopped
    short bounds nothing about the bodies after its stop. Never
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

    `workers` is how many reconstructed-binary processes run at once in
    pass A, in recovery and in pass C (None reads
    `PHASE_C_COVERAGE_WORKERS` / `PIPELINE_PARALLELISM`, default 4); the
    judge's reading names only the binary its single bounded sweep died
    in, so a tree whose instrumented build spins in seven binaries paid
    seven bounds serially per turn before this (run `3698228-901de95e`:
    121 minutes over ten steps). `exact_timeout` bounds the single-test
    processes (None reads `PHASE_C_EXACT_TEST_TIMEOUT`, else the suite
    bound); the longest completing run is printed for sizing it.

    `failing_binaries` maps a failing name to the cargo labels of the
    binaries the sweep saw fail it (`test_context` for
    `tests/test_context.rs`); pass C measures the name on each of them and
    unions (see `_owner_map`). None keeps the first-claimant answer.
    """
    bound = float(timeout if timeout is not None else _suite_timeout())
    exact_bound = (float(exact_timeout) if exact_timeout is not None
                   else _exact_timeout(bound))
    pool = workers if workers is not None else _coverage_workers()
    longest = _Longest()
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
        recovered: dict[tuple[str, str], ExactRun] = {}
        hung_skip: dict[Path, set[str]] = {}
        for b in bins:
            (run_a / b.name).mkdir()

        def _pass_a(b: Path) -> tuple[
                Path, subprocess.CompletedProcess[str], list[Path], bool]:
            d = run_a / b.name
            e = dict(env)
            e["LLVM_PROFILE_FILE"] = str(d / "%p-%m.profraw")
            ran, secs = _run_timed([str(b), "--test-threads=1"], cwd=tree,
                                   env=e, timeout=bound)
            raws = sorted(d.glob("*.profraw"))
            done = _completed(ran.returncode, raws)
            if done:
                longest.note(secs, _binary_label(b))
            return b, ran, raws, done

        # The binaries the judge did not already see hang, `pool` at a
        # time; the results are read back in `bins` order so the log line
        # and the unmeasured list are the same under any worker count.
        ran_by: dict[Path, tuple[subprocess.CompletedProcess[str],
                                 list[Path], bool]] = {
            b: (ran, raws, done)
            for b, ran, raws, done in _map_concurrently(
                _pass_a, [b for b in bins
                          if _binary_label(b) not in hung_map], pool)}
        for b in bins:
            d = run_a / b.name
            label = _binary_label(b)
            if label in hung_map:
                unmeasured_bins.append((b, -1))
                hung_name = hung_map[label]
                hung_tests.add(f"{label}::{hung_name}")
                hung_skip.setdefault(b, set()).add(hung_name)
                recovered[(label, hung_name)] = ExactRun(
                    name=hung_name, binary=b, returncode=-1,
                    raw=d / "hung.profraw", completed=False)
                continue
            ran, raws, done = ran_by[b]
            if done:
                completed_raws.extend(raws)
                continue
            unmeasured_bins.append((b, ran.returncode))
            dangling = _in_flight_test(ran.stdout or "")
            # Only the bound's kill is a hang (`run_bounded` reports it as
            # -1). A binary killed by a signal of its own (SIGABRT from a
            # panic during cleanup, SIGSEGV) died in that test, which is
            # re-run alone like the rest: run `497151-a6fa205b`
            # (2026-09-22, iter 4) printed 28 aborts as "hung inside …
            # (killed after 120s)" and emitted them as hung tests.
            if dangling and ran.returncode == -1:
                hung_tests.add(f"{label}::{dangling}")
                hung_skip.setdefault(b, set()).add(dangling)
                recovered[(label, dangling)] = ExactRun(
                    name=dangling, binary=b, returncode=ran.returncode,
                    raw=d / "hung.profraw", completed=False)
        recovered.update(_recover_binaries(
            tree, [(b, hung_skip.get(b, ())) for b, _rc in unmeasured_bins],
            env, prof, timeout=bound, exact_timeout=exact_bound,
            workers=pool, longest=longest))
        completed_raws.extend(
            r.raw for r in recovered.values() if r.completed)
        if unmeasured_bins and log:
            still = sorted(n for (_lab, n), r in recovered.items()
                           if not r.completed)
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
        reading = _owner_map(
            tree, bins, prof, env, asked, profdata_bin, cov_bin, index,
            prior=recovered, timeout=exact_bound, workers=pool,
            longest=longest, failing_binaries=failing_binaries,
            premise=_premise_cut_enabled())
        owners, unmeasured_asked, multi = (
            reading.owners, reading.unmeasured, reading.multi)
        # The crashed binaries' tests that still died alone, under the name
        # the binary lists, unless an asked name already covers them.
        unmeasured_tests = set(unmeasured_asked)
        for (_lab, name), run in recovered.items():
            if not run.completed and matching_test_key(
                    name, unmeasured_asked) is None:
                unmeasured_tests.add(name)
        slice_ = slice_from_counts(
            _counted_bodies(index, bodies, lines),
            owners=owners, rendered=reading.rendered, failing=asked,
            unmeasured_binaries=[_binary_label(b) for b, _ in unmeasured_bins],
            unmeasured_tests=unmeasured_tests,
            hung_tests=hung_tests)
        if log:
            log(f"  [coverage] {len(slice_.dead)} dead body(ies), "
                f"{len(slice_.dead_regions)} body(ies) with unreachable lines, "
                f"owner set for {len(owners)}/{len(asked)} failing test(s), "
                f"{len(unmeasured_asked)} unmeasured (binary did not complete"
                + (f"; {len(reading.cut_before_death)} of them owned up to "
                   "their first panic from the debugger's flush before the "
                   "process died" if reading.cut_before_death else "")
                + ")"
                + (f", {len(multi)} failing in several binaries measured on "
                   f"each and unioned ({', '.join(sorted(multi)[:6])})"
                   if multi else "")
                + (f", premise cut for "
                   f"{len(owners) - len(reading.premise_fallbacks)}/"
                   f"{len(owners)} ({len(reading.rendered)} with bodies run "
                   f"only while the failure was reported)"
                   if _premise_cut_enabled() else ", premise cut OFF")
                + (", PREMISE FALLBACK to the full profile for "
                   + "; ".join(f"{n} ({why})" for n, why in
                               sorted(reading.premise_fallbacks.items())[:6])
                   if reading.premise_fallbacks else "")
                + " "
                f"(staged, own target dir; {pool} worker(s), single-test "
                f"bound {int(exact_bound)}s, longest completing run "
                f"{longest.render()})")
        return slice_
    except (OSError, subprocess.SubprocessError) as exc:
        if log:
            log(f"  [coverage] probe failed ({type(exc).__name__}) — "
                "dead-body demotion skipped")
        return None
    finally:
        shutil.rmtree(work, ignore_errors=True)
