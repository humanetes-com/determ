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
flags, and the cfg delivered through RUSTFLAGS.

The second class, run `292191-d60a5308` (2026-10-02): because that cfg reached
rustc only, a cluster ran the doc tests of the OTHER world, and a doc example
its crate gates on the cfg compiled as an empty shell and passed — on the
reconstruction, which had lost the behaviour it asserts, and on the reference.
Every cluster env now goes through `domain.cluster_env.overlay_cluster_env`,
which hands rustdoc the cfgs it hands rustc, and the reference floor gates a
doc test that runs and fails. `_reported` builds its env through that rule;
`_reported_rustflags_only` is the pre-fix env and the negative control. If
cargo starts forwarding RUSTFLAGS cfgs to rustdoc,
`test_premise_rustdoc_does_not_read_rustflags` fails and says so: the overlay
would then deliver each cfg twice and can be removed.

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
from pipeline_core.domain.cluster_env import overlay_cluster_env
from pipeline_core.domain.models import BuildCluster
from pipeline_core.domain.reference_floor import (
    ClusterReference,
    judge_against_reference,
    pairs_of,
)
from pipeline_core.domain.same_failure import FailedReading, ReferenceFailure
from pipeline_core.domain.test_results import (
    parse_binary_results,
    parse_test_counts,
)

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


def _run(root: Path, cluster: BuildCluster, *, overlay: bool) -> tuple[int, str]:
    env = dict(os.environ)
    env.pop("RUSTFLAGS", None)
    env.pop("RUSTDOCFLAGS", None)
    if overlay:
        overlay_cluster_env(env, cluster)
    else:
        env.update(cluster.env_extra or {})
    env["CARGO_TARGET_DIR"] = str(
        _TARGET / root.name / (cluster.name + ("" if overlay else "-rustc-only")))
    _rc, out = run_cargo(CargoCli(), "cargo test --offline --no-fail-fast",
                         root, env, timeout=600)
    passed, failed, ignored = parse_test_counts(out)
    return passed + failed + ignored, out


def _reported(root: Path, cluster: BuildCluster) -> tuple[int, str]:
    """What the cluster's `cargo test` reports under the env the pipeline
    builds for it, read the way task3 reads it."""
    return _run(root, cluster, overlay=True)


def _reported_rustflags_only(root: Path, cluster: BuildCluster) -> tuple[int, str]:
    """The same under the cluster's stored `env_extra` alone — the env every
    cluster cargo ran under until 2026-10-02."""
    return _run(root, cluster, overlay=False)


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
    _n, out = _reported_rustflags_only(root, alt)
    doc = _doc_test_count(out)
    assert doc == 1, (
        f"rustdoc ran {doc} doc test(s) under RUSTFLAGS=--cfg {_CFG}; the base "
        "copy has 1 and the alternate 2. If this is 2, cargo now forwards "
        "RUSTFLAGS cfgs to rustdoc and `domain.cluster_env` no longer has to "
        "deliver them.\n" + out[-2000:])


def test_the_cluster_env_runs_the_doc_tests_of_its_own_world(tmp_path: Path) -> None:
    root = _crate(tmp_path)
    _base, alt = _clusters()
    _n, out = _reported(root, alt)
    assert _doc_test_count(out) == 2, out[-2000:]


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


# --- a doc example gated on the cluster's cfg ------------------------------

def _gated_lib(value: int) -> str:
    """`probe()` returns `value`; its one example asserts it returns 1 and is
    wrapped so that without the cfg its whole body is a `stringify!` argument."""
    return f"""/// ```
#[cfg_attr(not({_CFG}), doc = "# _ = stringify! {{")]
/// assert_eq!(gadgets::probe(), 1);
#[cfg_attr(not({_CFG}), doc = "# }};")]
/// ```
pub fn probe() -> u8 {{
    {value}
}}
"""


def _gated_crate(tmp_path: Path, name: str, value: int) -> Path:
    root = tmp_path / name / "gadgets"
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(_CARGO_TOML)
    (root / "src" / "lib.rs").write_text(_gated_lib(value))
    return root


def _floor(tmp_path: Path, reporter: Any) -> Any:
    """The reference floor's ruling on a tree whose `probe()` is wrong (2),
    against a reference whose `probe()` is right (1), both run by `reporter`
    under the cfg cluster."""
    _base, alt = _clusters()
    _n, ref_out = reporter(_gated_crate(tmp_path, "reference", 1), alt)
    _n, our_out = reporter(_gated_crate(tmp_path, "ours", 2), alt)
    r_pass, r_fail, r_ign, r_inc = pairs_of(parse_binary_results(ref_out))
    o_pass, o_fail, _i, _c = pairs_of(parse_binary_results(our_out))
    assert r_pass and not r_fail, ref_out[-2000:]
    ref = ClusterReference(cluster="alt", passed=r_pass, failures=dict.fromkeys(r_fail, ReferenceFailure()),
                           ignored=r_ign, incomplete=r_inc)
    return judge_against_reference(ref, o_pass, FailedReading(pairs=o_fail)), our_out


def test_a_wrong_body_fails_the_floor_through_its_cfg_gated_doc_example(
        tmp_path: Path) -> None:
    verdict, out = _floor(tmp_path, _reported)
    assert verdict.met is False, out[-2000:]
    assert any("probe" in name for _label, name in verdict.unexcused_failures)


def test_negative_control_without_the_cfg_at_rustdoc_the_example_is_a_shell(
        tmp_path: Path) -> None:
    verdict, out = _floor(tmp_path, _reported_rustflags_only)
    assert verdict.met is True, out[-2000:]
    assert not verdict.unexcused_failures and not verdict.unmet


