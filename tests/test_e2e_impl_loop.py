"""End-to-end harness tests for the per-impl fill loop (`_phase_c_fill_impl_blocks`)
— FixLoop plan step f1: the net the ImplFixUnit port (f3) lands against.

Block records are scripted via the `_collect_impl_blocks_for_cluster` seam (the
extractor binary is absent in CI); the model seam is `pipeline.ask` (per-impl
protocol: `<FN name="..." block="...">body</FN>`); cargo via the harness
`check_diag` seam. Splice (`_splice_fn_with_where`), revert, and the FloorStall
gauge are the REAL engine on a real temp workspace.
"""
from __future__ import annotations

import json
from pathlib import Path

import pipeline
from pipeline_core.domain.attribution import FailureFrontier
from pipeline_core.domain.models import FnLocator, FnRecord, ImplBlock
from pipeline_core.domain.retro_gate import (
    ConvictionEvidence,
    RetroProvenance,
    stamp_conviction,
)
from tests.harness import (
    diag,
    first_user,
    install_app_seams,
    install_cluster_scan,
    install_fake_extractor,
    install_scripted_cargo,
    install_scripted_turns,
)

IMPL_LIB = """pub struct Foo { tag: u32 }

impl Foo {
    pub fn kind(&self) -> u32 {
        unimplemented!()
    }
    pub fn name(&self) -> u32 {
        unimplemented!()
    }
}
"""

RESPONSE_BOTH = (
    '<FN name="kind" block="impl Foo">self.tag</FN>\n'
    '<FN name="name" block="impl Foo">self.tag + 1</FN>'
)


def _block() -> ImplBlock:
    members = (
        FnRecord(file="lib.rs", fn_name="kind", container="impl Foo",
                 sig_text="fn kind (& self) -> u32", has_unimplemented_body=True),
        FnRecord(file="lib.rs", fn_name="name", container="impl Foo",
                 sig_text="fn name (& self) -> u32", has_unimplemented_body=True),
    )
    return ImplBlock(file="lib.rs", container="impl Foo",
                     members=members, stub_members=members)


def _setup(monkeypatch, tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "lib.rs").write_text(IMPL_LIB)
    # Keep-while-red is env-driven and .env sets it ON for live runs — pin it OFF here so
    # the revert-path tests are environment-independent; the keep-while-red test pins it ON.
    monkeypatch.setenv("PHASE_C_KEEP_NONREGRESSING", "0")
    # Route order resolves from the task2 MODEL when the env is silent
    # (domain/model_defaults.py), and the operator's .env may name a model
    # whose default is file_primary — which dispatches the file route ahead
    # of the per-block loop these tests assert on. The model here is a
    # scripted fake, so pin the historical ladder explicitly.
    monkeypatch.setenv("PHASE_C_ROUTE_ORDER", "default")
    # The prompt-size budget is the same class of dependency but CANNOT be pinned
    # here: `pipeline.MODEL_CONTEXT_CHARS` is snapshotted at import (pipeline.py,
    # `SETTINGS = Settings.from_env(...)`) and baked into the frozen `_ASK_CLIENT`,
    # so a monkeypatch.setenv at call time changes nothing. It is pinned in the
    # root conftest.py instead, before `import pipeline` binds it.
    install_cluster_scan(monkeypatch, impl_blocks=lambda *a, **k: [_block()])
    install_cluster_scan(monkeypatch, stub_records=lambda *a, **k: None)
    # fns passes through to the REAL reader (these tests exercise the syn
    # extractor's record-based splice); only types/dispatch are stubbed.
    install_fake_extractor(
        monkeypatch, fns=lambda sd: pipeline._STRUCTURE.enumerate_fns(sd),
        types=[], dispatch_families=[])
    return ws


def _install_model(monkeypatch, responses):
    seq = list(responses)
    monkeypatch.setattr(pipeline, "_ask_raw",
                        lambda *a, **k: seq.pop(0) if len(seq) > 1 else seq[0])


def _run(ws: Path) -> pipeline.ClusterFillResult:
    # target_src_dir is the dir block["file"] is relative to (lib.rs → src/).
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
        tdd_frontier_keys=None,
    )


def test_impl_block_filled_in_one_call(monkeypatch, tmp_path):
    ws = _setup(monkeypatch, tmp_path)
    _install_model(monkeypatch, [RESPONSE_BOTH])
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    res = _run(ws)
    text = (ws / "src" / "lib.rs").read_text()
    assert res.impls_filled == 1 and res.fns_filled == 2
    assert "self.tag" in text and "unimplemented!()" not in text
    assert res.remaining == 0


def test_impl_floor_stall_reverts_block(monkeypatch, tmp_path):
    ws = _setup(monkeypatch, tmp_path)
    _install_model(monkeypatch, [RESPONSE_BOTH])
    # Distinct E-code each attempt (sig never repeats), constant count=1 →
    # floor never improves; PHASE_C_IMPL_MAX_STALLS (3) exhausts on attempt 4.
    codes = iter(["E0308", "E0425", "E0277", "E0599", "E0061"])
    install_scripted_cargo(
        monkeypatch,
        check_diag=lambda: (101, "error: x",
                            [diag("src/lib.rs", code=next(codes, "E0999"))]))
    res = _run(ws)
    text = (ws / "src" / "lib.rs").read_text()
    assert res.impls_filled == 0 and res.impls_failed == 1
    # Round 1: floor stall exhausts at 4, then the maximal last-resort turn
    # (a model call, counted since it became a turn on the block's
    # conversation). Retro round 1 baselines -> one informed retry; round 2
    # repeats E0999 twice -> repeated exit at 2.
    assert res.attempts == 7
    assert text == IMPL_LIB              # reverted to pristine stubs
    # Extractor seam returns None → remaining must come from the text-scan
    # fallback (was 0, silently skipping the caller's per-fn cleanup pass).
    assert res.remaining == 2


def test_impl_repeated_error_signature_stops_after_two(monkeypatch, tmp_path):
    """A repeated error set is the block's outcome for this fill.

    Two identical compile failures stop it. The retrospective is stored.
    The loop is not re-entered, the maximal turn is not bought, and the
    green check waiting after those two is never reached. The pre-fill
    stubs stay."""
    ws = _setup(monkeypatch, tmp_path)
    _install_model(monkeypatch, [RESPONSE_BOTH])
    install_scripted_cargo(
        monkeypatch,
        check_diag=[(101, "error: x", [diag("src/lib.rs", code="E0308")]),
                    (101, "error: x", [diag("src/lib.rs", code="E0308")]),
                    (0, "", [])])
    res = _run(ws)
    assert res.impls_filled == 0 and res.impls_failed == 1
    assert res.attempts == 2
    assert (ws / "src" / "lib.rs").read_text() == IMPL_LIB
    from pipeline_core.adapters.retros import load_retrospectives, retro_key
    rec = load_retrospectives(ws)[retro_key("lib.rs", "*impl*", "impl Foo")]
    assert rec["retrospective_count"] == 1
    ev_path = Path(pipeline._EVENTS_LOG)
    ev = ev_path.read_text(encoding="utf-8") if ev_path.exists() else ""
    assert "phase_c.impl.retro_retry" not in ev
    assert "phase_c.impl.fill.maximal" not in ev
    assert "phase_c.impl.fill.salvaged" not in ev


def test_impl_repeated_stop_keeps_no_compiling_image(monkeypatch, tmp_path):
    """The same failing set on two attempts that compiled. Salvage is
    holding that image, and the fill does not write it back: the pre-fill
    stubs stay."""
    from pipeline_core.domain.test_report import U2Verdict
    ws = _setup(monkeypatch, tmp_path)
    _install_model(monkeypatch, [RESPONSE_BOTH])
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    install_app_seams(
        monkeypatch,
        u2_test_gate=lambda *a, **k: U2Verdict.reject(
            frozenset({"test_kind"}), "test kind ... FAILED"))
    res = _run(ws)
    assert res.impls_filled == 0 and res.impls_failed == 1
    assert res.attempts == 2
    assert (ws / "src" / "lib.rs").read_text() == IMPL_LIB


def test_impl_retry_prompt_carries_the_previous_attempts_errors(
        monkeypatch, tmp_path):
    """Attempt 2 of the per-impl fix loop must SEE what attempt 1 broke.

    Its only error feedback used to be TYPES_IN_SCOPE re-selection from the
    error text, so an error naming no new types rebuilt attempt 1's prompt
    byte-identically and the retry was a temperature resample of an unchanged
    question — both E0277 units of run `275772-425da59c` (2026-08-19) sent
    the SAME prompt sha on attempts 1 and 2 (18:48:29 and 18:58:59) while
    other units' attempt-2 prompts differed, and each burned its whole fix
    loop on it. The negative control is the first attempt: no previous error,
    no LAST_ERROR section — which is also what guarantees attempt 2's prompt
    can never equal attempt 1's."""
    ws = _setup(monkeypatch, tmp_path)
    sent = install_scripted_turns(monkeypatch, RESPONSE_BOTH)
    install_scripted_cargo(
        monkeypatch,
        check_diag=(101, "error[E0308]: mismatched types --> src/lib.rs:4",
                    [diag("src/lib.rs", code="E0308")]))
    _run(ws)
    impl_calls = [t for t in sent if "TARGET_IMPL_BLOCK" in first_user(t)]
    assert len(impl_calls) >= 2, "the fix loop must have retried"
    first, second = impl_calls[0], impl_calls[1]
    assert "<LAST_ERROR>" not in first_user(first), \
        "attempt 1 has no previous attempt to report"
    # Attempt 2 is a turn on the same conversation: the opening prompt and
    # the model's own reply are above it, and the turn carries the errors.
    assert [m["role"] for m in second][:4] == ["system", "user", "assistant", "user"]
    assert second[2]["content"] == RESPONSE_BOTH
    retry = second[3]["content"]
    assert "TARGET_IMPL_BLOCK" not in retry, "a retry is not a rebuilt prompt"
    section = retry.split("<COMPILE_ERRORS>", 1)[1].split("</COMPILE_ERRORS>", 1)[0]
    assert "E0308" in section


def test_impl_prompt_carries_its_whole_file_read_each_attempt(
        monkeypatch, tmp_path):
    """The per-impl prompt shows the whole file its block lives in, read
    fresh per attempt. It used to carry only the target block, the
    same-trait related blocks and other files' API, and 66 of the 105
    context requests per-impl fills made from 2026-09-01 to 2026-09-24
    asked for exactly that file. Read back through the prompt's own
    grammar: a declared whole view of `src/lib.rs`."""
    from pipeline_core.domain.prompt_holdings import FileView, read_holdings
    ws = _setup(monkeypatch, tmp_path)
    prompts: list[str] = []

    def _capture(prompt, *a, **k):
        prompts.append(prompt)
        return RESPONSE_BOTH

    monkeypatch.setattr(pipeline, "_ask_raw", _capture)
    install_scripted_cargo(
        monkeypatch,
        check_diag=(101, "error[E0308]: mismatched types --> src/lib.rs:4",
                    [diag("src/lib.rs", code="E0308")]))
    _run(ws)
    impl_prompts = [p for p in prompts if "TARGET_IMPL_BLOCK" in p]
    assert impl_prompts
    held = read_holdings(impl_prompts[0]).whole_view("src/lib.rs")
    assert held is not None and held.view is FileView.WHOLE
    assert held.text.strip() == IMPL_LIB.strip()


def test_impl_b3_keeps_nonregressing_fill(monkeypatch, tmp_path):
    # Keep-while-red: crate still has a PRE-EXISTING error elsewhere, but this
    # block's fill adds no NEW error and none lands on the filled fns →
    # keep the fill instead of reverting (convergence-deadlock dissolver).
    ws = _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("PHASE_C_KEEP_NONREGRESSING", "1")
    _install_model(monkeypatch, [RESPONSE_BOTH])
    elsewhere = diag("src/other.rs", code="E0425")
    install_scripted_cargo(
        monkeypatch,
        # 1st call = keep-while-red baseline, 2nd = post-splice verify: same single error.
        check_diag=[(101, "error: y", [elsewhere]),
                    (101, "error: y", [elsewhere])],
        frontier={},   # no diagnostic attributes to the filled fns
    )
    res = _run(ws)
    text = (ws / "src" / "lib.rs").read_text()
    assert res.impls_filled == 1 and res.fns_filled == 2
    assert "self.tag" in text and "unimplemented!()" not in text


def test_impl_green_but_test_failing_fill_is_kept_deferred_not_cashed(monkeypatch, tmp_path):
    # When the crate COMPILES (rc==0) but the block's own U2 test fails and
    # none of its tests was passing at baseline, the body is KEPT on
    # non-regression and judged at the next sweep — the one bar every route
    # applies (the file route always did; this route used to require crash
    # evidence and otherwise reverted, then salvaged the same body without
    # crediting it). It is a landing (`impls_filled`), never a CASH: the
    # fill class is `unreachable`, the disposition KEEP_DEFERRED, and the
    # behavioral re-fill route retries the block off the sweep's verdict.
    #
    # What stays IN THE TREE is the compiling body, not the stub:
    # `unimplemented!()` panics, which aborts the whole test binary and
    # leaves every sibling test in it without a verdict, so the compiling
    # body is the strictly better end state as long as it reddens nothing
    # (checked by the regression gate before it is kept). See
    # IMPL_LIB_WRONG_KIND below.
    from pipeline_core.domain.models import FnLocator
    ws = _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("PHASE_C_KEEP_NONREGRESSING", "1")
    _install_model(monkeypatch, [RESPONSE_BOTH])
    # Green compile, but the scoped test run reports a failing own-test.
    install_scripted_cargo(
        monkeypatch,
        check_diag=(0, "", []),
        # A real assertion failure, with the stdout section libtest always
        # prints for one — a bare FAILED line with no captured output means an
        # aborted process, which carries no verdict and must not convict.
        run=(101, "test kind_works ... FAILED\n"
                  "\nfailures:\n\n"
                  "---- kind_works stdout ----\n"
                  "thread 'kind_works' panicked at tests/t.rs:3:5:\n"
                  "assertion `left == right` failed\n"
                  "  left: 1\n right: 2\n"
                  "\nfailures:\n    kind_works\n"
                  "\ntest result: FAILED. 0 passed; 1 failed; 0 ignored"))
    res = pipeline._phase_c_fill_impl_blocks(
        project_dir=ws, target_src_dir=ws / "src", tests_dir_files=[],
        module_name="m", cluster_name="t", cluster=None, check_cmd="cargo check",
        cluster_env={}, fr_path=None, tdd_frontier_keys=None,
        tdd_test_cmd="cargo test",
        tdd_frontier_tests=FailureFrontier.of_direct({FnLocator("lib.rs", "kind"): {"kind_works"},
                            FnLocator("lib.rs", "name"): {"kind_works"}}))
    text = (ws / "src" / "lib.rs").read_text()
    assert res.impls_filled == 1            # landed and kept, deferred
    assert res.fill_classes is not None
    assert res.fill_classes.cashed == 0 and res.fill_classes.unreachable == 1
    # Kept, not re-stubbed: the compiling body survives so the binary runs.
    assert "unimplemented!()" not in text
    assert "self.tag" in text


