"""Measurement provenance for a tdd iteration (F6, pure domain).

Pass counts were recorded with NO tree identity and NO test names, so a
session-to-session claim like "pass climbed 14→22" was unverifiable and the
oracle silently grew 61→68 between runs unflagged. A :class:`Measurement`
stamps every `phase_c.tdd.iter` event with the content hash of the fill tree it
measured, the oracle size (denominator), and the actual passing/failing test
names — the scientific control that makes cross-run comparison valid.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Measurement:
    """One tdd iteration's measured state, keyed to the exact tree it ran on."""

    cluster: str
    iteration: int
    tree_sha: str            # content hash over the cluster's fill-tree *.rs
    oracle_size: int         # tests that reported a verdict (the denominator)
    pass_count: int
    fail_count: int
    passed: frozenset[str]
    failed: frozenset[str]

    def to_event(self, *, kind: str, rc: int, frontier_size: int,
                 name_cap: int = 200) -> dict[str, object]:
        """The `phase_c.tdd.iter` payload — historical keys plus provenance
        (tree_sha, oracle_size, capped name lists)."""
        return {
            "cluster": self.cluster, "iter": self.iteration, "rc": rc,
            "kind": kind, "frontier_size": frontier_size,
            "pass_count": self.pass_count, "fail_count": self.fail_count,
            "tree_sha": self.tree_sha, "oracle_size": self.oracle_size,
            "passed": sorted(self.passed)[:name_cap],
            "failed": sorted(self.failed)[:name_cap],
        }


@dataclass(frozen=True, slots=True)
class PassDelta:
    """Whether two measurements' pass counts may be compared at all, and why."""

    comparable: bool
    verdict: str


def compare_measurements(prev: Measurement, cur: Measurement,
                         name_cap: int = 200) -> PassDelta:
    """Rule on a `prev.pass_count → cur.pass_count` claim.

    Stamping provenance is only half the control: a pass count rising while the
    DENOMINATOR moves is a measurement artefact, and reading it as progress is
    the failure this answers (14/oracle=69 → 35/oracle=111 on a tree whose
    content hash never changed — 21 of the 21 newly-passing names were tests
    that did not exist in the earlier suite).

    A delta is comparable only when both readings scored the SAME set of test
    names. `to_event` caps each name list at `name_cap`, so a reading that hit
    the cap degrades to the size check rather than pretending membership is
    known. Set size below `oracle_size` is NOT truncation — one test name runs in
    several binaries and counts once per verdict.
    """
    prev_names, cur_names = prev.passed | prev.failed, cur.passed | cur.failed
    truncated = any(len(names) >= name_cap for names in
                    (prev.passed, prev.failed, cur.passed, cur.failed))
    delta = cur.pass_count - prev.pass_count
    same_tree = prev.tree_sha == cur.tree_sha

    if truncated:
        if prev.oracle_size != cur.oracle_size:
            return PassDelta(False, (
                f"oracle size {prev.oracle_size}→{cur.oracle_size} and test names "
                f"are truncated — pass {prev.pass_count}→{cur.pass_count} is not "
                f"comparable"))
        return PassDelta(True, (
            f"oracle size held at {cur.oracle_size} (names truncated, membership "
            f"unverified) — pass delta {delta:+d}"))

    added, removed = cur_names - prev_names, prev_names - cur_names
    if added or removed:
        tree_note = (f"the fill tree never changed (sha {cur.tree_sha}), so the "
                     f"whole delta is measurement" if same_tree else
                     "the tree also changed, so progress cannot be separated "
                     "from the suite change")
        return PassDelta(False, (
            f"oracle membership moved (+{len(added)}/-{len(removed)} tests, size "
            f"{prev.oracle_size}→{cur.oracle_size}): pass {prev.pass_count}→"
            f"{cur.pass_count} is NOT comparable — {tree_note}"))

    if same_tree and delta != 0:
        return PassDelta(False, (
            f"same tree (sha {cur.tree_sha}) and same {cur.oracle_size}-test oracle "
            f"but pass moved {delta:+d} — the suite is flaky, not progressing"))

    return PassDelta(True, (
        f"same {cur.oracle_size}-test oracle, tree {prev.tree_sha}→{cur.tree_sha}: "
        f"pass delta {delta:+d} is real"))


def tree_content_sha(src_root: Path) -> str:
    """16-hex content hash over every `*.rs` under `src_root`, path-sorted so it
    is deterministic and identifies the exact tree a measurement ran on. Empty /
    missing tree → "0" * 16 (a stable sentinel, still comparable across runs)."""
    if not src_root.exists():
        return "0" * 16
    h = hashlib.sha256()
    for p in sorted(src_root.rglob("*.rs")):
        try:
            h.update(str(p.relative_to(src_root)).encode())
            h.update(b"\0")
            h.update(p.read_bytes())
            h.update(b"\0")
        except OSError:
            continue
    return h.hexdigest()[:16]
