"""Per-fn pass context for the phase-c fill engine (P2 de-closure of
`_process_one_stub` → `_fill_one_stub`)."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.domain.models import FillResult, Frontier, ImplBlock, Stub


@dataclass(frozen=True, slots=True)
class PerFnPassCtx:
    """Read-only per-pass context for the per-fn fill unit: everything the
    unit used to capture from the enclosing engine scope, made explicit.
    Mutable members (`retros_cache`, `splice_check_lock`, the record lists)
    are SHARED with the pass by design — same objects the closure shared."""
    project_dir: Path
    target_src_dir: Path
    module_name: str
    cluster_name: str
    check_cmd: str
    cluster_env: dict[str, str]
    fr_path: Path | None
    tests_dir_files: list[Path]
    macros_section: str
    invariants_section: str
    crate_type_records: list[Any]
    src_for_macros: Path
    failures_dir: Path
    tdd_frontier_tests: Frontier | None
    tdd_test_cmd: str | None
    splice_check_lock: threading.Lock
    retros_cache: dict[str, Any]
    idiom_blocks: list[ImplBlock]
    pass_n: int
    total: int


def fill_skip(rec: Stub, attempts: int) -> FillResult:
    """Early-exit result for the fill unit (skip/race/read-failure paths)."""
    return FillResult(file=rec.file, fn_name=rec.fn_name,
                      filled=False, attempts=attempts)