# --- per-impl behavioral re-fill (block-level, no stubs left) -----------------

IMPL_LIB_WRONG_KIND = """pub struct Foo { tag: u32 }

impl Foo {
    pub fn kind(&self) -> u32 {
        self.tag + 999
    }
    pub fn name(&self) -> u32 {
        self.tag + 1
    }
}
"""

RESPONSE_KIND_ONLY = '<FN name="kind" block="impl Foo">self.tag</FN>'


def _refill_block() -> ImplBlock:
    # NO stub_members — both members already compile. `kind`'s body is WRONG
    # (a prior fill/refill attempt), `name`'s is correct. Mirrors the anyhow
    # seal/verify co-design ceiling: the block has zero stubs left, yet one
    # member's attributed test still fails.
    members = (
        FnRecord(file="lib.rs", fn_name="kind", container="impl Foo",
                 sig_text="fn kind (& self) -> u32", body_text="self.tag + 999",
                 has_unimplemented_body=False),
        FnRecord(file="lib.rs", fn_name="name", container="impl Foo",
                 sig_text="fn name (& self) -> u32", body_text="self.tag + 1",
                 has_unimplemented_body=False),
    )
    return ImplBlock(file="lib.rs", container="impl Foo",
                     members=members, stub_members=())


def test_refill_only_block_reaches_llm_and_splices_with_allow_nonstub(
        monkeypatch, tmp_path):
    # A block with stub_members=() would previously never be
    # collected (collect_impl_blocks_for_cluster drops all-filled blocks) nor
    # reach `_fill_one_block` (`_viable` required stub_members). With a TDD
    # frontier naming `kind` as test-failing, `collect_impl_refill_targets`
    # must stamp `refill_members=(kind,)`, `_viable` must accept it, and the
    # splice must allow overwriting `kind`'s non-stub body.
    from pipeline_core.domain.models import FnLocator
    ws = _setup(monkeypatch, tmp_path)
    (ws / "src" / "lib.rs").write_text(IMPL_LIB_WRONG_KIND)
    install_cluster_scan(monkeypatch, impl_blocks=lambda *a, **k: [_refill_block()])
    _install_model(monkeypatch, [RESPONSE_KIND_ONLY])
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    res = pipeline._phase_c_fill_impl_blocks(
        project_dir=ws, target_src_dir=ws / "src", tests_dir_files=[],
        module_name="m", cluster_name="t", cluster=None, check_cmd="cargo check",
        cluster_env={}, fr_path=None,
        tdd_frontier_keys={FnLocator("lib.rs", "kind")},
        tdd_frontier_tests=FailureFrontier.of_direct({FnLocator("lib.rs", "kind"): {"kind_is_wrong"}}))
    text = (ws / "src" / "lib.rs").read_text()
    assert res.impls_filled == 1 and res.fns_filled == 1
    assert "self.tag + 999" not in text         # wrong `kind` body replaced
    assert "self.tag + 1" in text               # `name` untouched (not a target)


def test_block_with_no_refill_target_is_not_collected(monkeypatch, tmp_path):
    # Control: same all-filled block, but the frontier doesn't name `kind` as
    # test-failing → no refill_members → the block stays non-viable, no LLM call.
    ws = _setup(monkeypatch, tmp_path)
    (ws / "src" / "lib.rs").write_text(IMPL_LIB_WRONG_KIND)
    install_cluster_scan(monkeypatch, impl_blocks=lambda *a, **k: [_refill_block()])
    def _no_call(*_a, **_k):
        raise AssertionError("model must not be asked without a refill target")
    monkeypatch.setattr(pipeline, "_ask_raw", _no_call)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    res = pipeline._phase_c_fill_impl_blocks(
        project_dir=ws, target_src_dir=ws / "src", tests_dir_files=[],
        module_name="m", cluster_name="t", cluster=None, check_cmd="cargo check",
        cluster_env={}, fr_path=None, tdd_frontier_keys=None,
        tdd_frontier_tests=None)
    assert res.impls_filled == 0 and res.impls_failed == 0
    assert res.attempts == 0


# --- stale stub_members / already-filled blocks -------------------------------

IMPL_LIB_FILLED = IMPL_LIB.replace("unimplemented!()", "7")


def test_already_complete_block_skips_llm_call(monkeypatch, tmp_path):
    """Freshness gate: the re-scan says no member of this block is still a
    stub → skip without prompting, and the block is NOT counted failed."""
    ws = _setup(monkeypatch, tmp_path)
    (ws / "src" / "lib.rs").write_text(IMPL_LIB_FILLED)
    # Fresh scan authority: no stubs anywhere (empty list ≠ None).
    install_cluster_scan(monkeypatch, stub_records=lambda *a, **k: [])
    def _no_call(*_a, **_k):
        raise AssertionError("model must not be asked for a complete block")
    monkeypatch.setattr(pipeline, "_ask_raw", _no_call)
    res = _run(ws)
    assert res.impls_failed == 0 and res.impls_filled == 0
    assert res.attempts == 0


def test_partial_staleness_refills_only_remaining_stub(monkeypatch, tmp_path):
    """One member already filled by an earlier family fill: the block record's
    stub list is refreshed, the filled fn's returned body refuses
    (target_not_stub) and the still-stub member splices."""
    ws = _setup(monkeypatch, tmp_path)
    half = IMPL_LIB.replace(
        "pub fn kind(&self) -> u32 {\n        unimplemented!()\n    }",
        "pub fn kind(&self) -> u32 {\n        9\n    }")
    (ws / "src" / "lib.rs").write_text(half)
    install_app_seams(
        monkeypatch,
        scan_stub_records_for_cluster=lambda *a, **k: [
            {"file": "lib.rs", "fn_name": "name", "container": "impl Foo"}])
    _install_model(monkeypatch, [RESPONSE_BOTH])
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    res = _run(ws)
    text = (ws / "src" / "lib.rs").read_text()
    assert res.impls_filled == 1 and res.impls_failed == 0
    assert res.fns_filled == 1
    assert "self.tag + 1" in text          # `name` filled from the reply
    assert "9" in text                     # earlier `kind` fill untouched


def test_all_target_not_stub_refusals_count_as_complete(monkeypatch, tmp_path):
    """Race remnant: the freshness gate is unavailable (extractor absent →
    scan None) and every routed body refuses target_not_stub → the block is
    complete, not failed, and stops after one attempt."""
    ws = _setup(monkeypatch, tmp_path)
    (ws / "src" / "lib.rs").write_text(IMPL_LIB_FILLED)
    _install_model(monkeypatch, [RESPONSE_BOTH])
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    res = _run(ws)
    assert res.impls_failed == 0 and res.impls_filled == 0
    assert res.attempts == 1
    assert (ws / "src" / "lib.rs").read_text() == IMPL_LIB_FILLED


# --- Maximal last-resort attempt (budgets off) --------------------------------


def _two_reds_then_green():
    """Two compile failures with different codes, then a clean check.

    The codes differ, so the repeated-signature stop does not fire. With
    PHASE_C_IMPL_MAX_STALLS=1 the second failure stalls the floor."""
    return [(101, "error: x", [diag("src/lib.rs", code="E0308")]),
            (101, "error: x", [diag("src/lib.rs", code="E0425")]),
            (0, "", [])]


def test_maximal_last_resort_rescues_exhausted_block(monkeypatch, tmp_path):
    """The floor stalls on two different error codes, then the ONE maximal
    attempt — whole file + tests + full error — fills the block. A repeated
    error set does not buy this turn."""
    ws = _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("PHASE_C_IMPL_MAX_STALLS", "1")
    _install_model(monkeypatch, [RESPONSE_BOTH])
    install_scripted_cargo(monkeypatch, check_diag=_two_reds_then_green())
    res = _run(ws)
    text = (ws / "src" / "lib.rs").read_text()
    assert res.impls_filled == 1 and res.fns_filled == 2
    assert res.impls_failed == 0
    # Two loop attempts and the maximal turn — a model call on the block's
    # conversation, counted as one.
    assert res.attempts == 3
    assert "self.tag" in text and "unimplemented!()" not in text


def test_maximal_disabled_by_env(monkeypatch, tmp_path):
    ws = _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("PHASE_C_IMPL_MAX_STALLS", "1")
    monkeypatch.setenv("PHASE_C_MAXIMAL_LAST_RESORT", "0")
    # Cap retro rounds to 1 so no informed retry fires either — this test
    # pins ONLY that the maximal-last-resort path is env-disabled.
    monkeypatch.setenv("PER_FN_RETRO_ROUNDS_MAX", "1")
    _install_model(monkeypatch, [RESPONSE_BOTH])
    install_scripted_cargo(monkeypatch, check_diag=_two_reds_then_green())
    res = _run(ws)
    assert res.impls_failed == 1        # green 3rd check never reached
    assert (ws / "src" / "lib.rs").read_text() == IMPL_LIB


def test_impl_retro_retry_recovers_on_second_round(monkeypatch, tmp_path):
    # The value pin: round 1 exhausts on a stalled floor (two different
    # codes, so this is not the repeated-signature stop), the retro's
    # guidance feeds an immediate second round whose fill compiles clean.
    ws = _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("PHASE_C_IMPL_MAX_STALLS", "1")
    monkeypatch.setenv("PHASE_C_MAXIMAL_LAST_RESORT", "0")
    _install_model(monkeypatch, [RESPONSE_BOTH, RESPONSE_BOTH, RESPONSE_BOTH])
    install_scripted_cargo(monkeypatch, check_diag=_two_reds_then_green())
    res = _run(ws)
    assert res.impls_filled == 1 and res.impls_failed == 0
    assert res.attempts == 3            # 2 failed + 1 green post-retro


def test_noncompiling_block_hands_its_stubs_to_the_per_fn_route(
        monkeypatch, tmp_path):
    # src/error.rs::Error ground the whole of iteration 1 in runs
    # 2370111-1800b8b9, 2452699-e19bc335 and 2509252-f1f8c11b (2026-08-17):
    # the floor moved 4 -> 3 with stall=0, every gate read that as progress,
    # and after 89 minutes the file still held the same 39 `unimplemented!()`
    # it started with. This route splices a block's members as a SET and
    # reverts the file on any error, so a block that never compiles has
    # nothing to keep however far its floor falls.
    ws = _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("PHASE_C_IMPL_MAX_STALLS", "1")
    monkeypatch.setenv("PHASE_C_MAXIMAL_LAST_RESORT", "0")
    monkeypatch.setenv("PHASE_C_IMPL_NONCOMPILING_ROUNDS_MAX", "1")
    _install_model(monkeypatch, [RESPONSE_BOTH])
    # The codes alternate, so the repeated-signature stop does not fire and
    # the cap is what ends the block.
    install_scripted_cargo(
        monkeypatch, check_diag=_alternating_compile_errors("E0308", "E0425"))
    res = _run(ws)
    assert res.impls_filled == 0 and res.impls_failed == 1
    assert (ws / "src" / "lib.rs").read_text() == IMPL_LIB
    # The stubs survive for the per-fn route, which gates each body on its own
    # `cargo check` — that is the whole point of handing off.
    assert res.remaining == 2
    assert res.attempts == 2          # round 1 only, then handed off

    # Negative control: with the cap lifted the pre-fix behaviour reproduces —
    # a block that has compiled NOTHING still buys another round.
    ws2 = _setup(monkeypatch, tmp_path / "b")
    monkeypatch.setenv("PHASE_C_IMPL_NONCOMPILING_ROUNDS_MAX", "99")
    _install_model(monkeypatch, [RESPONSE_BOTH])
    install_scripted_cargo(
        monkeypatch, check_diag=_alternating_compile_errors("E0308", "E0425"))
    res2 = _run(ws2)
    assert res2.impls_filled == 0
    assert res2.attempts == 4, res2.attempts


def _alternating_compile_errors(*codes: str):
    """A cargo script whose error code alternates, so consecutive attempts
    are not a repeated signature."""
    n = {"i": 0}

    def _check():
        code = codes[n["i"] % len(codes)]
        n["i"] += 1
        return (101, "error: x", [diag("src/lib.rs", code=code)])

    return _check


def test_noncompiling_cap_does_not_fire_once_something_compiled(
        monkeypatch, tmp_path):
    # The cap keys on "never once compiled", not on "failed". Round 1 stalls
    # on two different codes (not a repeated set) without a compiling image,
    # and the retro round still runs and lands the block.
    ws = _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("PHASE_C_IMPL_MAX_STALLS", "1")
    monkeypatch.setenv("PHASE_C_MAXIMAL_LAST_RESORT", "0")
    _install_model(monkeypatch, [RESPONSE_BOTH, RESPONSE_BOTH, RESPONSE_BOTH])
    install_scripted_cargo(monkeypatch, check_diag=_two_reds_then_green())
    res = _run(ws)
    assert res.impls_filled == 1 and res.impls_failed == 0


# --- Pull protocol through the impl fill path ---------------------------------


def test_pull_request_answered_then_block_fills(monkeypatch, tmp_path):
    """Model asks for a file first (bounded pull), gets it, then delivers the
    bodies — block fills in one loop attempt."""
    ws = _setup(monkeypatch, tmp_path)
    _install_model(monkeypatch, [
        '<REQUEST kind="file" arg="lib.rs"/>', RESPONSE_BOTH])
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    res = _run(ws)
    text = (ws / "src" / "lib.rs").read_text()
    assert res.impls_filled == 1 and res.fns_filled == 2
    assert "self.tag" in text and "unimplemented!()" not in text


