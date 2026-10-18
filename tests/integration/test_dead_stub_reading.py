"""Integration: the end gate reads rustc's own dead-code analysis to tell a
stub the finished tree cannot reach from one it can.

Real cargo and rustc, the pipeline's cargo env, the real syn extractor, the
real stub scan and the end gate's classifier. No model.

The class, run `292191-d60a5308` (2026-10-02): every test passed and the run
exited 1 on two private helpers still `unimplemented!()`. The filled callers
routed around both — the only text naming one was a macro no body expands —
so no test could reach them and no fill of them could be judged. rustc reports
exactly that set as never used; the gate could not read it and judged the
stubs by the original's execution record instead.

The fix rests on three behaviours of the toolchain, each pinned by a
`test_premise_*` below:
- `--force-warn dead_code` reports over the crate-level `#![allow(dead_code)]`
  every tree this pipeline writes carries, and over `-A warnings` in RUSTFLAGS;
- a `pub fn` in a private module nothing uses is reported, one a public fn
  calls is not;
- the JSON diagnostic's span starts inside the fn's signature, where the
  extractor's `sig_byte_start..body_byte_start` range finds it.

The crate is invented: a private module holding one stub a public fn calls and
one stub named only by a macro nothing expands, plus a module compiled only
under a cfg, holding one more unused stub, and two cluster worlds.

Gated on `INTEGRATION=1` (plain cargo, no dependencies);
`scripts/run_integration.sh` is the launcher.
"""
from __future__ import annotations

import functools
import os
from pathlib import Path

import pytest

from pipeline_core.adapters.cargo import cluster_cargo_env
from pipeline_core.adapters.dead_code import measure_world_dead_code
from pipeline_core.adapters.extractor import CliExtractor
from pipeline_core.adapters.structure import CliStructureReader
from pipeline_core.application.dead_stub_reading import (
    DeadStubDeps,
    read_dead_stub_sites,
)
from pipeline_core.application.orchestrator import (
    classify_remaining_stubs,
    report_stub_verdict,
)
from pipeline_core.application.reconstruct.stub_scan import scan_stub_inventory
from pipeline_core.domain.cfg import CfgSet
from pipeline_core.domain.models import BuildCluster, FnLocator, FnSite

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("INTEGRATION") != "1",
                       reason="real cargo + rustc; set INTEGRATION=1"),
]

_REPO = Path(__file__).resolve().parents[2]
_EXTRACTOR = (_REPO / "tools" / "skeleton_extractor" / "target" / "release"
              / "skeleton-extractor")
_CFG = "gadget_alt"

_CARGO_TOML = """[package]
name = "gadgets"
version = "0.1.0"
edition = "2021"

[lints.rust]
unexpected_cfgs = { level = "allow" }
"""

_LIB = f"""#![allow(unused, dead_code)]
mod helpers;
#[cfg({_CFG})]
mod extras;

macro_rules! lookup_or {{
    ($x:expr) => {{
        $crate::helpers::lookup($x)
    }};
}}

pub fn size() -> u8 {{
    helpers::fetch()
}}
"""

_HELPERS = """#![allow(unused, dead_code)]
pub fn fetch() -> u8 {
    ::core::unimplemented!()
}
pub fn lookup(x: u8) -> u8 {
    ::core::unimplemented!()
}
"""

_EXTRAS = """#![allow(unused, dead_code)]
pub fn spare() -> u8 {
    ::core::unimplemented!()
}
"""

_FETCH = FnSite("src/helpers.rs", "fetch", "")
_LOOKUP = FnSite("src/helpers.rs", "lookup", "")
_SPARE = FnSite("src/extras.rs", "spare", "")


def _crate(tmp_path: Path) -> Path:
    root = tmp_path / "gadgets"
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(_CARGO_TOML)
    (root / "src" / "lib.rs").write_text(_LIB)
    (root / "src" / "helpers.rs").write_text(_HELPERS)
    (root / "src" / "extras.rs").write_text(_EXTRAS)
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


def _structure() -> CliStructureReader:
    assert _EXTRACTOR.exists(), (
        f"{_EXTRACTOR} missing — build it (bash pre_run.sh does) before the "
        "integration tests; they read the real extractor")
    return CliStructureReader(CliExtractor(_EXTRACTOR))


