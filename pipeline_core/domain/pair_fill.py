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

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import NamedTuple

from pipeline_core.domain.attribution import (
    FormatterPair,
    container_key,
    key_as_site,
)
from pipeline_core.domain.body_ledger import body_sha
from pipeline_core.domain.last_error import (
    LastErrorKind,
    instruction_for,
    resolve_kind,
)
from pipeline_core.domain.models import FnLocator, FnRecord, FnSite, ImplBlock


class PairDispatch(NamedTuple):
    """One pair ready to dispatch: the pair plus each endpoint's block
    narrowed to that single member (stub if the body is still a stub,
    refill otherwise).

    ``extra_blocks`` are same-file table slots a compiling pair
    crash-revert recovered (``crash_vtable_callees``). They live in a
    member's file, so pair_wait parks a run of them; a separately-judged
    file of those locators on the rolled-back tree cannot pay — the
    current members do not enter them (run ``10959-9c95ea89``,
    2026-09-14: two recovered slots filled FLAT net +0, then the pair
    crash-reverted the same five binaries). A slot already in
    ``refill_members`` or still a stub is a rewrite in the same
    transaction. A filled slot the *current* crash extract named is
    also a rewrite (aimed), unless the last failed reply already
    emitted it: quoting a never-written callee is how run
    ``579131-0ef5e885`` fill 1 was forbidden from rewriting the
    callees ``crash_vtable_callees`` had already matched to the panic
    stem. A filled slot the last failed body already wrote stays
    quoted — run ``609480-8edf9bf2`` fill 1 aimed those slots, the
    reply rewrote them, and the keep crash-reverted; fill 2 then
    un-aimed them because the new panic stem no longer rematched.
    A filled slot that is only remembered, not in the current extract,
    stays quoted: forcing every remembered slot to refill is how run
    ``171582-cb964fb9`` spent 23 pair fills turning working table slots
    into ``None`` / a wild deref. Empty (the default) is every recorded
    pair that did not crash this way.
    """

    pair: FormatterPair
    dispatcher_block: ImplBlock
    implementer_block: ImplBlock
    extra_blocks: tuple[ImplBlock, ...] = ()

    def member_locators(self) -> frozenset[FnLocator]:
        extra = frozenset(
            FnLocator(b.file, m.fn_name)
            for b in self.extra_blocks
            for m in (*b.stub_members, *b.refill_members, *b.members))
        return self.pair.member_locators() | extra

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
        for b in (self.dispatcher_block, self.implementer_block,
                  *self.extra_blocks):
            seen: set[str] = set()
            for m in sorted(
                    (*b.stub_members, *b.refill_members, *b.members),
                    key=lambda rec: (b.file, rec.fn_name)):
                if m.fn_name in seen:
                    continue
                seen.add(m.fn_name)
                texts.append(m.body_text)
        return body_sha(texts)


def crash_last_error(preview: str, failed_body: str) -> str:
    """LAST_ERROR text for a pair whose last joint revert was a crash.

    Compile already quotes the rustc line as the approach not to resubmit.
    A crash summary names the binaries and not the bodies, so the next
    fill of run `186665-40892dbd` resubmitted fill 1's `by_ref` dispatcher
    after an honoured diagnosis that named a different pointer expression.
    The previous bodies are the approach the LAST_ERROR prefix's "do not
    resubmit the same approach" refers to. Empty ``failed_body`` is the
    preview alone (the pre-fix crash slot)."""
    preview = (preview or "").strip()
    body = (failed_body or "").strip()
    if not body:
        return preview
    block = "Your previous attempt wrote:\n```rust\n" + body + "\n```"
    return f"{preview}\n\n{block}" if preview else block


@dataclass(frozen=True, slots=True)
class PairLastError:
    """The next pair prompt's LAST_ERROR: the text AND what produced it.

    The kind is returned rather than left to be recovered from the text
    because recovering it from the text is the defect this type exists to
    close — the reader's substring and the renderer's wording drifted and
    no test joined them, so every compiling crash reached the prompt
    dressed as a rustc rejection (run `681204-b695ab3f`, 2026-09-14).
    """

    text: str
    kind: LastErrorKind