def test_pulls_disabled_by_env(monkeypatch, tmp_path):
    """CONTEXT_PULLS=0: a REQUEST reply is just a no-FN response — the loop
    treats it as a failed attempt instead of answering it."""
    ws = _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("CONTEXT_PULLS", "0")
    monkeypatch.setenv("PHASE_C_MAXIMAL_LAST_RESORT", "0")
    _install_model(monkeypatch, ['<REQUEST kind="file" arg="lib.rs"/>'])
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    res = _run(ws)
    assert res.impls_filled == 0 and res.impls_failed == 1


# --- File-first fill for multi-block files -------------------------------------

IMPL_LIB_TWO = """\
pub struct Foo { tag: u32 }
pub struct Bar { tag: u32 }

impl Foo {
    pub fn kind(&self) -> u32 {
        unimplemented!()
    }
}
impl Bar {
    pub fn name(&self) -> u32 {
        unimplemented!()
    }
}
"""

RESPONSE_TWO = (
    '<FN name="kind" block="impl Foo">self.tag</FN>\n'
    '<FN name="name" block="impl Bar">self.tag + 1</FN>'
)


def _two_blocks() -> list[ImplBlock]:
    a = FnRecord(file="lib.rs", fn_name="kind", container="impl Foo",
                 sig_text="fn kind (& self) -> u32", has_unimplemented_body=True)
    b = FnRecord(file="lib.rs", fn_name="name", container="impl Bar",
                 sig_text="fn name (& self) -> u32", has_unimplemented_body=True)
    return [ImplBlock(file="lib.rs", container="impl Foo",
                      members=(a,), stub_members=(a,)),
            ImplBlock(file="lib.rs", container="impl Bar",
                      members=(b,), stub_members=(b,))]


def test_file_first_fills_multi_block_file_in_one_call(monkeypatch, tmp_path):
    ws = _setup(monkeypatch, tmp_path)
    (ws / "src" / "lib.rs").write_text(IMPL_LIB_TWO)
    install_cluster_scan(monkeypatch, impl_blocks=lambda *a, **k: _two_blocks())
    # Fresh scan authority: after the file pass everything is filled, so the
    # per-block loop must SKIP both blocks (no extra LLM calls counted).
    install_cluster_scan(monkeypatch, stub_records=lambda *a, **k: [])
    _install_model(monkeypatch, [RESPONSE_TWO])
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    res = _run(ws)
    text = (ws / "src" / "lib.rs").read_text()
    assert res.fns_filled == 2 and res.impls_failed == 0
    assert res.attempts == 1                    # ONE file-level call
    assert "self.tag" in text and "unimplemented!()" not in text


def test_file_first_reverts_on_compile_failure_then_blocks_take_over(
        monkeypatch, tmp_path):
    ws = _setup(monkeypatch, tmp_path)
    (ws / "src" / "lib.rs").write_text(IMPL_LIB_TWO)
    install_cluster_scan(monkeypatch, impl_blocks=lambda *a, **k: _two_blocks())
    _install_model(monkeypatch, [RESPONSE_TWO])
    # Budget pinned to 1 so this test keeps exercising the FALLBACK contract
    # (file-first gives up → per-block takes over). The retry path itself is
    # pinned separately below.
    monkeypatch.setenv("PHASE_C_FILE_MAX_ATTEMPTS", "1")
    install_scripted_cargo(
        monkeypatch,
        check_diag=[(101, "error: x", [diag("src/lib.rs", code="E0308")]),
                    (0, "", [])])
    res = _run(ws)
    text = (ws / "src" / "lib.rs").read_text()
    # The routing widening lets a per-block turn splice a SIBLING
    # container's body too (not just its own target) when the model answers
    # both together, as RESPONSE_TWO always does here — Foo's turn now fills
    # BOTH `kind` and `name` in one call, so Bar's own turn finds `name`
    # already filled and completes via the freshness gate without a NEW
    # fill of its own. Content correctness (both fns filled, no stubs left)
    # is what matters — the block-attribution count is one accounting
    # consequence of the fix, not the thing being tested here.
    assert res.impls_filled == 1 and res.impls_failed == 0
    assert "self.tag" in text and "unimplemented!()" not in text


def test_file_first_peels_the_blamed_body_and_keeps_the_rest(
        monkeypatch, tmp_path):
    # The pathology: `src/error.rs` is split into groups of 16 and 13 fns and
    # no whole-group attempt compiled across five runs (1920662-40f711b2 ..
    # 2571732-149bc3cf, 2026-08-16/17); six file-first attempts in the last of
    # them spent an hour and landed nothing, on ordinary errors in a few of
    # sixteen bodies. Here `kind` is blamed and `name` is not, so `name` must
    # survive instead of being reverted with it.
    #
    # After peel, file-first used to FILE FILLED-return on attempt 1 of 3
    # (run 451330-7e8a7ca9, src/fmt.rs::debug), so the blamed name never
    # retried. The loop now continues scoped to the peeled names. The
    # scripted cargo is green from the post-peel check on, so the retry
    # lands `kind` too; a whole-file revert of attempt 2 would undo `name`.
    ws = _setup(monkeypatch, tmp_path)
    (ws / "src" / "lib.rs").write_text(IMPL_LIB_TWO)
    install_cluster_scan(monkeypatch, impl_blocks=lambda *a, **k: _two_blocks())
    install_cluster_scan(monkeypatch, stub_records=lambda *a, **k: [])
    prompts: list[str] = []

    def _capture(prompt, *a, **k):
        prompts.append(prompt)
        return RESPONSE_TWO

    monkeypatch.setattr(pipeline, "_ask_raw", _capture)
    install_scripted_cargo(
        monkeypatch,
        # red once (blaming `kind`), then green after the peel re-stubs it.
        check_diag=[(101, "error: x", [diag("src/lib.rs", code="E0308")]),
                    (0, "", [])],
        frontier={FnLocator("lib.rs", "kind"): {"<compile>"}})
    _run(ws)
    text = (ws / "src" / "lib.rs").read_text()
    assert "self.tag + 1" in text          # `name` kept across the retry
    assert "unimplemented!()" not in text  # `kind` retried and landed
    assert len(prompts) == 2, "peel must retry the blamed name file-first"
    # NEGATIVE CONTROL of the old FILE FILLED return: one call would leave
    # kind as a stub and never feed the diagnostic.
    assert "kind" in prompts[1]


def test_peel_retry_that_returns_empty_keeps_the_judged_remainder(
        monkeypatch, tmp_path):
    """The peel retry is taken only AFTER the gates judge the kept remainder,
    and an early return on the retry attempt loses only the names that
    attempt pursued.

    Attempt 1 lands `kind` and `name`; the compiler blames `kind`; the peel
    keeps `name`, which is judged and FILE FILLED before the retry begins.
    The retry reply carries no code. `kind` ends as a reverted try
    (`reverted_fns`), `name` as a judged landing — neither is a lost answer.
    Before the fix the retry `continue`d past the gates and the early return
    marked the WHOLE plan lost: `name`'s kept splice sat on the tree with no
    gate verdict and no FILE FILLED accounting (`fns_filled` 0), and its
    lost-answer row stripped the service of a body that had just landed."""
    ws = _setup(monkeypatch, tmp_path)
    (ws / "src" / "lib.rs").write_text(IMPL_LIB_TWO)
    install_cluster_scan(monkeypatch, impl_blocks=lambda *a, **k: _two_blocks())
    install_cluster_scan(monkeypatch, stub_records=lambda *a, **k: [])
    prompts: list[str] = []
    replies = [RESPONSE_TWO, "thinking, but no code today"]

    def _capture(prompt, *a, **k):
        prompts.append(prompt)
        return replies[min(len(prompts), len(replies)) - 1]

    monkeypatch.setattr(pipeline, "_ask_raw", _capture)
    install_scripted_cargo(
        monkeypatch,
        # red once (blaming `kind`), then green after the peel re-stubs it.
        check_diag=[(101, "error: x", [diag("src/lib.rs", code="E0308")]),
                    (0, "", [])],
        frontier={FnLocator("lib.rs", "kind"): {"<compile>"}})
    res = _run(ws)
    text = (ws / "src" / "lib.rs").read_text()
    assert "self.tag + 1" in text, "the kept body survives the failed retry"
    assert res.fns_filled >= 1, (
        "the kept remainder is judged and banked BEFORE the retry — an "
        "early-returning retry must not leave it unjudged")
    assert ("lib.rs", "kind") in res.reverted_fns
    assert res.answer_lost_fns == (), (
        "the retry pursued only the peeled names: `name` landed and `kind` "
        "has a reverted try — neither is a lost answer")
    assert len(prompts) == 3 and "kind" in prompts[1] and "kind" in prompts[2], (
        "an empty peel-retry is asked again while attempts remain, then "
        "gives up without FILE FILLED-returning the first empty")


def test_peel_retry_parse_broken_is_asked_again(monkeypatch, tmp_path):
    """A peel-retry that splices nothing is not FILE FILLED-done.

    Attempt 1 peels `kind` and keeps `name`. Attempt 2's body does not parse.
    Attempt 3 lands `kind`. Before this, spliced_now==0 on the retry returned
    and `kind` never saw the remaining attempt.
    """
    ws = _setup(monkeypatch, tmp_path)
    (ws / "src" / "lib.rs").write_text(IMPL_LIB_TWO)
    install_cluster_scan(monkeypatch, impl_blocks=lambda *a, **k: _two_blocks())
    install_cluster_scan(monkeypatch, stub_records=lambda *a, **k: [])
    from pipeline_core.application.reconstruct import splice as splice_mod
    _real_guard = splice_mod.parse_guarded_write

    def _guard(path, new_bytes, pre_bytes, validate_parse):
        if b"@@@" in new_bytes:
            return False
        return _real_guard(path, new_bytes, pre_bytes, validate_parse)

    monkeypatch.setattr(splice_mod, "parse_guarded_write", _guard)
    prompts: list[str] = []
    replies = [
        RESPONSE_TWO,
        '<FN name="kind" block="impl Foo">this is not rust @@@</FN>',
        '<FN name="kind" block="impl Foo">self.tag</FN>',
    ]

    def _capture(prompt, *a, **k):
        prompts.append(prompt)
        return replies[min(len(prompts) - 1, len(replies) - 1)]

    monkeypatch.setattr(pipeline, "_ask_raw", _capture)
    install_scripted_cargo(
        monkeypatch,
        check_diag=[(101, "error: x", [diag("src/lib.rs", code="E0308")]),
                    (0, "", [])],
        frontier={FnLocator("lib.rs", "kind"): {"<compile>"}})
    res = _run(ws)
    text = (ws / "src" / "lib.rs").read_text()
    assert "self.tag + 1" in text, "the kept remainder survives the broken retry"
    assert "unimplemented!()" not in text
    assert len(prompts) == 3, f"expected 3 file-first calls, got {len(prompts)}"
    assert "kind" in prompts[2]
    assert res.fns_filled >= 2


def test_file_first_without_the_peel_loses_the_good_body_too(
        monkeypatch, tmp_path):
    # Negative control. Same reply, same single blamed fn — but with the blame
    # naming EVERY landed body there is no proper subset to keep, which is the
    # pre-peel behaviour: the whole attempt reverts and `name` is lost with
    # `kind`. If this ever keeps `name`, the test above proves nothing.
    ws = _setup(monkeypatch, tmp_path)
    (ws / "src" / "lib.rs").write_text(IMPL_LIB_TWO)
    install_cluster_scan(monkeypatch, impl_blocks=lambda *a, **k: _two_blocks())
    _install_model(monkeypatch, [RESPONSE_TWO])
    monkeypatch.setenv("PHASE_C_FILE_MAX_ATTEMPTS", "1")
    install_scripted_cargo(
        monkeypatch,
        check_diag=[(101, "error: x", [diag("src/lib.rs", code="E0308")]),
                    (101, "error: x", [diag("src/lib.rs", code="E0308")])],
        frontier={FnLocator("lib.rs", "kind"): {"<compile>"},
                  FnLocator("lib.rs", "name"): {"<compile>"}})
    _run(ws)
    assert "self.tag + 1" not in (ws / "src" / "lib.rs").read_text()


def test_file_first_retries_with_the_compiler_errors_from_the_failed_attempt(
        monkeypatch, tmp_path):
    """A jointly-coupled file cannot be repaired one fn at a time, so when the
    whole-file attempt dies on a compile error the per-block fallback cannot
    recover it either — the retry has to happen HERE. It is only worth
    spending because it is feedback-carrying: attempt 2's prompt must render
    the diagnostics attempt 1 produced, otherwise it is a blind re-ask that
    reproduces the same mistake."""
    ws = _setup(monkeypatch, tmp_path)
    (ws / "src" / "lib.rs").write_text(IMPL_LIB_TWO)
    install_cluster_scan(monkeypatch, impl_blocks=lambda *a, **k: _two_blocks())
    install_cluster_scan(monkeypatch, stub_records=lambda *a, **k: [])
    sent = install_scripted_turns(monkeypatch, RESPONSE_TWO)
    monkeypatch.setenv("PHASE_C_FILE_MAX_ATTEMPTS", "3")
    install_scripted_cargo(
        monkeypatch,
        check_diag=[(101, "error[E0432]: unresolved import `crate::Nope`",
                     [diag("src/lib.rs", code="E0432")]),
                    (0, "", [])])
    res = _run(ws)
    text = (ws / "src" / "lib.rs").read_text()
    assert len(sent) == 2, "compile failure must be retried file-first"
    # The FEEDBACK is the retry turn, not a bare code substring: the opening
    # prompt carries a static RUST_GOTCHAS block that names E0432 among its
    # standing rules, so `"E0432" in prompt` says nothing about feedback.
    assert len(sent[0]) == 2 and "<COMPILE_ERRORS>" not in first_user(sent[0])
    retry = sent[1][-1]["content"]
    assert sent[1][-2]["content"] == RESPONSE_TWO, "the reply stays above the retry"
    assert retry.startswith("RESULT of your previous reply")
    assert "E0432" in retry.split("<COMPILE_ERRORS>", 1)[1]
    assert res.fns_filled == 2 and "unimplemented!()" not in text


