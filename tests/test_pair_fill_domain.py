"""Pair-fill selection and shaping (`domain/pair_fill.py`), on the recorded
pair of run `358833-aa15d93c`: `impl Debug for Error::fmt` (src/error.rs)
delegating to `ErrorImpl::debug` (src/fmt.rs). The route these feed
dispatches the two as ONE transaction; the selection here decides which
pairs dispatch and what the remaining routes may still open."""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.domain.attribution import (
    FailureFrontier,
    FormatterPair,
    pair_for_unit,
)
from pipeline_core.domain.models import FnLocator, FnRecord, FnSite, ImplBlock
from pipeline_core.domain.pair_fill import (
    PAIR_CRASH_CALLEES,
    blocks_after_pair_fill,
    dispatch_including_pairs,
    extra_write_members,
    pair_dispatches,
    pair_required_files,
    pairs_to_dispatch,
    quote_dispatcher_when_implementer_entered,
    remembered_pair_callees,
    union_pair_callees,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_FX = json.loads(
    (_FIXTURES / "formatter_pair_358833.json").read_text(encoding="utf-8"))

# The recorded pair, as the hop resolves it on the live records.
_PAIR = FormatterPair(
    FnSite("src/error.rs", "fmt", "impl Debug for Error"),
    FnSite("src/fmt.rs", "debug", "impl ErrorImpl"))
_SAME_FILE = FormatterPair(
    FnSite("src/error.rs", "fmt", "impl Debug for Error"),
    FnSite("src/error.rs", "debug", "impl ErrorImpl"))


def _rec(file: str, name: str, container: str) -> FnRecord:
    return FnRecord(file=file, fn_name=name, container=container,
                    sig_text=f"fn {name} (...)")


def _blocks() -> list[ImplBlock]:
    disp = _rec("src/error.rs", "fmt", "impl Debug for Error")
    other = _rec("src/error.rs", "chain", "impl Error")
    impl_ = _rec("src/fmt.rs", "debug", "impl ErrorImpl")
    return [
        ImplBlock(file="src/error.rs", container="impl Debug for Error",
                  members=(disp,), refill_members=(disp,)),
        ImplBlock(file="src/error.rs", container="impl Error",
                  members=(other,), refill_members=(other,)),
        ImplBlock(file="src/fmt.rs", container="impl ErrorImpl",
                  members=(impl_,), refill_members=(impl_,)),
    ]


def test_pair_for_unit_matches_either_member_and_skips_same_file() -> None:
    pairs = {_PAIR, _SAME_FILE}
    assert pair_for_unit(pairs, FnLocator("src/error.rs", "fmt")) == _PAIR
    assert pair_for_unit(pairs, FnLocator("src/fmt.rs", "debug")) == _PAIR
    assert pair_for_unit({_SAME_FILE},
                         FnLocator("src/error.rs", "fmt")) is None
    assert pair_for_unit(pairs, FnLocator("src/error.rs", "chain")) is None


def test_pairs_to_dispatch_admits_a_hop_pair_the_batch_cut_dropped() -> None:
    """Plan: a hop-resolved cross-file pair is a dispatch unit of its own.
    Control: the pre-fix filter required a member in the 12-cap batch, so
    a standing-flat dispatcher plus an unentered implementer produced no
    pair run (19980-f9ea6adb fill 1 and fill 2). The pair is a Display
    dispatcher in lib.rs delegating to render.rs — the shape the module
    claims selects identically, not this crate's leftover names."""
    other = FormatterPair(
        FnSite("src/lib.rs", "fmt", "impl Display for Report"),
        FnSite("src/render.rs", "render", "impl Renderer"))
    same_file = FormatterPair(
        FnSite("src/lib.rs", "fmt", "impl Display for Report"),
        FnSite("src/lib.rs", "render", "impl Renderer"))
    assert pairs_to_dispatch({other, same_file}) == [other]
    assert pairs_to_dispatch({other}) == [other]
    # Negative control: the pre-fix batch filter on an unrelated 12-cap
    # cut (neither member in the batch) is empty.
    unrelated = {FnLocator("src/z.rs", "other")}
    assert [p for p in pairs_to_dispatch({other})
            if p.member_locators() & unrelated] == []


def test_dispatch_including_pairs_keeps_conviction_when_the_batch_dropped_both() -> None:
    """Plan: a hop pair the 12-cap batch dropped still convicts on the
    tests either endpoint already owns. Control: the batch-only dict
    FailureFrontier.build used to take drops the implementer, so
    convicting_for it is empty and the pair prompt has no REFILL_EVIDENCE
    (200802-b835274e). The pair is a Display dispatcher in lib.rs
    delegating to render.rs — not this crate's leftover names."""
    disp = FnLocator("src/lib.rs", "fmt")
    impl = FnLocator("src/render.rs", "render")
    other = FnLocator("src/z.rs", "other")
    tests = {"t_format"}
    eligible = {disp: set(tests), impl: set(tests), other: {"t_unrelated"}}
    batch = {other}
    pair = FormatterPair(
        FnSite("src/lib.rs", "fmt", "impl Display for Report"),
        FnSite("src/render.rs", "render", "impl Renderer"))
    conv = {pair.implementer: set(tests)}
    # Negative control: batch-only dispatch drops both pair members.
    old = {k: t for k, t in eligible.items() if k in batch}
    old_fr = FailureFrontier.build(old, conv)
    assert old_fr.convicting_for(impl) == set()
    assert impl not in old_fr.dispatch
    # The helper puts both members in dispatch with the hop's tests.
    new = dispatch_including_pairs(eligible, batch, {pair}, conv)
    assert other in new and new[other] == {"t_unrelated"}
    assert new[disp] == tests and new[impl] == tests
    new_fr = FailureFrontier.build(new, conv)
    assert new_fr.convicting_for(impl) == tests
    assert new_fr.tests_for(disp) == tests
    assert new_fr.tests_for(impl) == tests
    # A pair that names no test on either endpoint is not added.
    empty_pair = FormatterPair(
        FnSite("src/a.rs", "fmt", "impl Display for A"),
        FnSite("src/b.rs", "show", "impl Show"))
    only_batch = dispatch_including_pairs(eligible, batch, {empty_pair}, {})
    assert only_batch == old


def test_pair_dispatches_resolves_both_endpoint_blocks() -> None:
    jobs = pair_dispatches({_PAIR}, _blocks())
    assert len(jobs) == 1
    job = jobs[0]
    assert job.dispatcher_block.file == "src/error.rs"
    assert [m.fn_name for m in job.dispatcher_block.refill_members] == ["fmt"]
    assert not job.dispatcher_block.stub_members
    assert job.implementer_block.file == "src/fmt.rs"
    assert [m.fn_name
            for m in job.implementer_block.refill_members] == ["debug"]
    # A missing endpoint block drops the pair — the ordinary routes still
    # own its members.
    assert pair_dispatches({_PAIR}, _blocks()[:2]) == []


def test_pair_dispatches_joins_same_file_table_slots() -> None:
    """A compiling crash-revert's table slots in a member's file join
    the pair write set. A slot in a third file does not — the pair
    writes two files. Control: empty extra_locators is the two-member
    pair every recorded fill used."""
    peek = _rec("src/error.rs", "peek", "")
    other = _rec("src/z.rs", "slot", "")
    blocks = _blocks() + [
        ImplBlock(file="src/error.rs", container="",
                  members=(peek,), refill_members=(peek,)),
        ImplBlock(file="src/z.rs", container="",
                  members=(other,), refill_members=(other,)),
    ]
    jobs = pair_dispatches(
        {_PAIR}, blocks,
        extra_locators={FnLocator("src/error.rs", "peek"),
                        FnLocator("src/z.rs", "slot")})
    assert len(jobs) == 1
    assert FnLocator("src/error.rs", "peek") in jobs[0].member_locators()
    assert FnLocator("src/z.rs", "slot") not in jobs[0].member_locators()
    assert [m.fn_name for b in jobs[0].extra_blocks
            for m in b.refill_members] == ["peek"]
    bare = pair_dispatches({_PAIR}, blocks)
    assert bare[0].extra_blocks == ()
    assert bare[0].member_locators() == _PAIR.member_locators()


def test_quote_dispatcher_when_implementer_entered() -> None:
    """Plan: mixed leftovers keep both halves as refill. Controls on the
    same pair, different entered-set: every leftover entered still quotes;
    none entered still joint-writes; empty leftover set does not quote;
    a stub dispatcher stays a write even when every leftover is entered."""
    jobs = pair_dispatches({_PAIR}, _blocks())
    assert len(jobs) == 1
    pd = jobs[0]
    pair_tests = {"t_entered", "t_plain"}
    both_files = frozenset({"src/error.rs", "src/fmt.rs"})
    mixed = quote_dispatcher_when_implementer_entered(
        pd, pair_tests=pair_tests, entered={"t_entered"})
    assert extra_write_members(mixed.dispatcher_block)
    assert extra_write_members(mixed.implementer_block)
    assert pair_required_files(mixed) == both_files
    quoted = quote_dispatcher_when_implementer_entered(
        pd, pair_tests=pair_tests, entered=pair_tests)
    assert extra_write_members(quoted.dispatcher_block) == ()
    assert extra_write_members(quoted.implementer_block)
    assert pair_required_files(quoted) == frozenset({"src/fmt.rs"})
    unentered = quote_dispatcher_when_implementer_entered(
        pd, pair_tests=pair_tests, entered=set())
    assert extra_write_members(unentered.dispatcher_block)
    assert extra_write_members(unentered.implementer_block)
    assert pair_required_files(unentered) == both_files
    empty = quote_dispatcher_when_implementer_entered(
        pd, pair_tests=set(), entered=pair_tests)
    assert extra_write_members(empty.dispatcher_block)
    assert extra_write_members(empty.implementer_block)
    assert pair_required_files(empty) == both_files
    stub = _rec("src/error.rs", "fmt", "impl Debug for Error")
    stub_blocks = [
        ImplBlock(file="src/error.rs", container="impl Debug for Error",
                  members=(stub,), stub_members=(stub,)),
        _blocks()[2],
    ]
    stub_jobs = pair_dispatches({_PAIR}, stub_blocks)
    still = quote_dispatcher_when_implementer_entered(
        stub_jobs[0], pair_tests=pair_tests, entered=pair_tests)
    assert extra_write_members(still.dispatcher_block)


def test_quoted_dispatcher_prompt_forbids_an_fn_for_it() -> None:
    """The implementer-only pair prompt must not ask for a dispatcher FN.
    Mixed leftovers keep the joint-write prompt. Control: both-refill
    still says fills BOTH target blocks."""
    from pipeline_core.prompts.context import PerImplSections
    from pipeline_core.prompts.per_impl import build_per_impl_prompt
    jobs = pair_dispatches({_PAIR}, _blocks())
    pair_tests = {"t_entered", "t_plain"}
    quoted = quote_dispatcher_when_implementer_entered(
        jobs[0], pair_tests=pair_tests, entered=pair_tests)
    sections = PerImplSections(
        types="", invariants="", notes="", crate_idioms="",
        dispatch_family="", macros="", siblings="",
        retrospective_guidance="", bound_methods="")
    prompt = build_per_impl_prompt(
        block=quoted.dispatcher_block,
        related_blocks=[],
        test_blocks=[],
        module_name="m",
        cluster_name="t",
        sections=sections,
        partner_block=quoted.implementer_block)
    assert "rewrites the implementer only" in prompt
    assert "do NOT emit an <FN> for it" in prompt
    assert "fills BOTH target blocks" not in prompt
    mixed = quote_dispatcher_when_implementer_entered(
        jobs[0], pair_tests=pair_tests, entered={"t_entered"})
    mixed_prompt = build_per_impl_prompt(
        block=mixed.dispatcher_block,
        related_blocks=[],
        test_blocks=[],
        module_name="m",
        cluster_name="t",
        sections=sections,
        partner_block=mixed.implementer_block)
    assert "fills BOTH target blocks" in mixed_prompt
    assert "emitted by this pair" in mixed_prompt
    assert "coverage shows failing tests already enter" not in mixed_prompt
    assert "rewrites the implementer only" not in mixed_prompt
    both = build_per_impl_prompt(
        block=jobs[0].dispatcher_block,
        related_blocks=[],
        test_blocks=[],
        module_name="m",
        cluster_name="t",
        sections=sections,
        partner_block=jobs[0].implementer_block)
    assert "fills BOTH target blocks" in both
    assert "rewrites the implementer only" not in both


def test_a_crash_named_slot_prompt_is_aimed_not_fails() -> None:
    """Plan: a passing extra the crash extract named is tagged aimed
    (callee of the faulting formatter), never 'its test below FAILS'.
    Control: the same extra without crash_named stays FILLED / do not
    emit. Display/Renderer crate, not leftover names."""
    from pipeline_core.prompts.context import PerImplSections
    from pipeline_core.prompts.per_impl import build_per_impl_prompt
    other = FormatterPair(
        FnSite("src/lib.rs", "fmt", "impl Display for Report"),
        FnSite("src/render.rs", "render", "impl Renderer"))
    disp = _rec("src/lib.rs", "fmt", "impl Display for Report")
    impl_ = _rec("src/render.rs", "render", "impl Renderer")
    peek = FnRecord(file="src/lib.rs", fn_name="peek", container="",
                    sig_text="fn peek() -> u32", body_text="0")
    blocks = [
        ImplBlock(file="src/lib.rs", container="impl Display for Report",
                  members=(disp,), refill_members=(disp,)),
        ImplBlock(file="src/render.rs", container="impl Renderer",
                  members=(impl_,), refill_members=(impl_,)),
        ImplBlock(file="src/lib.rs", container="",
                  members=(peek,), refill_members=()),
    ]
    loc = FnLocator("src/lib.rs", "peek")
    sections = PerImplSections(
        types="", invariants="", notes="", crate_idioms="",
        dispatch_family="", macros="", siblings="",
        retrospective_guidance="", bound_methods="")
    named = pair_dispatches(
        {other}, blocks, extra_locators={loc}, crash_named={loc})[0]
    named_prompt = build_per_impl_prompt(
        block=named.dispatcher_block,
        related_blocks=[],
        test_blocks=[],
        module_name="m",
        cluster_name="t",
        sections=sections,
        partner_block=named.implementer_block,
        extra_blocks=named.extra_blocks)
    assert "callee of the faulting frame" in named_prompt
    assert "this response also rewrites the same-file" in named_prompt
    # The extra's body is `0`; the dispatcher/implementer still use the
    # FAILS tag for their own failing tests. The lie this closes is that
    # tag on the passing slot.
    assert "FAILS — rewrite the BEHAVIOR */ 0" not in named_prompt
    quoted = pair_dispatches(
        {other}, blocks, extra_locators={loc})[0]
    quoted_prompt = build_per_impl_prompt(
        block=quoted.dispatcher_block,
        related_blocks=[],
        test_blocks=[],
        module_name="m",
        cluster_name="t",
        sections=sections,
        partner_block=quoted.implementer_block,
        extra_blocks=quoted.extra_blocks)
    assert "do NOT emit an <FN> for it" in quoted_prompt
    assert "callee of the faulting frame" not in quoted_prompt


def test_pair_dispatches_quotes_a_passing_table_slot() -> None:
    """A crash-recovered slot that is not already a refill (its tests
    pass) still joins member_locators so pair_wait parks a run of it,
    but is not a refill rewrite. Control: the same locator in
    refill_members is still a rewrite
    (test_pair_dispatches_joins_same_file_table_slots). A passing slot
    the current crash extract named is a refill
    (test_a_crash_named_passing_slot_is_a_refill)."""
    peek = _rec("src/error.rs", "peek", "")
    blocks = _blocks() + [
        ImplBlock(file="src/error.rs", container="",
                  members=(peek,), refill_members=()),
    ]
    jobs = pair_dispatches(
        {_PAIR}, blocks,
        extra_locators={FnLocator("src/error.rs", "peek")})
    assert len(jobs) == 1
    assert FnLocator("src/error.rs", "peek") in jobs[0].member_locators()
    assert [m.fn_name for b in jobs[0].extra_blocks
            for m in b.refill_members] == []
    assert [m.fn_name for b in jobs[0].extra_blocks
            for m in b.members] == ["peek"]
    assert [m.fn_name for b in jobs[0].extra_blocks
            for m in b.stub_members] == []


def test_a_crash_named_passing_slot_is_a_refill() -> None:
    """Plan: extras the current compiling crash extract named are refill
    even when their attributed tests pass, so the pair can rewrite the
    callee whose return the panic stem matched. Control: the same
    passing slot not in crash_named stays quoted (the 171582
    remembered-only case). The pair is a Display dispatcher in lib.rs
    delegating to render.rs — not this crate's leftover names."""
    other = FormatterPair(
        FnSite("src/lib.rs", "fmt", "impl Display for Report"),
        FnSite("src/render.rs", "render", "impl Renderer"))
    disp = _rec("src/lib.rs", "fmt", "impl Display for Report")
    impl_ = _rec("src/render.rs", "render", "impl Renderer")
    peek = _rec("src/lib.rs", "peek", "")
    blocks = [
        ImplBlock(file="src/lib.rs", container="impl Display for Report",
                  members=(disp,), refill_members=(disp,)),
        ImplBlock(file="src/render.rs", container="impl Renderer",
                  members=(impl_,), refill_members=(impl_,)),
        ImplBlock(file="src/lib.rs", container="",
                  members=(peek,), refill_members=()),
    ]
    loc = FnLocator("src/lib.rs", "peek")
    named = pair_dispatches(
        {other}, blocks, extra_locators={loc}, crash_named={loc})
    assert len(named) == 1
    assert loc in named[0].member_locators()
    assert extra_write_members(named[0].extra_blocks[0])
    assert [m.fn_name for b in named[0].extra_blocks
            for m in b.refill_members] == ["peek"]
    quoted = pair_dispatches(
        {other}, blocks, extra_locators={loc})
    assert extra_write_members(quoted[0].extra_blocks[0]) == ()
    assert [m.fn_name for b in quoted[0].extra_blocks
            for m in b.refill_members] == []
    assert pair_required_files(named[0]) >= frozenset({"src/lib.rs"})


def test_remembered_pair_callees_survive_a_pair_only_record() -> None:
    """A later pair-only write must not drop slots a joint write remembered.
    Control: empty crash_callees yields nothing."""
    hop = _PAIR.member_locators()
    peek = FnLocator("src/error.rs", "peek")
    rec = {PAIR_CRASH_CALLEES: ["src/error.rs::peek", "src/error.rs::fmt"]}
    assert remembered_pair_callees(rec, hop) == frozenset({peek})
    assert remembered_pair_callees({}, hop) == frozenset()
    assert remembered_pair_callees(None, hop) == frozenset()
    # Union keeps the remembered slot when this write has none.
    assert "src/error.rs::peek" in union_pair_callees(rec, ())
    # A hop endpoint in the stamp is not an extra.
    assert FnLocator("src/error.rs", "fmt") not in remembered_pair_callees(
        rec, hop)


def test_a_still_stubbed_endpoint_dispatches_as_a_stub() -> None:
    stub = _rec("src/fmt.rs", "debug", "impl ErrorImpl")
    blocks = _blocks()[:2] + [
        ImplBlock(file="src/fmt.rs", container="impl ErrorImpl",
                  members=(stub,), stub_members=(stub,))]
    jobs = pair_dispatches({_PAIR}, blocks)
    assert len(jobs) == 1
    assert [m.fn_name for m in jobs[0].implementer_block.stub_members] \
        == ["debug"]
    assert not jobs[0].implementer_block.refill_members


def test_blocks_after_pair_fill_drops_served_refills_keeps_stubs() -> None:
    stub = _rec("src/error.rs", "msg", "impl Error")
    blocks = [
        ImplBlock(file="src/error.rs", container="impl Debug for Error",
                  members=(_rec("src/error.rs", "fmt",
                                "impl Debug for Error"),),
                  refill_members=(
                      _rec("src/error.rs", "fmt", "impl Debug for Error"),)),
        ImplBlock(file="src/error.rs", container="impl Error",
                  members=(stub,), stub_members=(stub,)),
    ]
    kept, dropped = blocks_after_pair_fill(
        blocks, frozenset({FnLocator("src/error.rs", "fmt"),
                           FnLocator("src/fmt.rs", "debug")}))
    assert dropped == ["src/error.rs::fmt"]
    # The refill-only pair block dropped whole; the stub block stays.
    assert [b.container for b in kept] == ["impl Error"]
    assert kept[0].stub_members == (stub,)


def test_the_recorded_pair_selects_on_the_recorded_records() -> None:
    """The live-record shape end to end: blocks built from the recorded fn
    records select exactly the one cross-file pair."""
    by_key: dict[tuple[str, str], list[FnRecord]] = {}
    for r in _FX["fn_records"]:
        rec = FnRecord.from_dict(r)
        by_key.setdefault((rec.file, rec.container), []).append(rec)
    blocks = [
        ImplBlock(file=f, container=c, members=tuple(ms),
                  refill_members=tuple(ms))
        for (f, c), ms in by_key.items()]
    jobs = pair_dispatches({_PAIR}, blocks)
    assert len(jobs) == 1
    assert jobs[0].pair == _PAIR
    assert {b.file for b in (jobs[0].dispatcher_block,
                             jobs[0].implementer_block)} \
        == {"src/error.rs", "src/fmt.rs"}
