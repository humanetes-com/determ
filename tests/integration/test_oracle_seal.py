"""Integration: an edit to the oracle made outside the pipeline is refused at
the next launch, and sealing leaves the oracle's file modes alone.

The class, 2026-09-21/22, runs `450977-5f48b68a` and `92187-f71f12bf`: a
Claude session copied a cluster tree with `cp -r` for a scratch experiment.
The tree's `tests` is a symlink into the workspace, `cp -r` copied it as a
symlink, and the edit to the "copy" of `tests/test_context.rs` landed in the
oracle. The next launch's task0c checkpoint committed it; the only trace was a
new oracle fingerprint, under which every route-ledger row was foreign and
every clock keyed on the oracle restarted.

The workspace here is invented: a git repo with a `tests/` file, a gen_tests
sibling under `src/`, a baked behaviour suite, a strip-time seal tag, and a
cluster tree whose `tests` is a symlink to the workspace's, as the fill phase
makes it. Real git, real `cp -r`, the real adapters. Negative controls: the
write lands and the fingerprint moves with no drift reading at all (the live
shape), and the pre-seal workspace reads unguarded.

The first cut of the seal also made `tests/**/*.rs` read-only. Run
`190214-8de7c7a5` (2026-09-22) then lost the assertion probe on every
iteration: the probe stages `tests/common/mod.rs` with `copy2`, which carries
the mode, and its next copy onto that read-only stage file failed `EACCES`.
`test_a_mode_preserving_copier_can_restage_after_the_check` is that shape.

Needs git and a POSIX `cp`, no cargo, so it runs in the default suite;
`scripts/run_integration.sh` runs it with the rest.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pipeline_core.adapters.oracle_fingerprint import (
    cluster_oracle_fp,
    oracle_tag,
    workspace_oracle_drift,
)
from pipeline_core.application.orchestrator import check_oracle_seal

pytestmark = [pytest.mark.integration]

_TEST = """\
#[test]
fn t_value() {
    let v = shapes::value();
    assert_eq!(v, 3);
}
"""


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    if shutil.which("git") is None or shutil.which("cp") is None:
        pytest.skip("needs git and cp")
    ws = tmp_path / "ws"
    (ws / "tests").mkdir(parents=True)
    (ws / "src").mkdir()
    (ws / ".skeleton").mkdir()
    (ws / "tests" / "test_value.rs").write_text(_TEST)
    (ws / "src" / "lib.rs").write_text("pub fn value() -> u32 { unimplemented!() }\n")
    (ws / "src" / "lib.gen_tests_1.rs").write_text("#[test]\nfn g() {}\n")
    (ws / ".skeleton" / "_behaviours_main.json").write_text("[]\n")
    _git(ws, "init", "-q")
    _git(ws, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    _git(ws, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm",
         "pipeline: stripped — shapes")
    _git(ws, "tag", oracle_tag("shapes"))
    # The cluster tree as the fill phase makes it: a real `src/` holding a
    # copy of the gen_tests sibling, and `tests` symlinked to the workspace.
    tree = ws / ".skeleton-filled" / "main"
    (tree / "src").mkdir(parents=True)
    shutil.copy2(ws / "src" / "lib.gen_tests_1.rs", tree / "src")
    (tree / "tests").symlink_to(ws / "tests")
    return ws


def _scratch_edit(ws: Path, tmp: Path) -> Path:
    """What the session did: `cp -r` of the tree's children, then an edit
    to the copy's test file."""
    scratch = tmp / "scratch"
    scratch.mkdir()
    tree = ws / ".skeleton-filled" / "main"
    subprocess.run(["cp", "-r", str(tree / "src"), str(tree / "tests"),
                    str(scratch)], check=True)
    target = scratch / "tests" / "test_value.rs"
    target.write_text(target.read_text().replace(
        "let v = shapes::value();",
        'eprintln!("probe"); let v = shapes::value();'))
    return target