def test_file_first_attempt_budget_is_bounded(monkeypatch, tmp_path):
    """The budget must terminate: a file that never compiles stops at the cap
    and hands over, rather than looping on the model until the sweep dies."""
    ws = _setup(monkeypatch, tmp_path)
    (ws / "src" / "lib.rs").write_text(IMPL_LIB_TWO)
    install_cluster_scan(monkeypatch, impl_blocks=lambda *a, **k: _two_blocks())
    calls: list[str] = []

    def _capture(prompt, *a, **k):
        calls.append(prompt)
        return RESPONSE_TWO

    monkeypatch.setattr(pipeline, "_ask_raw", _capture)
    monkeypatch.setenv("PHASE_C_FILE_MAX_ATTEMPTS", "2")
    install_scripted_cargo(
        monkeypatch, check_diag=(101, "error: nope", [diag("src/lib.rs")]))
    _run(ws)
    # 2 file-first attempts, then the per-block fallback — never an unbounded
    # loop. The exact fallback count is not the contract; the cap is.
    assert len(calls) >= 2
    assert (ws / "src" / "lib.rs").read_text().count("unimplemented!()") == 2


def test_file_first_skips_the_byte_identical_resample(monkeypatch, tmp_path):
    """An attempt whose rebuilt prompt equals the previous attempt's is a
    temperature resample, not a retry — no new information reaches the model.
    The route retries on `prior_error` and has no repeated-signature exit, so
    an identical revert reason looped it verbatim: `src/ensure.rs::render`
    sent the SAME prompt sha on attempts 2 and 3 (2026-08-19, 19:45:30 and
    19:53:41 UTC; attempt 1 differed), reverted all three times on the
    identical 11-test newly-red list, then fell back to per-block anyway.
    With the error identical every attempt, exactly ONE feedback-carrying
    prompt goes out (attempt 2); attempt 3 is skipped. Pre-fix this counted 2."""
    ws = _setup(monkeypatch, tmp_path)
    (ws / "src" / "lib.rs").write_text(IMPL_LIB_TWO)
    install_cluster_scan(monkeypatch, impl_blocks=lambda *a, **k: _two_blocks())
    install_cluster_scan(monkeypatch, stub_records=lambda *a, **k: [])
    sent = install_scripted_turns(monkeypatch, RESPONSE_TWO)
    monkeypatch.setenv("PHASE_C_FILE_MAX_ATTEMPTS", "3")
    install_scripted_cargo(
        monkeypatch,
        check_diag=(101, "error[E0432]: unresolved import `crate::Nope`",
                    [diag("src/lib.rs", code="E0432")]))
    _run(ws)
    file_calls = [t for t in sent
                  if "Write EVERY function listed below" in first_user(t)]
    with_feedback = [t for t in file_calls
                     if str(t[-1]["content"]).startswith("RESULT of your previous reply")]
    assert len(with_feedback) == 1, (
        "an unchanged error must buy ONE feedback-carrying retry, not a "
        f"resample per remaining attempt (got {len(with_feedback)})")


IMPL_LIB_FREE = """\
pub fn not(cond: bool) -> bool {
    unimplemented!()
}

pub struct Foo { tag: u32 }
pub struct Bar { tag: u32 }

impl Foo {
    pub fn kind(&self) -> u32 {
        unimplemented!()
    }
}
impl Bar {
    pub fn name(&self) -> u32 {
        unimplemented!()
    }
}
"""

# Order matters: the impl-addressed `not` body comes FIRST — without the
# impl-header guard it would win the free stub (name-only reroute) and the
# correct free body would then be refused as not-a-stub.
RESPONSE_FREE = (
    '<FN name="not" block="impl Baz">!self.tag</FN>\n'
    '<FN name="kind" block="impl Foo">self.tag</FN>\n'
    '<FN name="name" block="impl Bar">self.tag + 1</FN>\n'
    '<FN name="not" block="pub fn not(cond: bool) -> bool">!cond</FN>'
)


def test_file_first_routes_free_fn_but_never_an_impl_addressed_body(
        monkeypatch, tmp_path):
    """File-first must land a file's FREE fns atomically with its impls: a
    dropped body whose block attr echoes the free fn's signature is routed to
    the module-level stub; a dropped body addressed to an IMPL header (`impl
    Baz`) must NEVER land there — splicing `!self` into the self-less free
    `fn not(cond)` is the same-name corruption container scoping prevents."""
    ws = _setup(monkeypatch, tmp_path)
    (ws / "src" / "lib.rs").write_text(IMPL_LIB_FREE)
    install_cluster_scan(monkeypatch, impl_blocks=lambda *a, **k: _two_blocks())
    install_cluster_scan(monkeypatch, stub_records=lambda *a, **k: [
        {"file": "lib.rs", "fn_name": "not", "container": "",
         "sig_text": "pub fn not(cond: bool) -> bool",
         "has_unimplemented_body": True}])
    _install_model(monkeypatch, [RESPONSE_FREE])
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    _run(ws)
    text = (ws / "src" / "lib.rs").read_text()
    free_body = text.split("pub struct Foo", 1)[0]
    assert "!cond" in free_body           # the free-fn body landed in the free fn
    assert "!self.tag" not in text        # the impl-addressed body landed nowhere
    assert "self.tag\n" in text and "self.tag + 1" in text


IMPL_LIB_FREE_REFILL = """\
pub struct Foo { tag: u32 }

pub fn vtable(x: u32) -> u32 {
    x + 999
}

impl Foo {
    pub fn kind(&self) -> u32 {
        self.tag + 999
    }
}
"""


def _free_refill_blocks() -> list[ImplBlock]:
    """One impl block and one FREE-fn block, both all-filled — the shape
    `collect_impl_blocks` produces for a file whose free fns have bodies (it
    groups them under `container=""` and emits that group like any other)."""
    kind = FnRecord(file="lib.rs", fn_name="kind", container="impl Foo",
                    sig_text="pub fn kind (& self) -> u32",
                    body_text="self.tag + 999", has_unimplemented_body=False)
    vtable = FnRecord(file="lib.rs", fn_name="vtable", container="",
                      sig_text="pub fn vtable (x : u32) -> u32",
                      body_text="x + 999", has_unimplemented_body=False)
    return [ImplBlock(file="lib.rs", container="impl Foo",
                      members=(kind,), stub_members=()),
            ImplBlock(file="lib.rs", container="",
                      members=(vtable,), stub_members=())]


def test_file_first_plan_names_a_filled_free_fn_the_frontier_convicts(
        monkeypatch, tmp_path):
    """A filled free fn implicated by a failing test must reach a prompt.

    Until this pin it reached none. The per-impl route declines a block on
    `bool(block.container)` and its docstring says the per-fn route will take
    it; the per-fn route is handed the deferred STUBS only, and a filled fn is
    not a stub; the file-first plan was built from the impl route's own viable
    list. On the live anyhow tree that left all 13 free fns of `src/error.rs`
    — `object_downcast`, `vtable`, `context_chain_downcast`, … — unwritable,
    which is where the crate's remaining use-after-free lives. Every fill
    target named in every prompt over 2026-08-02/03 was an impl member.

    The renderer was always ready: `_plan_line` prints `free fns` for an empty
    header, and `route_fn_bodies` already routes a body whose block names no
    impl. The PLAN was the only thing withholding them."""
    from pipeline_core.domain.models import FnLocator
    ws = _setup(monkeypatch, tmp_path)
    (ws / "src" / "lib.rs").write_text(IMPL_LIB_FREE_REFILL)
    install_cluster_scan(monkeypatch,
                         impl_blocks=lambda *a, **k: _free_refill_blocks())
    install_cluster_scan(monkeypatch, stub_records=lambda *a, **k: [])
    prompts: list[str] = []

    def _capture(prompt, *a, **k):
        prompts.append(prompt)
        return ('<FN name="kind" block="impl Foo">self.tag</FN>\n'
                '<FN name="vtable" block="">x + 1</FN>')

    monkeypatch.setattr(pipeline, "_ask_raw", _capture)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    keys = {FnLocator("lib.rs", "kind"), FnLocator("lib.rs", "vtable")}
    pipeline._phase_c_fill_impl_blocks(
        project_dir=ws, target_src_dir=ws / "src", tests_dir_files=[],
        module_name="m", cluster_name="t", cluster=None,
        check_cmd="cargo check", cluster_env={}, fr_path=None,
        tdd_frontier_keys=keys,
        tdd_frontier_tests=FailureFrontier.of_direct(
            {k: {"test_downcast"} for k in keys}))
    assert prompts, "the file-first route must have been reached"
    plan = prompts[0].split('<FILE path=', 1)[0]
    assert "free fns" in plan, (
        f"the free fn is missing from the file-first plan:\n{plan}")
    assert "vtable" in plan, (
        f"the free fn is not named as a target:\n{plan}")


def test_file_first_lands_a_free_fn_answered_with_an_empty_block_attr(
        monkeypatch, tmp_path):
    """`block=""` is the only honest answer a free fn can give, and it used to
    cost the whole reply.

    The prompt's protocol is `<FN name="FUNCTION_NAME" block="IMPL_HEADER">`
    and a free fn has no impl header, so the model empties the attribute.
    `_read_quoted_attr` demanded a value of >=1 char, returned None, and left
    the scan sitting on `block=""` — so `>` was never found and
    `_find_fn_blocks` dropped the record. Every tag in the reply went the same
    way, and the caller logged `empty/no FN blocks (N FN tag(s) in reply)`.

    nightly/src/error.rs 2026-08-04T16:12:06Z was the first prompt this
    pipeline ever sent that asked for free fns — `object_ref`,
    `object_downcast`, `context_downcast`, `context_chain_downcast`, the four
    that carry the `test_downcast` heap-use-after-free. The model answered all
    four, correctly enough to name the freed `_error` field, and 4 of 4 were
    discarded on the attribute.

    The test above pins the PLAN and stops there; its own canned reply carries
    `block=""` and nothing ever checked what became of it. This pins the body
    onto disk."""
    from pipeline_core.domain.models import FnLocator
    ws = _setup(monkeypatch, tmp_path)
    (ws / "src" / "lib.rs").write_text(IMPL_LIB_FREE_REFILL)
    install_cluster_scan(monkeypatch,
                         impl_blocks=lambda *a, **k: _free_refill_blocks())
    install_cluster_scan(monkeypatch, stub_records=lambda *a, **k: [])
    _install_model(monkeypatch, [
        '<FN name="kind" block="impl Foo">self.tag + 1</FN>\n'
        '<FN name="vtable" block="">x + 1</FN>'])
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    keys = {FnLocator("lib.rs", "kind"), FnLocator("lib.rs", "vtable")}
    pipeline._phase_c_fill_impl_blocks(
        project_dir=ws, target_src_dir=ws / "src", tests_dir_files=[],
        module_name="m", cluster_name="t", cluster=None,
        check_cmd="cargo check", cluster_env={}, fr_path=None,
        tdd_frontier_keys=keys,
        tdd_frontier_tests=FailureFrontier.of_direct(
            {k: {"test_downcast"} for k in keys}))
    text = (ws / "src" / "lib.rs").read_text()
    free_body = text.split("impl Foo", 1)[0]
    assert "x + 1" in free_body, (
        "the free fn's body was answered with block=\"\" and never landed:\n"
        + free_body)
    assert "x + 999" not in text                # the stale body is gone


IMPL_LIB_A_TWO_REFILLS = """\
pub struct Foo { tag: u32 }
pub struct Bar { tag: u32 }

impl Foo {
    pub fn kind(&self) -> u32 {
        self.tag + 999
    }
}
impl Bar {
    pub fn name(&self) -> u32 {
        self.tag + 999
    }
}
"""

IMPL_LIB_B_REFILL_PLUS_FREE = """\
pub struct Baz { tag: u32 }

pub fn gamma(x: u32) -> u32 {
    x + 999
}

impl Baz {
    pub fn beta(&self) -> u32 {
        self.tag + 999
    }
}
"""


def _two_file_refill_blocks() -> list[ImplBlock]:
    kind = FnRecord(file="a.rs", fn_name="kind", container="impl Foo",
                    sig_text="pub fn kind (& self) -> u32",
                    body_text="self.tag + 999", has_unimplemented_body=False)
    name = FnRecord(file="a.rs", fn_name="name", container="impl Bar",
                    sig_text="pub fn name (& self) -> u32",
                    body_text="self.tag + 999", has_unimplemented_body=False)
    beta = FnRecord(file="b.rs", fn_name="beta", container="impl Baz",
                    sig_text="pub fn beta (& self) -> u32",
                    body_text="self.tag + 999", has_unimplemented_body=False)
    gamma = FnRecord(file="b.rs", fn_name="gamma", container="",
                     sig_text="pub fn gamma (x : u32) -> u32",
                     body_text="x + 999", has_unimplemented_body=False)
    return [ImplBlock(file="a.rs", container="impl Foo",
                      members=(kind,), stub_members=()),
            ImplBlock(file="a.rs", container="impl Bar",
                      members=(name,), stub_members=()),
            ImplBlock(file="b.rs", container="impl Baz",
                      members=(beta,), stub_members=()),
            ImplBlock(file="b.rs", container="",
                      members=(gamma,), stub_members=())]


def _two_file_setup(monkeypatch, root):
    ws = _setup(monkeypatch, root)
    (ws / "src" / "a.rs").write_text(IMPL_LIB_A_TWO_REFILLS)
    (ws / "src" / "b.rs").write_text(IMPL_LIB_B_REFILL_PLUS_FREE)
    install_cluster_scan(monkeypatch,
                         impl_blocks=lambda *a, **k: _two_file_refill_blocks())
    install_cluster_scan(monkeypatch, stub_records=lambda *a, **k: [])
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    return ws


def _run_two_file_refills(monkeypatch, ws, replies: list[str],
                          prompts: list[str]):
    from pipeline_core.domain.models import FnLocator

    def _ask(prompt, *a, **k):
        prompts.append(prompt)
        return replies[len(prompts) - 1]

    monkeypatch.setattr(pipeline, "_ask_raw", _ask)
    keys = {FnLocator("a.rs", "kind"), FnLocator("a.rs", "name"),
            FnLocator("b.rs", "beta"), FnLocator("b.rs", "gamma")}
    return pipeline._phase_c_fill_impl_blocks(
        project_dir=ws, target_src_dir=ws / "src", tests_dir_files=[],
        module_name="m", cluster_name="t", cluster=None,
        check_cmd="cargo check", cluster_env={}, fr_path=None,
        tdd_frontier_keys=keys,
        tdd_frontier_tests=FailureFrontier.of_direct(
            {k: {"test_x"} for k in keys}))


