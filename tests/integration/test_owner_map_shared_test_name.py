"""Integration: a test name that fails in two binaries is two failing tests,
and the owner map holds what BOTH executed.

Real cargo, the pipeline's runner (`run_cargo`), the sweep's binary-qualified
verdicts (`parse_binary_results` -> `qualified_verdicts`), the instrumented
coverage reading (`measure_coverage_slice`, llvm tools, the syn extractor).
No model.

The class, run `3893442-cb547388` (2026-09-19): `test_downcast_ref` and
`test_downcast_mut` fail in two test binaries each. The owner map was keyed
by the name and answered it with the first binary that claimed it (or the
crash recovery's run of the later binary), so the red-slice session aimed at
the name got one binary's executed slice while its prompt quoted the other
binary's panic — whose asserted call was a body outside the editable set.
The shape here is invented: `alpha` is executed only by `tests/one.rs::t`,
`beta` only by `tests/two.rs::t`, both tests fail.

Asserted: with the sweep's failing binaries the owner set of `t` holds both
`alpha` and `beta`, and the log names the union. Negative control: the same
reading without them (the pre-fix call) holds exactly one of the two.
Premise: the sweep labels the binaries `tests/one.rs` / `tests/two.rs` and
cargo builds them as targets `one` / `two` — the mapping
`cargo_target_of` relies on.

The keep judge, run `14495-a26ad4ea` (2026-10-15, cluster `stable`, fill
6): `tests/test_repr.rs::test_drop` passed and `tests/test_downcast.rs::
test_drop` was red before and after a `src/error.rs` rewrite that read
`pass 120 -> 127 … no regression`; the joint keep counted `test_drop` newly
red by name and reverted the rewrite three times. Shape here: `t` passes in
`tests/one.rs` and fails in `tests/two.rs` before and after; `g` in
`tests/two.rs` turns green when `gamma` is fixed. Asserted through the real
U2 gate's scoped capture: the keep banks; a fill that also turns
`tests/one.rs::t` red is refused. Negative control: the name-set rule on the
same real readings names `t` newly red. Premise: libtest's OR filter in the
scoped run executes BOTH copies of `t`, so the capture holds a passing and a
failing `t`.

Gated on `INTEGRATION=1`; `scripts/run_integration.sh` is the launcher.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from pipeline_core.adapters.cargo import CargoCli
from pipeline_core.adapters.coverage_owner import ProbeWorld
from pipeline_core.adapters.extractor import CliExtractor
from pipeline_core.adapters.structure import CliStructureReader
from pipeline_core.application.reconstruct.cargo_runner import run_cargo
from pipeline_core.domain.attribution import WriteGround
from pipeline_core.domain.measurement import (
    cargo_target_of,
    failing_binaries_by_name,
    qualified_verdicts,
)
from pipeline_core.domain.test_results import parse_binary_results

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("INTEGRATION") != "1",
                       reason="real cargo + coverage; set INTEGRATION=1"),
]

_REPO = Path(__file__).resolve().parents[2]
_EXTRACTOR = (_REPO / "tools" / "skeleton_extractor" / "target" / "release"
              / "skeleton-extractor")
_TARGET = Path(tempfile.gettempdir()) / "determ-integration-target" / "shared-name"
_CLUSTER = "main"
_CMD = "cargo test --offline --no-fail-fast"

_LIB = """pub fn alpha() -> u8 {
    1
}

