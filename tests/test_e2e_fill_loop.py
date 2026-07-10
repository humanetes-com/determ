"""End-to-end harness tests for the per-fn fill loop (`_task2_fill_fns_one_by_one`
→ `_process_one_stub`) — the net the FixLoop dedup (slice 8a) is built against.

Scripted model + scripted cargo via tests/harness.py; the extractor seams are
forced to their no-binary fallbacks so the loop runs the text-scan stub path.
Each test drives the REAL engine on a real temp workspace: real splice, real
revert, real FloorStall gauge, real annotated-stub exhaustion path.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pipeline
from pipeline_core.domain.enums import FillUnit
from tests.harness import install_fake_extractor, install_scripted_cargo, install_scripted_model

STUB_LIB = """pub fn answer() -> u32 {
    unimplemented!()
}
"""

ERR = ("error[{code}]: {msg}\n --> src/lib.rs:2:5\n"
       "error: aborting due to previous error\n")


def _setup(monkeypatch, tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "lib.rs").write_text(STUB_LIB)
    # Per-fn unit (skip the per-impl path) without touching the frozen Settings.
    monkeypatch.setattr(
        pipeline, "SETTINGS",
        dataclasses.replace(pipeline.SETTINGS, phase_c_fill_unit=FillUnit.FN))
    # Extractor-absent fallbacks: name-only stub scan, no type/fn records.
    monkeypatch.setattr(pipeline, "_scan_unimplemented_records_for_cluster",
                        lambda *a, **k: None)
    install_fake_extractor(monkeypatch, fns=None, types=[])
    return ws


def _run(ws: Path) -> dict:
    return pipeline._task2_fill_fns_one_by_one(
        project_dir=ws,
        target_src_dir=ws / "src",
        tests_dir_files=[],
        module_name="m",
        cluster_name="t",
        cluster=None,
        check_cmd="cargo check",
        cluster_env={},
        max_passes=1,
        fr_path=None,
        skip_end_pass_test=True,
    )


def test_fill_first_try_success(monkeypatch, tmp_path):
    ws = _setup(monkeypatch, tmp_path)
    install_scripted_model(monkeypatch, per_fn_body="42")
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []), run=(0, "ok"))
    res = _run(ws)
    text = (ws / "src" / "lib.rs").read_text()
    assert res.fns_filled == 1 and res.remaining == 0
    assert "42" in text and "unimplemented!()" not in text


def test_fix_loop_recovers_on_second_attempt(monkeypatch, tmp_path):
    ws = _setup(monkeypatch, tmp_path)
    bodies = iter(["0", "42"])
    install_scripted_model(monkeypatch,
                           per_fn_body=lambda _fn: next(bodies, "42"))
    install_scripted_cargo(
        monkeypatch, check_diag=(0, "", []),
        run=[(101, ERR.format(code="E0308", msg="mismatched types")),
             (0, "ok")])
    res = _run(ws)
    text = (ws / "src" / "lib.rs").read_text()
    assert res.fns_filled == 1
    assert res.attempts == 2          # one failure, one recovery
    assert "42" in text and "unimplemented!()" not in text


def test_repeated_error_signature_stops_after_two_attempts(monkeypatch, tmp_path):
    ws = _setup(monkeypatch, tmp_path)
    install_scripted_model(monkeypatch, per_fn_body="0")
    # Same E-code (same sig) every time → FloorStall.repeated fires on attempt 2.
    install_scripted_cargo(
        monkeypatch, check_diag=(0, "", []),
        run=(101, ERR.format(code="E0308", msg="mismatched types")))
    res = _run(ws)
    text = (ws / "src" / "lib.rs").read_text()
    assert res.fns_filled == 0 and res.remaining == 1
    assert res.attempts == 2
    assert "unimplemented!()" in text    # annotated stub restored


def test_floor_stall_exhaustion_restores_annotated_stub(monkeypatch, tmp_path):
    ws = _setup(monkeypatch, tmp_path)
    install_scripted_model(monkeypatch, per_fn_body="0")
    # Distinct E-code each attempt (sig never repeats), constant error count
    # → the floor never improves; stall budget (3) exhausts on attempt 4.
    codes = iter(["E0308", "E0425", "E0277", "E0599", "E0061"])
    install_scripted_cargo(
        monkeypatch, check_diag=(0, "", []),
        run=lambda: (101, ERR.format(code=next(codes, "E0999"), msg="x")))
    res = _run(ws)
    text = (ws / "src" / "lib.rs").read_text()
    assert res.fns_filled == 0 and res.remaining == 1
    assert res.attempts == 4          # observe(improved) + 3 stalls
    assert "unimplemented!()" in text


# --- Plan 10 U2 routing through the per-fn loop (FixLoop plan, pre-f2 pins) ---


def test_u2_assertion_failure_routes_to_fix_loop(monkeypatch, tmp_path):
    # Compiles, but the fn's own attributed test FAILS with a real assertion
    # → must route through the fix loop like a compile error, then accept
    # when the gate passes on attempt 2.
    ws = _setup(monkeypatch, tmp_path)
    bodies = iter(["0", "42"])
    install_scripted_model(monkeypatch,
                           per_fn_body=lambda _fn: next(bodies, "42"))
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []), run=(0, "ok"))
    gates = iter([(frozenset({"t_answer"}),
                   "test t_answer FAILED: assertion `left == right` failed"),
                  None])
    monkeypatch.setattr(pipeline, "_u2_test_gate",
                        lambda *a, **k: next(gates, None))
    res = pipeline._task2_fill_fns_one_by_one(
        project_dir=ws, target_src_dir=ws / "src", tests_dir_files=[],
        module_name="m", cluster_name="t", cluster=None,
        check_cmd="cargo check", cluster_env={}, max_passes=1, fr_path=None,
        skip_end_pass_test=True,
        tdd_frontier_tests={("lib.rs", "answer"): {"t_answer"}},
        tdd_test_cmd="cargo test")
    assert res.fns_filled == 1
    assert res.attempts == 2
    assert "42" in (ws / "src" / "lib.rs").read_text()


def test_u2_codependent_stub_panic_keeps_body(monkeypatch, tmp_path):
    # Compiles, attributed test fails with a "not implemented" panic — a
    # SIBLING stub's fault (#3 co-dependency) → keep the body, no fix loop.
    ws = _setup(monkeypatch, tmp_path)
    install_scripted_model(monkeypatch, per_fn_body="42")
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []), run=(0, "ok"))
    monkeypatch.setattr(
        pipeline, "_u2_test_gate",
        lambda *a, **k: (frozenset({"t_answer"}),
                         "thread 't_answer' panicked at 'not implemented'"))
    res = pipeline._task2_fill_fns_one_by_one(
        project_dir=ws, target_src_dir=ws / "src", tests_dir_files=[],
        module_name="m", cluster_name="t", cluster=None,
        check_cmd="cargo check", cluster_env={}, max_passes=1, fr_path=None,
        skip_end_pass_test=True,
        tdd_frontier_tests={("lib.rs", "answer"): {"t_answer"}},
        tdd_test_cmd="cargo test")
    assert res.fns_filled == 1
    assert res.attempts == 1
    assert "42" in (ws / "src" / "lib.rs").read_text()


def test_sig_change_hatch_applies_on_attempt_five(monkeypatch, tmp_path):
    # P3.1 pin (pre-f2): after 4 compile failures with IMPROVING error counts
    # (so neither floor-stall nor repeated-sig exits first), attempt 5 opens
    # the sig-change hatch — a <SIGNATURE> block from a non-pub fn is applied
    # before the body splice. The hatch reads the RAW response, so this test
    # scripts `_ask_per_fn_body` directly instead of the harness model seam.
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    # Non-pub on purpose: _replace_fn_signature refuses pub fns.
    (ws / "src" / "lib.rs").write_text(
        "fn answer() -> u32 {\n    unimplemented!()\n}\n")
    monkeypatch.setattr(pipeline, "SETTINGS",
                        dataclasses.replace(pipeline.SETTINGS,
                                            phase_c_fill_unit=FillUnit.FN))
    monkeypatch.setattr(pipeline, "_scan_unimplemented_records_for_cluster",
                        lambda *a, **k: None)
    install_fake_extractor(monkeypatch, fns=None, types=[])

    raw_with_sig = ("<SIGNATURE>fn answer() -> u64</SIGNATURE>\n"
                    "<BODY>42</BODY>")
    asks = {"n": 0}

    def fake_ask(prompt, fn, *a, **k):
        asks["n"] += 1
        return ("42", raw_with_sig) if asks["n"] >= 5 else ("0", "<BODY>0</BODY>")

    monkeypatch.setattr(pipeline, "_ask_per_fn_body", fake_ask)
    # Attempts 1-4: strictly DECREASING distinct-E-code counts (improving
    # floor → no stall, different sigs → no repeated-exit). Attempt 5: ok.
    fails = iter([
        (101, "error[E0308]: a\nerror[E0425]: b\nerror[E0277]: c\nerror[E0599]: d\n --> src/lib.rs:2:5"),
        (101, "error[E0308]: a\nerror[E0425]: b\nerror[E0277]: c\n --> src/lib.rs:2:5"),
        (101, "error[E0308]: a\nerror[E0425]: b\n --> src/lib.rs:2:5"),
        (101, "error[E0308]: a\n --> src/lib.rs:2:5"),
        (0, "ok"),
    ])
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []),
                           run=lambda: next(fails, (0, "ok")))
    res = _run(ws)
    text = (ws / "src" / "lib.rs").read_text()
    assert res.fns_filled == 1
    assert res.attempts == 5
    assert "u64" in text and "42" in text and "unimplemented!()" not in text


# --- Retro-informed immediate retry (the t2a per-fn loop: fix loop exhausts →
# retrospective → re-enter the fix loop with the guidance, while the error
# floor keeps strictly improving; park only on a no-improvement round) ---

RETRO_ANSWER = "CATEGORY: WRONG_TYPE_CHOICE\nGUIDANCE: return a u32 literal"


def test_retro_retry_recovers_after_first_fix_loop_exhausts(monkeypatch, tmp_path):
    ws = _setup(monkeypatch, tmp_path)
    bodies = iter(["0", "0", "42"])
    install_scripted_model(monkeypatch,
                           per_fn_body=lambda _fn: next(bodies, "42"))
    # Real (non-transient) retro → round 1 establishes the improvement
    # baseline → the fn re-enters the fix loop instead of being parked.
    monkeypatch.setattr(pipeline, "_ask_retrospective",
                        lambda *a, **k: RETRO_ANSWER)
    # Round 1: same E-code twice → repeated-signature exit after 2 attempts.
    # Round 2 (post-retro): attempt 3 compiles clean → filled.
    install_scripted_cargo(
        monkeypatch, check_diag=(0, "", []),
        run=[(101, ERR.format(code="E0308", msg="mismatched types")),
             (101, ERR.format(code="E0308", msg="mismatched types")),
             (0, "ok")])
    res = _run(ws)
    text = (ws / "src" / "lib.rs").read_text()
    assert res.fns_filled == 1 and res.remaining == 0
    assert res.attempts == 3
    assert "42" in text and "unimplemented!()" not in text


def test_retro_retry_parks_fn_when_error_floor_stops_improving(monkeypatch, tmp_path):
    ws = _setup(monkeypatch, tmp_path)
    install_scripted_model(monkeypatch, per_fn_body="0")
    monkeypatch.setattr(pipeline, "_ask_retrospective",
                        lambda *a, **k: RETRO_ANSWER)
    # Every attempt fails with the SAME single error → round 1 baselines the
    # error count, round 2 shows no strict improvement → fully exhausted.
    install_scripted_cargo(
        monkeypatch, check_diag=(0, "", []),
        run=(101, ERR.format(code="E0308", msg="mismatched types")))
    res = _run(ws)
    text = (ws / "src" / "lib.rs").read_text()
    assert res.fns_filled == 0 and res.remaining == 1
    assert res.attempts == 4          # 2 rounds × repeated-sig exit at 2
    assert "unimplemented!()" in text    # annotated stub restored
    from pipeline_core.adapters.retros import load_retrospectives, retro_key
    rec = load_retrospectives(ws)[retro_key("lib.rs", "answer", "")]
    assert rec["fully_exhausted"] is True
    assert rec["retrospective_count"] == 2


def test_transient_retro_does_not_trigger_immediate_retry(monkeypatch, tmp_path):
    ws = _setup(monkeypatch, tmp_path)
    install_scripted_model(monkeypatch, per_fn_body="0")
    # install_scripted_model leaves _ask_retrospective returning "" →
    # empty_response (transient): no state update, no immediate retry.
    install_scripted_cargo(
        monkeypatch, check_diag=(0, "", []),
        run=(101, ERR.format(code="E0308", msg="mismatched types")))
    res = _run(ws)
    assert res.fns_filled == 0 and res.remaining == 1
    assert res.attempts == 2          # single round, no retro retry
    from pipeline_core.adapters.retros import load_retrospectives
    assert load_retrospectives(ws) == {}
