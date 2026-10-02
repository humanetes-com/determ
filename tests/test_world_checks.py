"""The compile check answers for the crate, not for one build world.

Run `292191-d60a5308` (2026-10-02), anyhow: six constructors the `stable`
cluster accepted read items that exist only in `stable`'s cfg world. Its own
check read 0 errors; the same tree under the `nightly` world read 13, all in
those bodies. `nightly` re-stubbed and refilled them (41 calls, 263
model-minutes). Every check call goes through `PortBoundCargoRunner.check_diag`,
so that is where the other worlds are built. Invented clusters and output.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from pipeline_core.adapters.cargo import Process
from pipeline_core.application.reconstruct.cargo_runner import (
    PortBoundCargoRunner,
    WorldChecks,
    cmd_for_world,
)
from pipeline_core.domain.cluster_env import WORLD_VAR, overlay_cluster_env, world_key

_BASE = SimpleNamespace(name="base", env_extra={}, requires_nightly=False, toolchain=None)
_ALT = SimpleNamespace(name="alt", env_extra={"RUSTFLAGS": "--cfg alt"},
                       requires_nightly=True, toolchain="nightly")
_FEATURE = SimpleNamespace(name="feature/x", env_extra={}, requires_nightly=False,
                           toolchain=None)


def _env(cluster: SimpleNamespace | None) -> dict[str, str]:
    env = {"RUSTFLAGS": "-A warnings"}
    overlay_cluster_env(env, cluster)
    return env


def _error(code: str, file: str, line: int, text: str) -> str:
    return json.dumps({"reason": "compiler-message", "message": {
        "level": "error", "code": {"code": code}, "message": text,
        "rendered": f"error[{code}]: {text}\n --> {file}:{line}:1\n",
        "spans": [{"file_name": file, "line_start": line, "is_primary": True}]}})


class _Cargo:
    """Fails in the worlds named in `red`, records every call."""

    def __init__(self, red: dict[str, int], timeout_in: str = "") -> None:
        self.red, self.timeout_in = red, timeout_in
        self.calls: list[tuple[str, str, str]] = []

    def run(self, cmd: str, cwd: Path, *, env: dict[str, str], timeout: int = 180) -> Process:
        world = env.get(WORLD_VAR, "")
        self.calls.append((world, cmd, env.get("CARGO_TARGET_DIR", "")))
        if self.timeout_in and world == self.timeout_in:
            return Process(-1, "", "TIMEOUT")
        if world in self.red:
            return Process(self.red[world],
                           _error("E0425", "src/build.rs", 7, "cannot find value `only_base`"),
                           "")
        return Process(0, "", "")


def _runner(cargo: _Cargo, clusters: list[SimpleNamespace],
            log: list[str] | None = None) -> PortBoundCargoRunner:
    sink = log if log is not None else []
    return PortBoundCargoRunner(cargo, worlds=WorldChecks(  # type: ignore[arg-type]
        clusters=lambda: clusters, cluster_env=_env, world_key=world_key,
        log=sink.append))


def test_a_tree_that_builds_at_home_is_built_in_the_other_world_and_its_failure_returned(
        tmp_path: Path) -> None:
    """The plan's sentence. NEGATIVE CONTROL: the same runner with no worlds
    bound — the pre-fix gate — reads the tree as clean."""
    cargo = _Cargo(red={"alt": 101})
    rc, text, diags = _runner(cargo, [_BASE, _ALT]).check_diag(
        "cargo check", tmp_path, _env(_BASE))
    assert rc == 101
    assert [w for w, _c, _t in cargo.calls] == ["base", "alt"]
    assert text.startswith("The tree compiles in cfg world `base` and FAILS in cfg world `alt`")
    assert [(d.code, d.file, d.line) for d in diags] == [("E0425", "src/build.rs", 7)]
    assert "this is the `alt` cfg world's build of the SAME source" in diags[0].rendered

    control = _Cargo(red={"alt": 101})
    assert PortBoundCargoRunner(control).check_diag(  # type: ignore[arg-type]
        "cargo check", tmp_path, _env(_BASE))[0] == 0
    assert [w for w, _c, _t in control.calls] == ["base"]


def test_the_other_world_runs_under_its_toolchain_env_and_own_target_dir(
        tmp_path: Path) -> None:
    cargo = _Cargo(red={})
    assert _runner(cargo, [_BASE, _ALT]).check_diag(
        "cargo check --all-targets", tmp_path, _env(_BASE))[0] == 0
    (_w0, cmd0, tgt0), (_w1, cmd1, tgt1) = cargo.calls
    assert cmd0.startswith("cargo check") and cmd1.startswith("cargo +nightly check")
    assert tgt0 == "" and tgt1 == str(tmp_path / "target" / "worlds" / "alt")
    # ...and from the cfg world back to the default one the selector is dropped.
    cargo = _Cargo(red={})
    _runner(cargo, [_BASE, _ALT]).check_diag("cargo +nightly check", tmp_path, _env(_ALT))
    assert [c.split(" --message")[0] for _w, c, _t in cargo.calls] == [
        "cargo +nightly check", "cargo check"]


def test_a_tree_that_fails_at_home_is_not_built_elsewhere(tmp_path: Path) -> None:
    cargo = _Cargo(red={"base": 101, "alt": 101})
    rc, text, _d = _runner(cargo, [_BASE, _ALT]).check_diag(
        "cargo check", tmp_path, _env(_BASE))
    assert rc == 101 and "FAILS in cfg world" not in text
    assert [w for w, _c, _t in cargo.calls] == ["base"]


def test_an_env_built_for_no_cluster_or_one_world_is_checked_once(tmp_path: Path) -> None:
    cargo = _Cargo(red={"alt": 101})
    assert _runner(cargo, [_BASE, _ALT]).check_diag(
        "cargo check", tmp_path, _env(None))[0] == 0
    assert _runner(cargo, [_BASE]).check_diag("cargo check", tmp_path, _env(_BASE))[0] == 0
    assert [w for w, _c, _t in cargo.calls] == ["", "base"]


def test_a_cluster_with_the_same_cargo_run_is_not_a_second_world(tmp_path: Path) -> None:
    cargo = _Cargo(red={"feature/x": 101})
    assert _runner(cargo, [_BASE, _FEATURE]).check_diag(
        "cargo check", tmp_path, _env(_BASE))[0] == 0
    assert [w for w, _c, _t in cargo.calls] == ["base"]


def test_no_reading_in_the_other_world_leaves_the_home_verdict_and_says_so(
        tmp_path: Path) -> None:
    log: list[str] = []
    cargo = _Cargo(red={}, timeout_in="alt")
    assert _runner(cargo, [_BASE, _ALT], log).check_diag(
        "cargo check", tmp_path, _env(_BASE))[0] == 0
    assert any("no reading in cfg world `alt`" in line for line in log)


def test_cmd_for_world_swaps_only_the_toolchain_selector() -> None:
    assert cmd_for_world("cargo check --lib", _ALT) == "cargo +nightly check --lib"
    assert cmd_for_world("cargo +nightly test --no-run", _BASE) == "cargo test --no-run"
    assert cmd_for_world("cargo +stable check", _ALT) == "cargo +nightly check"


def test_no_fill_route_takes_a_compile_verdict_around_the_check_port() -> None:
    """The every-world build lives in `check_diag`. A route that runs the
    check command through the plain `run` gets a one-world verdict and can
    keep a body the other world cannot build — the legacy per-fn test-fix
    re-check did (`self.cargo.run(check_cmd, …)`) until 2026-10-02. No call
    in the reconstruct package may hand a check command to `run`."""
    import ast

    root = Path(__file__).resolve().parents[1] / "pipeline_core" / "application" / "reconstruct"
    offenders: list[str] = []
    for path in sorted(root.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("run", "run_cargo") and node.args):
                continue
            first = node.args[0]
            # A bare name (`check_cmd`) or an attribute (`ctx.check_cmd`).
            name = (first.id if isinstance(first, ast.Name)
                    else first.attr if isinstance(first, ast.Attribute) else "")
            if "check" in name.lower():
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"compile verdict taken through a plain run: {offenders}"