pub fn beta() -> u8 {
    1
}
"""

_ONE = """#[test]
fn t() {
    assert_eq!(widgets::alpha(), 2);
}
"""

_TWO = """#[test]
fn t() {
    assert_eq!(widgets::beta(), 2);
}
"""


def _workspace(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    cluster = project / ".skeleton-filled" / _CLUSTER
    (cluster / "src").mkdir(parents=True)
    (cluster / "Cargo.toml").write_text(
        '[package]\nname = "widgets"\nversion = "0.1.0"\nedition = "2021"\n')
    (cluster / "src" / "lib.rs").write_text(_LIB)
    (project / "tests").mkdir()
    (project / "tests" / "one.rs").write_text(_ONE)
    (project / "tests" / "two.rs").write_text(_TWO)
    (cluster / "tests").symlink_to(project / "tests")
    return project


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("RUSTFLAGS", None)
    env["CARGO_TARGET_DIR"] = str(_TARGET)
    env["RUST_BACKTRACE"] = "0"
    return env


def _sweep(project: Path) -> frozenset[tuple[str, str]]:
    cluster = project / ".skeleton-filled" / _CLUSTER
    _rc, out = run_cargo(CargoCli(), _CMD, cluster, _env(), timeout=600)
    _passed, failed, _incomplete = qualified_verdicts(parse_binary_results(out))
    return failed


def _owners(project: Path, failed_pairs):
    from pipeline_core.adapters.coverage_owner import measure_coverage_slice
    logs: list[str] = []
    reading = measure_coverage_slice(
        project, _CLUSTER, failing_tests={"t"},
        worlds=(ProbeWorld("", _env()),),
        enumerate_fns=CliStructureReader(CliExtractor(_EXTRACTOR)).enumerate_fns,
        timeout=300, workers=1, failed_pairs=failed_pairs,
        log=logs.append)
    assert reading is not None, f"the coverage reading could not be taken: {logs}"
    return {s.fn_name for s in reading.owners.get("t", frozenset())}, logs


def _tools_present() -> None:
    if shutil.which("llvm-profdata") is None or shutil.which("llvm-cov") is None:
        pytest.skip("llvm-profdata / llvm-cov not on PATH")
    assert _EXTRACTOR.exists(), "build the release extractor (bash pre_run.sh)"


def test_premise_the_sweep_labels_map_to_the_cargo_targets(tmp_path: Path) -> None:
    project = _workspace(tmp_path)
    failed = _sweep(project)
    assert failed == {("tests/one.rs", "t"), ("tests/two.rs", "t")}
    assert {cargo_target_of(b) for b, _ in failed} == {"one", "two"}


def test_a_name_failing_in_two_binaries_owns_what_both_executed(
        tmp_path: Path) -> None:
    _tools_present()
    project = _workspace(tmp_path)
    failed = _sweep(project)
    assert failing_binaries_by_name(failed) == {"t": frozenset({"one", "two"})}
    owners, logs = _owners(project, failed)
    assert {"alpha", "beta"} <= owners
    assert any("failing in several binaries measured on each" in ln for ln in logs)


def test_negative_control_the_name_alone_measures_one_binary(tmp_path: Path) -> None:
    _tools_present()
    project = _workspace(tmp_path)
    owners, _logs = _owners(project, ())
    assert len(owners & {"alpha", "beta"}) == 1


# --- the keep judge over a name two binaries hold ---------------------------

_KEEP_LIB = """pub fn alpha() -> u8 {{
    {alpha}
}}

pub fn beta() -> u8 {{
    1
}}

pub fn gamma() -> u8 {{
    {gamma}
}}
"""

_KEEP_ONE = """#[test]
fn t() {
    assert_eq!(widgets::alpha(), 1);
}
"""

_KEEP_TWO = """#[test]
fn t() {
    assert_eq!(widgets::beta(), 2);
}

