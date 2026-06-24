"""Characterization for the relocated attribution-cluster diagnoses (P6).
Each use-case is driven through INJECTED fakes (no monkeypatch), pinning the
classifier parse, the regression-note hook, and the cargo-error implicator."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct.failure_diagnosis import (
    classify_phase_c_test_failures,
    implicated_fns_from_cargo_errors,
    record_tdd_regression,
)


def test_classify_parses_lines_and_drops_unknown() -> None:
    out_lines = (
        "TEST t_bad: BAD_FILL [fn=src/a.rs::foo] — wrong body\n"
        "TEST t_env: ENV_DEPENDENT — missing file\n"
        "TEST t_x: NONSENSE_CATEGORY — dropped\n"
    )
    logged: list[str] = []
    res = classify_phase_c_test_failures(
        "cargo tail", [("src/a.rs", "foo")], ["t_bad", "t_env", "t_x"], "stable",
        ask=lambda *a, **k: out_lines, log=logged.append, model="m", err_cap=1000,
    )
    assert res["t_bad"] == {"category": "BAD_FILL", "fn": "src/a.rs::foo", "reason": "wrong body"}
    assert res["t_env"]["category"] == "ENV_DEPENDENT"
    assert "t_x" not in res  # unknown category dropped


def test_classify_empty_on_no_failures() -> None:
    assert classify_phase_c_test_failures(
        "", [], [], "c", ask=lambda *a, **k: "x", log=lambda s: None,
        model="m", err_cap=10) == {}


def test_classify_swallows_llm_error() -> None:
    logged: list[str] = []

    def boom(*a: object, **k: object) -> str:
        raise RuntimeError("vllm down")

    res = classify_phase_c_test_failures(
        "tail", [], ["t1"], "c", ask=boom, log=logged.append, model="m", err_cap=5)
    assert res == {} and logged and "LLM call failed" in logged[0]


def test_record_tdd_regression_writes_note_for_broken_fn(tmp_path: Path) -> None:
    fr = tmp_path / "fr.md"
    notes: list[tuple[Path, str]] = []
    recs = [{"fn_name": "foo", "file": "src/a.rs", "body_text": "0 // wrong"}]
    msg = record_tdd_regression(
        fr, tmp_path, [("src/a.rs", "foo")], {"t1", "t2"}, 14, 11,
        enumerate_fns=lambda d: recs, append_note=lambda p, n: notes.append((p, n)),
        body_cap=200,
    )
    assert msg == "regressed t1, t2"
    assert notes and "`foo`" in notes[0][1] and "0 // wrong" in notes[0][1]


def test_implicated_fns_backward_scan_fallback(tmp_path: Path) -> None:
    src = tmp_path / "a.rs"
    src.write_text("fn helper() {\n    let x = 1;\n}\n", encoding="utf-8")
    cargo = "error[E0308]: mismatched types\n  --> a.rs:2:13\n"
    # enumerate_fns=None forces the first_fn_decl_name backward walk.
    pairs = implicated_fns_from_cargo_errors(cargo, tmp_path, enumerate_fns=lambda d: None)
    assert pairs == [("a.rs", "helper")]
