"""The red-slice session route: one transcript over every unit a failing
test executes, the compiler's and the suite's answers as turns, acceptance
as one transaction.

The crate is NOT the recorded one — a Display dispatcher in `lib.rs` and a
`render.rs` implementer, the never-steer check's own example. The route
reads only the owner-map slice, the frontier and the replies; it must run
identically here.

Negative control: with the session disabled the same inputs take the
stateless pair route (one rebuilt prompt per attempt), which is the shape
run `3003-93e9a9bf` (2026-09-02) recorded — six independent documents,
none carrying the previous reply or the printed values.
"""
from __future__ import annotations

from pathlib import Path

import model_interact
import pipeline
from pipeline_core.domain.attribution import FailureFrontier, FormatterPair
from pipeline_core.domain.models import FnLocator, FnRecord, FnSite, ImplBlock
from tests.harness import (
    diag,
    install_cluster_scan,
    install_fake_extractor,
    install_scripted_cargo,
)

DISPATCHER_LIB = """use std::fmt;
pub struct Report { msg: String }

impl fmt::Display for Report {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        write!(f, "{}", self.msg)
    }
}
"""

IMPLEMENTER_LIB = """pub struct Renderer { pub depth: u32 }

impl Renderer {
    pub fn render(&self) -> u32 {
        self.depth
    }
}
"""

_DISP = FnLocator("src/lib.rs", "fmt")
_IMPL = FnLocator("src/render.rs", "render")
_PAIR = FormatterPair(
    FnSite("src/lib.rs", "fmt", "impl fmt::Display for Report"),
    FnSite("src/render.rs", "render", "impl Renderer"))

FIRST_REPLY = (
    '<FN name="fmt" block="impl fmt::Display for Report">'
    'write!(f, "first {}", self.msg)</FN>\n'
    "<NOTES>tried a plain write</NOTES>"
)
SECOND_REPLY = (
    '<FN name="fmt" block="impl fmt::Display for Report">'
    'write!(f, "rendered {}", self.msg)</FN>\n'
    '<FN name="render" block="impl Renderer">self.depth + 7</FN>'
)

_RED = ("running 1 test\n"
        "test t_pair ... FAILED\n\n"
        "failures:\n\n"
        "---- t_pair stdout ----\n"
        "thread 't_pair' panicked at tests/display_gen_1.rs:4:5:\n"
        "assertion `left == right` failed\n"
        '  left: "first hi"\n'
        ' right: "rendered hi"\n\n'
        "failures:\n    t_pair\n\n"
        "test result: FAILED. 0 passed; 1 failed; 0 ignored\n")
_GREEN = ("running 1 test\n"
          "test t_pair ... ok\n\n"
          "test result: ok. 1 passed; 0 failed; 0 ignored\n")

DISPLAY_TEST = """#[test]
fn t_pair() {
    let r = Report { msg: String::from("hi") };
    assert_eq!(format!("{}", r), "rendered hi");
}
"""


def _blocks() -> list[ImplBlock]:
    disp = FnRecord(file="src/lib.rs", fn_name="fmt",
                    container="impl fmt::Display for Report",
                    sig_text="fn fmt (& self , f : & mut fmt :: Formatter) -> fmt :: Result")
    impl_ = FnRecord(file="src/render.rs", fn_name="render",
                     container="impl Renderer",
                     sig_text="pub fn render (& self) -> u32")
    return [
        ImplBlock(file="src/lib.rs", container="impl fmt::Display for Report",
                  members=(disp,), refill_members=(disp,)),
        ImplBlock(file="src/render.rs", container="impl Renderer",
                  members=(impl_,), refill_members=(impl_,)),
    ]


def _setup(monkeypatch, tmp_path: Path) -> tuple[Path, list[Path]]:
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "lib.rs").write_text(DISPATCHER_LIB)
    (ws / "src" / "render.rs").write_text(IMPLEMENTER_LIB)
    (ws / "tests").mkdir()
    (ws / "tests" / "display_gen_1.rs").write_text(DISPLAY_TEST)
    monkeypatch.setenv("PHASE_C_KEEP_NONREGRESSING", "0")
    monkeypatch.setenv("PHASE_C_ROUTE_ORDER", "default")
    monkeypatch.setenv("ASSERT_PROBE", "0")
    monkeypatch.setenv("SLICE_SESSION_MAX_TURNS", "4")
    install_cluster_scan(monkeypatch, impl_blocks=lambda *a, **k: _blocks())
    install_cluster_scan(monkeypatch, stub_records=lambda *a, **k: None)
    install_fake_extractor(
        monkeypatch, fns=lambda sd: pipeline._STRUCTURE.enumerate_fns(sd),
        types=[], dispatch_families=[])
    return ws, [ws / "tests" / "display_gen_1.rs"]


