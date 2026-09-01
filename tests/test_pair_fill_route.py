"""The pair-fill route: a cross-file dispatcher/implementer pair dispatches
as ONE transaction — one prompt carrying both blocks, both replies spliced,
one compile+test reading, kept or reverted together.

The class (run `358833-aa15d93c`, 2026-09-01): the paying write is
co-dependent across two files, and each half alone is correctly rejected by
the revert gate, so no sequence of independently-judged keeps can land it —
the recorded shape was three `src/fmt.rs` opens and two `src/error.rs`
reverts with no joint judgment possible.

The crate here is deliberately NOT that run's shape: a Display dispatcher in
`lib.rs` delegating to a `render.rs` implementer — the never-steer check's
own example. The mechanism reads only the hop's pair record and the
collected blocks; it must fire identically on this shape.
"""
from __future__ import annotations

from pathlib import Path

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

_DISP = FnLocator("lib.rs", "fmt")
_IMPL = FnLocator("render.rs", "render")
_PAIR = FormatterPair(
    FnSite("lib.rs", "fmt", "impl fmt::Display for Report"),
    FnSite("render.rs", "render", "impl Renderer"))

PAIR_REPLY = (
    '<FN name="fmt" block="impl fmt::Display for Report">'
    'write!(f, "PAIRED {}", self.msg)</FN>\n'
    '<FN name="render" block="impl Renderer">self.depth + 7</FN>'
)


def _blocks() -> list[ImplBlock]:
    disp = FnRecord(file="lib.rs", fn_name="fmt",
                    container="impl fmt::Display for Report",
                    sig_text="fn fmt (& self , f : & mut fmt :: Formatter) -> fmt :: Result")
    impl_ = FnRecord(file="render.rs", fn_name="render",
                     container="impl Renderer",
                     sig_text="pub fn render (& self) -> u32")
    return [
        ImplBlock(file="lib.rs", container="impl fmt::Display for Report",
                  members=(disp,), refill_members=(disp,)),
        ImplBlock(file="render.rs", container="impl Renderer",
                  members=(impl_,), refill_members=(impl_,)),
    ]


def _setup(monkeypatch, tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "lib.rs").write_text(DISPATCHER_LIB)
    (ws / "src" / "render.rs").write_text(IMPLEMENTER_LIB)
    monkeypatch.setenv("PHASE_C_KEEP_NONREGRESSING", "0")
    monkeypatch.setenv("PHASE_C_ROUTE_ORDER", "default")
    install_cluster_scan(monkeypatch, impl_blocks=lambda *a, **k: _blocks())
    install_cluster_scan(monkeypatch, stub_records=lambda *a, **k: None)
    install_fake_extractor(
        monkeypatch, fns=lambda sd: pipeline._STRUCTURE.enumerate_fns(sd),
        types=[], dispatch_families=[])
    return ws


def _install_model(monkeypatch, responses, prompts):
    seq = list(responses)

    def _ask(prompt, *a, **k):
        prompts.append(prompt)
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(pipeline, "_ask_raw", _ask)


def _frontier_tests() -> FailureFrontier:
    return FailureFrontier.build(
        {_DISP: {"t_pair"}, _IMPL: {"t_pair"}},
        {_DISP: {"t_pair"}, _IMPL: {"t_pair"}},
        pairs=(_PAIR,))


def _run(ws: Path) -> pipeline.ClusterFillResult:
    return pipeline._phase_c_fill_impl_blocks(  # type: ignore[no-any-return]
        project_dir=ws,
        target_src_dir=ws / "src",
        tests_dir_files=[],
        module_name="m",
        cluster_name="t",
        cluster=None,
        check_cmd="cargo check",
        cluster_env={},
        fr_path=None,
        tdd_frontier_keys={_DISP},
        tdd_frontier_tests=_frontier_tests(),
    )


