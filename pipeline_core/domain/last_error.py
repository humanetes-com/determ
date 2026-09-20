"""What produced a LAST_ERROR excerpt — the fact, carried as data.

A fill's LAST_ERROR is either a body rustc REJECTED or a body rustc
accepted that then failed when the tests ran. Everything downstream turns
on that difference: a rejected body is answered with "these errors — do
not resubmit the same approach", an accepted-then-faulting body has to be
told not to re-emit the call that faulted, and only the second class may
drop the joint-write sentence that tells a pair's model the implementer is
unentered because the dispatcher does not call it yet.

Until 2026-09-14 that difference was recovered by grepping the rendered
excerpt. The renderer (`domain.test_results.render_regression_detail`)
writes ``test binaries [...] CRASH before reporting results``; the reader
(`prompts.per_impl.last_error_is_compiling_crash`) looked for the
substring ``binaries crashed``. The binary list sits between the two words
the reader wanted adjacent, so the reader answered False on every crash
the renderer wrote. Measured on run `681204-b695ab3f` (launched 19:56:23
UTC under the code that shipped the crash wording): its 20:05:39 pair
prompt carried the compile prefix and the unentered-arm sentence, and the
predicate returned False on that prompt's own LAST_ERROR. 37 of that day's
50 sibling-regression reverts were this form.

So the kind is decided where the failure is MEASURED and travels with the
excerpt. The text heuristic below stays only for an excerpt that reaches a
prompt with no provenance (a record written before the kind was stored),
and it keys on the marks this module also hands the renderer, so the two
cannot drift without the contract test noticing.

The instruction sentences live here for the same reason. A stored excerpt
is cut to a fixed character budget, and the head's length grows with the
number of binaries named in it: the pair record's 240-char preview of a
five-binary crash ended ``they completed fine before thi`` and dropped
``Write a body that lets every test binary build and run to completion.``
entirely. The directive a truncation eats is the one part of the excerpt
that says what to do, so it is reconstructible from the kind alone.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum

# The distinctive phrase each rendered head carries. The renderer builds
# its heads from these, and `kind_from_text` recognises them, so a wording
# change moves both sides at once. Never inline one of these at a call
# site — that is the copy the grep-the-prose defect was made of.
CRASH_HEAD_MARK = "CRASH before reporting results"
HANG_HEAD_MARK = "HANG: the run was cut by the timeout"
RED_HEAD_MARK = "REGRESSED previously-passing test(s)"
EXECUTED_DROP_MARK = "dropped the number of tests that EXECUTE"
PASS_DROP_MARK = "dropped the cluster pass count"

# What a rustc diagnostic looks like, for the no-provenance fallback only.
_COMPILE_MARK = "error[e"
# A runtime fault quotes the panic line, whatever produced it.
_RUNTIME_MARKS = ("panicked at", "addresssanitizer")


class LastErrorKind(Enum):
    """How the previous attempt failed.

    ``UNKNOWN`` means no provenance travelled with the excerpt — read the
    text. Every other member is a claim made by the code that measured the
    failure, and outranks anything the prose says.
    """

    UNKNOWN = "unknown"
    #: rustc rejected the body; it never ran.
    COMPILE = "compile"
    #: Compiled; a binary aborted before reporting results.
    CRASHED = "crashed"
    #: Compiled; a binary never returned and the run was cut by the timeout.
    HUNG = "hung"
    #: Compiled; previously-passing tests went red.
    REDDENED = "reddened"
    #: Compiled; fewer tests executed, or the pass count fell, with no
    #: crash, hang or named red test to point at.
    LOST_TESTS = "lost_tests"

    @property
    def compiled(self) -> bool:
        """True when rustc accepted the body and the failure is a RUN.

        ``UNKNOWN`` is False: with no provenance the caller must consult
        :func:`kind_from_text` rather than assume either way.
        """
        return self in (LastErrorKind.CRASHED, LastErrorKind.HUNG,
                        LastErrorKind.REDDENED, LastErrorKind.LOST_TESTS)

    @property
    def has_faulting_call(self) -> bool:
        """True when the evidence names a call the body must not re-emit.

        A crash or a red assertion gives a stack; a hang gives the call
        that recurses. A plain loss of executed tests gives neither, so a
        prompt must not order the model away from an expression the
        evidence never named.
        """
        return self in (LastErrorKind.CRASHED, LastErrorKind.HUNG,
                        LastErrorKind.REDDENED)


def instruction_for(kind: LastErrorKind) -> str:
    """The directive that belongs with a failure of this kind.

    Rendered into the excerpt by the producer AND recoverable from the
    kind alone, so a stored excerpt cut to a character budget can have it
    put back rather than reaching a prompt as a situation with no ask.

    These are the sentences the renderer emits, not paraphrases of them:
    the renderer builds its heads from this function, so a restored
    instruction is byte-identical to the one the budget cut off.
    """
    if kind is LastErrorKind.CRASHED:
        return ("Write a body that lets every test binary build and "
                "run to completion.")
    if kind is LastErrorKind.HUNG:
        return ("Write a body that terminates: a Display/Debug body must "
                "format the inner value's own fields or delegate to a "
                "DIFFERENT impl, never to the value being formatted.")
    if kind is LastErrorKind.REDDENED:
        return ("Keep those tests passing while fixing the target — do not "
                "change shared behavior they rely on.")
    if kind is LastErrorKind.LOST_TESTS:
        return "Write a body that lets every test build and run."
    return ""


def kind_of_compiled_run(*, aborted: bool = False,
                         hung: bool = False) -> LastErrorKind:
    """Kind for an attempt rustc accepted.

    The per-fn loop already stores TEST vs ABORT vs COMPILE on the last
    attempt; prompt builders consume LastErrorKind, so a TEST retry is
    never described as cargo-check. Hang outranks abort.
    """
    if hung:
        return LastErrorKind.HUNG
    if aborted:
        return LastErrorKind.CRASHED
    return LastErrorKind.REDDENED


def faulted_clause(kind: LastErrorKind) -> tuple[str, str, str]:
    """(what the run did, the evidence below, the evidence just named).

    One vocabulary for the LAST_ERROR prefix and the pair joint-write
    paragraph, so a hang is never described as a panic.
    """
    if kind is LastErrorKind.HUNG:
        return ("never returned",
                "the call the evidence below shows it re-entered",
                "the call that evidence shows it re-entered")
    return ("panicked",
            "the call the stack below shows reached the panic",
            "the call that stack shows reached the panic")


def last_error_prefix(last_error: str,
                      kind: LastErrorKind | None = None) -> str:
    """LAST_ERROR lead-in. A body that ran and faulted names the faulting
    call; a compile excerpt keeps the older errors / same-approach
    wording."""
    resolved = resolve_kind(kind, last_error)
    if resolved.has_faulting_call:
        did, below, _that = faulted_clause(resolved)
        return (
            f"Your previous attempt compiled and then {did} — do not "
            f"emit {below}:\n"
        )
    return (
        "Your previous attempt at this block produced these errors — "
        "the bodies below must resolve them; do not resubmit the "
        "same approach:\n"
    )


def kind_from_text(text: str) -> LastErrorKind:
    """Classify an excerpt that arrived with no provenance.

    The fallback, never the primary reader. Ordered as the renderer's own
    branches are: a crash head outranks a hang head outranks a red head,
    because a run that crashed also lost the verdicts that would have
    named red tests. A rustc diagnostic with no runtime evidence beside it
    is ``COMPILE``; anything unrecognised stays ``UNKNOWN`` so a caller
    can tell "no evidence" from "evidence that says it compiled".
    """
    body = (text or "").lower()
    if not body.strip():
        return LastErrorKind.UNKNOWN
    if CRASH_HEAD_MARK.lower() in body:
        return LastErrorKind.CRASHED
    if HANG_HEAD_MARK.lower() in body:
        return LastErrorKind.HUNG
    if RED_HEAD_MARK.lower() in body:
        return LastErrorKind.REDDENED
    if EXECUTED_DROP_MARK.lower() in body or PASS_DROP_MARK.lower() in body:
        return LastErrorKind.LOST_TESTS
    if _COMPILE_MARK in body and not any(m in body for m in _RUNTIME_MARKS):
        return LastErrorKind.COMPILE
    if any(m in body for m in _RUNTIME_MARKS):
        # A panic line with no rendered head: the body ran, and the only
        # thing the text supports is that a test faulted.
        return LastErrorKind.REDDENED
    return LastErrorKind.UNKNOWN


# Retro-store field: the kind a revert recorded, beside the excerpt it
# cut to a character budget. Records written before this field existed
# simply lack it and fall back to `kind_from_text`.
LAST_ERROR_KIND = "last_error_kind"


def kind_from_record(record: object) -> LastErrorKind | None:
    """The kind a store record carries, or ``None`` when it carries none.

    ``None`` and an unrecognised value are the same answer — read the
    text — so a store written by an older build, or hand-edited, degrades
    to the fallback instead of raising.
    """
    if not isinstance(record, Mapping):
        return None
    raw = record.get(LAST_ERROR_KIND)
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return LastErrorKind(raw)
    except ValueError:
        return None


def resolve_kind(kind: LastErrorKind | None, text: str) -> LastErrorKind:
    """The kind to act on: the carried fact when there is one, else the
    text. ``UNKNOWN`` in, text out — that is what UNKNOWN means."""
    if kind is not None and kind is not LastErrorKind.UNKNOWN:
        return kind
    return kind_from_text(text)
