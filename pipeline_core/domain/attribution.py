"""Domain types + pure helper for failure→fn attribution.

`FailureKind` is a StrEnum (its members ARE their wire strings, so existing callers that
treat the kind as a plain string keep working) and it also answers whether the path it
names may CONVICT a unit. `line_to_fn_record` is the pure (text, line) → fn-record
mapping, moved out of pipeline.py.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, NamedTuple

from pipeline_core.domain.models import FnLocator, Frontier


class FailureKind(StrEnum):
    """Which attribution path produced a frontier (for logs/events)."""

    RUNTIME = "runtime"
    COMPILE = "compile"
    BEHAVIOR = "behavior"
    CLASSIFIER = "classifier"
    RECENTLY_FILLED = "recently_filled"
    REVERSE_MAP = "reverse_map"
    MERGED = "merged"
    # Fns reached through a test binary that produced NO verdict at all. A
    # binary that segfaults prints no `FAILED` line, so its tests convict
    # nothing and every other path is blind to them — and on `anyhow`
    # 2026-08-05 those were the only tests whose repair could have moved the
    # merged tree's score. It does NOT convict: the binary went down as a
    # whole and nothing in the output says which of its tests did it, so this
    # is "who might be responsible", never grounds to revert a fill.
    CRASHED_BINARY = "crashed_binary"
    NO_FRONTIER = "no_frontier"

    @property
    def convicts(self) -> bool:
        """True when this path OBSERVED the failure AT the unit — a panic
        located in its body, a compile error on its lines, or the failing
        test's own body naming it. Those are the only links strong enough to
        REVERT a fill. Every other path answers "who might be responsible"
        (a reachability guess, a classifier's opinion, "it was filled
        recently", the producers a futile test escalates to); widening work
        on such a guess costs compute, but judging on it makes acceptance
        unreachable by construction — the unit is asked to fix a test it
        cannot influence, so no attempt count ever helps."""
        return self in _CONVICTING_KINDS


_CONVICTING_KINDS = frozenset({
    FailureKind.RUNTIME, FailureKind.COMPILE, FailureKind.BEHAVIOR})


@dataclass(frozen=True, slots=True)
class FailureFrontier:
    """The iteration's work list, carrying BOTH of its projections so the two
    readings can never drift apart.

    `dispatch` — which unit to attempt and which failing tests motivate it.
    Over-inclusion here is cheap and often deliberate (unattributed stubs,
    producer escalation): the worst case is an attempt that changes nothing.

    `convicting` — the tests allowed to REVERT that unit when it compiles but
    they still fail. Over-inclusion here is fatal, not merely wasteful: a unit
    convicted by a test it cannot influence is reverted on every attempt, in
    every iteration, forever.

    Built once via `build`, which enforces `convicting[k] ⊆ dispatch[k]` and
    drops keys absent from dispatch — the invariant lives here, not in each
    fill route."""

    dispatch: Frontier
    convicting: Frontier
    # What the failing tests SAID — panic site + assertion text per test, keyed
    # as libtest printed the name (`test_report.failure_evidence_by_test`). The
    # prompt that asks for a re-fill owes the model this: on 2026-08-20 (run
    # `902902-daee577d`) the per-impl REFILL section read "its attributed test
    # (see Tests below) still FAILS" while the tests it meant (`test_iter`,
    # `test_len`, `test_rev`, `test_clone`) were absent from the prompt and
    # their `left: 4 / right: 1` never reached the model. Carried on the
    # frontier rather than threaded as one more engine parameter because the
    # frontier already travels every seam the evidence must reach.
    evidence: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def build(
        cls, dispatch: Frontier, convicting: Frontier,
        evidence: Mapping[str, str] | None = None,
    ) -> FailureFrontier:
        narrowed = {
            key: (convicting.get(key, set()) & tests)
            for key, tests in dispatch.items()
            if convicting.get(key)
        }
        return cls(dict(dispatch), {k: v for k, v in narrowed.items() if v},
                   dict(evidence or {}))

    @classmethod
    def of_direct(cls, attributed: Frontier) -> FailureFrontier:
        """A frontier whose every attribution rests on direct evidence — the
        two projections coincide."""
        return cls(dict(attributed), {k: set(v) for k, v in attributed.items() if v})

    @classmethod
    def of_dispatch(cls, dispatch: Frontier) -> FailureFrontier:
        """A frontier whose attributions carry no direct evidence — every unit
        is attempted, none can be reverted for a still-failing test. The
        compile gate and the global non-regression ratchet still apply."""
        return cls(dict(dispatch), {})

    def tests_for(self, key: FnLocator) -> set[str]:
        return self.dispatch.get(key, set())

    def convicting_for(self, key: FnLocator) -> set[str]:
        return self.convicting.get(key, set())

    def evidence_for(self, test: str) -> str:
        """The failing test's own words, or "". Exact name first; a bare name
        (the escalation's `test_rev`) matches a printed `chain::test_rev` on
        the last `::` segment, the same rule `oracle_ledger.bare_test_name`
        applies when reconciling the two name forms."""
        if test in self.evidence:
            return self.evidence[test]
        leaf = test.rsplit("::", 1)[-1]
        for name, text in self.evidence.items():
            if name.rsplit("::", 1)[-1] == leaf:
                return text
        return ""

    def __bool__(self) -> bool:
        return bool(self.dispatch)


class AttributionResult(NamedTuple):
    """`frontier` is the DISPATCH map (every path's attributions unioned);
    `convicting` is the direct-evidence subset of it (see
    `FailureKind.convicts`). Both are returned together so a caller cannot
    reach for one and silently get the other."""

    frontier: Frontier
    kind: FailureKind
    convicting: Frontier


def line_to_fn_record(
    file_text: str, line_no: int, fn_records: list[dict[str, Any]],
    col_no: int | None = None,
) -> dict[str, Any] | None:
    """Given file text and a 1-based (line, col), return the fn record whose body
    spans that position, else None. `fn_records` must already be filtered to the
    file. The extractor emits compact one-line-per-item output, so a line can hold
    several fns; `col_no` (when present) disambiguates, otherwise the first fn whose
    body range overlaps the line wins."""
    if line_no < 1 or not fn_records:
        return None
    lines_iter = file_text.splitlines(keepends=True)
    if line_no > len(lines_iter):
        return None
    line_start = sum(len(ln) for ln in lines_iter[:line_no - 1])
    line_end = line_start + len(lines_iter[line_no - 1])
    if col_no is not None and col_no >= 1:
        byte_offset = line_start + col_no - 1
        for rec in fn_records:
            s = rec.get("sig_byte_start", 0)
            e = rec.get("body_byte_end", 0)
            if s <= byte_offset < e:
                return rec
    # Fallback: pick the first fn whose body overlaps the line range.
    for rec in fn_records:
        s = rec.get("sig_byte_start", 0)
        e = rec.get("body_byte_end", 0)
        if max(s, line_start) < min(e, line_end):
            return rec
    return None


def union_unattributed_stubs(
    frontier: Frontier, remaining_stubs: list[FnLocator],
) -> Frontier:
    """Add every remaining `unimplemented!()` stub to the frontier so a helper
    that NO failing test attributes directly (a leaf called only transitively,
    e.g. inside another fn's test) still gets filled in phase-c.

    Without this, an un-attributed stub survives phase-c entirely — it never
    enters the test→fn frontier, so it's only filled by the post-TASK3
    whole-file sweep, while its tested callers thrash as "blocked by an unfilled
    sibling stub" until the cluster plateaus. An un-attributed stub carries an
    EMPTY test set (the u2 gate then accepts it on compile; the outer TDD loop
    re-tests). Already-attributed keys keep their test set unchanged. Returns a
    new dict; inputs are not mutated."""
    merged: Frontier = {key: set(tests) for key, tests in frontier.items()}
    for key in remaining_stubs:
        merged.setdefault(key, set())
    return merged
