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


# --------------------------------------------------------------------------- #
# A cluster tree without its own manifest. Cargo looks for `Cargo.toml` in its
# working directory and then in every ancestor; the cluster trees live inside
# the workspace crate. On every fresh cluster entry the pre-pass heal and the
# seed's compile gate ran before the tree had its manifest and built the
# workspace's stripped shell instead (checkpoint `18fb6a03d6`, the entry of
# run `412606-f903acb4`, replayed 2026-10-17). The runner refuses a cargo
# command in a directory with no manifest of its own.
# --------------------------------------------------------------------------- #

def _nested_tree(tmp_path: Path) -> tuple[Path, Path]:
    """A parent crate that does not compile, and below it a crate tree that
    does but has not been given its manifest yet."""
    parent = _crate(tmp_path, _BROKEN_AT_HOME)
    tree = parent / ".trees" / "base"
    (tree / "src").mkdir(parents=True)
    (tree / "src" / "lib.rs").write_text(_EVERY_WORLD)
    return parent, tree


def test_premise_cargo_climbs_to_the_ancestor_manifest(tmp_path: Path) -> None:
    import subprocess
    _parent, tree = _nested_tree(tmp_path)
    r = subprocess.run(["cargo", "check", "--offline"], cwd=tree,
                       capture_output=True, text=True, errors="replace",
                       timeout=600, env=cluster_cargo_env(_clusters()[0]))
    assert r.returncode != 0
    assert "missing_value" in r.stderr, (
        "cargo run in a directory with no Cargo.toml must build the ancestor "
        "crate; the runner's refusal exists because it does\n" + r.stderr[-1500:])


def test_the_runner_refuses_a_tree_with_no_manifest_of_its_own(
        tmp_path: Path) -> None:
    _parent, tree = _nested_tree(tmp_path)
    rc, text, diags = _check(tree, _runner(every_world=True), "base")
    assert rc == -1 and "NOT A CRATE ROOT" in text, text[-1500:]
    assert diags == [] and "missing_value" not in text
    # Given its manifest, the same tree builds as itself.
    (tree / "Cargo.toml").write_text(_CARGO_TOML)
    assert _check(tree, _runner(every_world=True), "base")[0] == 0


def test_control_without_the_refusal_the_runner_reports_the_parents_errors(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline_core.adapters import process
    monkeypatch.setattr(process, "cargo_manifest_refusal", lambda *_a: None)
    _parent, tree = _nested_tree(tmp_path)
    rc, text, _diags = _check(tree, _runner(every_world=False), "base")
    assert rc != 0 and "missing_value" in text


# --- the TEST verdict of the one-tree loop: every world, one capture ---------
#
# Run `1680589-7a6824f8` (2026-10-19): task 2a's two cluster trees came out
# byte-identical; the per-cluster loop judged each in one world. The one-tree
# loop's env (`cluster_plan.one_tree_cluster`) makes every test run cover
# each world (`PortBoundCargoRunner.run`), and the readers take the joined
# capture. That rests on how cargo lays out a test run: `Running` lines on
# stderr, libtest's sections on stdout, in the same order.

_TESTED = _EVERY_WORLD + f"""
pub fn answer() -> u8 {{
    #[cfg({_CFG})]
    {{ return 2; }}
    #[cfg(not({_CFG}))]
    {{ 1 }}
}}
"""
_TEST_FILE = """#[test]
fn answer_is_one() {
    assert_eq!(gadgets::answer(), 1);
}
"""


def _tested_crate(tmp_path: Path) -> Path:
    root = _crate(tmp_path, _TESTED)
    (root / "tests").mkdir()
    (root / "tests" / "answer.rs").write_text(_TEST_FILE)
    return root


def _loop_env() -> dict[str, str]:
    from pipeline_core.domain.cluster_plan import one_tree_cluster
    return cluster_cargo_env(one_tree_cluster(_clusters()))


def test_premise_one_test_passes_in_one_world_and_fails_in_the_other(
        tmp_path: Path) -> None:
    from pipeline_core.domain.test_results import parse_failed_tests
    root = _tested_crate(tmp_path)
    runner = _runner(every_world=False)
    base = {c.name: c for c in _clusters()}
    rc_b, out_b = runner.run("cargo test --offline --no-fail-fast", root,
                             cluster_cargo_env(base["base"]), 600)
    rc_a, out_a = runner.run("cargo test --offline --no-fail-fast", root,
                             cluster_cargo_env(base["alt"]), 600)
    assert rc_b == 0 and not parse_failed_tests(out_b)
    assert rc_a != 0 and set(parse_failed_tests(out_a)) == {"answer_is_one"}


def test_the_loop_test_run_reads_both_worlds_as_one_capture(tmp_path: Path) -> None:
    from pipeline_core.domain.test_results import (
        parse_binary_results,
        parse_failed_tests,
        parse_test_counts,
        world_sections,
    )
    root = _tested_crate(tmp_path)
    rc, out = _runner(every_world=True).run(
        "cargo test --offline --no-fail-fast", root, _loop_env(), 600)
    assert rc != 0
    assert set(parse_failed_tests(out)) == {"answer_is_one"}
    sections = world_sections(out)
    assert sorted(sections) == ["alt", "base"]
    assert parse_test_counts(sections["base"])[:2] == (1, 0)
    assert parse_test_counts(sections["alt"])[:2] == (0, 1)
    alt = {b.label: b for b in parse_binary_results(sections["alt"])}
    assert any("answer_is_one" in b.failed for b in alt.values())
    # Control: the same env with no worlds bound runs the home world only and
    # reads the tree as green.
    rc_home, out_home = _runner(every_world=False).run(
        "cargo test --offline --no-fail-fast", root, _loop_env(), 600)
    assert rc_home == 0 and not parse_failed_tests(out_home)
