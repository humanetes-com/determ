"""What a durable best-tree snapshot records about itself (pure domain).

`.best_tree/<cluster>/` is the tier `heal` restores a corrupted file from
before it falls through to excision and then to nuking the file back to the
baked skeleton. The slot is written whenever a sweep sets a new pass
high-water and it is written by REPLACEMENT — `save_cluster_best_tree`
rmtree's the directory before copying — so whatever decides that a sweep may
overwrite it is the only thing standing between a good stored tree and a worse
live one.

That decision was made against two variables held in process memory
(`_checkpoint_best`, `_checkpoint_sha`), seeded at `UNSET_PASS`/`None` at every
loop entry, while the snapshot they described outlived the process on disk. A
`None` sha reads to `snapshot_due` as "there is no snapshot yet", which is true
of the variables and false of the directory. So the FIRST sweep of every new
process overwrote the stored tree, however much worse it was.

Measured, 2026-08-07 on `anyhow`: run `241132-f9fae0a6` left
`.best_tree/stable` holding a tree that passed 81 of a 108-test oracle. Run
`259637-36db9aad` resumed it, its crash retrigger re-stubbed 56 bodies, and the
next sweep scored 21 — which the batch ratchet correctly WITHHELD from
charging, because a deliberate re-stub is not a regression. The durable slot
took the 21-passing tree anyway, then 23, then 26, while
`_tdd_floor_stable.json` still read `best_pass: 81`. Two durable records
describing one cluster, disagreeing, and the one heal reads held the worse
tree.

The mark is what makes the slot self-describing: the count it earned, the
iteration that earned it, the content hash of the tree it was copied from, and
the oracle that count was measured against. `tree_sha` is the field that fixes
the defect — with it, a new process can seed its comparison from disk instead
of from a sentinel that claims the disk is empty.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

UNKNOWN_TREE_SHA = "unknown"
"""The tree a snapshot came from, when the snapshot predates this record.

Slots written before the mark carried a `tree_sha` still hold a readable pass
count, and that count is the half worth honouring — it is what stops a worse
sweep from replacing them. Their sha is not recoverable (the tree they were
copied from has moved on), so it is spelled with a value that is never equal to
a content hash: `snapshot_due` then reads such a slot as "same count, different
tree", which re-snapshots rather than refusing — the safe direction, since the
live tree is the one this process can actually vouch for.
"""


@dataclass(frozen=True, slots=True)
class BestTreeMark:
    """A durable best-tree snapshot's own account of itself."""

    pass_count: int
    iteration: int
    tree_sha: str
    oracle_size: int

    def to_dict(self) -> dict[str, Any]:
        return {"pass_count": self.pass_count, "iteration": self.iteration,
                "tree_sha": self.tree_sha, "oracle_size": self.oracle_size}

    @staticmethod
    def from_dict(raw: Any) -> BestTreeMark | None:
        """Parse a persisted mark, or None when absent or malformed.

        Total by design, like `TddFloor.from_dict`: this runs at loop entry and
        a corrupt state file must cost a fresh comparison, never the run.

        `pass_count` is the only required field. A slot written before this
        record existed carries `pass_count` and `iteration` and nothing else,
        and refusing it whole would re-open the defect this module exists to
        close — the seed would be empty again and the next sweep would replace
        a tree it never compared itself against.
        """
        if not isinstance(raw, dict):
            return None
        try:
            pass_count = int(raw["pass_count"])
        except (KeyError, TypeError, ValueError):
            return None
        try:
            iteration = int(raw.get("iteration", 0))
        except (TypeError, ValueError):
            iteration = 0
        try:
            oracle_size = int(raw.get("oracle_size", 0))
        except (TypeError, ValueError):
            oracle_size = 0
        sha = raw.get("tree_sha")
        return BestTreeMark(
            pass_count=pass_count, iteration=iteration,
            tree_sha=str(sha) if isinstance(sha, str) and sha else UNKNOWN_TREE_SHA,
            oracle_size=oracle_size)

    def render(self) -> str:
        """How the mark is spelled in a log line."""
        oracle = f"/{self.oracle_size}" if self.oracle_size else ""
        return (f"{self.pass_count}{oracle} passing (iter {self.iteration}, "
                f"tree {self.tree_sha})")
