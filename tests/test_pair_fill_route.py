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
from pipeline_core.domain.models import (
    FnLocator,
    FnRecord,
    FnSite,
    ImplBlock,
    TypeRecord,
)
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

pub fn peek() -> u32 { 0 }
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


_PEEK = FnLocator("lib.rs", "peek")


def _peek_rec() -> FnRecord:
    return FnRecord(file="lib.rs", fn_name="peek", container="",
                    sig_text="pub fn peek () -> u32", body_text="0")


def _blocks(peek_refill: bool = True) -> list[ImplBlock]:
    disp = FnRecord(file="lib.rs", fn_name="fmt",
                    container="impl fmt::Display for Report",
                    sig_text="fn fmt (& self , f : & mut fmt :: Formatter) -> fmt :: Result")
    impl_ = FnRecord(file="render.rs", fn_name="render",
                     container="impl Renderer",
                     sig_text="pub fn render (& self) -> u32")
    peek = _peek_rec()
    return [
        ImplBlock(file="lib.rs", container="impl fmt::Display for Report",
                  members=(disp,), refill_members=(disp,)),
        ImplBlock(file="render.rs", container="impl Renderer",
                  members=(impl_,), refill_members=(impl_,)),
        ImplBlock(file="lib.rs", container="",
                  members=(peek,),
                  refill_members=(peek,) if peek_refill else ()),
    ]


# A documented `Report`, in the shape rustdoc produces: summary, prose, then
# a sentence introducing a fenced example that states the rendering. The
# attributed test above asserts a literal from that fence.
DOCUMENTED_REPORT = TypeRecord(
    file="lib.rs", kind="struct", name="Report",
    definition="\n".join([
        "/// A report.",
        "///",
        "/// `Report` carries one message.",
        "///",
        "/// The message is whatever the caller handed in; the renderer",
        "/// decides how it reaches a terminal.",
        "///",
        "/// and when the renderer has run:",
        "///",
        "/// ```console",
        "/// rendered hi",
        "/// ```",
        "pub struct Report { msg: String }",
    ]),
)


def _setup(monkeypatch, tmp_path: Path, types: list | None = None) -> Path:
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
        types=types or [], dispatch_families=[])
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
        if prompt.startswith("RESULT of your previous reply"):
            # A retry turn on the pair's conversation (its opening turn,
            # the one carrying PAIRED WRITE, is above it).
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


_CRASH_DETAIL = (
    "This made test binaries ['tests/keep_gen_1.rs'] CRASH before "
    "reporting results (e.g. segfault from bad unsafe code) — they "
    "completed fine before this.")


def _crash_guard(monkeypatch) -> None:
    from types import SimpleNamespace

    from pipeline_core.domain.test_results import RegressionComparison
    cmp = RegressionComparison(
        newly_red=frozenset({"t_keep"}),
        newly_crashed=("tests/keep_gen_1.rs",),
        excluded_crashed=(), not_run=(), measured=("t_keep", "t_pair"),
        base_pass=1, cur_pass=0, base_executed=2, cur_executed=2,
        crash_lost_pass=1)
    monkeypatch.setattr(pipeline, "_regression_guard", SimpleNamespace(
        baseline=lambda *a, **k: object(),
        gate=lambda *a, **k: (cmp, _CRASH_DETAIL)))


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
        # A failing test is dispatched against the pair (a pair no failing
        # test is dispatched against does not travel with the frontier) but
        # convicts nobody and has no source in the workspace.
        tdd_frontier_tests=FailureFrontier.build(
            {_DISP: {"t_elsewhere"}, _IMPL: set()}, {}, pairs=(_PAIR,)),
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


def test_the_pair_prompt_carries_a_documented_types_own_statement_of_a_pin(
        monkeypatch, tmp_path):
    """A type whose doc states an observable the attributed failing test
    pins carries that doc region into the fill prompt — the fenced example
    AND the sentence introducing it, past the leading-lines cap.

    The class (run `617614-0abaf2d0`, 2026-09-01): the pair dispatched with
    its convicting tests, their panics and their source, and still wrapped,
    because the crate's own worked example of the asserted format sat at doc
    line 50 of 101 and the renderer kept three. Every earlier prompt showed
    two lines and an ellipsis instead."""
    ws = _setup(monkeypatch, tmp_path, types=[DOCUMENTED_REPORT])
    tests = _write_tests(ws)
    prompts: list[str] = []
    _install_routed_model(monkeypatch, prompts)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    _run_with_tests(ws, tests)
    (pair_prompt,) = [p for p in prompts if "PAIRED WRITE" in p]
    assert "/// and when the renderer has run:" in pair_prompt
    assert "/// rendered hi" in pair_prompt


