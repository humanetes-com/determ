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

from .model_notes import (
    LayoutJudgement,
    judge_note_against_layout,
    layout_safe_guidance,
    struct_fields,
)
from .rust_idents import impl_self_type
from .rust_scan import contains_word, struct_names, stubbed_fn_names

# Bumped whenever the ACCEPTANCE semantics change: a conviction says "every
# attempt failed the gate", which means nothing once the gate is a different
# gate. Current value marks the comparative joint-route verdict (a joint fill
# banks a strict improvement instead of being reverted for imperfection).
GATE_VERSION = "comparative_2026_07"
# Bumped whenever the set of fill ROUTES changes: a unit convicted when its
# file was skipped whole (over the per-file fn cap) never met the route that
# now partitions the file into test-welded groups.
# `leakproof_revert`: before this, a refused per-fn revert silently left the
# failed body in the tree — sibling units then exhausted against a foreign
# compile error and were convicted on evidence that was never about them.
# Convictions earned under that engine are not a fair sample of any unit.
ROUTE_VERSION = "leakproof_revert_2026_07"


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
    # Counted too, so an empty retro store (fresh --clean run) renders as
    # "no_guidance=N" rather than as no line at all — otherwise "the sieve had
    # nothing to judge" is indistinguishable from "the gate never ran".
    no_guidance: int = 0
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
            elif verdict is RetroGateVerdict.NO_GUIDANCE:
                self.no_guidance += 1
        return verdict

    @property
    def judged(self) -> int:
        """Verdicts where guidance actually existed and was sieved."""
        return self.honoured + self.retired + self.unanchored

    @property
    def total(self) -> int:
        return self.judged + self.no_guidance

    def render(self) -> str:
        return (f"retro-gate: honoured={self.honoured} retired={self.retired} "
                f"unanchored={self.unanchored} no_guidance={self.no_guidance}")


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


@dataclass(frozen=True, slots=True)
class ConvictionEvidence:
    """The CONVICTING tests an exhaustion verdict was drawn from.

    Stamped on write next to the provenance, re-checked on read. A conviction
    asserts "every attempt left these tests red"; the moment a test is no longer
    one this unit answers for — attribution narrowed to direct evidence, the test
    left the oracle — the verdict rests on a failure that was never the unit's,
    so it may not skip an attempt. Context alone cannot catch this: the cluster,
    layout, gate and route set can all be unchanged while the blame map moves.

    A verdict drawn from compile errors alone names no tests, so an empty set is
    supported by every later set and such convictions stand undisturbed."""

    tests: frozenset[str]

    def stamp(self, record: dict[str, Any]) -> dict[str, Any]:
        """Record the evidence on a record about to be persisted (in place)."""
        record["convicted_by"] = sorted(self.tests)
        return record

    def still_supports(self, record: Mapping[str, Any] | None) -> bool:
        """True iff every test the verdict was drawn from may still convict."""
        if not record:
            return False
        stored = record.get("convicted_by")
        if not isinstance(stored, list):
            # No evidence stamp: earned under an unknown blame map, which is not
            # the same as "the current one" — the same rule as a missing
            # provenance stamp, and it retires the pre-stamp records by itself.
            return False
        return frozenset(str(t) for t in stored) <= self.tests


@dataclass(frozen=True, slots=True)
class BlockedOnStubs:
    """The hollow siblings a conviction's own failed attempts ran against.

    The third way a conviction rots, and the one neither :class:`RetroProvenance`
    nor :class:`ConvictionEvidence` can see. Both of those ask whether the
    verdict was earned in a comparable WORLD; this asks whether it was earned
    against a comparable TREE. A unit whose every attempt died because the fns
    it calls were `unimplemented!()` was never given a fair attempt at all, and
    nothing about the cluster, the layout fingerprint, the gate or the blame map
    moves when those fns fill.

    Measured on `anyhow` cluster `stable`, 2026-08-07: `src/error.rs::new` was
    convicted GENUINELY_HARD, and the stored guidance is the conviction arguing
    against itself — *"The vtable functions referenced in the body
    (`object_drop`, `object_ref`, `object_boxed`, `object_downcast`,
    `object_drop_front`, etc.) are not implemented in the crate — they panic
    with 'not implemented' ... this cannot be fixed by body-only changes."* By
    the time it mattered every fn that sentence names was filled on disk
    (`construct`, `object_drop`, `object_drop_front`, `object_ref`,
    `object_boxed`, `object_downcast`, `object_reallocate_boxed`,
    `context_drop_rest`, `context_chain_drop_rest`), and the record still
    skipped the unit on every dispatch. **A conviction is a claim about a
    moment; this is the moment, written down.**

    Recorded from the FAILED BODY's own callees rather than from the file's
    whole stub set: a file mid-reconstruction is mostly holes, so "any stub in
    this file filled" would retire every conviction almost every iteration and
    buy the retries with activity instead of evidence — the same defect
    `readmission_warranted` documents from the other direction.

    Retirement is self-limiting and needs no attempt cap. Each release is bought
    by a DISTINCT blocker filling, the caller narrows the stored set to those
    still hollow, and a stub set only shrinks — so a record can be released at
    most once per fn it recorded, and the last release leaves nothing to trigger
    another."""

    stubs: frozenset[str]

    @classmethod
    def at_conviction(
        cls, attempted_body: str, file_text: str, *, exclude: str = "",
    ) -> BlockedOnStubs:
        """The fns `attempted_body` names that are still hollow in `file_text`.

        `exclude` drops the convicted fn itself: its own body is a stub while
        the attempt that failed is being judged, and a record that lists itself
        would be released by its own eventual fill."""
        return cls(frozenset(
            name for name in stubbed_fn_names(file_text)
            if name != exclude and contains_word(attempted_body, name)))

    def stamp(self, record: dict[str, Any]) -> dict[str, Any]:
        """Record the blockers on a record about to be persisted (in place)."""
        record["blocked_on"] = sorted(self.stubs)
        return record


