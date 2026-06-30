"""C2: write_phase_c_test_fix_log disk-archive adapter."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.adapters.phase_c_log import write_phase_c_test_fix_log
from pipeline_core.domain.workspace_layout import PHASE_C_LOGS_DIR


def test_writes_iteration_archive(tmp_path: Path) -> None:
    write_phase_c_test_fix_log(
        tmp_path, cluster_name="default", pass_n=1, fix_iter=3,
        test_out="<<cargo output>>",
        failed_tests=["t::a", "t::b"],
        fns_retried=[("error.rs", "context")],
        cur_fail_count=5, new_fail_count=4, best_fail_count=4, stall=1,
    )
    fp = tmp_path / PHASE_C_LOGS_DIR / "default" / "pass_1" / "fix_03.txt"
    assert fp.exists()
    body = fp.read_text(encoding="utf-8")
    assert "cluster: default" in body
    assert "- t::a" in body and "error.rs::context" in body
    assert body.endswith("<<cargo output>>")


def test_write_failure_is_swallowed(tmp_path: Path, monkeypatch) -> None:
    # project_dir is a file → mkdir raises; the adapter must not propagate.
    bad = tmp_path / "afile"
    bad.write_text("x")
    write_phase_c_test_fix_log(
        bad, cluster_name="c", pass_n=1, fix_iter=1, test_out="",
        failed_tests=[], fns_retried=[], cur_fail_count=0, new_fail_count=0,
        best_fail_count=0, stall=0,
    )  # no raise