def test_a_pair_dispatches_once_and_both_halves_land_together(
        monkeypatch, tmp_path):
    ws = _setup(monkeypatch, tmp_path)
    prompts: list[str] = []
    _install_model(monkeypatch, [PAIR_REPLY], prompts)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    res = _run(ws)
    lib = (ws / "src" / "lib.rs").read_text()
    ren = (ws / "src" / "render.rs").read_text()
    assert "PAIRED" in lib and "self.depth + 7" in ren
    # ONE prompt for the pair — not two independent opens — and it carries
    # BOTH target blocks and the paired-write instruction.
    pair_prompts = [p for p in prompts if "PAIRED WRITE" in p]
    assert len(pair_prompts) == 1
    assert 'TARGET_IMPL_BLOCK file="lib.rs"' in pair_prompts[0]
    assert 'TARGET_IMPL_BLOCK file="render.rs"' in pair_prompts[0]
    assert res.fns_filled >= 2
    assert (_DISP[0], _DISP[1]) in (res.dispatched_fns or ())
    assert (_IMPL[0], _IMPL[1]) in (res.dispatched_fns or ())
    # The pair consumed its members for this call: no later route re-opened
    # either half (their refill entries dropped after the pair served them).
    assert len(prompts) == len(pair_prompts)


def test_a_failing_pair_reverts_both_halves_as_one_transaction(
        monkeypatch, tmp_path):
    """Negative control for the transaction: the recorded pre-fix shape was
    half-at-a-time judgments; here a red reading after the joint splice
    restores BOTH files — never a kept half."""
    ws = _setup(monkeypatch, tmp_path)
    prompts: list[str] = []
    _install_model(monkeypatch, [PAIR_REPLY], prompts)
    install_scripted_cargo(
        monkeypatch,
        check_diag=(101, "error: x", [diag("src/lib.rs", code="E0308")]))
    res = _run(ws)
    assert (ws / "src" / "lib.rs").read_text() == DISPATCHER_LIB
    assert (ws / "src" / "render.rs").read_text() == IMPLEMENTER_LIB
    assert any("PAIRED WRITE" in p for p in prompts)
    assert res.impls_failed >= 1
    # Both halves reported reverted, so the landed-change audit grades
    # neither as a measured try.
    assert ("lib.rs", "fmt") in (res.reverted_fns or ())
    assert ("render.rs", "render") in (res.reverted_fns or ())


def test_a_reply_covering_one_half_keeps_nothing(monkeypatch, tmp_path):
    """HALF a pair on the tree is the state the route exists to never
    create: a reply that answers only the implementer leaves both files
    untouched."""
    ws = _setup(monkeypatch, tmp_path)
    prompts: list[str] = []
    _install_model(
        monkeypatch,
        ['<FN name="render" block="impl Renderer">self.depth + 7</FN>'],
        prompts)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    _run(ws)
    assert (ws / "src" / "lib.rs").read_text() == DISPATCHER_LIB
    assert (ws / "src" / "render.rs").read_text() == IMPLEMENTER_LIB


DISPLAY_TEST = """#[test]
fn t_pair() {
    let r = Report { msg: String::from("hi") };
    assert_eq!(format!("{}", r), "rendered hi");
}
"""

KEEP_TEST = """#[test]
fn t_keep() {
    let r = Report { msg: String::from("p") };
    assert_eq!(format!("{}", r), "plain p");
}
"""

RETRO_REPLY = (
    "CATEGORY: WRONG_APPROACH\n"
    "GUIDANCE: Gate the extra render on the measured status so the plain "
    "arm keeps its existing contract.\n")

_EVIDENCE = {"t_pair": ('panicked at tests/display_gen_1.rs:4:5: assertion '
                        '`left == right` failed\n  left: "hi"\n  right: '
                        '"rendered hi"')}


def _write_tests(ws: Path) -> list[Path]:
    (ws / "tests").mkdir(exist_ok=True)
    (ws / "tests" / "display_gen_1.rs").write_text(DISPLAY_TEST)
    (ws / "tests" / "keep_gen_1.rs").write_text(KEEP_TEST)
    return [ws / "tests" / "display_gen_1.rs", ws / "tests" / "keep_gen_1.rs"]


def _evidence_frontier() -> FailureFrontier:
    return FailureFrontier.build(
        {_DISP: {"t_pair"}, _IMPL: {"t_pair"}},
        {_DISP: {"t_pair"}, _IMPL: {"t_pair"}},
        evidence=_EVIDENCE,
        pairs=(_PAIR,))


def _install_routed_model(monkeypatch, prompts: list[str]) -> None:
    """Route by prompt kind: the pair fill gets the paired reply, the retro
    diagnosis its CATEGORY/GUIDANCE lines, anything else an empty reply."""
    def _ask(prompt, *a, **k):
        prompts.append(prompt)
        if "You are diagnosing why a reconstruction attempt" in prompt:
            return RETRO_REPLY
        if "PAIRED WRITE" in prompt:
            return PAIR_REPLY
        return ""

    monkeypatch.setattr(pipeline, "_ask_raw", _ask)


