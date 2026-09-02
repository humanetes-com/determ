"""The session harness's keep/restore contracts, IO faked.

A Display crate — the never-steer check's own example. The loop reads the
slice, the replies and the suite readings; it must not keep a compiling
change it cannot judge, and a previously-passing test going red must not
survive peel even when net pass rose.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct.slice_session import (
    SessionPorts,
    _peel,
    run_slice_session,
)
from pipeline_core.domain.models import FnLocator, FnRecord
from pipeline_core.domain.slice_session import SessionSlice
from pipeline_core.domain.test_results import BinaryResult, TestStateReading

_FMT = FnLocator("src/lib.rs", "fmt")
_RENDER = FnLocator("src/render.rs", "render")
_RECS = {
    _FMT: FnRecord(file="src/lib.rs", fn_name="fmt",
                   container="impl fmt::Display for Report",
                   sig_text="fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result",
                   body_text="write!(f, \"{}\", self.msg)"),
    _RENDER: FnRecord(file="src/render.rs", fn_name="render",
                      container="impl Renderer",
                      sig_text="pub fn render(&self) -> u32",
                      body_text="self.depth"),
}


def _reading(passed: set[str], failed: set[str]) -> TestStateReading:
    p, f = frozenset(passed), frozenset(failed)
    return TestStateReading(binaries=(BinaryResult(
        label="unit", completed=True, passed=p, failed=f,
        pass_count=len(p), fail_count=len(f)),))


def _tree(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_text("SESSION_LIB")
    (src / "render.rs").write_text("SESSION_REN")
    return tmp_path


def _ports(*, measure, splice=None, ask=None, bodies=None):
    def _splice(path, container, name, body, where, **k):
        path.write_text(body)
        return True, "", 0

    return SessionPorts(
        ask_turns=ask or (lambda m: ""),
        splice=splice or _splice,
        check=lambda: (0, "", []),
        focus_errors=lambda out, f: out,
        blamed_type_api=lambda t: t,
        measure_suite=measure,
        read_source=lambda p: None,
        enumerate_bodies=lambda: bodies or {
            u: r.body_text for u, r in _RECS.items()},
        answer_pulls=lambda r: None,
        log=lambda m: None, emit=lambda *a, **k: None,
        check_graceful_stop=lambda s: None)


def test_an_inconclusive_opening_suite_does_not_run_or_keep(tmp_path):
    root = _tree(tmp_path)
    asked: list = []

    def ask(m: object) -> str:
        asked.append(m)
        return "should not run"

    out = run_slice_session(
        _ports(measure=lambda raw: None, ask=ask),
        target_src_dir=root, cluster_name="t",
        slice_=SessionSlice(tests=("t_pair",), units=(_FMT, _RENDER)),
        records=_RECS, turn0="turn0")
    assert out.outcome == "no_change" and out.turns == 0 and asked == []
    assert (root / "src" / "lib.rs").read_text() == "SESSION_LIB"
    assert (root / "src" / "render.rs").read_text() == "SESSION_REN"


def test_peel_does_not_keep_a_net_positive_state_that_still_reddened_a_sibling(
        tmp_path):
    """`RegressionComparison.regressed` is false when net pass rose even
    with newly-red tests. Peel must not treat that as a hold — that is a
    session regression. Negative control: the pre-fix stop would have
    kept the remaining unit."""
    root = _tree(tmp_path)
    baseline = _reading({"t_keep"}, {"t_pair"})
    # After peeling one unit: +1 pass, but t_keep went red.
    after = _reading({"t_pair", "t_bonus"}, {"t_keep"})
    assert after.pass_count - baseline.pass_count > 0
    from pipeline_core.domain.test_results import compare_readings
    assert not compare_readings(baseline, after).regressed
    assert compare_readings(baseline, after).newly_red == frozenset({"t_keep"})
    kept, reverted = _peel(
        _ports(measure=lambda raw: after),
        root,
        SessionSlice(tests=("t_pair",), units=(_FMT, _RENDER)),
        _RECS, baseline,
        {_FMT: "BASE_LIB", _RENDER: "BASE_REN"},
        {"src/lib.rs": "BASE_LIB", "src/render.rs": "BASE_REN"},
        frozenset({_FMT, _RENDER}), peel_max=1, label="t")
    assert kept == frozenset()
    assert (root / "src" / "lib.rs").read_text() == "BASE_LIB"
    assert (root / "src" / "render.rs").read_text() == "BASE_REN"


def test_a_failed_peel_splice_leaves_the_unit_on_the_kept_side(tmp_path):
    """Pop + continue without restore used to drop the unit from remaining
    while its post-session body stayed on disk. An early hold then reported
    it as neither kept nor reverted."""
    root = _tree(tmp_path)
    baseline = _reading({"t_keep"}, {"t_pair"})
    hold = _reading({"t_keep"}, {"t_pair"})

    def splice(path, container, name, body, where, **k):
        if name == "render":
            return False, "refused", 0
        path.write_text(body)
        return True, "", 0

    kept, reverted = _peel(
        _ports(measure=lambda raw: hold, splice=splice),
        root,
        SessionSlice(tests=("t_pair",), units=(_FMT, _RENDER)),
        _RECS, baseline,
        {_FMT: "BASE_LIB", _RENDER: "BASE_REN"},
        {"src/lib.rs": "BASE_LIB", "src/render.rs": "BASE_REN"},
        frozenset({_FMT, _RENDER}), peel_max=6, label="t")
    # Sorted (file, name): fmt then render; pop takes render first (fails),
    # then fmt (holds). render stays the session body and is kept.
    assert _RENDER in kept and _FMT in reverted
    assert (root / "src" / "render.rs").read_text() == "SESSION_REN"
    assert (root / "src" / "lib.rs").read_text() == "BASE_LIB"
