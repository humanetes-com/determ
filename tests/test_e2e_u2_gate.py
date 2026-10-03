"""Option B: the U2 test gate (_u2_test_gate) must reject a fill that compiles
but fails its attributed tests, and the fill loop must retry until the correct
body is found.

The gate (_u2_test_gate) is enabled by default —
_UNIFIED_FILL_LOOP = os.getenv("UNIFIED_FILL_LOOP", "1") == "1".

Observable: without the gate, the wrong-but-compiling body (a - b) is accepted
on compile check alone and stays in the file.  With the gate (the default), it
is rejected, the loop retries, and the correct body (a + b) is written.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pipeline
from pipeline_core.domain.attribution import FailureFrontier
from pipeline_core.domain.enums import FillUnit
from tests.harness import install_cargo_runner, install_cluster_scan, install_fake_extractor

_STUB_SRC = "pub fn add(a: i32, b: i32) -> i32 { unimplemented!() }\n"


def _setup(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    lib = ws / "src" / "lib.rs"
    lib.write_text(_STUB_SRC, encoding="utf-8")
    # Use per-fn unit; disable extractor (not built in CI).
    monkeypatch.setattr(
        pipeline, "SETTINGS",
        dataclasses.replace(pipeline.SETTINGS, phase_c_fill_unit=FillUnit.FN))
    install_cluster_scan(monkeypatch, stub_records=lambda *a, **k: None)
    install_fake_extractor(monkeypatch, fns=None, types=[])
    return ws, lib


def _run(ws: Path) -> None:
    pipeline._task2_fill_fns_one_by_one(
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
        # U2 gate inputs: "add" must pass "test_add"
        tdd_frontier_tests=FailureFrontier.of_direct({("lib.rs", "add"): {"test_add"}}),
        tdd_test_cmd="cargo test",
    )


def test_wrong_compiling_fill_rejected_by_u2_gate(monkeypatch, tmp_path):
    """A fill that compiles but fails the fn's attributed test must be rejected
    and retried until the correct implementation is found.

    With _UNIFIED_FILL_LOOP enabled (default): gate runs → (101, FAILED) for the
    wrong body (a - b) → fill rejected → retry → correct body (a + b) accepted.
    """
    ws, lib = _setup(monkeypatch, tmp_path)

    # Model: first attempt wrong, second correct.
    bodies = iter(["a - b", "a + b"])
    import dataclasses as _dc
    monkeypatch.setattr(pipeline, "_APP", _dc.replace(
        pipeline._APP,
        ask_per_fn_body=lambda *_a, **_k: (next(bodies, "a + b"), None)))



    # _run_cargo: distinguishes compile run ("cargo check") from U2 test run
    # ("cargo test") by inspecting the command string AND the file content.
    def _cargo_run(cmd, *_a, **_k):
        # Well-formed libtest output: named failure WITH its body and the
        # trailer. The earlier truncated form (no body, no trailer) parsed
        # as a process abort, so `test_add` landed in `no_verdict` and the
        # wrong body was only ever caught by the regression gate's
        # crash-only revert — the charge that gate no longer makes. The
        # gate THIS test pins is U2's self-failure REJECT, which needs a
        # verdict to reject on.
        if "test" in cmd:
            return (
                (101,
                 "running 1 test\n"
                 "test test_add ... FAILED\n"
                 "\nfailures:\n\n"
                 "---- test_add stdout ----\n"
                 "assertion `left == right` failed\n"
                 "\nfailures:\n    test_add\n"
                 "\ntest result: FAILED. 0 passed; 1 failed; 0 ignored")
                if "a - b" in lib.read_text(encoding="utf-8")
                else (0, "test result: ok. 1 passed; 0 failed; 0 ignored")
            )
        return (0, "")  # compile check passes

    # compile check always clean; run distinguishes check vs U2 test (above).
    install_cargo_runner(monkeypatch, run=_cargo_run,
                         check_diag=lambda *_a, **_k: (0, "", []))

    # _UNIFIED_FILL_LOOP is NOT patched here — we rely on the default (enabled).

    _run(ws)

    text = lib.read_text(encoding="utf-8")
    assert "a + b" in text, (
        "U2 gate should have rejected the wrong fill (a - b) and retried."
    )
    assert "a - b" not in text
    assert "unimplemented!" not in text