def test_negative_control_no_attributed_pin_leaves_the_doc_capped(
        monkeypatch, tmp_path):
    """The pre-fix render, which is also what a target with no attributed
    failing test still gets: the leading window and an ellipsis, with the
    crate's own statement of the format absent. Without this failing, the
    test above proves nothing."""
    ws = _setup(monkeypatch, tmp_path, types=[DOCUMENTED_REPORT])
    tests = _write_tests(ws)
    prompts: list[str] = []
    _install_routed_model(monkeypatch, prompts)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    # Same dispatch, no attributed tests — the frontier convicts nobody, so
    # nothing pins an observable and the doc stays at its leading window.
    pipeline._phase_c_fill_impl_blocks(
        project_dir=ws, target_src_dir=ws / "src", tests_dir_files=tests,
        module_name="m", cluster_name="t", cluster=None,
        check_cmd="cargo check", cluster_env={}, fr_path=None,
        tdd_frontier_keys={_DISP},
        # A failing test is dispatched against the pair (a pair no failing
        # test is dispatched against does not travel with the frontier) but
        # convicts nobody and has no source in the workspace.
        tdd_frontier_tests=FailureFrontier.build(
            {_DISP: {"t_elsewhere"}, _IMPL: set()}, {}, pairs=(_PAIR,)),
        tdd_test_cmd="cargo test",
    )
    (pair_prompt,) = [p for p in prompts if "PAIRED WRITE" in p]
    assert "/// A report." in pair_prompt
    assert "/// and when the renderer has run:" not in pair_prompt
    assert "/// rendered hi" not in pair_prompt


COMPILE_OUT = (
    "error[E0599]: no function or associated item named `from_bytes` "
    "found for struct `OsStr`\n"
    "  --> src/lib.rs:2:20\n"
)


def _dispatcher_only_frontier() -> FailureFrontier:
    """The live pair's grade split: the hop still names both members, but
    coverage convicts only the dispatcher — the implementer is unentered
    because the dispatcher does not yet call it on this arm."""
    return FailureFrontier.build(
        {_DISP: {"t_pair"}, _IMPL: {"t_pair"}},
        {_DISP: {"t_pair"}},
        evidence=_EVIDENCE,
        pairs=(_PAIR,))


def test_an_unentered_implementer_is_still_a_rewrite_on_the_pair(
        monkeypatch, tmp_path):
    """A pair whose convicting tests enter only the dispatcher still
    grades the implementer DIRECT and tells it to write the pinned
    literal. The recorded miss (run `719098-e021d93b`, prompt
    `7919cf60eba2`): REFILL_TARGETS said return the implementer
    UNCHANGED, TARGET_IMPL_BLOCK said rewrite the BEHAVIOR, and the
    reply followed REFILL_TARGETS.

    Negative control is test_no_attributed_tests_means_name_match_only_
    and_no_direct_claim — no donor, no promotion."""
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
        tdd_frontier_tests=_dispatcher_only_frontier(),
        tdd_test_cmd="cargo test",
    )
    (pair_prompt,) = [p for p in prompts if "PAIRED WRITE" in p]
    assert "`fmt` — DIRECT:" in pair_prompt
    assert "`render` — DIRECT:" in pair_prompt
    assert "return it UNCHANGED" not in pair_prompt
    assert "implicated by NAME only" not in pair_prompt
    assert "write the literal in the implementer" in pair_prompt
    assert "rewrite the BEHAVIOR" in pair_prompt