def pair_last_error(compile_err: str, preview: str, failed_body: str,
                    *, kind: LastErrorKind | None = None,
                    crash: str = "") -> PairLastError:
    """LAST_ERROR for the next pair fill.

    ``kind`` is the verdict the revert RECORDED, when the record carries
    one. Records written before the kind was stored pass ``None`` and are
    classified from the preview, which still carries the rendered head:
    the store's budget cuts the TAIL, and the head marks sit at the front.

    The stored preview is cut to a fixed character budget whose head grows
    with the number of binaries it names, so the directive at the end is
    what a wide crash loses: a five-binary preview of run
    `681204-b695ab3f` ended `they completed fine before thi` and carried
    no ask at all. The instruction is reconstructed from the kind, so a
    truncated excerpt still reaches the model as a situation WITH one.

    A compiling crash's rejected approach is the crashing body. A prior
    compile excerpt is a different body — rustc already rejected it.
    Concatenating the two is how the live pair prompt of 2026-09-14
    (run after e67f818, fill 3) showed E0308 as_ptr expected Ref found
    *mut next to a by_ref crash; the diagnostician classified WRONG_DEREF
    as_ptr, compiler_rejected retired that lesson, and the next fill
    resubmitted the crash.

    compiler_rejected still reads last_compile_error on the record, not
    this prompt string, so dropping the rustc lines here does not
    re-honour the rejected call. Empty parts are omitted; all-empty is
    "".

    ``crash`` is the crash the gate's settlement read (`PAIR_LAST_CRASH`),
    quoted whole after the excerpt and before the previous bodies.
    """
    crash = (crash or "").strip()
    if (preview or "").strip() or (failed_body or "").strip() or crash:
        resolved = resolve_kind(kind, preview)
        known = resolved.compiled
        if not known:
            # The crash slot is written only after a clean compile, so an
            # excerpt whose own text says nothing is still a RUN failure.
            # LOST_TESTS is the honest floor: compiled, with no stack this
            # prompt may point the model away from. The ask is NOT
            # restored in that case — putting a generic directive on an
            # excerpt nothing classified would be inventing evidence.
            resolved = LastErrorKind.LOST_TESTS
        # Restore the ask against the PREVIEW, so it stays with the
        # situation it belongs to rather than trailing the quoted body.
        head = _with_instruction(preview, resolved) if known else (
            (preview or "").strip())
        if crash:
            head = "\n\n".join(x for x in (
                head, "The crash the localizer read under that write:\n"
                + crash) if x)
        return PairLastError(crash_last_error(head, failed_body), resolved)
    err = (compile_err or "").strip()
    return PairLastError(err, LastErrorKind.COMPILE if err
                         else LastErrorKind.UNKNOWN)


def _with_instruction(text: str, kind: LastErrorKind) -> str:
    """Put back the directive a stored excerpt's character budget cut.

    No-op on an empty excerpt and on one that still carries the ask, so a
    full-length excerpt is unchanged and only a truncated one grows."""
    body = (text or "").strip()
    ask = instruction_for(kind)
    if not body or not ask or ask in body:
        return body
    return f"{body}\n{ask}"


def pair_record_container(pair: FormatterPair) -> str:
    """The container half of a pair record's retro-store key — both
    endpoints, so a pair's memory can never collide with either member
    block's own `*impl*` record."""
    d, i = pair.dispatcher, pair.implementer
    return f"{d.file}::{d.fn_name}+{i.file}::{i.fn_name}"


# Durable extra locators a compiling pair crash-revert recovered.
# Stamped on the pair retro record; a later pair-only write must not
# drop them (run `100178-9c95ea89` fill 1 joined two slots then
# compiled-and-reddened; fill 2's pair-only crash overwrote
# joint_members and crash_vtable saw no panic on fill 1's last
# record, so fill 2 dropped the slots).
PAIR_CRASH_CALLEES = "crash_callees"

