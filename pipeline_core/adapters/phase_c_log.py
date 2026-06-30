"""Phase C test-fix log archive — write one ``fix_NN.txt`` per test-fix
iteration under ``.phase_c_logs/<cluster>/pass_<n>/`` for post-hoc analysis.
Relocated from pipeline.py (C2). Pure observability: a write failure is logged
and swallowed, never control flow."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.domain.workspace_layout import PHASE_C_LOGS_DIR
from pipeline_core.log import log


def write_phase_c_test_fix_log(
    project_dir: Path,
    cluster_name: str,
    pass_n: int,
    fix_iter: int,
    test_out: str,
    failed_tests: list[str],
    fns_retried: list[tuple[str, str]],
    cur_fail_count: int,
    new_fail_count: int,
    best_fail_count: int,
    stall: int,
) -> None:
    """Archive one test-fix iteration to disk for post-hoc analysis (P4)."""
    try:
        out_dir = project_dir / PHASE_C_LOGS_DIR / cluster_name / f"pass_{pass_n}"
        out_dir.mkdir(parents=True, exist_ok=True)
        fp = out_dir / f"fix_{fix_iter:02d}.txt"
        header = (
            f"# Phase C test-fix iteration\n"
            f"cluster: {cluster_name}\n"
            f"pass:    {pass_n}\n"
            f"iter:    {fix_iter}\n"
            f"prev_fail_count: {cur_fail_count}\n"
            f"new_fail_count:  {new_fail_count}\n"
            f"best_fail_count: {best_fail_count}\n"
            f"stalled:         {stall}\n"
            f"failed_tests:\n"
            + "".join(f"  - {t}\n" for t in failed_tests)
            + "fns_retried:\n"
            + "".join(f"  - {r}::{f}\n" for r, f in fns_retried)
            + "\n=== cargo test output ===\n"
        )
        fp.write_text(header + test_out, encoding="utf-8")
    except Exception as e:
        log(f"  [phase-c/per-fn] failed to write test-fix log: {e}")
