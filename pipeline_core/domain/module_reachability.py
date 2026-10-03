"""Crate module-reachability — which files sit behind an externally PUBLIC
chain of `mod` declarations from the crate root.

A type's own `pub`/`pub(crate)` keyword is not enough to know whether it is
part of the crate's TRUE external API: `pub struct Own<T>` declared inside a
private `mod ptr;` is invisible to downstream crates regardless of its own
visibility — Rust visibility can never be widened past a `mod` that isn't
itself reachable. This module walks the `ModRecord` declaration graph (syn-
derived, no regex — Hard Rule) from the crate root, following ONLY "public"
mod edges, and reports the files still reachable at the end of that chain.

Pure domain: consumes already-enumerated ModRecords, no IO.
"""
from __future__ import annotations

import posixpath

from pipeline_core.domain.models import ModRecord

ROOT_BASENAMES = frozenset({"lib.rs", "main.rs"})


def resolve_child_path(declaring_file: str, mod_file: str) -> str:
    """The file a `mod <name>;` (or `#[path = "..."]`) declaration in
    `declaring_file` points at, per Rust's module-path convention: a crate-root
    or `mod.rs` file's submodules sit alongside it; any other file's
    submodules sit in a same-named subdirectory."""
    parent_dir = posixpath.dirname(declaring_file)
    base = posixpath.basename(declaring_file)
    if base in ROOT_BASENAMES or base == "mod.rs":
        anchor_dir = parent_dir
    else:
        stem = base[:-len(".rs")] if base.endswith(".rs") else base
        anchor_dir = posixpath.join(parent_dir, stem) if parent_dir else stem
    return posixpath.normpath(posixpath.join(anchor_dir, mod_file)) if anchor_dir else mod_file


def externally_reachable_files(
    mods: list[ModRecord], all_files: frozenset[str],
) -> frozenset[str]:
    """Every file in `all_files` reachable from the crate root by following
    ONLY "public" mod edges. Fails OPEN (marks a file reachable) whenever the
    declaration graph can't confidently place it — losing a real private-mod
    wall wrongly widens what t1.5 may rewrite (caught later by the missing-pin gate's
    own layout-sensitivity check), which is far cheaper than wrongly treating
    a genuinely public file as private-replaceable and corrupting the API."""
    root_files = {f for f in all_files if posixpath.basename(f) in ROOT_BASENAMES}
    if not root_files:
        # No recognizable crate root among the enumerated files — nothing to
        # anchor the walk on; degrade to "no restriction known" rather than
        # silently hiding every type behind a phantom wall.
        return frozenset(all_files)

    by_declared_in: dict[str, list[ModRecord]] = {}
    for m in mods:
        by_declared_in.setdefault(m.declared_in, []).append(m)

    seen: set[str] = set(root_files)
    stack = list(root_files)
    while stack:
        cur = stack.pop()
        for m in by_declared_in.get(cur, ()):
            if m.vis != "public":
                continue
            child = resolve_child_path(cur, m.file)
            if child in seen:
                continue
            seen.add(child)
            stack.append(child)
    return frozenset(seen)