def test_a_compile_fail_retries_with_last_error_and_does_not_lock_the_next_pass(
        monkeypatch, tmp_path):
    """A compile failure is cargo answering, not a regression. The pair
    retries in this call with LAST_ERROR (the builder slot per-impl
    already had) and clears last_prompt_sha on exhaust so the next pass
    is not refused as a resample of the no-error prompt. The recorded
    miss (run `719098-e021d93b`): E0599 OsStr::from_bytes reverted the
    joint write, stamped the no-error prompt's sha, and the next pass
    would have rebuilt that prompt verbatim."""
    import json as _json

    from pipeline_core.adapters.retros import retrospectives_path
    ws = _setup(monkeypatch, tmp_path)
    tests = _write_tests(ws)
    prompts: list[str] = []
    _install_routed_model(monkeypatch, prompts)
    install_scripted_cargo(
        monkeypatch,
        check_diag=(101, COMPILE_OUT, [
            diag("src/lib.rs", code="E0599",
                 message="no function named `from_bytes`")]))
    _run_with_tests(ws, tests)
    first_wave = [p for p in prompts if "PAIRED WRITE" in p]
    assert first_wave, "the pair must dispatch"
    assert "<LAST_ERROR>" not in first_wave[0]
    data = _json.loads(retrospectives_path(ws).read_text())
    (key,) = [k for k in data if "*pair*" in k]
    rec = data[key]
    assert rec.get("last_compile_error")
    assert "from_bytes" in rec["last_compile_error"]
    assert not rec.get("last_prompt_sha"), (
        "compile exhaust must clear the sha so the next pass is not locked")
    # The in-call retry is a turn on the pair's conversation carrying the
    # compile errors — not a rebuilt PAIRED WRITE prompt.
    retries = [p for p in prompts if p.startswith("RESULT of your previous reply")]
    assert len(first_wave) == 1 and retries
    assert "from_bytes" in retries[0].split("<COMPILE_ERRORS>", 1)[1]
    n = len(first_wave)
    _run_with_tests(ws, tests)
    second_wave = [p for p in prompts if "PAIRED WRITE" in p]
    assert len(second_wave) > n, (
        "cleared sha must allow the next pass to send the LAST_ERROR prompt")
    assert "<LAST_ERROR>" in second_wave[n]
    assert "from_bytes" in second_wave[n]


def test_a_crash_revert_puts_the_verdict_in_last_error_on_the_next_fill(
        monkeypatch, tmp_path):
    """A compiling pair that CRASHes sibling binaries stores the crash
    summary on last_error_preview. The next fill's pair prompt must carry
    that verdict in LAST_ERROR — compile already had this slot; a crash
    left it empty, so run `22062-a20ae3a2` fills 1–5 sent the same 94518
    char no-error prompt after every crash-revert.

    Negative control: the first fill of this test has no LAST_ERROR (same
    as the compile-fail test's attempt 1)."""
    import json as _json

    from pipeline_core.adapters.retros import retrospectives_path
    ws = _setup(monkeypatch, tmp_path)
    tests = _write_tests(ws)
    prompts: list[str] = []
    _install_routed_model(monkeypatch, prompts)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    _crash_guard(monkeypatch)
    _run_with_tests(ws, tests)
    first_wave = [p for p in prompts if "PAIRED WRITE" in p]
    assert first_wave, "the pair must dispatch"
    assert "<LAST_ERROR>" not in first_wave[0]
    data = _json.loads(retrospectives_path(ws).read_text())
    (key,) = [k for k in data if "*pair*" in k]
    rec = data[key]
    assert "CRASH" in str(rec.get("last_error_preview") or "")
    n = len(first_wave)
    _run_with_tests(ws, tests)
    second_wave = [p for p in prompts if "PAIRED WRITE" in p]
    assert len(second_wave) > n, (
        "a crash verdict must change the next prompt so it is not refused "
        "as a resample")
    assert "<LAST_ERROR>" in second_wave[n]
    assert "CRASH" in second_wave[n]
    assert "keep_gen_1.rs" in second_wave[n]
    # Compile quotes the rustc line as the approach not to resubmit; a
    # crash summary named the binaries and not the bodies, so run
    # `186665-40892dbd` fill 2 resubmitted fill 1's dispatcher. The
    # previous bodies are the approach.
    assert rec.get("last_failed_body")
    assert "PAIRED" in str(rec.get("last_failed_body") or "")
    assert "PAIRED" in second_wave[n]
    assert "Your previous attempt wrote:" in second_wave[n]


def _clean_guard(monkeypatch) -> None:
    from types import SimpleNamespace
    monkeypatch.setattr(pipeline, "_regression_guard", SimpleNamespace(
        baseline=lambda *a, **k: object(),
        gate=lambda *a, **k: None))