def _deps(log: list[str] | None = None) -> DeadStubDeps:
    st = _structure()
    sink = log if log is not None else []
    return DeadStubDeps(
        enumerate_fns=st.enumerate_fns, enumerate_mods=st.enumerate_mods,
        measure_world=functools.partial(
            measure_world_dead_code, cluster_env=cluster_cargo_env, timeout=600),
        log=sink.append)


def test_premise_force_warn_reports_over_the_crate_level_allow(tmp_path: Path) -> None:
    root = _crate(tmp_path)
    base = _clusters()[0]
    assert "-A warnings" in cluster_cargo_env(base)["RUSTFLAGS"]
    world = measure_world_dead_code(root, base, cluster_env=cluster_cargo_env,
                                    timeout=600)
    assert world.measured, world.reason
    assert world.spans, (
        "rustc reported no dead_code diagnostic for a crate whose private "
        "module holds an unused pub fn, under `#![allow(dead_code)]` and "
        "`-A warnings`. If `--force-warn` no longer overrides those, the end "
        "gate's dead-stub reading sees nothing and must be re-derived.")


def test_premise_the_span_starts_inside_the_fn_signature(tmp_path: Path) -> None:
    root = _crate(tmp_path)
    world = measure_world_dead_code(root, _clusters()[0],
                                    cluster_env=cluster_cargo_env, timeout=600)
    text = (root / "src" / "helpers.rs").read_text()
    sig_start = text.index("pub fn lookup")
    body_open = text.index("{", sig_start)
    inside = [b for f, b in world.spans
              if f == "src/helpers.rs" and sig_start <= b < body_open]
    assert inside, (
        f"no dead_code span in bytes {sig_start}..{body_open} of "
        f"src/helpers.rs; spans: {sorted(world.spans)}. The join to the "
        "extractor's signature range rests on rustc pointing at the fn name.")


def test_the_unused_stub_is_dead_and_the_called_one_is_not(tmp_path: Path) -> None:
    root = _crate(tmp_path)
    dead = read_dead_stub_sites(root, _clusters(), _deps())
    assert _LOOKUP in dead      # named only by a macro nothing expands
    assert _SPARE in dead       # compiled only under the cfg cluster, unused there
    assert _FETCH not in dead   # `size()` is public and calls it


def test_the_gate_excuses_only_the_stubs_the_tree_cannot_reach(tmp_path: Path) -> None:
    root = _crate(tmp_path)
    clusters = _clusters()
    cfg_sets = [CfgSet.of(cfg_true=c.cfg_true, cfg_false=c.cfg_false)
                for c in clusters]
    stubs = scan_stub_inventory(
        root, enumerate_fns=_structure().enumerate_fns).site_preds()
    dead = read_dead_stub_sites(root, clusters, _deps())
    verdict = classify_remaining_stubs(stubs, cfg_sets, (), (), dead)
    assert set(verdict.dead_in_tree) == {
        FnLocator("src/helpers.rs", "lookup"), FnLocator("src/extras.rs", "spare")}
    assert verdict.orphaned == (FnLocator("src/helpers.rs", "fetch"),)
    assert report_stub_verdict(verdict, [].append, len(cfg_sets)) is False

    # NEGATIVE CONTROL: the gate without the reading counts all three.
    control = classify_remaining_stubs(stubs, cfg_sets, (), ())
    assert len(control.orphaned) == 3 and control.dead_in_tree == ()


def test_a_world_that_does_not_build_excuses_nothing(tmp_path: Path) -> None:
    root = _crate(tmp_path)
    # The cfg cluster's own module stops compiling: that world has no reading.
    (root / "src" / "extras.rs").write_text(_EXTRAS + "pub fn broken() -> u8 { }\n")
    log: list[str] = []
    dead = read_dead_stub_sites(root, _clusters(), _deps(log))
    assert _SPARE not in dead
    assert _LOOKUP not in dead   # shared: the broken world compiles it too
    assert any("no dead-code reading" in line for line in log), log


# ---- the oracle's own tests are callers; an exported stub is never dead ----
# Decided 2026-10-17: a crate-private stub no rebuilt caller and no oracle test
# reaches is not counted against green; a public fn, or a stub any oracle test
# or rebuilt caller reaches, still blocks. The plain `cargo check --lib`
# compiles without `cfg(test)`, so a private body only an in-crate test module
# calls read as dead there and was excused.

