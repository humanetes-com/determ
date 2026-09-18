"""Pre-strip preservation of inline unit tests into `gen_tests_N` siblings.

Runs in the pre-strip window, immediately before the API-surface
capture and the skeleton bake, so every later phase sees ONE shape: production
code in the module file, tests in sibling files reached by a `#[cfg(test)] mod
gen_tests_N;` path-decl. A module that kept its tests inline would hand them to
the fill routes as ordinary file text — see `domain/inline_tests` for why that
is unrepairable once a reply lands on one.

Preservation is unconditional: it does not matter whether TASK 0c
augmented this module, because the hazard is the tests' LOCATION, not their
origin. The tests keep running — the sibling compiles into the same `--lib`
binary from the same module position.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pipeline_core.domain.inline_tests import (
    InlineTestMod,
    find_inline_test_mods,
    splice_out_inline_test_mod,
)
from pipeline_core.domain.rust_scan import rust_delimiters_balanced
from pipeline_core.domain.workspace_layout import SKIP_DIR_NAMES


@dataclass(frozen=True)
class PreserveInlineTestsDeps:
    log: Callable[[str], None]
    next_gen_tests_n: Callable[[Path], int]
    gen_tests_path: Callable[[Path, int], Path]
    build_gen_tests_decl: Callable[..., str]
    derive_gentest_prelude: Callable[[Path], tuple[str, str]]
    is_gen_tests_file: Callable[[str], bool]
    emit: Callable[..., None] = lambda *_a, **_k: None


@dataclass(frozen=True)
class PreserveInlineTestsResult:
    moved: int = 0
    refused: int = 0
    files: tuple[str, ...] = ()


def preserve_inline_tests(
    src_dir: Path, project_dir: Path, deps: PreserveInlineTestsDeps,
) -> PreserveInlineTestsResult:
    """Relocate every inline `#[cfg(test)] mod` under `src_dir` into a sibling.

    Refusals (a shape `domain/inline_tests` will not model, or a body whose
    delimiters do not balance) leave the block untouched and are LOGGED: a test
    silently left inline is exactly the state this step exists to make visible,
    so it must never fail quietly.
    """
    allow_line, _prelude = deps.derive_gentest_prelude(project_dir)
    moved = refused = 0
    touched: list[str] = []
    for rs in sorted(src_dir.rglob("*.rs")):
        if any(part in SKIP_DIR_NAMES for part in rs.parts):
            continue
        if deps.is_gen_tests_file(rs.name):
            continue
        try:
            text = rs.read_text(encoding="utf-8")
        except OSError as e:
            deps.log(f"  [inline-tests] {rs.name}: unreadable ({e}) — skipped.")
            continue
        blocks = find_inline_test_mods(text)
        if not blocks:
            continue
        rel = str(rs.relative_to(src_dir))
        n_moved, n_refused, text = _relocate_file(
            rs, rel, text, blocks, allow_line, deps)
        if n_moved:
            rs.write_text(text, encoding="utf-8")
            touched.append(rel)
        moved += n_moved
        refused += n_refused
    if moved or refused:
        deps.log(f"  [inline-tests] preserved {moved} inline test mod(s) into "
                 f"gen_tests siblings; {refused} refused.")
        deps.emit("task0c.inline_tests.preserved", moved=moved, refused=refused,
                  files=list(touched))
    return PreserveInlineTestsResult(moved, refused, tuple(touched))


def _relocate_file(
    src_file: Path, rel: str, text: str, blocks: tuple[InlineTestMod, ...],
    allow_line: str, deps: PreserveInlineTestsDeps,
) -> tuple[int, int, str]:
    """Sibling-write + in-place decl swap for one file's blocks.

    Numbers are assigned in SOURCE order but spliced in reverse: an earlier
    splice would shift every later block's offsets, and a reader expects
    `gen_tests_1` to be the file's first test mod, not its last.
    """
    plan: list[tuple[InlineTestMod, int]] = []
    refused = 0
    next_n = deps.next_gen_tests_n(src_file)
    for b in blocks:
        reason = b.refusal or (
            "" if rust_delimiters_balanced(b.body) else "unbalanced delimiters")
        if reason:
            refused += 1
            deps.log(f"  [inline-tests] {rel}::{b.name} left INLINE ({reason}) "
                     "— it stays inside the fill routes' writable surface.")
            continue
        plan.append((b, next_n))
        next_n += 1
    for b, n in reversed(plan):
        sibling = deps.gen_tests_path(src_file, n)
        sibling.write_text(b.body, encoding="utf-8")
        decl = deps.build_gen_tests_decl(
            sibling.name, n, allow_line, cluster_cfg_attr=b.extra_cfg)
        text = splice_out_inline_test_mod(text, b, decl.strip("\n") + "\n")
        deps.log(f"  [inline-tests] {rel}::{b.name} → {sibling.name}")
    return len(plan), refused, text