def _capture_logs(monkeypatch) -> list[str]:
    logs: list[str] = []
    monkeypatch.setattr(pipeline, "_log", lambda m: logs.append(str(m)))
    return logs


def _regressing_guard(monkeypatch) -> None:
    from types import SimpleNamespace

    from pipeline_core.domain.test_results import RegressionComparison
    cmp = RegressionComparison(
        newly_red=frozenset({"t_keep"}), newly_crashed=(),
        excluded_crashed=(), not_run=(), measured=("t_keep", "t_pair"),
        base_pass=1, cur_pass=0, base_executed=2, cur_executed=2)
    monkeypatch.setattr(pipeline, "_regression_guard", SimpleNamespace(
        baseline=lambda *a, **k: object(),
        gate=lambda *a, **k: (
            cmp, "This compiled, but it REGRESSED previously-passing "
                 "test(s): ['t_keep'] (cluster pass count 1→0).")))


def _run_with_tests(ws: Path, tests: list[Path]) -> pipeline.ClusterFillResult:
    return pipeline._phase_c_fill_impl_blocks(  # type: ignore[no-any-return]
        project_dir=ws, target_src_dir=ws / "src", tests_dir_files=tests,
        module_name="m", cluster_name="t", cluster=None,
        check_cmd="cargo check", cluster_env={}, fr_path=None,
        tdd_frontier_keys={_DISP},
        tdd_frontier_tests=_evidence_frontier(),
        tdd_test_cmd="cargo test",
    )


def test_the_pair_prompt_carries_its_attributed_tests_and_their_evidence(
        monkeypatch, tmp_path):
    """A pair dispatch carries the source and failure text of the tests its
    members are attributed to: the attributed test's FILE is in the Tests
    section (looked up by the failing test's name, not by an fn-name match
    the test never writes), and REFILL_EVIDENCE quotes what the test said.
    The recorded pre-fix shape — prompt `f226f03a7097` of run
    `537093-ffcfd139`, three name-matched files, no evidence block — is the
    negative this closes."""
    ws = _setup(monkeypatch, tmp_path)
    tests = _write_tests(ws)
    prompts: list[str] = []
    _install_routed_model(monkeypatch, prompts)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    _run_with_tests(ws, tests)
    (pair_prompt,) = [p for p in prompts if "PAIRED WRITE" in p]
    assert "display_gen_1.rs" in pair_prompt
    assert "<REFILL_TARGETS>" in pair_prompt
    assert "t_pair said:" in pair_prompt
    assert 'assertion `left == right` failed' in pair_prompt
    # The frontier convicts both members, so the grade is DIRECT — not the
    # name-match-only INFERRED the old channel produced.
    assert "DIRECT: a failing test's own failure points" in pair_prompt


def test_no_attributed_tests_means_name_match_only_and_no_direct_claim(
        monkeypatch, tmp_path):
    """Second negative control from the plan: a member set with no
    attributed failing tests renders no DIRECT claim and pulls no
    attributed test file — the name-match fallback alone, unchanged."""
    ws = _setup(monkeypatch, tmp_path)
    tests = _write_tests(ws)
    prompts: list[str] = []
    _install_routed_model(monkeypatch, prompts)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    pipeline._phase_c_fill_impl_blocks(
        project_dir=ws, target_src_dir=ws / "src", tests_dir_files=tests,
        module_name="m", cluster_name="t", cluster=None,
        check_cmd="cargo check", cluster_env={}, fr_path=None,
        tdd_frontier_keys={_DISP},
        tdd_frontier_tests=FailureFrontier.build(
            {_DISP: set(), _IMPL: set()}, {}, pairs=(_PAIR,)),
    )
    (pair_prompt,) = [p for p in prompts if "PAIRED WRITE" in p]
    assert "display_gen_1.rs" not in pair_prompt
    assert "DIRECT: a failing test's own failure points" not in pair_prompt


