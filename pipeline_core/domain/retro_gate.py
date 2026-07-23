"""Admission rules for persisted retrospectives — the one place that decides
whether a stored conviction (``fully_exhausted``) and its guidance still apply
to the attempt being prompted.

The store (``.phase_c_logs/retrospectives.json``) is keyed by (file, fn,
container) and survives runs, clusters, re-bakes and gate-semantics changes.
Two independent things can rot:

* the GUIDANCE text — it may name fields of a layout that no longer exists
  (judged here against the file being compiled, see
  :func:`judge_guidance`), and
* the CONVICTION — "this unit is hard" is only evidence if the attempts that
  earned it ran under the current layout, cluster, acceptance gate and fill
  routes (:class:`RetroProvenance`).

Both are read-side judgements: the shared store is never pruned, because the
same record can be true for the other cluster's layout.
"""
from __future__ import annotations

import hashlib
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .model_notes import LayoutJudgement, judge_note_against_layout, struct_fields
from .rust_idents import impl_self_type
from .rust_scan import struct_names

# Bumped whenever the ACCEPTANCE semantics change: a conviction says "every
# attempt failed the gate", which means nothing once the gate is a different
# gate. Current value marks the comparative joint-route verdict (a joint fill
# banks a strict improvement instead of being reverted for imperfection).
GATE_VERSION = "comparative_2026_07"
# Bumped whenever the set of fill ROUTES changes: a unit convicted when its
# file was skipped whole (over the per-file fn cap) never met the route that
# now partitions the file into test-welded groups.
ROUTE_VERSION = "test_welded_groups_2026_07"


class RetroGateVerdict(Enum):
    """What the sieve did with one record's guidance on one read."""
    NO_GUIDANCE = "no_guidance"
    HONOURED = "honoured"
    RETIRED = "retired"
    UNANCHORED = "unanchored"


def anchor_types_for_container(container: str) -> frozenset[str]:
    """The struct a record's field claims are about when its own prose names
    none: the self type of the impl (or type) the record is keyed on. Empty for
    free fns, which have no self type to anchor to."""
    name = impl_self_type(container) or ""
    return frozenset({name}) if name else frozenset()


def judge_guidance(
    guidance: str, file_text: str, *,
    anchor_types: frozenset[str] = frozenset(),
) -> RetroGateVerdict:
    """Sieve verdict for one stored guidance string against the current layout.

    ``UNANCHORED`` is the honest third outcome: the text ties to no struct
    declared in this file, so it is injected (nothing disproves it) but it was
    never actually checked — counting it separately is what makes "the sieve
    retires everything" and "the sieve retires nothing" distinguishable in a
    log instead of both looking like silence."""
    if not guidance:
        return RetroGateVerdict.NO_GUIDANCE
    judgement = judge_note_against_layout(
        guidance, file_text, anchor_types=anchor_types)
    if judgement is LayoutJudgement.CONTRADICTED:
        return RetroGateVerdict.RETIRED
    if judgement is LayoutJudgement.UNANCHORED:
        return RetroGateVerdict.UNANCHORED
    return RetroGateVerdict.HONOURED


@dataclass
class RetroGateTally:
    """Per-batch counts of sieve verdicts, rendered as ONE log line per fill
    batch. Counts, not events: the per-unit lines already exist for the
    interesting case (a retirement), what was missing is the denominator."""
    honoured: int = 0
    retired: int = 0
    unanchored: int = 0
    # Fill batches dispatch units to a thread pool; `+=` on an attribute is not
    # atomic, so the counters take the same treatment as every other shared
    # aggregate in the engines.
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False)

    def record(self, verdict: RetroGateVerdict) -> RetroGateVerdict:
        """Tally ``verdict`` (returned unchanged, so call sites can inline it)."""
        with self._lock:
            if verdict is RetroGateVerdict.HONOURED:
                self.honoured += 1
            elif verdict is RetroGateVerdict.RETIRED:
                self.retired += 1
            elif verdict is RetroGateVerdict.UNANCHORED:
                self.unanchored += 1
        return verdict

    @property
    def total(self) -> int:
        return self.honoured + self.retired + self.unanchored

    def render(self) -> str:
        return (f"retro-gate: honoured={self.honoured} retired={self.retired} "
                f"unanchored={self.unanchored}")


def layout_fingerprint(file_text: str) -> str:
    """Stable short digest of every struct layout declared in ``file_text``.

    Two texts share a fingerprint iff they declare the same structs with the
    same field sets — exactly the property a field-level conviction depends on.
    Bodies, fn signatures and comments are deliberately NOT in it: a conviction
    should survive its neighbours' fills and die only when the layout moves."""
    if not file_text:
        return ""
    parts: list[str] = []
    for name in sorted(set(struct_names(file_text))):
        fields = struct_fields(file_text, name)
        if fields is None:
            parts.append(f"{name}:-")
        else:
            parts.append(f"{name}:{','.join(sorted(fields))}")
    return hashlib.md5(";".join(parts).encode("utf-8")).hexdigest()[:8]


@dataclass(frozen=True, slots=True)
class RetroProvenance:
    """The context a retrospective record was earned in.

    Stamped on write, checked on read at the exhaustion gates. A record with a
    missing stamp is legacy — it was earned in an UNKNOWN context, which is not
    the same as "the current one", so it cannot convict either."""
    cluster: str
    layout_fp: str
    gate_ver: str = GATE_VERSION
    route_ver: str = ROUTE_VERSION

    @classmethod
    def current(cls, cluster: str, file_text: str) -> RetroProvenance:
        return cls(cluster=cluster, layout_fp=layout_fingerprint(file_text))

    def stamp(self, record: dict[str, Any]) -> dict[str, Any]:
        """Add this provenance to a record about to be persisted (in place)."""
        record["cluster"] = self.cluster
        record["layout_fp"] = self.layout_fp
        record["gate_ver"] = self.gate_ver
        record["route_ver"] = self.route_ver
        return record

    def matches(self, record: Mapping[str, Any] | None) -> bool:
        """True iff ``record`` carries every stamp and all of them are ours."""
        if not record:
            return False
        return all(record.get(k) == v for k, v in (
            ("cluster", self.cluster), ("layout_fp", self.layout_fp),
            ("gate_ver", self.gate_ver), ("route_ver", self.route_ver)))


def conviction_is_current(
    record: Mapping[str, Any] | None, provenance: RetroProvenance,
) -> bool:
    """Whether ``record``'s ``fully_exhausted`` verdict may be honoured here.

    A conviction is proof of hardness only for the (cluster, layout, gate,
    routes) it was earned under. Cross-cluster reuse in particular convicted
    one cluster's unit on the other cluster's private layout — the duplicate
    attempts this costs are cheap next to a permanently skipped unit."""
    return provenance.matches(record)