_A_FILE_REPLY = ('<FN name="kind" block="impl Foo">self.tag + 1</FN>\n'
                 '<FN name="name" block="impl Bar">self.tag + 2</FN>')
_BETA_BLOCK_REPLY = '<FN name="beta" block="impl Baz">self.tag + 7</FN>'


def test_a_file_that_produced_nothing_keeps_its_per_block_fallback(
        monkeypatch, tmp_path):
    """The same-call refill-only drop keys on PRODUCTION, not on the
    dispatch list. Run `347397-01e2463b` iter 1 (2026-08-31): the
    src/ensure.rs file-first call replied with 0 FN tags, logged `falling
    back to per-block fills`, and six records later the drop removed its two
    `__dispatch_ensure` impls anyway — the fallback its own log line had
    just promised — because the served set was the files DISPATCHED, not the
    files a call landed bodies in. Of that call's 14 dropped blocks, 12
    belonged to files that had produced (a correct drop) and 2 were this
    defect.

    Here a.rs produces (FILE FILLED, both refills land) and b.rs replies
    with no FN tags. a.rs's refill-only blocks are dropped — the
    same-iteration re-buy guard of run `463713-b7c666c8`, kept — while
    b.rs's impl block falls back to a real per-block call that lands, and
    b.rs's free fn, which only the file route can name, is reported LOST
    (`answer_lost_fns`) rather than graded as a try nobody made."""
    ws = _two_file_setup(monkeypatch, tmp_path)
    prompts: list[str] = []
    res = _run_two_file_refills(
        monkeypatch, ws,
        [_A_FILE_REPLY, "thinking, but no code today", _BETA_BLOCK_REPLY],
        prompts)
    a_text = (ws / "src" / "a.rs").read_text()
    b_text = (ws / "src" / "b.rs").read_text()
    assert "self.tag + 1" in a_text and "self.tag + 2" in a_text, (
        "a.rs must be designed by its file call")
    assert "self.tag + 7" in b_text, (
        "b.rs's impl block must reach the per-block fallback and land")
    assert "x + 999" in b_text, "the free fn had no fallback route"
    assert len(prompts) == 3, (
        "two file calls, then ONE per-block call — the produced file's "
        f"refill-only blocks stay dropped (got {len(prompts)} prompts)")
    assert res.answer_lost_fns == (("b.rs", "gamma"),), (
        "the free-fn refill's only planner returned nothing: lost, not "
        "attempted; beta was superseded by its real per-block attempt")

    # NEGATIVE CONTROL: the pre-fix served set — the dispatch list, which
    # here is both files. b.rs's impl block is dropped despite the empty
    # reply, the promised fallback never runs, and both of its units are
    # carried as lost.
    import pipeline_core.application.reconstruct.impl_fill as impl_fill_mod
    from pipeline_core.domain.refill_stamp import blocks_after_file_first
    real_drop = blocks_after_file_first
    monkeypatch.setattr(
        impl_fill_mod, "blocks_after_file_first",
        lambda viable, served: real_drop(viable, {"a.rs", "b.rs"}))
    ws2 = _two_file_setup(monkeypatch, tmp_path / "ctrl")
    c_prompts: list[str] = []
    c_res = _run_two_file_refills(
        monkeypatch, ws2,
        [_A_FILE_REPLY, "thinking, but no code today", _BETA_BLOCK_REPLY],
        c_prompts)
    c_b_text = (ws2 / "src" / "b.rs").read_text()
    assert "self.tag + 7" not in c_b_text and "self.tag + 999" in c_b_text, (
        "under the dispatch-keyed drop the fallback is suppressed")
    assert len(c_prompts) == 2, "no per-block call ever happens"
    assert c_res.answer_lost_fns == (("b.rs", "beta"), ("b.rs", "gamma"))


IMPL_LIB_FREE_REFILL_OVER_BOUND = """\
pub struct Foo { tag: u32 }
pub struct Bar { tag: u32 }

pub fn vtable(x: u32) -> u32 {
    x + 999
}

impl Foo {
    pub fn kind(&self) -> u32 {
        self.tag + 999
    }
}
impl Bar {
    pub fn name(&self) -> u32 {
        self.tag + 998
    }
}
"""


def _free_refill_blocks_over_bound() -> list[ImplBlock]:
    """Two filled impl blocks and one filled FREE-fn block — the same shape as
    `_free_refill_blocks`, sized so the file's total exceeds a small bound and
    the route has to PARTITION rather than send one prompt."""
    kind = FnRecord(file="lib.rs", fn_name="kind", container="impl Foo",
                    sig_text="pub fn kind (& self) -> u32",
                    body_text="self.tag + 999", has_unimplemented_body=False)
    name = FnRecord(file="lib.rs", fn_name="name", container="impl Bar",
                    sig_text="pub fn name (& self) -> u32",
                    body_text="self.tag + 998", has_unimplemented_body=False)
    vtable = FnRecord(file="lib.rs", fn_name="vtable", container="",
                      sig_text="pub fn vtable (x : u32) -> u32",
                      body_text="x + 999", has_unimplemented_body=False)
    return [ImplBlock(file="lib.rs", container="impl Foo",
                      members=(kind,), stub_members=()),
            ImplBlock(file="lib.rs", container="impl Bar",
                      members=(name,), stub_members=()),
            ImplBlock(file="lib.rs", container="",
                      members=(vtable,), stub_members=())]


def _over_bound_frontier():
    """`vtable` is dispatched but not CONVICTED — the shape a producer
    escalation creates. A free fn never appears in an assertion's panic
    location, so the only thing that ever puts one in the frontier is the
    escalation, and what it adds carries inferred attribution only."""
    from pipeline_core.domain.models import FnLocator
    keys = {FnLocator("lib.rs", "kind"), FnLocator("lib.rs", "name"),
            FnLocator("lib.rs", "vtable")}
    return keys, FailureFrontier.build(
        {k: {"test_downcast"} for k in keys},
        {FnLocator("lib.rs", "kind"): {"test_downcast"},
         FnLocator("lib.rs", "name"): {"test_downcast"}})


def _plans_of(prompts: list[str]) -> list[str]:
    return [p.split('<FILE path=', 1)[0] for p in prompts
            if "Functions to write (by impl block)" in p]


def test_file_first_over_bound_still_plans_the_block_no_route_covers(
        monkeypatch, tmp_path):
    """A file too big for one prompt must not strand its free-fn block.

    Placing free fns in the file-first PLAN is only half a route: when the file
    exceeds `PHASE_C_FILE_MAX_FNS` the plan is built per welded GROUP, and a
    group ran only at `len(g) >= 2` because a lone block "is the per-block
    route's unit". That is false of a free-fn or oversized block — the
    per-block loop iterates `viable_blocks`, which excludes exactly those two
    shapes — so the singleton was dropped, not delegated.

    A free-fn block is always a singleton, too: welding is keyed on CONVICTING
    tests and the escalation that puts a free fn in the frontier attributes it
    by inference, so its test set is empty and it can weld to nothing.

    Live anyhow/nightly 2026-08-04 12:12 UTC, the first sweep since 07-31 whose
    frontier named free fns at all: `src/error.rs: 35 fn(s) exceeds
    PHASE_C_FILE_MAX_FNS=20 — split into 1 test-welded group(s) of 20 fn(s)
    (+12 uncoupled block(s) → per-block loop)`, and the block holding
    `object_downcast`, `object_ref`, `context_downcast` and
    `context_chain_downcast` was among the 12. No prompt in that run named one
    of them."""
    ws = _setup(monkeypatch, tmp_path)
    (ws / "src" / "lib.rs").write_text(IMPL_LIB_FREE_REFILL_OVER_BOUND)
    install_cluster_scan(
        monkeypatch, impl_blocks=lambda *a, **k: _free_refill_blocks_over_bound())
    install_cluster_scan(monkeypatch, stub_records=lambda *a, **k: [])
    monkeypatch.setenv("PHASE_C_FILE_MAX_FNS", "2")     # 3 fns > bound
    prompts: list[str] = []

    def _capture(prompt, *a, **k):
        prompts.append(prompt)
        return '<FN name="kind" block="impl Foo">self.tag</FN>'

    monkeypatch.setattr(pipeline, "_ask_raw", _capture)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    keys, frontier = _over_bound_frontier()
    pipeline._phase_c_fill_impl_blocks(
        project_dir=ws, target_src_dir=ws / "src", tests_dir_files=[],
        module_name="m", cluster_name="t", cluster=None,
        check_cmd="cargo check", cluster_env={}, fr_path=None,
        tdd_frontier_keys=keys, tdd_frontier_tests=frontier)
    plans = _plans_of(prompts)
    assert any("free fns" in p and "vtable" in p for p in plans), (
        "the free-fn block was stranded by the over-bound partition:\n"
        + "\n---\n".join(plans))


def test_file_first_over_bound_leaves_a_covered_block_to_the_per_block_loop(
        monkeypatch, tmp_path):
    """…and the singleton rule still holds for every block that route DOES
    iterate. Widening it to all singletons would run each uncoupled impl block
    in both routes and double its LLM attempts, which is the cost the `len(g)
    >= 2` filter was installed to avoid. Only "no other route covers this" buys
    the exemption."""
    from pipeline_core.domain.models import FnLocator
    ws = _setup(monkeypatch, tmp_path)
    (ws / "src" / "lib.rs").write_text(IMPL_LIB_FREE_REFILL_OVER_BOUND)
    blocks = [b for b in _free_refill_blocks_over_bound() if b.container]
    install_cluster_scan(monkeypatch, impl_blocks=lambda *a, **k: blocks)
    install_cluster_scan(monkeypatch, stub_records=lambda *a, **k: [])
    monkeypatch.setenv("PHASE_C_FILE_MAX_FNS", "1")     # 2 fns > bound
    prompts: list[str] = []

    def _capture(prompt, *a, **k):
        prompts.append(prompt)
        return '<FN name="kind" block="impl Foo">self.tag</FN>'

    monkeypatch.setattr(pipeline, "_ask_raw", _capture)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    keys = {FnLocator("lib.rs", "kind"), FnLocator("lib.rs", "name")}
    pipeline._phase_c_fill_impl_blocks(
        project_dir=ws, target_src_dir=ws / "src", tests_dir_files=[],
        module_name="m", cluster_name="t", cluster=None,
        check_cmd="cargo check", cluster_env={}, fr_path=None,
        tdd_frontier_keys=keys,
        tdd_frontier_tests=FailureFrontier.of_direct(
            {FnLocator("lib.rs", "kind"): {"t1"},
             FnLocator("lib.rs", "name"): {"t2"}}))
    assert not _plans_of(prompts), (
        "two uncoupled impl blocks, each of which the per-block loop takes, "
        "must not get file-first groups of their own")


IMPL_LIB_TWO_MODS = """\
pub mod a {
    pub fn helper() -> u32 {
        unimplemented!()
    }
}
pub mod b {
    pub fn helper() -> u32 {
        unimplemented!()
    }
}

pub struct Foo { tag: u32 }
pub struct Bar { tag: u32 }

impl Foo {
    pub fn kind(&self) -> u32 {
        unimplemented!()
    }
}
impl Bar {
    pub fn name(&self) -> u32 {
        unimplemented!()
    }
}
"""

RESPONSE_TWO_MODS = (
    '<FN name="kind" block="impl Foo">self.tag</FN>\n'
    '<FN name="name" block="impl Bar">self.tag + 1</FN>\n'
    '<FN name="helper" block="mod b">42</FN>'
)


def _two_mod_stub_records(*a, **k):
    return [
        {"file": "lib.rs", "fn_name": "helper", "container": "mod a",
         "sig_text": "pub fn helper() -> u32",
         "has_unimplemented_body": True},
        {"file": "lib.rs", "fn_name": "helper", "container": "mod b",
         "sig_text": "pub fn helper() -> u32",
         "has_unimplemented_body": True},
    ]


def test_free_fn_reroute_same_name_in_two_mods_binds_by_container(
        monkeypatch, tmp_path):
    """Two same-named FREE fns stubbed in different mods of one file must not
    collapse to one record (last-wins routed a body into the wrong mod): the
    body's own block attr picks its mod, the other stub stays untouched."""
    ws = _setup(monkeypatch, tmp_path)
    (ws / "src" / "lib.rs").write_text(IMPL_LIB_TWO_MODS)
    install_cluster_scan(monkeypatch, impl_blocks=lambda *a, **k: _two_blocks())
    install_cluster_scan(monkeypatch, stub_records=_two_mod_stub_records)
    _install_model(monkeypatch, [RESPONSE_TWO_MODS])
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    _run(ws)
    text = (ws / "src" / "lib.rs").read_text()
    mod_a = text.split("pub mod b", 1)[0]
    mod_b = text.split("pub mod b", 1)[1].split("pub struct Foo", 1)[0]
    assert "unimplemented!()" in mod_a    # mod a's stub untouched
    assert "42" in mod_b                  # the body landed in mod b only


def test_free_fn_reroute_ambiguous_two_mods_refuses(
        monkeypatch, tmp_path):
    """A dropped body whose block attr names NO mod cannot pick between two
    same-named mod stubs — the reroute must refuse, never guess."""
    ws = _setup(monkeypatch, tmp_path)
    (ws / "src" / "lib.rs").write_text(IMPL_LIB_TWO_MODS)
    install_cluster_scan(monkeypatch, impl_blocks=lambda *a, **k: _two_blocks())
    install_cluster_scan(monkeypatch, stub_records=_two_mod_stub_records)
    _install_model(monkeypatch, [
        '<FN name="kind" block="impl Foo">self.tag</FN>\n'
        '<FN name="name" block="impl Bar">self.tag + 1</FN>\n'
        '<FN name="helper" block="pub fn helper() -> u32">42</FN>'])
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    _run(ws)
    text = (ws / "src" / "lib.rs").read_text()
    assert text.count("unimplemented!()") == 2   # both mod stubs untouched


def test_file_first_skipped_for_single_block(monkeypatch, tmp_path):
    ws = _setup(monkeypatch, tmp_path)      # default single-block fixture
    _install_model(monkeypatch, [RESPONSE_BOTH])
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    res = _run(ws)
    assert res.impls_filled == 1          # per-block path, as before


