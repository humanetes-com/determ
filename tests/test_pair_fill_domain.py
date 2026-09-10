"""Pair-fill selection and shaping (`domain/pair_fill.py`), on the recorded
pair of run `358833-aa15d93c`: `impl Debug for Error::fmt` (src/error.rs)
delegating to `ErrorImpl::debug` (src/fmt.rs). The route these feed
dispatches the two as ONE transaction; the selection here decides which
pairs dispatch and what the remaining routes may still open."""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.domain.attribution import (
    FailureFrontier, FormatterPair, pair_for_unit,
)
from pipeline_core.domain.models import FnLocator, FnRecord, FnSite, ImplBlock
from pipeline_core.domain.pair_fill import (
    blocks_after_pair_fill,
    dispatch_including_pairs,
    pair_dispatches,
    pairs_to_dispatch,
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
