"""Pins for the post-pass test-retry loop (FixLoop plan f4 — loop C in
`_task2_fill_fns_one_by_one`'s end-of-pass test phase), written BEFORE the
port onto `_drive_fix_loop` so the port is held to today's behavior.

C's contract (differs from loops A/B): NO revert — re-splices are kept on
every exit path; the metric is the failing-test count with the floor SEEDED
by the count the loop entered with (a non-improving first iteration counts
as stall 1); naive token-match candidate selection on iteration 1, the LLM
classifier only from stall>=1 (patched out here); a broken cargo check stops
the loop ("leaving for next pass") keeping the splice.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pipeline
from pipeline_core.domain.enums import FillUnit
from tests.harness import (
    install_app_seams,
    install_cluster_scan,
    install_fake_extractor,
    install_scripted_cargo,
    install_scripted_model,
)

STUB_LIB = """pub fn answer() -> u32 {
    unimplemented!()
}
"""

FAILED = ("running 1 test\ntest t_answer ... FAILED\n\nfailures:\n"
          "    t_answer\n\ntest result: FAILED. 0 passed; 1 failed\n")


def _setup(monkeypatch, tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "lib.rs").write_text(STUB_LIB)
    (ws / "tests").mkdir()
    (ws / "tests" / "t_answer.rs").write_text(
        "#[test]\nfn t_answer() { assert_eq!(answer(), 42); }\n")
    monkeypatch.setattr(
        pipeline, "SETTINGS",
        dataclasses.replace(pipeline.SETTINGS, phase_c_fill_unit=FillUnit.FN))
    install_cluster_scan(monkeypatch, stub_records=lambda *a, **k: None)
    install_fake_extractor(monkeypatch, fns=None, types=[])
    # The classifier is an LLM call — patch it out via the App seam; candidates
    # then come from the naive token match ("answer" ⊂ "t_answer").
    install_app_seams(monkeypatch,
                      classify_phase_c_test_failures=lambda *a, **k: {})
    return ws


def _run(ws: Path) -> dict:
    return pipeline._task2_fill_fns_one_by_one(
        project_dir=ws,
        target_src_dir=ws / "src",
        tests_dir_files=[ws / "tests" / "t_answer.rs"],
        module_name="m",
        cluster_name="t",
        cluster=None,
        check_cmd="cargo check",
        cluster_env={},
        max_passes=1,
        fr_path=None,
        skip_end_pass_test=False,
    )


def test_retry_fixes_failing_test(monkeypatch, tmp_path):
    ws = _setup(monkeypatch, tmp_path)
    bodies = iter(["41", "42"])
    install_scripted_model(monkeypatch,
                           per_fn_body=lambda _fn: next(bodies, "42"))
    # fill verify ok and retry check ok (both the check port, where the tree
    # is also built in the other cfg worlds) → pass-test FAILED → test ok.
    cargo = install_scripted_cargo(
        monkeypatch, check_diag=(0, "", []),
        run=[(101, FAILED), (0, "ok")])
    res = _run(ws)
    text = (ws / "src" / "lib.rs").read_text()
    assert "42" in text and "41" not in text      # re-splice kept
    assert res.attempts == 2                   # fill + one retry
    assert cargo["run"].calls == 2


def test_retry_stall_exits_after_three_nonimproving_iters(monkeypatch, tmp_path):
    # Floor is SEEDED with the entry failure count (1): every non-improving
    # iteration stalls — 3 iterations (stall 1,2,3), then stop. Last splice
    # is KEPT (no revert in loop C).
    ws = _setup(monkeypatch, tmp_path)
    bodies = iter(["41", "b1", "b2", "b3"])
    install_scripted_model(monkeypatch,
                           per_fn_body=lambda _fn: next(bodies, "b3"))
    cargo = install_scripted_cargo(
        monkeypatch, check_diag=(0, "", []),
        run=[(101, FAILED), (101, FAILED), (101, FAILED), (101, FAILED)])
    res = _run(ws)
    text = (ws / "src" / "lib.rs").read_text()
    assert "b3" in text                           # last attempt kept
    assert res.attempts == 4                   # fill + 3 retries
    assert cargo["run"].calls == 4                # 1 pass-test + 3×test


def test_retry_broken_check_restores_the_presplice_tree(monkeypatch, tmp_path):
    # Re-splice breaks cargo check → the loop stops after ONE iteration and
    # RESTORES the pre-splice file. This loop splices with allow_nonstub=True
    # under no U2 and no regression gate, so its old "leaving for next pass"
    # exit was the one site in the fill machinery that could break a
    # compiling tree and keep the wreck — every sibling judged after it
    # inherited manufactured compile errors.
    ws = _setup(monkeypatch, tmp_path)
    bodies = iter(["41", "broken_body"])
    install_scripted_model(monkeypatch,
                           per_fn_body=lambda _fn: next(bodies, "broken_body"))
    def _check():  # noqa: ANN202
        broken = "broken_body" in (ws / "src" / "lib.rs").read_text()
        return ((101, "error[E0308]: broken\n --> src/lib.rs:2:5", []) if broken
                else (0, "", []))

    cargo = install_scripted_cargo(
        monkeypatch, check_diag=_check,
        run=[(101, FAILED)])
    res = _run(ws)
    text = (ws / "src" / "lib.rs").read_text()
    assert "broken_body" not in text
    assert "41" in text                        # the compiling fill survives
    assert res.attempts == 2
    assert cargo["run"].calls == 1                # no cargo test after broken check


def test_retry_no_resplice_stops_immediately(monkeypatch, tmp_path):
    # Fix model yields nothing → any_progress False → loop stops after one
    # iteration without running cargo again; the fill body stays.
    ws = _setup(monkeypatch, tmp_path)
    bodies = iter(["41"])
    install_scripted_model(monkeypatch,
                           per_fn_body=lambda _fn: next(bodies, None))
    cargo = install_scripted_cargo(
        monkeypatch, check_diag=(0, "", []),
        run=[(101, FAILED)])
    res = _run(ws)
    text = (ws / "src" / "lib.rs").read_text()
    assert "41" in text
    assert res.attempts == 2                   # fill + the no-body retry ask
    assert cargo["run"].calls == 1
