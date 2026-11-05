"""Who a crash under a written body convicts, when other unproven bodies are
on its stack (pure domain).

The state this module exists for: a body kept while its tests could not run
(they stopped in a still-stubbed function) stands on the tree with no evidence
that it is right. When that stub is later written, the stub's candidate is the
first body under which those tests run, so a defect in the kept body surfaces
as a crash under the candidate. Run `1057870-b427a3a2` (2026-10-15): the
kept `construct_from_std` called the `vtable` accessor on
`NonNull::dangling()`; every correct accessor body segfaulted reading
address 0x8, the per-fn judge convicted the accessor four attempts running,
and the constructor was never offered again because its tests stopped in the
accessor's stub.

The question is answered on a measurement, never on standing alone: the
crash site's culprit (`CrashSite.culprit_frames` — the origin the detector
reported, the frame `traced_origin_index` traced the address to, or a
recursion's cycle). Standing decides only what may be put back to a stub:

- a culprit inside the written set convicts the write (OWN);
- culprits all outside it, none of them PROVEN, are the bodies to put back to
  stubs, and the write stands while the suite is re-run without them (STUB);
- a site that blames nobody, or blames a body the ledger has proven, names
  nothing to put back; it is carried to the reading taken without the stubs
  the other sites justify, and when it is still there (or there were no
  such stubs) the write is judged as before (UNSETTLED).

A PROVEN body is never put back: its tests ran and passed with it on the
tree, and a stub would lose them. A body whose standing is anything else has
no evidence a stub can lose, which is what makes putting it back safe; the
caller still re-measures the whole suite and gives the stubs back when any
test that passed before the write is lost.

OWN is a verdict on one body, not on the write. When the write is several
bodies and the culprit is a proper subset of them, the caller that can say
what each body replaced (`crash_settler.JudgedWrite`) withdraws the culprits
back to their pre-write text and the rest of the write is judged without
them (NARROWED). Run `757853-e28f1a7d` (2026-10-16, iter 4): a four-body
`src/error.rs` file-first read `pass 59 -> 64`, then `59 -> 65`, and was
restored whole both times because one of the four, `into_boxed_dyn_error`,
was the double drop the sanitizer named; the settlement had that culprit and
nothing read it.
"""
from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from pipeline_core.domain.crash_site import SETTLEABLE_FAULTS, CrashFrame, CrashSite
from pipeline_core.domain.models import FnLocator


class SettlementOutcome(StrEnum):
    NO_CRASH = "no_crash"
    """No crash left to read: the write is judged by its tests as usual."""
    OWN = "own"
    """A crash's culprit is a body the write wrote."""
    STUB = "stub"
    """Every crash's culprits are unproven bodies outside the write."""
    UNSETTLED = "unsettled"
    """A crash blames nobody, or blames a proven body outside the write."""
    SETTLED = "settled"
    """The culprits went back to stubs, the suite no longer crashes on a new
    binary, and no test that passed before the write was lost."""
    NARROWED = "narrowed"
    """The write's own culprits went back to their pre-write text, nothing
    outside it was put back, and the suite no longer crashes on a new binary:
    the rest of the write is judged as written."""


_SETTLEABLE = SETTLEABLE_FAULTS


def frame_locator(frame: CrashFrame) -> FnLocator:
    return FnLocator(frame.file_rel, frame.fn_name)


@dataclass(frozen=True, slots=True)
class CarriedSite:
    """A crash of the reading that names no body this step may act on, and
    why: the settlement re-reads without the stubs it does make, and this
    crash has to be gone from that reading for anything to be committed."""

    site: CrashSite
    reason: str


@dataclass(frozen=True, slots=True)
class SettlementStep:
    outcome: SettlementOutcome
    culprits: tuple[CrashFrame, ...] = ()
    reason: str = ""
    #: STUB only: the reading's crashes this step could not act on.
    carried: tuple[CarriedSite, ...] = ()


