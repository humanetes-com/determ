"""Integration: the exercise runner hands grcov the one binary that ran.

Real cargo, real grcov, the pipeline's `exercises_by_coverage` and its reader
of cargo's JSON messages (`domain.cargo_artifacts`). No model.

The class: task 0d's exercise step gave grcov `--binary-path target/debug`
for every spec, and grcov loads every binary under that path. On the
workspace of run `42057-641e589c` (2026-10-25) that directory held 438
executables in 7.3 GB; one profraw took 43.8 s there and 0.4 s with its own
binary, against a live median of 37.7 s per spec, about 250 of t0d's 520
minutes. A directory that only grows across launches makes the step slower
every launch without any change to the code.

The crate is invented: one function a test calls, one a probe program calls,
one nothing calls. Asserted: each spec's grcov call names a FILE, the one
cargo reported for that target, and the units attributed are exactly the
function that spec ran. Negative control: grcov over the debug directory,
the pre-change argument, attributes the same hit lines, so the one binary
loses nothing the directory had.

`test_premise_cargo_names_test_and_example_executables` fails loudly if
cargo stops reporting `executable` on a `compiler-artifact` message for a
test target under `--no-run` or for an example under `cargo run` (fresh or
rebuilt): the runner would fall back to the directory and say so with
`HEALTH ‼ cargo named no binary`.

Gated on `INTEGRATION=1` and on grcov being installed;
`scripts/run_integration.sh` is the launcher.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from pipeline_core.adapters.behaviour_exercises import (
    exercises_by_coverage,
    grcov_binary,
    grcov_command,
    instrumented_env,
)
from pipeline_core.adapters.behaviour_probe import subprocess_runner, t0d_target_dir
from pipeline_core.domain.behaviour import (
    BehaviourSpec,
    BehaviourSubKind,
    ObservableOutcome,
)
from pipeline_core.domain.behaviour_coverage import coveralls_hit_lines
from pipeline_core.domain.cargo_artifacts import executables_from_messages
from pipeline_core.domain.models import BuildCluster

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("INTEGRATION") != "1",
                       reason="real cargo and grcov; set INTEGRATION=1"),
    pytest.mark.skipif(grcov_binary() is None, reason="grcov is not installed"),
]

_CRATE = "exgrcov_demo"
_LIB = (
    "pub fn hit() -> u32 {\n"
    "    1\n"
    "}\n"
    "pub fn probed() -> u32 {\n"
    "    2\n"
    "}\n"
    "pub fn untouched() -> u32 {\n"
    "    3\n"
    "}\n"
)
_TEST = f"#[test]\nfn calls_hit() {{\n    assert_eq!({_CRATE}::hit(), 1);\n}}\n"
_PROBE = f"fn main() {{\n    println!(\"{{}}\", {_CRATE}::probed());\n}}\n"


def _crate() -> Path:
    root = Path(tempfile.gettempdir()) / "determ-integration-target" / "exercise-grcov"
    if root.exists():
        shutil.rmtree(root / "src", ignore_errors=True)
        shutil.rmtree(root / "tests", ignore_errors=True)
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "Cargo.toml").write_text(
        f'[package]\nname = "{_CRATE}"\nversion = "0.1.0"\nedition = "2021"\n',
        encoding="utf-8")
    (root / "src" / "lib.rs").write_text(_LIB, encoding="utf-8")
    (root / "tests" / "t.rs").write_text(_TEST, encoding="utf-8")
    return root


def _cluster() -> BuildCluster:
    return BuildCluster(name="stable", env_extra={}, tarpaulin_exclude_files=[],
                        excluded_cfg=[], active_cfg=[], prompt_nightly_rule="")


def _records() -> list[dict[str, object]]:
    b = _LIB.encode("utf-8")
    out: list[dict[str, object]] = []
    for name in ("hit", "probed", "untouched"):
        start = b.index(f"pub fn {name}".encode())
        out.append({"file": "src/lib.rs", "fn_name": name, "sig_byte_start": start,
                    "body_byte_end": b.index(b"}\n", start) + 1, "is_test": False})
    return out


def _specs() -> list[BehaviourSpec]:
    return [
        BehaviourSpec("testassert::tests/t.rs::calls_hit::0", "", "hit()",
                      ObservableOutcome.display("1"), subkind=BehaviourSubKind.OUTCOME,
                      source_ref="tests/t.rs::calls_hit"),
        BehaviourSpec("probe::stable::probed::0", _PROBE, "probed",
                      ObservableOutcome.stdout("2"), subkind=BehaviourSubKind.OUTCOME),
    ]


def test_each_spec_is_read_through_the_binary_cargo_named() -> None:
    project = _crate()
    calls: list[list[str]] = []

    def recording(cmd: list[str], cwd: Path, env: dict[str, str], timeout: int):
        calls.append(list(cmd))
        return subprocess_runner(cmd, cwd, env, timeout)

    logs: list[str] = []
    got, _n = exercises_by_coverage(project, _cluster(), _specs(), _records(),
                                    run=recording, log=logs.append)
    assert got == {
        "testassert::tests/t.rs::calls_hit::0": frozenset({"src/lib.rs::hit"}),
        "probe::stable::probed::0": frozenset({"src/lib.rs::probed"}),
    }, logs
    bins = [Path(c[c.index("--binary-path") + 1]) for c in calls if c[0].endswith("grcov")]
    assert len(bins) == 2
    assert all(p.is_file() for p in bins), bins
    debug = t0d_target_dir(project, _cluster()) / "debug"
    assert bins[0].parent == debug / "deps" and bins[0].name.startswith("t-")
    assert bins[1].parent == debug / "examples"
    assert not any("HEALTH" in ln for ln in logs), logs


def test_negative_control_the_directory_reads_the_same_lines() -> None:
    """The pre-change argument (the debug directory) and the one binary give
    the same hit lines for one profraw."""
    project = _crate()
    grcov = grcov_binary()
    assert grcov is not None
    cluster = _cluster()
    env = instrumented_env(cluster, project)
    prof = t0d_target_dir(project, cluster) / "coverage" / "negative_control"
    shutil.rmtree(prof, ignore_errors=True)
    prof.mkdir(parents=True)
    build = subprocess.run(["cargo", "test", "--no-run", "--quiet", "--message-format=json"],
                           cwd=project, env={**env, "LLVM_PROFILE_FILE": str(prof.parent / "b-%p-%m.profraw")},
                           capture_output=True, text=True, timeout=600)
    assert build.returncode == 0, build.stderr[-800:]
    exe = executables_from_messages(build.stdout)[("test", "t")]
    run = subprocess.run(["cargo", "test", "--quiet", "--test", "t", "--", "--exact", "calls_hit"],
                         cwd=project, env={**env, "LLVM_PROFILE_FILE": str(prof / "%p-%m.profraw")},
                         capture_output=True, text=True, timeout=600)
    assert run.returncode == 0, run.stderr[-800:]
    readings = []
    for binary in (Path(exe), t0d_target_dir(project, cluster) / "debug"):
        out_json = prof.parent / f"{binary.name}.json"
        rc = subprocess.run(grcov_command(grcov, prof, project, out_json, binary_path=binary),
                            cwd=project, capture_output=True, text=True, timeout=300).returncode
        assert rc == 0
        readings.append(coveralls_hit_lines(json.loads(out_json.read_text(encoding="utf-8"))))
    assert readings[0] == readings[1]
    assert readings[0].get("src/lib.rs") == [1, 2, 3]


def test_premise_cargo_names_test_and_example_executables() -> None:
    project = _crate()
    env = instrumented_env(_cluster(), project)
    env["LLVM_PROFILE_FILE"] = os.devnull
    build = subprocess.run(["cargo", "test", "--no-run", "--quiet", "--message-format=json"],
                           cwd=project, env=env, capture_output=True, text=True, timeout=600)
    assert build.returncode == 0, build.stderr[-800:]
    named = executables_from_messages(build.stdout)
    assert ("test", "t") in named and Path(named[("test", "t")]).is_file(), (
        "cargo --no-run --message-format=json no longer names the test executable")
    (project / "examples").mkdir(exist_ok=True)
    (project / "examples" / "premise.rs").write_text(_PROBE, encoding="utf-8")
    try:
        for _fresh in (False, True):
            run = subprocess.run(
                ["cargo", "run", "--quiet", "--message-format=json", "--example", "premise"],
                cwd=project, env=env, capture_output=True, text=True, timeout=600)
            assert run.returncode == 0, run.stderr[-800:]
            exe = executables_from_messages(run.stdout).get(("example", "premise"))
            assert exe and Path(exe).is_file(), (
                "cargo run --message-format=json no longer names the example executable")
            assert "2" in run.stdout.splitlines(), "the program's own stdout is interleaved"
    finally:
        shutil.rmtree(project / "examples", ignore_errors=True)
