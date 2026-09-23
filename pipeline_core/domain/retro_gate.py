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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, NamedTuple

from .coverage_slice import measured_entry_sets, unreached_by
from .model_notes import (
    LayoutJudgement,
    contradicted_notes,
    judge_note_against_layout,
    layout_safe_guidance,
    struct_fields,
)
from .models import FnLocator
from .rust_idents import impl_self_type
from .rust_scan import contains_word, struct_names, stubbed_fn_names
from .test_results import REGRESSED_ATTEMPT, RegressedAttempt

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


# Set on a record whose guidance PRESCRIBES REMOVING an observable the tests
# the unit answers for PIN — a lesson that, obeyed, re-creates the failure it
# was written to explain.
#
# Run `3003-93e9a9bf` (2026-09-02, cluster `stable`): the paired write of
# `src/error.rs::fmt` + `src/fmt.rs::debug` (the Debug formatter and its
# implementer) was reverted because printing a backtrace unconditionally
# reddened ten previously-passing sibling tests (`test_debug`,
# `debug_for_anyhow_literal`, `message_error_debug_with_string`, …). The
# retrospective written from that revert reads, verbatim from the store
# (`src/error.rs::*pair*::src/error.rs::fmt+src/fmt.rs::debug`): "In the
# Debug impl for Error in src/error.rs::fmt, drop the manual
# `self.chain().collect()` / numbered “Caused by” formatting and the trailing
# `core::fmt::Debug::fmt(bt, formatter)` backtrace emission in the
# non-alternate arm and replace the whole else block with a simple Display
# delegation … with no backtrace or manual cause-chain handling." Its sibling
# record (`src/fmt.rs::*impl*::ErrorImpl`) says "… remove the `while let
# Some(e) = source` “Caused by” loop and the `Self::backtrace(this)` /
# `format!("{:?}", bt)` printing entirely from the non-alternate path." The
# two convicting tests of that unit (`test_debug_with_captured_backtrace_and_
# cause`, `test_debug_with_captured_backtrace_simple`) pin `"\n\nCaused by:"`
# and `"Stack backtrace"` in `format!("{:?}", err)` — the retrospective
# prompt lists the pins under "The tests this unit answers for pin:" and the
# diagnosis still prescribed removing exactly the emissions the target
# requires. Honoured on the next pass as RETROSPECTIVE_GUIDANCE, the reply
# obeyed it (the implementer stripped to a Display forward) and reddened the
# same ten siblings again: the store had turned one revert into a standing
# instruction to re-create it.
#
# No rule below sees the class. `prescription_sacrifices_tests` keys on
# degenerate-body markers, and a Display forward is not one;
# `regression_scoped` exempts a joint record read back by the transaction
# that earned it (and the reddened siblings ARE tests the pair answers for at
# its own gate); the premise rules (`no_tree_premise`,
# `convictor_entry_moved`, `joint_premise_moved`, `stale_evidence`) judge
# WHERE a lesson still applies, never WHETHER it contradicts the target it is
# injected for. That question has two inputs — the guidance text and the
# pins of the tests the unit answers for — and both are in hand at write
# time (the same pins string the retrospective prompt carried) and at
# prompt-build; the stamp records the write-time answer, and `screen_guidance`
# re-judges live when a caller passes `pins`, so a record stored before the
# stamp existed is screened too.
CONTRADICTS_TARGET = "contradicts_target"
# `GuidanceScreening.retired_by` when `layout_safe_guidance` emptied the
# text. Run `106897-f7b6fb77` fill 8 retired a pair crash diagnosis this
# way and printed no line — the next fill looked as if the unit had no
# record.
LAYOUT_CONTRADICTED = "layout_contradicted"
# rustc named a missing method that the stored lesson still prescribes as a
# call. Run `235765-62f1505b` fill 1 attempt 2: E0599 `as_ptr` on Own;
# attempt 3 compiled `by_ref` and crash-reverted, wiping the compile
# error; fill 2 honoured the as_ptr lesson again and compiled the same
# E0308. The notes channel already retires this shape
# (`contradicted_notes`); the pair record did not consult it.
COMPILER_REJECTED = "compiler_rejected"
# A compiled-crash lesson that tells the next fill to drop a conversion
# the rustc-accepted body used. Run `579131-0ef5e885` fill 1 compiled
# `callee(self.inner.by_ref())` then panicked formatting a table-slot
# return; the diagnostician prescribed `callee(self.inner)` (Own where
# the signature is Ref); fill 2 honoured it and rustc said E0308.
# `compiled_rule` in the retro prompt already forbade replacing a
# type-checked call; the model ignored it. `compiler_rejected` cannot
# see this: last_compile_error is empty or a prior excerpt of a
# different expression. LAST_ERROR still carries the crashing body.
TYPECHECKED_CALL = "typechecked_call"

# Words a pinned literal can carry that name nothing: they occur in almost
# any sentence of guidance, so a removal sentence carrying one would fire on
# prose, not on the observable.
_PIN_STOPWORDS: frozenset[str] = frozenset({
    "the", "this", "that", "with", "from", "into", "then", "else", "when",
    "test", "tests", "error", "errors", "value", "string", "format",
})
# A quoted literal in a pins string: the double-quoted Rust source token
# verbatim, escapes honoured (`"Condition failed: `b\"hmm\"[1] == b'c'`
# (109 vs 99)"` is ONE token, apostrophes and all), or a single-quoted
# literal. The single-quoted form may not span a line or carry `"`, `<`
# or `>`: a lifetime (`+ 'static>>`, `&'a str>`) opens with the same
# character and would otherwise swallow the pins list up to the next
# apostrophe.
_PIN_LITERAL = re.compile(
    r'"((?:[^"\\]|\\.)*)"'
    r"|'((?:[^'\"\\\n<>]|\\.)*)'")