_KEEPER_LIB = """#![allow(unused, dead_code)]
mod helpers;

pub fn size() -> u8 {
    helpers::fetch()
}

pub fn exported() -> u8 {
    ::core::unimplemented!()
}

#[cfg(test)]
mod checks {
    #[test]
    fn probe_is_one() {
        assert_eq!(crate::helpers::probe(), 1);
    }
}
"""

_KEEPER_HELPERS = """#![allow(unused, dead_code)]
pub fn fetch() -> u8 {
    1
}
pub fn probe() -> u8 {
    ::core::unimplemented!()
}
pub fn routed() -> u8 {
    ::core::unimplemented!()
}
"""


def _keeper(tmp_path: Path) -> Path:
    root = tmp_path / "keeper"
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(_CARGO_TOML.replace("gadgets", "keeper"))
    (root / "src" / "lib.rs").write_text(_KEEPER_LIB)
    (root / "src" / "helpers.rs").write_text(_KEEPER_HELPERS)
    return root


def _plain_and_test_profile(root: Path) -> tuple[set[str], set[str]]:
    from pipeline_core.adapters.process import run_bounded
    from pipeline_core.domain.dead_code import dead_code_spans
    env = cluster_cargo_env(_clusters()[0])
    env["RUSTFLAGS"] = f"{env.get('RUSTFLAGS', '')} --force-warn dead_code"
    env["CARGO_TARGET_DIR"] = str(root / "target" / "premise")
    text = (root / "src" / "helpers.rs").read_text()
    out = []
    for extra in ([], ["--profile", "test"]):
        run = run_bounded(["cargo", "check", "--lib", *extra,
                           "--message-format=json"],
                          cwd=root, env=env, timeout=600.0)
        assert run.returncode == 0, run.stderr
        names = set()
        for f, b in dead_code_spans(run.stdout):
            if f == "src/helpers.rs":
                names.add(text[b:].split("(", 1)[0])
        out.append(names)
    return out[0], out[1]


def test_premise_the_test_profile_counts_an_in_crate_test_as_a_caller(
        tmp_path: Path) -> None:
    """The negative control is the plain reading the gate took before: it
    reports `probe`, which an oracle test calls."""
    plain, test_profile = _plain_and_test_profile(_keeper(tmp_path))
    assert plain == {"probe", "routed"}, plain
    assert test_profile == {"routed"}, (
        f"`cargo check --lib --profile test` reported {test_profile}: if it "
        "no longer compiles the library under cfg(test), the gate cannot see "
        "an oracle test as a caller and must be re-derived.")


def test_only_the_body_nothing_reaches_is_excused(tmp_path: Path) -> None:
    root = _keeper(tmp_path)
    clusters = _clusters()[:1]
    cfg_sets = [CfgSet.of(cfg_true=c.cfg_true, cfg_false=c.cfg_false)
                for c in clusters]
    stubs = scan_stub_inventory(
        root, enumerate_fns=_structure().enumerate_fns).site_preds()
    dead = read_dead_stub_sites(root, clusters, _deps())
    assert dead == {FnSite("src/helpers.rs", "routed", "")}
    verdict = classify_remaining_stubs(stubs, cfg_sets, (), (), dead)
    assert verdict.dead_in_tree == (FnLocator("src/helpers.rs", "routed"),)
    assert set(verdict.orphaned) == {
        FnLocator("src/helpers.rs", "probe"),   # an oracle test reaches it
        FnLocator("src/lib.rs", "exported"),    # public: rustc never calls it dead
    }
    assert report_stub_verdict(verdict, [].append, len(cfg_sets)) is False

    # The same tree with the reached and the public stub filled: the routed
    # private body alone does not fail the run.
    (root / "src" / "helpers.rs").write_text(
        _KEEPER_HELPERS.replace(
            "pub fn probe() -> u8 {\n    ::core::unimplemented!()",
            "pub fn probe() -> u8 {\n    1"))
    (root / "src" / "lib.rs").write_text(
        _KEEPER_LIB.replace(
            "pub fn exported() -> u8 {\n    ::core::unimplemented!()",
            "pub fn exported() -> u8 {\n    2"))
    stubs = scan_stub_inventory(
        root, enumerate_fns=_structure().enumerate_fns).site_preds()
    dead = read_dead_stub_sites(root, clusters, _deps())
    verdict = classify_remaining_stubs(stubs, cfg_sets, (), (), dead)
    assert verdict.orphaned == ()
    assert report_stub_verdict(verdict, [].append, len(cfg_sets)) is True
