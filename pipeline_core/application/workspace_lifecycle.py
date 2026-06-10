"""Module-start workspace lifecycle (slice 9): save siblings → (maybe) wipe → stub.

Codifies THE invariant that broke in session 59 (the wipe-vs-resume bug,
commit 9e182bb): a startup wipe of regenerated sources is allowed ⟺ the
phase that REGENERATES the wiped artefact will rerun. Wiping while the
regenerating phase's done-marker stands leaves later phases running against
a stub tree (the t2c→t3 fall-through). :func:`may_wipe_regenerated` is that
invariant as code; :func:`wipe_module_sources` refuses to wipe unless it
holds, so a call site cannot wipe without stating the phase guard.

Bodies moved verbatim from pipeline.py's module loop. Filename regex stays
in pipeline.py behind the injected ``is_gen_tests_file`` predicate (Group-E
convention); logging goes through the ambient log facade.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from pathlib import Path

from pipeline_core.log import log


def may_wipe_regenerated(*, regenerating_phase_done: bool) -> bool:
    """The wipe⟺rerun invariant: wiping an artefact is safe only when the
    phase that regenerates it will rerun (its done-marker is NOT set)."""
    return not regenerating_phase_done


def save_gen_tests_siblings(
    module_dir: Path,
    project_dir: Path,
    existing_test_files: Collection[str],
    *,
    gen_tests_siblings: Callable[[Path], list[tuple[int, Path]]],
    is_gen_tests_file: Callable[[str], bool],
) -> dict[str, list[tuple[int, str]]]:
    """Save gen_tests siblings + their declarations BEFORE wiping.

    TASK 0c writes test code to foo.gen_tests_N.rs siblings; TASK 2
    regenerates source files. The caller reattaches siblings + decls after
    each TASK 2 write so TASK 3 can actually run the tests.

    Returns {rel_path of parent → list of (pass_n, sibling_body)}.
    """
    saved_gen_blocks: dict[str, list[tuple[int, str]]] = {}
    if module_dir.exists():
        for src_file in sorted(module_dir.rglob("*.rs")):
            rel = str(src_file.relative_to(project_dir))
            if rel in existing_test_files:
                continue
            if is_gen_tests_file(src_file.name):
                continue  # siblings handled via their parent
            try:
                siblings = gen_tests_siblings(src_file)
                if siblings:
                    saved_gen_blocks[rel] = [
                        (n, p.read_text(encoding="utf-8"))
                        for n, p in siblings
                    ]
            except Exception:
                pass
    if saved_gen_blocks:
        _total = sum(len(v) for v in saved_gen_blocks.values())
        log(f"  Saved {_total} gen_tests sibling(s) from "
            f"{len(saved_gen_blocks)} file(s) for reattachment.")
    return saved_gen_blocks


# Never wiped: cargo's build output (contains dependency build-script outputs
# like target/debug/build/thiserror-xxx/out/private.rs that cargo expects on
# subsequent compiles — deleting these breaks dependency compilation in
# mysterious ways). Dot-dirs (.skeleton/, .skeleton-filled/, .task1_partials/,
# .cluster_augmented/, …) are skipped by the component check below.
_WIPE_SKIP_DIRS = frozenset({"target"})


def wipe_module_sources(
    module_dir: Path,
    project_dir: Path,
    existing_test_files: Collection[str],
    *,
    regenerating_phase_done: bool,
    write_stub: Callable[[], object],
) -> list[str]:
    """Wipe previously generated files for a module (interrupted run) — ONLY
    when :func:`may_wipe_regenerated` holds for the regenerating phase
    (task2b merge). When task2b will rerun it regenerates src/ from
    .skeleton-filled/, so wiping is safe; when task2b IS done, src/ is the
    resumable product and t2c's fills live only there.

    Only regenerated source files are wiped — never scratch/cache dot-dirs
    or anything cargo manages. After a wipe pass, ``write_stub`` is invoked
    so the workspace still parses while regenerating.

    Returns the wiped rel-paths ([] when the invariant forbade the wipe).
    """
    if not module_dir.exists() or not may_wipe_regenerated(
            regenerating_phase_done=regenerating_phase_done):
        return []
    wiped: list[str] = []
    for src_file in sorted(module_dir.rglob("*.rs")):
        rel = str(src_file.relative_to(project_dir))
        if rel in existing_test_files:
            continue
        # Skip scratch dirs (any component starting with '.') and
        # cargo's target/ tree.
        parts = src_file.parts
        if any(p.startswith(".") for p in parts):
            continue
        if any(p in _WIPE_SKIP_DIRS for p in parts):
            continue
        src_file.unlink()
        wiped.append(rel)
    if wiped:
        log(f"  Wiped {len(wiped)} leftover generated file(s): {', '.join(wiped)}")
    # Stub so workspace parses while we generate.
    write_stub()
    return wiped
