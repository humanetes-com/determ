"""A joint write is kept on its own suite reading when its only aborts are
already-red tests at a crate frame.

Run `4112620-8a89f05c` (2026-10-29, cluster `stable`): the `src/error.rs`
file route read `pass 229 -> 249` twice (and +4, +15 in the fills around
it), nothing newly red, no binary newly crashed, and reverted every attempt
on "no strict improvement; aborted: test_drop, test_unsuccessful_downcast".
Both were red at the sweep, and each crash named a crate frame. The abort
partition was asked whether an AIMED test turned green, and the +20 landed
on tests no sweep attributed to the file. The keep now hands the partition
the write's own suite gain and loss once its regression gate has measured
them (`impl_fill.suite_bank`, `JointImprovement.banks_on`).

The route is the real file route over a two-block invented file. The
scoped gate's reject and the joint judgment are scripted. The suite
comparison the keep reads is the one the regression gate measured: the
first cargo run is the baseline, the later runs are the after-reading.

NEGATIVE CONTROL: the same reading under the pre-change keep (no suite
bank) reverts. Red before and after with net 0 while a binary newly
crashes, a test that passed and now fails, a newly crashed binary beside
a positive net, an abort with no crate frame, and an abort of a test that
passed all still refuse on the same call. A suite refusal writes no
ignored-input decline. The same-crash guard does not veto the suite keep.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import pipeline
from pipeline_core.application.reconstruct import impl_fill
from pipeline_core.application.reconstruct.fill_reading import (
    reset_last_comparison,
)
from pipeline_core.domain.assertion_position import SuiteDelta, TestOutcomes
from pipeline_core.domain.attribution import FailureFrontier
from pipeline_core.domain.models import FnLocator, FnRecord, ImplBlock
from pipeline_core.domain.test_report import JointImprovement, U2Verdict
from pipeline_core.domain.test_results import RegressionComparison
from tests.harness import (
    install_app_seams,
    install_cluster_scan,
    install_scripted_cargo,
)
from tests.test_e2e_impl_loop import (
    IMPL_LIB_TWO,
    RESPONSE_TWO,
    _install_model,
    _setup,
    _two_blocks,
)

_DEAD = frozenset({"t_dead"})


def _cmp(*, base: int = 229, cur: int = 249,
         newly_red: frozenset[str] = frozenset(),
         newly_crashed: tuple[str, ...] = (),
         crash_lost: int = 0) -> RegressionComparison:
    return RegressionComparison(
        newly_red=newly_red, newly_crashed=newly_crashed,
        excluded_crashed=(), not_run=(), measured=("tests/t.rs",),
        base_pass=base, cur_pass=cur, base_executed=349, cur_executed=349,
        crash_lost_pass=crash_lost)


def _suite_capture(passed: int, failed: tuple[str, ...] = ("t_red",)) -> str:
    """One completed binary. The pass tally is the line the regression
    gate sums; the named failure is red on both sides, so it is not newly
    red."""
    lines = [
        "    Running tests/t.rs (t/t-1)",
        f"running {passed + len(failed)} tests",
        *(f"test {n} ... FAILED" for n in failed),
        f"test result: FAILED. {passed} passed; {len(failed)} failed; "
        "0 ignored",
    ]
    return "\n".join(lines) + "\n"


def _outcomes(**k: frozenset[str]) -> TestOutcomes:
    base = {"aborted": _DEAD, "aborted_located": _DEAD}
    base.update(k)
    return TestOutcomes(**base)


def _file_route(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *,
                cmp: RegressionComparison | None, outcomes: TestOutcomes,
                capsys: pytest.CaptureFixture[str],
                reply: str = RESPONSE_TWO, source: str = IMPL_LIB_TWO,
                blocks: list[ImplBlock] | None = None,
                ) -> tuple[str, str]:
    ws = _setup(monkeypatch, tmp_path)
    (ws / "src" / "lib.rs").write_text(source)
    install_cluster_scan(
        monkeypatch, impl_blocks=lambda *a, **k: blocks or _two_blocks())
    install_cluster_scan(monkeypatch, stub_records=lambda *a, **k: [])
    monkeypatch.setenv("PHASE_C_ROUTE_ORDER", "file_primary")
    _install_model(monkeypatch, [reply])
    if cmp is None:
        # The gate measures this. The first run is the baseline; every
        # later run is the after-reading. Nothing is newly red.
        reset_last_comparison()
        install_scripted_cargo(
            monkeypatch, check_diag=(0, "", []),
            run=[(101, _suite_capture(229)), (101, _suite_capture(249))])
    else:
        # A refusal case hands the comparison the route will read. The
        # cargo capture is stable, so the gate itself accepts and the
        # stub is the only loss.
        install_scripted_cargo(
            monkeypatch, check_diag=(0, "", []),
            run=(101, _suite_capture(229)))
        monkeypatch.setattr(impl_fill, "last_comparison", lambda: cmp)
    install_app_seams(monkeypatch, u2_test_gate=lambda *a, **k: U2Verdict.reject(
        frozenset({"t_red"}), "t_red ... FAILED"))
    monkeypatch.setattr(impl_fill, "judge_refill",
                        lambda *a, **k: JointImprovement(
                            baseline_failing=frozenset({"t_red", "t_dead"}),
                            outcomes=outcomes))
    pipeline._phase_c_fill_impl_blocks(
        project_dir=ws, target_src_dir=ws / "src", tests_dir_files=[],
        module_name="m", cluster_name="t", cluster=None,
        check_cmd="cargo check", cluster_env={}, fr_path=None,
        tdd_test_cmd="cargo test",
        tdd_frontier_keys={FnLocator("lib.rs", "kind"),
                           FnLocator("lib.rs", "name")},
        tdd_frontier_tests=FailureFrontier.of_direct({
            FnLocator("lib.rs", "kind"): {"t_red"},
            FnLocator("lib.rs", "name"): {"t_dead"}}))
    return capsys.readouterr().out, (ws / "src" / "lib.rs").read_text()


def test_a_suite_gain_beside_located_red_aborts_is_kept_by_the_file_route(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    out, text = _file_route(monkeypatch, tmp_path, cmp=None,
                            outcomes=_outcomes(), capsys=capsys)
    assert "re-fill KEPT on its suite reading (net +20" in out
    assert "FILE FILLED" in out
    assert "self.tag" in text


def test_negative_control_the_pre_change_keep_reverts_the_same_reading(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(impl_fill, "suite_bank", lambda *a, **k: None)
    out, _text = _file_route(monkeypatch, tmp_path, cmp=_cmp(),
                             outcomes=_outcomes(), capsys=capsys)
    assert "FILE FILLED" not in out
    assert "KEPT on its suite reading" not in out


def _refusing_cases() -> Iterator[tuple[str, RegressionComparison,
                                         TestOutcomes]]:
    # Net is zero because the one pass gained elsewhere is the pass the
    # newly crashed binary lost. A flat tally with no crashed binary is
    # not this case.
    yield ("net zero while a binary dies",
           _cmp(cur=230, newly_crashed=("tests/u.rs",), crash_lost=1),
           _outcomes())
    yield ("a passing test now fails", _cmp(newly_red=frozenset({"t_ok"})),
           _outcomes())
    yield ("a binary newly crashed", _cmp(newly_crashed=("tests/u.rs",)),
           _outcomes())
    yield ("an abort with no crate frame", _cmp(),
           _outcomes(aborted_located=frozenset()))
    yield ("an abort of a test that passed", _cmp(),
           _outcomes(aborted_green=_DEAD))
    yield ("an aimed test lost", _cmp(),
           _outcomes(lost=frozenset({"t_ok"})))


@pytest.mark.parametrize(("label", "cmp", "outcomes"), list(_refusing_cases()),
                         ids=[c[0] for c in _refusing_cases()])
def test_the_same_call_still_refuses(
        label: str, cmp: RegressionComparison, outcomes: TestOutcomes,
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    out, _text = _file_route(monkeypatch, tmp_path, cmp=cmp,
                             outcomes=outcomes, capsys=capsys)
    assert "KEPT on its suite reading" not in out, label
    assert "FILE FILLED" not in out, label


def test_the_rule_reads_the_suite_pair_not_the_aimed_set() -> None:
    joint = JointImprovement(baseline_failing=frozenset({"t_red", "t_dead"}),
                             outcomes=_outcomes())
    assert not joint.banks
    assert joint.banks_on(SuiteDelta.of(_cmp()))
    assert not joint.banks_on(SuiteDelta.of(_cmp(cur=229)))
    assert not joint.banks_on(
        SuiteDelta.of(_cmp(newly_red=frozenset({"t_ok"}))))
    assert not joint.banks_on(SuiteDelta.of(
        _cmp(newly_crashed=("tests/u.rs",))))
    assert not joint.banks_on(SuiteDelta.of(
        _cmp(cur=230, newly_crashed=("tests/u.rs",), crash_lost=1)))


def _impl_route(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *,
                capsys: pytest.CaptureFixture[str]) -> str:
    from tests.test_e2e_impl_loop import RESPONSE_BOTH
    ws = _setup(monkeypatch, tmp_path)
    _install_model(monkeypatch, [RESPONSE_BOTH])
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []),
                           run=(0, "test t_ok ... ok\n\ntest result: ok. "
                                   "1 passed; 0 failed; 0 ignored"))
    install_app_seams(monkeypatch, u2_test_gate=lambda *a, **k: U2Verdict.reject(
        frozenset({"t_red"}), "t_red ... FAILED"))
    monkeypatch.setattr(impl_fill, "judge_refill",
                        lambda *a, **k: JointImprovement(
                            baseline_failing=frozenset({"t_red", "t_dead"}),
                            outcomes=_outcomes()))
    monkeypatch.setattr(impl_fill, "last_comparison", lambda: _cmp())
    pipeline._phase_c_fill_impl_blocks(
        project_dir=ws, target_src_dir=ws / "src", tests_dir_files=[],
        module_name="m", cluster_name="t", cluster=None,
        check_cmd="cargo check", cluster_env={}, fr_path=None,
        tdd_test_cmd="cargo test",
        tdd_frontier_keys={FnLocator("lib.rs", "kind"),
                           FnLocator("lib.rs", "name")},
        tdd_frontier_tests=FailureFrontier.of_direct({
            FnLocator("lib.rs", "kind"): {"t_red"},
            FnLocator("lib.rs", "name"): {"t_dead"}}))
    return capsys.readouterr().out


def test_the_impl_route_keeps_the_same_reading(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    out = _impl_route(monkeypatch, tmp_path, capsys=capsys)
    assert "[phase-c/impl]" in out and "KEPT on its suite reading (net +20" in out


def test_negative_control_the_impl_route_reverts_it_without_the_suite_bank(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(impl_fill, "suite_bank", lambda *a, **k: None)
    out = _impl_route(monkeypatch, tmp_path, capsys=capsys)
    assert "KEPT on its suite reading" not in out


def test_the_suite_keep_stands_when_the_same_crash_guard_would_refuse(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    """The same-crash guard stays on the non-regression keep. A suite
    gain whose every aimed test is still a native crash is still a suite
    gain: the guard is not asked, so a refuses that returns true cannot
    revert it."""
    monkeypatch.setattr(
        impl_fill.SameCrashJudge, "refuses", lambda self, **k: True)
    out, text = _file_route(monkeypatch, tmp_path, cmp=None,
                            outcomes=_outcomes(), capsys=capsys)
    assert "re-fill KEPT on its suite reading (net +20" in out
    assert "self.tag" in text


_FLAG_LIB = """\
pub struct Foo { tag: u32 }
pub struct Bar { tag: u32 }