_PIN_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0"}
_PIN_ESCAPE = re.compile(r"\\(.)", re.DOTALL)
_WORD = re.compile(r"[A-Za-z0-9_]+")
# The shortest word that names an observable rather than occurring in one:
# `Stack`, `Caused`, `outer`, `inner` are words; `by`, `bt`, `vs` are not.
_PIN_WORD_MIN_CHARS = 4
# A guidance sentence is a removal instruction when it carries one of
# these, case-insensitive and whole-word: the negated emission verbs
# ("must not emit", "do not print", "don't emit", "never print"), the
# removal verbs with their inflections ("remove", "drop the", "omit",
# "strip", "delete"), and the absence phrases ("without", "no longer",
# "entirely from", "with no backtrace"). `\bdrop\b` also matches the
# `Drop` trait, and `without` matches a preserving "without changing"; a
# false positive here withholds one lesson from one prompt, which is the
# cheap side of this class.
_EMIT_REMOVAL = re.compile(
    r"\b(?:(?:must|do|does|did|should|will|would)\s*(?:not|n[’']t)|never)"
    r"\s+(?:emit|print|write|output)\b",
    re.IGNORECASE)
_REMOVAL_MARKER = re.compile(
    r"\b(?:(?:must|do|does|did|should|will|would)\s*(?:not|n[’']t)|never)"
    r"\s+(?:emit|print|write|output)\b"
    r"|\bremov(?:e|es|ed|ing|al)\b"
    r"|\bdrop(?:s|ped|ping)?\b"
    r"|\bomit(?:s|ted|ting)?\b"
    r"|\bstrip(?:s|ped|ping)?\b"
    r"|\bdelet(?:e|es|ed|ing)\b"
    r"|\bget rid of\b"
    r"|\bwithout\b"
    r"|\bno longer\b"
    r"|\bentirely from\b"
    r"|\bwith no\b",
    re.IGNORECASE)
# Cuts the object of a remove/drop/omit verb off from the reason that
# follows. A crash diagnosis names the pinned field as the thing the
# bogus pointer was used to format — whole-sentence overlap with a pin
# word retired those lessons while every pair fill crash-reverted
# (run `22062-a20ae3a2` rounds 2–6 "that caused"; run `106897-f7b6fb77`
# rounds 9/10/14 "that triggered" / "that was being chased" / "that
# corrupts"). The tail is any relative clause (`that`/`which` + a word)
# or `because`, not only `that caused`. "Caused by" is a pinned literal:
# it has no `that`/`which` in front, so it stays in the object.
_RELATIVE_CLAUSE = re.compile(
    r"\b(?:that|which)\s+\w+\b|\bbecause\b", re.IGNORECASE)
# A pin word used as a modifier of a runtime object (pointer, address,
# handle, object, deref, slice) is naming the corrupted value, not
# forbidding the printed observable. "removes the garbage backtrace
# pointer" (run `106897-f7b6fb77` round 9) is that shape; "drop the
# backtrace emission" / "with no backtrace" still hits because the
# following word is not in this set.
_RUNTIME_OBJECT_NOUN = frozenset({
    "pointer", "address", "handle", "object", "deref", "slice",
})
# A sentence boundary: a newline, a `;`, or a `.` followed by whitespace or
# the end of the text. The whitespace requirement is what keeps
# `self.chain().collect()`, `src/error.rs::fmt` and `core::fmt::Debug::fmt`
# inside their sentence — the live guidance quoted above holds its removal
# verb and the pinned words on opposite sides of `self.chain()`, and a
# split on every `.` would put them in different sentences. `e.g.` and
# `i.e.` are not boundaries either.
_SENTENCE_BREAK = re.compile(r"(?<!\be\.g)(?<!\bi\.e)[.;](?=\s|$)|\n+")


def guidance_sentences(text: str) -> list[str]:
    """`text` split at sentence boundaries (`_SENTENCE_BREAK`), each piece
    stripped, empty pieces dropped."""
    return [s.strip() for s in _SENTENCE_BREAK.split(text or "") if s.strip()]


def pinned_words(pins: str) -> frozenset[str]:
    """The words a pins string's quoted literals are made of, lower-cased,
    at least `_PIN_WORD_MIN_CHARS` long and outside `_PIN_STOPWORDS`.

    ``pins`` is the string `refill_evidence.pinned_observables` renders —
    a comma-joined list of assertion forms (`downcast::<T>`, `is::<T>`,
    `format!("{:#?}", …)`) and quoted source tokens (`"Stack backtrace"`,
    `"\\n\\nCaused by:"`, `"inner"`). Only the quoted literals are read,
    escapes unescaped first so `"\\n\\nCaused by:"` yields `caused`, and
    only literals whose alphanumeric content is at least four characters:
    `"msg"`, `"top"` and the `"{:#?}"` of a format form identify nothing.
    A pins string with no literal, or one made only of turbofish forms,
    yields the empty set."""
    out: set[str] = set()
    for m in _PIN_LITERAL.finditer(pins or ""):
        raw = m.group(1) if m.group(1) is not None else (m.group(2) or "")
        text = _PIN_ESCAPE.sub(
            lambda e: _PIN_ESCAPES.get(e.group(1), e.group(1)), raw)
        if sum(ch.isalnum() for ch in text) < 4:
            continue
        for word in _WORD.findall(text):
            low = word.lower()
            if len(low) >= _PIN_WORD_MIN_CHARS and low not in _PIN_STOPWORDS:
                out.add(low)
    return frozenset(out)


