"""Integration: the compile check builds a cluster tree in every world, and a
body that only one world can build is refused with the other world's
diagnostics.

Real cargo and rustc, the pipeline's cargo runner and cluster env. No model.

The class, run `292191-d60a5308` (2026-10-02): six constructors the default
cluster accepted read items that exist only where its cfg is off. Its own
`cargo check` read 0 errors; the same tree under the other cluster's flags
read 13. The other cluster seeded those bodies, re-stubbed them at entry and
refilled them.

The fix rests on the toolchain, pinned by `test_premise_*`: the two builds of
one source report different diagnostics (an unguarded read of a cfg-gated
field is clean where the field exists and E0560/E0609 where it does not), in
cargo's JSON stream, from a second target directory, without disturbing the
first build's verdict.

The crate is invented: a struct with one field only the default world has,
and a constructor written three ways.

Gated on `INTEGRATION=1` (plain cargo, no dependencies);
`scripts/run_integration.sh` is the launcher.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from pipeline_core.adapters.cargo import CargoCli, cluster_cargo_env
from pipeline_core.application.reconstruct.cargo_runner import (
    PortBoundCargoRunner,
    WorldChecks,
)
from pipeline_core.domain.cluster_env import world_key
from pipeline_core.domain.models import BuildCluster

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("INTEGRATION") != "1",
                       reason="real cargo; set INTEGRATION=1"),
]

_CFG = "gadget_alt"

_CARGO_TOML = """[package]
name = "gadgets"
version = "0.1.0"
edition = "2021"

[lints.rust]
unexpected_cfgs = { level = "allow" }
"""

_TYPES = f"""pub struct Table {{
    pub open: u8,
    #[cfg(not({_CFG}))]
    pub trace: u8,
}}
"""

# Written while shown one world: reads the default-only field unguarded.
_ONE_WORLD = _TYPES + """
pub fn build() -> Table {
    Table { open: 1, trace: 2 }
}
"""
# Written for the crate: the field's initialiser carries the field's gate.
_EVERY_WORLD = _TYPES + f"""
pub fn build() -> Table {{
    Table {{
        open: 1,
        #[cfg(not({_CFG}))]
        trace: 2,
    }}
}}
"""
_BROKEN_AT_HOME = _TYPES + """
pub fn build() -> Table {
    Table { open: missing_value }
}
"""


def _crate(tmp_path: Path, lib: str) -> Path:
    root = tmp_path / "gadgets"
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(_CARGO_TOML)
    (root / "src" / "lib.rs").write_text(lib)
    return root


def _clusters() -> list[BuildCluster]:
    return [
        BuildCluster(name="base", env_extra={}, tarpaulin_exclude_files=[],
                     excluded_cfg=[_CFG], active_cfg=[], prompt_nightly_rule="",
                     is_default=True),
        BuildCluster(name="alt", env_extra={"RUSTFLAGS": f"--cfg {_CFG}"},
                     tarpaulin_exclude_files=[], excluded_cfg=[],
                     active_cfg=[_CFG], prompt_nightly_rule="", is_default=False),
    ]


def _runner(*, every_world: bool) -> PortBoundCargoRunner:
    clusters = _clusters()
    return PortBoundCargoRunner(CargoCli(), worlds=WorldChecks(
        clusters=lambda: clusters, cluster_env=cluster_cargo_env,
        world_key=world_key, log=lambda _m: None) if every_world else None)


def _check(root: Path, runner: PortBoundCargoRunner, world: str) -> tuple[int, str, list]:
    cluster = {c.name: c for c in _clusters()}[world]
    return runner.check_diag("cargo check --offline", root,
                             cluster_cargo_env(cluster), 600)


def test_premise_one_source_builds_clean_in_one_world_and_not_in_the_other(
        tmp_path: Path) -> None:
    root = _crate(tmp_path, _ONE_WORLD)
    home = _check(root, _runner(every_world=False), "base")
    other = _check(root, _runner(every_world=False), "alt")
    assert home[0] == 0, home[1][-1500:]
    assert other[0] != 0 and any(d.code == "E0560" for d in other[2]), (
        "an unguarded initialiser of a field gated out of this world must be "
        "E0560 in cargo's JSON diagnostics; the every-world check reads the "
        "class from exactly that\n" + other[1][-1500:])


def test_a_one_world_body_is_refused_from_its_own_world_with_the_other_worlds_errors(
        tmp_path: Path) -> None:
    root = _crate(tmp_path, _ONE_WORLD)
    rc, text, diags = _check(root, _runner(every_world=True), "base")
    assert rc != 0
    assert text.startswith(
        "The tree compiles in cfg world `base` and FAILS in cfg world `alt`")
    assert [d.code for d in diags] == ["E0560"]
    assert diags[0].file == "src/lib.rs" and diags[0].line > 0
    assert "the `alt` cfg world's build of the SAME source" in diags[0].rendered
    # NEGATIVE CONTROL: the home-world-only check keeps it.
    assert _check(root, _runner(every_world=False), "base")[0] == 0
    # ...and the home build is still clean after the other world's run.
    assert (root / "target" / "worlds" / "alt").is_dir()


def test_a_body_that_carries_the_field_gate_builds_in_every_world(
        tmp_path: Path) -> None:
    root = _crate(tmp_path, _EVERY_WORLD)
    for world in ("base", "alt"):
        rc, text, _d = _check(root, _runner(every_world=True), world)
        assert rc == 0, text[-1500:]


def test_a_tree_broken_at_home_reports_its_home_errors_only(tmp_path: Path) -> None:
    root = _crate(tmp_path, _BROKEN_AT_HOME)
    rc, text, diags = _check(root, _runner(every_world=True), "base")
    assert rc != 0 and "FAILS in cfg world" not in text
    assert not (root / "target" / "worlds").exists()
    assert all("cfg world's build" not in d.rendered for d in diags)
