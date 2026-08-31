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
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, NamedTuple

from .coverage_slice import unreached_by
from .model_notes import (
    LayoutJudgement,
    judge_note_against_layout,
    layout_safe_guidance,
    struct_fields,
)
from .models import FnLocator
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

    A verdict drawn from compile errors alone names no tests. It stands while
    the blame map still names none — and dies the moment a failing test blames
    the unit, because that is a different question from the one it answered.
    "I could not make this compile" says nothing about a body that now compiles
    and is wrong; honouring it there skips the unit on every dispatch, forever.
    Measured on `anyhow`/`stable`, run `1246060-2339497b` (2026-08-21):
    `src/context.rs::*impl*::StdErrorforContextError<C,Error>` carried
    `fully_exhausted: true, convicted_by: []` — stamped on a nesting
    diagnostic, a reply carrying its own `fn source(&self)` line spliced as a
    body — while nine failing tests blamed its `None` body; `frozenset() <=
    tests` was True in every context, the impl route logged `skipped (prior
    retro marked exhausted)` at both dispatches, and none of the four release
    paths could open on a score only this unit could move. The same run retired
    the `Error`, `ErrorImpl` and `IteratorforChain` records seven times as
    `stale_evidence` — the units convicted ON tests were retried; the one
    convicted on none never was."""

    tests: frozenset[str]

    def stamp(self, record: dict[str, Any]) -> dict[str, Any]:
        """Record the evidence on a record about to be persisted (in place)."""
        record["convicted_by"] = sorted(self.tests)
        return record

    def still_supports(self, record: Mapping[str, Any] | None) -> bool:
        """True iff the verdict was drawn from the blame map it is read under:
        every test it names may still convict, and a verdict that named none is
        read where none does.

        What "the blame map" is depends on what the record learned from,
        and the call sites choose (`evidence_for_record`). A record earned
        from TEST outcomes (`error_count` 0) is stamped with, and read
        under, the tests the unit ANSWERS FOR — DIRECT and ESCALATED, the
        dispatch projection — not the convicting subset alone: the live
        `src/ensure.rs::*impl*::BothDebugfor(A,B)` record carried
        `error_count 0, failing_test_count 13, convicted_by []` while
        `test_trailer` sat on the unit through an escalation, and under the
        convicting-only reading `[] <= {}` held, so the `[]` supported the
        record on a unit that answered for thirteen red tests. Under this
        reading `convicted_by: []` on such a record supports NOTHING once the
        block has any attributed test, escalated included — `not self.tests`
        is False the moment the dispatch projection names one."""
        if not record:
            return False
        stored = record.get("convicted_by")
        if not isinstance(stored, list):
            # No evidence stamp: earned under an unknown blame map, which is not
            # the same as "the current one" — the same rule as a missing
            # provenance stamp, and it retires the pre-stamp records by itself.
            return False
        if not stored:
            # Convicted by no test — a compile-only verdict. It is evidence only
            # while no test blames the unit; a frontier that names one asks a
            # question those attempts never answered.
            return not self.tests
        return frozenset(str(t) for t in stored) <= self.tests



def evidence_for_record(
    record: Mapping[str, Any] | None, *,
    convicting: ConvictionEvidence, answers: ConvictionEvidence,
) -> ConvictionEvidence:
    """The evidence a stored record is READ under — the same choice
    `stamp_conviction`'s callers make on write, so a record is judged by
    the blame map it was stamped with.

    A record that COMPILED (`error_count` 0) learned from test outcomes
    alone, and the tests it is answerable to are every test the unit
    answers for (`answers`: DIRECT ∪ ESCALATED). A record earned from
    compile errors is a verdict on the compiler's question; it is stamped
    with the convicting tests of the moment (`convicting`) and read under
    them, exactly as before. A missing record reads as compiled: the
    wider set is the safer default for a legacy record, since a wider
    current set can only make a stored `[]` support less."""
    rec = record or {}
    if int(rec.get("error_count") or 0) == 0:
        return answers
    return convicting


# The head `render_regression_detail` (`domain/test_results.py`) writes when a
# compiled attempt reddened previously-passing tests: the list is Python's
# `sorted(set)` repr, so every name is quoted with `'` (or `"` when the name
# itself carries a `'`). Parsed tolerantly to the end of the text: the
# store's `last_error_preview` is the excerpt cut at 240 characters, and a
# 13-test list does not fit.
_REGRESSED_HEAD = re.compile(
    r"REGRESSED previously-passing test\(s\):\s*\[")
_QUOTED_NAME = re.compile(r"'([^']*)'|\"([^\"]*)\"")


def regressed_tests_from_excerpt(text: str) -> tuple[str, ...]:
    """The tests a regression verdict names, in the order it names them, or
    () when `text` is not (or does not contain) a regression verdict — a
    compile excerpt, a U2 failure, an empty string. A list cut mid-name by
    the preview cap yields the complete names before the cut."""
    if not text:
        return ()
    m = _REGRESSED_HEAD.search(text)
    if not m:
        return ()
    tail = text[m.end():]
    close = tail.find("]")
    body = tail if close < 0 else tail[:close]
    return tuple(a or b for a, b in _QUOTED_NAME.findall(body))


REGRESSION_SCOPED = "regression_scoped"


def _leaf(test: str) -> str:
    return test.rsplit("::", 1)[-1]


def regression_scoped(
    record: Mapping[str, Any] | None, evidence: ConvictionEvidence,
) -> bool:
    """Whether ``record``'s lesson was written about tests this unit does
    not answer for: its `regressed` stamp is non-empty and disjoint (on
    leaf names) from ``evidence.tests``.

    A retrospective written from a regression verdict diagnoses WHY the
    attempt reddened those tests — and the diagnosis prescribes, for the
    next attempt, whatever un-reddens them. When the reddened tests are
    not the ones the unit is being re-filled FOR, the prescription is
    scoped to the wrong tests: the live `src/ensure.rs::*impl*::
    BothDebugfor(A,B)` record (cluster `stable`, `error_count 0,
    failing_test_count 13, convicted_by []`, preview "This compiled, but it
    REGRESSED previously-passing test(s): ['test_as', 'test_atom', …") was
    earned when a `SmallBuf` body reddened thirteen `ensure!`-message
    tests, and its guidance — "Drop the fixed 40-byte SmallBuf and format
    into an allocating Write … avoids the overflow fallback" — is the
    removal of the exact observable `test_trailer` and `test_whitespace`
    pin (`tests/test_ensure.rs:456`, `:495`). Read on a re-fill for those
    two tests, the lesson prescribes the defect. A record with no
    `regressed` stamp (earned before the stamp, or from a compile error)
    is not this class."""
    if not record:
        return False
    stored = record.get("regressed")
    if not isinstance(stored, list) or not stored:
        return False
    named = {_leaf(str(t)) for t in stored}
    return named.isdisjoint(_leaf(t) for t in evidence.tests)


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


# Phrases by which stored guidance stops explaining a failure and starts
# asserting that the crate CANNOT do the thing. The distinction is the whole
# point: "deref via the trait instead" is a correction the next attempt can
# act on, while "the only body-only source is `None`" and "no body can satisfy
# this" and "Do not retry this fn" are claims about the entire tree, and a
# false one steers every later re-fill back into the defect it describes.
#
# All three live examples on `anyhow`/`stable` (2026-08-14) were earned from
# REAL compile errors, which is why `GUIDANCE_FROM_HOLLOW_TREE` — keyed on
# `error_count == 0` — never touched them:
#   - `context.rs::*impl*::StdErrorforContextError<C,Error>`: the E0277 on
#     `Some(&self.error)` was real; the conclusion "so the only body-only
#     source is `None`" is refuted by the frozen tree, where
#     `ErrorImpl::error` (`.skeleton/stable/src/error.rs:585`) returns a
#     `&(dyn StdError + Send + Sync + 'static)` that coerces. Four chain tests
#     and six context tests have failed on that `None` since 2026-08-13.
#   - `context.rs::*impl*::Context<T,E>forResult<T,E>`: "no body can satisfy
#     this. ... Do not retry this fn" — and the correct `ext_context` body is
#     on disk and compiles.
#   - `error.rs::*impl*::Error`: "The skeleton does not expose the VTable
#     layout ... the chain cannot be traversed correctly" — `struct
#     ErrorVTable` is declared at `.skeleton/stable/src/error.rs:509` with
#     `object_ref` (:511) and `object_downcast` (:516), and `vtable(p)` at
#     :575.
IMPOSSIBILITY_MARKERS: tuple[str, ...] = (
    "do not retry",
    "cannot be fixed",
    "cannot be done",
    "cannot be traversed",
    "cannot be satisfied",
    "no body can",
    "not possible",
    "is impossible",
    "the only body-only",
    "there is no way",
    "nothing can be done",
)


def asserts_impossibility(guidance: str) -> bool:
    """Whether stored guidance concludes that the crate cannot do this at all,
    as opposed to telling the next attempt what to write instead."""
    low = guidance.lower()
    return any(m in low for m in IMPOSSIBILITY_MARKERS)


def impossibility_claim_is_unfounded(
    record: Mapping[str, Any] | None, still_stubbed: frozenset[str],
) -> bool:
    """Whether an impossibility claim in ``record``'s guidance may no longer be
    injected into a prompt.

    An impossibility claim is a statement about the WHOLE TREE at the moment it
    was written, and the tree moves under it every time a sibling fills. Two
    ways it stops being answerable, and both retire it:

    - a blocker it recorded has since filled. The claim was made against a tree
      that had a hole in it, the hole is closed, and the claim was never re-put
      to the compiler. Self-limiting exactly as :class:`BlockedOnStubs` argues:
      each release is bought by a distinct fn filling, and a stub set only
      shrinks.
    - it recorded no tree premise at all (`blocked_on` absent or empty). Then
      nothing about it is checkable, it can never expire on its own evidence,
      and it will steer every re-fill of the unit for the life of the store. An
      unfalsifiable claim about the tree is not a lesson; re-opening the unit
      costs one attempt and buys the chance to disprove it.

    Corrective guidance is untouched by all of this — only the impossibility
    class is judged here, and only for INJECTION. The exhaustion verdict has
    its own gates (`conviction_standing`, `cleared_blockers`)."""
    if not record:
        return False
    if not asserts_impossibility(str(record.get("guidance") or "")):
        return False
    stored = record.get("blocked_on")
    if not isinstance(stored, list) or not stored:
        return True
    return bool(cleared_blockers(record, still_stubbed))


# Phrases by which stored guidance prescribes a body that does NO WORK — a
# constant return, a discarded argument, a hollow marker — as the way to make
# a unit type-check. Such a prescription is a compile-time answer; whether it
# is the right body is a run-time question the record itself has usually
# already answered: the live instance carried `best_failing_tests: 1`.
#
# `src/context.rs::*impl*::StdErrorforContextError<C,Error>`, 9 rounds,
# `TRAIT_BOUND_VIOLATION`, `blocked_on: ["provide"]`, guidance verbatim: "For
# `source` replace the body with `None` instead of `Some(&self.error)`, and
# for `provide` replace the body with a no-op such as `let _ = request;`. This
# makes both methods type-check under the frozen `C: Display` bound alone."
# The premise is true — anyhow's `Error` implements no `StdError` — and the
# prescription is the severed chain link diagnosed on 2026-08-14: every
# `.context()` over an `anyhow::Error` stops at the first hop, ten tests
# (`test_iter/rev/len/clone`, `test_altdisplay/debug`, `test_boxed_anyhow`,
# `test_anyhow_from_anyhow`, `test_root_cause`) fail on it. Honoured into the
# prompt at every dispatch of the unit (`retro-honoured … rounds=9`, run
# `626784-4b7c38ba`, 2026-08-20), the model wrote `None` again, byte-identical
# to disk. It escaped `impossibility_claim_is_unfounded` because it is phrased
# as a correction, not a claim about the crate, and `cleared_blockers` because
# `provide` is cfg-gated out of the cluster and can never fill.
DEGENERATE_BODY_MARKERS: tuple[str, ...] = (
    "replace the body with `none`",
    "body with `none`",
    "body to `none`",
    "return `none`",
    "returning `none`",
    "with a no-op",
    "as a no-op",
    "replace the body with `ok(())`",
    "`let _ = ",
    "unimplemented!()",
    "todo!()",
    "leave the body empty",
    "empty body",
)


def prescribes_degenerate_body(guidance: str) -> bool:
    """Whether stored guidance tells the next attempt to write a body that
    does no work (a constant, a discarded argument, a hollow marker)."""
    low = guidance.lower()
    return any(m in low for m in DEGENERATE_BODY_MARKERS)


def prescription_sacrifices_tests(
    record: Mapping[str, Any] | None,
    evidence: ConvictionEvidence | None = None,
) -> bool:
    """Whether ``record`` prescribes a do-nothing body for a unit that answers
    for failing tests — on the record's own counts (`best_failing_tests`,
    `failing_test_count`), its conviction stamp (`convicted_by`), or the
    tests the unit currently answers for (``evidence``).

    A degenerate body for a unit with NO test evidence is left alone: the
    tree may hold no test for it, and then "make it type-check" is all there
    is to ask. With failing tests on the unit, the prescription is refuted by
    the measurement that produced them — the tests ARE the run-time answer —
    and injecting it steers every re-fill into the body that keeps them red."""
    if not record:
        return False
    if not prescribes_degenerate_body(str(record.get("guidance") or "")):
        return False
    recorded = (int(record.get("best_failing_tests") or 0) >= 1
                or int(record.get("failing_test_count") or 0) >= 1)
    convicted = record.get("convicted_by")
    stamped = isinstance(convicted, list) and bool(convicted)
    live = evidence is not None and bool(evidence.tests)
    return recorded or stamped or live


# Set on a record whose lesson was written from a test outcome that hollow
# siblings, not the attempted body, decided. `blocked_on` alone cannot carry
# this: the caller narrows that set as blockers fill, so it empties out and the
# text would become admissible again on the strength of the blockers having been
# fixed — which is precisely when the false premise is provable, not forgivable.
GUIDANCE_FROM_HOLLOW_TREE = "guidance_from_hollow_tree"

# Set (a dict `{"cluster": ..., "tests": [...]}`) when the futility escalation
# declared the unit's rewrites futile for the named tests: the attributed
# unit-set was UNCHANGED across judged sweeps while the tests kept failing, so
# "what should the next rewrite of this body do" — the question the guidance
# answers — has been answered "nothing" by measurement. The live instance:
# `src/chain.rs::*impl*::DoubleEndedIteratorforChain` carried a
# WRONG_TYPE_CHOICE record (rounds=14) steering "keep the buffered storage as
# the in-place Vec held by ChainState::Buff…" against a tree whose
# `ChainState::Buffered` holds `vec::IntoIter` — the model wrote `.pop()` on
# an `IntoIter` (E0599) and dereferenced `ChainState` (E0614) in run
# `2773540-e47c3754` (2026-08-17) while, in the same sweep, the escalation
# declared the accessor futile. Two mechanisms steering one unit in opposite
# directions; the suspension makes the measured one win. Cluster-scoped
# because the store is shared and the futility was measured under one
# cluster's tree; the verdict half of the record (`fully_exhausted`) is
# deliberately untouched — skipping a futile accessor is what futility wants.
FUTILITY_SUSPENDED = "futility_suspended"


def futility_suspension_applies(
    record: Mapping[str, Any] | None, cluster: str,
) -> bool:
    """Whether ``record``'s guidance is suspended for ``cluster``.

    An empty ``cluster`` (a caller that does not know its cluster) never
    matches: the futility was measured under one cluster's tree, and
    suppressing another cluster's prompt on it would be the cross-cluster
    reuse `conviction_standing` exists to forbid."""
    if not record or not cluster:
        return False
    susp = record.get(FUTILITY_SUSPENDED)
    return isinstance(susp, Mapping) and susp.get("cluster") == cluster


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


class GuidanceScreening(NamedTuple):
    """What `screen_guidance` decided: the text a prompt may carry (``""`` when
    nothing may), and the rule that retired it (``""`` when honoured or when
    there was nothing to judge)."""
    text: str
    retired_by: str


TEST_SACRIFICING_PRESCRIPTION = "test_sacrificing_prescription"
CONVICTORS_NEVER_EXECUTE = "convictors_never_execute"
NO_TREE_PREMISE = "no_tree_premise"


def tree_premise_absent(record: Mapping[str, Any] | None) -> bool:
    """True when a stored conviction recorded no checkable tree premise.

    `blocked_on` absent or empty is the same unfalsifiable claim
    `impossibility_claim_is_unfounded` already retires for the impossibility
    half of the channel. Corrective records were excluded there because
    `asserts_impossibility` is false, so a WRONG_TYPE_CHOICE lesson with
    `blocked_on: []` steered every re-fill for the life of the store — run
    `9933-efe68f37` (2026-08-31): `src/error.rs::context::Error` honoured
    six times into an E0107, `src/error.rs::msg::Error` wrote
    `force_capture` at loop exit. 30 of 33 records in that run's store
    were this class.

    A missing cache entry (`{}` / `None`) is not a stored conviction: no
    guidance and no exhaustion flag means there is nothing to retire.
    Callers that pass `retros_cache.get(key, {})` must not poison every
    unit without a record.
    """
    if not record:
        return False
    if not (record.get("guidance") or record.get("fully_exhausted")):
        return False
    stored = record.get("blocked_on")
    return not isinstance(stored, list) or not stored


def screening_unit(file: str, fn_name: str) -> FnLocator | None:
    """The locator `screen_guidance`'s eighth rule may judge, or None.

    The owner map is keyed per fn. A block record is stored under `*impl*`,
    which is never a body llvm-cov names, so passing that locator would
    refuse every block whose convicted_by tests entered any crate body —
    a sibling's reachability, not this block's. File-first yields both
    kinds of key from `file_first_guidance_keys` (the live Ok record is
    one of the per-fn keys); this is what keeps the `*impl*` keys on the
    impl-route policy and the per-fn keys on the per-fn policy, in the
    one place the eighth rule is judged.
    """
    if not file or not fn_name or fn_name == "*impl*":
        return None
    return FnLocator(file, fn_name)


def admissible_guidance(
    record: Mapping[str, Any] | None, file_text: str, *,
    evidence: ConvictionEvidence,
    anchor_types: frozenset[str] = frozenset(),
    cluster: str = "",
    cfg_dead: frozenset[str] = frozenset(),
    unit: FnLocator | None = None,
) -> str:
    """The stored lesson a prompt may carry, or ``""`` — `screen_guidance`'s
    text half, for the callers that do not log the verdict."""
    return screen_guidance(record, file_text, evidence=evidence,
                          anchor_types=anchor_types, cluster=cluster,
                          cfg_dead=cfg_dead, unit=unit).text


def screen_guidance(
    record: Mapping[str, Any] | None, file_text: str, *,
    evidence: ConvictionEvidence,
    anchor_types: frozenset[str] = frozenset(),
    cluster: str = "",
    cfg_dead: frozenset[str] = frozenset(),
    unit: FnLocator | None = None,
) -> GuidanceScreening:
    """The stored lesson a prompt may carry, or ``""``, with the retiring rule.

    ``cfg_dead`` names the fns of ``file_text`` whose every stub occurrence
    is gated out of the cluster's build. They are textually `unimplemented!()`
    and can never fill here, so a conviction that recorded one as a blocker
    would otherwise never expire on its evidence: the live ContextError record
    carried `blocked_on: ["provide"]` under a cluster that never compiles
    `provide`. A blocker outside the build is not a tree premise.

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
      (`context::Error`, `error_count` 2) is a real lesson and stays;
    - it was written from a REGRESSION of tests this unit does not answer
      for (`regression_scoped`): the diagnosis explains why those tests
      reddened and prescribes what un-reddens them, which on a re-fill for
      OTHER tests can be the removal of the very observable they pin.

    ``evidence`` is the tests the unit ANSWERS FOR (DIRECT ∪ ESCALATED,
    `evidence_for_record`), not the convicting subset: a compiled record's
    `convicted_by: []` must stop supporting it the moment any attributed
    test names the unit, escalated included."""
    rec = record or {}
    guidance = str(rec.get("guidance") or "")
    if not guidance:
        return GuidanceScreening("", "")
    if rec.get(GUIDANCE_FROM_HOLLOW_TREE):
        return GuidanceScreening("", "hollow_tree")
    # The fifth way: the futility escalation measured that rewrites of this
    # unit change nothing for the tests it answers for — the exact question
    # the guidance advises on. Scoped to the cluster the futility was
    # measured under; see `FUTILITY_SUSPENDED`.
    if futility_suspension_applies(rec, cluster):
        return GuidanceScreening("", "futility_suspended")
    # The sixth way: a do-nothing body prescribed for a unit that answers for
    # failing tests. Judged before the impossibility rule because it is the
    # class that rule cannot see — phrased as a correction, earned from a
    # real compile error, and exactly what the failing tests measure against.
    if prescription_sacrifices_tests(rec, evidence):
        return GuidanceScreening("", TEST_SACRIFICING_PRESCRIPTION)
    # The fourth way, and the one the three above cannot see: a claim that the
    # crate CANNOT do this, made against a tree that has since moved (or against
    # no recorded tree at all). Judged before the evidence rule because it
    # applies to compiler-taught records too — every live instance of it was
    # earned from a real compile error, which is exactly what makes the
    # `error_count == 0` rules above skip it.
    # `file_text` is the same file `BlockedOnStubs.at_conviction` read the
    # blockers out of, so its current stub set is the right vocabulary to
    # compare them against — deriving it here rather than taking it as a
    # parameter is what keeps the two sides from drifting.
    if impossibility_claim_is_unfounded(
            rec, stubbed_fn_names(file_text) - cfg_dead):
        return GuidanceScreening("", "impossibility_unfounded")
    # The seventh way, judged before the evidence rule and the layout rule
    # because both can PASS it: a lesson written from a regression of tests
    # this unit does not answer for. `stale_evidence` reads `convicted_by`,
    # which such a record stamps empty; the layout rule reads field claims,
    # and "drop the 40-byte buffer" makes none. See `regression_scoped`.
    if regression_scoped(rec, evidence):
        return GuidanceScreening("", REGRESSION_SCOPED)
    # The eighth way, and the only one drawn from a measurement rather than
    # from the record's own text: the tests the record was convicted on do
    # not execute the body it advises about. `stale_evidence` below asks
    # whether those tests still blame the unit — a record can pass that and
    # still be a diagnosis of somewhere else, because attribution can blame
    # a unit no convicting test enters. Live instance, cluster `stable`,
    # run `2450551-9a6a8683` (2026-08-30): `src/lib.rs::Ok` carries
    # `convicted_by: ["test_trailer"]` and guidance to return `Err(value)`
    # from `fn Ok<T>(value: T) -> Result<T>`; `test_trailer` enters 24
    # bodies and none of them is `Ok`. `fully_exhausted` is stamped, so
    # nothing retires it on its own evidence — obeying it never compiles,
    # and the retirement rules want compile evidence. Judged here rather
    # than at the injection sites so the rule cannot drift between them;
    # `unit=None` can never fire it, and neither can an unbound or silent
    # coverage reading. Callers pass `screening_unit(file, fn)` so a
    # block key (`*impl*`) stays None — the honest predicate there is
    # "no fn of this block is entered", which this signature cannot
    # express — and a per-fn key (file-first's Ok, the per-fn route)
    # carries the locator the owner map actually names. Passing no unit
    # at a per-fn injection site is how the live Ok record would still
    # steer the file-first prompt for `src/lib.rs`.
    if unit is not None:
        convicted = rec.get("convicted_by")
        if isinstance(convicted, list) and convicted and unreached_by(
                unit, [str(t) for t in convicted]):
            return GuidanceScreening("", CONVICTORS_NEVER_EXECUTE)
    # The ninth way, and the one that closes the other half of the
    # channel `impossibility_claim_is_unfounded` already covers: a
    # record with `blocked_on` absent or empty cannot expire on its
    # own evidence. Judged AFTER the eighth rule so a bound Ok
    # record (empty `blocked_on`, convictors that never enter it)
    # still retires as `convictors_never_execute` — the 225 bound
    # tests stay green. Judged BEFORE `stale_evidence` so an
    # empty-premise behavioural record is this class, not a
    # moved-blame-map class. A nonempty `blocked_on` still stubbed
    # (`src/fmt.rs::debug::ErrorImpl`) is not this class. 30 of 33
    # records on the `9933-efe68f37` store drop on the next read,
    # including compile-taught lessons that never named a stub —
    # that trade is the contract, not a mute of the three checkable
    # rows.
    if tree_premise_absent(rec):
        return GuidanceScreening("", NO_TREE_PREMISE)
    if int(rec.get("error_count") or 0) == 0 and not evidence.still_supports(rec):
        return GuidanceScreening("", "stale_evidence")
    text = layout_safe_guidance(guidance, file_text, anchor_types=anchor_types)
    return GuidanceScreening(text, "" if text else "layout_contradicted")


def retired_guidance_line(
    unit_key: str, record: Mapping[str, Any] | None, verdict: GuidanceScreening,
) -> str:
    """One log line when a stored record was retired as a class whose
    instance steered with no signature but the honoured line, or "".

    The record stays in the store and its text is what was dropped, so
    silence would convert the honoured line into nothing — the same
    observability hole those classes were written to close. The other
    retirements keep their existing silence (the per-batch tally counts
    them)."""
    rec = record or {}
    if verdict.retired_by == REGRESSION_SCOPED:
        # Same reason the class above gets a line: the record stays in the
        # store, its text is what was dropped, and the ensure instance
        # (`BothDebugfor(A,B)`, 2026-08) was honoured into a re-fill for
        # `test_trailer` with no signature but the honoured line.
        regressed = [str(t) for t in (rec.get("regressed") or [])]
        head = " ".join(str(rec.get("guidance") or "").split())[:140]
        return (f"[phase-c/retro-retired] {unit_key}: "
                f"category={rec.get('category') or '?'} "
                f"rounds={rec.get('retrospective_count') or 0} was written "
                f"from a regression of {len(regressed)} test(s) this unit "
                f"does not answer for ({regressed[:4]}) — retro guidance "
                f"withheld ({REGRESSION_SCOPED}): {head}")
    if verdict.retired_by == CONVICTORS_NEVER_EXECUTE:
        # Same reason: the Ok record (`src/lib.rs::Ok::`, run
        # `2450551-9a6a8683`) stays on disk as `fully_exhausted` and
        # nothing retires it on its own evidence. Dropping the honoured
        # line without a retired one would make the next launch look as
        # if the unit had no record.
        names = [str(t) for t in (rec.get("convicted_by") or [])]
        head = " ".join(str(rec.get("guidance") or "").split())[:140]
        return (f"[phase-c/retro-retired] {unit_key}: "
                f"category={rec.get('category') or '?'} "
                f"rounds={rec.get('retrospective_count') or 0} was convicted "
                f"by test(s) that never execute this body ({names[:4]}) "
                f"— NOT injected ({CONVICTORS_NEVER_EXECUTE}): {head}")
    if verdict.retired_by == NO_TREE_PREMISE:
        # Same shape: the record stays, the text is what was dropped.
        # Run `9933-efe68f37` honoured `src/error.rs::context::Error`
        # six times with `NO tree premise` and no retirement line.
        head = " ".join(str(rec.get("guidance") or "").split())[:140]
        return (f"[phase-c/retro-retired] {unit_key}: "
                f"category={rec.get('category') or '?'} "
                f"rounds={rec.get('retrospective_count') or 0} recorded "
                f"no tree premise — NOT injected ({NO_TREE_PREMISE}): {head}")
    if verdict.retired_by != TEST_SACRIFICING_PRESCRIPTION:
        return ""
    convicted = rec.get("convicted_by")
    tests = (f"convicted_by={sorted(str(t) for t in convicted)}"
             if isinstance(convicted, list) and convicted else
             f"best_failing_tests={rec.get('best_failing_tests') or 0}, "
             f"failing_test_count={rec.get('failing_test_count') or 0}")
    text = " ".join(str(rec.get("guidance") or "").split())
    # Quote the sentence that prescribes, not the record's opening premise —
    # the premise is usually true and the prescription is what was dropped.
    sentences = [x.strip() for x in re.split(r"(?<=[.;])\s+", text) if x.strip()]
    hit = next((x for x in sentences if prescribes_degenerate_body(x)), text)
    head = hit[:140]
    return (f"[phase-c/retro-retired] {unit_key}: "
            f"category={rec.get('category') or '?'} "
            f"rounds={rec.get('retrospective_count') or 0} prescribes a body "
            f"that does no work while the unit answers for failing test(s) "
            f"({tests}) — NOT injected: {head}")


def honoured_guidance_line(
    unit_key: str, record: Mapping[str, Any] | None, guidance: str,
) -> str:
    """One log line per HONOURED retro record at prompt-build, or "" when
    nothing was honoured.

    This is the observability floor for the poisoned-evidence class: a stored
    record steers every re-fill of its unit silently — the store is consulted
    at prompt-build and echoed nowhere, so a record whose premise the tree has
    outgrown has NO log signature at all. The instance that proved it: the
    `context.rs` ContextError-source guidance forcing the body to `None`
    (written 2026-08-13, refuted by the frozen tree the whole time) steered
    ten tests for two days and was visible only through its downstream symptom
    — the four chain tests escalating to an identical producer set in 11
    consecutive runs (`scripts/pattern_census.py` chronics table). With this
    line, "which record steered this prompt" is a grep instead of a forensic
    reconstruction, and a stalled unit's log carries the record key to check
    against the tree.

    Shows the premise class (recorded blockers vs none) because that is what
    decides whether the record can ever expire on its own evidence
    (`impossibility_claim_is_unfounded`), and the guidance HEAD verbatim so a
    reader recognizes the record without opening the store."""
    if not guidance:
        return ""
    rec = record or {}
    blocked = rec.get("blocked_on")
    premise = (f"blocked_on[{len(blocked)}]" if isinstance(blocked, list)
               and blocked else "NO tree premise")
    head = " ".join(guidance.split())[:90]
    return (f"[phase-c/retro-honoured] {unit_key}: "
            f"category={rec.get('category') or '?'} "
            f"rounds={rec.get('retrospective_count') or 0} {premise} "
            f"guidance: {head}")


def stamp_conviction(
    record: dict[str, Any],
    provenance: RetroProvenance,
    evidence: ConvictionEvidence,
    blockers: BlockedOnStubs | None = None,
    *,
    regressed: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Every half a later reader needs: the CONTEXT the verdict was earned in
    (equality-checked), the EVIDENCE it rests on (subset-checked), the hollow
    siblings it ran against (`cleared_blockers`, difference-checked) and the
    tests the failed attempt REGRESSED (`regression_scoped`,
    disjointness-checked).

    `blockers` is optional so a caller with no file snapshot in hand still
    writes a well-formed record; such a record simply carries no tree claim and
    is never released by `cleared_blockers`. `regressed` is
    `regressed_tests_from_excerpt` of the attempt's FULL failure text (the
    240-character preview the record also carries cuts a 13-test list
    mid-name); always written, so a record earned from a compile error
    carries `regressed: []` and reads as "not this class" rather than as a
    legacy record."""
    stamped = evidence.stamp(provenance.stamp(record))
    stamped["regressed"] = sorted(regressed)
    if blockers is None:
        return stamped
    stamped[GUIDANCE_FROM_HOLLOW_TREE] = bool(
        blockers.stubs) and int(stamped.get("error_count") or 0) == 0
    return blockers.stamp(stamped)


# Cargo's text for a fn signature nested INSIDE a function body — a reply that
# carried its own `fn source(&self) …` line and was spliced as a body — and the
# splicer's own refusal reasons. None of them is a verdict on what the model
# wrote; each is a verdict on how the pipeline placed it.
SPLICE_DIAGNOSTIC_MARKS: tuple[str, ...] = (
    "`self` parameter is only allowed in associated functions",
    "parse_broken",
    "item_set_changed",
    "body_redeclares_fn",
)


def exhaustion_from_splice_diagnostic(error_excerpt: str) -> bool:
    """True when the failure a conviction would be written from is a SPLICE or
    nesting diagnostic rather than a compiler verdict on the body's semantics.

    Such a failure bought no model evidence — the same rule the per-component
    mechanical cap already applies ("a splice/locate failure is refunded") —
    so it may not set `fully_exhausted`. Measured on `anyhow`/`stable`: the
    `StdErrorforContextError<C,Error>` record's `last_error_preview` was
    "error: `self` parameter is only allowed in associated functions -->
    src/context.rs:148:19 | fn source(&self) -> …", stamped `fully_exhausted:
    true` by run `902902-daee577d`, and honoured by run `1246060-2339497b` as
    proof the unit was hard (2026-08-20/21)."""
    text = error_excerpt or ""
    return any(mark in text for mark in SPLICE_DIAGNOSTIC_MARKS)