def test_file_first_work_bound_counts_refills_and_says_so_when_it_skips(
        monkeypatch, tmp_path, capsys):
    # Live regression (run 1154362): the bound started counting re-fills but
    # kept its stubs-era value, so a file needing 8 stubs + 6 rewrites dropped
    # out of the joint-design route — and the old `return` was silent, which
    # in the log is indistinguishable from a file with no work at all.
    ws = _setup(monkeypatch, tmp_path)
    (ws / "src" / "lib.rs").write_text(IMPL_LIB_TWO)
    install_cluster_scan(monkeypatch, impl_blocks=lambda *a, **k: _two_blocks())
    install_cluster_scan(monkeypatch, stub_records=lambda *a, **k: [])
    _install_model(monkeypatch, [RESPONSE_TWO])
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    monkeypatch.setenv("PHASE_C_FILE_MAX_FNS", "1")     # 2 fns > bound
    _run(ws)
    out = capsys.readouterr().out
    assert "skipped — 2 fn(s)" in out
    assert "PHASE_C_FILE_MAX_FNS=1" in out


IMPL_LIB_TWO_WRONG = """\
pub struct Foo { tag: u32 }
pub struct Bar { tag: u32 }

impl Foo {
    pub fn kind(&self) -> u32 {
        self.tag + 999
    }
}
impl Bar {
    pub fn name(&self) -> u32 {
        self.tag + 777
    }
}
"""


def _two_refill_blocks() -> list[ImplBlock]:
    # Two blocks, ZERO stubs — both bodies compile but are behaviorally wrong.
    a = FnRecord(file="lib.rs", fn_name="kind", container="impl Foo",
                 sig_text="fn kind (& self) -> u32", body_text="self.tag + 999",
                 has_unimplemented_body=False)
    b = FnRecord(file="lib.rs", fn_name="name", container="impl Bar",
                 sig_text="fn name (& self) -> u32", body_text="self.tag + 777",
                 has_unimplemented_body=False)
    return [ImplBlock(file="lib.rs", container="impl Foo",
                      members=(a,), stub_members=()),
            ImplBlock(file="lib.rs", container="impl Bar",
                      members=(b,), stub_members=())]


def test_file_first_u2_gate_runs_the_refill_members_tests(monkeypatch, tmp_path,
                                                          capsys):
    # Live regression (run 1170946): the file route built its U2 test set from
    # `stub_members` alone. A behavioral re-fill has no stub left, so an
    # all-refill file produced an EMPTY test set — and an empty set is an
    # unconditional ACCEPT in `u2_test_gate`. The route then reported
    # "FILE FILLED" for bodies whose failing tests were never run (34 fns across
    # 5 files in one pass, with the cluster pass-count flat at 14).
    from pipeline_core.domain.models import FnLocator
    ws = _setup(monkeypatch, tmp_path)
    (ws / "src" / "lib.rs").write_text(IMPL_LIB_TWO_WRONG)
    install_cluster_scan(monkeypatch,
                         impl_blocks=lambda *a, **k: _two_refill_blocks())
    install_cluster_scan(monkeypatch, stub_records=lambda *a, **k: [])
    _install_model(monkeypatch, [RESPONSE_TWO])
    # Compiles clean; the scoped test run still reports the re-fill's own test
    # failing — so the replacement bodies are no better than what they replaced.
    install_scripted_cargo(
        monkeypatch,
        check_diag=(0, "", []),
        run=(101, "test kind_is_wrong ... FAILED\n"
                  "\nfailures:\n\n"
                  "---- kind_is_wrong stdout ----\n"
                  "thread 'kind_is_wrong' panicked at tests/t.rs:3:5:\n"
                  "assertion `left == right` failed\n"
                  "\nfailures:\n    kind_is_wrong\n"
                  "\ntest result: FAILED. 0 passed; 1 failed; 0 ignored"))
    res = pipeline._phase_c_fill_impl_blocks(
        project_dir=ws, target_src_dir=ws / "src", tests_dir_files=[],
        module_name="m", cluster_name="t", cluster=None, check_cmd="cargo check",
        cluster_env={}, fr_path=None, tdd_test_cmd="cargo test",
        tdd_frontier_keys={FnLocator("lib.rs", "kind"),
                           FnLocator("lib.rs", "name")},
        tdd_frontier_tests=FailureFrontier.of_direct({FnLocator("lib.rs", "kind"): {"kind_is_wrong"},
                            FnLocator("lib.rs", "name"): {"name_is_wrong"}}))
    out = capsys.readouterr().out
    assert "file-first fill (2 block(s), 0 stub(s), 2 re-fill(s)" in out
    # The defect boundary this test was written for is unchanged: the U2 gate
    # RUNS the refill members' tests — the keep below is the gate's REJECT
    # being downgraded on evidence, not the old empty-test-set unconditional
    # accept (an unconditional accept prints no non-regression guard).
    assert "re-fill KEPT on non-regression" in out
    # What changed on top of it: a REJECT whose every self-failure was
    # already failing at baseline is no longer chargeable — the old bodies
    # fail the same test, so reverting to them re-purchases nothing (the
    # live instance: `src/error.rs[group 1/3]`'s 20 re-fills reverted three
    # times on this exact reason, 72 calls for 137 net bytes). The group is
    # KEPT and judged at the next sweep in the joint tree; a body that costs
    # tests there is clawed back by the batch ratchet.
    assert ("[phase-c/file] lib.rs: reverted "
            "(U2 test failure (no strict improvement))") not in out
    del res


def test_file_first_default_bound_admits_a_stub_plus_refill_workload(
        monkeypatch, tmp_path):
    # The default must still admit the shape that motivated counting refills:
    # a file with stubs AND rewrites. Pinning the default keeps a future
    # tightening from silently disabling the route again.
    ws = _setup(monkeypatch, tmp_path)
    (ws / "src" / "lib.rs").write_text(IMPL_LIB_TWO)
    install_cluster_scan(monkeypatch, impl_blocks=lambda *a, **k: _two_blocks())
    install_cluster_scan(monkeypatch, stub_records=lambda *a, **k: [])
    _install_model(monkeypatch, [RESPONSE_TWO])
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    monkeypatch.delenv("PHASE_C_FILE_MAX_FNS", raising=False)
    monkeypatch.delenv("PHASE_C_FILE_MAX_STUBS", raising=False)
    res = _run(ws)
    assert res.attempts == 1 and res.fns_filled == 2    # route ran


# --- exhaustion vs. layout-contradicted guidance -----------------------------
# The retro store is keyed (file, fn, container) with NO cluster, so one
# guidance string serves layouts that differ. A block convicted while its own
# guidance named a field the current layout lacks was never fairly attempted.

def _write_retro(ws: Path, guidance: str, *, cluster: str = "t",
                 blocked_on: list[str] | None = None) -> None:
    d = ws / ".phase_c_logs"
    d.mkdir(parents=True, exist_ok=True)
    # Stamped as if earned here and now, on compile errors (no convicting
    # tests): an unstamped verdict is honoured nowhere (it names neither a
    # context nor its evidence), which would mask what these pin.
    # Default `blocked_on` is a named stub so these pin layout / cluster /
    # fingerprint, not the ninth screen_guidance rule (empty blocked_on
    # retires every stored lesson). Pass blocked_on=[] to pin that rule.
    rec = stamp_conviction(
        {"category": "API_HALLUCINATION", "guidance": guidance,
         "fully_exhausted": True, "best_error_count": 3,
         "blocked_on": list(blocked_on if blocked_on is not None
                            else ["other_stub"])},
        RetroProvenance.current(
            cluster, (ws / "src" / "lib.rs").read_text(encoding="utf-8")),
        ConvictionEvidence(frozenset()))
    (d / "retrospectives.json").write_text(
        json.dumps({"lib.rs::*impl*::Foo": rec}), encoding="utf-8")


def test_impl_exhausted_block_retried_when_guidance_contradicts_layout(
        monkeypatch, tmp_path):
    ws = _setup(monkeypatch, tmp_path)
    _write_retro(ws, "Read the payload from the `_object` field of Foo, "
                     "never `tag`.")
    _install_model(monkeypatch, [RESPONSE_BOTH])
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    res = _run(ws)
    assert res.impls_filled == 1 and res.fns_filled == 2
    assert "unimplemented!()" not in (ws / "src" / "lib.rs").read_text()


def test_impl_exhausted_block_still_skipped_when_guidance_matches_layout(
        monkeypatch, tmp_path):
    ws = _setup(monkeypatch, tmp_path)
    _write_retro(ws, "Read the payload from the `tag` field of Foo.")
    _install_model(monkeypatch, [RESPONSE_BOTH])
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    res = _run(ws)
    assert res.impls_filled == 0 and res.fns_filled == 0
    assert "unimplemented!()" in (ws / "src" / "lib.rs").read_text()


def test_impl_exhausted_block_retried_when_blocked_on_is_empty(
        monkeypatch, tmp_path):
    # Layout-consistent guidance with empty blocked_on is the ninth rule's
    # constituency: honouring exhaustion would skip the block, so the
    # screen would never see the record. Run 9933-efe68f37 left msg and
    # context in that shape on the per-fn keys; the impl route is the
    # same skip predicate.
    ws = _setup(monkeypatch, tmp_path)
    _write_retro(ws, "Read the payload from the `tag` field of Foo.",
                 blocked_on=[])
    _install_model(monkeypatch, [RESPONSE_BOTH])
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    res = _run(ws)
    assert res.impls_filled == 1 and res.fns_filled == 2
    assert "unimplemented!()" not in (ws / "src" / "lib.rs").read_text()


def test_impl_exhausted_block_retried_when_verdict_earned_in_another_cluster(
        monkeypatch, tmp_path):
    # Layout-consistent guidance, so the sieve keeps it — but the verdict was
    # earned on the other cluster, whose private layout this file does not
    # share. A conviction is evidence only for the context that produced it.
    ws = _setup(monkeypatch, tmp_path)
    _write_retro(ws, "Read the payload from the `tag` field of Foo.",
                 cluster="other")
    _install_model(monkeypatch, [RESPONSE_BOTH])
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    res = _run(ws)
    assert res.impls_filled == 1 and res.fns_filled == 2


def test_impl_exhausted_block_retried_when_layout_moved_under_the_verdict(
        monkeypatch, tmp_path):
    # Same cluster, but the private layout was re-authored since the verdict:
    # the attempts it counted were compiled against a different struct.
    ws = _setup(monkeypatch, tmp_path)
    _write_retro(ws, "Read the payload from the `tag` field of Foo.")
    store = ws / ".phase_c_logs" / "retrospectives.json"
    recs = json.loads(store.read_text(encoding="utf-8"))
    recs["lib.rs::*impl*::Foo"]["layout_fp"] = "deadbeef"
    store.write_text(json.dumps(recs), encoding="utf-8")
    _install_model(monkeypatch, [RESPONSE_BOTH])
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    res = _run(ws)
    assert res.impls_filled == 1 and res.fns_filled == 2


def test_impl_poisoned_guidance_is_withheld_from_the_prompt(
        monkeypatch, tmp_path):
    # Retrying is not enough — the same lie must not be re-injected.
    ws = _setup(monkeypatch, tmp_path)
    _write_retro(ws, "Read the payload from the `_object` field of Foo.")
    seen: list[str] = []

    def _capture(*a, **k):
        seen.append(a[0] if a else k.get("prompt", ""))
        return RESPONSE_BOTH
    monkeypatch.setattr(pipeline, "_ask_raw", _capture)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    _run(ws)
    assert seen and not any("_object" in p for p in seen)


SIBLING_LIB = """pub struct Foo { tag: u32 }
pub struct Bar { tag: u32 }

impl Bar {
    pub fn kind(&self) -> u32 {
        self.tag.wrapping_add(1)
    }
}

impl Foo {
    pub fn kind(&self) -> u32 {
        unimplemented!()
    }
    pub fn name(&self) -> u32 {
        unimplemented!()
    }
}
"""

RESPONSE_LIKE_SIBLING = (
    '<FN name="kind" block="impl Foo">self.tag.wrapping_add(1)</FN>\n'
    '<FN name="name" block="impl Foo">self.tag + 1</FN>'
)

_REJECTED_RENDERED = (
    "error[E0599]: no method named `wrapping_add` found for struct `Foo`\n"
    "  --> src/lib.rs:12:18\n"
    "   |\n"
    "12 |         self.tag.wrapping_add(1)\n"
    "   |                  ^^^^^^^^^^^^ method not found\n")


def test_impl_retry_flips_a_sibling_that_carries_the_rejected_expression(
        monkeypatch, tmp_path):
    """Attempt 2 must not be told to "follow the same structural pattern" as a
    sibling whose body carries the expression attempt 1 was rejected on. On
    run `902902-daee577d` (2026-08-20, prompt `11899c17478d`) LAST_ERROR's
    E0277 quoted `Some(&self.error)` and the SIBLING_REFERENCE endorsed a
    sibling containing it; the model wrote it again. Negative control: attempt
    1 has no previous error, so its sibling section carries the endorsement."""
    from pipeline_core.domain.models import Diagnostic

    ws = _setup(monkeypatch, tmp_path)
    (ws / "src" / "lib.rs").write_text(SIBLING_LIB)
    sent = install_scripted_turns(monkeypatch, RESPONSE_LIKE_SIBLING)
    install_scripted_cargo(
        monkeypatch,
        check_diag=(101, _REJECTED_RENDERED,
                    [Diagnostic(code="E0599", file="src/lib.rs", line=12,
                                message="no method named `wrapping_add`",
                                rendered=_REJECTED_RENDERED)]))
    _run(ws)
    impl_calls = [t for t in sent if "TARGET_IMPL_BLOCK" in first_user(t)]
    assert len(impl_calls) >= 2, "the fix loop must have retried"
    first = first_user(impl_calls[0])
    retry = str(impl_calls[1][-1]["content"])
    assert "<SIBLING_REFERENCE>" in first, \
        "Bar::kind is a same-named filled sibling and must be shown"
    assert "follow the same structural pattern" in first
    assert "CAUTION" not in first
    # The retry turn names the contradiction with the section above it.
    assert "CAUTION" in retry and "`self.tag.wrapping_add(1)`" in retry
    assert "SIBLING_REFERENCE above" in retry


# --- The producer-bypass closure, route → prompt ------------------------------