# The crash the gate's settlement read under the pair's last reverted write
# (`crash_settler.crash_text_of`), stored whole: the preview beside it is cut
# to a budget, and the crash text a gate appends to its detail is what that
# budget cut first (run `42057-641e589c`, 2026-10-26: the pair's next prompt
# never carried the localized site). Rewritten on every revert, "" for one
# with no crash, so a stale crash never outlives the write it describes.
PAIR_LAST_CRASH = "last_crash"
#: The stored crash is a prompt section, not a store dump.
PAIR_LAST_CRASH_CAP = 4000


def locators_from_scope(names: Iterable[str]) -> frozenset[FnLocator]:
    """Parse ``file::fn`` strings (joint_members / crash_callees)."""
    out: set[FnLocator] = set()
    for raw in names:
        s = str(raw)
        if "::" not in s:
            continue
        file, fn = s.rsplit("::", 1)
        if file and fn:
            out.add(FnLocator(file, fn))
    return frozenset(out)


def fn_names_in_failed_body(body: str) -> frozenset[str]:
    """``<FN name="…">`` names a last pair reply actually emitted.

    Protocol parse of the stored failed body, not of crate source. An
    extra whose name is here was already a write in that reply; one
    whose name is absent was omitted (quoted, or the model skipped it).
    """
    out: set[str] = set()
    lead = '<FN name="'
    text = body or ""
    i = 0
    while True:
        start = text.find(lead, i)
        if start == -1:
            return frozenset(out)
        value = start + len(lead)
        end = text.find('"', value)
        if end in (-1, value):
            i = start + 1
            continue
        name = text[value:end].strip()
        if name:
            out.add(name)
        i = end + 1


def crash_named_extras(
    extract: Collection[FnLocator],
    *,
    failed_body: str = "",
) -> frozenset[FnLocator]:
    """Passing extras the current crash extract named, minus those the
    last failed reply already emitted.

    Extract-named extras the last reply omitted stay aimed (run
    ``579131-0ef5e885``: quoting forbade rewriting the callee the panic
    stem named). Extras the last failed body already wrote stay quoted
    (run ``609480-8edf9bf2`` fill 1 aimed them, the reply rewrote them,
    the keep crash-reverted; fill 2's rematch then used a different
    panic stem and un-aimed them). Empty extract is the remembered-only
    quote (run ``171582-cb964fb9``). Empty body leaves the extract
    unchanged — the first fill after a rematch, before any reply.
    """
    named = frozenset(extract)
    if not (failed_body or "").strip():
        return named
    emitted = fn_names_in_failed_body(failed_body)
    return frozenset(loc for loc in named if loc.fn_name not in emitted)


def remembered_pair_callees(
    rec: Mapping[str, object] | None,
    hop: Collection[FnLocator],
) -> frozenset[FnLocator]:
    """Table slots a pair record remembered, minus the hop endpoints."""
    if not rec:
        return frozenset()
    raw = rec.get(PAIR_CRASH_CALLEES) or ()
    if not isinstance(raw, (list, tuple)):
        return frozenset()
    return locators_from_scope(str(s) for s in raw) - frozenset(hop)


def union_pair_callees(
    rec: Mapping[str, object] | None,
    extras: Collection[FnLocator],
) -> list[str]:
    """Union of remembered slots and this write's extras, as ``file::fn``."""
    prev = remembered_pair_callees(rec, ())
    now = frozenset(extras) | prev
    return sorted(f"{k.file}::{k.fn_name}" for k in now)


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