def removal_sentence(guidance: str) -> str:
    """The first sentence of ``guidance`` that carries a removal marker, or
    "" — the sentence a retirement line quotes, since the prescription is
    what was withheld and the record's opening premise is usually true."""
    return next((s for s in guidance_sentences(guidance)
                 if _REMOVAL_MARKER.search(s)), "")


def _removal_object_hits_pin(span: str, words: frozenset[str]) -> bool:
    """True when a pin word in ``span`` is the thing being removed, not a
    modifier of a runtime object (``_RUNTIME_OBJECT_NOUN``)."""
    tokens = list(_WORD.finditer(span))
    for i, m in enumerate(tokens):
        low = m.group(0).lower()
        if low not in words:
            continue
        if i + 1 < len(tokens):
            nxt = tokens[i + 1].group(0).lower()
            between = span[m.end():tokens[i + 1].start()]
            if nxt in _RUNTIME_OBJECT_NOUN and re.fullmatch(r"[\s/`']*", between):
                continue
        return True
    return False


def prescription_removes_pinned_observable(guidance: str, pins: str) -> bool:
    """Whether ``guidance`` instructs the next attempt to remove an
    observable that ``pins`` names: some sentence of it carries a removal
    marker (`_REMOVAL_MARKER`) AND, as a whole word, a word of one of the
    pinned literals (`pinned_words`) sits in the removal's object.

    Sentence-scoped on purpose — "Remove the unused import. Then write
    `Stack backtrace:` before the frames" is a removal and a mention that
    never meet. For a negated emission verb (`must not emit`, `never
    print`) the object is the whole sentence: that verb's object is
    everything it forbids printing. For remove/drop/omit/without the
    object is the text after the marker, cut at a relative clause
    (`that`/`which` + a word, or `because`) so a crash diagnosis that
    removes a pointer-as-value use *because* it garbled a pinned field
    is not read as forbidding that field; a pin word that then remains
    as a modifier of a runtime object (pointer, address, handle) is
    still the corrupted value, not the emission. Empty ``guidance`` or
    empty ``pins`` is False: nothing to judge, or nothing to judge
    against.

    Pure text against text: no record, no unit, no test name. The same
    predicate fires for any crate whose retrospective prescribes away a
    string its own tests assert."""
    if not guidance or not pins:
        return False
    words = pinned_words(pins)
    if not words:
        return False
    for sentence in guidance_sentences(guidance):
        m = _REMOVAL_MARKER.search(sentence)
        if not m:
            continue
        if _EMIT_REMOVAL.search(sentence):
            span = sentence
            if not words.isdisjoint(w.lower() for w in _WORD.findall(span)):
                return True
            continue
        span = sentence[m.end():]
        cut = _RELATIVE_CLAUSE.search(span)
        if cut:
            span = span[:cut.start()]
        if _removal_object_hits_pin(span, words):
            return True
    return False


# Set on a record whose lesson was written from a test outcome that hollow
# siblings, not the attempted body, decided. `blocked_on` alone cannot carry
# this: the caller narrows that set as blockers fill, so it empties out and the
# text would become admissible again on the strength of the blockers having been
# fixed — which is precisely when the false premise is provable, not forgivable.
GUIDANCE_FROM_HOLLOW_TREE = "guidance_from_hollow_tree"

# Stamped (as `["file:line", …]`) on a record whose verdict was earned from
# a test process that CRASHED or ABORTED under a compiling body: the
# `(file, line)` stub-panic sites the gate located in the crashed binaries'
# own output, resolving OUTSIDE the unit. Non-empty means the tree's holes
# decided the outcome — a correct body reached a still-stubbed `Drop`-path
# fn, the stub panicked during unwinding, the runtime aborted — so the
# lesson explains an abort the stubs caused and the exhaustion counted
# attempts no body could have passed. `blocked_on` cannot carry this: it
# is read off the FAILED BODY's callees, and the body that reaches the
# stub through `Error::drop` names none of them (the live
# `src/ptr.rs::new::Own<T>` record: `blocked_on: []`,
# `guidance_from_hollow_tree: false`, `fully_exhausted: true`, guidance
# about ZST null pointers written over the textbook `Box::into_raw` body;
# run `2585062-494e630e`, 2026-09-08). Present-and-empty means the failure
# WAS a crash and the gate located no stub panic (a segfault): the body's
# own, honoured. Absent means the record predates the stamp, and the
# verdict's prose decides (`crash_earned_conviction`).
CRASH_STUB_SITES = "crash_stub_sites"
# `GuidanceScreening.retired_by` for the class above.
CRASH_EARNED = "crash_earned"
# The openers of the two gate verdicts a compiled attempt can earn from a
# crash: the per-fn abort renderer (`cargo_runner._render_u2_abort`) and
# the regression detail's crash head (`test_results.render_regression_detail`).
# Read only for records that carry no `crash_stub_sites` stamp.
_CRASH_VERDICT_HEADS: tuple[str, ...] = (
    "Your body ABORTED the test process",
    "This made test binar",
)


