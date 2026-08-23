"""Evidence behind a behavioural re-fill — what a prompt may CLAIM about a body
it asks the model to rewrite.

A [REFILL] mark says "this body compiles but its attributed test fails". The
frontier that stamps it is keyed on `(file, fn_name)`, and that key is weaker
than the claim in two ways the prompt used to hide:

* The test may sit in the DISPATCH projection only (a producer escalation or a
  traversal step — `FailureFrontier.convicting_for` is empty): the failure's
  path runs through a fn of this NAME, and nothing singles out this body. On
  2026-08-20 (run `902902-daee577d`) the frontier entry `src/context.rs::source`
  stamped BOTH `impl<C, E> StdError for ContextError<C, E>` (whose
  `Some(&self.error)` is correct) and `impl<C> StdError for
  ContextError<C, Error>` (the severed `None`); the prompt told the model the
  correct one "compiles but its test below FAILS — rewrite the BEHAVIOR" and
  showed the severed sibling as a settled `/* FILLED */` preview. The model's
  reasoning trace spent 71K characters concluding the target was right and the
  sibling was the bug, then hit the token ceiling.
* The same certainty rewrote `Error::construct_from_context` from an explicit
  per-type vtable (`object_downcast: context_downcast::<C, E>`, the shape the
  downcast tests need) into a `construct_from_std(ContextError { .. })`
  delegation, in the same run; the regression gate kept it because every
  attributed test was already red ("no self-failure is newly red"). A loop
  with no gradient on red tests walks downhill when it is told a correct body
  is wrong.

This module computes, per re-fill member, what the record actually supports:
the attributed tests; whether any of them CONVICTS this unit (DIRECT) or only
names it by inference (INFERRED); the same-named re-fill candidates in the
file's other impl blocks; and the failing tests' own words (panic site and
assertion, from `FailureFrontier.evidence`). Pure — no I/O, no prompt budget
decisions; the builders render what it returns.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum, auto

from pipeline_core.domain.attribution import FailureFrontier
from pipeline_core.domain.models import FnLocator, ImplBlock


class Attribution(Enum):
    """How strongly the record implicates a re-fill target. Three states,
    because collapsing the last two into one bool is what locked the pass at
    92.

    DIRECT — an attributed test's OWN failure points at this unit. Unchanged
    meaning, unchanged prompt line.

    ESCALATED — a futility escalation added this unit AFTER measuring that
    rewriting its consumers changed no verdict. That is a MEASUREMENT, and it
    is the strongest evidence the loop ever produces about a producer: the
    accessors were tried and they did not pay.

    NAMED — a traversal step or a name match mentioned the fn and nothing was
    measured. Only this one deserves "leave it unchanged".

    Why the split. `direct` was a bool and `inferred` its complement, so
    ESCALATED and NAMED rendered identically — as `re-check (… implicated by
    NAME only — rewrite ONLY if it is wrong for its bounds, else leave it
    unchanged)`. On the live tree that grade went to the five
    `construct_from_*` in all three prompts that listed them, while `rewrite`
    went to `from` twenty times over — the accessors the escalation had just
    declared futile. The unit is escalated BECAUSE its consumers were measured
    futile, and was then handed the weakest instruction in the vocabulary.
    """

    DIRECT = auto()
    ESCALATED = auto()
    NAMED = auto()

@dataclass(frozen=True, slots=True)
class RefillEvidence:
    """One re-fill member and what the record says about it."""

    fn_name: str
    # Attributed failing tests (the dispatch projection), sorted, capped.
    tests: tuple[str, ...]
    # How the record implicates this unit (see :class:`Attribution`).
    attribution: Attribution
    # Headers of the other impl blocks in the same file whose same-named
    # member is also a re-fill target: the competing candidates.
    sibling_containers: tuple[str, ...]
    # (test, what it said) for the attributed tests that left a panic body.
    excerpts: tuple[tuple[str, str], ...]

    @property
    def direct(self) -> bool:
        """A test's own failure points here. Kept as the reading it always
        had — `not direct` is NO LONGER "inference only", because ESCALATED
        sits between the two; callers that meant that want
        :func:`inferred_refill_names`."""
        return self.attribution is Attribution.DIRECT


def container_header(container: str) -> str:
    """The impl header as one line, without the body brace."""
    head = container.split("{", 1)[0]
    lines = [ln.strip() for ln in head.splitlines() if ln.strip()]
    return " ".join(lines)


def collect_refill_evidence(
    block: ImplBlock,
    blocks: Iterable[ImplBlock],
    frontier: FailureFrontier | None,
    *,
    max_tests: int = 6,
    escalated: frozenset[FnLocator] = frozenset(),
) -> tuple[RefillEvidence, ...]:
    """What the frontier supports for each of `block.refill_members`.

    `blocks` is the file's (or cluster's) full block list as stamped by
    `collect_impl_refill_targets`, so a same-named member of another block in
    the same file that carries the same stamp is reported as a sibling
    candidate. With no frontier every member is INFERRED with no tests — the
    honest reading of a re-fill nobody can name a test for."""
    out: list[RefillEvidence] = []
    for m in block.refill_members:
        key = FnLocator(block.file, m.fn_name)
        tests = (tuple(sorted(frontier.tests_for(key)))[:max_tests]
                 if frontier is not None else ())
        # A convicting test outranks the escalation: the unit's own failure is
        # better evidence than the measurement that its consumers were futile.
        if frontier is not None and frontier.convicting_for(key):
            grade = Attribution.DIRECT
        elif key in escalated:
            grade = Attribution.ESCALATED
        else:
            grade = Attribution.NAMED
        siblings = tuple(
            b.container for b in blocks
            if b.file == block.file and b.container != block.container
            and any(r.fn_name == m.fn_name for r in b.refill_members))
        excerpts: list[tuple[str, str]] = []
        if frontier is not None:
            for t in tests:
                text = frontier.evidence_for(t)
                if text:
                    excerpts.append((t, text))
        out.append(RefillEvidence(
            fn_name=m.fn_name, tests=tests, attribution=grade,
            sibling_containers=siblings, excerpts=tuple(excerpts)))
    return tuple(out)


def inferred_refill_names(evidence: Iterable[RefillEvidence]) -> frozenset[str]:
    """Members the record NAMES and never measured — rendered `[REFILL?]` and
    graded `re-check`. Escalated members are deliberately NOT here: they were
    measured, and telling the model to leave them unchanged is what this split
    exists to stop."""
    return frozenset(e.fn_name for e in evidence
                     if e.attribution is Attribution.NAMED)


def escalated_refill_names(evidence: Iterable[RefillEvidence]) -> frozenset[str]:
    """Members a futility escalation added after measuring their consumers'
    rewrites as changing nothing."""
    return frozenset(e.fn_name for e in evidence
                     if e.attribution is Attribution.ESCALATED)


def attributed_test_leaves(
    evidence: Iterable[RefillEvidence], *, cap: int = 4,
) -> tuple[str, ...]:
    """The attributed tests' bare names, direct members first, deduplicated,
    capped — the lookup keys for putting the tests' SOURCE in the prompt. A
    printed `chain::test_rev` and the escalation's `test_rev` are the same
    test, so the last `::` segment is the key."""
    seen: list[str] = []
    for e in sorted(evidence, key=lambda e: not e.direct):
        for t in e.tests:
            leaf = t.rsplit("::", 1)[-1]
            if leaf and leaf not in seen:
                seen.append(leaf)
            if len(seen) >= cap:
                return tuple(seen)
    return tuple(seen)


def render_refill_evidence(evidence: Iterable[RefillEvidence]) -> str:
    """The REFILL section's body: per member, the strength of the claim, the
    attributed tests, and what they said. "" when there is nothing to say."""
    lines: list[str] = []
    for e in evidence:
        if e.direct:
            lines.append(
                f"`{e.fn_name}` — DIRECT: a failing test's own failure points "
                f"at this body. Rewrite its BEHAVIOR; keep the signature.")
        elif e.attribution is Attribution.ESCALATED:
            # States the MEASUREMENT, never a certainty about this body. The
            # 2026-08-20 regression came from a prompt that ASSERTED a correct
            # body had failed, with no evidence attached; the model believed it
            # and replaced a working per-type vtable with a delegation. What is
            # true here is narrower and checkable: the fns that CONSUME this
            # value were rewritten and no verdict moved.
            lines.append(
                f"`{e.fn_name}` — ESCALATED: the fns that consume what this one "
                f"produces were rewritten over repeated sweeps and NO test "
                f"changed its verdict, so the defect is not in them — it is in "
                f"what this fn constructs or returns. This is a measurement, "
                f"not a claim about which line is wrong. Rewrite it.")
        else:
            lines.append(
                f"`{e.fn_name}` — INFERRED: a failing test's path runs through "
                f"a fn NAMED `{e.fn_name}` in this file; nothing singles out "
                f"THIS body. Rewrite it ONLY if it is wrong for its own bounds "
                f"and trait contract; if it is already right, return it "
                f"UNCHANGED — a different body written to look busy makes the "
                f"crate worse.")
            if e.sibling_containers:
                lines.append(
                    "  same-named re-fill candidate(s) in this file, tried "
                    "separately: " + ", ".join(
                        f"`{container_header(c)}`" for c in e.sibling_containers))
        if e.tests:
            lines.append("  attributed failing test(s): " + ", ".join(e.tests))
        for test, text in e.excerpts:
            lines.append(f"  {test} said:")
            lines.extend("    " + ln for ln in text.splitlines())
    return "\n".join(lines)