def _block_for_locator(
    loc: FnLocator, all_blocks: Sequence[ImplBlock],
    *,
    crash_named: Collection[FnLocator] = (),
) -> ImplBlock | None:
    """The block declaring ``loc``, narrowed to that one member.

    Unlike :func:`_endpoint_block` this keys on file+name only, so a free
    fn (container ``""``) is not matched to the first impl in the file.
    Stub / refill classification is kept so the pair splice overwrites a
    filled slot and fills a still-hollow one.

    ``crash_named`` is the current compiling crash extract
    (``crash_vtable_callees`` of the last failed body and panic text),
    minus extras that body already emitted (see
    :func:`crash_named_extras`). A filled member in that set is a
    refill even when its attributed tests pass: those tests often bind
    the return and never format it, so a garbage value still passes
    them, and quoting the slot forbids the pair from rewriting the
    callee the panic stem already named (run ``579131-0ef5e885`` fill
    1). A filled member not in that set stays quoted — remembered-only
    extras, or extras the last crashing reply already wrote.
    """
    named = frozenset(crash_named)
    for b in all_blocks:
        if b.file != loc.file:
            continue
        stub = tuple(m for m in b.stub_members if m.fn_name == loc.fn_name)
        if stub:
            return replace(b, stub_members=stub, refill_members=(),
                           members=stub)
        refill = tuple(m for m in b.refill_members if m.fn_name == loc.fn_name)
        if refill:
            return replace(b, stub_members=(), refill_members=refill,
                           members=refill)
        member = tuple(m for m in b.members if m.fn_name == loc.fn_name)
        if member:
            if loc in named:
                return replace(b, stub_members=(), refill_members=member,
                               members=member)
            # Filled, and collect_impl_refill_targets did not stamp it:
            # its attributed tests already pass, and the current crash
            # extract did not name it. Quote it in the pair prompt so
            # the pair can call it; do not mark refill. Forcing every
            # remembered slot to refill is how run `171582-cb964fb9`
            # spent 23 pair fills rewriting working table slots to
            # `None` / a wild deref. pair_wait still parks a run of
            # this locator (it is in extra_blocks.members).
            return replace(b, stub_members=(), refill_members=(),
                           members=member)
    return None