#[test]
fn g() {
    assert_eq!(widgets::gamma(), 2);
}
"""


class _NoRecords:
    def enumerate_fns(self, _src: Path) -> list[dict[str, object]]:
        return []


def _keep_crate(tmp_path: Path, alpha: int, gamma: int) -> Path:
    root = tmp_path / f"keep-{alpha}-{gamma}"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "Cargo.toml").write_text(
        '[package]\nname = "widgets"\nversion = "0.1.0"\nedition = "2021"\n')
    (root / "src" / "lib.rs").write_text(_KEEP_LIB.format(alpha=alpha, gamma=gamma))
    (root / "tests" / "one.rs").write_text(_KEEP_ONE)
    (root / "tests" / "two.rs").write_text(_KEEP_TWO)
    return root


def _baseline(root: Path):
    from pipeline_core.application.reconstruct.cargo_runner import (
        oracle_test_source,
    )
    from pipeline_core.domain.test_report import reading_of_capture
    _rc, out = run_cargo(CargoCli(), _CMD, root, _env(), timeout=600)
    return reading_of_capture(out, sources=oracle_test_source(root))


def _gate(root: Path, baseline):
    from pipeline_core.application.reconstruct.cargo_runner import u2_test_gate

    def _run(cmd: str, cwd: Path, env_: dict[str, str], timeout: int = 600):
        return run_cargo(CargoCli(), cmd, cwd, env_, timeout=timeout)
    return u2_test_gate(
        _run, WriteGround(frozenset({"t", "g"}), frozenset({"t", "g"})),
        frozenset(), _CMD, root, _env(),
        structure=_NoRecords(), enabled=True, timeout=600, err_cap=2000,
        guard=lambda *_: None, baseline=baseline)


def test_premise_the_scoped_run_holds_both_copies_of_the_name(
        tmp_path: Path) -> None:
    from pipeline_core.domain.test_results import TestStateReading
    root = _keep_crate(tmp_path, alpha=1, gamma=1)
    base = _baseline(root)
    assert "t" in base.passed_names and "t" in base.failed_names, (
        "the two copies no longer report one passing and one failing")
    v = _gate(_keep_crate(tmp_path, alpha=1, gamma=2), base)
    after = TestStateReading(parse_binary_results(v.capture)).keyed()
    assert {(k.binary, k.test) for k in after.passed} >= {
        ("tests/one.rs", "t"), ("tests/two.rs", "g")}
    assert ("tests/two.rs", "t") in {(k.binary, k.test) for k in after.failed}


def test_a_gain_beside_a_shared_name_red_before_and_after_banks(
        tmp_path: Path) -> None:
    from pipeline_core.domain.test_report import U2Decision, judge_refill
    base = _baseline(_keep_crate(tmp_path, alpha=1, gamma=1))
    v = _gate(_keep_crate(tmp_path, alpha=1, gamma=2), base)
    assert v.decision is U2Decision.REJECT and v.self_failures == {"t"}
    ji = judge_refill({"t", "g"}, base, v)
    assert ji.new_failures == frozenset() and ji.resolved == {"g"}
    assert ji.banks
    # NEGATIVE CONTROL: the name-set rule the judge read before.
    assert v.self_failures & base.passed_names == {"t"}


def test_turning_the_green_copy_red_is_still_refused(tmp_path: Path) -> None:
    from pipeline_core.domain.test_report import judge_refill
    base = _baseline(_keep_crate(tmp_path, alpha=1, gamma=1))
    v = _gate(_keep_crate(tmp_path, alpha=3, gamma=2), base)
    ji = judge_refill({"t", "g"}, base, v)
    assert ji.new_failures == {"t"} and not ji.non_regression


# --- a test only another build world compiles --------------------------------
#
# Run `1947461-b4eb65c3` (2026-10-24): the tree was judged in two worlds and
# the owner probe ran in the home one. The last owed red test was gated on a
# cfg only the other world sets, so the home build never compiled it, and it
# had no owner for 14 iterations. Shape here: `tests/gated.rs` is
# `#![cfg(other_world)]` and fails through `beta`. Asserted through the real
# probe in both worlds: `g` owns `beta`. Negative control: the home world
# alone leaves `g` unclaimed (asked, answered nowhere). Premise: cargo builds
# the gated test only when the world's env sets the cfg.

_GATED = """#![cfg(other_world)]
#[test]
fn g() {
    assert_eq!(widgets::beta(), 2);
}
"""


def _gated_workspace(tmp_path: Path) -> Path:
    project = _workspace(tmp_path)
    (project / "tests" / "gated.rs").write_text(_GATED)
    return project


def _other_env() -> dict[str, str]:
    env = _env()
    env["RUSTFLAGS"] = "--cfg other_world"
    env["CARGO_TARGET_DIR"] = str(_TARGET / "worlds" / "other")
    return env


def _gated_owners(project: Path, worlds: list[ProbeWorld]):
    from pipeline_core.adapters.coverage_owner import measure_coverage_slice
    logs: list[str] = []
    reading = measure_coverage_slice(
        project, _CLUSTER, worlds=worlds, failing_tests={"g"},
        failed_pairs={("other::tests/gated.rs", "g")},
        enumerate_fns=CliStructureReader(CliExtractor(_EXTRACTOR)).enumerate_fns,
        timeout=300, workers=1, log=logs.append)
    assert reading is not None, f"the coverage reading could not be taken: {logs}"
    return reading


def test_premise_a_cfg_gated_test_is_built_only_under_its_worlds_env(
        tmp_path: Path) -> None:
    project = _gated_workspace(tmp_path)
    cluster = project / ".skeleton-filled" / _CLUSTER
    _rc, home = run_cargo(CargoCli(), _CMD, cluster, _env(), timeout=600)
    _rc, other = run_cargo(CargoCli(), _CMD, cluster, _other_env(), timeout=600)
    _p, home_failed, _i = qualified_verdicts(parse_binary_results(home))
    _p, other_failed, _i = qualified_verdicts(parse_binary_results(other))
    assert ("tests/gated.rs", "g") not in home_failed
    assert ("tests/gated.rs", "g") in other_failed


def test_a_test_only_another_world_builds_is_owned_when_that_world_is_probed(
        tmp_path: Path) -> None:
    _tools_present()
    project = _gated_workspace(tmp_path)
    reading = _gated_owners(project, [ProbeWorld("main", _env()),
                                      ProbeWorld("other", _other_env())])
    assert "beta" in {s.fn_name for s in reading.owners.get("g", frozenset())}
    assert reading.worlds == {"main", "other"} and not reading.unclaimed


def test_negative_control_the_home_world_alone_leaves_it_unclaimed(
        tmp_path: Path) -> None:
    _tools_present()
    project = _gated_workspace(tmp_path)
    reading = _gated_owners(project, [ProbeWorld("main", _env())])
    assert "g" not in reading.owners and reading.unclaimed == {"g"}
