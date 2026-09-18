"""RunContext — the per-module loop-invariant data the reconstruction
use-cases read. A plain value object (not behaviour): resolved once when a module
is selected and passed to every phase, so no phase reaches for a loop local."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class RunContext:
    project_dir: Path
    module_name: str
    module_dir: Path
    crate_type: str
    module_manifest: Path
    module_dir_rel: str
    module_test_cmd: str
    module_build_check: str | None
    module_has_tests: bool
    module_req_path: Path
    genesis_path: Path
    failure_log_path: Path
    is_multi_module: bool
    src_hash: str | None
    workspace_deps: set[str]
    locked_modules: set[str]          # shared mutable set — lock_or_complete records into it
    workspace_structure: str
    project_profile: str
    project_context: str
    readme_text: str
    test_files: list[tuple[str, str]]
    existing_test_files: set[str]
    clusters: Any                     # list[BuildCluster]
    saved_gen_blocks: dict[str, list[tuple[int, str]]]