# --- the line rustdoc names a doc test by, which the doc stand-in join reads ---
#
# The reference floor matches each of the original's doc tests to the
# re-hosted example that stands for it in the oracle (`domain.doc_stand_in`)
# by the line rustdoc names the doc test with. The join assumes rustdoc's rule:
# the item's first doc line plus the opening fence's line in the joined doc
# text, and, for a doc written inside a `macro_rules!` body, the expansion's
# line plus that offset. Measured 2026-11-02 on the pristine anyhow tree: 24
# of 26 doc tests at record line plus offset, the two in the `__ensure` macro
# body at its call site plus offset. If rustdoc changes the rule, the premise
# below fails before the floor reports a covered doc test absent, or a wrong
# one covered.

_LINES_LIB = """//! Crate doc.
//!
//! ```
//! assert_eq!(gadgets::one(), 1);
//! ```

/// Plain.
///
/// ```
/// assert_eq!(gadgets::one(), 1);
/// ```
pub fn one() -> u8 {
    1
}

/// Split by an attribute.
#[inline]
///
/// ```
/// assert_eq!(gadgets::two(), 2);
/// ```
pub fn two() -> u8 {
    2
}

macro_rules! documented {
    ($item:item) => {
        /// Made by a macro.
        ///
        /// ```
        /// assert_eq!(gadgets::three(), 3);
        /// ```
        ///
        /// ```ignore
        /// not compiled
        /// ```
        ///
        /// ```no_run
        /// let _ = gadgets::three();
        /// ```
        $item
    };
}

documented! {
    pub fn three() -> u8 {
        3
    }
}
"""


def _lines_crate(tmp_path: Path) -> Path:
    root = tmp_path / "gadgets"
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(_CARGO_TOML)
    (root / "src" / "lib.rs").write_text(_LINES_LIB)
    return root


def _rustdoc_lines(root: Path) -> dict[tuple[str, str], tuple[str, int]]:
    """The reference's own reading: `cargo test --doc` through the real
    runner and parser, normalized pairs to (file, line)."""
    from pipeline_core.application.reference_run import _doc_lines
    env = dict(os.environ)
    env.pop("RUSTFLAGS", None)
    env.pop("RUSTDOCFLAGS", None)
    env["CARGO_TARGET_DIR"] = str(_TARGET / "doc-lines")
    _rc, out = run_cargo(CargoCli(), "cargo test --offline --doc", root, env, timeout=600)
    return _doc_lines(parse_binary_results(out))


def _origins(root: Path, *, macro_flag: bool) -> list[Any]:
    from pipeline_core.domain.doc_example_test import doc_fences
    from pipeline_core.domain.doc_stand_in import DocOrigin
    records = _deps(doc_records=True).enumerate_doc_items(root) or []
    out = []
    for rec in records:
        for i, f in enumerate(doc_fences(rec.doc)):
            out.append(DocOrigin(rec.file, rec.line, i, f.offset,
                                 rec.in_macro_body and macro_flag,
                                 kept_as=f"doc_lib_gen_{rec.line}_{i}.rs", test_fn="t"))
    return out


def _first_doc_line(marker: str) -> int:
    return _LINES_LIB.splitlines().index(marker) + 1


#: Each rustdoc doc test of the crate, by normalized name, and the record it
#: must join: (the record's first doc line, the fence's index in that doc).
_EXPECTED = {
    "src/lib.rs -#0": ("//! Crate doc.", 0),
    "src/lib.rs - one#0": ("/// Plain.", 0),
    "src/lib.rs - two#0": ("/// Split by an attribute.", 0),
    "src/lib.rs - three#0": ("        /// Made by a macro.", 0),
    "src/lib.rs - three#1": ("        /// Made by a macro.", 1),
    "src/lib.rs - three#2": ("        /// Made by a macro.", 2),
}


def test_premise_rustdoc_numbers_a_doc_test_from_its_first_doc_line_plus_offset(
        tmp_path: Path) -> None:
    """The crate doc, a plain doc and a doc split by `#[inline]` are named at
    the record line plus the fence offset in the joined text. A doc in a
    `macro_rules!` body is named from the `documented!` call site plus the
    same offset, `ignore` and `no_run` fences included: rustdoc numbers both
    (`ignored`, `compile`), and libtest's ignored names reach the reading."""
    root = _lines_crate(tmp_path)
    lines = _rustdoc_lines(root)
    assert sorted(b for _a, b in lines) == sorted(_EXPECTED), lines
    call_site = _first_doc_line("documented! {")
    for o in _origins(root, macro_flag=True):
        want = [b for b, (marker, index) in _EXPECTED.items()
                if _first_doc_line(marker) == o.item_line and index == o.index]
        assert len(want) == 1, o
        (_file, line), = [v for (_a, b), v in lines.items() if b == want[0]]
        base = call_site if o.in_macro_body else o.item_line
        assert line == base + o.offset, (o, line)


def test_every_doc_test_joins_its_own_example_and_the_macro_rule_is_load_bearing(
        tmp_path: Path) -> None:
    from pipeline_core.domain.doc_stand_in import join_origins
    root = _lines_crate(tmp_path)
    lines = _rustdoc_lines(root)
    joined = join_origins(lines, _origins(root, macro_flag=True))
    assert len(joined) == len(_EXPECTED)
    for (_a, name), o in joined.items():
        marker, index = _EXPECTED[name]
        assert (o.item_line, o.index) == (_first_doc_line(marker), index), (name, o)
    # NEGATIVE CONTROL: a record that does not say its doc is in a macro body
    # is joined at its own line, which rustdoc does not use, so none of the
    # macro's three fences join.
    unflagged = join_origins(lines, _origins(root, macro_flag=False))
    assert sorted(b for _a, b in unflagged) == [
        "src/lib.rs - one#0", "src/lib.rs - two#0", "src/lib.rs -#0"]
