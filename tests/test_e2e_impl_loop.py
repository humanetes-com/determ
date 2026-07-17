"""End-to-end harness tests for the per-impl fill loop (`_phase_c_fill_impl_blocks`)
— FixLoop plan step f1: the net the ImplFixUnit port (f3) lands against.

Block records are scripted via the `_collect_impl_blocks_for_cluster` seam (the
extractor binary is absent in CI); the model seam is `pipeline.ask` (per-impl
protocol: `<FN name="..." block="...">body</FN>`); cargo via the harness
`check_diag` seam. Splice (`_splice_fn_with_where`), revert, and the FloorStall
gauge are the REAL engine on a real temp workspace.
"""
from __future__ import annotations

from pathlib import Path

import pipeline
from pipeline_core.domain.models import FnRecord, ImplBlock
from tests.harness import (
    diag,
    install_app_seams,
    install_cluster_scan,
    install_fake_extractor,
    install_scripted_cargo,
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
    # B3 is env-driven and .env sets it ON for live runs — pin it OFF here so
    # the revert-path tests are environment-independent; the B3 test pins it ON.
    monkeypatch.setenv("PHASE_C_KEEP_NONREGRESSING", "0")
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
    # Round 1: floor stall exhausts at 4. Retro round 1 baselines -> one
    # informed retry; round 2 repeats E0999 twice -> repeated exit at 2.
    assert res.attempts == 6
    assert text == IMPL_LIB              # reverted to pristine stubs
    # Extractor seam returns None → remaining must come from the text-scan
    # fallback (was 0, silently skipping the caller's per-fn cleanup pass).
    assert res.remaining == 2


def test_impl_repeated_error_signature_stops_after_two(monkeypatch, tmp_path):
    # Retro-retry parity with the per-fn path: the repeated-signature exit
    # still fires at 2 attempts per round; retro round 1 baselines the error
    # count (always eligible), round 2 shows no strict improvement -> parked.
    ws = _setup(monkeypatch, tmp_path)
    _install_model(monkeypatch, [RESPONSE_BOTH])
    install_scripted_cargo(
        monkeypatch,
        check_diag=(101, "error: x", [diag("src/lib.rs", code="E0308")]))
    res = _run(ws)
    assert res.impls_filled == 0 and res.impls_failed == 1
    assert res.attempts == 4          # 2 rounds x repeated-sig exit at 2
    assert (ws / "src" / "lib.rs").read_text() == IMPL_LIB


def test_impl_b3_keeps_nonregressing_fill(monkeypatch, tmp_path):
    # B3 (FR-029): crate still has a PRE-EXISTING error elsewhere, but this
    # block's fill adds no NEW error and none lands on the filled fns →
    # keep the fill instead of reverting (convergence-deadlock dissolver).
    ws = _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("PHASE_C_KEEP_NONREGRESSING", "1")
    _install_model(monkeypatch, [RESPONSE_BOTH])
    elsewhere = diag("src/other.rs", code="E0425")
    install_scripted_cargo(
        monkeypatch,
        # 1st call = B3 baseline, 2nd = post-splice verify: same single error.
        check_diag=[(101, "error: y", [elsewhere]),
                    (101, "error: y", [elsewhere])],
        frontier={},   # no diagnostic attributes to the filled fns
    )
    res = _run(ws)
    text = (ws / "src" / "lib.rs").read_text()
    assert res.impls_filled == 1 and res.fns_filled == 2
    assert "self.tag" in text and "unimplemented!()" not in text


def test_impl_b3_does_not_keep_green_but_test_failing_fill(monkeypatch, tmp_path):
    # debt #46 point 4: when the crate COMPILES (rc==0) but the block's own U2
    # test fails, B3 must NOT keep the green-but-wrong body — it reverts to stub
    # so the per-fn behavioral re-fill retries it. B3 keeps only while rc!=0.
    from pipeline_core.domain.models import FnLocator
    ws = _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("PHASE_C_KEEP_NONREGRESSING", "1")
    _install_model(monkeypatch, [RESPONSE_BOTH])
    # Green compile, but the scoped test run reports a failing own-test.
    install_scripted_cargo(
        monkeypatch,
        check_diag=(0, "", []),
        run=(101, "test kind_works ... FAILED\n"
                  "test result: FAILED. 0 passed; 1 failed; 0 ignored"))
    res = pipeline._phase_c_fill_impl_blocks(
        project_dir=ws, target_src_dir=ws / "src", tests_dir_files=[],
        module_name="m", cluster_name="t", cluster=None, check_cmd="cargo check",
        cluster_env={}, fr_path=None, tdd_frontier_keys=None,
        tdd_test_cmd="cargo test",
        tdd_frontier_tests={FnLocator("lib.rs", "kind"): {"kind_works"},
                            FnLocator("lib.rs", "name"): {"kind_works"}})
    text = (ws / "src" / "lib.rs").read_text()
    assert res.impls_filled == 0            # NOT kept
    assert "unimplemented!()" in text          # reverted to stub


# --- debt #46 point 2: per-impl behavioral re-fill (block-level, no stubs left) ---

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
    # debt #46 point 2: a block with stub_members=() would previously never be
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
        tdd_frontier_tests={FnLocator("lib.rs", "kind"): {"kind_is_wrong"}})
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


