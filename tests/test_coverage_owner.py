"""Pass-A coverage adapter: skip without llvm; map a region back to its fn.

The I/O path is isolated (own CARGO_TARGET_DIR, staged copy). These tests
drive the parser and the skip/success branches without touching a live
cluster tree.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from pipeline_core.adapters import coverage_owner as cov
from pipeline_core.adapters.coverage_owner import (
    _body_counts,
    _cov_env,
    _fn_at,
    _merge_export,
    _region_counts,
    _stage,
    _test_binaries,
    measure_coverage_slice,
)
from pipeline_core.domain.models import FnLocator


def test_measure_returns_none_when_llvm_tools_are_missing(
        monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cov.shutil, "which", lambda _: None)
    assert measure_coverage_slice(tmp_path, "stable") is None


def test_fn_at_walks_up_from_a_body_line_to_the_declaration(
        tmp_path: Path) -> None:
    src = tmp_path / "src" / "lib.rs"
    src.parent.mkdir()
    src.write_text("fn format_err() {\n    let x = 1;\n}\n", encoding="utf-8")
    assert _fn_at(tmp_path, "src/lib.rs", 2) == "format_err"
    assert _fn_at(tmp_path, "src/lib.rs", 1) == "format_err"
    assert _fn_at(tmp_path, "src/missing.rs", 1) == "?"


def test_stage_copies_the_filled_tree_and_workspace_tests(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    filled = project / ".skeleton-filled" / "stable"
    (filled / "src").mkdir(parents=True)
    (filled / "src" / "lib.rs").write_text("fn x() {}\n", encoding="utf-8")
    (filled / "Cargo.toml").write_text("[package]\nname='t'\n", encoding="utf-8")
    (project / "tests").mkdir()
    (project / "tests" / "t.rs").write_text("// t\n", encoding="utf-8")
    (project / "rust-toolchain.toml").write_text("[toolchain]\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    tree = _stage(project, "stable", work)
    assert (tree / "src" / "lib.rs").read_text(encoding="utf-8") == "fn x() {}\n"
    assert (tree / "Cargo.toml").exists()
    assert (tree / "tests" / "t.rs").exists()
    assert (tree / "rust-toolchain.toml").exists()


def test_cov_env_points_instrumentation_at_the_scratch_target(tmp_path: Path) -> None:
    env = _cov_env(tmp_path, "/tmp/p.profraw")
    assert env["CARGO_TARGET_DIR"] == str(tmp_path / "target-cov")
    assert env["RUSTFLAGS"] == "-C instrument-coverage"
    assert env["LLVM_PROFILE_FILE"] == "/tmp/p.profraw"


def test_test_binaries_skips_metadata_and_keeps_executables(tmp_path: Path) -> None:
    deps = tmp_path / "target-cov" / "debug" / "deps"
    deps.mkdir(parents=True)
    exe = deps / "t-abc"
    exe.write_text("x", encoding="utf-8")
    exe.chmod(0o755)
    (deps / "t-abc.d").write_text("d", encoding="utf-8")
    (deps / "libx.rlib").write_text("r", encoding="utf-8")
    assert _test_binaries(tmp_path) == [exe]
    assert _test_binaries(tmp_path / "missing") == []


def test_test_binaries_skips_nightly_on_a_stable_cluster(
        tmp_path: Path) -> None:
    deps = tmp_path / "target-cov" / "debug" / "deps"
    deps.mkdir(parents=True)
    nightly = deps / "behaviour_nightly_gen_1-abc"
    nightly.write_text("x", encoding="utf-8")
    nightly.chmod(0o755)
    stable = deps / "test_fmt-def"
    stable.write_text("x", encoding="utf-8")
    stable.chmod(0o755)
    assert _test_binaries(tmp_path, "stable") == [stable]
    got = _test_binaries(tmp_path, "nightly")
    assert nightly in got and stable in got


def _export(tree: Path, count: int, regions: list) -> dict:
    path = str(tree / "src" / "lib.rs")
    return {"data": [{"functions": [{
        "filenames": [path],
        "regions": regions,
        "count": count,
    }]}]}


def test_body_and_region_counts_key_on_src_relative_paths(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "src").mkdir(parents=True)
    (tree / "src" / "lib.rs").write_text("fn f() {}\n", encoding="utf-8")
    export = _export(tree, 4, [[3, 1, 5, 1, 4], [4, 1, 4, 8, 0]])
    bodies = _body_counts(export, tree)
    assert bodies[("src/lib.rs", 3)] == 4
    lines = _region_counts(export, tree)
    assert lines[("src/lib.rs", 3)] == 4
    assert lines[("src/lib.rs", 4)] == 4
    assert lines[("src/lib.rs", 5)] == 4


def test_body_counts_skip_foreign_files_and_empty_regions(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "src").mkdir(parents=True)
    export = {"data": [{"functions": [
        {"filenames": ["/other/src/x.rs"], "regions": [[1, 1, 1, 1, 1]],
         "count": 1},
        {"filenames": [str(tree / "src" / "lib.rs")], "regions": [],
         "count": 1},
        {"filenames": [], "regions": [[1, 1, 1, 1, 1]], "count": 1},
    ]}]}
    assert _body_counts(export, tree) == {}


def test_merge_export_returns_none_on_empty_or_failed_tools(
        monkeypatch, tmp_path: Path) -> None:
    dest = tmp_path / "all.json"
    assert _merge_export([], [], dest, "llvm-profdata", "llvm-cov") is None

    def _fail(cmd, cwd, env=None):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="err")

    monkeypatch.setattr(cov, "_run", _fail)
    assert _merge_export(
        [tmp_path / "a.profraw"], [], dest, "llvm-profdata", "llvm-cov") is None


def test_merge_export_parses_json_and_rejects_non_objects(
        monkeypatch, tmp_path: Path) -> None:
    dest = tmp_path / "all.json"
    calls: list[list[str]] = []

    def _ok(cmd, cwd, env=None):
        calls.append(cmd)
        if cmd[0] == "llvm-profdata":
            Path(cmd[-1]).write_bytes(b"pd")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"data": []}), stderr="")

    monkeypatch.setattr(cov, "_run", _ok)
    parsed = _merge_export(
        [tmp_path / "a.profraw"], [tmp_path / "bin"], dest,
        "llvm-profdata", "llvm-cov")
    assert parsed == {"data": []}
    assert any(c[0] == "llvm-cov" for c in calls)

    def _not_obj(cmd, cwd, env=None):
        if cmd[0] == "llvm-profdata":
            Path(cmd[-1]).write_bytes(b"pd")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="[1]", stderr="")

    monkeypatch.setattr(cov, "_run", _not_obj)
    assert _merge_export(
        [tmp_path / "a.profraw"], [], dest, "llvm-profdata", "llvm-cov") is None

    def _bad_json(cmd, cwd, env=None):
        if cmd[0] == "llvm-profdata":
            Path(cmd[-1]).write_bytes(b"pd")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="not-json", stderr="")

    def _empty_out(cmd, cwd, env=None):
        if cmd[0] == "llvm-profdata":
            Path(cmd[-1]).write_bytes(b"pd")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="   ", stderr="")

    monkeypatch.setattr(cov, "_run", _empty_out)
    assert _merge_export(
        [tmp_path / "a.profraw"], [], dest, "llvm-profdata", "llvm-cov") is None


def _filled_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    filled = project / ".skeleton-filled" / "stable"
    src = filled / "src"
    src.mkdir(parents=True)
    (src / "lib.rs").write_text(
        "fn live() {\n    let a = 1;\n    let b = 2;\n}\n"
        "fn dead() {\n    let z = 0;\n}\n",
        encoding="utf-8")
    (filled / "Cargo.toml").write_text("[package]\nname='t'\n", encoding="utf-8")
    return project


def test_measure_returns_none_when_the_filled_tree_is_missing(
        monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cov.shutil, "which", lambda n: "/usr/bin/" + n)
    assert measure_coverage_slice(tmp_path, "stable") is None


def test_measure_returns_none_when_the_staged_tree_does_not_build(
        monkeypatch, tmp_path: Path) -> None:
    project = _filled_project(tmp_path)
    monkeypatch.setattr(cov.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(
        cov, "_run",
        lambda cmd, cwd, env=None: subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="error"))
    notes: list[str] = []
    assert measure_coverage_slice(
        project, "stable", log=notes.append) is None
    assert any("does not build" in n for n in notes)


def test_measure_returns_none_when_no_test_binaries_exist(
        monkeypatch, tmp_path: Path) -> None:
    project = _filled_project(tmp_path)
    monkeypatch.setattr(cov.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(
        cov, "_run",
        lambda cmd, cwd, env=None: subprocess.CompletedProcess(
            cmd, 0, stdout="", stderr=""))
    monkeypatch.setattr(cov, "_test_binaries", lambda *_a, **_k: [])
    assert measure_coverage_slice(project, "stable") is None


def test_measure_returns_none_when_the_export_is_empty(
        monkeypatch, tmp_path: Path) -> None:
    project = _filled_project(tmp_path)
    monkeypatch.setattr(cov.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(
        cov, "_run",
        lambda cmd, cwd, env=None: subprocess.CompletedProcess(
            cmd, 0, stdout="", stderr=""))
    monkeypatch.setattr(
        cov, "_test_binaries", lambda *a, **k: [a[0] / "bin"])
    monkeypatch.setattr(cov, "_merge_export", lambda *a, **k: None)
    notes: list[str] = []
    assert measure_coverage_slice(
        project, "stable", log=notes.append) is None
    assert any("no whole-suite export" in n for n in notes)


def test_measure_names_dead_bodies_and_unreachable_lines(
        monkeypatch, tmp_path: Path) -> None:
    project = _filled_project(tmp_path)
    monkeypatch.setattr(cov.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(
        cov, "_run",
        lambda cmd, cwd, env=None: subprocess.CompletedProcess(
            cmd, 0, stdout="", stderr=""))
    monkeypatch.setattr(
        cov, "_test_binaries", lambda *a, **k: [a[0] / "bin"])

    def _fake_export(prof_files, objects, dest, profdata_bin, cov_bin):
        tree = dest.parent.parent / "tree"
        src = str(tree / "src" / "lib.rs")
        return {"data": [{"functions": [
            {"filenames": [src], "count": 3,
             "regions": [[1, 1, 2, 1, 3], [3, 1, 3, 8, 0]]},
            {"filenames": [src], "count": 0,
             "regions": [[5, 1, 7, 1, 0]]},
        ]}]}

    monkeypatch.setattr(cov, "_merge_export", _fake_export)
    notes: list[str] = []
    slice_ = measure_coverage_slice(project, "stable", log=notes.append)
    assert slice_ is not None
    assert FnLocator("src/lib.rs", "dead") in slice_.dead
    assert FnLocator("src/lib.rs", "live") not in slice_.dead
    assert any("never reached" in s for s in slice_.dead_regions.values()), (
        slice_.dead_regions)
    assert any("dead body" in n for n in notes)


def test_measure_does_not_mark_a_locator_dead_when_a_same_name_body_is_live(
        monkeypatch, tmp_path: Path) -> None:
    """Adapter path of the ambiguous-key contract: two `backtrace` bodies,
    one executed, must not put the locator in the dead set the ranking reads."""
    project = tmp_path / "proj"
    filled = project / ".skeleton-filled" / "stable"
    src = filled / "src"
    src.mkdir(parents=True)
    (src / "error.rs").write_text(
        "impl Error {\n"
        "    fn backtrace() {\n"
        "        let a = 1;\n"
        "    }\n"
        "}\n"
        "impl ErrorImpl {\n"
        "    fn backtrace() {\n"
        "        let z = 0;\n"
        "    }\n"
        "}\n",
        encoding="utf-8")
    (filled / "Cargo.toml").write_text(
        "[package]\nname='t'\n", encoding="utf-8")
    monkeypatch.setattr(cov.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(
        cov, "_run",
        lambda cmd, cwd, env=None: subprocess.CompletedProcess(
            cmd, 0, stdout="", stderr=""))
    monkeypatch.setattr(
        cov, "_test_binaries", lambda *a, **k: [a[0] / "bin"])

    def _fake_export(prof_files, objects, dest, profdata_bin, cov_bin):
        tree = dest.parent.parent / "tree"
        path = str(tree / "src" / "error.rs")
        return {"data": [{"functions": [
            {"filenames": [path], "count": 8,
             "regions": [[2, 1, 4, 1, 8]]},
            {"filenames": [path], "count": 0,
             "regions": [[7, 1, 9, 1, 0]]},
        ]}]}

    monkeypatch.setattr(cov, "_merge_export", _fake_export)
    slice_ = measure_coverage_slice(project, "stable")
    assert slice_ is not None
    loc = FnLocator("src/error.rs", "backtrace")
    assert loc not in slice_.dead
    assert loc not in slice_.dead_regions


def test_measure_logs_and_returns_none_on_oserror(
        monkeypatch, tmp_path: Path) -> None:
    project = _filled_project(tmp_path)
    monkeypatch.setattr(cov.shutil, "which", lambda n: "/usr/bin/" + n)

    def _boom(*_a, **_k):
        raise OSError("disk")

    monkeypatch.setattr(cov, "_stage", _boom)
    notes: list[str] = []
    assert measure_coverage_slice(
        project, "stable", log=notes.append) is None
    assert any("probe failed" in n for n in notes)