def test_a_kept_pair_write_is_not_followed_by_the_reverted_attempts_last_error(
        monkeypatch, tmp_path):
    """A kept joint write is the last attempt, so the next pair prompt has
    no LAST_ERROR. Run `11358-e5de094e` (2026-09-15) sent fills 4 and 5
    "Your previous attempt compiled and then panicked" over fill 2's crash
    after fill 3 cashed and fill 4 was kept: the keep cleared the compile
    excerpt and the body and left the crash preview and its kind.

    Negative control: before the keep, the same record puts the crash in
    the next prompt (the crash-revert test above pins that sentence)."""
    import json as _json

    from pipeline_core.adapters.retros import retrospectives_path
    from pipeline_core.domain.last_error import LAST_ERROR_KIND
    ws = _setup(monkeypatch, tmp_path)
    tests = _write_tests(ws)
    prompts: list[str] = []
    _install_routed_model(monkeypatch, prompts)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    _crash_guard(monkeypatch)
    _run_with_tests(ws, tests)
    (key,) = [k for k in _json.loads(retrospectives_path(ws).read_text())
              if "*pair*" in k]
    crashed = _json.loads(retrospectives_path(ws).read_text())[key]
    assert "CRASH" in str(crashed.get("last_error_preview") or "")
    assert crashed.get(LAST_ERROR_KIND)

    _clean_guard(monkeypatch)
    n = len([p for p in prompts if "PAIRED WRITE" in p])
    _run_with_tests(ws, tests)
    kept_wave = [p for p in prompts if "PAIRED WRITE" in p][n:]
    assert kept_wave, "the pair must dispatch on the crash's LAST_ERROR"
    assert "<LAST_ERROR>" in kept_wave[0]
    rec = _json.loads(retrospectives_path(ws).read_text())[key]
    assert not rec.get("last_error_preview")
    assert not rec.get(LAST_ERROR_KIND)
    assert not rec.get("last_failed_body")

    from pipeline_core.domain.last_error import kind_from_record
    from pipeline_core.domain.pair_fill import pair_last_error
    after_keep = pair_last_error(
        str(rec.get("last_compile_error") or ""),
        str(rec.get("last_error_preview") or ""),
        str(rec.get("last_failed_body") or ""),
        kind=kind_from_record(rec))
    assert after_keep.text == ""
    before_keep = pair_last_error(
        "", str(crashed.get("last_error_preview") or ""),
        str(crashed.get("last_failed_body") or ""),
        kind=kind_from_record(crashed))
    assert "CRASH" in before_keep.text


def test_a_revert_a_later_keep_superseded_is_not_called_the_previous_attempt(
        monkeypatch, tmp_path):
    """After a joint revert that reddened a sibling, a clean kept pair write
    makes the next pair prompt say an EARLIER attempt was reverted and a
    later one kept — not "the PREVIOUS attempt ... was REVERTED". Run
    `11358-e5de094e` (2026-09-15) sent that sentence in fills 4 and 5 over
    fill 1's revert.

    Negative control: the pre-stamp reading (every record read as the revert
    that wrote it) injected into the same three fills keeps the old label."""
    import pipeline_core.application.reconstruct.impl_fill as _impl

    def _three_fills(ws, tests, prompts):
        _regressing_guard(monkeypatch)
        _run_with_tests(ws, tests)
        _clean_guard(monkeypatch)
        _run_with_tests(ws, tests)
        n = len([p for p in prompts if "PAIRED WRITE" in p])
        _run_with_tests(ws, tests)
        return [p for p in prompts if "PAIRED WRITE" in p][n:]

    ws = _setup(monkeypatch, tmp_path)
    tests = _write_tests(ws)
    prompts: list[str] = []
    _install_routed_model(monkeypatch, prompts)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    third = _three_fills(ws, tests, prompts)
    assert third, "the pair must dispatch after the keep"
    assert "t_keep" in third[0]
    assert "an EARLIER attempt at these blocks was REVERTED" in third[0]
    assert "the PREVIOUS attempt at these blocks was REVERTED" not in third[0]

    from pipeline_core.domain.test_results import RegressedAttempt
    monkeypatch.setattr(_impl, "regressed_attempt_of",
                        lambda *_a, **_k: RegressedAttempt.REVERTED)
    ws2 = _setup(monkeypatch, tmp_path / "control")
    tests2 = _write_tests(ws2)
    prompts2: list[str] = []
    _install_routed_model(monkeypatch, prompts2)
    control = _three_fills(ws2, tests2, prompts2)
    assert control
    assert "the PREVIOUS attempt at these blocks was REVERTED" in control[0]


