"""One evidence assembly, shared by every route that serves impl blocks.

Evidence assembly used to be duplicated per route, so the route added last
got a fraction of it. The pair route (run `537093-ffcfd139`, 2026-09-01)
reimplemented one of the per-impl route's seven assembly calls inline: its
prompt `f226f03a7097` carried three name-matched test files — none of them
holding the two failing tests the pair was dispatched to fix — and no
`REFILL_EVIDENCE` block at all, while `build_per_impl_prompt` already had
the arguments that render both. 3 of the 10 expectations the joint write
violated were in that prompt; 0 of the 2 it existed to satisfy were.

This module owns the whole assembly, keyed on a SET of target blocks rather
than on one block, so the pair's two-file member set is expressible in the
same call as the per-impl route's one-block set — and a future route cannot
be added with a subset of the evidence.

Inputs are members, blocks, the frontier's attributed map, the
escalated/crashed/aimed sets and the ledger: no live test name, no live
unit name, no remaining-failure set. The same call on a crate whose Display
dispatcher lives in `lib.rs` and whose implementer lives in `render.rs`
assembles identically. It adds, removes and reassigns no test-to-unit edge —
it renders edges the frontier already holds.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from pipeline_core.application.reconstruct.test_locator import (
    attributed_test_blocks,
)
from pipeline_core.domain.attribution import FailureFrontier
from pipeline_core.domain.models import FnLocator, ImplBlock
from pipeline_core.domain.refill_evidence import (
    Attribution,
    RefillEvidence,
    aimed_refill_names,
    asserted_output_literals_from_tests,
    attributed_test_leaves,
    collect_refill_evidence,
    crashed_refill_names,
    escalated_refill_names,
    history_for_block,
    inferred_refill_names,
    pinned_observables_from_tests,
    render_refill_evidence,
    wraps_crate_tuple_struct,
)

# One test lookup: (bare fn/test name, max_blocks) -> (path, text) pairs.
TestLookup = Callable[[str, int], Sequence[tuple[str, str]]]

# Caps shared by every assembly: attributed-test SOURCE is capped by distinct
# FILES (several attributed tests routinely live in one file), the leaf list
# by NAMES. Values are the ones the per-impl and file-first routes measured
# in (`attributed_test_blocks` carries the 263257 instance).
ATTRIBUTED_TEST_LEAF_CAP = 12
ATTRIBUTED_TEST_FILE_CAP = 4

# How many fill-target names feed the name-match fallback, and how many
# blocks each may pull — the per-impl route's historical shape, kept
# byte-identical by the extraction.
_NAME_MATCH_TARGETS = 4
_NAME_MATCH_BLOCKS = 2


@dataclass(frozen=True, slots=True)
class BlockEvidence:
    """Everything a block-serving prompt may claim about its targets.

    `observables` are the literal texts those tests assert appear in some
    rendering — the type renderer keeps a documented type's own statement of
    one past its leading-lines cap, which is how a crate that documents its
    output format tells a fill what the assertion means.

    `test_blocks` is attributed-first, then name-match, capped by distinct
    file. `refill_evidence` is the rendered REFILL section (with the pins
    and, when a previous joint write of these members was reverted for
    regressing tests, an honestly-labelled section carrying what those
    tests assert). The name sets grade the TARGET render (`[REFILL?]` /
    `[REFILL!]` / re-derive), exactly as the per-impl route filled them."""

    evidence: tuple[RefillEvidence, ...]
    test_blocks: tuple[tuple[str, str], ...]
    refill_evidence: str
    pins: str
    observables: frozenset[str]
    inferred_names: frozenset[str]
    crashed_names: frozenset[str]
    aimed_names: frozenset[str]
    escalated_names: frozenset[str]


def render_prior_regression(
    regressed: Sequence[str], pins: str,
    outputs: Mapping[str, str] | None = None,
) -> str:
    """The section a reverted joint write owes the next one: the tests the
    previous attempt reddened, what they assert, and — when the record kept
    it — what they PRINTED when that attempt reddened them.

    `outputs` is the reverted attempt's `regressed_output` stamp (bare test
    name → the failing assertion with its `left` / `right`). Without it the
    section is names plus the pins extraction, which on run
    `3003-93e9a9bf` (2026-09-02) read `they assert: "hello",
    format!("{:#?}", …), "msg", "simple literal"` — a list from which the
    model could not tell that its output had gained a `<disabled>` suffix.

    A measurement, never an attribution — after the revert the coverage
    binding describes the baseline tree, in which none of these tests enter
    the implementer at all, so naming a guilty half would be a guess dressed
    as one. The model is shown the expectations it broke; their source is in
    the Tests section under the same file cap as the attributed channel."""
    if not regressed:
        return ""
    lines = [
        "the PREVIOUS attempt at these blocks was REVERTED because it "
        "reddened these previously-passing test(s) — the next write must "
        "satisfy the targets above WITHOUT breaking these (their source is "
        "under Tests below):",
        "  " + ", ".join(regressed),
    ]
    if pins:
        lines.append(f"  they assert: {pins}")
    if outputs:
        shown: list[str] = []
        for name in regressed:
            said = outputs.get(name) or outputs.get(name.rsplit("::", 1)[-1])
            if not said:
                continue
            body = [ln for ln in said.splitlines() if ln.strip()][:8]
            shown.append(f"    {name} said:\n"
                         + "\n".join(f"      {ln}" for ln in body))
        if shown:
            lines.append("  what they printed when that attempt reddened them "
                         "(`left` is what the reverted body produced, `right` "
                         "what the test expects):")
            lines.extend(shown)
    return "\n".join(lines)


def promote_pair_write_grades(
    evidence: tuple[RefillEvidence, ...],
) -> tuple[RefillEvidence, ...]:
    """A hop pair is one write. Coverage of the CURRENT tree often proves
    the failing tests enter the dispatcher and not the implementer — that
    emptiness is the pair's premise (the dispatcher does not yet call the
    implementer on this arm), not a licence to leave the implementer
    unchanged.

    NAMED members in this assembly become DIRECT, carrying the union of
    tests and excerpts already on DIRECT members so REFILL_TARGETS names
    the same convicting tests on both halves. CRASHED / ESCALATED / AIMED
    grades outrank and are left alone. If nobody in the assembly is
    DIRECT, the tuple is returned unchanged — a pair with no convicting
    tests is still a name-match fill, not a silent promotion.

    Prompt rendering only: the frontier's test-to-unit edges are not
    touched. The same call on a Display dispatcher in `lib.rs` whose
    tests do not yet enter `render.rs` promotes identically.
    """
    donors = [e for e in evidence if e.attribution is Attribution.DIRECT]
    if not donors:
        return evidence
    tests = tuple(dict.fromkeys(t for e in donors for t in e.tests))
    excerpts = tuple(dict.fromkeys(x for e in donors for x in e.excerpts))
    out: list[RefillEvidence] = []
    for e in evidence:
        if e.attribution is Attribution.NAMED:
            out.append(replace(
                e, attribution=Attribution.DIRECT,
                tests=e.tests or tests,
                excerpts=e.excerpts or excerpts))
        else:
            out.append(e)
    return tuple(out)


def assemble_block_evidence(
    target_blocks: Sequence[ImplBlock],
    all_blocks: Sequence[ImplBlock],
    frontier: FailureFrontier | None,
    *,
    find_tests: TestLookup,
    escalated: frozenset[FnLocator] = frozenset(),
    crashed: frozenset[FnLocator] = frozenset(),
    aimed: frozenset[FnLocator] = frozenset(),
    read_source: Callable[[str], str | None] | None = None,
    ledger_entries: Sequence[Any] = (),
    oracle_fp: str = "",
    tuple_structs: Iterable[str] = (),
    prior_regressed: Sequence[str] = (),
    prior_regressed_output: Mapping[str, str] | None = None,
    joint_rewrite: bool = False,
    leaf_cap: int = ATTRIBUTED_TEST_LEAF_CAP,
    file_cap: int = ATTRIBUTED_TEST_FILE_CAP,
) -> BlockEvidence:
    """The assembly, for any number of target blocks under one budget.

    Per target block: `collect_refill_evidence` over the file's (or
    cluster's) collected blocks with that block's own ledger history.
    Across blocks: one attributed-test-source budget (capped by distinct
    file), pins from the attributed leaves against those sources, then the
    name-match fallback over the first `_NAME_MATCH_TARGETS` fill-target
    names — the per-impl route's exact historical sequence, so a one-block
    call renders byte-identically to the code this replaces.

    `prior_regressed` is the newly-red set a previous JOINT write of these
    members was reverted on (the pair route's durable record). Its tests'
    source enters `test_blocks` under its own file cap of the same size,
    and the evidence text gains `render_prior_regression`.

    `joint_rewrite` is the pair route's flag: both endpoints are one write,
    so an unentered implementer is not graded INFERRED (run `719098-e021d93b`,
    2026-09-01: coverage redirected the leftovers onto the dispatcher, REFILL
    told the implementer to return unchanged, and the reply left
    `Debug::fmt(bt, f)`). One-block callers leave this false and stay
    byte-identical."""
    evs: list[RefillEvidence] = []
    for b in target_blocks:
        evs.extend(collect_refill_evidence(
            b, all_blocks, frontier,
            escalated=escalated, crashed=crashed, aimed=aimed,
            read_source=read_source,
            history=history_for_block(b, ledger_entries,
                                      oracle_fp=oracle_fp)))
    evidence = tuple(evs)
    if joint_rewrite:
        evidence = promote_pair_write_grades(evidence)
    leaves = attributed_test_leaves(evidence, cap=leaf_cap)
    test_blocks: list[tuple[str, str]] = attributed_test_blocks(
        leaves, lambda leaf: find_tests(leaf, 1),
        file_cap=file_cap, into=[])
    # Pins from the ATTRIBUTED sources only — the name-match files below are
    # a fallback channel and must not leak a sibling test's pins.
    attributed_sources = tuple(t for _, t in test_blocks)
    pins = pinned_observables_from_tests(leaves, attributed_sources)
    observables = asserted_output_literals_from_tests(
        leaves, attributed_sources)
    esc_names = escalated_refill_names(evidence)
    structs = frozenset(tuple_structs)
    wraps = any(
        wraps_crate_tuple_struct(m.body_text, structs)
        for b in target_blocks for m in b.refill_members
        if m.fn_name in esc_names)
    rendered = render_refill_evidence(evidence, pins=pins, wraps=wraps)
    # The previous joint write's reddened tests: their own source under the
    # same distinct-file cap (a separate budget — the attributed channel's
    # slots belong to the tests the dispatch is FOR), their assertions as
    # pins, and the labelled section.
    prior_leaves = tuple(dict.fromkeys(
        t.rsplit("::", 1)[-1] for t in prior_regressed if t))
    if prior_leaves:
        prior_blocks = attributed_test_blocks(
            prior_leaves, lambda leaf: find_tests(leaf, 1),
            file_cap=file_cap, into=[])
        prior_sources = tuple(t for _, t in prior_blocks)
        prior_pins = pinned_observables_from_tests(prior_leaves, prior_sources)
        # A reddened test's assertions are expectations the next write must
        # honour, so they select documentation exactly as the attributed
        # ones do.
        observables |= asserted_output_literals_from_tests(
            prior_leaves, prior_sources)
        seen = {p for p, _ in test_blocks}
        for path_str, txt in prior_blocks:
            if path_str not in seen:
                seen.add(path_str)
                test_blocks.append((path_str, txt))
        section = render_prior_regression(
            prior_leaves, prior_pins, prior_regressed_output)
        rendered = f"{rendered}\n{section}" if rendered else section
    # Name-match fallback, after the attributed channel exactly as before.
    fill_target_names = [
        m.fn_name for b in target_blocks
        for m in (*b.stub_members, *b.refill_members)]
    for fn_name in fill_target_names[:_NAME_MATCH_TARGETS]:
        for path_str, txt in find_tests(fn_name, _NAME_MATCH_BLOCKS):
            if not any(p == path_str for p, _ in test_blocks):
                test_blocks.append((path_str, txt))
    return BlockEvidence(
        evidence=evidence,
        test_blocks=tuple(test_blocks),
        refill_evidence=rendered,
        pins=pins,
        observables=observables,
        inferred_names=inferred_refill_names(evidence),
        crashed_names=crashed_refill_names(evidence),
        aimed_names=aimed_refill_names(evidence),
        escalated_names=esc_names,
    )