def _unactionable(
    site: CrashSite, is_proven: Callable[[FnLocator], bool],
) -> tuple[str, tuple[CrashFrame, ...]] | None:
    """Why a site outside the write names no body a stub may replace, with
    the proven culprits when that is the reason; None when it does. The
    site's own half is :meth:`CrashSite.producer_refusal`, the reading
    proven standing shares (`body_standing.proven_units`); this adds only
    the proven check, which standing cannot read because it computes it."""
    refusal = site.producer_refusal()
    if refusal is not None:
        return refusal, ()
    culprits = site.culprit_frames
    proven = tuple(f for f in culprits if is_proven(frame_locator(f)))
    if proven:
        return (f"{site.key}: the culprit {proven[0].file_rel}::"
                f"{proven[0].fn_name} is proven on this tree"), proven
    return None


def settle_step(
    sites: Sequence[CrashSite],
    written: Collection[FnLocator],
    is_proven: Callable[[FnLocator], bool],
) -> SettlementStep:
    """One reading's verdict. ``sites`` are the localized crashes of binaries
    that did not crash before the write (the caller filters the rest).

    Every site is read before the step decides:

    - a culprit inside the write, on any site, convicts it (OWN);
    - otherwise the unproven culprits outside the write that any site names
      are put back (STUB), and the sites that name no such body ride along
      as ``carried``: blames nobody, a fault that is not settled, a trace
      that stops at the reader, a proven culprit;
    - a reading of carried sites alone is UNSETTLED.

    A carried crash does not stop the stubs the other sites justify: the
    caller re-reads the suite without them, and a carried crash still there
    is a site of that reading, so a reading of nothing else ends UNSETTLED
    and every stub is given back. Nothing is committed while a crash of the
    write remains unexplained, which is what the rule this replaced
    protected, but a crash that goes away with the attributable culprits is
    no longer a veto on them. Until 2026-10-23 this returned at the first
    such site: run `79929-5a16b1d7`'s destructor write read `pass 100 ->
    132`, `100 -> 117` and was reverted seven times across three routes
    because `tests/test_convert.rs` blamed nobody, while five sites of the
    same readings named the two Box conversions the destructor had made
    reachable. That return also hid an OWN on any later site."""
    if not sites:
        return SettlementStep(SettlementOutcome.NO_CRASH)
    written_set = frozenset(written)
    own: list[tuple[CrashSite, CrashFrame]] = []
    outside: list[CrashFrame] = []
    carried: list[CarriedSite] = []
    proven: list[CrashFrame] = []
    for site in sites:
        mine = [f for f in site.culprit_frames
                if frame_locator(f) in written_set]
        if mine and site.fault in _SETTLEABLE:
            own.extend((site, f) for f in mine)
            continue
        why = _unactionable(site, is_proven)
        if why is not None:
            carried.append(CarriedSite(site, why[0]))
            proven.extend(why[1])
            continue
        outside.extend(site.culprit_frames)
    if own:
        site0, f0 = own[0]
        return SettlementStep(
            SettlementOutcome.OWN, tuple(_distinct(f for _s, f in own)),
            reason=(f"{site0.key}: the culprit is the written body "
                    f"{f0.fn_name} ({f0.file_rel}:{f0.line})"))
    if outside:
        return SettlementStep(SettlementOutcome.STUB,
                              tuple(_distinct(outside)),
                              carried=tuple(carried))
    return SettlementStep(SettlementOutcome.UNSETTLED,
                          tuple(_distinct(proven)),
                          reason=carried[0].reason)


def _distinct(frames: Iterable[CrashFrame]) -> list[CrashFrame]:
    seen: set[tuple[str, str, int]] = set()
    out: list[CrashFrame] = []
    for f in frames:
        k = (f.file_rel, f.fn_name, f.line)
        if k not in seen:
            seen.add(k)
            out.append(f)
    return out