def test_crash_last_error_quotes_the_previous_bodies() -> None:
    from pipeline_core.domain.pair_fill import crash_last_error
    preview = "This made test binaries ['t.rs'] CRASH before reporting results"
    body = '<FN name="fmt">foo()</FN>'
    assert crash_last_error(preview, "") == preview
    assert crash_last_error("", "") == ""
    assert "foo()" in crash_last_error("", body)
    out = crash_last_error(preview, body)
    assert preview in out and "foo()" in out
    assert "Your previous attempt wrote:" in out


PAIR_REPLY_WITH_SLOT = (
    PAIR_REPLY + '\n<FN name="peek" block="free fns">1</FN>'
)


def _install_crash_callees(monkeypatch):
    monkeypatch.setattr(
        "pipeline_core.application.reconstruct.impl_fill."
        "crash_vtable_callees_from_store",
        lambda *a, **k: frozenset({_PEEK}))


def test_same_file_table_slots_join_the_pair_write(
        monkeypatch, tmp_path):
    """A compiling crash-revert's same-file slot is in the pair prompt
    and splices under the same judgment. Control: without the recovered
    locator the pair is still the two members (existing tests)."""
    ws = _setup(monkeypatch, tmp_path)
    _install_crash_callees(monkeypatch)
    prompts: list[str] = []
    _install_model(monkeypatch, [PAIR_REPLY_WITH_SLOT], prompts)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    res = _run(ws)
    lib = (ws / "src" / "lib.rs").read_text()
    assert "PAIRED" in lib
    assert "fn peek" in lib
    assert "pub fn peek() -> u32 { 0 }" not in lib
    (pair_prompt,) = [p for p in prompts if "PAIRED WRITE" in p]
    assert "TABLE SLOTS" in pair_prompt
    assert "peek" in pair_prompt
    # Same-file slots are table slots, not the faulting frame. The CRASHED
    # grade on fill 1 of run 100178 rewrote them as a memory-fault body
    # and reddened ten already-passing tests.
    assert "`peek` — CRASHED" not in pair_prompt
    assert "faulting frame is this body" not in pair_prompt
    assert "`peek` — DIRECT:" not in pair_prompt
    assert "`peek` — AIMED:" in pair_prompt
    assert (_PEEK[0], _PEEK[1]) in (res.dispatched_fns or ())


def test_a_pair_reply_that_omits_its_table_slot_keeps_nothing(
        monkeypatch, tmp_path):
    """A pair that lands without the recovered slots is the independently
    judged split. Control: the same reply without recovered slots is the
    two-member keep (test_a_pair_dispatches_once)."""
    ws = _setup(monkeypatch, tmp_path)
    _install_crash_callees(monkeypatch)
    prompts: list[str] = []
    _install_model(monkeypatch, [PAIR_REPLY], prompts)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    _run(ws)
    assert (ws / "src" / "lib.rs").read_text() == DISPATCHER_LIB
    assert (ws / "src" / "render.rs").read_text() == IMPLEMENTER_LIB


def _install_remembered_only_callees(monkeypatch):
    """A remembered slot the current crash extract does not name."""
    monkeypatch.setattr(
        "pipeline_core.application.reconstruct.impl_fill."
        "crash_vtable_callees_from_store",
        lambda *a, **k: frozenset())
    monkeypatch.setattr(
        "pipeline_core.application.reconstruct.impl_fill."
        "remembered_pair_callees",
        lambda *a, **k: frozenset({_PEEK}))


