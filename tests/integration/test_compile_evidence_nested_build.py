"""Integration: a test that rebuilds the crate must not put bodies on the
compile frontier.

Real cargo, real trybuild, the pipeline's own cargo runner (its stdout/stderr
layout), the real syn extractor and the real attribution reader. No model.

The class: a compile-fail harness inside the suite rebuilds the crate as a
dependency of a scratch project and drops the caller's `RUSTFLAGS`, so a crate
that compiles under the cluster's `--cfg` fails to compile in the nested build,
and cargo relays those diagnostics into the sweep's capture after `running 1
test`. Run `603120-24aabfc5` (2026-09-16) put six constructors on the frontier
under `<compile>` that way, on a tree with 150 of 152 tests passing.

The crate is invented and generated per run. Its one shape is a struct field
gated on a cfg the sweep sets, so the nested build (without the cfg) needs a
field the literal does not initialise. If cargo or trybuild changes so the
nested build no longer fails, or its diagnostics stop landing after the first
`running` line, `test_premise_the_nested_build_still_prints_after_running`
fails and says so: the cut in `extract_tdd_compile_frontier` was derived from
that shape and has to be re-derived, not silently trusted.

Gated on `INTEGRATION=1`: the first run builds trybuild and its dependencies
(offline, from the local registry cache); later runs reuse a target directory
under the system temp dir. `scripts/run_integration.sh` is the launcher.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from pipeline_core.adapters.cargo import CargoCli
from pipeline_core.adapters.extractor import CliExtractor
from pipeline_core.adapters.structure import CliStructureReader
from pipeline_core.application.reconstruct import attribution as attr
from pipeline_core.application.reconstruct.cargo_runner import run_cargo
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.test_results import parse_rustc_compile_errors

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("INTEGRATION") != "1",
                       reason="real cargo + trybuild; set INTEGRATION=1"),
]

_REPO = Path(__file__).resolve().parents[2]
_EXTRACTOR = (_REPO / "tools" / "skeleton_extractor" / "target" / "release"
              / "skeleton-extractor")
_TARGET = Path(tempfile.gettempdir()) / "determ-integration-target" / "nested-build"
_CFG = "widget_extra"

_CARGO_TOML = """[package]
name = "widgets"
version = "0.1.0"
edition = "2021"

[dev-dependencies]
trybuild = "1"

[lints.rust]
unexpected_cfgs = { level = "allow" }
"""

_LIB = f"""pub struct Widget {{
    pub a: u8,
    #[cfg(not({_CFG}))]
    pub b: u8,
}}

pub fn make_widget() -> Widget {{
    Widget {{ a: 1 }}
}}

pub fn other() -> u8 {{
    0
}}
"""

_BASIC = """#[test]
fn builds() {
    assert_eq!(widgets::make_widget().a, 1);
}
"""

_HARNESS = """#[test]
fn ui() {
    let t = trybuild::TestCases::new();
    t.compile_fail("tests/ui/*.rs");
}
"""

_UI_CASE = """fn main() {
    let _: u8 = widgets::other() + "x";
}
"""


def _crate(tmp_path: Path) -> Path:
    root = tmp_path / "widgets"
    (root / "src").mkdir(parents=True)
    (root / "tests" / "ui").mkdir(parents=True)
    (root / "Cargo.toml").write_text(_CARGO_TOML)
    (root / "src" / "lib.rs").write_text(_LIB)
    (root / "tests" / "basic.rs").write_text(_BASIC)
    (root / "tests" / "compiletest.rs").write_text(_HARNESS)
    (root / "tests" / "ui" / "bad.rs").write_text(_UI_CASE)
    return root


def _sweep(root: Path, rustflags: str | None) -> str:
    """One sweep as the loop captures it: the pipeline's cargo runner, so the
    stdout/stderr layout is the one the attribution reads live."""
    env = dict(os.environ)
    env.pop("RUSTFLAGS", None)
    if rustflags is not None:
        env["RUSTFLAGS"] = rustflags
    env["CARGO_TARGET_DIR"] = str(_TARGET / ("cfg" if rustflags else "plain"))
    _rc, out = run_cargo(CargoCli(), "cargo test --offline --no-fail-fast",
                         root, env, timeout=900)
    return out


def _deps() -> attr.AttributionDeps:
    assert _EXTRACTOR.exists(), (
        f"{_EXTRACTOR} missing — build it (bash pre_run.sh does) before the "
        "integration tests; they read the real extractor")
    return attr.AttributionDeps(
        structure=CliStructureReader(CliExtractor(_EXTRACTOR)),
        classify=lambda **_k: {},
        locate_fn=lambda *_a, **_k: None,
        run_check_diag=lambda *_a, **_k: (0, "", []),
        derive_check_only_cmd=lambda c: c,
        is_gen_tests_file=lambda _f: False,
        log=lambda _m: None,
    )


@pytest.fixture(scope="module")
def nested(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    root = _crate(tmp_path_factory.mktemp("nested"))
    return root, _sweep(root, f"--cfg {_CFG}")


def test_premise_the_nested_build_still_prints_after_running(
        nested: tuple[Path, str]) -> None:
    _root, out = nested
    assert "test builds ... ok" in out, (
        "the crate itself must compile and its ordinary test pass under the "
        "sweep's cfg — otherwise this is not the nested-build shape:\n" + out[-4000:])
    diags = [d for d in parse_rustc_compile_errors(out)
             if d.file.endswith("src/lib.rs")]
    assert diags, (
        "the nested build no longer reports a src/ diagnostic — trybuild or "
        "cargo changed; re-derive the build-phase cut before trusting it:\n"
        + out[-4000:])
    first_running = min(i for i, ln in enumerate(out.splitlines())
                        if ln.strip().startswith("running "))
    at = next(i for i, ln in enumerate(out.splitlines())
              if ln.startswith("error[") and "src/lib.rs" in
              out.splitlines()[i + 1])
    assert at > first_running, (
        "the nested diagnostic now precedes the first `running` line in the "
        "captured layout; `build_phase_output` would keep it")


def test_nested_build_diagnostic_is_not_a_compile_frontier(
        nested: tuple[Path, str]) -> None:
    root, out = nested
    frontier, sites = attr.extract_tdd_compile_frontier(_deps(), out, root)
    assert frontier == {} and sites == {}


def test_negative_control_whole_capture_puts_the_body_on_the_frontier(
        nested: tuple[Path, str], monkeypatch: Any) -> None:
    root, out = nested
    monkeypatch.setattr(attr, "build_phase_output", lambda text: text)
    frontier, _ = attr.extract_tdd_compile_frontier(_deps(), out, root)
    assert frontier == {FnLocator("src/lib.rs", "make_widget"): {"<compile>"}}


def test_a_genuine_red_build_is_still_attributed(tmp_path: Path) -> None:
    root = _crate(tmp_path)
    out = _sweep(root, None)
    assert "running " not in out, "a red build runs no binary:\n" + out[-4000:]
    frontier, _ = attr.extract_tdd_compile_frontier(_deps(), out, root)
    assert frontier == {FnLocator("src/lib.rs", "make_widget"): {"<compile>"}}
