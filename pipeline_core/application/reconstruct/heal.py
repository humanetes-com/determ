"""Heal-hierarchy use-case: recover an unparseable fill-tree file with the
LEAST destructive option that yields a parseable result.

The old recovery jumped straight to copying the baked skeleton over the file,
discarding every accumulated fill (22→14 in the incident). This tries, in order:
surgical excision of doc-comment-intruding code → this cluster's best-tree
snapshot → the newest workspace-git version that parses → and only then the
baked skeleton (loud loss event). Each candidate is committed through the same
parse-gated write as the splice gate, so a candidate that doesn't parse is never left on disk.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pipeline_core.application.reconstruct.splice_txn import (
    ParseValidator,
    parse_guarded_write,
)
from pipeline_core.domain.heal import (
    HealOutcome,
    RestorePoint,
    apply_excision,
    count_unimplemented,
)


@dataclass(frozen=True, slots=True)
class HealDeps:
    """Collaborators for :func:`heal_file`, injected by the composition root."""

    parse_ok: ParseValidator                         # StructureReader.parse_ok (syn)
    best_tree_bytes: Callable[[str], bytes | None]   # rel -> best-tree snapshot bytes
    git_ancestor_blobs: Callable[[str], list[bytes]]  # rel -> prior versions, newest first
    skeleton_bytes: Callable[[str], bytes | None]    # rel -> baked-skeleton bytes
    log: Callable[[str], None]
    emit: Callable[..., None]


def _stubs(b: bytes) -> int:
    return count_unimplemented(b.decode("utf-8", "replace"))


def heal_file(filled_dir: Path, rel: str, cluster: str,
              deps: HealDeps) -> HealOutcome:
    """Restore `filled_dir/rel` to a parseable state, trying restore points in
    increasing destructiveness. Returns the :class:`HealOutcome` (which point
    committed + fill loss). A pure surgical excision keeps every fill; the
    skeleton fallback emits `phase_c.heal.skeleton_fallback` with the stub-count
    delta so any real loss is LOUD."""
    path = filled_dir / rel
    try:
        current = path.read_bytes()
    except OSError:
        return HealOutcome(RestorePoint.NONE)
    stubs_before = _stubs(current)

    # 1. SURGICAL_EXCISION — delete only the code lines intruding a doc-comment.
    excised_text, n_excised = apply_excision(current.decode("utf-8", "replace"))
    if n_excised:
        cand = excised_text.encode("utf-8")
        if parse_guarded_write(path, cand, current, deps.parse_ok):
            deps.log(f"  [heal] {rel}: SURGICAL_EXCISION removed {n_excised} "
                     "doc-comment-intruding line(s) — all fills kept.")
            return HealOutcome(RestorePoint.SURGICAL_EXCISION, stubs_before,
                               _stubs(cand), n_excised)

    # 2. BEST_TREE_COPY — this cluster's best ratcheted snapshot of the file.
    bt = deps.best_tree_bytes(rel)
    if bt is not None and parse_guarded_write(path, bt, current, deps.parse_ok):
        deps.log(f"  [heal] {rel}: restored from best-tree snapshot.")
        return HealOutcome(RestorePoint.BEST_TREE_COPY, stubs_before, _stubs(bt))

    # 3. GIT_ANCESTOR — newest committed workspace version that parses.
    for blob in deps.git_ancestor_blobs(rel):
        if parse_guarded_write(path, blob, current, deps.parse_ok):
            deps.log(f"  [heal] {rel}: restored from a git ancestor that parses.")
            return HealOutcome(RestorePoint.GIT_ANCESTOR, stubs_before, _stubs(blob))

    # 4. BAKED_SKELETON — last resort; loses every fill in the file. LOUD.
    sk = deps.skeleton_bytes(rel)
    if sk is not None:
        path.write_bytes(sk)   # skeleton is known-parseable by construction
        after = _stubs(sk)
        deps.emit("phase_c.heal.skeleton_fallback", data={
            "cluster": cluster, "file": rel,
            "stubs_before": stubs_before, "stubs_after": after,
            "fills_lost": max(0, after - stubs_before)})
        deps.log(f"  [heal] {rel}: NO gentler restore worked — restored whole "
                 f"file from skeleton ({max(0, after - stubs_before)} fill(s) "
                 "lost, will be refilled).")
        return HealOutcome(RestorePoint.BAKED_SKELETON, stubs_before, after)

    return HealOutcome(RestorePoint.NONE, stubs_before, stubs_before)
