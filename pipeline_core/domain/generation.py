"""Value objects for the TASK-2 source generators (P4).

`Task2Config` carries the prompt constants/budgets the generators read (resolved
from `SETTINGS`/pipeline.py globals at the composition root). `GenerationRequest`
is the parameter object for `Task2Generator.generate` — the 15 call arguments
bundled into one named, immutable record so adding a context input is a
one-field change instead of a 5-signature edit.

Pure domain: stdlib only, no I/O.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
class Task1Config:
    """Prompt scaffolding constants + char budgets for the TASK-1 synthesizers
    (resolved from `SETTINGS`/pipeline.py globals at the composition root)."""
    model: str
    task1_model: str | None
    thinking_directive: str
    file_format_instruction: str
    single_call_char_limit: int
    synthesis_group_size: int
    readme_cap: int
    strategy_cap: int
    chunk_cache_dir: str
    parallelism: int = 1


@dataclass(frozen=True, slots=True)
class SkeletonConfig:
    """Runtime knobs for the 4-phase skeleton generator (resolved from
    `SETTINGS`/env at the composition root). `phase_c_per_fn`/`phase_c_tdd`
    are the two mode flags; the remaining ints are loop budgets."""
    task2_model: str | None
    parallelism: int
    phase_c_per_fn: bool
    phase_c_tdd: bool
    phase_c_max_passes: int = 3
    validate_iters: int = 3
    post_merge_max_stalls: int = 3
    post_merge_hard_cap: int = 10_000
    post_merge_test_timeout: int = 180
    recently_filled_max: int = 50


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
    existing_test_files: Any  # list[str] | set[str] — caller's collection, passed through
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


def parse_gen_tests_filename(name: str) -> tuple[str, int] | None:
    """Parse a `<stem>.gen_tests_<N>.rs` sibling filename → (stem, N), or None.
    Accepts an optional trailing `.broken` quarantine suffix (regex-free, R7).
    Equivalent to `^(.+?)\\.gen_tests_(\\d+)\\.rs(?:\\.broken)?$` — `partition`
    takes the first `.gen_tests_` to mirror the non-greedy stem capture."""
    rest = name[: -len(".broken")] if name.endswith(".broken") else name
    if not rest.endswith(".rs"):
        return None
    stem, sep, n_str = rest[:-3].partition(".gen_tests_")
    if not sep or not stem or not n_str.isdigit():
        return None
    return stem, int(n_str)


# --- gen_tests path-mod declaration toolkit (R7: structured, replaces the four
# `decl_re` regexes that did string surgery on the pipeline's OWN fixed format) ---
#
# The decl builder emits, for each augmentation sibling:
#
#     [#[cfg(<gate>)]]      ← optional cluster gate (nightly etc.)
#     #[cfg(test)]
#     [#[allow(...)] ...]   ← zero or more further attributes
#     #[path = "<stem>.gen_tests_<N>.rs"]
#     [pub ]mod gen_tests_<N>;
#
# Everything below is a pure structural line-scan anchored on the `mod` line.


@dataclass(frozen=True, slots=True)
class GenTestsDecl:
    """One `[pub ]mod gen_tests_<N>;` path-mod declaration block. `start`/`end` are
    the inclusive line-index span of the whole block — its contiguous leading
    `#[...]` attribute run through the `mod` line."""
    pass_n: int
    sibling_name: str | None   # value of `#[path = "..."]`, or None when absent
    gate: str                  # leading non-test `#[cfg(<gate>)]` predicate, or ""
    start: int
    end: int


def _mod_gen_tests_pass(stripped_line: str) -> int | None:
    """Pass N from a `[pub ]mod gen_tests_<N>;` line (already `.strip()`ed), else None."""
    body = stripped_line.removeprefix("pub ")
    if not (body.startswith("mod gen_tests_") and body.endswith(";")):
        return None
    n_str = body[len("mod gen_tests_"):-1].strip()
    return int(n_str) if n_str.isdigit() else None


def _attr_path_value(stripped_attr: str) -> str | None:
    """The quoted value of a `#[path = "..."]` attribute line, or None."""
    open_q = stripped_attr.find('"')
    if open_q < 0:
        return None
    close_q = stripped_attr.find('"', open_q + 1)
    return stripped_attr[open_q + 1:close_q] if close_q > open_q else None


def iter_gen_tests_decls(text: str) -> Iterator[GenTestsDecl]:
    """Yield every gen_tests path-mod decl block in `text`, scanning each
    `mod gen_tests_<N>;` anchor and walking back over its contiguous `#[...]`
    attribute run. Pure structural scan of the pipeline's own fixed format (R7)."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        pass_n = _mod_gen_tests_pass(line.strip())
        if pass_n is None:
            continue
        sibling_name: str | None = None
        gate = ""
        start = i
        j = i - 1
        while j >= 0 and lines[j].lstrip().startswith("#["):
            attr = lines[j].strip()
            if attr.startswith("#[path"):
                sibling_name = _attr_path_value(attr)
            elif attr.startswith("#[cfg(") and attr != "#[cfg(test)]":
                inner = attr[len("#[cfg("):].rsplit(")]", 1)[0]
                if inner != "test":
                    gate = inner
            start = j
            j -= 1
        yield GenTestsDecl(pass_n, sibling_name, gate, start, i)


def _is_gen_tests_sibling(name: str | None) -> bool:
    return name is not None and parse_gen_tests_filename(name) is not None


def _strip_decls(text: str, keep: Callable[[GenTestsDecl], bool]) -> str:
    """Drop every decl block for which `keep` is False; preserve the rest verbatim."""
    spans = [(d.start, d.end) for d in iter_gen_tests_decls(text) if not keep(d)]
    if not spans:
        return text
    lines = text.splitlines()
    for start, end in sorted(spans, reverse=True):  # bottom-up: indices stay valid
        del lines[start:end + 1]
    out = "\n".join(lines)
    return out + "\n" if text.endswith("\n") else out


def strip_gen_tests_decl(parent_text: str, sibling_name: str, pass_n: int) -> str:
    """Remove the one decl block whose `#[path]` is `sibling_name` and pass is
    `pass_n`; leave all others untouched. (#9: replaces the reattach-quarantine regex.)"""
    return _strip_decls(
        parent_text,
        keep=lambda d: not (d.pass_n == pass_n and d.sibling_name == sibling_name))


def strip_gen_tests_decl_n(parent_text: str, pass_n: int) -> str:
    """Remove the decl block for pass `pass_n` (any sibling)."""
    return _strip_decls(parent_text, keep=lambda d: d.pass_n != pass_n)


def strip_all_gen_tests_decls(text: str) -> str:
    """Remove every gen_tests path-mod decl block (path is a `*.gen_tests_<N>.rs`)."""
    return _strip_decls(text, keep=lambda d: not _is_gen_tests_sibling(d.sibling_name))


def gen_tests_decl_blocks(text: str) -> list[GenTestsDecl]:
    """Every gen_tests-sibling decl block, in source order — for cluster gating
    (each block's `gate` predicate + `sibling_name`)."""
    return [d for d in iter_gen_tests_decls(text) if _is_gen_tests_sibling(d.sibling_name)]


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