# A call whose first argument is `recv.method()` — the compiled conversion
# a crash lesson must not strip into a type error. `Name::name` and
# `name` both match; nested parens in the receiver do not (the live
# bodies are `self.inner.by_ref()`).
_COMPILED_CONV_CALL = re.compile(
    r"((?:[A-Za-z_]\w*::)*[A-Za-z_]\w*)\s*\(\s*"
    r"((?:[A-Za-z_]\w*(?:\.\w+)*)\.[A-Za-z_]\w*\s*\(\s*\))"
)


def prescription_drops_compiled_conversion(
    guidance: str, failed_body: str,
) -> bool:
    """True when ``guidance`` tells the next fill to pass ``recv`` to a
    callee the compiled body invoked as ``callee(recv.method())``.

    The live fill 1 body was ``ErrorImpl::debug(self.inner.by_ref(),
    formatter)``; the lesson prescribed ``ErrorImpl::debug(self.inner,
    formatter)``. A lesson that keeps the conversion (``callee(recv.method()``)
    does not match: after ``recv`` comes ``.method()``, not a comma or
    close-paren. Empty either side is not this class.
    """
    if not (guidance or "").strip() or not (failed_body or "").strip():
        return False
    for m in _COMPILED_CONV_CALL.finditer(failed_body):
        callee, conv_arg = m.group(1), m.group(2).strip()
        recv = conv_arg.rsplit(".", 1)[0]
        if re.search(
                re.escape(callee) + r"\s*\(\s*" + re.escape(recv)
                + r"\s*[,)]",
                guidance):
            return True
    return False


def crash_earned_conviction(record: Mapping[str, Any] | None) -> bool:
    """True when ``record``'s lesson and exhaustion were earned from a crash
    or abort the tree's still-stubbed fns decided.

    Stamped records answer from the stamp: `crash_stub_sites` non-empty.
    Records written before the stamp answer from the verdict's opening
    sentence — a compiled attempt (`error_count` 0) whose failure text is
    the abort renderer's or the regression detail's crash head. That prose
    also covers a segfault, and for a legacy record the two cannot be told
    apart; retiring a segfault lesson costs one unguided re-attempt, while
    honouring a crash-earned one locks the unit behind the readmission
    deadlock the repo conventions describe (passing needs the body,
    releasing the body needs passing). A stamped-empty record is the
    post-stamp segfault and stays honoured.

    Deliberately NOT keyed on `convictor_entry_moved`: that premise is true
    while the body is a stub (the convicting tests no longer enter it) and
    turns false the moment the gate keeps the correct body — exactly when
    the stored lesson would be injected against it."""
    if not record:
        return False
    if int(record.get("error_count") or 0) != 0:
        return False
    stamped = record.get(CRASH_STUB_SITES)
    if isinstance(stamped, list):
        return bool(stamped)
    preview = str(record.get("last_error_preview") or "")
    return preview.startswith(_CRASH_VERDICT_HEADS)

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
#: Field: the measured entry set per convicting test at conviction time
#: (`{test: ["file::fn", …]}`), stamped by `stamp_conviction` for behavioural
#: records. On the endgame tree `blocked_on` is empty on essentially every
#: new record (`BlockedOnStubs.at_conviction` reads STUB callees, and one
#: stub remained in 209 fillable units), so every behavioural lesson was
#: retired as `no_tree_premise` on its first read — one model call per retro
#: round for a guidance product discarded by design (2026-09-01: 53
#: `no_tree_premise` retirements in the current log against 9 honours, and
#: 0 of 19 `src/error.rs` prompts carried MODEL_NOTES). This stamp is the
#: checkable premise that keeps the channel alive: the lesson explains a
#: failure OF THIS EXECUTION, so it stands while its convicting tests still
#: enter the same bodies and expires when that reading moves.
CONVICTOR_ENTRY = "convictor_entry"
CONVICTOR_ENTRY_MOVED = "convictor_entry_moved"
#: Fields a JOINT-transaction record (a cross-file pair written and judged
#: as one splice+reading) stamps at write: the member set the transaction
#: wrote (`joint_members`, sorted `file::fn` strings) and the sha of those
#: members' KEPT bodies after the revert (`joint_body_sha`). Together they
#: are the record's checkable premise — the lesson diagnoses a joint write
#: against exactly that tree state, stands while the member bodies still
#: hold it, and expires the moment any route lands a different body in
#: either member (`joint_premise_moved`). They also scope the regression
#: screen: the reddened tests ARE tests the same transaction answers for at
#: its own acceptance gate (the joint reading re-judges the whole baseline),
#: so a record read back by the SAME member set is not `regression_scoped` —
#: while a per-half read of any record without a matching set keeps today's
#: withhold (the `SmallBuf` poison class).
JOINT_MEMBERS = "joint_members"
JOINT_BODY_SHA = "joint_body_sha"
JOINT_PREMISE_MOVED = "joint_premise_moved"
#: Field: the fix loop a record was written in (an opaque token the loop
#: mints once per unit fill). A lesson a fix loop just wrote diagnoses the
#: body that loop is still revising, on the tree it is still reading, so
#: inside that loop its premise holds by construction. Outside it the token
#: matches nothing and the record is judged exactly as before. Before the
#: stamp, a compile-taught lesson whose failed body named no stub carried
#: no premise and was retired `no_tree_premise` on the loop's own re-entry:
#: run `271137-e728ae95` (2026-09-23) logged `retro round 1 kept the error
#: floor improving — re-entering the fix loop with the new guidance` for
#: `src/error.rs::construct_from_boxed`, then `recorded no tree premise —
#: NOT injected` on both re-entry prompts, and stopped on the same E0277.
FIX_LOOP_SCOPE = "fix_loop_scope"