def test_a_passing_table_slot_is_quoted_not_rewritten(
        monkeypatch, tmp_path):
    """A remembered slot whose tests already pass, and that the current
    crash extract did not name, is quoted, not marked refill. Omitting
    it from the reply keeps the pair — the tree already holds the body.
    Control: the same slot in the current crash extract is a rewrite
    (test_a_crash_named_passing_slot_is_rewritten)."""
    ws = _setup(monkeypatch, tmp_path)
    install_cluster_scan(
        monkeypatch, impl_blocks=lambda *a, **k: _blocks(peek_refill=False))
    _install_remembered_only_callees(monkeypatch)
    prompts: list[str] = []
    _install_model(monkeypatch, [PAIR_REPLY], prompts)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    res = _run(ws)
    lib = (ws / "src" / "lib.rs").read_text()
    assert "PAIRED" in lib
    assert "pub fn peek() -> u32 { 0 }" in lib
    (pair_prompt,) = [p for p in prompts if "PAIRED WRITE" in p]
    assert "TABLE SLOTS" in pair_prompt
    assert "do NOT emit an <FN> for it" in pair_prompt
    assert "this pair will call this body" in pair_prompt
    assert "rewrite the BEHAVIOR */ 0" not in pair_prompt
    assert "callee of the faulting frame" not in pair_prompt
    assert (_DISP[0], _DISP[1]) in (res.dispatched_fns or ())


def test_a_crash_named_passing_slot_is_rewritten(
        monkeypatch, tmp_path):
    """Plan: a passing slot the current crash extract named is aimed
    refill and splices under the same judgment. Pre-fix (quoting
    because tests pass) kept the pair without rewriting the callee
    the panic stem named. Control: omitting it reverts
    (test_omitting_a_crash_named_passing_slot_keeps_nothing);
    remembered-only still quotes
    (test_a_passing_table_slot_is_quoted_not_rewritten)."""
    ws = _setup(monkeypatch, tmp_path)
    install_cluster_scan(
        monkeypatch, impl_blocks=lambda *a, **k: _blocks(peek_refill=False))
    _install_crash_callees(monkeypatch)
    prompts: list[str] = []
    _install_model(monkeypatch, [PAIR_REPLY_WITH_SLOT], prompts)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    res = _run(ws)
    lib = (ws / "src" / "lib.rs").read_text()
    assert "PAIRED" in lib
    assert "fn peek" in lib
    assert "pub fn peek() -> u32 { 0 }" not in lib
    (pair_prompt,) = [p for p in prompts if "PAIRED WRITE" in p]
    assert "callee of the faulting frame" in pair_prompt
    assert "this response also rewrites the same-file" in pair_prompt
    assert "FAILS — rewrite the BEHAVIOR */ 0" not in pair_prompt
    # Leftover tests belong to the hop endpoints. Stamping them DIRECT
    # onto the extra is how run 609480 fill 1 rewrote a passing slot.
    assert "`peek` — DIRECT:" not in pair_prompt
    assert "`peek` — AIMED:" in pair_prompt
    assert (_PEEK[0], _PEEK[1]) in (res.dispatched_fns or ())


def test_a_slot_already_in_the_last_failed_body_is_quoted(
        monkeypatch, tmp_path):
    """Plan: extras the last crashing reply already emitted stay quoted
    even when the current extract still names them. Omitting them from
    the reply can keep. Control: empty last_failed_body still aims
    (test_a_crash_named_passing_slot_is_rewritten) and omitting then
    keeps nothing (test_omitting_a_crash_named_passing_slot_keeps_nothing).
    Display/Renderer crate."""
    import json as _json

    from pipeline_core.adapters.retros import retro_key, retrospectives_path
    from pipeline_core.domain.pair_fill import pair_record_container
    ws = _setup(monkeypatch, tmp_path)
    install_cluster_scan(
        monkeypatch, impl_blocks=lambda *a, **k: _blocks(peek_refill=False))
    _install_crash_callees(monkeypatch)
    key = str(retro_key(
        _PAIR.dispatcher.file, "*pair*", pair_record_container(_PAIR)))
    retrospectives_path(ws).write_text(_json.dumps({
        key: {"last_failed_body": (
            '<FN name="fmt" block="impl fmt::Display for Report">x</FN>\n'
            '<FN name="peek" block="free fns">None</FN>'
        )},
    }))
    prompts: list[str] = []
    _install_model(monkeypatch, [PAIR_REPLY], prompts)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    _run(ws)
    lib = (ws / "src" / "lib.rs").read_text()
    assert "PAIRED" in lib
    assert "pub fn peek() -> u32 { 0 }" in lib
    (pair_prompt,) = [p for p in prompts if "PAIRED WRITE" in p]
    assert "do NOT emit an <FN> for it" in pair_prompt
    assert "callee of the faulting frame" not in pair_prompt