def test_premise_cp_r_copies_a_symlink_argument_as_a_symlink(
        workspace: Path, tmp_path: Path) -> None:
    """The class rests on this: if `cp -r` ever dereferences a symlink named
    on its command line, the scratch copy is a copy and nothing here fires."""
    scratch = tmp_path / "p"
    scratch.mkdir()
    subprocess.run(["cp", "-r", str(workspace / ".skeleton-filled" / "main"
                                    / "tests"), str(scratch)], check=True)
    assert (scratch / "tests").is_symlink()


def test_negative_control_the_write_lands_and_moves_the_fingerprint_silently(
        workspace: Path, tmp_path: Path) -> None:
    tree = workspace / ".skeleton-filled" / "main"
    before = cluster_oracle_fp(tree, workspace, "main")
    _scratch_edit(workspace, tmp_path)
    assert "probe" in (workspace / "tests" / "test_value.rs").read_text()
    assert cluster_oracle_fp(tree, workspace, "main") != before


def test_the_seal_names_the_edited_file(workspace: Path, tmp_path: Path) -> None:
    _scratch_edit(workspace, tmp_path)
    drift = workspace_oracle_drift(workspace, "shapes")
    assert drift is not None and not drift.clean
    assert drift.changed == ("tests/test_value.rs",)
    assert drift.added == () and drift.removed == ()


def test_a_reformat_is_not_drift(workspace: Path) -> None:
    p = workspace / "tests" / "test_value.rs"
    p.write_text(p.read_text().replace("    ", "\t"))
    drift = workspace_oracle_drift(workspace, "shapes")
    assert drift is not None and drift.clean


def test_negative_control_unsealed_workspace_reads_unguarded(
        workspace: Path) -> None:
    _git(workspace, "tag", "-d", oracle_tag("shapes"))
    assert workspace_oracle_drift(workspace, "shapes") is None


def _run_deps(ws: Path, lines: list[str], events: list[Any]) -> Any:
    return SimpleNamespace(
        phase_valid=lambda _p, key: key == "strip/shapes",
        oracle_drift=workspace_oracle_drift,
        log=lines.append, emit=lambda name, data=None: events.append((name, data)))


def test_the_launch_check_refuses_a_drifted_oracle(
        workspace: Path, tmp_path: Path) -> None:
    _scratch_edit(workspace, tmp_path)
    lines: list[str] = []
    events: list[Any] = []
    with pytest.raises(RuntimeError, match="tests/test_value.rs"):
        check_oracle_seal(_run_deps(workspace, lines, events), workspace,
                          ["shapes"])
    assert any("HEALTH ‼ ORACLE_DRIFT" in ln for ln in lines)
    assert events and events[0][0] == "pipeline.oracle_drift"


def test_the_launch_check_passes_a_sealed_oracle(workspace: Path) -> None:
    lines: list[str] = []
    check_oracle_seal(_run_deps(workspace, lines, []), workspace, ["shapes"])
    assert any("matches its seal" in ln for ln in lines)


def test_a_mode_preserving_copier_can_restage_after_the_check(
        workspace: Path, tmp_path: Path) -> None:
    """The regression: the launch check and the seal must not change a mode,
    because every `copy2`/`copytree` consumer of the oracle (the probe's
    helper staging, the coverage stage) carries it into a copy it later
    overwrites."""
    check_oracle_seal(_run_deps(workspace, [], []), workspace, ["shapes"])
    src = workspace / "tests" / "test_value.rs"
    assert src.stat().st_mode & 0o200
    stage = tmp_path / "stage"
    stage.mkdir()
    shutil.copy2(src, stage / src.name)
    shutil.copy2(src, stage / src.name)


def test_a_module_not_yet_stripped_is_not_checked(
        workspace: Path, tmp_path: Path) -> None:
    """Before strip the module is still authoring its oracle (a fresh run, or
    a restore to the augmented tag): a difference is not drift."""
    _scratch_edit(workspace, tmp_path)
    deps = _run_deps(workspace, [], [])
    deps.phase_valid = lambda _p, _k: False
    check_oracle_seal(deps, workspace, ["shapes"])