def _install_turns(monkeypatch, replies: list[str]) -> list[list[dict]]:
    transcripts: list[list[dict]] = []
    seq = list(replies)

    def _ask_turns(messages, **k):
        transcripts.append([dict(m) for m in messages])
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(model_interact, "ask_turns", _ask_turns)
    monkeypatch.setattr(pipeline, "_ask_raw", lambda *a, **k: "")
    return transcripts


def _frontier() -> FailureFrontier:
    return FailureFrontier.build(
        {_DISP: {"t_pair"}, _IMPL: {"t_pair"}},
        {_DISP: {"t_pair"}, _IMPL: {"t_pair"}},
        evidence={"t_pair": ('panicked at tests/display_gen_1.rs:4:5: assertion '
                             '`left == right` failed\n  left: "hi"\n  right: '
                             '"rendered hi"')},
        pairs=(_PAIR,))


def _run(ws: Path, tests: list[Path], *, slices=None) -> pipeline.ClusterFillResult:
    return pipeline._phase_c_fill_impl_blocks(  # type: ignore[no-any-return]
        project_dir=ws, target_src_dir=ws, tests_dir_files=tests,
        module_name="m", cluster_name="t", cluster=None,
        check_cmd="cargo check", cluster_env={}, fr_path=None,
        tdd_frontier_keys={_DISP},
        tdd_frontier_tests=_frontier(),
        tdd_test_cmd="cargo test",
        session_slices=slices,
    )


def test_a_session_carries_the_previous_reply_and_verdict_into_the_next_turn(
        monkeypatch, tmp_path):
    ws, tests = _setup(monkeypatch, tmp_path)
    transcripts = _install_turns(monkeypatch, [FIRST_REPLY, SECOND_REPLY])
    # Suite readings in order: session baseline (red), after turn 1 (still
    # red, with what the assertion printed), after turn 2 (green).
    install_scripted_cargo(
        monkeypatch, check_diag=(0, "", []),
        run=[(101, _RED), (101, _RED), (0, _GREEN)])
    res = _run(ws, tests, slices={"t_pair": frozenset({_DISP, _IMPL})})
    lib = (ws / "src" / "lib.rs").read_text()
    ren = (ws / "src" / "render.rs").read_text()
    assert "rendered {}" in lib and "self.depth + 7" in ren
    # Two model calls, ONE transcript: the second call carries the system
    # turn the ask client prefixes, turn 0, the first reply verbatim and the
    # harness's verdict for it.
    assert len(transcripts) == 2
    assert [m["role"] for m in transcripts[0]] == ["system", "user"]
    assert [m["role"] for m in transcripts[1]] == ["system", "user", "assistant", "user"]
    assert transcripts[1][2]["content"] == FIRST_REPLY
    verdict = transcripts[1][3]["content"]
    assert "FAIL t_pair" in verdict
    assert 'left: "first hi"' in verdict and 'right: "rendered hi"' in verdict
    # Turn 0 names the editable units of both files and the target's words.
    turn0 = transcripts[0][1]["content"]
    headers = [ln for ln in turn0.splitlines() if ln.startswith("<FILE ")]
    assert any(h.startswith('<FILE path="src/lib.rs" editable="fmt (') for h in headers), headers
    assert any(h.startswith('<FILE path="src/render.rs" editable="render (') for h in headers), headers
    assert "t_pair — FAILING" in turn0 and 'right: "rendered hi"' in turn0
    # Both slice units count as dispatched; nothing was reverted.
    assert (_DISP.file, _DISP.fn_name) in (res.dispatched_fns or ())
    assert (_IMPL.file, _IMPL.fn_name) in (res.dispatched_fns or ())
    assert not res.reverted_fns
    assert res.fns_filled >= 2
    # The session's own notes are its memory for the next session on this
    # slice.
    recs = pipeline._APP.impl_fill_deps.load_retrospectives(ws)
    session_recs = [v for k, v in recs.items() if "*session*" in str(k)]
    assert session_recs and session_recs[0]["guidance"] == "tried a plain write"
    assert session_recs[0]["session_outcome"] == "accepted"