def occurrence_of_frame(
    spans: Sequence[tuple[int, int]], text: str, line: int,
) -> int | None:
    """Index of the occurrence a frame ran.

    A positive line is the span that holds it
    (:func:`occurrence_of_line`). A frame recovered from caller source has
    line 0: the stack named the function and not a source line. When that
    name occurs once in the file, that occurrence is the body. A shared
    name with no line stays unresolved — nothing picks one of them."""
    if line > 0:
        return occurrence_of_line(spans, text, line)
    if len(spans) == 1:
        return 0
    return None


def occurrence_of_line(
    spans: Sequence[tuple[int, int]], text: str, line: int,
) -> int | None:
    """Which of a function name's occurrences in ``text`` holds ``line``
    (1-based): the index into ``spans`` (character ``start, end`` of each
    occurrence, signature through body) of the narrowest span containing the
    line's first character. A frame names a body by location; a locator
    names every body sharing the identifier (`from` under three impls), and
    only the one the stack ran may go back to a stub."""
    if line <= 0:
        return None
    offset = 0
    for _ in range(line - 1):
        nl = text.find("\n", offset)
        if nl == -1:
            return None
        offset = nl + 1
    best: int | None = None
    best_len = -1
    for i, (start, end) in enumerate(spans):
        if start <= offset <= end and (best is None or end - start < best_len):
            best, best_len = i, end - start
    return best


def refuted_crash_note(body: str, evidence: str, iteration: int,
                       *, unit: str = "", max_body: int = 1500) -> str:
    """What the next writer of a unit is told about the body a crash
    settlement took off it: the body, and the crash as the localizer read it.

    It says only what the row records. The settlement put that body back to
    a stub (STUB) or to the unit's earlier text (NARROWED). The wording
    names neither, so it cannot lie about which of the two the tree holds.
    The caller omits the note when the body on the tree is the quoted one
    (`refuted_notes`): that equality is a later keep of those bytes, and on
    the settlement itself the committed text and the quoted body differ.
    Run `757853-e28f1a7d` (2026-10-16): a stub's iter-4 prompt said only
    that the fn was a stub, and the reply wrote the same double drop three
    times. Run `630350-93c5a6ee` (2026-11-05): a filled unit's settled body
    reached no later prompt at all (`refuted_body`)."""
    shown = body.strip()
    if len(shown) > max_body:
        shown = shown[:max_body].rstrip() + "\n// …"
    who = f"`{unit}`" if unit else "this fn"
    lines = [
        f"At iteration {iteration} a body of {who} was taken off the tree: "
        f"a test process crashed on it. That body was:",
        shown,
    ]
    if evidence.strip():
        lines += ["The crash it caused, as the localizer read it:",
                  evidence.strip()]
    lines.append("Do not write that body again.")
    return "\n".join(lines)


def with_held_stub_evidence(
    evidence: Mapping[FnLocator, str] | None,
    held: Iterable[tuple[FnLocator, Collection[str]]],
) -> dict[FnLocator, str]:
    """`evidence` with the held-stub sentence (:func:`held_stub_evidence`)
    for every stub in `held` that holds tests and has no text yet. The sweep
    writes the sentence for the stubs it held; a run opened on a blocker the
    fill measured carries its own ground (`RouteRun.held_tests`) and reaches
    the per-fn passes through this, so its ask states the same fact. A text
    already present wins, as on the sweep (`setdefault`): a crash reading is
    stronger evidence than a count."""
    out = dict(evidence or {})
    for loc, tests in held:
        if tests:
            out.setdefault(loc, held_stub_evidence(len(tests)))
    return out


def held_stub_evidence(held_tests: int) -> str:
    """CRASH_EVIDENCE for a still-stubbed fn that holds ``held_tests``
    failing tests at its `unimplemented!()`. What a crash settlement took
    off the stub is not here: it is its own section for every unit, filled
    or stubbed (`refuted_body.refuted_text_for`), because this text reaches
    a writer through `CrashSites.evidence_for`, which drops it once a frame
    of the name resolved."""
    return (f"This fn is still a stub, and {held_tests} failing "
            "test(s) stop at its `unimplemented!()` before they check "
            "anything else. No other body can move those tests until "
            "this one exists. Give it a real body.")