def cleared_blockers(
    record: Mapping[str, Any] | None, still_stubbed: frozenset[str],
) -> frozenset[str]:
    """The blockers a conviction recorded that are no longer hollow.

    Empty for a record with no `blocked_on` stamp — a conviction earned before
    the stamp existed, or one drawn from compile errors that named no sibling,
    asserts nothing about the tree and is left to the provenance and evidence
    gates. Silence is not a release."""
    if not record:
        return frozenset()
    stored = record.get("blocked_on")
    if not isinstance(stored, list):
        return frozenset()
    return frozenset(str(s) for s in stored) - still_stubbed


# Set on a record whose lesson was written from a test outcome that hollow
# siblings, not the attempted body, decided. `blocked_on` alone cannot carry
# this: the caller narrows that set as blockers fill, so it empties out and the
# text would become admissible again on the strength of the blockers having been
# fixed — which is precisely when the false premise is provable, not forgivable.
GUIDANCE_FROM_HOLLOW_TREE = "guidance_from_hollow_tree"


class ConvictionStanding(Enum):
    """Why a stored ``fully_exhausted`` verdict does or does not apply here."""
    CURRENT = "current"
    STALE_CONTEXT = "stale_context"     # cluster / layout / gate / routes moved
    STALE_EVIDENCE = "stale_evidence"   # the tests it was drawn from moved


def conviction_standing(
    record: Mapping[str, Any] | None,
    provenance: RetroProvenance,
    evidence: ConvictionEvidence,
) -> ConvictionStanding:
    """Whether ``record``'s ``fully_exhausted`` verdict may be honoured here.

    A conviction is proof of hardness only for the (cluster, layout, gate,
    routes) it was earned under AND only for the failures it was drawn from.
    Cross-cluster reuse in particular convicted one cluster's unit on the other
    cluster's private layout — the duplicate attempts a wrong release costs are
    cheap next to a permanently skipped unit."""
    if not provenance.matches(record):
        return ConvictionStanding.STALE_CONTEXT
    if not evidence.still_supports(record):
        return ConvictionStanding.STALE_EVIDENCE
    return ConvictionStanding.CURRENT


def admissible_guidance(
    record: Mapping[str, Any] | None, file_text: str, *,
    evidence: ConvictionEvidence,
    anchor_types: frozenset[str] = frozenset(),
) -> str:
    """The stored lesson a prompt may carry, or ``""``.

    Two independent ways a stored lesson goes bad, judged in ONE place so the
    injection sites cannot drift apart on the rule:

    - it contradicts the layout being compiled — it names a field this struct
      does not have, and steers every attempt into the same E0609/E0560;
    - it was drawn from failures this unit no longer answers for. A record
      carrying compile errors learned from the COMPILER, which no change to the
      blame map can invalidate; one that COMPILED and still failed learned from
      test outcomes alone, so a narrowed frontier voids the lesson exactly as it
      voids the verdict. Re-admitting a unit while still handing it the lesson a
      foreign red test taught just spends the fresh attempts on the same wrong
      target;
    - it was written from a test run the tree's own holes decided. A body that
      compiled and then aborted the test binary, while the fns it calls were
      still `unimplemented!()`, produces a retrospective explaining an abort the
      stubs caused. On cluster `stable` (2026-08-09) all four of `error.rs`'s
      Error constructors earned exactly this record — `error_count` 0, one or
      two failing tests, `blocked_on` naming `construct`, `no_backtrace`,
      `error` or `object_drop_front` — and the resulting text told the model to
      write `object_drop_rest::<E>`, which is an `ErrorVTable` FIELD and not a
      fn the crate declares. Three iterations and six hours held pass=13 with
      every constructor stubbed. The same run's one compiler-taught record
      (`context::Error`, `error_count` 2) is a real lesson and stays."""
    rec = record or {}
    guidance = str(rec.get("guidance") or "")
    if not guidance:
        return ""
    if rec.get(GUIDANCE_FROM_HOLLOW_TREE):
        return ""
    if int(rec.get("error_count") or 0) == 0 and not evidence.still_supports(rec):
        return ""
    return layout_safe_guidance(guidance, file_text, anchor_types=anchor_types)


def stamp_conviction(
    record: dict[str, Any],
    provenance: RetroProvenance,
    evidence: ConvictionEvidence,
    blockers: BlockedOnStubs | None = None,
) -> dict[str, Any]:
    """Every half a later reader needs: the CONTEXT the verdict was earned in
    (equality-checked), the EVIDENCE it rests on (subset-checked) and the hollow
    siblings it ran against (`cleared_blockers`, difference-checked).

    `blockers` is optional so a caller with no file snapshot in hand still
    writes a well-formed record; such a record simply carries no tree claim and
    is never released by `cleared_blockers`."""
    stamped = evidence.stamp(provenance.stamp(record))
    if blockers is None:
        return stamped
    stamped[GUIDANCE_FROM_HOLLOW_TREE] = bool(
        blockers.stubs) and int(stamped.get("error_count") or 0) == 0
    return blockers.stamp(stamped)