def test_a_non_compiling_change_set_is_restored_and_the_diagnostic_is_a_turn(
        monkeypatch, tmp_path):
    ws, tests = _setup(monkeypatch, tmp_path)
    transcripts = _install_turns(monkeypatch, [FIRST_REPLY, SECOND_REPLY])
    install_scripted_cargo(
        monkeypatch,
        check_diag=[(101, "error[E0616]: field `msg` of struct `Report` is private",
                     [diag("src/lib.rs", code="E0616")]), (0, "", [])],
        run=[(101, _RED), (0, _GREEN)])
    _run(ws, tests, slices={"t_pair": frozenset({_DISP, _IMPL})})
    assert "rendered {}" in (ws / "src" / "lib.rs").read_text()
    assert len(transcripts) == 2
    result_turn = transcripts[1][3]["content"]
    assert "did NOT compile" in result_turn and "E0616" in result_turn


def test_a_session_that_ends_regressing_restores_the_baseline(monkeypatch, tmp_path):
    ws, tests = _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("SLICE_SESSION_MAX_TURNS", "1")
    _install_turns(monkeypatch, [SECOND_REPLY])
    red_plus = _RED.replace("running 1 test\ntest t_pair ... FAILED",
                            "running 2 tests\ntest t_keep ... FAILED\ntest t_pair ... FAILED")
    base = _RED.replace("running 1 test\n", "running 2 tests\ntest t_keep ... ok\n")
    # Baseline: t_keep green, t_pair red. After the only turn: t_keep red.
    install_scripted_cargo(
        monkeypatch, check_diag=(0, "", []),
        run=[(101, base), (101, red_plus), (101, red_plus), (101, red_plus)])
    res = _run(ws, tests, slices={"t_pair": frozenset({_DISP, _IMPL})})
    assert (ws / "src" / "lib.rs").read_text() == DISPATCHER_LIB
    assert (ws / "src" / "render.rs").read_text() == IMPLEMENTER_LIB
    assert (_DISP.file, _DISP.fn_name) in (res.reverted_fns or ())


def test_negative_control_with_the_session_off_the_pair_route_rebuilds_a_prompt(
        monkeypatch, tmp_path):
    """The pre-fix shape: the same slice under SLICE_SESSION=0 goes to the
    stateless pair route — a single-prompt document, no transcript."""
    ws, tests = _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("SLICE_SESSION", "0")
    transcripts = _install_turns(monkeypatch, [SECOND_REPLY])
    prompts: list[str] = []

    def _ask(prompt, *a, **k):
        prompts.append(prompt)
        return SECOND_REPLY

    monkeypatch.setattr(pipeline, "_ask_raw", _ask)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []), run=(0, _GREEN))
    _run(ws, tests, slices={"t_pair": frozenset({_DISP, _IMPL})})
    assert transcripts == []
    assert any("PAIRED WRITE" in p for p in prompts)


def test_a_session_that_lands_nothing_does_not_block_the_pair_route(
        monkeypatch, tmp_path):
    """A no_change session used to union the whole slice into served and
    drop those units from pair/per-fn this call — GPU spent, tree unchanged,
    the only other route that could move the tests never ran."""
    ws, tests = _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("SLICE_SESSION_MAX_TURNS", "1")
    transcripts = _install_turns(monkeypatch, ["<NOTES>nothing yet</NOTES>"])
    prompts: list[str] = []

    def _ask(prompt, *a, **k):
        prompts.append(prompt)
        return SECOND_REPLY

    monkeypatch.setattr(pipeline, "_ask_raw", _ask)
    install_scripted_cargo(
        monkeypatch, check_diag=(0, "", []),
        run=[(101, _RED), (0, _GREEN)])
    _run(ws, tests, slices={"t_pair": frozenset({_DISP, _IMPL})})
    assert transcripts  # the session ran
    assert any("PAIRED WRITE" in p for p in prompts)
    assert "rendered {}" in (ws / "src" / "lib.rs").read_text()


# --- what the first live session taught (run 164454-8ec9c4d4, 2026-09-02) ---

