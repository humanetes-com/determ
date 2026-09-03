"""Pass-A coverage adapter: skip without llvm; map a region back to its fn.

The I/O path is isolated (own CARGO_TARGET_DIR, staged copy). These tests
drive the parser and the skip/success branches without touching a live
cluster tree.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from pipeline_core.adapters import coverage_owner as cov
from pipeline_core.adapters.coverage_owner import (
    _body_counts,
    _cov_env,
    _merge_export,
    _region_counts,
    _stage,
    _test_binaries,
    measure_coverage_slice,
)
from pipeline_core.adapters.extractor import CliExtractor
from pipeline_core.domain.models import FnLocator, FnSite

_BIN = (
    Path(__file__).resolve().parents[1]
    / "tools" / "skeleton_extractor" / "target" / "release" / "skeleton-extractor"
)


def _enumerate(tree: Path) -> list:
    """The real extractor over the staged tree — the enumerator the
    composition root injects; skips when the release binary is absent."""
    if not _BIN.exists():
        pytest.skip("extractor release binary missing")
    return CliExtractor(_BIN).enumerate_fns(tree) or []


def _measure(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
    """`measure_coverage_slice` with the enumerator the composition root
    injects — every call site takes the reading the loop would."""
    return measure_coverage_slice(*args, enumerate_fns=_enumerate, **kwargs)


def test_measure_returns_none_when_llvm_tools_are_missing(
        monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cov.shutil, "which", lambda _: None)
    assert _measure(tmp_path, "stable") is None


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


def test_cov_env_inherits_cluster_rustflags_then_appends_instrumentation(
        tmp_path: Path) -> None:
    """The owner map must see the same cfg world the sweep ran. A replace
    of RUSTFLAGS dropped `--cfg` so a crate-gated test never matched."""
    env = _cov_env(
        tmp_path, "/tmp/p.profraw",
        cluster_env={
            "RUSTFLAGS": "-A warnings --cfg error_generic_member_access",
            "RUSTUP_TOOLCHAIN": "nightly",
        })
    flags = env["RUSTFLAGS"]
    assert "--cfg error_generic_member_access" in flags
    assert "-C instrument-coverage" in flags
    assert env["RUSTUP_TOOLCHAIN"] == "nightly"
    assert env["CARGO_TARGET_DIR"] == str(tmp_path / "target-cov")
    # Isolation: a cluster env that names the loop's target dir must not
    # leak instrumentation into it.
    leaked = _cov_env(
        tmp_path, "/tmp/p.profraw",
        cluster_env={"CARGO_TARGET_DIR": "/loop/target",
                     "RUSTFLAGS": "-A warnings"})
    assert leaked["CARGO_TARGET_DIR"] == str(tmp_path / "target-cov")


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
    assert _measure(tmp_path, "stable") is None


def test_measure_returns_none_when_the_staged_tree_does_not_build(
        monkeypatch, tmp_path: Path) -> None:
    project = _filled_project(tmp_path)
    monkeypatch.setattr(cov.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(
        cov, "_run",
        lambda cmd, cwd, env=None: subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="error"))
    notes: list[str] = []
    assert _measure(project, "stable", log=notes.append) is None
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
    assert _measure(project, "stable") is None


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
    assert _measure(project, "stable", log=notes.append) is None
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
    slice_ = _measure(project, "stable", log=notes.append)
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
        "    fn other() {\n"
        "        let q = 0;\n"
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
            {"filenames": [path], "count": 0,
             "regions": [[10, 5, 12, 6, 0]]},
        ]}]}

    monkeypatch.setattr(cov, "_merge_export", _fake_export)
    slice_ = _measure(project, "stable")
    assert slice_ is not None
    loc = FnLocator("src/error.rs", "backtrace")
    assert loc not in slice_.dead
    assert loc not in slice_.dead_regions
    # Positive control that the indented bodies were resolved at all: the
    # sibling nothing executes IS dead. Without it the assertion above held
    # vacuously on a resolver that named no impl method (2026-09-03).
    assert FnLocator("src/error.rs", "other") in slice_.dead


def test_measure_logs_and_returns_none_on_oserror(
        monkeypatch, tmp_path: Path) -> None:
    project = _filled_project(tmp_path)
    monkeypatch.setattr(cov.shutil, "which", lambda n: "/usr/bin/" + n)

    def _boom(*_a, **_k):
        raise OSError("disk")

    monkeypatch.setattr(cov, "_stage", _boom)
    notes: list[str] = []
    assert _measure(project, "stable", log=notes.append) is None
    assert any("probe failed" in n for n in notes)


# ---- pass C: per-test owner sets ----
# The whole-suite merge says a body ran, not WHICH test ran it, so it can only
# convict a body nothing executes. The per-test profile is what answers "can a
# rewrite of this unit move THIS red test" — the question that cost 4,550 s on
# `src/lib.rs::Ok` for `test_trailer`, run `2450551-9a6a8683`, 2026-08-30.

def _owner_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    src = project / ".skeleton-filled" / "stable" / "src"
    src.mkdir(parents=True)
    (src / "lib.rs").write_text(
        "fn entered() {\n    let a = 1;\n}\n"
        "fn elsewhere() {\n    let z = 0;\n}\n", encoding="utf-8")
    return project


def _owner_env(monkeypatch, claimed: set[str]) -> None:
    """Every binary claims the tests in `claimed` and no others."""
    monkeypatch.setattr(cov.shutil, "which", lambda n: "/usr/bin/" + n)

    def _fake_run(cmd, cwd, env=None):
        filt = cmd[cmd.index("--exact") + 1] if "--exact" in cmd else ""
        if filt and env and "LLVM_PROFILE_FILE" in env:
            Path(env["LLVM_PROFILE_FILE"]).write_text("raw")
        out = "running 1 test\n" if filt in claimed else "running 0 tests\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(cov, "_run", _fake_run)
    monkeypatch.setattr(cov, "_test_binaries", lambda *a, **k: [a[0] / "bin"])


def test_owner_map_names_the_bodies_one_test_enters(
        monkeypatch, tmp_path: Path) -> None:
    project = _owner_project(tmp_path)
    _owner_env(monkeypatch, {"t_one"})

    def _fake_export(prof_files, objects, dest, profdata_bin, cov_bin):
        src = str(dest.parent.parent / "tree" / "src" / "lib.rs")
        return {"data": [{"functions": [
            {"filenames": [src], "count": 4, "regions": [[1, 1, 3, 1, 4]]},
            {"filenames": [src], "count": 0, "regions": [[4, 1, 6, 1, 0]]},
        ]}]}

    monkeypatch.setattr(cov, "_merge_export", _fake_export)
    notes: list[str] = []
    slice_ = _measure(project, "stable",
                                    failing_tests=["t_one"], log=notes.append)
    assert slice_ is not None
    assert slice_.owners["t_one"] == frozenset(
        {FnSite("src/lib.rs", "entered", "")}), slice_.owners
    assert any("owner set for 1/1" in n for n in notes), notes


def test_a_test_no_binary_claims_is_absent_not_empty(
        monkeypatch, tmp_path: Path) -> None:
    """Unmeasured and measured-empty license different answers, so the
    adapter must not collapse them: an absent test decides nothing, while an
    empty owner set would be read as a positive reading."""
    project = _owner_project(tmp_path)
    _owner_env(monkeypatch, set())
    monkeypatch.setattr(
        cov, "_merge_export",
        lambda *a, **k: {"data": [{"functions": []}]})
    slice_ = _measure(project, "stable",
                                    failing_tests=["t_missing"])
    assert slice_ is not None
    assert "t_missing" not in slice_.owners


def test_no_failing_tests_leaves_the_owner_map_empty(
        monkeypatch, tmp_path: Path) -> None:
    """The reading degrades to what it was before pass C existed."""
    project = _owner_project(tmp_path)
    _owner_env(monkeypatch, {"t_one"})
    monkeypatch.setattr(
        cov, "_merge_export",
        lambda *a, **k: {"data": [{"functions": []}]})
    slice_ = _measure(project, "stable")
    assert slice_ is not None
    assert dict(slice_.owners) == {}


def test_owner_map_filters_on_the_cargo_name_not_the_leaf(
        monkeypatch, tmp_path: Path) -> None:
    """`--exact` matches the full cargo name. A generated test
    `gen_tests_2::t_one` is unmeasured if the leaf is what the binary sees,
    and then the gate degrades to pre-fix silence for every qualified name."""
    project = _owner_project(tmp_path)
    seen: list[str] = []
    monkeypatch.setattr(cov.shutil, "which", lambda n: "/usr/bin/" + n)

    def _fake_run(cmd, cwd, env=None):
        filt = cmd[cmd.index("--exact") + 1] if "--exact" in cmd else ""
        if filt:
            seen.append(filt)
        if filt and env and "LLVM_PROFILE_FILE" in env:
            Path(env["LLVM_PROFILE_FILE"]).write_text("raw")
        out = ("running 1 test\n" if filt == "gen_tests_2::t_one"
               else "running 0 tests\n")
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(cov, "_run", _fake_run)
    monkeypatch.setattr(cov, "_test_binaries", lambda *a, **k: [a[0] / "bin"])

    def _fake_export(prof_files, objects, dest, profdata_bin, cov_bin):
        src = str(dest.parent.parent / "tree" / "src" / "lib.rs")
        return {"data": [{"functions": [
            {"filenames": [src], "count": 4, "regions": [[1, 1, 3, 1, 4]]},
        ]}]}

    monkeypatch.setattr(cov, "_merge_export", _fake_export)
    slice_ = _measure(project, "stable", failing_tests=["gen_tests_2::t_one"])
    assert slice_ is not None
    assert "gen_tests_2::t_one" in seen
    assert "t_one" not in seen
    assert "gen_tests_2::t_one" in slice_.owners


def test_two_tests_sharing_a_leaf_do_not_share_a_profile(
        monkeypatch, tmp_path: Path) -> None:
    """The leaf as a filename would clobber: `a::t` then `b::t` write the
    same `one-t.profraw`."""
    project = _owner_project(tmp_path)
    paths: list[str] = []
    monkeypatch.setattr(cov.shutil, "which", lambda n: "/usr/bin/" + n)

    def _fake_run(cmd, cwd, env=None):
        if "--exact" in cmd and env and "LLVM_PROFILE_FILE" in env:
            paths.append(env["LLVM_PROFILE_FILE"])
            Path(env["LLVM_PROFILE_FILE"]).write_text("raw")
        return subprocess.CompletedProcess(cmd, 0, stdout="running 1 test\n",
                                           stderr="")

    monkeypatch.setattr(cov, "_run", _fake_run)
    monkeypatch.setattr(cov, "_test_binaries", lambda *a, **k: [a[0] / "bin"])
    monkeypatch.setattr(
        cov, "_merge_export",
        lambda *a, **k: {"data": [{"functions": []}]})
    slice_ = _measure(project, "stable", failing_tests=["a::t", "b::t"])
    assert slice_ is not None
    stems = {p.rsplit("/", 1)[-1] for p in paths}
    assert stems == {"one-a_t.profraw", "one-b_t.profraw"}, stems


def test_a_generator_of_failing_tests_is_not_counted_as_zero(
        monkeypatch, tmp_path: Path) -> None:
    """The live-validation log line is `owner set for N/N`. Consuming the
    iterable twice would print `1/0` whenever a caller passed a generator."""
    project = _owner_project(tmp_path)
    _owner_env(monkeypatch, {"t_one"})
    monkeypatch.setattr(
        cov, "_merge_export",
        lambda *a, **k: {"data": [{"functions": []}]})
    notes: list[str] = []
    slice_ = _measure(project, "stable",
        failing_tests=(t for t in ["t_one"]), log=notes.append)
    assert slice_ is not None
    assert any("owner set for 1/1" in n for n in notes), notes


def test_the_composition_binding_accepts_failing_tests(
        monkeypatch, tmp_path: Path) -> None:
    """Run 2629191-02835a4c aborted both clusters: composition bound
    `lambda pd, cl` and the driver now passes `failing_tests=`."""
    from pipeline_core.composition import _tdd_measure_coverage

    seen: dict[str, object] = {}

    def _fake(pd, cl, *, failing_tests=(), log=None, cluster_env=None,
              enumerate_fns=None):
        seen["failing"] = failing_tests
        seen["log"] = log
        seen["cluster_env"] = cluster_env
        seen["enumerate_fns"] = enumerate_fns
        return None

    monkeypatch.setattr(
        "pipeline_core.composition._measure_coverage_slice", _fake)
    notes: list[str] = []
    logger = notes.append
    bound = _tdd_measure_coverage(logger, _enumerate)
    cluster = {"RUSTUP_TOOLCHAIN": "nightly",
               "RUSTFLAGS": "-A warnings --cfg X"}
    assert bound(tmp_path, "stable", failing_tests=frozenset({"t"}),
                 cluster_env=cluster) is None
    # The extractor port the fill routes use is the one the probe names
    # bodies with — one enumeration, one identity, one cfg world.
    assert seen["enumerate_fns"] is _enumerate
    assert seen["failing"] == frozenset({"t"})
    assert seen["log"] is logger
    assert seen["cluster_env"] == cluster
    # Negative control: the two-argument lambda that shipped is the crash.
    old = lambda pd, cl: None  # noqa: E731
    with pytest.raises(TypeError, match="failing_tests"):
        old(tmp_path, "stable", failing_tests=frozenset({"t"}))
