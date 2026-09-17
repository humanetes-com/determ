"""Integration: the expected-test count must equal what cargo reports when a
documented item has cfg-gated copies.

Real cargo and rustdoc, the pipeline's own cargo runner and verdict parser, the
real syn extractor and the real inventory walk. No model.

The class: a multi-cluster merge keeps one `#[cfg]`-gated copy of every
divergent item per cluster, and the doc comment travels with each copy.
rustdoc collects doc tests only from the copy that compiles under ITS cfg set,
and cargo hands a cluster's `RUSTFLAGS=--cfg N` to rustc but not to rustdoc.
The whole-file doc scan counted every copy's examples under every cluster, so
run `185710-f6135a93` (2026-09-17) expected 160 tests where `stable` reported
152 (150 passed, 2 ignored, rc=0), graded the clean cluster incomplete, and
ended the run non-converged.

The crate is invented and generated per run: two gated copies of one documented
fn with different example counts, one `#[test]`, and two cluster envs — no
flags, and the cfg delivered only through RUSTFLAGS. If cargo starts forwarding
RUSTFLAGS cfgs to rustdoc, `test_premise_rustdoc_does_not_read_rustflags`
fails and says so: `rustdoc_cfg_sets` was derived from that behaviour.

Gated on `INTEGRATION=1` (plain cargo, no dependencies);
`scripts/run_integration.sh` is the launcher.
"""
from __future__ import annotations

import dataclasses
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from pipeline_core.adapters.cargo import CargoCli
from pipeline_core.adapters.extractor import CliExtractor
from pipeline_core.adapters.structure import CliStructureReader
from pipeline_core.adapters.structure_cache import StructureCache
from pipeline_core.application.reconstruct.cargo_runner import run_cargo
from pipeline_core.application.test_count import (
    TestCountDeps as _CountDeps,
)
from pipeline_core.application.test_count import count_active_tests_for_cluster
from pipeline_core.domain.models import BuildCluster
from pipeline_core.domain.test_results import parse_test_counts

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("INTEGRATION") != "1",
                       reason="real cargo + rustdoc; set INTEGRATION=1"),
]

_REPO = Path(__file__).resolve().parents[2]
_EXTRACTOR = (_REPO / "tools" / "skeleton_extractor" / "target" / "release"
              / "skeleton-extractor")
_TARGET = Path(tempfile.gettempdir()) / "determ-integration-target" / "doc-copies"
_CFG = "gadget_alt"

_CARGO_TOML = """[package]
name = "gadgets"
version = "0.1.0"
edition = "2021"

[lints.rust]
unexpected_cfgs = { level = "allow" }
"""

_GATE = f'any({_CFG}, feature = "{_CFG}")'
_LIB = f"""#[cfg(not({_GATE}))]
/// Base copy.
/// ```
/// assert!(gadgets::size() > 0);
/// ```
pub fn size() -> u8 {{
    1
}}

#[cfg({_GATE})]
/// Alternate copy.
/// ```
/// let _ = gadgets::size();
/// ```
/// ```
/// assert!(gadgets::size() > 0);
/// ```
pub fn size() -> u8 {{
    2
}}

#[cfg(test)]
mod tests {{
    #[test]
    fn sized() {{
        assert!(super::size() > 0);
    }}
}}
"""


def _crate(tmp_path: Path) -> Path:
    root = tmp_path / "gadgets"
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(_CARGO_TOML)
    (root / "src" / "lib.rs").write_text(_LIB)
    return root


def _clusters() -> tuple[BuildCluster, BuildCluster]:
    base = BuildCluster(name="base", env_extra={}, tarpaulin_exclude_files=[],
                        excluded_cfg=[_CFG], active_cfg=[],
                        prompt_nightly_rule="", is_default=True)
    alt = BuildCluster(name="alt", env_extra={"RUSTFLAGS": f"--cfg {_CFG}"},
                       tarpaulin_exclude_files=[], excluded_cfg=[],
                       active_cfg=[_CFG], prompt_nightly_rule="",
                       is_default=False)
    return base, alt


def _reported(root: Path, cluster: BuildCluster) -> tuple[int, str]:
    """What the cluster's `cargo test` reports, read the way task3 reads it."""
    env = dict(os.environ)
    env.pop("RUSTFLAGS", None)
    env.pop("RUSTDOCFLAGS", None)
    env.update(cluster.env_extra or {})
    env["CARGO_TARGET_DIR"] = str(_TARGET / cluster.name)
    _rc, out = run_cargo(CargoCli(), "cargo test --offline --no-fail-fast",
                         root, env, timeout=600)
    passed, failed, ignored = parse_test_counts(out)
    return passed + failed + ignored, out


def _deps(*, doc_records: bool) -> _CountDeps:
    assert _EXTRACTOR.exists(), (
        f"{_EXTRACTOR} missing — build it (bash pre_run.sh does) before the "
        "integration tests; they read the real extractor")
    ext = CliExtractor(_EXTRACTOR)
    st = CliStructureReader(ext)
    cache = StructureCache(enumerate_fns=st.enumerate_fns,
                           enumerate_mods=ext.enumerate_mods,
                           collect_impl_blocks=lambda _p: None,
                           log=lambda _m: None)
    deps = _CountDeps(enumerate_fns=st.enumerate_fns,
                      mod_records=cache.mod_records,
                      file_cfg_predicate=cache.file_cfg_predicate,
                      is_gen_tests_file=lambda _n: False,
                      enumerate_doc_items=st.enumerate_doc_items)
    if doc_records:
        return deps
    return dataclasses.replace(deps, enumerate_doc_items=lambda _p: None)


def _doc_test_count(out: str) -> int:
    """rustdoc names each doc test `<file> - <item> (line N)`; cargo prints
    the `Doc-tests` header on stderr and the verdict lines on stdout, so the
    names are the reading that does not depend on interleaving."""
    return sum(1 for line in out.splitlines()
               if line.startswith("test src/") and " - " in line)


def test_premise_rustdoc_does_not_read_rustflags(tmp_path: Path) -> None:
    root = _crate(tmp_path)
    _base, alt = _clusters()
    _n, out = _reported(root, alt)
    doc = _doc_test_count(out)
    assert doc == 1, (
        f"rustdoc ran {doc} doc test(s) under RUSTFLAGS=--cfg {_CFG}; the base "
        "copy has 1 and the alternate 2. If this is 2, cargo now forwards "
        "RUSTFLAGS cfgs to rustdoc and `rustdoc_cfg_sets` must be re-derived.\n"
        + out[-2000:])


@pytest.mark.parametrize("which", ["base", "alt"])
def test_expected_count_equals_what_cargo_reports(tmp_path: Path, which: str) -> None:
    root = _crate(tmp_path)
    cluster: Any = dict(zip(("base", "alt"), _clusters(), strict=True))[which]
    reported, out = _reported(root, cluster)
    expected = count_active_tests_for_cluster(root, cluster, _deps(doc_records=True))
    assert expected == reported, (expected, reported, out[-2000:])


def test_negative_control_the_whole_file_scan_overcounts(tmp_path: Path) -> None:
    root = _crate(tmp_path)
    for cluster in _clusters():
        reported, out = _reported(root, cluster)
        expected = count_active_tests_for_cluster(
            root, cluster, _deps(doc_records=False))
        assert expected > reported, (cluster.name, expected, reported, out[-2000:])