def test_omitting_a_crash_named_passing_slot_keeps_nothing(
        monkeypatch, tmp_path):
    """Negative control for crash-named passing extras: a reply that
    lands the pair halves but omits the slot is the independently
    judged split. Pre-fix quoted the slot, so the same reply kept."""
    ws = _setup(monkeypatch, tmp_path)
    install_cluster_scan(
        monkeypatch, impl_blocks=lambda *a, **k: _blocks(peek_refill=False))
    _install_crash_callees(monkeypatch)
    prompts: list[str] = []
    _install_model(monkeypatch, [PAIR_REPLY], prompts)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    _run(ws)
    assert (ws / "src" / "lib.rs").read_text() == DISPATCHER_LIB
    assert (ws / "src" / "render.rs").read_text() == IMPLEMENTER_LIB
    (pair_prompt,) = [p for p in prompts if "PAIRED WRITE" in p]
    assert "callee of the faulting frame" in pair_prompt


def test_pair_last_error_a_compiling_crash_drops_the_prior_rustc_excerpt() -> None:
    """Plan: a compiling crash's next LAST_ERROR is the crash and the
    crashing body, not a prior E0308 of a different expression. The live
    pair prompt of 2026-09-14 fill 3 concatenated as_ptr's E0308 with a
    by_ref crash; the diagnostician prescribed as_ptr, compiler_rejected
    retired it, the next fill resubmitted the crash. Control: compile
    excerpt alone is still the LAST_ERROR when there is no crash."""
    from pipeline_core.domain.pair_fill import pair_last_error
    compile_err = (
        "error[E0308]: mismatched types\n"
        "expected `Ref<'_, ErrorImpl>`, found `*mut ErrorImpl`\n"
        "self.inner.ptr.as_ptr()")
    preview = "This made test binaries ['t.rs'] CRASH"
    body = '<FN name="fmt">unsafe { ErrorImpl::debug(self.inner.by_ref(), f) }</FN>'
    out = pair_last_error(compile_err, preview, body)
    assert "CRASH" in out.text
    assert "by_ref" in out.text
    assert "as_ptr" not in out.text
    assert "E0308" not in out.text
    # Negative control: the concatenation this function used to return.
    mixed = compile_err + "\n\n" + preview + "\n\n" + body
    assert "as_ptr" in mixed and "by_ref" in mixed
    assert pair_last_error("", preview, "").text == preview
    assert pair_last_error(compile_err, "", "").text == compile_err
    assert pair_last_error("", "", "").text == ""



def test_a_pair_reply_refused_for_its_text_is_asked_again_with_the_parser(
        monkeypatch, tmp_path):
    """A pair body the splicer refuses for its own text (rustc's ambiguous
    `+` in a cast type) is a failed attempt: both halves are reverted, and
    the retry turn carries the parser's diagnostic. Before, the route
    classified `ANSWER_DROPPED` and returned after one call.
    Control: with `reply_refusal_feedback` deleted no prompt says why."""
    from pipeline_core.application.reconstruct import impl_fill as impl_fill_mod
    from pipeline_core.application.reconstruct import splice as splice_mod
    from pipeline_core.domain.reconstruction import SpliceRefusal
    diag_text = "error: ambiguous `+` in a type\n --> <stdin>:4:40\n"
    cast = ('<FN name="fmt" block="impl fmt::Display for Report">'
            'write!(f, "PAIRED {}", self.msg)</FN>\n'
            '<FN name="render" block="impl Renderer">'
            'unsafe { &*(self as *const Self as *const dyn Tag + Send) }.depth</FN>')

    def _drive(root: Path, *, delete_the_call: bool) -> tuple[Path, list[str]]:
        ws = _setup(monkeypatch, root)
        real_guard = splice_mod.parse_guarded_write

        def _guard(path, new_bytes, pre_bytes, validate_parse):
            if b"dyn Tag + Send" in new_bytes:
                return False
            return real_guard(path, new_bytes, pre_bytes, validate_parse)

        monkeypatch.setattr(splice_mod, "parse_guarded_write", _guard)
        monkeypatch.setattr(splice_mod, "parse_refusal",
                            lambda text, validate: SpliceRefusal("parse_broken", diag_text))
        monkeypatch.setattr(
            impl_fill_mod, "reply_refusal_feedback",
            (lambda *a, **k: None) if delete_the_call
            else splice_feedback_rule)
        prompts: list[str] = []
        _install_model(monkeypatch, [cast, PAIR_REPLY], prompts)
        install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
        _run(ws)
        return ws, prompts

    from pipeline_core.domain.splice_feedback import (
        reply_refusal_feedback as splice_feedback_rule,
    )
    ws, prompts = _drive(tmp_path, delete_the_call=False)
    retries = [p for p in prompts if p.startswith("RESULT of your previous reply")]
    assert retries and "ambiguous `+` in a type" in retries[0]
    assert "render.rs" in retries[0]
    assert "PAIRED" in (ws / "src" / "lib.rs").read_text()
    assert "self.depth + 7" in (ws / "src" / "render.rs").read_text()

    c_ws, c_prompts = _drive(tmp_path / "control", delete_the_call=True)
    assert not any("ambiguous `+` in a type" in p for p in c_prompts)
    assert (c_ws / "src" / "render.rs").read_text() == IMPLEMENTER_LIB


