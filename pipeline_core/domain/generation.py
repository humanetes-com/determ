"""Value objects for the TASK-2 source generators (P4).

`Task2Config` carries the prompt constants/budgets the generators read (resolved
from `SETTINGS`/pipeline.py globals at the composition root). `GenerationRequest`
is the parameter object for `Task2Generator.generate` — the 15 call arguments
bundled into one named, immutable record so adding a context input is a
one-field change instead of a 5-signature edit.

Pure domain: stdlib only, no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Task2Config:
    """Prompt scaffolding constants + char budgets for the generators."""
    file_format_instruction: str
    task2_model: str | None
    thinking_directive: str
    verbosity_guard: str
    genesis_cap: int
    strategy_cap: int
    single_call_char_limit: int
    model_label: str = ""


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    """One TASK-2 generation request — the inputs every generator's `generate`
    consumes. `existing_test_files` is the cluster's read-only test spec (a list
    or set of relative paths); the remaining strings are optional prompt
    context, empty when not applicable."""
    project_dir: Path
    genesis_path: Path
    functional_req_path: Path
    manifest_path: Path
    existing_test_files: object  # list[str] | set[str] — caller's collection, passed through
    project_profile: str = ""
    workspace_structure: str = ""
    build_error: str = ""
    locked_dep_context: str = ""
    current_module: str = ""
    module_dir_rel: str = ""
    whole_project_context: str = ""
    coverage_strategy: str = ""
    api_surface: str = ""
    prev_source_context: str = ""


def strip_test_prefix(stem: str) -> str:
    """Drop a leading `test_`/`tests_`/`it_` prefix from a test file stem
    (regex-free, R7). Equivalent to `re.sub(r'^(test_|tests_|it_)', '', stem)` —
    `test_` cannot match a `tests_` stem (5th char differs), so first-match
    order is preserved."""
    for p in ("test_", "tests_", "it_"):
        if stem.startswith(p):
            return stem[len(p):]
    return stem


def testfile_paths(text: str) -> list[str]:
    """Every `path` in `<TESTFILE path="...">` markers within `text`, in order
    (regex-free, R7). Equivalent to `re.findall(r'<TESTFILE path="([^"]+)"', text)`
    — empty paths are skipped (the `+` quantifier required ≥1 char)."""
    out: list[str] = []
    marker = '<TESTFILE path="'
    i = 0
    while True:
        j = text.find(marker, i)
        if j < 0:
            break
        start = j + len(marker)
        k = text.find('"', start)
        if k < 0:
            break
        if k > start:  # non-empty path only (matches the regex's `+`)
            out.append(text[start:k])
        i = k + 1
    return out


def topo_sort_files(file_paths: list[str], known_content: dict[str, str]) -> list[str]:
    """FR-010: topological order of `file_paths` by `mod` declarations parsed
    from `known_content` — a file that declares `mod foo;` must be generated
    before `foo.rs`. Pure: uses stdlib `graphlib` + the domain mod-decl scan."""
    from graphlib import TopologicalSorter

    from .rust_scan import mod_decls
    path_set = set(file_paths)
    stems = {Path(p).stem: p for p in file_paths}
    deps: dict[str, set[str]] = {p: set() for p in file_paths}
    for path, content in known_content.items():
        for mod_name in mod_decls(content):
            sub = stems.get(mod_name)
            if sub and sub != path and sub in path_set:
                deps[sub].add(path)  # sub must come after path (path declares it)
    ts: TopologicalSorter[str] = TopologicalSorter(deps)
    return list(ts.static_order())
