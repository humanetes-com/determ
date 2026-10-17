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
- anything else — a site that blames nobody, or blames a body the ledger has
  proven — cannot be settled here, and the write is judged as before
  (UNSETTLED).

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

from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from pipeline_core.domain.crash_site import CrashFrame, CrashSite, FaultClass
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


_SETTLEABLE = frozenset({FaultClass.INVALID_MEMORY, FaultClass.EXHAUSTION})


def frame_locator(frame: CrashFrame) -> FnLocator:
    return FnLocator(frame.file_rel, frame.fn_name)


@dataclass(frozen=True, slots=True)
class SettlementStep:
    outcome: SettlementOutcome
    culprits: tuple[CrashFrame, ...] = ()
    reason: str = ""


def settle_step(
    sites: Sequence[CrashSite],
    written: Collection[FnLocator],
    is_proven: Callable[[FnLocator], bool],
) -> SettlementStep:
    """One reading's verdict. ``sites`` are the localized crashes of binaries
    that did not crash before the write (the caller filters the rest)."""
    if not sites:
        return SettlementStep(SettlementOutcome.NO_CRASH)
    written_set = frozenset(written)
    outside: list[CrashFrame] = []
    for site in sites:
        if site.fault not in _SETTLEABLE:
            # A runtime abort (a panic while unwinding) is sited where it
            # happened and is the double-panic renderer's question; only a
            # fault whose culprit is a body that made bad memory or a cycle
            # can be settled against that body.
            return SettlementStep(
                SettlementOutcome.UNSETTLED,
                reason=f"{site.binary_label}: {site.fault} is not settled")
        culprits = site.culprit_frames
        if not culprits:
            return SettlementStep(
                SettlementOutcome.UNSETTLED,
                reason=f"{site.binary_label}: {site.blame_label}")
        own = [f for f in culprits if frame_locator(f) in written_set]
        if own:
            return SettlementStep(
                SettlementOutcome.OWN, tuple(own),
                reason=(f"{site.binary_label}: the culprit is the written "
                        f"body {own[0].fn_name} ({own[0].file_rel}:"
                        f"{own[0].line})"))
        proven = [f for f in culprits if is_proven(frame_locator(f))]
        if proven:
            return SettlementStep(
                SettlementOutcome.UNSETTLED, tuple(proven),
                reason=(f"{site.binary_label}: the culprit "
                        f"{proven[0].file_rel}::{proven[0].fn_name} is proven "
                        "on this tree"))
        outside.extend(culprits)
    return SettlementStep(SettlementOutcome.STUB,
                          tuple(_distinct(outside)))


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
                       *, max_body: int = 1500) -> str:
    """What the next writer of a stub is told about the body a crash
    settlement took off it: the body, and the crash as the localizer read it.

    The settlement's refutation is a ledger row whose only reader was the
    reseed gate. Run `757853-e28f1a7d` (2026-10-16): iter 3 settled on
    `into_boxed_dyn_error` and put it back to a stub; the iter-4 prompt's
    CRASH_EVIDENCE said only that the fn was a stub holding three tests, and
    the reply wrote the same double drop three times."""
    shown = body.strip()
    if len(shown) > max_body:
        shown = shown[:max_body].rstrip() + "\n// …"
    lines = [
        f"A body of this fn was put back to this stub at iteration "
        f"{iteration}: a test process crashed on it. That body was:",
        shown,
    ]
    if evidence.strip():
        lines += ["The crash it caused, as the localizer read it:",
                  evidence.strip()]
    lines.append("Do not write that body again.")
    return "\n".join(lines)


def held_stub_evidence(held_tests: int, refuted_body: str = "",
                       refuted_evidence: str = "",
                       refuted_iteration: int = 0) -> str:
    """CRASH_EVIDENCE for a still-stubbed fn that holds ``held_tests``
    failing tests at its `unimplemented!()`, and — when a crash settlement
    made it a stub — what the body it took off did (`refuted_crash_note`)."""
    text = (f"This fn is still a stub, and {held_tests} failing "
            "test(s) stop at its `unimplemented!()` before they check "
            "anything else. No other body can move those tests until "
            "this one exists. Give it a real body.")
    if refuted_body.strip():
        text += "\n\n" + refuted_crash_note(
            refuted_body, refuted_evidence, refuted_iteration)
    return text