def test_a_joint_revert_is_remembered_and_the_next_prompt_differs(
        monkeypatch, tmp_path):
    """The transaction remembers its own revert. Dispatch 1 regresses and
    both halves revert; the durable record lands with the newly-red set,
    the joint premise and the diagnosis. Dispatch 2 (a fresh engine call,
    as the next traversal pass is) rebuilds a DIFFERENT prompt that carries:
    the reddened tests' names and assertions (the honestly-labelled
    section), their source file, and the screened guidance — so the
    identical-prompt refusal does not fire."""
    import json as _json

    from pipeline_core.adapters.retros import retrospectives_path
    from pipeline_core.domain.retro_gate import JOINT_BODY_SHA, JOINT_MEMBERS
    ws = _setup(monkeypatch, tmp_path)
    tests = _write_tests(ws)
    prompts: list[str] = []
    _install_routed_model(monkeypatch, prompts)
    logs = _capture_logs(monkeypatch)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    _regressing_guard(monkeypatch)
    _run_with_tests(ws, tests)
    assert (ws / "src" / "lib.rs").read_text() == DISPATCHER_LIB
    assert (ws / "src" / "render.rs").read_text() == IMPLEMENTER_LIB
    data = _json.loads(retrospectives_path(ws).read_text())
    (key,) = [k for k in data if "*pair*" in k]
    rec = data[key]
    assert rec["regressed"] == ["t_keep"]
    assert rec[JOINT_MEMBERS] == ["lib.rs::fmt", "render.rs::render"]
    assert rec[JOINT_BODY_SHA]
    assert rec["last_prompt_sha"]
    assert "Gate the extra render" in rec["guidance"]
    first = [p for p in prompts if "PAIRED WRITE" in p]
    assert len(first) == 1
    # Second dispatch: the next pass re-dispatches the pair.
    _run_with_tests(ws, tests)
    pair_prompts = [p for p in prompts if "PAIRED WRITE" in p]
    assert len(pair_prompts) == 2, "the refusal must NOT fire — the prompt changed"
    second = pair_prompts[1]
    assert second != first[0]
    assert "reddened these previously-passing test(s)" in second
    assert "t_keep" in second
    assert "keep_gen_1.rs" in second
    assert 'they assert: "plain p"' in second
    assert "Gate the extra render" in second
    assert not any("rebuilt a byte-identical prompt" in ln for ln in logs)


def test_negative_control_no_memory_means_the_refusal_fires(
        monkeypatch, tmp_path):
    """With the diagnosis suppressed (transient retro failure — no record
    beyond the prompt sha), the second dispatch rebuilds the identical
    prompt, and the route refuses the temperature resample instead of
    silently spending the call — the recorded run's second dispatch would
    have rebuilt `f226f03a7097` verbatim."""
    ws = _setup(monkeypatch, tmp_path)
    tests = _write_tests(ws)
    prompts: list[str] = []

    def _ask(prompt, *a, **k):
        prompts.append(prompt)
        if "You are diagnosing why a reconstruction attempt" in prompt:
            return ""  # transient: empty retro → no record written
        if "PAIRED WRITE" in prompt:
            return PAIR_REPLY
        return ""

    monkeypatch.setattr(pipeline, "_ask_raw", _ask)
    logs = _capture_logs(monkeypatch)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    _regressing_guard(monkeypatch)
    _run_with_tests(ws, tests)
    assert len([p for p in prompts if "PAIRED WRITE" in p]) == 1
    _run_with_tests(ws, tests)
    assert len([p for p in prompts if "PAIRED WRITE" in p]) == 1, (
        "byte-identical rebuild must be refused, not re-sent")
    assert any("rebuilt a byte-identical prompt" in ln for ln in logs)


def test_without_a_pair_the_route_is_inert(monkeypatch, tmp_path):
    """No pair on the frontier → no pair prompt; the ordinary routes keep
    the call (pre-fix behaviour preserved)."""
    ws = _setup(monkeypatch, tmp_path)
    prompts: list[str] = []
    _install_model(monkeypatch, [PAIR_REPLY], prompts)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    pipeline._phase_c_fill_impl_blocks(
        project_dir=ws, target_src_dir=ws / "src", tests_dir_files=[],
        module_name="m", cluster_name="t", cluster=None,
        check_cmd="cargo check", cluster_env={}, fr_path=None,
        tdd_frontier_keys={_DISP},
        tdd_frontier_tests=FailureFrontier.build(
            {_DISP: {"t_pair"}}, {_DISP: {"t_pair"}}),
    )
    assert not any("PAIRED WRITE" in p for p in prompts)
