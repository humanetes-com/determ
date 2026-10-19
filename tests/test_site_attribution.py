"""How a fill route grades ONE definition of a name, and what a NAMED ask
opens with.

Run `641699-ae684bdb` (2026-10-19): the pin test panicked in the
`unimplemented!()` of `provide` under `impl StdError for ContextError<C,
Error>`, and the locator reading graded the other definition, whose body was
right, as if that failure pointed at it. The recorded sweep of that run is the
fixture (`tests/fixtures/task3_retry/641699-ae684bdb_task3_output.txt`); the
convicting site is what `extract_tdd_frontier` returned on it over the tree it
was measured on.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.domain.attribution import FailureFrontier
from pipeline_core.domain.last_error import LastErrorKind
from pipeline_core.domain.models import FnLocator, FnRecord, FnSite, ImplBlock
from pipeline_core.domain.refill_evidence import (
    Attribution,
    collect_refill_evidence,
    site_attribution,
)
from pipeline_core.domain.test_report import failure_evidence_by_test
from pipeline_core.prompts.context import PerFnFixSections
from pipeline_core.prompts.per_fn import build_per_fn_fix_prompt

FIXTURE = (Path(__file__).parent / "fixtures" / "task3_retry"
           / "641699-ae684bdb_task3_output.txt")
PIN = "context_error_provide_is_invoked_and_forwards"
FILE = "src/context.rs"
STUBBED = "impl StdError for ContextError < C , Error >"
FILLED = "impl StdError for ContextError < C , E >"
HOMONYMS = (FILLED, STUBBED)


def _sweep_frontier() -> FailureFrontier:
    output = FIXTURE.read_text()
    return FailureFrontier.build(
        {FnLocator(FILE, "provide"): {PIN}},
        {FnSite(FILE, "provide", STUBBED): {PIN}},
        evidence={t: e for t, e in failure_evidence_by_test(output).items()
                  if t != "ui"},
        held={PIN})


def test_the_fill_routes_grade_a_homonym_by_its_own_conviction():
    """`collect_refill_evidence` read DIRECT off the locator union, so the
    filled sibling of a convicted stub was told a failing test's own failure
    pointed at it."""
    fr = _sweep_frontier()
    filled = FnRecord(file=FILE, fn_name="provide")
    blocks = (
        ImplBlock(file=FILE, container=FILLED, members=(filled,),
                  refill_members=(filled,)),
        ImplBlock(file=FILE, container=STUBBED,
                  members=(FnRecord(file=FILE, fn_name="provide"),)),
    )
    [ev] = collect_refill_evidence(blocks[0], blocks, fr)
    assert ev.attribution is Attribution.NAMED
    assert site_attribution(fr, FILE, "provide", STUBBED,
                            HOMONYMS) is Attribution.DIRECT
    # Negative control: the locator union convicts both definitions.
    assert fr.convicting_for(FnLocator(FILE, "provide")) == {PIN}


def test_a_name_with_one_definition_is_graded_as_before():
    fr = FailureFrontier.build(
        {FnLocator("src/a.rs", "len"): {"t_len"}},
        {FnSite("src/a.rs", "len", "impl Bag"): {"t_len"}})
    assert site_attribution(fr, "src/a.rs", "len", "impl Bag",
                            ("impl Bag",)) is Attribution.DIRECT


def test_a_named_ask_does_not_open_by_ordering_a_change():
    sections = PerFnFixSections(
        macros="", notes="", container=FILLED, types="", invariants="",
        siblings="", crate_idioms="", bound_methods="")
    kw = {"file_text": "fn provide() {}\n", "fn_name": "provide",
          "sig_text": "fn provide()", "test_blocks": [], "module_name": "m",
          "cluster_name": "nightly", "file_rel": FILE, "prev_body": "{}",
          "error_excerpt": "`provide` — INFERRED: …", "sections": sections,
          "kind": LastErrorKind.UNPASSED}
    named = build_per_fn_fix_prompt(**kw, attribution=Attribution.NAMED)
    assert named.startswith("RE-check ONE Rust function.")
    assert "Don't repeat the previous body" not in named
    assert "previous attempt reverted" not in named
    control = build_per_fn_fix_prompt(**kw)
    assert "Don't repeat the previous body" in control
    assert "previous attempt reverted" in control