def test_a_refused_half_beside_a_landed_same_file_slot_is_asked_again(
        monkeypatch, tmp_path):
    """A same-file table slot landing puts the file in the spliced set,
    so the refused half is neither a missing file nor a missing extra.
    Both writes revert and the retry carries the parser's diagnostic.
    Before, the route cargo-checked that partial pair and kept the slot.
    Control: with `reply_refusal_feedback` deleted the diagnostic is
    never sent and the refused half stays the stub."""
    from pipeline_core.application.reconstruct import impl_fill as impl_fill_mod
    from pipeline_core.application.reconstruct import splice as splice_mod
    from pipeline_core.domain.reconstruction import SpliceRefusal
    diag_text = "error: ambiguous `+` in a type\n --> <stdin>:4:40\n"
    refused = (
        '<FN name="fmt" block="impl fmt::Display for Report">'
        'unsafe { &*(self as *const Self as *const dyn Tag + Send) }</FN>\n'
        '<FN name="render" block="impl Renderer">self.depth + 7</FN>\n'
        '<FN name="peek" block="free fns">1</FN>')

    def _drive(root: Path, *, delete_the_call: bool) -> tuple[Path, list[str]]:
        ws = _setup(monkeypatch, root)
        _install_crash_callees(monkeypatch)
        real_guard = splice_mod.parse_guarded_write

        def _guard(path, new_bytes, pre_bytes, validate_parse):
            if b"dyn Tag + Send" in new_bytes:
                return False
            return real_guard(path, new_bytes, pre_bytes, validate_parse)

        monkeypatch.setattr(splice_mod, "parse_guarded_write", _guard)
        monkeypatch.setattr(splice_mod, "parse_refusal",
                            lambda text, validate: SpliceRefusal("parse_broken", diag_text))
        monkeypatch.setattr(
            impl_fill_mod, "reply_refusal_feedback",
            (lambda *a, **k: None) if delete_the_call
            else splice_feedback_rule)
        prompts: list[str] = []
        _install_model(monkeypatch, [refused, PAIR_REPLY_WITH_SLOT], prompts)
        install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
        _run(ws)
        return ws, prompts

    from pipeline_core.domain.splice_feedback import (
        reply_refusal_feedback as splice_feedback_rule,
    )
    ws, prompts = _drive(tmp_path, delete_the_call=False)
    retries = [p for p in prompts if p.startswith("RESULT of your previous reply")]
    assert retries and "ambiguous `+` in a type" in retries[0]
    assert "lib.rs" in retries[0]
    lib = (ws / "src" / "lib.rs").read_text()
    assert "PAIRED" in lib
    assert "pub fn peek() -> u32 { 0 }" not in lib
    assert "self.depth + 7" in (ws / "src" / "render.rs").read_text()

    c_ws, c_prompts = _drive(tmp_path / "control", delete_the_call=True)
    assert not any("ambiguous `+` in a type" in p for p in c_prompts)
    assert "PAIRED" not in (c_ws / "src" / "lib.rs").read_text()
    assert 'write!(f, "{}", self.msg)' in (c_ws / "src" / "lib.rs").read_text()