def informed_reentry(verdict: GuidanceScreening) -> bool:
    """True when a retro round's fresh record would reach the next prompt.

    A fix loop re-enters after a retrospective on the promise of new
    guidance. That promise is only kept when the screen the next prompt
    runs admits the record, so the re-entry decision reads the same
    verdict. Measured over the logs on 2026-09-23: of the per-fn and impl
    re-entries logged as `re-entering the fix loop with the new guidance`,
    about four in five were followed by no honoured guidance at all, most
    of them retired silently. Each one bought three more model calls with
    nothing but the compiler errors the loop had already stopped on."""
    return bool(verdict.text)


def earned_in_loop(record: Mapping[str, Any] | None, loop_scope: str) -> bool:
    """True when ``record`` was written by the fix loop ``loop_scope`` names.

    An empty scope (every caller outside a fix loop) is never a match, so a
    stored record keeps every premise rule it had before the stamp."""
    return bool(loop_scope) and str(
        (record or {}).get(FIX_LOOP_SCOPE) or "") == loop_scope


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

    A `convictor_entry` stamp is a premise too: a behavioural lesson whose
    convicting tests' measured entry sets were recorded at conviction is
    checkable against the current reading (`convictor_entry_moved`), so it
    is not this class even with `blocked_on: []` — which is the endgame-tree
    case this stamp exists for.
    """
    if not record:
        return False
    if not (record.get("guidance") or record.get("fully_exhausted")):
        return False
    entry = record.get(CONVICTOR_ENTRY)
    if isinstance(entry, Mapping) and any(v for v in entry.values()):
        return False
    # A joint-transaction record's premise is the member bodies it diagnosed
    # against (`JOINT_BODY_SHA`) — checkable, and checked by
    # `joint_premise_moved` at the read site.
    if str(record.get(JOINT_BODY_SHA) or ""):
        return False
    stored = record.get("blocked_on")
    return not isinstance(stored, list) or not stored


def convictor_entry_moved(record: Mapping[str, Any] | None) -> bool:
    """True when a stamped convicting test measurably enters a DIFFERENT set
    of bodies than it did at conviction time.

    The lesson diagnosed an execution: "given what this test ran, here is why
    it failed". When a later fill redirects that execution — the test now
    enters other bodies — the diagnosis describes a superseded run, the same
    shape as `convictors_never_execute` one measurement earlier. Conservative
    on ignorance exactly as `measured_entry_sets` is: an unbound reading or a
    test the current sweep did not measure decides nothing, so the record is
    honoured as before the stamp existed."""
    if not record:
        return False
    entry = record.get(CONVICTOR_ENTRY)
    if not isinstance(entry, Mapping) or not entry:
        return False
    current = measured_entry_sets(str(t) for t in entry)
    for t, stamped in entry.items():
        now = current.get(str(t))
        if now is None:
            continue
        if frozenset(now) != frozenset(str(x) for x in (stamped or [])):
            return True
    return False


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
    joint_members: frozenset[str] = frozenset(),
    joint_body_sha: str = "",
    pins: str = "",
    observables: Iterable[str] = (),
    loop_scope: str = "",
) -> str:
    """The stored lesson a prompt may carry, or ``""`` — `screen_guidance`'s
    text half, for the callers that do not log the verdict."""
    return screen_guidance(record, file_text, evidence=evidence,
                          anchor_types=anchor_types, cluster=cluster,
                          cfg_dead=cfg_dead, unit=unit,
                          joint_members=joint_members,
                          joint_body_sha=joint_body_sha, pins=pins,
                          observables=observables,
                          loop_scope=loop_scope).text


def screen_guidance(
    record: Mapping[str, Any] | None, file_text: str, *,
    evidence: ConvictionEvidence,
    anchor_types: frozenset[str] = frozenset(),
    cluster: str = "",
    cfg_dead: frozenset[str] = frozenset(),
    unit: FnLocator | None = None,
    joint_members: frozenset[str] = frozenset(),
    joint_body_sha: str = "",
    pins: str = "",
    observables: Iterable[str] = (),
    loop_scope: str = "",
) -> GuidanceScreening:
    """The stored lesson a prompt may carry, or ``""``, with the retiring rule.

    ``loop_scope`` is the token of the fix loop building this prompt, or
    ``""``. A record that loop wrote (`earned_in_loop`) has a premise the
    ninth rule cannot see: it diagnoses the body the loop is still revising
    on the tree it is still reading. Only that rule yields to it; every
    other rule judges the record exactly as it would any stored lesson.

    ``pins`` is the pinned-observables string of the tests the unit answers
    for at THIS prompt-build (`refill_evidence.pinned_observables_from_tests`).
    ``observables`` are the asserted-output literals of those same tests
    (`asserted_output_literals_from_tests`) — the one-argument
    `assert!(s.contains("…"))` shape puts its literals here and leaves
    ``pins`` empty. The two are joined (`pins_for_contradiction_guard`)
    before the live re-judgment, so a record stored before
    `CONTRADICTS_TARGET` was stamped at write is screened too; both empty,
    only the stamp decides.

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
    # The same class by a different record: the lesson explains a crash or
    # abort the tree's holes decided (`CRASH_STUB_SITES`), and `blocked_on`
    # is empty because the failed body never named the stub it reached
    # through `Drop`. Judged before the premise rules below because the
    # entry premise flips to TRUE the moment the correct body is kept.
    if crash_earned_conviction(rec):
        return GuidanceScreening("", CRASH_EARNED)
    # rustc already rejected the approach the lesson still prescribes
    # (`contradicted_notes`: missing method, or E0308 found type). The
    # pair's compile-then-crash path used to drop last_compile_error on
    # the successful compile, so the next fill honoured as_ptr and
    # rebuilt the E0308; and even with the excerpt kept, E0308 was not
    # a missing-name code so the lesson still injected.
    _cerr = str(rec.get("last_compile_error") or "")
    if _cerr and contradicted_notes([guidance], _cerr):
        return GuidanceScreening("", COMPILER_REJECTED)
    # rustc accepted the last body, then a crash. A lesson that drops a
    # conversion that body used (recv.method() → recv as the callee
    # argument) is the type-error oscillation compiler_rejected cannot
    # see until rustc spends the next attempt. Control: error_count > 0
    # is a compile failure (compiler_rejected owns it); a crash preview
    # that keeps the compiled call is not this class.
    _preview = str(rec.get("last_error_preview") or "")
    _failed = str(rec.get("last_failed_body") or "")
    _crashy = ("panicked at" in _preview.lower()
               or "crash" in _preview.lower())
    if (int(rec.get("error_count") or 0) == 0 and _crashy
            and prescription_drops_compiled_conversion(guidance, _failed)):
        return GuidanceScreening("", TYPECHECKED_CALL)
    # The eleventh way, judged before every rule below because none of them
    # can see it: the lesson prescribes removing an observable the tests this
    # unit answers for pin. Obeyed, it re-creates the failure it explains —
    # the `3003-93e9a9bf` pair record (see `CONTRADICTS_TARGET`) was honoured
    # here, passed `regression_scoped` as the same transaction, passed the
    # premise rules on a live `convictor_entry`, and steered the next reply
    # into the same ten-sibling regression. The stamp is the write-time
    # answer; `pins` re-asks it against the target of THIS prompt-build.
    if rec.get(CONTRADICTS_TARGET) or (
            (guard_pins := pins_for_contradiction_guard(pins, observables))
            and prescription_removes_pinned_observable(guidance, guard_pins)):
        return GuidanceScreening("", CONTRADICTS_TARGET)
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
    # EXEMPT: a JOINT-transaction record read back by the same member set
    # that earned it (`joint_members` equal and non-empty). Its acceptance
    # gate re-judges the whole baseline, so the reddened tests are tests the
    # transaction answers for — the disjointness premise mis-models it. A
    # per-half read (no member set passed, or a different one) keeps the
    # withhold: that is the poison the rule exists for.
    _stored_scope = rec.get(JOINT_MEMBERS)
    _same_transaction = bool(
        joint_members and isinstance(_stored_scope, list) and _stored_scope
        and frozenset(str(s) for s in _stored_scope) == joint_members)
    if not _same_transaction and regression_scoped(rec, evidence):
        return GuidanceScreening("", REGRESSION_SCOPED)
    # A joint record's own expiry: the lesson diagnosed a joint write against
    # the member bodies whose sha it stamped; when either body has since
    # moved (any route landed something), the state it explains is gone.
    # Conservative on ignorance — a caller that passes no current sha (every
    # non-joint injection site) decides nothing.
    _stored_sha = str(rec.get(JOINT_BODY_SHA) or "")
    if _stored_sha and joint_body_sha and _stored_sha != joint_body_sha:
        return GuidanceScreening("", JOINT_PREMISE_MOVED)
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
    # rows. The one premise it cannot see is the fix loop that wrote the
    # record and is still running (`earned_in_loop`); out of that loop the
    # rule applies unchanged.
    if tree_premise_absent(rec) and not earned_in_loop(rec, loop_scope):
        return GuidanceScreening("", NO_TREE_PREMISE)
    # The tenth way, the ninth rule's complement for records that DO carry
    # the premise: the stamped convicting tests now measurably enter
    # different bodies, so the lesson explains a superseded execution.
    # Judged after `tree_premise_absent` (a record without the stamp is
    # never this class) and before `stale_evidence` (the blame map can be
    # unchanged while the entry reading moves).
    if convictor_entry_moved(rec):
        return GuidanceScreening("", CONVICTOR_ENTRY_MOVED)
    if int(rec.get("error_count") or 0) == 0 and not evidence.still_supports(rec):
        return GuidanceScreening("", "stale_evidence")
    text = layout_safe_guidance(guidance, file_text, anchor_types=anchor_types)
    return GuidanceScreening(text, "" if text else LAYOUT_CONTRADICTED)


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
    if verdict.retired_by == COMPILER_REJECTED:
        head = " ".join(str(rec.get("guidance") or "").split())[:140]
        return (f"[phase-c/retro-retired] {unit_key}: "
                f"category={rec.get('category') or '?'} "
                f"rounds={rec.get('retrospective_count') or 0} prescribes an "
                f"approach rustc already rejected — NOT injected "
                f"({COMPILER_REJECTED}): {head}")
    if verdict.retired_by == TYPECHECKED_CALL:
        head = " ".join(str(rec.get("guidance") or "").split())[:140]
        return (f"[phase-c/retro-retired] {unit_key}: "
                f"category={rec.get('category') or '?'} "
                f"rounds={rec.get('retrospective_count') or 0} replaces a "
                f"call rustc already type-checked — NOT injected "
                f"({TYPECHECKED_CALL}): {head}")
    if verdict.retired_by == LAYOUT_CONTRADICTED:
        # Same observability hole as CONTRADICTS_TARGET: the record stays
        # on disk, the text is what was dropped, and fill 8 of run
        # `106897-f7b6fb77` had no honoured line and no retired line.
        head = " ".join(str(rec.get("guidance") or "").split())[:140]
        return (f"[phase-c/retro-retired] {unit_key}: "
                f"category={rec.get('category') or '?'} "
                f"rounds={rec.get('retrospective_count') or 0} claims a "
                f"field none of the structs this unit is about declare — "
                f"NOT injected ({LAYOUT_CONTRADICTED}): {head}")
    if verdict.retired_by == CONTRADICTS_TARGET:
        # The record stays on disk and was honoured with no signature but
        # the honoured line on run `3003-93e9a9bf`; quote the sentence that
        # removes, not the record's opening premise — the removal is what
        # was withheld.
        pinning = [str(t) for t in (rec.get("convicted_by") or [])]
        flat = " ".join(str(rec.get("guidance") or "").split())
        head = (removal_sentence(flat) or flat)[:140]
        return (f"[phase-c/retro-retired] {unit_key}: "
                f"category={rec.get('category') or '?'} "
                f"rounds={rec.get('retrospective_count') or 0} prescribes "
                f"removing an observable the unit's own test(s) pin "
                f"(convicted_by={pinning[:4]}) — NOT injected "
                f"({CONTRADICTS_TARGET}): {head}")
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
    if verdict.retired_by == CONVICTOR_ENTRY_MOVED:
        # A healthy expiry, but the same observability need: the record
        # stays on disk, so silence would make the next launch read it as
        # never screened.
        entry = rec.get(CONVICTOR_ENTRY)
        names = sorted(str(t) for t in entry) if isinstance(
            entry, Mapping) else []
        head = " ".join(str(rec.get("guidance") or "").split())[:140]
        return (f"[phase-c/retro-retired] {unit_key}: "
                f"category={rec.get('category') or '?'} "
                f"rounds={rec.get('retrospective_count') or 0} was earned "
                f"when its convicting test(s) ({names[:4]}) entered a "
                f"different set of bodies — the execution it explains is "
                f"superseded; NOT injected ({CONVICTOR_ENTRY_MOVED}): {head}")
    if verdict.retired_by == CRASH_EARNED:
        # The record stays on disk (`fully_exhausted` and all); the text is
        # what was dropped. Without a line the next launch reads the unit as
        # never screened — and this class held `src/ptr.rs::new::Own<T>` out
        # of dispatch as `fully exhausted` for a whole night.
        sites = [str(s) for s in (rec.get(CRASH_STUB_SITES) or [])]
        head = " ".join(str(rec.get("guidance") or "").split())[:140]
        return (f"[phase-c/retro-retired] {unit_key}: "
                f"category={rec.get('category') or '?'} "
                f"rounds={rec.get('retrospective_count') or 0} was earned "
                "from a crash/abort the tree's still-stubbed fn(s) decided "
                + (f"(stub panics at {sites[:4]}) " if sites
                   else "(pre-stamp record; verdict text is the abort/crash head) ")
                + f"— NOT injected, exhaustion not honoured ({CRASH_EARNED}): "
                f"{head}")
    if verdict.retired_by == JOINT_PREMISE_MOVED:
        # Same shape as the entry-moved expiry: healthy, but the record stays
        # on disk, so silence would make the next launch read it as never
        # screened.
        head = " ".join(str(rec.get("guidance") or "").split())[:140]
        return (f"[phase-c/retro-retired] {unit_key}: "
                f"category={rec.get('category') or '?'} "
                f"rounds={rec.get('retrospective_count') or 0} diagnosed a "
                f"joint write against member bodies that have since moved — "
                f"NOT injected ({JOINT_PREMISE_MOVED}): {head}")
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
    entry = rec.get(CONVICTOR_ENTRY)
    premise = ("NO tree premise")
    if isinstance(blocked, list) and blocked:
        premise = f"blocked_on[{len(blocked)}]"
    elif isinstance(entry, Mapping) and entry:
        premise = f"convictor_entry[{len(entry)}]"
    head = " ".join(guidance.split())[:90]
    return (f"[phase-c/retro-honoured] {unit_key}: "
            f"category={rec.get('category') or '?'} "
            f"rounds={rec.get('retrospective_count') or 0} {premise} "
            f"guidance: {head}")