impl Foo {
    pub fn kind(&self, flag: u32) -> u32 {
        unimplemented!()
    }
}
impl Bar {
    pub fn name(&self) -> u32 {
        unimplemented!()
    }
}
"""

_FLAG_REPLY = (
    '<FN name="kind" block="impl Foo">0</FN>\n'
    '<FN name="name" block="impl Bar">self.tag + 1</FN>'
)


def _flag_blocks() -> list[ImplBlock]:
    kind = FnRecord(
        file="lib.rs", fn_name="kind", container="impl Foo",
        sig_text="fn kind (& self , flag : u32) -> u32",
        has_unimplemented_body=True)
    name = FnRecord(
        file="lib.rs", fn_name="name", container="impl Bar",
        sig_text="fn name (& self) -> u32", has_unimplemented_body=True)
    return [
        ImplBlock(file="lib.rs", container="impl Foo",
                  members=(kind,), stub_members=(kind,)),
        ImplBlock(file="lib.rs", container="impl Bar",
                  members=(name,), stub_members=(name,)),
    ]


def test_a_suite_keep_declines_the_body_that_ignores_its_input(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    out, text = _file_route(
        monkeypatch, tmp_path, cmp=_cmp(), outcomes=_outcomes(),
        capsys=capsys, reply=_FLAG_REPLY, source=_FLAG_LIB,
        blocks=_flag_blocks())
    assert "[phase-c/declined]" in out
    assert "KEPT on its suite reading (net +20" in out
    assert "self.tag + 1" in text
    assert "unimplemented!()" in text


def test_a_suite_refusal_writes_no_decline(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    out, text = _file_route(
        monkeypatch, tmp_path,
        cmp=_cmp(cur=230, newly_crashed=("tests/u.rs",), crash_lost=1),
        outcomes=_outcomes(), capsys=capsys, reply=_FLAG_REPLY,
        source=_FLAG_LIB, blocks=_flag_blocks())
    assert "[phase-c/declined]" not in out
    assert "KEPT on its suite reading" not in out
    assert "self.tag + 1" not in text
    assert text.count("unimplemented!()") == 2


def _pair_route(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *,
                capsys: pytest.CaptureFixture[str]) -> tuple[str, str, str]:
    from tests.test_pair_fill_route import (
        PAIR_REPLY,
        _frontier_tests,
        _install_model,
        _setup,
    )
    ws = _setup(monkeypatch, tmp_path)
    prompts: list[str] = []
    _install_model(monkeypatch, [PAIR_REPLY], prompts)
    install_scripted_cargo(
        monkeypatch, check_diag=(0, "", []),
        run=(101, _suite_capture(229)))
    install_app_seams(monkeypatch, u2_test_gate=lambda *a, **k: U2Verdict.reject(
        frozenset({"t_red"}), "t_red ... FAILED"))
    monkeypatch.setattr(impl_fill, "judge_refill",
                        lambda *a, **k: JointImprovement(
                            baseline_failing=frozenset({"t_red", "t_dead"}),
                            outcomes=_outcomes()))
    monkeypatch.setattr(impl_fill, "last_comparison", lambda: _cmp())
    pipeline._phase_c_fill_impl_blocks(
        project_dir=ws, target_src_dir=ws / "src", tests_dir_files=[],
        module_name="m", cluster_name="t", cluster=None,
        check_cmd="cargo check", cluster_env={}, fr_path=None,
        tdd_test_cmd="cargo test",
        tdd_frontier_keys={FnLocator("lib.rs", "fmt"),
                           FnLocator("render.rs", "render")},
        tdd_frontier_tests=_frontier_tests())
    return (capsys.readouterr().out,
            (ws / "src" / "lib.rs").read_text(),
            (ws / "src" / "render.rs").read_text())


def test_the_pair_route_keeps_the_same_reading(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    out, lib, render = _pair_route(monkeypatch, tmp_path, capsys=capsys)
    assert "[phase-c/pair]" in out and "KEPT on its suite reading (net +20" in out
    assert "PAIRED" in lib
    assert "self.depth + 7" in render


def test_negative_control_the_pair_route_reverts_it_without_the_suite_bank(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(impl_fill, "suite_bank", lambda *a, **k: None)
    out, lib, render = _pair_route(monkeypatch, tmp_path, capsys=capsys)
    assert "KEPT on its suite reading" not in out
    assert "PAIRED" not in lib
    assert "self.depth + 7" not in render