BYPASS_LIB = """\
pub struct W { x: u32 }
pub struct Vt { f: fn(u32) -> u32 }

pub fn drop_w(v: u32) -> u32 { v }

impl W {
    pub fn maker(x: u32) -> W {
        let t = Vt { f: drop_w::<W> };
        let _ = t;
        W { x }
    }
    pub fn caller(x: u32) -> W {
        W { x }
    }
    pub fn other(&self) -> u32 {
        unimplemented!()
    }
}
impl Default for W {
    fn default() -> W {
        unimplemented!()
    }
}
impl Clone for W {
    fn clone(&self) -> W {
        W { x: self.x }
    }
}
pub trait Tagged {
    fn wrap(self) -> W;
}
impl<Z> Tagged for Z {
    fn wrap(self) -> W {
        W { x: 0 }
    }
}
"""

E0425_NOTE = ("[E0425] I tried building the value with a manually referenced "
              "table because the value isn't in scope, use the plain "
              "construction helper that omits the table instead.")


def _bypass_blocks() -> list[ImplBlock]:
    mk = FnRecord(file="lib.rs", fn_name="maker", container="impl W",
                  sig_text="fn maker (x : u32) -> W")
    ca = FnRecord(file="lib.rs", fn_name="caller", container="impl W",
                  sig_text="fn caller (x : u32) -> W")
    ot = FnRecord(file="lib.rs", fn_name="other", container="impl W",
                  sig_text="fn other (& self) -> u32",
                  has_unimplemented_body=True)
    de = FnRecord(file="lib.rs", fn_name="default",
                  container="impl Default for W",
                  sig_text="fn default () -> W", has_unimplemented_body=True)
    cl = FnRecord(file="lib.rs", fn_name="clone", container="impl Clone for W",
                  sig_text="fn clone (& self) -> W")
    return [
        ImplBlock(file="lib.rs", container="impl W",
                  members=(mk, ca, ot), stub_members=(ot,)),
        ImplBlock(file="lib.rs", container="impl Default for W",
                  members=(de,), stub_members=(de,)),
        ImplBlock(file="lib.rs", container="impl Clone for W",
                  members=(cl,), stub_members=()),
    ]


def _bypass_setup(monkeypatch, tmp_path: Path) -> tuple[Path, list[str]]:
    from pipeline_core.domain.models import FieldRecord, TypeRecord
    ws = _setup(monkeypatch, tmp_path)
    (ws / "src" / "lib.rs").write_text(BYPASS_LIB)
    install_cluster_scan(monkeypatch, impl_blocks=lambda *a, **k: _bypass_blocks())
    install_cluster_scan(monkeypatch, stub_records=lambda *a, **k: [])
    install_fake_extractor(
        monkeypatch, fns=lambda sd: pipeline._STRUCTURE.enumerate_fns(sd),
        types=[TypeRecord(file="lib.rs", kind="struct", name="Vt", fields=(
                   FieldRecord(name="f", ty="fn(u32) -> u32"),)),
               TypeRecord(file="lib.rs", kind="struct", name="W", fields=())],
        dispatch_families=[])
    (ws / "fr.md").write_text(
        "# FR\n\nspec.\n\n## Model Notes — Confusions to Avoid\n\n"
        f"- {E0425_NOTE}\n")
    prompts: list[str] = []

    def _ask(prompt, *a, **k):
        prompts.append(prompt)
        return ('<FN name="other" block="impl W">self.x</FN>\n'
                '<FN name="default" block="impl Default for W">W { x: 0 }</FN>')

    monkeypatch.setattr(pipeline, "_ask_raw", _ask)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    return ws, prompts


def test_file_first_prompt_lists_the_bypass_callers_and_drops_the_lesson(
        monkeypatch, tmp_path):
    """199.21's stated test, at the route seam: with the typed producer
    (`maker` builds a `Vt` table instantiated at `W`) in the MEASURED
    escalated set, the file-first prompt lists every fn that constructs `W`
    without invoking `maker` — `caller` inside a dispatched block, `clone`
    inside a block this dispatch does not even carry (`_extra_plans`), `wrap`
    inside a generic impl. And the same prompt's `<MODEL_NOTES>` carries no
    `[E0xxx]` lesson: the route passes the grade, so the channel rule fires
    where the measured instruction is (the contradiction run `40938-f4e8e94b`
    carried at line 1213).

    The INSTRUCTION splits on the erased inner type, through the REAL
    extractor's `impl_self_generic`: `wrap` (`impl<Z> Tagged for Z` — generic
    self) gets the call form naming `maker`; `caller` and `clone` (concrete
    self `W`) get the table form, with the producer's name NOWHERE in their
    lines. The pre-fix control is recorded live: the call form rendered for
    the concrete-self `Error::context` is what run `57025-6ba652f2` complied
    with over the compiler through three identical `E0277`s."""
    ws, prompts = _bypass_setup(monkeypatch, tmp_path)
    res = pipeline._phase_c_fill_impl_blocks(
        project_dir=ws, target_src_dir=ws / "src", tests_dir_files=[],
        module_name="m", cluster_name="t", cluster=None,
        check_cmd="cargo check", cluster_env={}, fr_path=ws / "fr.md",
        tdd_frontier_keys=None,
        escalated_measured=frozenset({FnLocator("lib.rs", "maker")}),
        escalated_keys=frozenset({FnLocator("lib.rs", "maker")}))
    assert res.fns_filled >= 2
    file_prompt = prompts[0]
    line = next(ln for ln in file_prompt.splitlines()
                if ln.startswith("  in `impl W`") and "caller" in ln)
    assert "BUILD THE PER-TYPE TABLE" in line
    assert "maker" not in line and "REWRITE TO INVOKE" not in line
    assert "CHAIN-walking" in line and "different impl" in line
    extra = next(ln for ln in file_prompt.splitlines()
                 if ln.startswith("  in `impl Clone for W`"))
    assert "clone" in extra and "BUILD THE PER-TYPE TABLE" in extra
    assert "maker" not in extra
    assert "CHAIN-walking" in extra
    generic = next(ln for ln in file_prompt.splitlines()
                   if ln.startswith("  in `impl Tagged for Z`"))
    assert "REWRITE TO INVOKE THE PRODUCER" in generic
    assert "rewrite it to call `maker`" in generic and "wrap" in generic
    assert "[E0425]" not in file_prompt


def test_file_first_prompt_without_the_measured_set_offers_no_bypass(
        monkeypatch, tmp_path):
    """NEGATIVE CONTROL, pre-fix shape: no escalated measurement → no bypass
    line anywhere, and the `[E0425]` lesson still reaches the prompt — the
    exact state the FILE FILLED 20 reply complied with."""
    ws, prompts = _bypass_setup(monkeypatch, tmp_path)
    pipeline._phase_c_fill_impl_blocks(
        project_dir=ws, target_src_dir=ws / "src", tests_dir_files=[],
        module_name="m", cluster_name="t", cluster=None,
        check_cmd="cargo check", cluster_env={}, fr_path=ws / "fr.md",
        tdd_frontier_keys=None)
    file_prompt = prompts[0]
    assert "REWRITE TO INVOKE THE PRODUCER" not in file_prompt
    assert "BUILD THE PER-TYPE TABLE" not in file_prompt
    # the undispatched block gets no plan entry without the measured set
    plan = file_prompt.split("Functions to write", 1)[1].split('<FILE path=', 1)[0]
    assert "impl Clone for W" not in plan
    assert "[E0425]" in file_prompt


# --- one authorization set: what the prompt calls writable, the splice lets overwrite ---

INSTALLABLE_LIB = """\
pub struct W { x: u32 }
pub struct Vt { f: fn(u32) -> u32 }

pub fn drop_w(v: u32) -> u32 { v }

pub fn helper(v: u32) -> u64 { v as u64 }

impl W {
    pub fn maker(x: u32) -> W {
        let t = Vt { f: drop_w };
        let _ = t;
        W { x }
    }
    pub fn other(&self) -> u32 {
        unimplemented!()
    }
}
impl Default for W {
    fn default() -> W {
        unimplemented!()
    }
}
"""


def _installable_blocks() -> list[ImplBlock]:
    """Two blocks, so the default route ladder opens the FILE-first route
    (a single-block file goes to the per-impl route)."""
    mk = FnRecord(file="lib.rs", fn_name="maker", container="impl W",
                  sig_text="fn maker (x : u32) -> W")
    ot = FnRecord(file="lib.rs", fn_name="other", container="impl W",
                  sig_text="fn other (& self) -> u32",
                  has_unimplemented_body=True)
    de = FnRecord(file="lib.rs", fn_name="default",
                  container="impl Default for W",
                  sig_text="fn default () -> W", has_unimplemented_body=True)
    return [ImplBlock(file="lib.rs", container="impl W",
                      members=(mk, ot), stub_members=(ot,), refill_members=(mk,)),
            ImplBlock(file="lib.rs", container="impl Default for W",
                      members=(de,), stub_members=(de,))]


def _installable_setup(monkeypatch, tmp_path: Path, reply: str) -> tuple[Path, list[str]]:
    from pipeline_core.domain.models import FieldRecord, TypeRecord
    ws = _setup(monkeypatch, tmp_path)
    (ws / "src" / "lib.rs").write_text(INSTALLABLE_LIB)
    install_cluster_scan(monkeypatch, impl_blocks=lambda *a, **k: _installable_blocks())
    install_cluster_scan(monkeypatch, stub_records=lambda *a, **k: [])
    install_fake_extractor(
        monkeypatch, fns=lambda sd: pipeline._STRUCTURE.enumerate_fns(sd),
        types=[TypeRecord(file="lib.rs", kind="struct", name="Vt", fields=(
                   FieldRecord(name="f", ty="fn(u32) -> u32"),)),
               TypeRecord(file="lib.rs", kind="struct", name="W", fields=())],
        dispatch_families=[])
    prompts: list[str] = []

    def _ask(prompt, *a, **k):
        prompts.append(prompt)
        return reply

    monkeypatch.setattr(pipeline, "_ask_raw", _ask)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    return ws, prompts


_JOINT_REPLY = (
    '<FN name="maker" block="impl W">let t = Vt { f: drop_w }; let _ = t; W { x: x + 1 }</FN>\n'
    '<FN name="drop_w">v + 7</FN>\n'
    '<FN name="helper">v as u64 + 1</FN>\n'
    '<FN name="other" block="impl W">self.x</FN>\n'
    '<FN name="default" block="impl Default for W">W { x: 0 }</FN>')


def _run_escalated(ws: Path, *, measured) -> pipeline.ClusterFillResult:
    return pipeline._phase_c_fill_impl_blocks(  # type: ignore[no-any-return]
        project_dir=ws, target_src_dir=ws / "src", tests_dir_files=[],
        module_name="m", cluster_name="t", cluster=None,
        check_cmd="cargo check", cluster_env={}, fr_path=None,
        tdd_frontier_keys=None,
        escalated_measured=measured, escalated_keys=measured)


def test_file_first_splices_the_installable_the_prompt_offered_beside_its_producer(
        monkeypatch, tmp_path):
    """Run `3014175-d98ed09e` (2026-09-08, iter 2, `src/error.rs`): the
    prompt said three installables were "writable in this prompt … rewrite
    it together with the producer", the reply opened with all three, and
    every one was refused `target_not_stub [overwrite not authorized]` —
    the splice read `_refill_names` and the plan's `installable` tuple
    was never consulted. The plan's sentence: a file-first reply carrying
    a producer and its installables with changed bodies splices all of
    them. The scope control in the same reply: a filled free fn the prompt
    did NOT offer (`helper`, whose signature matches no table field) is
    still refused."""
    ws, prompts = _installable_setup(monkeypatch, tmp_path, _JOINT_REPLY)
    _run_escalated(ws, measured=frozenset({FnLocator("lib.rs", "maker")}))
    assert "INSTALLABLE" in prompts[0] and "`drop_w`" in prompts[0]
    text = (ws / "src" / "lib.rs").read_text()
    assert "W { x: x + 1 }" in text            # the producer (a refill target)
    assert "v + 7" in text                    # its installable, offered → authorized
    assert "self.x" in text                   # the stub
    assert "v as u64 + 1" not in text         # not offered → not authorized


def test_without_the_offer_the_same_installable_body_is_refused(monkeypatch, tmp_path):
    """Negative control, the pre-fix shape from the splice's side: no
    measured escalation, so nothing is offered as installable, and the
    same `drop_w` body is refused `target_not_stub` while the producer
    lands alone — halves of two answers, which is what the one set exists
    to make impossible."""
    ws, prompts = _installable_setup(monkeypatch, tmp_path, _JOINT_REPLY)
    _run_escalated(ws, measured=frozenset())
    assert "INSTALLABLE" not in prompts[0]
    text = (ws / "src" / "lib.rs").read_text()
    assert "W { x: x + 1 }" in text
    assert "v + 7" not in text
    assert "v as u64 + 1" not in text


def _retro_says(monkeypatch, guidance: str) -> None:
    install_app_seams(monkeypatch, phase_c_retrospective=lambda **k: {
        "signal": "", "category": "WRONG_TYPE_CHOICE", "guidance": guidance})


def test_impl_round_whose_lesson_would_not_reach_the_prompt_does_not_re_enter(
        monkeypatch, tmp_path):
    # Run `271137-e728ae95` (2026-09-23) re-entered fix loops "with the new
    # guidance" while the screen retired that guidance at the next prompt.
    # The re-entry reads the same screen now: an improved floor whose
    # retrospective carries nothing the prompt will show buys no round.
    ws = _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("PHASE_C_IMPL_MAX_STALLS", "1")
    monkeypatch.setenv("PHASE_C_MAXIMAL_LAST_RESORT", "0")
    _retro_says(monkeypatch, "")
    _install_model(monkeypatch, [RESPONSE_BOTH])
    # Different codes: the floor stalls and the repeated-signature stop
    # stays quiet, so the screen is what decides the re-entry.
    install_scripted_cargo(monkeypatch, check_diag=_two_reds_then_green())
    res = _run(ws)
    assert res.impls_filled == 0
    assert res.attempts == 2          # round 1 only: nothing to re-enter on

    # Control: the same run with a lesson the screen admits re-enters and
    # fills on the green third check.
    ws2 = _setup(monkeypatch, tmp_path / "b")
    _retro_says(monkeypatch, "Return the tag field; do not construct a Foo.")
    _install_model(monkeypatch, [RESPONSE_BOTH])
    install_scripted_cargo(monkeypatch, check_diag=_two_reds_then_green())
    res2 = _run(ws2)
    assert res2.impls_filled == 1 and res2.attempts == 3


