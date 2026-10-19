"""What a task 3 per-fn ask tells the model about the body it asks for.

Run `641699-ae684bdb` (2026-10-19): the tree ran, the one red test panicked
in the `unimplemented!()` of the second of two same-named definitions, and
the ask for that definition opened "Previous attempt broke cargo check" over
a `<COMPILE_ERRORS>` block holding the trybuild harness's `error[E0599]`,
a test the retry had already excluded. The sibling definition, whose body
was right, got the same excerpt; a body the accept gate reverted
`broke_build` left no trace in any later prompt.

The recorded cargo text of that sweep is the fixture
(`tests/fixtures/task3_retry/641699-ae684bdb_task3_output.txt`). The
attribution outputs below (the convicting site, the stub hold) are what the
live extractor and `make_fn_site_locator` returned on that text and the
tree it was measured on.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pipeline_core.application.reconstruct.attribution import sweep_stub_hold
from pipeline_core.application.reconstruct.cargo_errors import focus_cargo_errors
from pipeline_core.application.reconstruct.fill_rules import AttemptFailure
from pipeline_core.application.reconstruct.per_fn_retry import (
    BodyGate,
    RetryContext,
    RetryDeps,
    apply_per_fn_fixes,
    generate_one_body,
)
from pipeline_core.application.reconstruct.retry_ask import (
    AskForm,
    plan_retry_ask,
    rejection_of,
)
from pipeline_core.domain.attribution import FailureFrontier
from pipeline_core.domain.last_error import LastErrorKind, kind_from_text
from pipeline_core.domain.models import FnLocator, FnRecord, FnSite, ImplBlock
from pipeline_core.domain.reconstruction import SpliceOutcome
from pipeline_core.domain.refill_evidence import (
    Attribution,
    collect_refill_evidence,
    site_attribution,
)
from pipeline_core.domain.retry_outcome import (
    BodyVerdict,
    ClusterReading,
    TreeReading,
    judge_body,
)
from pipeline_core.domain.test_report import failure_evidence_by_test
from pipeline_core.prompts.context import PerFnFixSections
from pipeline_core.prompts.per_fn import build_per_fn_fix_prompt
from tests.conversation_fakes import idle_conversation

FIXTURE = (Path(__file__).parent / "fixtures" / "task3_retry"
           / "641699-ae684bdb_task3_output.txt")
PIN = "context_error_provide_is_invoked_and_forwards"
FILE = "src/context.rs"
STUBBED = "impl StdError for ContextError < C , Error >"
FILLED = "impl StdError for ContextError < C , E >"
HOMONYMS = (FILLED, STUBBED)
# The cap under which `focus_cargo_errors` on the fixture reproduces the live
# prompt's `<COMPILE_ERRORS>` block byte for byte (prompt `48784f837891`).
LIVE_CAP = 12_000


def _sweep_frontier(output: str) -> FailureFrontier:
    """The frontier the retry builds from that sweep: the pin seeds
    `provide`, the call-hop walk gives a callee the seed's test, the panic's
    site convicts the stubbed definition only, the stub holds the pin, and
    the excluded `ui` harness has no words in it."""
    return FailureFrontier.build(
        {FnLocator(FILE, "provide"): {PIN},
         FnLocator("src/error.rs", "chain"): {PIN}},
        {FnSite(FILE, "provide", STUBBED): {PIN}},
        evidence={t: e for t, e in failure_evidence_by_test(output).items()
                  if t != "ui"},
        held={PIN})


def _ask(site: FnSite, body: str, output: str, **kw):
    return plan_retry_ask(
        site=site, body_on_disk=body, homonyms=HOMONYMS, ran=True,
        compile_excerpt=lambda: focus_cargo_errors(output, site.file, LIVE_CAP),
        frontier=_sweep_frontier(output), **kw)


def test_the_stubbed_definition_is_asked_as_a_stub_not_as_a_compile_failure():
    output = FIXTURE.read_text()
    ask = _ask(FnSite(FILE, "provide", STUBBED), "{\n    ::core::unimplemented!()\n}",
               output)
    assert ask.form is AskForm.STUB
    assert "no-impl.rs" not in ask.excerpt
    # Negative control: what the ask was built from before.
    old = focus_cargo_errors(output, FILE, LIVE_CAP)
    assert kind_from_text(old) is LastErrorKind.COMPILE
    assert "no-impl.rs" in old and "panicked at" not in old


def test_the_filled_sibling_is_not_told_the_stub_panic_is_its_failure():
    output = FIXTURE.read_text()
    fr = _sweep_frontier(output)
    ask = _ask(FnSite(FILE, "provide", FILLED),
               "{\n    self.error.provide(request);\n}", output)
    assert ask.form is AskForm.FIX
    assert ask.attribution is Attribution.NAMED
    assert ask.kind is LastErrorKind.UNPASSED
    assert "context.rs:148" not in ask.excerpt
    assert "no-impl.rs" not in ask.excerpt
    # Negative control: the locator reading the fill routes graded by
    # convicts both definitions of the name.
    assert fr.convicting_for(FnLocator(FILE, "provide")) == {PIN}


def test_a_call_hop_callee_is_named_and_carries_no_stub_panic():
    output = FIXTURE.read_text()
    ask = plan_retry_ask(
        site=FnSite("src/error.rs", "chain", "impl Error"),
        body_on_disk="{\n    Chain::new(self.inner.error())\n}",
        homonyms=("impl Error",), ran=True, compile_excerpt=lambda: "",
        frontier=_sweep_frontier(output))
    assert ask.attribution is Attribution.NAMED
    assert "not implemented" not in ask.excerpt
    assert PIN in ask.excerpt


def test_a_convicted_compiling_definition_quotes_its_tests_words():
    """Invented shape: an assertion inside a filled method convicts it."""
    out = ("running 1 test\ntest t_len ... FAILED\n\nfailures:\n\n"
           "---- t_len stdout ----\n\nthread 't_len' panicked at src/a.rs:4:5:\n"
           "assertion `left == right` failed\n  left: 1\n right: 2\n\n"
           "failures:\n    t_len\n\n"
           "test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; "
           "0 filtered out; finished in 0.00s\n")
    fr = FailureFrontier.build(
        {FnLocator("src/a.rs", "len"): {"t_len"}},
        {FnSite("src/a.rs", "len", "impl Bag"): {"t_len"}},
        evidence=failure_evidence_by_test(out))
    ask = plan_retry_ask(
        site=FnSite("src/a.rs", "len", "impl Bag"), body_on_disk="{ 1 }",
        homonyms=("impl Bag",), ran=True, compile_excerpt=lambda: "",
        frontier=fr)
    assert ask.attribution is Attribution.DIRECT
    assert "left: 1" in ask.excerpt and "DIRECT" in ask.excerpt


def test_a_sweep_with_no_verdicts_stays_a_compile_ask():
    out = ("error[E0308]: mismatched types\n --> src/a.rs:3:5\n  |\n"
           "3 |     1u8\n  |     ^^^ expected `u32`, found `u8`\n\n"
           "error: could not compile `a` (lib) due to 1 previous error\n")
    ask = plan_retry_ask(
        site=FnSite("src/a.rs", "len", "impl Bag"), body_on_disk="{ 1u8 }",
        homonyms=("impl Bag",), ran=False,
        compile_excerpt=lambda: focus_cargo_errors(out, "src/a.rs", 4000),
        frontier=None)
    assert ask.form is AskForm.FIX and ask.kind is LastErrorKind.COMPILE
    assert "E0308" in ask.excerpt


def _broken(cluster: str, output: str) -> ClusterReading:
    return ClusterReading(cluster=cluster, ran=False, failures=0,
                          diag_count=1, output=output)


BROKE = ("error[E0034]: multiple applicable items in scope\n"
         " --> src/a.rs:3:20\n  |\n3 |         self.inner.as_ref().go(r)\n"
         "  |                    ^^^^^^ multiple `as_ref` found\n\n"
         "error: could not compile `a` (lib) due to 1 previous error\n")


def test_a_reverted_body_is_the_next_asks_evidence():
    before = TreeReading((ClusterReading("stable", True, 0),
                          ClusterReading("nightly", True, 1)))
    after = TreeReading((ClusterReading("stable", True, 0),
                         _broken("nightly", BROKE)))
    judgement = judge_body(before=before, after=after)
    assert judgement.verdict is BodyVerdict.REVERT_BROKE_BUILD
    rej = rejection_of(judgement, body="self.inner.as_ref().go(r)",
                       file_rel="src/a.rs",
                       focus=lambda o, f: focus_cargo_errors(o, f, 4000))
    assert rej.failure is AttemptFailure.COMPILE
    assert "nightly" in rej.diagnostic and "E0034" in rej.diagnostic
    ask = plan_retry_ask(
        site=FnSite("src/a.rs", "go", "impl Wrap"),
        body_on_disk="{ unimplemented!() }", homonyms=("impl Wrap",),
        ran=True, compile_excerpt=lambda: "", frontier=None,
        rejected=(rej,))
    assert ask.form is AskForm.FIX and ask.kind is LastErrorKind.COMPILE
    assert ask.prev_body == "self.inner.as_ref().go(r)"
    assert "E0034" in ask.excerpt


def test_a_body_that_reddens_is_quoted_with_the_tests_it_reddened():
    out = ("running 2 tests\ntest t_a ... ok\ntest t_b ... FAILED\n\n"
           "failures:\n\n---- t_b stdout ----\n\n"
           "thread 't_b' panicked at src/a.rs:9:5:\nboom\n\n"
           "failures:\n    t_b\n\ntest result: FAILED. 1 passed; 1 failed; "
           "0 ignored; 0 measured; 0 filtered out; finished in 0.00s\n")
    before = TreeReading((ClusterReading("stable", True, 0),))
    after = TreeReading((ClusterReading("stable", True, 1,
                                        failing=frozenset({"t_b"}),
                                        output=out),))
    rej = rejection_of(judge_body(before=before, after=after), body="x()",
                       file_rel="src/a.rs", focus=lambda o, f: "")
    assert rej.failure is AttemptFailure.TEST
    assert "boom" in rej.diagnostic
    ask = plan_retry_ask(
        site=FnSite("src/a.rs", "go", ""), body_on_disk="{ 1 }",
        homonyms=("",), ran=True, compile_excerpt=lambda: "",
        frontier=None, rejected=(rej,))
    assert ask.kind is LastErrorKind.REDDENED


def _src(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    fp = tmp_path / "src" / "a.rs"
    fp.write_text("impl Wrap {\n    fn go(&self) {\n        unimplemented!()\n"
                  "    }\n}\n")
    return fp


def _retry_deps(shown: dict, **kw) -> RetryDeps:
    def _fix(**k):
        shown.update(form="fix", **k)
        return "prompt"

    def _stub(**k):
        shown.update(form="stub", **k)
        return "prompt"
    base = {
        "locate_fn": lambda text, fn: (
            "fn go(&self)", 0, text.index("{\n        "),
            text.index("    }\n}") + 5),
        "locate_all_fns": lambda text, fn: [],
        "gather_fix_context": lambda **k: SimpleNamespace(
            container="impl Wrap", types_section="", notes_section="",
            bound_methods_section="", blueprint_section=""),
        "build_per_fn_fix_prompt": _fix, "build_per_fn_prompt": _stub,
        "ask_per_fn_body": lambda *a, **k: ("self.inner.as_ref().go(r)", ""),
        "open_conversation": idle_conversation,
        "splice_fn_body": lambda *a, **k: SpliceOutcome(True, "ok", 0),
        "splice_body_scoped": lambda *a, **k: SpliceOutcome(True, "ok", 0),
        "find_tests_for_fn": lambda *a, **k: [],
        "focus_cargo_errors": lambda out, rel: focus_cargo_errors(
            out, rel, 4000),
        "is_gen_tests_file": lambda n: False, "log": lambda m: None}
    base.update(kw)
    return RetryDeps(**base)  # type: ignore[arg-type]


RAN = ("running 1 test\ntest t_go ... FAILED\n\nfailures:\n\n"
       "---- t_go stdout ----\n\nthread 't_go' panicked at src/a.rs:3:9:\n"
       "not implemented\n\nfailures:\n    t_go\n\ntest result: FAILED. 0 "
       "passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in "
       "0.00s\n")


def test_the_retry_writes_the_rejection_and_the_next_ask_reads_it(tmp_path):
    """Through the real `apply_per_fn_fixes` and `generate_one_body`: the
    gate reverts the body `broke_build`, the file holds the stub again, and
    the next ask of that definition shows the rejected body and its error —
    not the stub the revert restored (the negative control is the first
    ask, which is the stub prompt)."""
    _src(tmp_path)
    ctx = RetryContext(project_dir=tmp_path, src_root=tmp_path / "src",
                       cluster_label="nightly",
                       containers={FnLocator("a.rs", "go"): "impl Wrap"},
                       type_recs=[], fr_path=None, tests_dir_files=[])
    shown: dict = {}
    deps = _retry_deps(shown)
    good = TreeReading((ClusterReading("nightly", True, 1),))
    gate = BodyGate(read_tree=lambda: TreeReading((_broken("nightly", BROKE),)),
                    baseline=good, log=lambda m: None)
    assert apply_per_fn_fixes([("src/a.rs", "go")], RAN, ctx, deps,
                              gate=gate) == 0
    assert shown["form"] == "stub"
    assert ctx.rejected[("src/a.rs", "go", "impl Wrap")][0].body == (
        "self.inner.as_ref().go(r)")
    shown.clear()
    generate_one_body("src/a.rs", "go", RAN, ctx, deps)
    assert shown["form"] == "fix"
    assert shown["prev_body"] == "self.inner.as_ref().go(r)"
    assert "E0034" in shown["error_excerpt"]
    assert shown["kind"] is LastErrorKind.COMPILE


def test_sweep_stub_hold_names_the_stubbed_definition_not_its_homonym(tmp_path):
    text = ("impl A {\n    fn go(&self) {\n        1;\n    }\n}\n"
            "impl B {\n    fn go(&self) {\n        unimplemented!()\n    }\n}\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.rs").write_text(text)

    def _rec(container: str, start: int) -> dict:
        sig = text.index("fn go", start)
        open_ = text.index("{", sig)
        end = text.index("\n    }", open_) + 6
        return {"file": "src/a.rs", "fn_name": "go", "container": container,
                "sig_byte_start": sig, "body_byte_end": end,
                "body_text": text[open_:end]}
    recs = [_rec("impl A", 0), _rec("impl B", text.index("impl B"))]
    deps = SimpleNamespace(structure=SimpleNamespace(
        enumerate_fns=lambda sd: recs))
    out = RAN.replace("src/a.rs:3:9", "src/a.rs:8:9")
    hold = sweep_stub_hold(deps, out, tmp_path)  # type: ignore[arg-type]
    assert hold.by_test == {"t_go": frozenset({FnSite("src/a.rs", "go", "impl B")})}


def test_the_fill_routes_grade_a_homonym_by_its_own_conviction():
    """`collect_refill_evidence` read DIRECT off the locator union, so the
    filled sibling of a convicted stub was told a failing test's own failure
    pointed at it."""
    out = FIXTURE.read_text()
    fr = _sweep_frontier(out)
    filled = FnRecord(file=FILE, fn_name="provide")
    blocks = (
        ImplBlock(file=FILE, container=FILLED, members=(filled,),
                  refill_members=(filled,)),
        ImplBlock(file=FILE, container=STUBBED,
                  members=(FnRecord(file=FILE, fn_name="provide"),)),
    )
    [ev] = collect_refill_evidence(blocks[0], blocks, fr)
    assert ev.attribution is Attribution.NAMED
    assert site_attribution(fr, FILE, "provide", STUBBED,
                            HOMONYMS) is Attribution.DIRECT


def test_a_named_ask_does_not_open_by_ordering_a_change():
    sections = PerFnFixSections(
        macros="", notes="", container=FILLED, types="", invariants="",
        siblings="", crate_idioms="", bound_methods="")
    kw = {"file_text": "fn provide() {}\n", "fn_name": "provide",
          "sig_text": "fn provide()", "test_blocks": [], "module_name": "m",
          "cluster_name": "nightly", "file_rel": FILE, "prev_body": "{}",
          "error_excerpt": "`provide` — INFERRED: …", "sections": sections,
          "kind": LastErrorKind.UNPASSED}
    named = build_per_fn_fix_prompt(**kw, attribution=Attribution.NAMED)
    assert named.startswith("RE-check ONE Rust function.")
    assert "Don't repeat the previous body" not in named
    assert "previous attempt reverted" not in named
    control = build_per_fn_fix_prompt(**kw)
    assert "Don't repeat the previous body" in control
    assert "previous attempt reverted" in control