# --- debt #42: stale stub_members / already-filled blocks ---------------------

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


# --- P6: maximal last-resort attempt (budgets off) ---------------------------


def test_maximal_last_resort_rescues_exhausted_block(monkeypatch, tmp_path):
    """Budgeted loop dies (identical-error stop), then the ONE maximal
    attempt — whole file + tests + full error — fills the block."""
    ws = _setup(monkeypatch, tmp_path)
    _install_model(monkeypatch, [RESPONSE_BOTH])
    red = (101, "error: x", [diag("src/lib.rs", code="E0308")])
    install_scripted_cargo(
        monkeypatch, check_diag=[red, red, (0, "", [])])
    res = _run(ws)
    text = (ws / "src" / "lib.rs").read_text()
    assert res.impls_filled == 1 and res.fns_filled == 2
    assert res.impls_failed == 0
    assert res.attempts == 2            # maximal is not a loop attempt
    assert "self.tag" in text and "unimplemented!()" not in text


def test_maximal_skipped_when_over_char_cap(monkeypatch, tmp_path):
    ws = _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("PHASE_C_MAXIMAL_CHAR_CAP", "10")
    _install_model(monkeypatch, [RESPONSE_BOTH])
    install_scripted_cargo(
        monkeypatch,
        check_diag=(101, "error: x", [diag("src/lib.rs", code="E0308")]))
    res = _run(ws)
    assert res.impls_failed == 1
    assert (ws / "src" / "lib.rs").read_text() == IMPL_LIB   # reverted


def test_maximal_disabled_by_env(monkeypatch, tmp_path):
    ws = _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("PHASE_C_MAXIMAL_LAST_RESORT", "0")
    # Cap retro rounds to 1 so no informed retry fires either — this test
    # pins ONLY that the maximal-last-resort path is env-disabled.
    monkeypatch.setenv("PER_FN_RETRO_ROUNDS_MAX", "1")
    _install_model(monkeypatch, [RESPONSE_BOTH])
    red = (101, "error: x", [diag("src/lib.rs", code="E0308")])
    install_scripted_cargo(monkeypatch, check_diag=[red, red, (0, "", [])])
    res = _run(ws)
    assert res.impls_failed == 1        # green 3rd check never reached


def test_impl_retro_retry_recovers_on_second_round(monkeypatch, tmp_path):
    # The value pin: round 1 exhausts (repeated sig), the retro's guidance
    # feeds an immediate second round whose fill compiles clean -> filled.
    ws = _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("PHASE_C_MAXIMAL_LAST_RESORT", "0")
    _install_model(monkeypatch, [RESPONSE_BOTH, RESPONSE_BOTH, RESPONSE_BOTH])
    red = (101, "error: x", [diag("src/lib.rs", code="E0308")])
    install_scripted_cargo(monkeypatch, check_diag=[red, red, (0, "", [])])
    res = _run(ws)
    assert res.impls_filled == 1 and res.impls_failed == 0
    assert res.attempts == 3            # 2 failed + 1 green post-retro


# --- P2: pull protocol through the impl fill path -----------------------------


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


# --- P5: file-first fill for multi-block files --------------------------------

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
    # File-first is one shot (an immediate retry with the same limited
    # context just reproduces the same mistake — retry investment goes to
    # the end-of-iteration retro round instead) → one revert; per-block
    # takes over and succeeds.
    install_scripted_cargo(
        monkeypatch,
        check_diag=[(101, "error: x", [diag("src/lib.rs", code="E0308")]),
                    (0, "", [])])
    res = _run(ws)
    text = (ws / "src" / "lib.rs").read_text()
    # debt #54's routing widening lets a per-block turn splice a SIBLING
    # container's body too (not just its own target) when the model answers
    # both together, as RESPONSE_TWO always does here — Foo's turn now fills
    # BOTH `kind` and `name` in one call, so Bar's own turn finds `name`
    # already filled and completes via the freshness gate without a NEW
    # fill of its own. Content correctness (both fns filled, no stubs left)
    # is what matters — the block-attribution count is one accounting
    # consequence of the fix, not the thing being tested here.
    assert res.impls_filled == 1 and res.impls_failed == 0
    assert "self.tag" in text and "unimplemented!()" not in text


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


def test_file_first_skipped_for_single_block(monkeypatch, tmp_path):
    ws = _setup(monkeypatch, tmp_path)      # default single-block fixture
    _install_model(monkeypatch, [RESPONSE_BOTH])
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    res = _run(ws)
    assert res.impls_filled == 1          # per-block path, as before
