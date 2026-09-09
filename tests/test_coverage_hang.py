"""The coverage slice bounds every reconstructed-binary invocation and
takes the judge's hang reading as input.

The instance: run `3500465-67c40e78` (2026-09-08), session turn 1 judged
at 18:00:14 UTC with `lib_gen_1` hung; the per-turn coverage slice then
re-ran that binary through an unbounded `subprocess.run` and the pipeline
sat in `futex_wait` behind it for two hours (pid 3605384). The judge had
the binary AND the in-flight test one call earlier.

Sentences pinned here:
- a pass-A process the bound cuts is an unmeasured binary whose in-flight
  test is named in `hung_tests`; recovery re-runs the binary's OTHER tests
  and never the hung one, and pass C never `--exact`s it either;
- a binary the caller's own reading already named hung is not run in pass
  A at all — straight to recovery minus the named test;
- every test-binary invocation carries the bound; `cargo test --no-run`
  and the llvm tools carry none (a 120s bound can kill a healthy
  instrumented compile, and they are not the reconstructed tree).
The negative control is the pre-fix runner's shape: a test-binary call
with no `timeout` — the fake refuses it, so the old `_run` cannot pass.
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from pipeline_core.adapters import coverage_owner as cov
from pipeline_core.adapters.coverage_owner import measure_coverage_slice
from pipeline_core.domain.models import FnLocator, FnSite

_BIN = (Path(__file__).resolve().parents[1] / "tools" / "skeleton_extractor"
        / "target" / "release" / "skeleton-extractor")


def _enumerate(tree: Path) -> list:
    from pipeline_core.adapters.extractor import CliExtractor
    if not _BIN.exists():
        pytest.skip("extractor release binary missing")
    return CliExtractor(_BIN).enumerate_fns(tree) or []


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    src = project / ".skeleton-filled" / "stable" / "src"
    src.mkdir(parents=True)
    (src / "lib.rs").write_text(
        "fn only_hang_binary() {\n    let a = 1;\n}\n"
        "fn nothing_runs() {\n    let z = 0;\n}\n"
        "fn other_binary() {\n    let q = 0;\n}\n", encoding="utf-8")
    return project


def _hang_env(monkeypatch, tmp_path: Path) -> list[tuple[list[str], object]]:
    """Two binaries. `lib_gen_1-<hash>` lists `t_ok` and `t_hang`; on the
    whole suite the bound cuts it inside `t_hang` (rc -1, partial stdout,
    no profile). `ok_bin-<hash>` completes and enters `other_binary`. An
    `--exact t_hang` call is the defect and raises."""
    calls: list[tuple[list[str], object]] = []
    monkeypatch.setattr(cov.shutil, "which", lambda n: "/usr/bin/" + n)
    deps = tmp_path / "bins"
    deps.mkdir(exist_ok=True)
    hang = deps / "lib_gen_1-1039523f4c62a3ce"
    ok = deps / "ok_bin-0123456789abcdef"
    monkeypatch.setattr(cov, "_test_binaries", lambda *a, **k: [hang, ok])

    def _fake_run(cmd, cwd, env=None, **k):
        calls.append((list(cmd), k.get("timeout")))
        prog = Path(cmd[0]).name
        if cmd[0] in ("cargo", "llvm-profdata", "llvm-cov"):
            assert k.get("timeout") is None, "the bound is for test binaries only"
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        assert k.get("timeout"), f"test binary invoked with no bound: {cmd}"
        pattern = (env or {}).get("LLVM_PROFILE_FILE", "")
        if "--list" in cmd:
            names = (["t_ok: test", "t_hang: test"]
                     if prog.startswith("lib_gen_1") else ["t_other: test"])
            return subprocess.CompletedProcess(
                cmd, 0, stdout="\n".join(names) + "\n", stderr="")
        if "--exact" in cmd:
            name = cmd[cmd.index("--exact") + 1]
            assert name != "t_hang", "the hung test was re-run alone"
            mine = (name == "t_ok" if prog.startswith("lib_gen_1")
                    else name == "t_other")
            if not mine:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="running 0 tests\n", stderr="")
            Path(pattern).write_bytes(b"counts:" + name.encode())
            return subprocess.CompletedProcess(
                cmd, 0, stdout="running 1 test\n", stderr="")
        # whole-suite pass A
        if prog.startswith("lib_gen_1"):
            return subprocess.CompletedProcess(
                cmd, -1,
                stdout="running 2 tests\ntest t_ok ... ok\ntest t_hang ... ",
                stderr="\nTIMEOUT after 120s")
        Path(pattern.replace("%p", "1").replace("%m", "1")).write_bytes(
            b"counts:t_other")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(cov, "_run", _fake_run)

    def _fake_export(prof_files, objects, dest, profdata_bin, cov_bin):
        src = str(dest.parent.parent / "tree" / "src" / "lib.rs")
        blob = b"".join(Path(p).read_bytes() for p in prof_files)
        ok_n = 4 if b"t_ok" in blob else 0
        other = 4 if b"t_other" in blob else 0
        return {"data": [{"functions": [
            {"filenames": [src], "count": ok_n, "regions": [[1, 1, 3, 1, ok_n]]},
            {"filenames": [src], "count": 0, "regions": [[4, 1, 6, 1, 0]]},
            {"filenames": [src], "count": other, "regions": [[7, 1, 9, 1, other]]},
        ]}]}

    monkeypatch.setattr(cov, "_merge_export", _fake_export)
    return calls


def _pass_a_calls(calls, label: str) -> list[list[str]]:
    return [c for c, _t in calls
            if Path(c[0]).name.startswith(label) and c[1:] == ["--test-threads=1"]]


def test_a_pass_a_hang_names_the_test_and_recovery_skips_it(
        monkeypatch, tmp_path: Path) -> None:
    project = _project(tmp_path)
    calls = _hang_env(monkeypatch, tmp_path)
    notes: list[str] = []
    slice_ = measure_coverage_slice(
        project, "stable", failing_tests=["t_hang", "t_other"],
        log=notes.append, enumerate_fns=_enumerate, timeout=120)
    assert slice_ is not None
    assert slice_.unmeasured_binaries == frozenset({"lib_gen_1"})
    assert slice_.hung_tests == frozenset({"lib_gen_1::t_hang"})
    assert "t_hang" in slice_.unmeasured_tests and "t_hang" not in slice_.owners
    assert slice_.owners["t_other"] == frozenset(
        {FnSite("src/lib.rs", "other_binary", "")})
    # The hung binary's completing test was recovered alone, so the body only
    # it executes is live; a body nothing executes is still dead.
    assert FnLocator("src/lib.rs", "only_hang_binary") not in slice_.dead
    assert FnLocator("src/lib.rs", "nothing_runs") in slice_.dead
    assert len(_pass_a_calls(calls, "lib_gen_1")) == 1
    assert any("hung inside lib_gen_1::t_hang (killed after 120s)" in n
               for n in notes)
    # Every test-binary call carried the bound handed in.
    assert {t for c, t in calls if "/bins/" in c[0]} == {120.0}


def test_a_binary_the_judge_named_hung_is_not_run_in_pass_a(
        monkeypatch, tmp_path: Path) -> None:
    project = _project(tmp_path)
    calls = _hang_env(monkeypatch, tmp_path)
    slice_ = measure_coverage_slice(
        project, "stable", failing_tests=["t_hang", "t_other"],
        enumerate_fns=_enumerate, timeout=120,
        hung={"lib_gen_1": "t_hang"})
    assert slice_ is not None
    assert _pass_a_calls(calls, "lib_gen_1") == []
    assert slice_.hung_tests == frozenset({"lib_gen_1::t_hang"})
    assert "t_hang" in slice_.unmeasured_tests
    assert FnLocator("src/lib.rs", "only_hang_binary") not in slice_.dead


def test_the_bound_defaults_to_the_suite_timeout_and_never_to_unbounded(
        monkeypatch, tmp_path: Path) -> None:
    project = _project(tmp_path)
    calls = _hang_env(monkeypatch, tmp_path)
    monkeypatch.setenv("PHASE_C_TEST_TIMEOUT", "77")
    assert measure_coverage_slice(
        project, "stable", failing_tests=["t_other"],
        enumerate_fns=_enumerate) is not None
    assert {t for c, t in calls if "/bins/" in c[0]} == {77.0}


def _spin_env(monkeypatch, tmp_path: Path, *, spinners: int = 4, fast: int = 3
              ) -> list[tuple[list[str], object]]:
    """`spinners` binaries the bound cuts inside `t_spin` after SLEEPING the
    whole bound — the instrumented build spins where the judge's plain build
    completed, so no hung map can name them (run `3698228-901de95e`: seven
    such binaries, ten steps, 12.1 min each) — and `fast` binaries that
    complete at once. Nothing is asked for, so pass C runs no test."""
    calls: list[tuple[list[str], object]] = []
    monkeypatch.setattr(cov.shutil, "which", lambda n: "/usr/bin/" + n)
    deps = tmp_path / "bins"
    deps.mkdir(exist_ok=True)
    bins = ([deps / f"spin_{i}-{i:016x}" for i in range(spinners)]
            + [deps / f"fast_{i}-{i + 100:016x}" for i in range(fast)])
    monkeypatch.setattr(cov, "_test_binaries", lambda *a, **k: list(bins))

    def _fake_run(cmd, cwd, env=None, **k):
        calls.append((list(cmd), k.get("timeout")))
        prog = Path(cmd[0]).name
        if cmd[0] in ("cargo", "llvm-profdata", "llvm-cov"):
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        assert k.get("timeout"), f"test binary invoked with no bound: {cmd}"
        pattern = (env or {}).get("LLVM_PROFILE_FILE", "")
        if "--list" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="t_ok: test\nt_spin: test\n", stderr="")
        if "--exact" in cmd:
            name = cmd[cmd.index("--exact") + 1]
            assert name != "t_spin", "the hung test was re-run alone"
            Path(pattern).write_bytes(b"counts:" + name.encode())
            return subprocess.CompletedProcess(
                cmd, 0, stdout="running 1 test\n", stderr="")
        if prog.startswith("spin_"):
            # A real wall-clock wait: the repo conftest no-ops `time.sleep`.
            threading.Event().wait(float(k["timeout"]))
            return subprocess.CompletedProcess(
                cmd, -1,
                stdout="running 2 tests\ntest t_ok ... ok\ntest t_spin ... ",
                stderr="\nTIMEOUT")
        Path(pattern.replace("%p", "1").replace("%m", "1")).write_bytes(
            b"counts:fast")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(cov, "_run", _fake_run)
    monkeypatch.setattr(cov, "_merge_export",
                        lambda *a, **k: {"data": [{"functions": []}]})
    return calls


def test_b_spinning_binaries_pay_the_bound_concurrently_and_read_the_same(
        monkeypatch, tmp_path: Path) -> None:
    """Four binaries that never return cost one bound with four workers and
    four bounds serially; the reading — which binaries were unmeasured,
    which test each hung inside — is byte-identical either way. The serial
    run IS the pre-concurrency adapter (`_map_concurrently` at one worker
    is the plain loop), so it is the negative control, not a mock of one."""
    project = _project(tmp_path)
    bound = 1
    _spin_env(monkeypatch, tmp_path)
    t0 = time.monotonic()
    four = measure_coverage_slice(project, "stable", enumerate_fns=_enumerate,
                                  timeout=bound, workers=4)
    wall_four = time.monotonic() - t0
    _spin_env(monkeypatch, tmp_path)
    t0 = time.monotonic()
    one = measure_coverage_slice(project, "stable", enumerate_fns=_enumerate,
                                 timeout=bound, workers=1)
    wall_one = time.monotonic() - t0
    assert four is not None and one is not None
    spinners = frozenset(f"spin_{i}" for i in range(4))
    assert four.unmeasured_binaries == one.unmeasured_binaries == spinners
    assert four.hung_tests == one.hung_tests == frozenset(
        f"spin_{i}::t_spin" for i in range(4))
    assert four.dead == one.dead
    assert wall_one >= 4 * bound - 0.1, (
        f"the serial control pays one bound per spinning binary: {wall_one:.2f}s")
    assert wall_four < 2 * bound + 0.9, (
        f"four workers pay the bound once for four spinners: {wall_four:.2f}s")


def test_c_single_test_runs_carry_their_own_bound(
        monkeypatch, tmp_path: Path) -> None:
    """`exact_timeout` bounds the `--exact` processes and nothing else; unset,
    they carry the suite bound (no behaviour change until the printed
    longest-completing-run reading sizes it)."""
    project = _project(tmp_path)
    calls = _spin_env(monkeypatch, tmp_path, spinners=1, fast=1)
    slice_ = measure_coverage_slice(project, "stable", enumerate_fns=_enumerate,
                                    timeout=1, workers=1, exact_timeout=7)
    assert slice_ is not None
    assert {t for c, t in calls if "--exact" in c} == {7.0}
    assert {t for c, t in calls if c[1:] == ["--test-threads=1"]} == {1.0}
    monkeypatch.delenv("PHASE_C_EXACT_TEST_TIMEOUT", raising=False)
    calls = _spin_env(monkeypatch, tmp_path, spinners=1, fast=1)
    notes: list[str] = []
    measure_coverage_slice(project, "stable", enumerate_fns=_enumerate,
                           timeout=1, workers=1, log=notes.append)
    assert {t for c, t in calls if "--exact" in c} == {1.0}
    assert any("longest completing run" in n and "single-test bound 1s" in n
               for n in notes), notes