TWO_BODY_REPLY = (
    '<FN name="fmt" block="impl fmt::Display for Report">'
    'write!(f, "rendered {}", self.msg)</FN>\n'
    '<FN name="render" block="impl Renderer">self.depth + broken</FN>'
)


def test_a_compile_failure_peels_the_blamed_body_and_judges_the_rest(
        monkeypatch, tmp_path):
    """Turn 1 of the live session sent the right implementer body together
    with a sibling that did not type-check, and the whole change set was
    restored. Now the compiler's own implication drops the blamed body and
    the rest is kept and judged in the same turn."""
    ws, tests = _setup(monkeypatch, tmp_path)
    transcripts = _install_turns(monkeypatch, [TWO_BODY_REPLY])
    install_scripted_cargo(
        monkeypatch,
        check_diag=[(101, "error[E0425]: cannot find value `broken`",
                     [diag("src/render.rs", code="E0425")]), (0, "", [])],
        run=[(101, _RED), (0, _GREEN)],
        frontier={_IMPL: {"<compile>"}})
    res = _run(ws, tests, slices={"t_pair": frozenset({_DISP, _IMPL})})
    assert "rendered {}" in (ws / "src" / "lib.rs").read_text()
    assert (ws / "src" / "render.rs").read_text() == IMPLEMENTER_LIB
    assert len(transcripts) == 1
    assert (_DISP.file, _DISP.fn_name) in (res.dispatched_fns or ())
    assert res.fns_filled >= 1


def test_turn_zero_marks_which_editable_units_the_failing_tests_execute(
        monkeypatch, tmp_path):
    ws, tests = _setup(monkeypatch, tmp_path)
    transcripts = _install_turns(monkeypatch, [""])
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []), run=(101, _RED))
    pipeline._phase_c_fill_impl_blocks(
        project_dir=ws, target_src_dir=ws, tests_dir_files=tests,
        module_name="m", cluster_name="t", cluster=None,
        check_cmd="cargo check", cluster_env={}, fr_path=None,
        tdd_frontier_keys={_DISP}, tdd_frontier_tests=_frontier(),
        tdd_test_cmd="cargo test",
        session_slices={"t_pair": frozenset({_DISP, _IMPL})},
        session_owners={"t_pair": frozenset({_DISP})})
    turn0 = transcripts[0][1]["content"]
    headers = [ln for ln in turn0.splitlines() if ln.startswith("<FILE ")]
    # The container text is the extractor's (spaces around `::`); the claim
    # is the executed/not-executed mark, not the header's punctuation.
    fmt_h = next(h for h in headers if 'editable="fmt (' in h)
    render_h = next(h for h in headers if 'editable="render (' in h)
    assert "[executed by the failing tests]" in fmt_h
    assert "[NOT executed" not in fmt_h
    assert "[NOT executed by the failing tests on the current tree]" in render_h


def test_a_judged_turn_states_which_changed_units_the_targets_execute(
        monkeypatch, tmp_path):
    """Turns 3 to 8 of the live session rewrote an implementer the
    dispatcher never called and nothing in the transcript said so; the
    coverage probe now answers that after every judged turn."""
    import dataclasses as _dc

    from pipeline_core.domain.coverage_slice import CoverageSlice
    ws, tests = _setup(monkeypatch, tmp_path)
    transcripts = _install_turns(monkeypatch, [
        '<FN name="render" block="impl Renderer">self.depth + 7</FN>', ""])
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []), run=(101, _RED))
    deps = _dc.replace(
        pipeline._APP.impl_fill_deps,
        measure_coverage_slice=lambda pd, cl, failing_tests=frozenset(), **k: CoverageSlice(
            owners={"t_pair": frozenset({_DISP})}))
    monkeypatch.setattr(pipeline, "_APP", _dc.replace(pipeline._APP, impl_fill_deps=deps))
    monkeypatch.setattr(pipeline, "_phase_c_fill_impl_blocks",
                        __import__("functools").partial(
                            pipeline._impl_fill.fill_impl_blocks, deps))
    _run(ws, tests, slices={"t_pair": frozenset({_DISP, _IMPL})})
    verdict = transcripts[1][3]["content"]
    assert "changed units they do NOT execute: src/render.rs::render" in verdict
    assert "editable units the targets execute now: src/lib.rs::fmt" in verdict
