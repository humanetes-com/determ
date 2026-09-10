"""Selecting and shaping a formatter pair's dispatch — the cross-file
dispatcher/implementer transaction.

The class this serves (run `358833-aa15d93c`, 2026-09-01, cluster `stable`):
the paying write for a trait-format leftover is co-dependent across two
files — a delegation arm in the dispatcher (`impl Debug for Error::fmt`,
`src/error.rs`) plus the section it delegates to in the implementer
(`ErrorImpl::debug`, `src/fmt.rs`). File-first is per-file and the revert
gate judges per fill, so each half alone is correctly rejected: delegation
alone routes `{:?}` into the unconditional printer and reddens the
no-backtrace Debug tests (measured: the last-resort attempt's 5 newly-red);
a printer rewrite alone moves no leftover (measured: red-dead) while it can
redden the alternate-arm tests (`test_altdebug` in the same 5). No sequence
of independently-judged keeps can land it. The pair the attribution hop
already resolved (`FormatterPair`) is the dispatch unit that can: one
prompt carrying both blocks, one splice of both replies, one compile+test
reading, kept or reverted together. It dispatches from the hop set, not
from the 12-cap batch: a standing-flat dispatcher plus an unentered
implementer is exactly the pair, not a reason to skip it.

Everything here is pure selection/shaping; the transaction itself lives in
the impl fill route. Inputs are the hop's pair records and the collected
impl blocks — no test name, no unit name, no crate shape: a Display
dispatcher in `lib.rs` delegating to `render.rs` on another crate selects
identically.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace
from typing import NamedTuple

from pipeline_core.domain.attribution import FormatterPair, container_key
from pipeline_core.domain.body_ledger import body_sha
from pipeline_core.domain.models import FnLocator, FnSite, ImplBlock


class PairDispatch(NamedTuple):
    """One pair ready to dispatch: the pair plus each endpoint's block
    narrowed to that single member (stub if the body is still a stub,
    refill otherwise)."""

    pair: FormatterPair
    dispatcher_block: ImplBlock
    implementer_block: ImplBlock

    def member_locators(self) -> frozenset[FnLocator]:
        return self.pair.member_locators()

    def member_scope(self) -> frozenset[str]:
        """The transaction's member identity as `file::fn` strings — the
        `joint_members` stamp a pair's durable record carries and the scope
        a read-back is judged under (the same-transaction exemption of the
        regression screen)."""
        return frozenset(f"{k.file}::{k.fn_name}"
                         for k in self.member_locators())

    def member_body_sha(self) -> str:
        """Identity of the members' CURRENT bodies, in deterministic member
        order — the checkable premise a joint-transaction record stamps at
        write (post-revert, the kept tree) and re-computes at read: the
        lesson diagnoses a joint write against exactly this tree state and
        expires when any route lands a different body in either member."""
        texts = []
        for b in (self.dispatcher_block, self.implementer_block):
            for m in sorted((*b.stub_members, *b.refill_members),
                            key=lambda m: (b.file, m.fn_name)):
                texts.append(m.body_text)
        return body_sha(texts)


def pair_record_container(pair: FormatterPair) -> str:
    """The container half of a pair record's retro-store key — both
    endpoints, so a pair's memory can never collide with either member
    block's own `*impl*` record."""
    d, i = pair.dispatcher, pair.implementer
    return f"{d.file}::{d.fn_name}+{i.file}::{i.fn_name}"


def _endpoint_block(
    site: FnSite, all_blocks: Sequence[ImplBlock],
) -> ImplBlock | None:
    """The impl block holding `site`, narrowed to that one member.

    Matched on file + canonical container identity (`container_key`, the
    one mechanism both printers of a header go through), then the member by
    name. The member keeps its stub/refill classification: a still-stubbed
    endpoint splices as a stub, a filled one as a refill overwrite."""
    want = container_key(site.container) if site.container else ""
    for b in all_blocks:
        if b.file != site.file:
            continue
        if want and container_key(b.container) != want:
            continue
        stub = tuple(m for m in b.stub_members if m.fn_name == site.fn_name)
        if stub:
            return replace(b, stub_members=stub, refill_members=())
        member = tuple(m for m in b.members if m.fn_name == site.fn_name)
        if member:
            return replace(b, stub_members=(), refill_members=member)
    return None


def pairs_to_dispatch(
    pairs: Iterable[FormatterPair],
) -> list[FormatterPair]:
    """Every cross-file pair the hop resolved, deduped, in a deterministic
    order. The pair is its own dispatch unit: it does not wait for a
    member to survive ``take_batch`` or standing-flat. Run
    ``19980-f9ea6adb`` (2026-09-10): the hop named the pair, standing-flat
    excluded the dispatcher from the 12-cap batch, coverage dropped the
    implementer as unentered, ``pairs_to_dispatch`` of that batch was
    empty, and two sessions split the transaction. Same-file pairs never
    dispatch (file-first already writes both under one judgment)."""
    out: list[FormatterPair] = []
    seen: set[FormatterPair] = set()
    for p in sorted(pairs, key=lambda p: (p.dispatcher, p.implementer)):
        if p in seen or not p.cross_file:
            continue
        seen.add(p)
        out.append(p)
    return out


def pair_dispatches(
    pairs: Iterable[FormatterPair],
    all_blocks: Sequence[ImplBlock],
) -> list[PairDispatch]:
    """The hop's dispatchable pairs with both endpoint blocks resolved.
    A pair either of whose endpoints has no collected block (record drift,
    a free fn) is dropped — the ordinary routes still own its members."""
    out: list[PairDispatch] = []
    for p in pairs_to_dispatch(pairs):
        db = _endpoint_block(p.dispatcher, all_blocks)
        ib = _endpoint_block(p.implementer, all_blocks)
        if db is None or ib is None:
            continue
        out.append(PairDispatch(p, db, ib))
    return out


def blocks_after_pair_fill(
    blocks: Sequence[ImplBlock],
    endpoints: frozenset[FnLocator],
) -> tuple[list[ImplBlock], list[str]]:
    """The remaining routes' block list after the pair route served
    `endpoints` this call: their refill entries drop (a same-iteration
    re-open of a pair-served member is a re-buy — the next sweep is the
    first moment it can be judged, the same rule file-first applies to the
    files it produced), stub entries stay (a stub still must be written),
    and a block left with no work drops whole.

    Returns ``(kept, dropped_unit_labels)``."""
    kept: list[ImplBlock] = []
    dropped: list[str] = []
    for b in blocks:
        refills = tuple(
            m for m in b.refill_members
            if FnLocator(b.file, m.fn_name) not in endpoints)
        if len(refills) != len(b.refill_members):
            dropped.extend(
                f"{b.file}::{m.fn_name}" for m in b.refill_members
                if FnLocator(b.file, m.fn_name) in endpoints)
            b = replace(b, refill_members=refills)
        if b.stub_members or b.refill_members:
            kept.append(b)
    return kept, sorted(set(dropped))