def pairs_to_dispatch(
    pairs: Iterable[FormatterPair],
) -> list[FormatterPair]:
    """Every cross-file pair the hop resolved, deduped, in a deterministic
    order. The pair is its own dispatch unit: it does not wait for a
    member to survive the batch cut or standing-flat. Run
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


def extra_write_members(block: ImplBlock) -> tuple[FnRecord, ...]:
    """Stub and refill extras the pair splices. Members-only extras are
    quoted, not in this tuple."""
    return (*block.stub_members, *block.refill_members)


def extra_quoted_members(block: ImplBlock) -> tuple[FnRecord, ...]:
    """Filled extras whose tests already pass — shown, not rewritten."""
    written = {m.fn_name for m in extra_write_members(block)}
    return tuple(m for m in block.members if m.fn_name not in written)


def quote_dispatcher_when_implementer_entered(
    pd: PairDispatch,
    *,
    pair_tests: Collection[str],
    entered: Collection[str],
) -> PairDispatch:
    """A filled dispatcher is quoted, not rewritten, only when every
    leftover of the pair is proven entered on the implementer.

    Mixed leftovers keep both halves as refill: an implementer-only
    rewrite cannot cash the unentered tests, and quoting the dispatcher
    is how those tests became unpayable (run 524736-e3b0c853). Empty
    leftover set must not vacuously quote.

    Control: every leftover entered still quotes; every leftover
    unentered still joint-writes; mixed joint-writes. A still-stubbed
    dispatcher stays a write.
    """
    if not pair_tests or not all(t in entered for t in pair_tests):
        return pd
    d = pd.dispatcher_block
    if d.stub_members:
        return pd
    members = d.refill_members or d.members
    if not members:
        return pd
    return pd._replace(
        dispatcher_block=replace(
            d, stub_members=(), refill_members=(), members=members))


def pair_required_files(pd: PairDispatch) -> frozenset[str]:
    """Files the splice must land. A quoted half (members only) is not
    required — omitting its FN keeps the current body."""
    out: set[str] = set()
    for b in (pd.dispatcher_block, pd.implementer_block, *pd.extra_blocks):
        if extra_write_members(b):
            out.add(b.file)
    return frozenset(out)


def _as_loc(key: FnLocator | tuple[str, str]) -> FnLocator:
    if isinstance(key, FnLocator):
        return key
    return FnLocator(key[0], key[1])


def dispatch_including_pairs(
    eligible: Mapping[FnLocator, set[str]] | Mapping[tuple[str, str], set[str]],
    frontier_keys: Collection[FnLocator] | Collection[tuple[str, str]],
    pairs: Iterable[FormatterPair],
    convicting: Mapping[FnLocator, set[str]]
    | Mapping[FnSite, set[str]]
    | Mapping[tuple[str, ...], set[str]]
    | None = None,
) -> dict[FnLocator, set[str]]:
    """The fill's dispatch map: the 12-cap batch, plus every hop-pair
    member this fill will actually write.

    ``FailureFrontier.build`` drops conviction for a locator absent from
    dispatch, which is the right invariant for a unit the fill will not
    touch. ``pairs_to_dispatch`` no longer requires a batch member, so a
    standing-flat dispatcher plus an unentered implementer still opens
    as a pair — and that write then ran with an empty convicting set
    (run ``200802-b835274e``, 2026-09-10: pair prompt had no
    REFILL_EVIDENCE, joint-reverted on a hang). Adding the pair's
    members, with the tests the hop already attributed to either
    endpoint, is the conviction half of that dispatch. Same-file pairs
    are ignored (file-first already writes both). A pair that names no
    test on either endpoint is not added — empty dispatch would reopen
    the accept-on-no-tests hole.
    """
    elig_by_loc = {_as_loc(key): set(tests) for key, tests in eligible.items()}
    keys = {_as_loc(k) for k in frontier_keys}
    out: dict[FnLocator, set[str]] = {
        loc: set(tests) for loc, tests in elig_by_loc.items() if loc in keys
    }
    conv = convicting or {}
    for p in pairs_to_dispatch(pairs):
        members = p.member_locators()
        tests: set[str] = set()
        for loc in members:
            tests |= elig_by_loc.get(loc, set())
        for ckey, ctests in conv.items():
            site = key_as_site(ckey)
            if FnLocator(site.file, site.fn_name) in members:
                tests |= set(ctests)
        if not tests:
            continue
        for loc in members:
            out.setdefault(loc, set()).update(tests)
    return out


def pair_dispatches(
    pairs: Iterable[FormatterPair],
    all_blocks: Sequence[ImplBlock],
    extra_locators: Collection[FnLocator] = (),
    crash_named: Collection[FnLocator] = (),
) -> list[PairDispatch]:
    """The hop's dispatchable pairs with both endpoint blocks resolved.
    A pair either of whose endpoints has no collected block (record drift,
    a free fn) is dropped — the ordinary routes still own its members.

    ``extra_locators`` whose file is a pair member's file join that pair's
    write set (same-file table slots a compiling crash-revert recovered).
    Empty (the default) leaves every recorded pair a two-member
    transaction. A locator in a third file is skipped here: the pair
    writes two files, and that run stays crash-blamed so pair_wait does
    not park it.

    ``crash_named`` is the current crash extract after
    :func:`crash_named_extras`. A passing extra in that set is a refill
    (aimed); a passing extra only in ``extra_locators`` stays quoted.
    Empty (the default) quotes every passing extra — the pre-fix
    behaviour, the remembered-only case, and extras the last failed
    body already wrote.
    """
    extras = frozenset(extra_locators)
    named = frozenset(crash_named)
    out: list[PairDispatch] = []
    for p in pairs_to_dispatch(pairs):
        db = _endpoint_block(p.dispatcher, all_blocks)
        ib = _endpoint_block(p.implementer, all_blocks)
        if db is None or ib is None:
            continue
        member_files = {p.dispatcher.file, p.implementer.file}
        member_locs = p.member_locators()
        extra_blocks: list[ImplBlock] = []
        seen: set[FnLocator] = set()
        for loc in sorted(extras, key=lambda k: (k.file, k.fn_name)):
            if (loc.file not in member_files or loc in member_locs
                    or loc in seen):
                continue
            blk = _block_for_locator(loc, all_blocks, crash_named=named)
            if blk is None:
                continue
            seen.add(loc)
            extra_blocks.append(blk)
        out.append(PairDispatch(p, db, ib, tuple(extra_blocks)))
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
