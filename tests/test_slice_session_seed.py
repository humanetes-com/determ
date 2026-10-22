"""A joint ask is one session run, seeded with the reverted answer's bodies.

Fill 9 of run `4012759-5c5177c6` put the candidate on the shared tree before
the fill and judged it only after; every gate in between read a regressed
premise. Here the seed is a turn of the session: written after the opening
PREMISE, measured as a JUDGMENT, kept or restored by the session's rule like
any other turn, and shown to the model in turn 0. IO is faked; the splice
writes the body as the file's whole text and the suite reads the files.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct.slice_session import (
    SessionPorts,
    run_slice_session,
)
from pipeline_core.domain.crash_keep import SameCrashJudge
from pipeline_core.domain.fill_ground import MeasureRole
from pipeline_core.domain.models import FnRecord, FnSite
from pipeline_core.domain.slice_session import SessionSlice
from pipeline_core.domain.test_results import BinaryResult, TestStateReading

_FMT = FnSite("src/lib.rs", "fmt", "impl fmt::Display for Report")
_REN = FnSite("src/render.rs", "render", "impl Renderer")
_RECS = {
    _FMT: FnRecord(file="src/lib.rs", fn_name="fmt",
                   container="impl fmt::Display for Report",
                   sig_text="fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result",
                   body_text="OPEN_LIB"),
    _REN: FnRecord(file="src/render.rs", fn_name="render",
                   container="impl Renderer", sig_text="pub fn render(&self) -> u32",
                   body_text="OPEN_REN"),
}
_SLICE = SessionSlice(
    tests=("t_gain", "t_keep"), units=(_FMT, _REN),
    seed=(("src/render.rs::render", "SEED_REN"),),
    joint="jabc", joint_label="session[2 test(s), 2 unit(s)] #0",
    joint_gained=("t_gain",))


def _reading(passed: set[str], failed: set[str]) -> TestStateReading:
    p, f = frozenset(passed), frozenset(failed)
    return TestStateReading(binaries=(BinaryResult(
        label="unit", completed=True, passed=p, failed=f,
        pass_count=len(p), fail_count=len(f)),))


def _tree(root: Path) -> Path:
    (root / "src").mkdir()
    (root / "src" / "lib.rs").write_text("OPEN_LIB")
    (root / "src" / "render.rs").write_text("OPEN_REN")
    return root


def _suite(root: Path, roles: list[MeasureRole]):  # type: ignore[no-untyped-def]
    """The opening state passes t_keep; the seed turns t_gain green and
    t_keep red; a `fmt` fix beside the seed passes both."""
    def measure(raw: list[str], role: MeasureRole) -> TestStateReading:
        roles.append(role)
        raw.append("---- t_keep stdout ----\nthread 't_keep' panicked at "
                   "src/lib.rs:1:1:\nassertion `left == right` failed\n"
                   "  left: \"seeded\"\n right: \"open\"\n")
        lib = (root / "src" / "lib.rs").read_text()
        ren = (root / "src" / "render.rs").read_text()
        if ren != "SEED_REN":
            return _reading({"t_keep"}, {"t_gain"})
        if "FIX_LIB" in lib:
            return _reading({"t_gain", "t_keep"}, set())
        return _reading({"t_gain"}, {"t_keep"})
    return measure


def _ports(root: Path, *, measure, replies: list[str], sent: list[str]):  # type: ignore[no-untyped-def]
    def _splice(path: Path, container: str, name: str, body: str, where: object,
                **_k: object) -> tuple[bool, str, int]:
        path.write_text(body)
        return True, "", 0

    def _ask(msgs: list[dict]) -> str:
        sent.append(str(msgs[-1]["content"]))
        return replies.pop(0) if replies else ""

    return SessionPorts(
        ask_turns=_ask, splice=_splice, check=lambda: (0, "", []),
        focus_errors=lambda out, f: out, blamed_type_api=lambda t: t,
        measure_suite=measure, read_source=lambda p: None,
        enumerate_bodies=lambda: {
            _FMT: (root / "src" / "lib.rs").read_text(),
            _REN: (root / "src" / "render.rs").read_text()},
        answer_pulls=lambda r, conversation="": None,
        log=lambda m: None, emit=lambda *a, **k: None,
        check_graceful_stop=lambda s: None,
        same_crash=SameCrashJudge(frozenset(), None))


def test_a_seed_the_turns_do_not_repair_is_restored_to_the_pre_seed_bytes(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    roles: list[MeasureRole] = []
    sent: list[str] = []
    out = run_slice_session(
        _ports(root, measure=_suite(root, roles), replies=[], sent=sent),
        target_src_dir=root, cluster_name="stable", slice_=_SLICE,
        records=_RECS, turn0="TURN0", max_turns=1)
    assert out.seed_judged
    assert out.kept == frozenset()
    assert (root / "src" / "render.rs").read_text() == "OPEN_REN"
    assert (root / "src" / "lib.rs").read_text() == "OPEN_LIB"
    assert roles[0] is MeasureRole.PREMISE
    assert all(r is MeasureRole.JUDGMENT for r in roles[1:])


def test_a_turn_that_repairs_the_lost_test_keeps_the_seed_against_the_opening(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    roles: list[MeasureRole] = []
    sent: list[str] = []
    fix = ('<FN name="fmt" block="impl fmt::Display for Report">'
           'write!(f, "{}", FIX_LIB)</FN>')
    out = run_slice_session(
        _ports(root, measure=_suite(root, roles), replies=[fix], sent=sent),
        target_src_dir=root, cluster_name="stable", slice_=_SLICE,
        records=_RECS, turn0="TURN0", max_turns=2)
    assert out.outcome == "accepted"
    assert out.kept == frozenset({_FMT, _REN})
    assert (root / "src" / "render.rs").read_text() == "SEED_REN"
    assert out.model_calls == 1


def test_turn_0_carries_the_seeds_newly_red_output(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    sent: list[str] = []
    run_slice_session(
        _ports(root, measure=_suite(root, []), replies=[], sent=sent),
        target_src_dir=root, cluster_name="stable", slice_=_SLICE,
        records=_RECS, turn0="TURN0", max_turns=1)
    assert sent[0].startswith("TURN0")
    assert "SEED:" in sent[0] and "src/render.rs::render" in sent[0]
    assert "t_keep" in sent[0]


def test_a_seed_that_is_already_green_is_accepted_without_a_model_call(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    (root / "src" / "lib.rs").write_text('write!(f, "{}", FIX_LIB)')
    sent: list[str] = []
    out = run_slice_session(
        _ports(root, measure=_suite(root, []), replies=[], sent=sent),
        target_src_dir=root, cluster_name="stable", slice_=_SLICE,
        records=_RECS, turn0="TURN0", max_turns=3)
    assert out.outcome == "accepted" and out.model_calls == 0 and not sent
    assert out.turns == 0, "the seed turn charges no turn"


def test_negative_control_an_unseeded_slice_never_writes_the_candidate(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    sent: list[str] = []
    plain = SessionSlice(tests=_SLICE.tests, units=_SLICE.units)
    out = run_slice_session(
        _ports(root, measure=_suite(root, []), replies=[], sent=sent),
        target_src_dir=root, cluster_name="stable", slice_=plain,
        records=_RECS, turn0="TURN0", max_turns=1)
    assert not out.seed_judged
    assert "SEED:" not in sent[0]


def test_the_joint_word_reads_the_tree_the_session_left_not_its_best_reading(
        tmp_path: Path) -> None:
    """A net-negative seed (t_gain green, t_keep red: net 0 here, so the
    session's rule refuses it) is put back. Classified on the best reading
    before that, the row stored `gains_held` while the tree held none of the
    gain (review of the 2026-10-22 implementation). `held_green` is the
    targets passing on the tree the session left, and the word follows it.
    NEGATIVE CONTROL: the same classifier on `final` says `gains_held`."""
    from pipeline_core.domain.candidate_tiers import joint_outcome_of
    root = _tree(tmp_path)
    out = run_slice_session(
        _ports(root, measure=_suite(root, []), replies=[], sent=[]),
        target_src_dir=root, cluster_name="stable", slice_=_SLICE,
        records=_RECS, turn0="TURN0", max_turns=1)
    assert out.seed_judged and out.kept == frozenset()
    assert (root / "src" / "render.rs").read_text() == "OPEN_REN"
    assert out.held_green == frozenset({"t_keep"})
    lost = frozenset(_SLICE.tests) - frozenset(_SLICE.joint_gained)
    assert joint_outcome_of(gained=_SLICE.joint_gained, lost=lost,
                            after_green=out.held_green) == "refuted"
    assert out.final is not None
    assert joint_outcome_of(gained=_SLICE.joint_gained, lost=lost,
                            after_green=out.final.green_targets) == "gains_held"


def test_an_accepted_seed_holds_its_greens(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    fix = ('<FN name="fmt" block="impl fmt::Display for Report">'
           'write!(f, "{}", FIX_LIB)</FN>')
    out = run_slice_session(
        _ports(root, measure=_suite(root, []), replies=[fix], sent=[]),
        target_src_dir=root, cluster_name="stable", slice_=_SLICE,
        records=_RECS, turn0="TURN0", max_turns=2)
    assert out.outcome == "accepted"
    assert out.held_green == frozenset({"t_gain", "t_keep"})
