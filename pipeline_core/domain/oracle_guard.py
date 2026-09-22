"""Whether the oracle a workspace is scored by is still the one strip sealed.

The oracle is the crate's `tests/`, the gen_tests siblings and the baked
behaviour suite (`adapters/oracle_fingerprint.py` names the set). After strip
nothing in the pipeline writes it: across the four days and nine launches
from 2026-09-18 to 2026-09-22 on the anyhow workspace, the only commit after
`pipeline: stripped` that touched an oracle path was one that swept in an
edit made OUTSIDE the pipeline. A Claude session copied a cluster tree with
`cp -r`, which copied the tree's `tests` symlink as a symlink, and its
diagnostic edit to the "copy" of `tests/test_context.rs` (a panic hook, probe
prints, a split `unwrap`) landed in the oracle at 2026-09-21 18:50:03Z. The
next launch's task0c checkpoint committed it with the best tree and the
floor. The only trace was a new fingerprint on the ledger line: `1c3b0664`
became `580d469e`, every route-ledger row became foreign, every clock keyed on
the oracle restarted at zero, and run `92187-f71f12bf` re-bought pairs the old
record had already graded flat five times.

So the check reads the SEALED copy, not the writer: whatever wrote the file —
a session, a script, a tool the model drives — the comparison against the seal
catches it. Text is whitespace-folded exactly as the fingerprint folds it
(`canon_body`), so a change here is a change the fingerprint also sees and a
reformat the fingerprint forgives is not drift.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pipeline_core.domain.body_ledger import canon_body


@dataclass(frozen=True, slots=True)
class OracleDrift:
    """The oracle paths that differ from the sealed copy, by kind."""

    seal: str                         # what the baseline is (a tag name)
    changed: tuple[str, ...] = ()
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return not (self.changed or self.added or self.removed)

    def paths(self) -> tuple[str, ...]:
        return tuple(sorted({*self.changed, *self.added, *self.removed}))

    def summary(self) -> str:
        parts = []
        for label, items in (("changed", self.changed), ("added", self.added),
                             ("removed", self.removed)):
            if items:
                shown = ", ".join(items[:6])
                more = f" (+{len(items) - 6} more)" if len(items) > 6 else ""
                parts.append(f"{label}: {shown}{more}")
        return "; ".join(parts) if parts else "no drift"


def oracle_drift(current: Mapping[str, str], sealed: Mapping[str, str],
                 *, seal: str) -> OracleDrift:
    """Compare the oracle as it is on disk (`current`, rel path -> text) with
    the sealed copy (`sealed`, same keys). A path in one and not the other is
    added or removed; a path in both whose folded text differs is changed."""
    changed = tuple(sorted(
        p for p in current.keys() & sealed.keys()
        if canon_body(current[p]) != canon_body(sealed[p])))
    added = tuple(sorted(current.keys() - sealed.keys()))
    removed = tuple(sorted(sealed.keys() - current.keys()))
    return OracleDrift(seal=seal, changed=changed, added=added, removed=removed)
