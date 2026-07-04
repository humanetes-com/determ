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
from tests.harness import diag, install_scripted_cargo

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
    monkeypatch.setattr(pipeline, "_collect_impl_blocks_for_cluster",
                        lambda *a, **k: [_block()])
    monkeypatch.setattr(pipeline, "_scan_unimplemented_records_for_cluster",
                        lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_enumerate_types_via_extractor",
                        lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "_dispatch_families_via_extractor",
                        lambda *a, **k: [])
    return ws


def _install_model(monkeypatch, responses):
    seq = list(responses)
    monkeypatch.setattr(pipeline, "_ask_raw",
                        lambda *a, **k: seq.pop(0) if len(seq) > 1 else seq[0])


def _run(ws: Path) -> dict:
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
    assert res["impls_filled"] == 1 and res["fns_filled"] == 2
    assert "self.tag" in text and "unimplemented!()" not in text
    assert res["remaining"] == 0


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
    assert res["impls_filled"] == 0 and res["impls_failed"] == 1
    assert res["attempts"] == 4
    assert text == IMPL_LIB              # reverted to pristine stubs
    # Extractor seam returns None → remaining must come from the text-scan
    # fallback (was 0, silently skipping the caller's per-fn cleanup pass).
    assert res["remaining"] == 2


def test_impl_repeated_error_signature_stops_after_two(monkeypatch, tmp_path):
    ws = _setup(monkeypatch, tmp_path)
    _install_model(monkeypatch, [RESPONSE_BOTH])
    install_scripted_cargo(
        monkeypatch,
        check_diag=(101, "error: x", [diag("src/lib.rs", code="E0308")]))
    res = _run(ws)
    assert res["impls_filled"] == 0 and res["impls_failed"] == 1
    assert res["attempts"] == 2
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
    assert res["impls_filled"] == 1 and res["fns_filled"] == 2
    assert "self.tag" in text and "unimplemented!()" not in text


# --- debt #42: stale stub_members / already-filled blocks ---------------------

IMPL_LIB_FILLED = IMPL_LIB.replace("unimplemented!()", "7")


def test_already_complete_block_skips_llm_call(monkeypatch, tmp_path):
    """Freshness gate: the re-scan says no member of this block is still a
    stub → skip without prompting, and the block is NOT counted failed."""
    ws = _setup(monkeypatch, tmp_path)
    (ws / "src" / "lib.rs").write_text(IMPL_LIB_FILLED)
    # Fresh scan authority: no stubs anywhere (empty list ≠ None).
    monkeypatch.setattr(pipeline, "_scan_unimplemented_records_for_cluster",
                        lambda *a, **k: [])
    def _no_call(*_a, **_k):
        raise AssertionError("model must not be asked for a complete block")
    monkeypatch.setattr(pipeline, "_ask_raw", _no_call)
    res = _run(ws)
    assert res["impls_failed"] == 0 and res["impls_filled"] == 0
    assert res["attempts"] == 0


def test_partial_staleness_refills_only_remaining_stub(monkeypatch, tmp_path):
    """One member already filled by an earlier family fill: the block record's
    stub list is refreshed, the filled fn's returned body refuses
    (target_not_stub) and the still-stub member splices."""
    ws = _setup(monkeypatch, tmp_path)
    half = IMPL_LIB.replace(
        "pub fn kind(&self) -> u32 {\n        unimplemented!()\n    }",
        "pub fn kind(&self) -> u32 {\n        9\n    }")
    (ws / "src" / "lib.rs").write_text(half)
    monkeypatch.setattr(
        pipeline, "_scan_unimplemented_records_for_cluster",
        lambda *a, **k: [{"file": "lib.rs", "fn_name": "name",
                          "container": "impl Foo"}])
    _install_model(monkeypatch, [RESPONSE_BOTH])
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    res = _run(ws)
    text = (ws / "src" / "lib.rs").read_text()
    assert res["impls_filled"] == 1 and res["impls_failed"] == 0
    assert res["fns_filled"] == 1
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
    assert res["impls_failed"] == 0 and res["impls_filled"] == 0
    assert res["attempts"] == 1
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
    assert res["impls_filled"] == 1 and res["fns_filled"] == 2
    assert res["impls_failed"] == 0
    assert res["attempts"] == 2            # maximal is not a loop attempt
    assert "self.tag" in text and "unimplemented!()" not in text


def test_maximal_skipped_when_over_char_cap(monkeypatch, tmp_path):
    ws = _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("PHASE_C_MAXIMAL_CHAR_CAP", "10")
    _install_model(monkeypatch, [RESPONSE_BOTH])
    install_scripted_cargo(
        monkeypatch,
        check_diag=(101, "error: x", [diag("src/lib.rs", code="E0308")]))
    res = _run(ws)
    assert res["impls_failed"] == 1
    assert (ws / "src" / "lib.rs").read_text() == IMPL_LIB   # reverted


def test_maximal_disabled_by_env(monkeypatch, tmp_path):
    ws = _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("PHASE_C_MAXIMAL_LAST_RESORT", "0")
    _install_model(monkeypatch, [RESPONSE_BOTH])
    red = (101, "error: x", [diag("src/lib.rs", code="E0308")])
    install_scripted_cargo(monkeypatch, check_diag=[red, red, (0, "", [])])
    res = _run(ws)
    assert res["impls_failed"] == 1        # green 3rd check never reached