# --- a later call of a claimed unit is asked on the claim's ground ----------
# Run `3040-51eab48c` (2026-10-02, fill 5): a file keep turned the last red
# test green, and two file runs already inside that wave then each opened a
# second attempt for a peeled name. The unit's claim hands its ground
# question to `unit_clock`; the attempt loop reads it before every call
# after the first. The two tests above (peel retry lands `kind`; compile
# failure is retried with the diagnostics) are the negative controls: the
# same scripts with no moved ground buy the second call.


def _claim_with_ground(monkeypatch, moved) -> None:
    """Every claim of the fill carries ``moved`` as its ground question —
    what `RunClock` hands the unit clock when the fill has a premise gate.
    The harness fill has none, so the claim's own call is wrapped."""
    from pipeline_core.domain import unit_clock
    real = unit_clock.start
    monkeypatch.setattr(
        unit_clock, "start",
        lambda bound, **kw: real(bound, **{**kw, "ground": moved}))


def test_a_peel_retry_is_not_bought_once_the_claims_ground_moved(
        monkeypatch, tmp_path):
    ws = _setup(monkeypatch, tmp_path)
    (ws / "src" / "lib.rs").write_text(IMPL_LIB_TWO)
    install_cluster_scan(monkeypatch, impl_blocks=lambda *a, **k: _two_blocks())
    install_cluster_scan(monkeypatch, stub_records=lambda *a, **k: [])
    prompts: list[str] = []

    def _capture(prompt, *a, **k):
        prompts.append(prompt)
        return RESPONSE_TWO

    monkeypatch.setattr(pipeline, "_ask_raw", _capture)
    install_scripted_cargo(
        monkeypatch,
        check_diag=[(101, "error: x", [diag("src/lib.rs", code="E0308")]),
                    (0, "", [])],
        frontier={FnLocator("lib.rs", "kind"): {"<compile>"}})
    # The claim's ground: unmoved when the unit opens, moved by the time its
    # first reply has been judged (another run of the wave kept a body).
    asked: list[frozenset[str] | None] = []

    def _moved(tests=None) -> bool:
        asked.append(None if tests is None else frozenset(tests))
        return bool(prompts)

    _claim_with_ground(monkeypatch, _moved)
    res = _run(ws)
    # Asked on the tests of the one name the retry would send (`kind`, the
    # peeled body) — an explicit set, never the claim's scope (None). The
    # peeled stub holds no test in this harness, so the set is empty: a
    # call that asks no named test, refused once anything has moved.
    assert asked and asked[0] == frozenset()
    text = (ws / "src" / "lib.rs").read_text()
    assert "self.tag + 1" in text, "the judged remainder of attempt 1 stays"
    assert len(prompts) == 1, (
        "no call opens for the peeled name on ground that moved")
    assert res.answer_lost_fns == (), "a deferred name is not a lost answer"


def test_a_compile_retry_is_not_bought_once_the_claims_ground_moved(
        monkeypatch, tmp_path):
    """The revert that falls through the bottom of the attempt loop is asked
    at the same boundary as the `continue` paths: no retry turn is sent."""
    ws = _setup(monkeypatch, tmp_path)
    (ws / "src" / "lib.rs").write_text(IMPL_LIB_TWO)
    install_cluster_scan(monkeypatch, impl_blocks=lambda *a, **k: _two_blocks())
    install_cluster_scan(monkeypatch, stub_records=lambda *a, **k: [])
    sent = install_scripted_turns(monkeypatch, RESPONSE_TWO)
    monkeypatch.setenv("PHASE_C_FILE_MAX_ATTEMPTS", "3")
    install_scripted_cargo(
        monkeypatch,
        check_diag=(101, "error[E0432]: unresolved import `crate::Nope`",
                    [diag("src/lib.rs", code="E0432")]))
    _claim_with_ground(monkeypatch, lambda _tests=None: bool(sent))
    _run(ws)
    retries = [t for t in sent
               if t[-1]["content"].startswith("RESULT of your previous reply")]
    assert retries == [], "no file-first retry turn on moved ground"


# --- A reply-text refusal on the file route is a failed attempt ---------------
# Run `473471-33301154`'s shape: a cast to a `dyn` type with an unparenthesized
# `+`. rustc's parser rejects it; the splicer refuses it `parse_broken` with
# that diagnostic. Before, the file route dropped the answer (nothing spliced)
# or silently lost the refused name (partial), and the model was never told.

_CAST = "unsafe { &*(self as *const Self as *const dyn Tag + Send) }.tag"
_DIAG = "error: ambiguous `+` in a type\n --> <stdin>:5:51\n"


def _refuse_the_cast(monkeypatch) -> None:
    from pipeline_core.application.reconstruct import splice as splice_mod
    from pipeline_core.domain.reconstruction import SpliceRefusal
    real_guard = splice_mod.parse_guarded_write

    def _guard(path, new_bytes, pre_bytes, validate_parse):
        if b"dyn Tag + Send" in new_bytes:
            return False
        return real_guard(path, new_bytes, pre_bytes, validate_parse)

    monkeypatch.setattr(splice_mod, "parse_guarded_write", _guard)
    monkeypatch.setattr(splice_mod, "parse_refusal",
                        lambda text, validate: SpliceRefusal("parse_broken", _DIAG))


def _file_first_run(monkeypatch, tmp_path, replies: list[str], *,
                    delete_the_call: bool = False) -> tuple[Path, list[str]]:
    ws = _setup(monkeypatch, tmp_path)
    (ws / "src" / "lib.rs").write_text(IMPL_LIB_TWO)
    install_cluster_scan(monkeypatch, impl_blocks=lambda *a, **k: _two_blocks())
    install_cluster_scan(monkeypatch, stub_records=lambda *a, **k: [])
    _refuse_the_cast(monkeypatch)
    if delete_the_call:
        from pipeline_core.application.reconstruct import impl_fill as impl_fill_mod
        monkeypatch.setattr(impl_fill_mod, "reply_refusal_feedback",
                            lambda *a, **k: None)
    prompts: list[str] = []

    def _capture(prompt, *a, **k):
        prompts.append(prompt)
        return replies[min(len(prompts) - 1, len(replies) - 1)]

    monkeypatch.setattr(pipeline, "_ask_raw", _capture)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    _run(ws)
    return ws, prompts


def test_a_file_first_reply_refused_for_its_text_is_asked_again_with_the_parser(
        monkeypatch, tmp_path):
    both_cast = (f'<FN name="kind" block="impl Foo">{_CAST}</FN>\n'
                 f'<FN name="name" block="impl Bar">{_CAST}</FN>')
    ws, prompts = _file_first_run(monkeypatch, tmp_path, [both_cast, RESPONSE_TWO])
    text = (ws / "src" / "lib.rs").read_text()
    assert len(prompts) >= 2
    assert "ambiguous `+` in a type" in prompts[1]
    assert "self.tag + 1" in text and "unimplemented!()" not in text

    # NEGATIVE CONTROL: without the call the answer is dropped after one
    # attempt and no later prompt is told why.
    c_ws, c_prompts = _file_first_run(monkeypatch, tmp_path / "control",
                                      [both_cast, RESPONSE_TWO], delete_the_call=True)
    assert not any("ambiguous `+` in a type" in p for p in c_prompts)


def test_a_partial_file_first_keeps_the_rest_and_retries_the_refused_name(
        monkeypatch, tmp_path):
    one_cast = (f'<FN name="kind" block="impl Foo">{_CAST}</FN>\n'
                '<FN name="name" block="impl Bar">self.tag + 1</FN>')
    retry = '<FN name="kind" block="impl Foo">self.tag</FN>'
    ws, prompts = _file_first_run(monkeypatch, tmp_path, [one_cast, retry])
    text = (ws / "src" / "lib.rs").read_text()
    assert len(prompts) == 2
    assert "ambiguous `+` in a type" in prompts[1] and "kind" in prompts[1]
    assert "self.tag + 1" in text, "the landed body is judged and kept"
    assert "unimplemented!()" not in text, "the refused name is retried and lands"

    c_ws, c_prompts = _file_first_run(monkeypatch, tmp_path / "control",
                                      [one_cast, retry], delete_the_call=True)
    assert not any("ambiguous `+` in a type" in p for p in c_prompts)


# --- the crash peel: a crash the settlement pins on one body of the reply ----
#
# Run `757853-e28f1a7d` (2026-10-16, iter 4): a four-body `src/error.rs`
# file-first read `pass 59 -> 64` and then `59 -> 65`, and was restored whole
# both times, because one body (`into_boxed_dyn_error`) was the double drop
# the sanitizer named. The settlement had that culprit; nothing read it. The
# shape here is invented: `kind`'s answer crashes `kind_works`' process,
# `name`'s answer is right.

def _isolated_crash_out() -> str:
    # The shape the live crash took: under test isolation an aborted test is
    # re-run alone and rebuilt as a FAILED entry of a completed section.
    from pipeline_core.domain.test_isolation import (
        IsolatedStatus,
        IsolatedTest,
        render_isolated_section,
    )
    return ("     Running tests/t.rs (target/debug/deps/t-1)\n\n"
            + render_isolated_section([
                IsolatedTest("name_works", IsolatedStatus.PASSED, ""),
                IsolatedTest("kind_works", IsolatedStatus.ABORTED,
                             "free(): double free detected in tcache 2\n"
                             "(exit status -6)")]))


_CRASH_OUT = _isolated_crash_out()
_GREEN_OUT = ("     Running tests/t.rs (target/debug/deps/t-1)\n\n"
              "running 2 tests\ntest name_works ... ok\n"
              "test kind_works ... ok\n\n"
              "test result: ok. 2 passed; 0 failed; 0 ignored\n")
_BASE_OUT = ("     Running tests/t.rs (target/debug/deps/t-1)\n\n"
             "running 2 tests\ntest name_works ... FAILED\n"
             "test kind_works ... FAILED\n\nfailures:\n\n"
             "---- name_works stdout ----\n"
             "thread 'name_works' panicked at lib.rs:10:9:\nnot implemented\n"
             "---- kind_works stdout ----\n"
             "thread 'kind_works' panicked at lib.rs:5:9:\nnot implemented\n"
             "\nfailures:\n    name_works\n    kind_works\n\n"
             "test result: FAILED. 0 passed; 2 failed; 0 ignored\n")


def _crash_peel_case(monkeypatch, tmp_path, *, settle: bool):
    from pipeline_core.application.reconstruct import crash_settler
    from pipeline_core.domain.crash_site import CrashFrame, CrashSite

    ws = _setup(monkeypatch, tmp_path)
    lib = ws / "src" / "lib.rs"
    lib.write_text(IMPL_LIB_TWO)
    install_cluster_scan(monkeypatch, impl_blocks=lambda *a, **k: _two_blocks())
    install_cluster_scan(monkeypatch, stub_records=lambda *a, **k: [])
    prompts: list[str] = []
    replies = [RESPONSE_TWO,
               '<FN name="kind" block="impl Foo">self.tag + 2</FN>']

    def _capture(prompt, *a, **k):
        prompts.append(prompt)
        return replies.pop(0) if len(replies) > 1 else replies[0]

    monkeypatch.setattr(pipeline, "_ask_raw", _capture)

    def _run_out():
        text = lib.read_text()
        if "self.tag\n" in text or "self.tag }" in text:
            return (101, _CRASH_OUT)       # `kind`'s first answer is on the tree
        if "unimplemented!()" in text:
            return (101, _BASE_OUT)
        return (0, _GREEN_OUT)

    install_scripted_cargo(monkeypatch, check_diag=(0, "", []), run=_run_out)
    site = CrashSite(
        binary_label="tests/t.rs", test_name="kind_works", signal="SIGSEGV",
        frames=(CrashFrame("lib.rs", 10, "name", "name"),),
        origin_frames=(CrashFrame("lib.rs", 0, "kind", "kind"),),
        origin_capable=True)
    rounds = iter([[site], []])
    recorded: list = []
    settler = crash_settler.CrashSettler(crash_settler.CrashSettlerDeps(
        localize=lambda _cap, _ex: next(rounds, []),
        run_suite=lambda: _run_out()[1], is_proven=lambda _u: False,
        src_dir=ws / "src",
        record_settled=lambda locs, _s: recorded.append(list(locs)),
        log=lambda _m: None))
    crash_settler.bind(settler if settle else None)
    try:
        pipeline._phase_c_fill_impl_blocks(
            project_dir=ws, target_src_dir=ws / "src", tests_dir_files=[],
            module_name="m", cluster_name="t", cluster=None,
            check_cmd="cargo check", cluster_env={}, fr_path=None,
            tdd_test_cmd="cargo test",
            tdd_frontier_keys={FnLocator("lib.rs", "kind"),
                               FnLocator("lib.rs", "name")},
            tdd_frontier_tests=FailureFrontier.of_direct({
                FnLocator("lib.rs", "kind"): {"kind_works"},
                FnLocator("lib.rs", "name"): {"name_works"}}))
    finally:
        crash_settler.bind(None)
    return lib.read_text(), prompts, recorded


def test_a_crash_pinned_on_one_body_peels_it_and_keeps_the_rest(
        monkeypatch, tmp_path):
    text, prompts, recorded = _crash_peel_case(monkeypatch, tmp_path,
                                               settle=True)
    # `name` stood through the crash; `kind` was retried alone, with the crash.
    assert "self.tag + 1" in text and "self.tag + 2" in text
    assert len(prompts) == 2
    assert "kind" in prompts[1]
    assert "crashed the test process" in prompts[1]
    assert recorded == [[FnLocator("lib.rs", "kind")]]


def test_negative_control_without_the_crash_peel_the_reply_goes_back_whole(
        monkeypatch, tmp_path, capsys):
    # No settler bound: the pre-change judgment, and the live log's line
    # (`reverted (U2 test failure (no strict improvement; aborted: …))`). The
    # crash convicts the whole reply, the right `name` goes back with `kind`,
    # and the retry does not say which body crashed.
    text, prompts, recorded = _crash_peel_case(monkeypatch, tmp_path,
                                               settle=False)
    assert recorded == []
    assert ("reverted (U2 test failure (no strict improvement; aborted: "
            "kind_works))") in capsys.readouterr().out
    assert "self.tag + 1" not in text       # `name`'s right answer was lost
    assert "crashed the test process" not in "".join(prompts[1:])