def pins_for_contradiction_guard(pins: str, observables: Iterable[str]) -> str:
    """The pinned-literal text the contradiction guard reads for a unit: the
    pins extraction plus every asserted-output literal, each quoted.

    `pinned_observables` reads helper-asserted literals (the last argument of
    a two-plus-argument assertion) and is EMPTY for a test written as
    `assert!(s.contains("Stack backtrace"))` — the one-argument shape the two
    convicting tests of run `3003-93e9a9bf` use; those literals reach
    `BlockEvidence.observables` instead. Handing the guard the pins alone
    would leave it blind on exactly the instance that named the class."""
    parts = [pins.strip()] if pins and pins.strip() else []
    for lit in sorted(set(observables)):
        if not lit:
            continue
        escaped = lit.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        parts.append(f'"{escaped}"')
    return " ".join(parts)


def stamp_conviction(
    record: dict[str, Any],
    provenance: RetroProvenance,
    evidence: ConvictionEvidence,
    blockers: BlockedOnStubs | None = None,
    *,
    regressed: tuple[str, ...] = (),
    pins: str = "",
    crash_stub_sites: Iterable[tuple[str, int]] | None = None,
    loop_scope: str = "",
) -> dict[str, Any]:
    """Every half a later reader needs: the CONTEXT the verdict was earned in
    (equality-checked), the EVIDENCE it rests on (subset-checked), the hollow
    siblings it ran against (`cleared_blockers`, difference-checked), the
    tests the failed attempt REGRESSED (`regression_scoped`,
    disjointness-checked) and whether the lesson CONTRADICTS the target it
    will be injected for (`CONTRADICTS_TARGET`, judged here against `pins`).

    `blockers` is optional so a caller with no file snapshot in hand still
    writes a well-formed record; such a record simply carries no tree claim and
    is never released by `cleared_blockers`. `regressed` is
    `regressed_tests_from_excerpt` of the attempt's FULL failure text (the
    240-character preview the record also carries cuts a 13-test list
    mid-name); always written, so a record earned from a compile error
    carries `regressed: []` and reads as "not this class" rather than as a
    legacy record.

    `pins` is the pinned-observables string of the tests the unit answers
    for — the string the retrospective prompt itself carried under "The
    tests this unit answers for pin:". Non-empty, the record's guidance is
    judged against it (`prescription_removes_pinned_observable`) and a
    lesson that prescribes removing a pinned observable is stamped
    `contradicts_target: True`; otherwise the key is ABSENT — removed, not
    left, because a record is re-stamped with new guidance every round and
    a stamp from a previous round's text would withhold a lesson that no
    longer contradicts anything. Empty `pins` (every caller before the
    stamp existed, and a unit with no attributed test to pin) judges
    nothing and writes the record exactly as before.

    `crash_stub_sites` is the gate's evidence when the failure this record
    is written from was a CRASH or ABORT of the test process: the
    `(file, line)` stub-panic sites located in the crashed binaries' own
    output, outside the unit (`U2Verdict.external_stub_sites` per-fn,
    `RegressionComparison.crash_stub_sites` on the joint routes). Written
    as `CRASH_STUB_SITES`; non-empty also sets `GUIDANCE_FROM_HOLLOW_TREE`
    (the lesson explains an outcome the stubs decided) and clears
    `fully_exhausted` (the attempts counted could not have passed while
    the drop path was hollow). ``None`` — the failure was not a crash —
    writes no key, so the record reads as "not this class" rather than as
    a legacy record.

    `loop_scope` is the token of the fix loop writing the record
    (`FIX_LOOP_SCOPE`). Written when non-empty and removed otherwise, so a
    record re-stamped outside a loop never carries an old loop's token."""
    stamped = evidence.stamp(provenance.stamp(record))
    if loop_scope:
        stamped[FIX_LOOP_SCOPE] = loop_scope
    else:
        stamped.pop(FIX_LOOP_SCOPE, None)
    stamped["regressed"] = sorted(regressed)
    # A conviction is written by a failed attempt: whatever it reddened was
    # reverted with it. An empty set carries no claim to label.
    if stamped["regressed"]:
        stamped[REGRESSED_ATTEMPT] = RegressedAttempt.REVERTED.value
    else:
        stamped.pop(REGRESSED_ATTEMPT, None)
    if crash_stub_sites is not None:
        _sites = sorted(f"{f}:{ln}" for f, ln in set(crash_stub_sites))
        stamped[CRASH_STUB_SITES] = _sites
        if _sites and int(stamped.get("error_count") or 0) == 0:
            stamped[GUIDANCE_FROM_HOLLOW_TREE] = True
            stamped["fully_exhausted"] = False
    else:
        stamped.pop(CRASH_STUB_SITES, None)
    if pins and prescription_removes_pinned_observable(
            str(stamped.get("guidance") or ""), pins):
        stamped[CONTRADICTS_TARGET] = True
    else:
        stamped.pop(CONTRADICTS_TARGET, None)
    # Behavioural records (learned from test outcomes, `error_count` 0) get
    # the entry premise: the bodies each convicting test measurably entered
    # at conviction time. This is what keeps such a lesson alive on a tree
    # with no stub callees to record in `blocked_on` — it stands while the
    # reading stands and `convictor_entry_moved` expires it when a fill
    # redirects the execution. Unbound / unmeasured stamps nothing, and the
    # record then falls to `tree_premise_absent` exactly as before.
    if int(stamped.get("error_count") or 0) == 0:
        _entries = measured_entry_sets(
            str(t) for t in (stamped.get("convicted_by") or []))
        if _entries:
            stamped[CONVICTOR_ENTRY] = {
                t: list(v) for t, v in _entries.items()}
    if blockers is None:
        return stamped
    stamped[GUIDANCE_FROM_HOLLOW_TREE] = (
        bool(stamped.get(GUIDANCE_FROM_HOLLOW_TREE))
        or (bool(blockers.stubs)
            and int(stamped.get("error_count") or 0) == 0))
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
