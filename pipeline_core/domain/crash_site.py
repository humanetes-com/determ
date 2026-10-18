"""Crash localization (pure domain).

A test binary that dies mid-run (SIGSEGV / abort) takes every unreported
test's verdict with it — the sweep sees WHICH tests vanished but not WHY.
Reverse-mapping vanished tests to fns is weak evidence (a test statically
reaches dozens of fns); the faulting STACK is exact. These types carry a
debugger backtrace as typed evidence: the innermost in-crate frame names the
fn whose body must change, outranking any test-name correlation during
attribution, and the rendered form is injected into that unit's fill prompt
so the model sees the actual crash path instead of guessing from a vanished
test list.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum

from pipeline_core.domain.abort_cause import AbortCause, AbortReading
from pipeline_core.domain.attribution import container_key
from pipeline_core.domain.generation import (
    is_gen_tests_sibling_name,
    parse_gen_tests_integration_filename,
)
from pipeline_core.domain.impl_routing import norm_src_rel
from pipeline_core.domain.models import FnLocator, FnRecord, FnSite


@dataclass(frozen=True, slots=True)
class CrashFrame:
    """One backtrace frame inside the crate under reconstruction.

    ``fn_name`` is the bare fn identifier (last path segment of the symbol,
    generics stripped) so it matches extractor records; ``symbol`` keeps the
    full demangled path for prompt display."""

    file_rel: str
    line: int
    fn_name: str
    symbol: str
    # When this frame was recovered from a caller source line that is not
    # itself a crate frame of the named fn (inlined conversion, From wrapper),
    # this is that (file, line). Render uses it instead of fabricating `:0`.
    named_by: tuple[str, int] | None = None


# Signals that mean "memory was used after it stopped being valid". For this
# class the faulting frame is routinely INNOCENT — it merely reads a pointer
# some other fn fabricated — so a backtrace alone points at the wrong fn to fix
# (live: a dangling stack vtable made `Drop::drop` fault while the defect sat in
# the constructor that built the vtable on its stack frame). Only a detector
# that reports where the memory ORIGINATED can name the culprit. Every
# AddressSanitizer verdict is in this class by construction — that is what the
# sanitizer detects — so the substring covers `heap-use-after-free`,
# `stack-use-after-return` and the rest without enumerating them.
_MEMORY_FAULT_SIGNALS = (
    "SIGSEGV", "SIGBUS", "SIGILL", "SIGABRT", "ADDRESSSANITIZER")


class FaultClass(Enum):
    """What a crash report's KIND says about where the defect lives.

    The blame rules differ by class, and one catch-all answered for all of
    them: `_MEMORY_FAULT_SIGNALS` lists `ADDRESSSANITIZER` because a use-after-
    free is "in this class by construction", and a stack-overflow report is
    an AddressSanitizer verdict too. Read as invalid memory, the faulting
    frames of an unbounded recursion were "routinely an innocent reader", the
    report's missing origin stack was "unattributed", and the loop blamed
    NOBODY for 13 of the last 14 failures of run `38934-2999cc94` (2026-09-26,
    sweeps 5-7) while the report held the whole cycle — 81-123 repeats of two
    or three in-crate fns — and named the two units that were rewritten
    around it for three fills and never opened.

    INVALID_MEMORY — the faulting frame reads memory another fn invalidated;
        the culprit is on an origin stack (freed by / allocated by), and an
        origin the detector cannot place is unattributed. The reading every
        rule of this module was written for.
    EXHAUSTION — a bounded resource ran out in the faulting stack itself
        (the sanitizer's `stack-overflow`). Nothing was invalidated and no
        origin stack exists to look for: the faulting stack IS the defect
        and its repeating in-crate frames are the culprits, jointly.
    OTHER — a stub's abort, a panic, a signal that names no memory class: the
        faulting frame is the site.

    The class is read once, from the detector's own kind string, by
    :func:`fault_class`; every consumer asks the site (`CrashSite.fault`)
    instead of matching the string again."""

    INVALID_MEMORY = "invalid_memory"
    EXHAUSTION = "exhaustion"
    OTHER = "other"


#: The sanitizer's kind for unbounded stack use. Not a substring of any memory
#: class, and checked before the ADDRESSSANITIZER catch-all.
_EXHAUSTION_SIGNALS = ("STACK-OVERFLOW",)


def fault_class(signal: str, abort: AbortReading | None = None) -> FaultClass:
    """The :class:`FaultClass` of a fault, most specific reading first: a
    sanitizer's own verdict (its kind string is a detector's diagnosis), then
    the cause the test's own process announced (`abort_cause`), then the
    bare signal. A debugger reports a stack overflow as `SIGSEGV` and a panic
    in a destructor as `SIGABRT`; read off the signal alone, both were
    invalid memory, and the runtime had said which one it was in the same
    test's block."""
    up = signal.upper()
    if any(s in up for s in _EXHAUSTION_SIGNALS):
        return FaultClass.EXHAUSTION
    if "SANITIZER" in up:
        return FaultClass.INVALID_MEMORY
    if abort is not None and abort.cause is AbortCause.STACK_EXHAUSTION:
        return FaultClass.EXHAUSTION
    if abort is not None and abort.cause is AbortCause.PANIC_IN_PANIC:
        return FaultClass.OTHER
    if any(s in up for s in _MEMORY_FAULT_SIGNALS):
        return FaultClass.INVALID_MEMORY
    return FaultClass.OTHER


def is_memory_fault_signal(signal: str) -> bool:
    """Whether ``signal`` names a use-of-invalid-memory class — the class for
    which the faulting frame is routinely an innocent reader. A stack
    exhaustion is not one (see :class:`FaultClass`)."""
    return fault_class(signal) is FaultClass.INVALID_MEMORY


# Blame provenance. A frame-derived blame is an OBSERVED fault location — a
# stack frame a detector printed. A chain-derived blame is an INFERENCE: the
# fns on the construction chain the crashed test exercises, synthesized only
# when an origin-capable detector looked and placed nothing in-crate. The two
# must never be confusable, because they warrant different force: frame blame
# may convict, crash-block and aim the layout walk; chain blame only prompts
# and voids exhaustion. It does NOT re-aim the layout walk: the retrigger
# grades a component OWNS against frame provenance only. Letting it read
# chain blame is measured — iter 3 of run `1577437-c93d5c26` (2026-09-06)
# graded 8 components OWNS off 50 chain-blamed fns (`src/fmt.rs::write_str`
# alone put `Quoted` in the tier), fired attempt 2 on `Quoted`, re-authored a
# type 0 of 373 fns reference, and reset the stall budget and the pass
# cursor. Measured case for the second kind: the
# deterministic `test_downcast` heap-use-after-free (run `635043-5f7c61f5`,
# 2026-08-14, `logs/asan/asan-20260814T025642115609.txt` report at line 3877)
# — access, freed-by and allocated-by stacks all resolve to std/test frames
# only, because an Arc refcount error planted in the crate's construct/vtable
# machinery surfaces later, in std's `Arc::drop` at the test's own drop
# sites. No widening of the frame parse can name crate code that is not in
# any stack.
PROVENANCE_FRAME = "frame_derived"
PROVENANCE_CHAIN = "chain_derived"


def authorable_frames(
    frames: tuple[CrashFrame, ...],
) -> tuple[CrashFrame, ...]:
    """`frames` minus the pipeline's OWN generated test siblings.

    The in-crate filter keeps a frame iff its file starts with `src/`, and the
    generated tests live at `src/<stem>.gen_tests_<N>.rs` — so a frame in a
    crashing test's own body passes it. That is not a bug in the filter: those
    frames ARE the access path, they belong in the rendered evidence, and
    `crash_evidence_by_locator` should keep keying by them. It is a bug
    wherever the frame becomes BLAME, because blame is a claim about a body
    the loop can rewrite and no fill ever writes a generated test.

    Measured, run `263217-d41a5bb5` (2026-08-15): the only two frames in the
    whole ASAN report were `src/error.gen_tests_4.rs:14
    test_reallocate_into_boxed_dyn_error_without_backtrace` and its
    `{closure#0}` at `:11`. That report's origin was unattributed, so nothing
    was blamed and the chain-derived channel armed correctly. A gdb backtrace
    of the same shape does not get that far: gdb is not origin-capable, so
    `blamed` falls back to the innermost access frame, and without this filter
    the crashing test's own fn becomes the ORIGIN blame — a name the crate
    declares nowhere. Three things then break at once: the chain synthesis
    stands down (its precondition is "no real frame exists"), the layout aim
    grades NONE against every component, and `blame_outside_unit` reads True
    for every unit, so a newly-crashed block is crash-blocked against a file
    fingerprint that cannot change during the fill phase — a permanent lock.
    """
    return tuple(f for f in frames
                 if not is_gen_tests_sibling_name(f.file_rel.rsplit("/", 1)[-1]))


def non_destructor_frames(
    frames: tuple[CrashFrame, ...],
) -> tuple[CrashFrame, ...]:
    """`frames` minus the destructors, for use where a frame becomes BLAME.

    THE RULE THIS ENCODES: the frame that RELEASED a resource is not the frame
    that owns the bug. On a use-after-free the `freed by` stack's innermost
    in-crate frame is the destructor BY CONSTRUCTION — freeing is what a
    destructor is for — and the defect is always that some other body kept or
    handed out a second owner. Blaming the freer aims every rewrite at a
    correct body.

    Measured, run `1618155-b60a8c4c` (2026-08-30, cluster `stable`), report
    `logs/asan/asan-20260830T031254431377.txt`. Twelve tests unmeasurable
    behind `heap-use-after-free` in five binaries. The READ stack holds no
    `src/` frame at all — `drop_glue::<Option<Backtrace>>` inside
    `drop_glue::<ErrorImpl<MyErr>>` inside `drop_glue::<Box<dyn Error + Sync +
    Send>>`, reached from `behaviour_stable_gen_1.rs:29`. The `freed by` stack
    is `<anyhow::Error as core::ops::drop::Drop>::drop` at `src/error.rs:706`,
    called from that test's line 27 — `let boxed = err.into_boxed_dyn_error();`
    So the allocation is freed by the conversion and read by the `Box` the
    conversion returned, and the real defect is `into_boxed_dyn_error` taking
    `self` by value and never neutralising it before handing the pointer to
    `object_boxed`. `Error::drop` reads the vtable and calls `object_drop`: a
    correct destructor with nothing in it to fix.

    What blaming it cost: the layout retrigger spent 2 of a cap of 2 on that
    blame and the cap is per-blame and outlives the process (identical refusal
    in five consecutive runs); then the crash-evidence dispatch put it at ranks
    0-2 with the fault as its instruction, the model rewrote the body, 14 fns
    landed — and iter 2 read `pass=101 improved=False`, `cashed=0`, with the
    same five binaries still crashed. The loop ran the experiment: it fixed the
    blamed body and the crash did not move. `into_boxed_dyn_error` has never
    been aimed at for this crash.

    THIS FUNCTION IS THE FILTER, NOT THE POLICY. Applying it unconditionally
    was wrong and a recorded walk caught it: where the destructor is the only
    in-crate frame the evidence has, discarding it leaves the aim with nothing.
    :func:`prefer_non_destructor` is the policy callers want — it uses this
    filter when something survives it and keeps the destructor when nothing
    does.

    Compiler drop glue and `drop_in_place` are matched with the same marks:
    they are the same event with the destructor inlined away.

    ONLY THE SYMBOL MARKS ARE MATCHED. A gdb backtrace carries no
    `as core::ops::drop::Drop>` in its symbol — the debugger prints the
    function, not the trait impl that owns it — so a native site keeps a
    frame named `drop` (run `1790133-2181e509`, 2026-08-30 05:20:18: two
    SIGSEGV siblings built from gdb frames read `blamed: ["src/error.rs",
    "drop"]` while the ASAN site skipped it). Matching the bare name `drop`
    was the removed catalog driver's rule (git tag ``catalog-driver-last``,
    2026-10-17); the pipeline does not special-case a language name the
    fill model is the expert on. A sanitized sweep of the same binary
    supersedes the gdb site (:func:`localize_crashes`), which is where the
    destructor is skipped by evidence rather than by name.
    """
    return tuple(f for f in frames
                 if not any(m in f.symbol for m in _DROP_SYMBOL_MARKS))


def prefer_non_destructor(
    frames: tuple[CrashFrame, ...],
) -> tuple[CrashFrame, ...]:
    """`frames` with destructors dropped — unless that empties it.

    A DESTRUCTOR IS THE WEAKEST CANDIDATE, NOT A DISQUALIFIED ONE, and this
    fallback is the whole difference between the two. Where a caller survives
    behind the destructor, that caller is the blame and the destructor never
    should have been. Where the destructor is the ONLY in-crate frame the
    evidence has, discarding it buys nothing and costs the aim: blame goes
    empty, the layout retrigger is handed no component, and the mechanism that
    the recorded walk `tests/walks/test_walk_mech_budget_lockout.py` exists to
    protect — "the aim is handed `src/error.rs::drop` as ORIGIN blame, not the
    crashing test's `src/error.gen_tests_1.rs` frame" — regresses to the
    starvation it was written to end. That walk caught exactly this when the
    filter was applied unconditionally.

    So the rule is a PREFERENCE over the candidate list, not a filter on
    eligibility, and it composes with the module's standing judgement that
    weak blame beats none wherever nothing better exists.
    """
    kept = non_destructor_frames(frames)
    return kept or frames


# A drop is running when the symbol is a `Drop::drop` impl, the compiler's drop
# glue, or `drop_in_place`. Matched on the DEMANGLED symbol, which is what ASAN
# prints for a Rust binary built with debuginfo.
_DROP_SYMBOL_MARKS = (
    "as core::ops::drop::Drop>::drop",
    "core::ptr::drop_glue",
    "core::ptr::drop_in_place",
)
# Frames from the toolchain's own sources, which name no body anyone can change.
_TOOLCHAIN_PATH_MARKS = ("/rustc/", "/compiler-rt/", "/library/", "<unknown>")
# Ownership-transfer primitives. A body holding one of these is a place where a
# value's owner changes without the type system tracking it, which is the whole
# vocabulary of a double drop: `ptr::read` copies an owner, `Box::from_raw`
# creates one, `mem::forget`/`ManuallyDrop` suppress one. The crate's own
# `object_drop_front` on 2026-08-16 was `Box::from_raw` with no `ManuallyDrop` —
# `object_drop` with a second parameter it never read.
_OWNERSHIP_PRIMITIVES = (
    "ptr::read", "from_raw", "mem::forget", "ManuallyDrop", "transmute",
    "into_raw", "as_ptr", "cast::<",
)
# Ways a body fabricates a pointer that names no object: `NonNull::dangling()`
# yields `align_of::<T>()` as the address, `null()`/`null_mut()` yield 0, and
# `NonNull::new_unchecked(<literal>)` yields whatever was written. All three are
# legitimate as a PLACEHOLDER — a value written into a slot whose real contents
# have just been moved out — and none of them is legitimate as a value handed to
# a caller who will dereference it. `placeholder_pointer_subjects` separates the
# two by what the occurrence is used FOR, not by which mark it is.
_POINTER_SENTINELS = ("dangling()", "::null()", "::null_mut()")
# The in-place-exchange idiom, and the whole of the legitimate use above: the
# sentinel is an ARGUMENT here, written into a slot in exchange for the real
# value being taken out. Measured on the live anyhow tree 2026-08-16 — of the
# seven `NonNull::dangling()` occurrences in `.skeleton-filled/stable/src`, four
# are `core::mem::replace(&mut self.inner, Own { ptr: …dangling() })` in
# `downcast`, `into_boxed_dyn_error`,
# `reallocate_into_boxed_dyn_error_without_backtrace` and `Drop::drop`, and all
# four are correct.
_SENTINEL_EXCHANGE_CALLS = ("replace(", "swap(", "ptr::write")


def _raw_is_destructor(func: str) -> bool:
    return any(m in func for m in _DROP_SYMBOL_MARKS)


def _raw_is_toolchain(file_: str) -> bool:
    return any(m in file_ for m in _TOOLCHAIN_PATH_MARKS)


@dataclass(frozen=True, slots=True)
class SourceWindow:
    """A bounded slice of a test file, read by the localizer for the
    access-side recovery: ``lines[i]`` is line ``first_line + i`` of
    ``file_rel``. Carries its own coordinates so the recovered frame can
    name the exact line that called the conversion."""

    file_rel: str
    first_line: int
    lines: tuple[str, ...]

    def line_no(self, index: int) -> int:
        return self.first_line + index


# The opening line of a Rust fn, for bounding the access-side window to the
# enclosing test fn: `fn name(`, `pub fn`, `async fn`, `unsafe fn`, ...
_FN_OPEN_RE = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+|unsafe\s+|const\s+|extern\s+\"[^\"]*\"\s+)*fn\s+[A-Za-z_][A-Za-z0-9_]*")


def enclosing_fn_window(
    lines: Sequence[str],
    line_no: int,
    *,
    max_lines: int = 40,
) -> tuple[int, list[str]]:
    """``(first_line_no, text_lines)`` of the window that ends at 1-based
    ``line_no`` and starts at the enclosing fn's opening `fn ` line — or,
    when no such line sits within ``max_lines`` above, at ``max_lines``
    above. Empty when ``line_no`` is outside ``lines``.

    Pure: the localizer reads the file; this decides how much of it the
    unique-name gate may see. Bounded because an unbounded upward scan of a
    test file would reach other tests' conversions and the gate would then
    refuse on a count that has nothing to do with this crash.
    """
    if not (1 <= line_no <= len(lines)):
        return line_no, []
    start = max(1, line_no - max_lines)
    for i in range(line_no, start - 1, -1):
        if _FN_OPEN_RE.match(lines[i - 1]):
            start = i
            break
    return start, list(lines[start - 1:line_no])


def first_test_file_on_access(
    access: list[dict[str, object]],
) -> tuple[str, int] | None:
    """The innermost ACCESS frame that is a test file with a line number:
    ``(file_rel, line)``, or None.

    The access-side sibling of :func:`first_test_file_after_destructor`,
    for the report whose ORIGIN stack is empty — a SEGV carries no free or
    allocation stack, so origin recovery has nothing to walk, yet the READ
    stack still ends in the test line that dereferenced the conversion's
    result (`error_gen_3.rs:33` under `Box<dyn Error>::to_string`, run
    `1577437-c93d5c26`). A test file is one under `tests/` or one of the
    pipeline's own `src/<stem>.gen_tests_<N>.rs` siblings.
    """
    for rec in access:
        file_ = str(rec.get("file") or "")
        raw_line = rec.get("line")
        if not file_ or _raw_is_toolchain(file_):
            continue
        if not (isinstance(raw_line, int) and raw_line >= 1):
            continue
        if (file_.startswith("tests/")
                or is_gen_tests_sibling_name(file_.rsplit("/", 1)[-1])):
            return (file_, raw_line)
    return None


def first_test_file_after_destructor(
    origin: Sequence[Mapping[str, object]],
) -> tuple[str, int] | None:
    """If the first in-crate origin frame is a destructor, return the next
    non-toolchain caller's ``(file_rel, line)``.

    Two shapes, one owner:

    * a test file — the call was inlined
      (`Drop::drop` at `src/error.rs:706` from
      `tests/behaviour_stable_gen_1.rs:27`);
    * an in-crate caller — live, `From<Error> for Box<dyn Error>` at
      `src/error.rs:955`, run `1869206-7fd52754`, report
      `logs/asan/asan-20260830T083211573812.txt`.

    A crate-symbol catalog once excluded callers whose bare name it listed
    as a "conversion"; it was removed on 2026-10-17 (git tag
    ``catalog-driver-last``), so every in-crate caller is returned and
    :func:`recover_owner_from_caller_source` reads its line. Consecutive
    destructor
    frames (`Error::drop` then `object_drop`) are walked past.
    """
    seen_destructor = False
    for rec in origin:
        file_ = str(rec.get("file") or "")
        func = str(rec.get("func") or "")
        if not file_ or not func or _raw_is_toolchain(file_):
            continue
        if not seen_destructor:
            if file_.startswith("src/") and _raw_is_destructor(func):
                seen_destructor = True
                continue
            return None
        if file_.startswith("src/") and _raw_is_destructor(func):
            continue
        raw_line = rec.get("line")
        if not (isinstance(raw_line, int) and raw_line >= 1):
            return None
        if file_.startswith("tests/"):
            return (file_, raw_line)
        if file_.startswith("src/"):
            return (file_, raw_line)
        return None
    return None


def _declared_calls(line: str, declared: frozenset[str]) -> list[str]:
    """Distinct calls on one source line, in source order, to a fn the
    anchor file declares.

    Every ``.name(`` / ``::name(`` call counts. A bare call is not one. A
    name the file does not declare (``to_string``, ``unwrap``) is std's or
    the test's, not a body this crash can blame."""
    from pipeline_core.domain.language_driver import _called_idents
    out: list[str] = []
    for name in _called_idents(line):
        if name in declared and name not in out:
            out.append(name)
    return out


def _name_from_binding(
    lines: Sequence[str], declared: frozenset[str],
) -> tuple[str | None, int] | None:
    """The call the faulting line's value was bound to.

    The last line is the fault. When that line itself calls a fn the
    anchor file declares, the text cannot say whether the fault ran inside
    that call or inside the value it was handed: ``(None, 0)``, nobody,
    and the caller does not fall through. Otherwise the nearest earlier
    line that binds a name the fault reads (and does not bind itself) is
    the binder — the nearest, because a later `let` shadows an earlier
    one. Exactly one declared call on that line is the name:
    ``(name, index)``. Two is nobody. No binder, or a binder that calls
    nothing the file declares, is None: the caller falls through to the
    window's unique-name gate.

    Three use-after-free reports of 2026-10-15
    (`asan-20261015T120750691462.txt` `error_gen_1.rs`,
    `asan-20261015T172115843898.txt` `error_gen_2.rs` and
    `error_gen_4.rs`) fault on ``let boxed = err.<conversion>();``. A
    rule that skipped the faulting line's own call followed ``err`` to
    its constructor and named ``msg`` or ``new``."""
    from pipeline_core.domain.example_setup import binds, uses
    if not lines:
        return None
    fault = lines[-1]
    if _declared_calls(fault, declared):
        return (None, 0)
    fault_names = uses(fault) - binds(fault)
    for index in range(len(lines) - 2, -1, -1):
        if not binds(lines[index]) & fault_names:
            continue
        calls = _declared_calls(lines[index], declared)
        if len(calls) > 1:
            return (None, 0)
        if not calls:
            return None
        return (calls[0], index)
    return None


def _anchor_file(
    origin_raw: Sequence[Mapping[str, object]], named_file: str | None,
) -> str:
    """The crate file a recovered frame is placed in, or "".

    The origin's in-crate destructor file when the origin has one (the
    conversion and the destructor live in one file on every recorded
    use-after-free); otherwise the crate file the naming test file
    answers for (:func:`crate_file_for_test_file`)."""
    for rec in origin_raw:
        file_ = str(rec.get("file") or "")
        func = str(rec.get("func") or "")
        if file_.startswith("src/") and _raw_is_destructor(func):
            return file_
    if named_file:
        return crate_file_for_test_file(named_file) or ""
    return ""


def recover_owner_from_caller_source(
    origin_raw: list[dict[str, object]],
    access_raw: list[dict[str, object]],
    source_line: str | None,
    *,
    named_by: tuple[str, int] | None = None,
    source_window: SourceWindow | None = None,
    declared_in: Callable[[str], frozenset[str] | None] | None = None,
) -> CrashFrame | None:
    """Name the crate fn the caller source called, when a stack licenses it.

    The recovery is licensed by EITHER stack — the access stack's symbols
    or the origin's destructor-then-caller shape, both read through the
    language driver. The unique-name gate runs over ``source_line`` (the
    origin's caller line, located by ``named_by``) when given, else over
    ``source_window`` (the access-side window of the enclosing test fn,
    whose own coordinates supply ``named_by`` — the binding line, not the
    faulting access). On a line, with the anchor's declared names in hand,
    the name is the one call to a declared fn, and two or none is nobody;
    without them, the line's first call. On a window, the name the faulting
    line reads is resolved
    first (:func:`_name_from_binding`), counting only calls to fns the
    anchor file declares (``declared_in``, the anchor's fn names, or None
    when it cannot be read): the nearest line that bound it, when that
    line calls one such fn. A faulting line that calls one itself, or a
    binder that calls two, names nobody. No binding, or no declared names
    to count with, falls through to the unique-name gate. Unlicensed, or
    no unique name: None, and blame is unchanged.

    ``file_rel`` of the recovered frame: the origin's in-crate destructor
    file when the origin has one (the shape every recorded UAF has — the
    conversion and `Error::drop` live in one file); otherwise the crate
    file the crashing test file answers for, :func:`crate_file_for_test_file`
    on the naming file — a SEGV has no origin stack at all, and its
    access stack holds no crate frame either (`error_gen_3.rs:33` under
    std's `to_string`, run `1577437-c93d5c26`), so the pipeline's own
    `tests/<stem>_gen_<N>.rs` naming is the only anchor the report leaves.
    An innermost access-stack `src/` frame was considered and rejected as
    the fallback: it is where the READ ran (`src/fmt.rs::debug` on a Debug
    path), not where the conversion is declared. When neither anchors,
    None. ``line`` is 0: the conversion was inlined and has no crate frame
    of its own; ``named_by`` is the (file, line) that justified the
    recovery, and render prints that instead of fabricating ``:0``.
    """
    from pipeline_core.domain.language_driver import language_driver
    drv = language_driver()
    access_symbols = "\n".join(str(r.get("func") or "") for r in access_raw)
    licensed = (
        drv.access_licenses_conversion(access_symbols)
        or drv.origin_licenses_conversion(origin_raw))
    if not licensed:
        return None
    if source_line:
        # The line path counts calls the way the window path does: only fns
        # the anchor file declares, exactly one. The first call on the line
        # is std's as often as the crate's — `bail_fmt().unwrap_err()
        # .downcast::<String>()` (`tests/test_downcast.rs:21`, run
        # `1577437-c93d5c26`) named `unwrap_err`, which no crate file
        # declares, so the blame dropped out of the frontier downstream.
        anchor = _anchor_file(
            origin_raw, named_by[0] if named_by is not None else None)
        declared = (declared_in(anchor) if declared_in is not None and anchor
                    else None)
        if declared is not None:
            calls = _declared_calls(source_line, declared)
            name = calls[0] if len(calls) == 1 else None
        else:
            name = drv.unique_called_name(source_line)
    elif source_window is not None and source_window.lines:
        anchor = _anchor_file(origin_raw, source_window.file_rel)
        declared = (declared_in(anchor) if declared_in is not None and anchor
                    else None)
        bound = (_name_from_binding(source_window.lines, declared)
                 if declared is not None else None)
        if bound is not None and bound[0] is None:
            return None
        if bound is not None:
            name = bound[0]
            named_by = (source_window.file_rel,
                        source_window.line_no(bound[1]))
        else:
            name = None
            for i, line in enumerate(source_window.lines):
                cand = drv.unique_called_name(line)
                if cand is None:
                    continue
                if name is not None and cand != name:
                    name = None
                    break
                name = cand
                named_by = (source_window.file_rel,
                            source_window.line_no(i))
    else:
        return None
    if name is None:
        return None
    dest_file = _anchor_file(
        origin_raw, named_by[0] if named_by is not None else None)
    if not dest_file:
        return None
    return CrashFrame(file_rel=dest_file, line=0, fn_name=name, symbol=name,
                      named_by=named_by)


#: A cycle is a recursion, not a coincidence, when its frames come round this
#: many times in one stack. Every recorded overflow repeats 80-120 times; a
#: finite recursion that then hit something else is nowhere near it.
CYCLE_MIN_REPEATS = 3
#: The longest cycle looked for. Mutual recursion through a handful of fns is
#: the shape; a longer period is a search that finds noise.
CYCLE_MAX_PERIOD = 8


@dataclass(frozen=True, slots=True)
class StackCycle:
    """The repeating part of a faulting stack: ``frames`` (innermost first, one
    period) came round ``repeats`` times. Identity is (file, fn), never the
    line — a recursion may re-enter the same fn from different call sites."""

    frames: tuple[CrashFrame, ...]
    repeats: int

    @property
    def members(self) -> tuple[CrashFrame, ...]:
        """One frame per distinct fn of the period, in stack order."""
        seen: set[tuple[str, str]] = set()
        out: list[CrashFrame] = []
        for f in self.frames:
            key = (f.file_rel, f.fn_name)
            if key not in seen:
                seen.add(key)
                out.append(f)
        return tuple(out)

    @property
    def key(self) -> tuple[tuple[str, str], ...]:
        """The cycle as a rotation-free identity, so the same recursion seen
        from two binaries (which may start at different members) compares
        equal."""
        ids = [(f.file_rel, f.fn_name) for f in self.frames]
        return min(tuple(ids[i:] + ids[:i]) for i in range(len(ids)))


def repeating_cycle(
    frames: Sequence[CrashFrame], *,
    min_repeats: int = CYCLE_MIN_REPEATS,
    max_period: int = CYCLE_MAX_PERIOD,
) -> StackCycle | None:
    """The cycle a faulting stack's in-crate frames repeat, or None.

    ``frames`` are innermost-first, already reduced to authorable crate frames
    (the std frames a recursion passes through are gone, so a cycle through an
    iterator adapter or a trait shim is still periodic here). The search is
    over the shape alone: for each period p and start offset s, count how many
    times ``frames[s:s+p]`` comes round back to back; the winner covers the
    most of the stack, the shortest period on a tie. Innermost frames that
    belong to the fault rather than the loop (an inlined callee the symbolizer
    labelled with the wrong parent) sit before ``s`` and are not members.
    Names nothing: two fns, three, or one that calls itself decide alike."""
    ids = [(f.file_rel, f.fn_name) for f in frames]
    n = len(ids)
    best: tuple[int, int, int, int] | None = None     # (coverage, -period, s, repeats)
    for p in range(1, max_period + 1):
        for s in range(0, min(n, max_period + 1)):
            if s + p * min_repeats > n:
                continue
            unit = ids[s:s + p]
            if len(unit) < p:
                continue
            r = 1
            while ids[s + r * p:s + (r + 1) * p] == unit:
                r += 1
            if r < min_repeats:
                continue
            cand = (r * p, -p, -s, r)
            if best is None or cand > best:
                best = cand
    if best is None:
        return None
    _cov, negp, negs, r = best
    p, s = -negp, -negs
    return StackCycle(frames=tuple(frames[s:s + p]), repeats=r)


def _frame_locus(fr: CrashFrame) -> str:
    """Where a frame is shown in prompt evidence.

    A recovered conversion has no crate line of its own (`line` is 0).
    Printing `src/error.rs:0` is a fabricated location — live, the
    error.rs file-first prompt of run `1869206-7fd52754` (sha
    `1a7625b18d24a2d5d50896a4fce613f8b0ff73a822924e6aa78300917d49769c`)
    named `into_boxed_dyn_error` at `:0` twice. Name the caller line that
    justified the recovery instead.
    """
    if fr.named_by is not None:
        src, ln = fr.named_by
        return f"{fr.file_rel} (named by {src}:{ln})"
    return f"{fr.file_rel}:{fr.line}"


def ownership_transfer_must(names: Collection[str]) -> str:
    """Bound-driver plan-line MUST when a crashed refill is a conversion."""
    from pipeline_core.domain.language_driver import language_driver
    return language_driver().ownership_transfer_must(names)


@dataclass(frozen=True, slots=True)
class DestructionFault:
    """A use-after-free whose FREE and whose ACCESS are both a DROP: the same
    allocation was dropped twice.

    Why this reading is worth extracting when the frame filter has already
    given up: it is derived from the frames the filter DISCARDS. Run
    `1064759-0f23fb68` (2026-08-15/16) localized `heap-use-after-free` in
    `tests/test_downcast.rs::test_drop` on all seven sweeps and blamed NOBODY
    every time, because no frame in either stack is in `src/` — and yet the
    report is unambiguous about what happened:

        freed by thread T5 here:
          #5 <alloc::sync::Arc<Atomic<bool>> as Drop>::drop  alloc/src/sync.rs
          #6 test_downcast::test_drop      tests/test_downcast.rs:83:5
        use:
          #2 <alloc::sync::Arc<Atomic<bool>> as Drop>::drop  alloc/src/sync.rs
          #4 core::ptr::drop_glue::<test_downcast::drop::Flag>
          #5 test_downcast::test_drop      tests/test_downcast.rs:85:1

    Two drops of one `Arc`, two source lines apart, in a test that holds two
    handles and should therefore free at neither. Nothing in the crate appears
    in either stack — the fault is a MISSING call (`ManuallyDrop`, `forget`),
    and an absence has no frame. That is why waiting for an in-crate frame
    waits for ever on this class, and why the fault must be read from the
    shape of the two stacks instead.

    `freed_type` is what was dropped, `free_site`/`access_site` are the two
    source positions. All three go into the prompt: they say WHICH value is
    double-owned, which is the one thing the crate's own frames cannot."""

    freed_type: str
    free_site: str
    access_site: str

    def render(self) -> str:
        return (
            f"DOUBLE DROP: `{self.freed_type}` is dropped twice — freed at "
            f"{self.free_site}, then touched again by drop glue at "
            f"{self.access_site}. Both drops run in test code, so no crate "
            "frame appears in either stack: the defect is a value this crate "
            "hands to a SECOND owner without giving up the first (a vtable "
            "drop that also drops a payload another path has already read out, "
            "a missing `ManuallyDrop`/`mem::forget` after a `ptr::read`, a "
            "`Box::from_raw` on memory something else still owns). Look for "
            "the fn that transfers ownership of this value and does not "
            "suppress the original owner's drop.")


@dataclass(frozen=True, slots=True)
class PlaceholderPointerFault:
    """A dereference of an address too small to be one: a pointer some fn
    fabricated as a placeholder and returned instead of computing.

    The sibling reading to `DestructionFault`, for the class where the frame
    filter succeeds and is therefore MORE misleading than when it fails. Run
    `1408670-3c074ece` (2026-08-16), `tests/test_downcast.rs`:

        thread 'test_drop' panicked at src/error.rs:440:34:
        misaligned pointer dereference: address must be a multiple of 0x8 but
        is 0x1

    `src/error.rs:440` is inside `Error::downcast_ref`, and the localizer
    blamed it — correctly, as a frame. But `0x1` is `NonNull::<()>::dangling()`,
    and `downcast_ref` does not make it: `object_downcast` two dozen lines away
    returns `Some(Ref::from_raw(NonNull::dangling()))` where upstream returns
    the address of the payload field. The blamed fn dereferences; the fn that
    must change fabricates. That crash aborted the whole 7-test binary on every
    sweep of a 6h run in which `object_downcast` was never dispatched once.

    The discriminator is arithmetic, not a guess: an address BELOW the
    alignment its own type requires cannot be a real allocation of that type,
    so it was written as a constant. A genuinely misaligned heap pointer is
    large and fails this test, which is the point — this text enters a prompt
    as a statement of fact."""

    required_align: int
    address: int
    deref_site: str

    def render(self) -> str:
        return (
            f"PLACEHOLDER POINTER: the dereference at {self.deref_site} read "
            f"address {hex(self.address)}, and the type it was read as "
            f"requires an address that is a multiple of "
            f"{hex(self.required_align)}. No allocation of that type can live "
            f"below its own alignment, so {hex(self.address)} was never a real "
            "address: it is a sentinel some fn WROTE — `NonNull::dangling()`, "
            "a null, a fixed constant — and then handed out. The fn to change "
            "is the one that FABRICATED this pointer, not the one that "
            "dereferenced it: look for a body that returns a dangling or null "
            "pointer where it is supposed to compute the address of the value "
            "it points at (the address of a field, a cast of a live "
            "allocation). A sentinel written INTO a slot in exchange for a "
            "value moved out of it is a different, correct idiom — leave "
            "those alone.")


@dataclass(frozen=True, slots=True)
class CrashSite:
    """A localized binary crash: which test was in flight, what signal, the
    in-crate faulting frames innermost-first, and (when the detector reports
    it — ASAN does) the ``origin_frames`` of the code that CREATED the
    invalid memory being accessed.

    ``raw_report`` keeps the detector's own text. Without it the only way to
    learn WHO freed the memory is to pay for another instrumented build, and
    the free stack is exactly what the filtered frames throw away when it
    symbolizes entirely into std/alloc drop glue."""

    binary_label: str
    test_name: str | None
    signal: str
    frames: tuple[CrashFrame, ...]
    origin_frames: tuple[CrashFrame, ...] = ()
    raw_report: str = ""
    # Whether the DETECTOR that produced this site is able to report an origin
    # stack at all. gdb is not: it walks the faulting thread and nothing else,
    # so its silence about the origin is no evidence. ASAN is: it prints the
    # `freed by`/`allocated by` stacks, so ASAN finding no in-crate origin IS
    # evidence — and the difference decides whether an absent origin means
    # "unknown detector capability" or "looked, and could not attribute it".
    origin_capable: bool = False
    # See PROVENANCE_* above. Every log line and event that shows a site's
    # blame shows this beside it, so a chain-derived blame can never
    # masquerade as an observed frame.
    provenance: str = PROVENANCE_FRAME
    # Chain-derived sites only: the fns of the crashed tests' construction
    # chain (attributed fns plus producers of their types), supplied by the
    # caller — this module cannot derive them, attribution lives above it.
    chain_blamed: tuple[FnLocator, ...] = ()
    # The tests the chain was derived from, for rendering.
    chain_tests: tuple[str, ...] = ()
    # EXHAUSTION sites only: the repeating part of the faulting stack, found
    # from ALL the report's authorable crate frames (`frames` keeps the
    # innermost few for display, too few to hold a three-member period twice).
    cycle: StackCycle | None = None
    # What the in-flight test's own process announced when it aborted
    # (`abort_cause`), when the capture isolated it; None otherwise.
    abort: AbortReading | None = None
    # `origin_frames` is the frame `traced_origin_index` found by following
    # the fault address through the arguments each frame received — a
    # debugger measurement, not a sanitizer's origin stack.
    origin_traced: bool = False
    # The trace stopped at the innermost crate frame: no crate frame was
    # handed the bad address, so the frame that faulted is named its producer
    # by default. It may have computed the address or loaded it from memory
    # (a stale pointer in a field), and the trace cannot tell which
    # (`traced_origin_index`, "what it does not see"). A settlement does not
    # stub on such a site alone (`crash_settlement.settle_step`).
    origin_traced_at_reader: bool = False
    # How many `#N` lines the sanitizer printed under its origin introducer
    # (`AsanReport.origin_stack_lines`). Zero on a debugger site and on a
    # report that carried no free or allocation stack. The localizer
    # supersedes a traced site only when this is non-zero.
    origin_stack_lines: int = 0

    @property
    def fault(self) -> FaultClass:
        """The class of this fault (:func:`fault_class`) — read here once so
        no rule below matches the signal string again."""
        cls = fault_class(self.signal, self.abort)
        if cls is FaultClass.OTHER and self.origin_frames:
            # A detector that printed an ORIGIN stack (freed by / allocated by)
            # is speaking about invalid memory whatever its kind string says.
            return FaultClass.INVALID_MEMORY
        return cls

    @property
    def destruction(self) -> DestructionFault | None:
        """The double-drop reading of `raw_report`, when it supports one.

        A property rather than a constructor argument because every site that
        carries a report can answer it and a reading a caller has to remember
        to pass is one that goes missing at whichever construction site nobody
        updated — the localizer builds sites through `site_from_frames`, the
        driver through `chain_derived_site`, and the tests directly."""
        return read_destruction_fault(self.raw_report)

    @property
    def is_memory_fault(self) -> bool:
        """Whether the signal names a use-of-invalid-memory class, for which
        the faulting frame is routinely an innocent reader."""
        return self.fault is FaultClass.INVALID_MEMORY

    @property
    def blamed(self) -> CrashFrame | None:
        """The frame whose fn must change: the innermost ORIGIN frame.

        When an ORIGIN-CAPABLE detector reports a memory fault and none of its
        origin frames survive the in-crate filter, this is None — nobody is
        blamed. The faulting frame is not a fallback there: this whole module
        exists because that frame merely READS a pointer some other fn
        fabricated, and the localizer escalates to ASAN saying so in as many
        words. Live cost of the old fallback: `src/error.rs::downcast` was
        blamed on every sweep of every run for a heap-use-after-free whose free
        path is in the vtable drop family, and the model was handed "fix the
        pointer/lifetime scheme in `downcast`" — a correct fn — for four hours
        per iteration. An unattributed crash is more honest than a wrong
        attribution, and it stops the pipeline paying twice to rediscover that
        the readers are futile.

        A gdb backtrace is NOT origin-capable, so its lack of origin frames
        says nothing and the innermost faulting frame still stands: on a host
        with no sanitizer that backtrace is the only evidence there is, and
        weak blame beats none. Likewise for any non-memory signal (a stub's
        `unimplemented!()` abort), where the faulting frame IS the site.

        Whichever stack it comes from, the blamed frame is drawn from
        `authorable_frames` — a generated test sibling is an access path, never
        a body the loop can be asked to fix (see that function for the run this
        cost). Blaming nobody is the honest answer there too."""
        if self.fault is FaultClass.EXHAUSTION:
            culprits = self.culprit_frames
            return culprits[0] if culprits else None
        origin = self.culprit_origin_frames
        if origin:
            return origin[0]
        if self.origin_unattributed:
            return None
        access = prefer_non_destructor(authorable_frames(self.frames))
        return access[0] if access else None

    @property
    def culprit_origin_frames(self) -> tuple[CrashFrame, ...]:
        """Origin frames that can carry BLAME: authorable, and not a
        destructor. See :func:`non_destructor_frames` for why freeing a
        resource never convicts the body that freed it."""
        return prefer_non_destructor(authorable_frames(self.origin_frames))

    @property
    def culprit_frames(self) -> tuple[CrashFrame, ...]:
        """The frames whose bodies jointly hold the defect, by fault class.

        EXHAUSTION: the distinct authorable members of the stack's repeating
        cycle — a recursion is closed by all of them and opened by any one, so
        the culprit is a SET (empty when the stack held no repeating in-crate
        frames: an overflow through std alone convicts nobody). Every other
        class: the single blamed frame, or nothing."""
        if self.fault is FaultClass.EXHAUSTION:
            if self.cycle is None:
                return ()
            return authorable_frames(self.cycle.members)
        f = self.blamed
        return (f,) if f is not None else ()

    @property
    def culprit_locators(self) -> frozenset[FnLocator]:
        return frozenset(FnLocator(f.file_rel, f.fn_name)
                         for f in self.culprit_frames)

    @property
    def blame_label(self) -> str:
        """The one-line reading of who is blamed, for logs and events."""
        culprits = sorted(self.culprit_locators)
        if culprits:
            return ", ".join(f"{c.file}::{c.fn_name}" for c in culprits)
        if self.fault is FaultClass.EXHAUSTION:
            return ("NOBODY (the stack overflowed with no repeating in-crate "
                    "frames)")
        return "NOBODY (origin not attributed in-crate)"

    @property
    def origin_unattributed(self) -> bool:
        """A memory fault whose origin a detector that LOOKED for one could not
        place in-crate. The site is still evidence — it names the binary, the
        test and the access path — it simply convicts nobody.

        "In-crate" means a file the loop can author: an origin stack whose only
        surviving frames are the crashing test's own generated siblings has
        attributed the fault to nothing answerable, and reads as unattributed
        here so the chain-derived channel — whose whole precondition is "an
        origin-capable detector looked and placed no frame in-crate" — still
        arms."""
        return (self.is_memory_fault and self.origin_capable
                and not self.culprit_origin_frames)

    @property
    def blamed_locator(self) -> FnLocator | None:
        f = self.blamed
        return FnLocator(f.file_rel, f.fn_name) if f else None

    @property
    def all_frames(self) -> tuple[CrashFrame, ...]:
        return self.frames + self.origin_frames

    def render_evidence(self) -> str:
        """Prompt-ready description of the crash. Concrete frames only — the
        model acts on the actual faulting path, not on prose about crashes."""
        if self.provenance == PROVENANCE_CHAIN:
            return self._render_chain_evidence()
        return FAULT_RENDERERS[self.fault](self)

    def _render_chain_evidence(self) -> str:
        """The chain-derived variant: no frame is shown because none exists —
        saying so, and saying the blame is an inference, is the point."""
        where = f" while running `{self.test_name}`" if self.test_name else ""
        tests = ", ".join(f"`{t}`" for t in self.chain_tests)
        lines = [
            f"Test binary `{self.binary_label}` died with {self.signal}"
            f"{where} — it never reported its remaining tests, and the crash "
            "reproduces deterministically.",
            "The sanitizer looked for the memory's origin and found NO "
            "in-crate frame in any stack: access, free and allocation all "
            "resolve to std/test code. That is the fingerprint of a REFCOUNT "
            "or ownership-bookkeeping error (an Arc/Box under-increment, a "
            "double `ptr::read`, a missing `mem::forget`) planted earlier: "
            "the corrupted count only surfaces later, in std's drop glue at "
            "the test's own drop sites.",
            "This blame is CHAIN-DERIVED, not an observed frame: this fn is "
            "on the construction chain the crashed test(s) "
            f"({tests or 'unnamed'}) exercise. Re-derive this body from the "
            "types, paying attention to every place it moves, clones, casts "
            "or forgets an owned pointer or a reference count.",
        ]
        if self.destruction is not None:
            # Read from the two stacks the frame filter discards, so it is the
            # one statement here that names a concrete VALUE rather than a
            # class of mistake. It goes above the raw report, which is long.
            lines.insert(2, self.destruction.render())
        if self.raw_report:
            lines.append(
                "The detector's own report (std frames included — they show "
                "WHAT was freed and dropped, which the crate code alone does "
                "not):")
            lines.append(self.raw_report)
        lines.append(_UNSOUND_SCHEME_ALREADY_WRITTEN)
        return "\n".join(lines)


#: How a builder introduces the rendered evidence of a crash inside the file
#: it shows: an observation, no mechanism. What the crash means belongs to the
#: fault class's renderer below.
CRASH_EVIDENCE_LEAD = "A test binary CRASHED at runtime inside this file:\n"


def crash_evidence_for_units(
    evidence: Mapping[FnLocator, str],
    units: Collection[FnSite] | None,
    sites: CrashSites | None = None,
) -> str:
    """The rendered evidence of every crash blamed on one of ``units`` (every
    crash when None), each distinct block once, in the localizer's order.

    With ``sites``, each unit is read through :meth:`CrashSites.evidence_for`
    for its own container: a frame resolved to another body of the same name
    contributes nothing, and a frame that resolved to none leaves the
    locator's text standing. Without ``sites`` the join stays on the
    locator, ``(file, fn_name)``, after the same path normalization every
    locator reader applies — a caller that has no site map."""
    if units is not None and sites is not None:
        return "\n\n".join(dict.fromkeys(
            t for u in units
            if (t := sites.evidence_for(
                evidence, u.file, u.fn_name, u.container))))
    keys = (None if units is None
            else {(norm_src_rel(u.file), u.fn_name) for u in units})
    return "\n\n".join(dict.fromkeys(
        txt for loc, txt in evidence.items()
        if txt and (keys is None
                    or (norm_src_rel(loc.file), loc.fn_name) in keys)))

#: The invalid-memory instruction about the bodies already on disk. It used to
#: be appended by the file-first builder to every crash block whatever its
#: class; it is this class's claim — memory that outlived its owner is a
#: scheme, and a scheme spreads by imitation — and no other class makes it.
_UNSOUND_SCHEME_ALREADY_WRITTEN = (
    "An already-written body in the file above uses a scheme that is UNSOUND "
    "— do NOT copy that pattern into the bodies you write, and if a stub you "
    "are filling participates in the same scheme, implement it the sound way.")


def _render_invalid_memory(site: CrashSite) -> str:
    """The invalid-memory reading: the faulting frames are the ACCESS path and
    the defect is where the memory was invalidated (an origin stack), so it
    prints both stacks and a pointer/lifetime instruction — the class this
    prescription was measured on, and no other."""
    where = f" while running `{site.test_name}`" if site.test_name else ""
    lines = [
        f"Test binary `{site.binary_label}` died with {site.signal}"
        f"{where} — it never reported its remaining tests.",
        "Faulting access (innermost first, crate frames only):",
    ]
    lines += [f"  #{i} {fr.symbol} at {_frame_locus(fr)}"
              for i, fr in enumerate(site.frames)]
    if site.origin_frames and site.origin_traced:
        # The debugger's trace (`traced_origin_index`): the faulting frame
        # was handed the address; this frame produced it.
        lines.append("The faulting frame was HANDED the address it read; "
                     "the address was produced by:")
        lines += [f"  #{i} {fr.symbol} at {_frame_locus(fr)}"
                  for i, fr in enumerate(site.origin_frames)]
    elif site.origin_frames:
        lines.append("The invalid memory it reads was CREATED by:")
        lines += [f"  #{i} {fr.symbol} at {_frame_locus(fr)}"
                  for i, fr in enumerate(site.origin_frames)]
    raw_origin = origin_stack_text(site.raw_report)
    if raw_origin:
        # UNFILTERED. The in-crate filter keeps only `src/…` frames, and on
        # this class the decisive frame is usually the std one it drops:
        # live, `free <- drop_glue::<RawVec<u8>> <- Error::downcast` says
        # the payload's buffer is being dropped by a vtable call after the
        # value was moved out — and only the middle frame says WHICH
        # payload. The blamed fn's own line number is where the call sits,
        # not necessarily where the defect is.
        lines.append("The detector's own stack for that memory "
                     "(std frames included — read them, the crate frames "
                     "alone do not say what was freed):")
        lines.append(raw_origin)
    f = site.blamed
    if f is not None:
        lines.append(
            f"Fix the pointer/lifetime scheme in `{f.fn_name}` "
            f"({_frame_locus(f)}) — this class of silent crash means "
            "memory that outlived its owner (dangling 'static from a "
            "stack local, wrong cast target, double free). Do not patch "
            "the calling test's fn.")
        # The ownership MUST is this class's claim about the blamed body.
        # The per-fn and per-impl builders used to prefix it to every crash
        # block by the prompt's fn name, so a stack overflow's cycle member
        # was told "the original owner must not drop" (run
        # `371055-1a407b3c`, per-fn `from_boxed`, prompt `104265c6…`).
        must = ownership_transfer_must((f.fn_name,))
        if must:
            lines.append(f"`{f.fn_name}` — {must}")
    elif site.origin_unattributed:
        # Say the origin is unknown instead of naming the reader. The
        # frames above ARE the access path and are worth showing; what
        # they do not tell anyone is which fn to change.
        lines.append(
            "The detector did not report an in-crate origin for the freed "
            "memory, so the fn that invalidated it is UNKNOWN — the frames "
            "above only read it. Do not assume the innermost frame is at "
            "fault: for this class the defect is normally in the code that "
            "frees or fabricates the pointer (drop glue, vtable "
            "construction, a wrong cast target), which may not appear "
            "above at all.")
    elif site.all_frames and not authorable_frames(site.all_frames):
        # Every surviving frame is in a generated test sibling, so there is
        # no body here to name. Without this the site would render its
        # frames and then say nothing at all about what to do with them.
        lines.append(
            "Every frame above is in a generated test file, which no fill "
            "may rewrite — so this crash names no fn to change. Treat the "
            "frames as the access path only: the defect is in whatever "
            "crate code produced the memory those tests touch.")
    lines.append(_UNSOUND_SCHEME_ALREADY_WRITTEN)
    return "\n".join(lines)


def _render_other_fault(site: CrashSite) -> str:
    """A fault of no memory class (a signal that names no invalid access, no
    exhaustion): the observation only — where the process died and the crate
    frames it was on. No mechanism is asserted: nothing here says what was
    freed, invalidated or recursed, so nothing tells the model to look for it.
    Measured before this split (2026-09-26, computed on a SIGFPE site with one
    crate frame): the memory-lifetime instruction was printed for it."""
    where = f" while running `{site.test_name}`" if site.test_name else ""
    lines = [
        f"Test binary `{site.binary_label}` died with {site.signal}"
        f"{where} — it never reported its remaining tests.",
        "Crate frames it was on (innermost first):",
    ]
    lines += [f"  #{i} {fr.symbol} at {_frame_locus(fr)}"
              for i, fr in enumerate(site.frames)]
    if site.all_frames and not authorable_frames(site.all_frames):
        lines.append(
            "Every frame above is in a generated test file, which no fill may "
            "rewrite — so this crash names no fn to change.")
    elif site.frames:
        lines.append(
            "The innermost frame is where the process died; it is not "
            "necessarily where the defect is.")
    return "\n".join(lines)


def _render_exhaustion(site: CrashSite) -> str:
    return render_exhaustion_evidence([site])


#: One renderer per :class:`FaultClass`. A class without an entry here cannot
#: be rendered, and `test_stack_exhaustion` fails when one is added without it:
#: what the model is told about a crash is decided per class, never inherited
#: from whichever branch happened to be written first.
FAULT_RENDERERS: Mapping[FaultClass, Callable[[CrashSite], str]] = {
    FaultClass.INVALID_MEMORY: _render_invalid_memory,
    FaultClass.EXHAUSTION: _render_exhaustion,
    FaultClass.OTHER: _render_other_fault,
}


def render_exhaustion_evidence(sites: Sequence[CrashSite]) -> str:
    """Prompt-ready evidence for stack exhaustion, ONE block for every binary
    that died of the same stack.

    Sites of the same cycle are one observation seen from several test
    binaries: run `38934-2999cc94` printed the same paragraph eleven times,
    once per binary, above frames it then told the reader not to trust. This
    states what was observed (the cycle, how often it came round, which tests
    died of it) and what it means — the fns call each other without a base
    case — and prescribes no body and no lifetime scheme: nothing was freed
    and nothing outlived its owner."""
    first = sites[0]
    tests = sorted({t for s in sites for t in [s.test_name] if t})
    who = (f"Test(s) {', '.join(f'`{t}`' for t in tests)}" if tests
           else f"Test binary `{first.binary_label}`")
    binaries = sorted({s.binary_label for s in sites})
    lines = [f"{who} died with a STACK OVERFLOW"
             + (f" ({len(binaries)} test binaries, one stack)"
                if len(binaries) > 1 else "")
             + " — the process aborted, so those tests (and any test that "
               "shares their binary and had not yet reported) have no verdict."]
    cyc = first.cycle
    if cyc is None:
        lines.append(
            "The faulting stack holds no repeating in-crate frames, so no fn "
            "of this crate is named as the loop: the recursion, if that is "
            "what it is, runs through std or dependency code.")
        if first.frames:
            lines.append("Innermost crate frames:")
            lines += [f"  #{i} {fr.symbol} at {_frame_locus(fr)}"
                      for i, fr in enumerate(first.frames)]
        return "\n".join(lines)
    lines.append(
        f"The stack is one cycle of {len(cyc.frames)} in-crate call(s), "
        f"repeated {cyc.repeats}+ times until the stack ran out "
        "(innermost first):")
    lines += [f"  #{i} {fr.symbol} at {_frame_locus(fr)}"
              for i, fr in enumerate(cyc.frames)]
    names = ", ".join(f"`{m.fn_name}` ({m.file_rel})" for m in cyc.members)
    lines.append(
        f"These fns call each other without a base case ({names}): each body "
        "reaches the next and the last reaches the first, so the recursion "
        "never returns for the input these tests give it. This is not a "
        "memory-lifetime fault — nothing was freed and nothing outlived its "
        "owner. The cycle is closed by its members together: a body that "
        "hands its work to the next member without ever computing a result "
        "keeps it closed, and so does rewriting one member while another "
        "still calls it back.")
    return "\n".join(lines)


def chain_derived_site(
    binary_label: str,
    test_name: str | None,
    signal: str,
    blamed: Iterable[FnLocator],
    *,
    derived_from_tests: Iterable[str] = (),
    raw_report: str = "",
) -> CrashSite:
    """A :class:`CrashSite` whose blame is the crashed tests' construction
    chain, for the crash class where frame filtering is structurally blind.

    Built by the TDD driver ONLY when both hold: the oracle shrink reproduced
    on an immediate re-sweep (the crash is deterministic — a transient must
    not synthesize blame), and an origin-capable detector looked and placed
    no frame in-crate (`origin_unattributed` — while a real frame exists,
    frame blame decides and this constructor must not run). The blamed fns
    are supplied by the caller because deriving them is attribution's job:
    the union of fns attributed to the crashed tests plus the producers of
    those fns' types — for `test_drop`, `Error::new` → `construct_from_std`
    → the vtable install/drop family.

    The site carries no frames on purpose. `blamed`/`blamed_locator` stay
    None, so `blamed_locators()` — the set that convicts, crash-blocks and
    immunizes other units — never contains a chain-derived blame: an
    inference from attribution must rank below an observed frame and must
    not acquire a frame's authority by riding its type. What chain blame
    DOES: it prompts (the chain fns get the rendered evidence) and voids
    exhaustion. What it does not do: re-aim the layout walk — the
    retrigger's OWNS grade reads frame provenance only (see the
    `PROVENANCE_*` note above for the `Quoted` fire that reading chain
    blame produced)."""
    return CrashSite(
        binary_label=binary_label,
        test_name=test_name,
        signal=signal or "fatal signal",
        frames=(),
        origin_frames=(),
        raw_report=raw_report,
        origin_capable=True,
        provenance=PROVENANCE_CHAIN,
        chain_blamed=tuple(sorted(set(blamed))),
        chain_tests=tuple(sorted(set(derived_from_tests))),
    )


def _strip_generics(symbol: str) -> str:
    """`ErrorImpl<()>::error` → `ErrorImpl::error` via bracket-depth scan."""
    out: list[str] = []
    depth = 0
    for ch in symbol:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def bare_fn_name(symbol: str) -> str:
    """Bare fn identifier from a demangled symbol:
    `anyhow::error::ErrorImpl<()>::error` → `error`;
    `anyhow::error::{impl#6}::drop` → `drop`;
    `<Error>::construct_from_std::<E>` → `construct_from_std`.

    The last form is why segments are scanned from the right instead of a
    single rsplit: a turbofish tail strips to an EMPTY trailing segment, and
    an empty fn_name silently produces a locator that matches no fn — the
    crash-blamed frame would drop out of the frontier entirely."""
    parts = [p for p in _strip_generics(symbol).split("::") if p.strip()]
    return parts[-1].strip() if parts else ""


def _crate_frames(
    raw_frames: list[dict[str, object]],
    crate_file_prefix: str,
    max_frames: int,
) -> tuple[CrashFrame, ...]:
    frames: list[CrashFrame] = []
    for rec in raw_frames:
        file_ = str(rec.get("file") or "")
        func = str(rec.get("func") or "")
        raw_line = rec.get("line")
        line = raw_line if isinstance(raw_line, int) else 0
        if not file_.startswith(crate_file_prefix) or not func:
            continue
        frames.append(CrashFrame(
            file_rel=file_, line=line,
            fn_name=bare_fn_name(func), symbol=func))
        if len(frames) >= max_frames:
            break
    return tuple(frames)


# The signals whose `siginfo` reports the address the access faulted on.
# SIGABRT (a runtime abort) and SIGILL carry none; their `si_addr` slot is
# another union member.
_FAULT_ADDRESS_SIGNALS = ("SIGSEGV", "SIGBUS")


def carries_fault_address(signal: str) -> bool:
    return any(s in signal.upper() for s in _FAULT_ADDRESS_SIGNALS)


def _received_address(rec: Mapping[str, object], addr: int) -> bool:
    """Whether a frame was HANDED the faulting address: one of its
    arguments holds a pointer whose pointee spans it. ``ptr_args`` is the
    debugger's flattening of the frame's arguments to `[value, pointee
    size]` pairs (a pointer, or the pointer field of a wrapper struct)."""
    for pair in rec.get("ptr_args") or ():  # type: ignore[attr-defined]
        try:
            value, size = int(pair[0]), int(pair[1])
        except (TypeError, ValueError, IndexError):
            continue
        if value <= addr < value + max(size, 1):
            return True
    return False


def traced_origin_index(
    raw_frames: Sequence[Mapping[str, object]],
    fault_addr: int | None,
    crate_file_prefix: str = "src/",
) -> int | None:
    """The frame that PRODUCED the address a memory fault read, traced
    through the arguments each frame received; None when it cannot be told.

    A debugger stack names where the fault happened, not who made the bad
    pointer, and the faulting frame is routinely a correct reader. Run
    `1057870-b427a3a2` (2026-10-15): a one-line accessor that reads the
    first field of the pointee it is handed was convicted four attempts
    running, because the constructor above it handed it
    `NonNull::dangling()`; the accessor's frame received address 0x8 as its
    argument, the constructor's frame received nothing that spans it.

    The rule walks outward from the faulting frame while each frame was
    handed an argument spanning the fault address. The first frame that was
    not handed it produced it: either it computed the address itself (the
    faulting frame, when nothing it received spans the address) or it
    passed a pointer it made. When that frame is outside the crate, the
    nearest crate frame further out is taken (a crate fn that called a
    library routine on arguments it chose); when no crate frame is further
    out, the address came from the test or the runtime and nothing in the
    crate is named.

    What it does not see. A pointer loaded from memory (a field of a valid
    pointee holding a stale pointer) is produced, by this rule, by the frame
    that loaded it, not by whoever stored it; that is the origin-capable
    sanitizer's question. Frames whose arguments the debugger could not read
    break the chain at that frame."""
    if fault_addr is None or not raw_frames:
        return None
    i = 0
    while i < len(raw_frames) and _received_address(raw_frames[i], fault_addr):
        i += 1
    for j in range(i, len(raw_frames)):
        file_ = str(raw_frames[j].get("file") or "")
        if file_.startswith(crate_file_prefix) and raw_frames[j].get("func"):
            return j
    return None


def site_from_frames(
    binary_label: str,
    test_name: str | None,
    signal: str,
    raw_frames: list[dict[str, object]],
    *,
    fault_addr: int | None = None,
    origin_raw_frames: list[dict[str, object]] | None = None,
    crate_file_prefix: str = "src/",
    max_frames: int = 6,
    raw_report: str = "",
    origin_capable: bool = False,
    source_line: str | None = None,
    source_loc: tuple[str, int] | None = None,
    source_window: SourceWindow | None = None,
    abort: AbortReading | None = None,
    origin_stack_lines: int = 0,
    declared_in: Callable[[str], frozenset[str] | None] | None = None,
) -> CrashSite:
    """Build a :class:`CrashSite` from debugger frame records
    (``{"file": …, "line": …, "func": …}``), keeping only frames whose file
    lies inside the crate source tree (relative ``src/…`` paths — the
    compiler embeds workspace-relative paths for crate-local code, absolute
    paths for std/deps). ``origin_raw_frames`` (ASAN's allocation stack)
    localizes the code that created the invalid memory.

    ``source_line`` is the caller line the free stack's next non-destructor
    frame points at (a test file, or an in-crate wrapper that is not
    itself a conversion); ``source_window`` is the access-side alternative
    — the enclosing test fn's lines up to the faulting access, for a report
    whose origin stack is empty. Recovery prepends a synthetic origin frame
    for the conversion the text uniquely names, gated by the family licence
    of either stack. ``source_loc`` is the (file, line) that justified it
    so render can name it instead of fabricating ``:0``. Both omitted (the
    default) leaves blame unchanged — destructor last resort when nothing
    else survives. That default is what keeps the recorded walk
    ``tests/walks/test_walk_mech_budget_lockout.py`` green: it injects a
    hardcoded drop site and never passes a source line.
    """
    origin_raw = origin_raw_frames or []
    origin = _crate_frames(origin_raw, crate_file_prefix, max_frames)
    # A debugger stop carries no origin stack, but it can carry the fault
    # address and each frame's pointer arguments (`traced_origin_index`).
    # Only for an invalid-memory fault: a stub's abort or an overflow is
    # sited where it happened.
    traced = False
    traced_at_reader = False
    if (not origin_raw and fault_addr is not None
            and carries_fault_address(signal or "")
            and fault_class(signal or "", abort) is FaultClass.INVALID_MEMORY):
        idx = traced_origin_index(raw_frames, fault_addr, crate_file_prefix)
        if idx is not None:
            origin = _crate_frames([raw_frames[idx]], crate_file_prefix, 1)
            traced = bool(origin)
            innermost = next(
                (j for j, f in enumerate(raw_frames)
                 if str(f.get("file") or "").startswith(crate_file_prefix)
                 and f.get("func")), None)
            traced_at_reader = traced and idx == innermost
    cycle: StackCycle | None = None
    if fault_class(signal or "", abort) is FaultClass.EXHAUSTION:
        # The whole faulting stack, not the innermost `max_frames`: a period
        # of three fns needs two periods to be seen as one.
        cycle = repeating_cycle(authorable_frames(_crate_frames(
            raw_frames, crate_file_prefix, len(raw_frames) or 1)))
    # The access-side window is read only when the origin names no in-crate
    # body other than a destructor: a free stack that ran a crate fn names
    # the body that freed, and a guess from the test's text must not
    # outrank it. A destructor alone is the victim, which is what the
    # caller-source recovery exists for. Three use-after-free reports of
    # 2026-10-15 named the conversion on their free stack and lost it to
    # the window's binding rule.
    named_origin = any(not _raw_is_destructor(f.symbol or f.fn_name)
                       for f in origin)
    window = None if named_origin else source_window
    if source_line or window is not None:
        recovered = recover_owner_from_caller_source(
            origin_raw, raw_frames, source_line, named_by=source_loc,
            source_window=window, declared_in=declared_in)
        if recovered is not None:
            origin = (recovered,) + tuple(
                f for f in origin if f.fn_name != recovered.fn_name)
    return CrashSite(
        binary_label=binary_label, test_name=test_name,
        signal=signal or "fatal signal",
        frames=_crate_frames(raw_frames, crate_file_prefix, max_frames),
        origin_frames=origin,
        raw_report=raw_report, origin_capable=origin_capable, cycle=cycle,
        abort=abort, origin_traced=traced,
        origin_traced_at_reader=traced_at_reader,
        origin_stack_lines=origin_stack_lines)


def asan_stack_excerpt(report: str, *, max_access: int = 24,
                       max_origin: int = 24) -> str:
    """The detector's own stacks, UNFILTERED, from an ASAN report.

    ``_crate_frames`` keeps only `src/…` frames, so a free stack that
    symbolizes entirely into std/alloc drop glue reduces to nothing and the
    site silently degrades to access-only. This keeps the text a human (or a
    prompt) can read to see who actually freed the memory, without paying for
    a second instrumented build — the report was already in hand and was being
    parsed and dropped.

    Scoped to the FIRST error report — from its `ERROR:` header to its
    `SUMMARY:` — for the same reason :func:`parse_asan_report` is: a sanitized
    run also emits LeakSanitizer reports, and a global line-grep mixes their
    allocation stacks in with the fault's.

    The budget is PER STACK, never one budget for the report. A single
    whole-report cap is spent by whichever stack is printed first, and ASAN
    prints the access stack first — the one stack whose frames this function
    exists to look past. Live on `anyhow/nightly`: a 33-frame access stack
    against a 40-line cap left the `freed by` stack exactly three frames
    before `… (truncated)` and dropped `previously allocated by` entirely, so
    the evidence the caller wanted survived on ordering luck and one more
    access frame would have removed it.
    """
    out: list[str] = []
    started = False
    # Frames kept for the stack currently being read, and its budget. The
    # access stack is introduced by the `ERROR:` header; every later stack is
    # introduced by a `… here:` / `is located` line and gets its own budget.
    kept = 0
    budget = max_access
    truncated = False
    for raw in report.splitlines():
        line = raw.rstrip()
        s = line.strip()
        if not started:
            if "ERROR: AddressSanitizer:" in s:
                started = True
                out.append(line)
            continue
        if s.startswith("SUMMARY:"):
            out.append(line)
            break
        if not s:
            continue
        if s.startswith("#"):
            kept += 1
            if kept <= budget:
                out.append(line)
            elif not truncated:
                truncated = True
                out.append("    … (stack truncated)")
            continue
        if (s.endswith("here:") or "is located" in s
                or s.startswith(("READ ", "WRITE "))):
            out.append(line)
            kept, budget, truncated = 0, max_origin, False
    return "\n".join(out)


def origin_stack_text(report: str, *, max_frames: int = 8) -> str:
    """The `freed by …` / `allocated by …` / `is located in stack of…` stack
    of the FIRST error report, verbatim and unfiltered.

    `_crate_frames` keeps only `src/…` frames, which on a use-after-free
    discards precisely the frame that identifies WHAT was freed — live,
    `drop_glue::<RawVec<u8>>` sitting between `free` and
    `<anyhow::Error>::downcast` is what says the payload's buffer is being
    dropped by a vtable call after the value was moved out. Returns "" when
    the report holds no such section."""
    keep: list[str] = []
    collecting = False
    for raw in report.splitlines():
        s = raw.strip()
        if s.startswith("SUMMARY:"):
            break
        low = s.lower()
        if ("freed by" in low or "allocated by" in low
                or "is located in stack of" in low):
            if keep:
                break                    # only the first such section
            collecting = True
            keep.append("  " + s)
            continue
        if collecting:
            if s.startswith("#"):
                if len(keep) > max_frames:
                    keep.append("    … (truncated)")
                    break
                keep.append("    " + s)
            elif s:
                break
    return "\n".join(keep) if len(keep) > 1 else ""


def _asan_first_report(report: str) -> list[str]:
    """Lines of the FIRST ASAN error report, header through `SUMMARY:`.

    Scoped exactly as `parse_asan_report` is, and for the same reason: a
    sanitized run also emits LeakSanitizer reports, whose allocation stacks
    would otherwise be read as this fault's."""
    out: list[str] = []
    started = False
    for raw in report.splitlines():
        line = raw.strip()
        if not started:
            if "ERROR: AddressSanitizer:" in line:
                started = True
                out.append(line)
            continue
        out.append(line)
        if line.startswith("SUMMARY:"):
            break
    return out


def _drop_symbol_type(symbol: str) -> str:
    """The type being dropped, from a drop symbol; "" when it is not one.

    `<alloc::sync::Arc<X> as core::ops::drop::Drop>::drop` -> `alloc::sync::Arc<X>`
    `core::ptr::drop_glue::<test_downcast::drop::Flag>`    -> `test_downcast::drop::Flag`
    """
    if " as core::ops::drop::Drop>" in symbol:
        return symbol.split(" as core::ops::drop::Drop>", 1)[0].lstrip("<").strip()
    for mark in ("core::ptr::drop_glue::<", "core::ptr::drop_in_place::<"):
        if mark in symbol:
            tail = symbol.split(mark, 1)[1]
            return tail.rsplit(">", 1)[0].strip()
    return ""


def _authored_site(frames: list[dict[str, object]]) -> str:
    """`file:line` of the innermost frame that is NOT toolchain source — the
    position a human or a model can actually open."""
    for rec in frames:
        f = str(rec.get("file") or "")
        if not f or any(m in f for m in _TOOLCHAIN_PATH_MARKS):
            continue
        tail = "/".join(f.rsplit("/", 2)[-2:]) if "/" in f else f
        line = rec.get("line")
        return f"{tail}:{line}" if isinstance(line, int) and line else tail
    return ""


def read_destruction_fault(report: str) -> DestructionFault | None:
    """The double-drop reading of an ASAN report, or None.

    Requires a use-after-free (a use-after-RETURN is a stack-lifetime error,
    a different fault class with a different remedy) whose access stack AND
    whose free stack each run through a drop. Anything less is not this
    reading and must produce None rather than a weaker guess: this text goes
    into a prompt as a statement of fact."""
    lines = _asan_first_report(report)
    if not lines or "use-after-free" not in lines[0]:
        return None
    access: list[dict[str, object]] = []
    freed: list[dict[str, object]] = []
    target: list[dict[str, object]] | None = access
    for line in lines[1:]:
        if line.startswith("SUMMARY:"):
            break
        if line.startswith("#"):
            rec = _parse_asan_frame_line(line)
            if rec is not None and target is not None:
                target.append(rec)
            continue
        low = line.lower()
        if "freed by" in low:
            target = freed if not freed else None
        elif line.endswith("here:") or "is located" in low:
            target = None
    if not access or not freed:
        return None

    def _dropped(frames: list[dict[str, object]]) -> str:
        for rec in frames:
            sym = str(rec.get("func") or "")
            if any(m in sym for m in _DROP_SYMBOL_MARKS):
                return _drop_symbol_type(sym) or sym
        return ""

    freed_type = _dropped(freed)
    if not freed_type or not _dropped(access):
        return None
    return DestructionFault(
        freed_type=freed_type,
        free_site=_authored_site(freed) or "unknown",
        access_site=_authored_site(access) or "unknown")


_MISALIGNED_RE = re.compile(
    r"misaligned pointer dereference: address must be a multiple of "
    r"(0x[0-9a-fA-F]+) but is (0x[0-9a-fA-F]+)")


def read_placeholder_pointer_fault(
    output: str,
) -> PlaceholderPointerFault | None:
    """The fabricated-pointer reading of a sweep's own output, or None.

    Read from the SWEEP TEXT rather than from a `CrashSite.raw_report`, because
    this class does not need a sanitizer: rustc's own debug alignment check
    prints it, so the gdb fast path produces it and the site carries an empty
    `raw_report` (run `1408670-3c074ece`, all five `phase_c.crash.localized`
    events). Reading it off the site would therefore find nothing exactly when
    the fault is present.

    Both conditions are required and neither is a heuristic: the message must
    be the alignment check's, and the address must be strictly below the
    alignment it violates — an allocation cannot sit below its own alignment,
    so such an address was written as a constant rather than computed."""
    site = ""
    for raw in (output or "").splitlines():
        line = raw.strip()
        if "panicked at " in line:
            site = line.split("panicked at ", 1)[1].strip().rstrip(":")
            # `src/error.rs:440:34` -> `src/error.rs:440`: the column is noise
            # in a prompt and the file:line is what a reader opens.
            head, _, col = site.rpartition(":")
            if col.isdigit() and head.rpartition(":")[2].isdigit():
                site = head
            continue
        m = _MISALIGNED_RE.search(line)
        if m is None:
            continue
        align, addr = int(m.group(1), 16), int(m.group(2), 16)
        if align <= 1 or addr >= align:
            return None
        return PlaceholderPointerFault(
            required_align=align, address=addr, deref_site=site or "unknown")
    return None


def placeholder_pointer_subjects(
    fn_records: Iterable[Mapping[str, object]],
    *, max_subjects: int = 12,
) -> frozenset[FnLocator]:
    """The crate's own fns that HAND OUT a fabricated pointer.

    One rule, and it is structural rather than a name guess: a body that
    constructs a no-argument pointer sentinel (`dangling()`, `null()`,
    `null_mut()` — each of which names no object by construction) in any
    position other than as an argument to an in-place exchange. The exchange
    case is the legitimate idiom and is excluded by what the occurrence is used
    for, not by which fn it is in.

    Measured on `tmp/anyhow/.skeleton-filled/stable` (2026-08-16) before being
    believed: seven occurrences, four of them `mem::replace` arguments, and the
    three survivors are `object_downcast`, `context_downcast` and
    `context_chain_downcast` — every one of them `Some(Ref::from_raw(NonNull::
    dangling()))` where upstream returns the address of a payload field, and
    every one of them absent from the crash evidence of the run that spent six
    hours on the binary they abort.

    Capped on `destruction_path_subjects`' rule and for its reason: a subject
    set wider than `max_subjects` contributes nothing at all, because a subject
    that names half the crate names none of it."""
    picked: set[FnLocator] = set()
    for rec in fn_records:
        file_ = str(rec.get("file") or "")
        fn_name = str(rec.get("fn_name") or "")
        if not file_ or not fn_name or not _is_authorable_file(file_):
            continue
        for line in str(rec.get("body_text") or "").splitlines():
            if not any(s in line for s in _POINTER_SENTINELS):
                continue
            if any(c in line for c in _SENTINEL_EXCHANGE_CALLS):
                continue
            picked.add(FnLocator(file_, fn_name))
            break
    return frozenset(picked) if 0 < len(picked) <= max_subjects else frozenset()


def callee_subjects(
    blamed: FnLocator,
    fn_records: Iterable[Mapping[str, object]],
    *, max_subjects: int = 12,
) -> frozenset[FnLocator]:
    """The in-crate fns `blamed`'s own body names — one hop, as EVIDENCE.

    A frame-derived blame is an observation about where execution faulted, and
    for every fault whose bad value is PRODUCED elsewhere (a returned raw
    pointer, a vtable fn pointer, a fabricated sentinel) the producer is one
    call away and appears in no stack: the caller faults, the callee is the
    defect. 177.3 shipped a producer aim for the case where the localizer
    blames NOBODY; this is the same gap on the route where it blames somebody,
    which is why `object_downcast` — reached from the blamed `downcast_ref` by
    the single call on the line above the fault — was never once dispatched in
    run `1408670-3c074ece`.

    An occurrence counts when the name is followed by `(`, `::<` or `)`. The
    third alternative is not slack: the live call is through a vtable field,
    `(vtbl.object_downcast)(ref_impl, target)`, so the name is followed by the
    closing paren of the parenthesized callee and a `(`-only rule misses
    exactly the fn that matters. A name followed by `::` is a module qualifier
    (`crate::error::vtable`) and is excluded on that ground — measured on the
    live tree, that one exclusion is the difference between four subjects and
    five, the fifth being the `error` module.

    Never blame: these locators carry evidence into a fill prompt and void
    exhaustion, and nothing here may convict, crash-block or revert. Capped for
    `destruction_path_subjects`' reason."""
    records = list(fn_records)
    body = "\n".join(
        str(r.get("body_text") or "") for r in records
        if str(r.get("file") or "") == blamed.file
        and str(r.get("fn_name") or "") == blamed.fn_name)
    if not body:
        return frozenset()
    by_name: dict[str, set[FnLocator]] = {}
    for rec in records:
        file_ = str(rec.get("file") or "")
        fn_name = str(rec.get("fn_name") or "")
        if not file_ or not fn_name or not _is_authorable_file(file_):
            continue
        if fn_name == blamed.fn_name:
            continue
        by_name.setdefault(fn_name, set()).add(FnLocator(file_, fn_name))
    picked: set[FnLocator] = set()
    for name, locs in by_name.items():
        if re.search(r"\b" + re.escape(name) + r"\s*(\(|::<|\))", body):
            picked |= locs
    return frozenset(picked) if 0 < len(picked) <= max_subjects else frozenset()


def _is_authorable_file(file_rel: str) -> bool:
    """Whether a fill may rewrite this file at all: crate source, and not one
    of the pipeline's own generated test siblings.

    The same rule `authorable_frames` applies to frames, applied to records —
    and it is not theoretical here. Enumerated over the live anyhow cluster
    tree on 2026-08-16, the drop-name rule below matches `tests/drop/mod.rs::
    drop`, `tests/test_downcast.rs::test_drop` and `tests/test_repr.rs::
    test_drop` beside the five real ones, and a subject naming a test file is
    the defect 173.1 shipped a filter for: no fill writes a test, so the key
    lands in the frontier and buys a dispatch that cannot change anything."""
    return (file_rel.startswith("src/")
            and not is_gen_tests_sibling_name(file_rel.rsplit("/", 1)[-1]))


def destruction_path_subjects(
    fn_records: Iterable[Mapping[str, object]],
    *, max_subjects: int = 12,
) -> frozenset[FnLocator]:
    """The crate's OWN destruction/ownership-transfer fns, from extractor
    records (`file`, `fn_name`, `body_text`): a fn whose BODY holds an
    ownership-transfer primitive.

    A name-token rule (`drop` in the fn name) ran first under the removed
    catalog driver (git tag ``catalog-driver-last``, 2026-10-17); the
    pipeline does not read a language's naming convention, so the body is
    the only evidence.

    A result wider than `max_subjects` contributes NOTHING, on
    `vanished_subject_fns`' precedent: the point is a subject, and a subject
    that names half the crate names none of it."""
    bodied: set[FnLocator] = set()
    for rec in fn_records:
        file_ = str(rec.get("file") or "")
        fn_name = str(rec.get("fn_name") or "")
        if not file_ or not fn_name:
            continue
        if not _is_authorable_file(file_):
            continue
        body = str(rec.get("body_text") or "")
        if any(p in body for p in _OWNERSHIP_PRIMITIVES):
            bodied.add(FnLocator(file_, fn_name))
    return (frozenset(bodied) if 0 < len(bodied) <= max_subjects
            else frozenset())


def crash_evidence_by_locator(
    sites: tuple[CrashSite, ...],
) -> dict[FnLocator, str]:
    """Map every fn appearing in any site's frames to that site's rendered
    evidence — the innermost (blamed) frame first, but callers up the stack
    also receive the evidence: when the blamed fn's fix requires a consistent
    scheme across its callers, each of them sees the same concrete crash.
    A chain-derived site has no frames; its keys are the chain-blamed fns —
    the same evidence channel, one grade weaker, and the rendered text says
    so."""
    out: dict[FnLocator, str] = {}

    def _put(keys: Iterable[FnLocator], text: str) -> None:
        for key in keys:
            if key in out and text in out[key]:
                continue
            out[key] = (out[key] + "\n\n" + text) if key in out else text

    # Stack exhaustion is ONE observation however many test binaries died of
    # it: sites of the same cycle share a single block (the tests they killed
    # listed in it), instead of one block per binary above the same frames.
    exhausted: dict[object, list[CrashSite]] = {}
    for site in sites:
        if site.fault is FaultClass.EXHAUSTION and site.provenance == PROVENANCE_FRAME:
            ident = (site.cycle.key if site.cycle is not None
                     else tuple((f.file_rel, f.fn_name) for f in site.frames))
            exhausted.setdefault(ident, []).append(site)
            continue
        _put([FnLocator(fr.file_rel, fr.fn_name) for fr in site.all_frames]
             + list(site.chain_blamed), site.render_evidence())
    for group in exhausted.values():
        # The cycle's members carry the evidence, not every frame: the
        # innermost frames of an overflow are wherever the stack ran out — on
        # the recorded report an inlined callee the symbolizer labelled with
        # the wrong parent (`backtrace.rs:10` under `Boxed::new`) — and keying
        # them ranked an unrelated unit as crash evidence. An overflow with no
        # repeating crate frame has no cycle to key and falls back to the
        # frames it has.
        keys = {loc for site in group for loc in site.culprit_locators}
        if not keys:
            keys = {FnLocator(fr.file_rel, fr.fn_name)
                    for site in group for fr in site.all_frames}
        _put(sorted(keys), render_exhaustion_evidence(group))
    return out


def origin_blamed_locators(sites: tuple[CrashSite, ...]) -> frozenset[FnLocator]:
    """Each site's origin-blamed locator, one per site.

    :func:`blamed_locators` adds the exhaustion cycle. The retrigger's
    filled-body gate and OWNS grade need the observed origin only: a
    filled recovered culprit plus an undeclared sibling must stay a fill,
    not a type re-author. Chain-derived sites contribute nothing:
    `chain_derived_site` builds with `frames=()` and `origin_frames=()`,
    so `CrashSite.blamed` — and therefore `blamed_locator` — is None for
    every `PROVENANCE_CHAIN` site by construction; a non-None locator
    here is always frame-derived, with no need to re-check `provenance`."""
    return frozenset(
        loc for s in sites
        if s.fault is not FaultClass.EXHAUSTION
        and (loc := s.blamed_locator) is not None)


def faulting_stack_records(
    sites: tuple[CrashSite, ...],
    recorded: Mapping[str, frozenset[FnLocator]],
    unowned: Collection[str],
) -> dict[str, frozenset[FnLocator]]:
    """``recorded`` with, for each unowned test a site was in flight for, the
    fns of that site's faulting stack.

    A test whose binary aborted has no measured executed set, and the fns its
    own faulting stack passed through were executed by it by definition —
    observed, not inferred. Without them an aborted test owns nothing, so a
    session planned for it lists none of the fns the crash names as executed
    (`unmeasured (binary did not complete)` on 13 of 14 failures of run
    `38934-2999cc94`) and the ranking has no positive evidence for them.
    ``recorded`` is the store's record of what the test entered when it last
    ran; the stack is unioned into it, and only for a test with no measured
    owner set (``unowned``): a measurement is never overwritten. Positive
    evidence only — it names what ran, never what did not. One in-flight
    test per binary has a stack; its siblings that never got to run have
    none and stay unowned. Every fault class contributes: the access stack of
    a use-after-free was executed too."""
    bare_unowned = {t.rsplit("::", 1)[-1] for t in unowned}
    out = {t: frozenset(v) for t, v in recorded.items()}
    for site in sites:
        test = (site.test_name or "").rsplit("::", 1)[-1]
        if (site.provenance != PROVENANCE_FRAME or not test
                or test not in bare_unowned):
            continue
        locs = {FnLocator(f.file_rel, f.fn_name)
                for f in authorable_frames(site.frames)} | set(
                    site.culprit_locators)
        if locs:
            out[test] = frozenset(out.get(test, frozenset()) | locs)
    return out


def frame_site(
    frame: CrashFrame,
    records: Sequence[FnRecord],
    file_text: Callable[[str], str | None],
) -> FnSite | None:
    """The one declared fn a frame's ``(file, line)`` lies inside, with its
    container — or None.

    A frame names a body by LOCATION; ``(file, fn_name)`` names every body
    that shares the identifier. `src/kind.rs::new` is three bodies (three
    impl blocks) on the anyhow tree, so the session's recorded-site join,
    which keeps a locator only when it names exactly one container, dropped
    the stack-overflow cycle's `new [impl Boxed]` from every session planned
    for the aborted tests (run `12616-8345ae1d`, 2026-09-27: the slices held
    `from_boxed` and neither cycle member). The frame's line is inside exactly
    one record's ``sig_byte_start..body_byte_end`` span; the narrowest one
    wins for a nested fn. Language-free: byte spans the extractor already
    records, no impl parsing. A frame whose located record does not carry the
    frame's own fn name is an inlined or macro-expanded frame the symbolizer
    labelled from elsewhere (the recorded overflow's innermost `Boxed::new`
    at `src/backtrace.rs:10`) and resolves to nothing rather than to a body
    the stack never named."""
    if frame.line <= 0:
        return None
    text = file_text(frame.file_rel)
    if text is None:
        return None
    raw = text.encode("utf-8")
    offset = 0
    for _ in range(frame.line - 1):
        nl = raw.find(b"\n", offset)
        if nl < 0:
            return None
        offset = nl + 1
    end = raw.find(b"\n", offset)
    line_end = len(raw) if end < 0 else end
    want = norm_src_rel(frame.file_rel)
    best: FnRecord | None = None
    for r in records:
        if norm_src_rel(r.file) != want or r.body_byte_end <= 0:
            continue
        if r.sig_byte_start <= line_end and offset <= r.body_byte_end:
            if best is None or (r.body_byte_end - r.sig_byte_start
                                < best.body_byte_end - best.sig_byte_start):
                best = r
    if best is None or best.fn_name != frame.fn_name:
        return None
    return FnSite(frame.file_rel, best.fn_name, best.container)


@dataclass(frozen=True, slots=True)
class CrashSites:
    """A sweep's crash blame and evidence at the declared bodies the frames
    ran (:func:`frame_site`), beside the locator-keyed sets dispatch reads.

    `crash_evidence_by_locator` and the blame both key `(file, fn_name)`,
    so `fmt` under `impl Debug for Error` and `fmt` under `impl Display for
    Error` shared one evidence string and one refill admission, and three
    `new` containers collapsed to one blame. ``blamed`` is every culprit
    frame resolved to its body; ``evidence`` is every frame's rendered
    evidence at its body. A frame `frame_site` cannot resolve is left out.

    The readers that hold a container narrow a locator fact with it: when
    any frame of that locator resolved, only the resolved containers carry
    it; when none did (a family member, a chain-derived blame, a table slot
    a route joined), the locator fact stands for every container, as
    before."""

    blamed: frozenset[FnSite] = frozenset()
    evidence: Mapping[FnSite, str] = field(default_factory=dict)
    #: Each blamed locator with the tests whose crash convicted it
    #: (:func:`blamed_tests`), for the schedule's blocker predicate.
    tests_of: Mapping[FnLocator, frozenset[str]] = field(default_factory=dict)

    def blame_map(
        self, culprits: Iterable[FnLocator],
    ) -> dict[FnLocator, frozenset[str]]:
        """`culprits` (the fill's blamed set, which may add units this
        sweep's sites did not convict: held stubs, joined callees) each with
        the tests whose crash convicted it here, empty when none did. The
        schedule's `crash_blamed` input."""
        return {u: self.tests_of.get(u, frozenset()) for u in culprits}

    def blames(self, blamed: Collection[FnLocator], file: str, fn_name: str,
               container: str) -> bool:
        """`(file, fn_name)` is in the locator blame, and the body in
        `container` is the one a culprit frame resolved to (or none did)."""
        return _narrowed(blamed, self.blamed, file, fn_name, container)

    def evidences(self, keys: Collection[FnLocator], file: str, fn_name: str,
                  container: str) -> bool:
        """`(file, fn_name)` carries crash evidence, narrowed to the body a
        frame resolved to as :meth:`blames` narrows the blame."""
        return _narrowed(keys, self.evidence.keys(), file, fn_name, container)

    def evidence_for(self, evidence: Mapping[FnLocator, str], file: str,
                     fn_name: str, container: str) -> str:
        """The evidence text of the body in `container`: its own resolved
        entries when any frame of the locator resolved, else the locator's."""
        own = _resolved(self.evidence.keys(), file, fn_name)
        if not own:
            return evidence.get(FnLocator(file, fn_name), "")
        want = container_key(container)
        return "\n\n".join(dict.fromkeys(
            t for site, t in self.evidence.items()
            if site in own and container_key(site.container) == want))


def _resolved(sites: Iterable[FnSite], file: str, fn_name: str
              ) -> frozenset[FnSite]:
    f = norm_src_rel(file)
    return frozenset(s for s in sites
                     if s.fn_name == fn_name and norm_src_rel(s.file) == f)


def _narrowed(locs: Collection[FnLocator], sites: Iterable[FnSite],
              file: str, fn_name: str, container: str) -> bool:
    if FnLocator(file, fn_name) not in locs:
        return False
    own = _resolved(sites, file, fn_name)
    want = container_key(container)
    return not own or any(container_key(s.container) == want for s in own)


def crash_sites_of(
    sites: tuple[CrashSite, ...],
    records: Sequence[FnRecord],
    file_text: Callable[[str], str | None],
) -> CrashSites:
    """The sweep's :class:`CrashSites`: every culprit frame (the origin
    blame, an exhaustion's cycle members) and every evidence frame resolved
    by :func:`frame_site`. Evidence follows `crash_evidence_by_locator`'s
    keys: a site's frames carry its rendered evidence, an exhaustion's
    cycle members carry the cycle's one block; a chain-derived site has no
    frame and contributes nothing here."""
    def resolve(frames: Iterable[CrashFrame]) -> list[FnSite]:
        return [r for r in (frame_site(f, records, file_text) for f in frames)
                if r is not None]

    blamed: set[FnSite] = set()
    evidence: dict[FnSite, str] = {}

    def _put(keys: Iterable[FnSite], text: str) -> None:
        for key in keys:
            if key in evidence and text in evidence[key]:
                continue
            evidence[key] = (evidence[key] + "\n\n" + text
                             if key in evidence else text)

    exhausted: dict[object, list[CrashSite]] = {}
    for site in sites:
        blamed.update(resolve(site.culprit_frames))
        if site.provenance != PROVENANCE_FRAME:
            continue
        if site.fault is FaultClass.EXHAUSTION:
            ident = (site.cycle.key if site.cycle is not None
                     else tuple((f.file_rel, f.fn_name) for f in site.frames))
            exhausted.setdefault(ident, []).append(site)
            continue
        _put(resolve(site.all_frames), site.render_evidence())
    for group in exhausted.values():
        keys = [k for s in group for k in resolve(s.culprit_frames)]
        if not keys:
            keys = [k for s in group for k in resolve(s.all_frames)]
        _put(sorted(set(keys)), render_exhaustion_evidence(group))
    return CrashSites(frozenset(blamed), evidence,
                      tests_of=blamed_tests(tuple(sites)))


def faulting_stack_sites(
    sites: tuple[CrashSite, ...],
    unowned: Collection[str],
    records: Sequence[FnRecord],
    file_text: Callable[[str], str | None],
) -> dict[str, frozenset[FnSite]]:
    """For each unowned test a site was in flight for, the declared bodies of
    its faulting stack — :func:`faulting_stack_records` resolved to sites by
    :func:`frame_site`, so a homonym is the ONE body the stack ran, not every
    body of that name and not none of them. This is what makes a joint defect
    writable in one transaction: a recursion's members sit in different files
    (`construct_from_boxed` in `src/error.rs`, `new [impl Boxed]` in
    `src/kind.rs`), the file and per-fn routes write one file, and the slice
    session — the route whose editable set spans files — lists what its tests
    executed. Positive evidence only, and only for tests with no measured
    owner set, exactly as the locator record."""
    bare_unowned = {t.rsplit("::", 1)[-1] for t in unowned}
    out: dict[str, set[FnSite]] = {}
    for site in sites:
        test = (site.test_name or "").rsplit("::", 1)[-1]
        if (site.provenance != PROVENANCE_FRAME or not test
                or test not in bare_unowned):
            continue
        for fr in (*authorable_frames(site.frames), *site.culprit_frames):
            resolved = frame_site(fr, records, file_text)
            if resolved is not None:
                out.setdefault(test, set()).add(resolved)
    return {t: frozenset(v) for t, v in out.items()}


def observed_cycle_sites(
    sites: tuple[CrashSite, ...],
    records: Sequence[FnRecord],
    file_text: Callable[[str], str | None],
) -> tuple[frozenset[FnSite], ...]:
    """Each stack exhaustion's repeating period as the declared bodies it
    ran (:func:`frame_site`), one set per distinct cycle. The write-set
    model's observed coupling (`domain.write_set`): members of one period
    are written together, whatever file they sit in. A member no record's
    span holds is left out rather than guessed; a period that resolves to
    fewer than two bodies couples nothing."""
    out: dict[tuple[tuple[str, str], ...], frozenset[FnSite]] = {}
    for site in sites:
        cyc = site.cycle
        if cyc is None or site.fault is not FaultClass.EXHAUSTION:
            continue
        resolved = frozenset(
            r for r in (frame_site(f, records, file_text) for f in cyc.members)
            if r is not None)
        if len(resolved) > 1:
            out.setdefault(cyc.key, resolved)
    return tuple(out.values())


def exhaustion_culprit_locators(
    sites: tuple[CrashSite, ...],
) -> frozenset[FnLocator]:
    """Every fn of every stack-exhaustion cycle: the joint culprit set.

    Kept out of :func:`origin_blamed_locators` on purpose. The layout
    retrigger's filled-body gate and OWNS grade read OBSERVED ORIGIN blame —
    a type whose construction wrote invalid memory — and a recursion between
    two bodies is not a type re-author; feeding it there would aim a layout
    walk at a call cycle."""
    return frozenset(loc for s in sites
                     if s.fault is FaultClass.EXHAUSTION
                     for loc in s.culprit_locators)


def blamed_locators(sites: tuple[CrashSite, ...]) -> frozenset[FnLocator]:
    """Everything a sweep's crash sites convict: the origin blame
    (:func:`origin_blamed_locators`) plus the cycle of every stack
    exhaustion (:func:`exhaustion_culprit_locators`). The set the refill
    stamp, the schedule, the dispatch grade (`Crash.BLAMED`) and the held
    batch (:func:`blocker_culprit_tests`) read.

    No locator is widened to names that share a file with the origin. A
    catalog of crate conversion names once did that (the culprit's whole
    "family" at its file); it was removed on 2026-10-17 because the
    pipeline may not know a crate's symbols, and the git tag
    ``catalog-driver-last`` holds it."""
    return origin_blamed_locators(sites) | exhaustion_culprit_locators(sites)


def blocker_culprit_tests(
    sites: tuple[CrashSite, ...],
) -> dict[FnLocator, frozenset[str]]:
    """Culprits the dispatch batch admits ahead of the cap, each with the
    tests whose crash convicted it: :func:`blamed_tests` of the same
    sites, the observed origin plus every exhaustion-cycle member. A
    culprit with an empty test set is still a key: membership admits it,
    and the count orders it."""
    return blamed_tests(sites)


def blamed_tests(
    sites: tuple[CrashSite, ...],
) -> dict[FnLocator, frozenset[str]]:
    """:func:`blamed_locators`, each culprit with the tests whose crash
    convicted it (bare names: the site's own test and a chain site's tests).

    The keys are exactly :func:`blamed_locators` of the same sites. A
    culprit is asked of a SESSION only when one of the session's aimed tests
    is among these: a session that merely has the culprit in its editable
    set, for another test's slice, is not asked for that body. Run
    `2157238-210637e6` (2026-10-11): every fill-2 session carried a crash
    culprit as a passenger and opened in the blockers' tier ahead of the
    unit routes that aimed at it."""
    out: dict[FnLocator, set[str]] = {
        loc: set() for loc in blamed_locators(sites)}
    for s in sites:
        names = {n.rsplit("::", 1)[-1]
                 for n in (s.test_name, *s.chain_tests) if n}
        for loc in blamed_locators((s,)):
            if loc in out:
                out[loc].update(names)
    return {loc: frozenset(ts) for loc, ts in out.items()}


def memory_blamed_locators(sites: tuple[CrashSite, ...]) -> frozenset[FnLocator]:
    """The reader blame of the INVALID-MEMORY sites only: the units an
    ownership instruction is about. A stack exhaustion's cycle and a fault
    of no memory class blame units too, and neither says anything about
    who owns a pointer; `_plan_line` attached the ownership MUST to every
    crashed conversion whatever its class, and run `371055-1a407b3c`'s
    file prompt `8bd6c469…` told a stack overflow's cycle member that the
    original owner must not drop."""
    return origin_blamed_locators(tuple(
        s for s in sites if s.fault is FaultClass.INVALID_MEMORY))


def blame_outside_unit(
    blamed: frozenset[FnLocator], unit_file: str, unit_fns: frozenset[str],
) -> bool:
    """True iff a localized blame exists and NONE of it lands in this unit
    (same file AND one of the unit's own fns). No blame at all is False —
    with nothing localized there is no evidence the unit is innocent."""
    return bool(blamed) and not any(
        b.file == unit_file and b.fn_name in unit_fns for b in blamed)


def crash_signal_hint(output: str, *, max_lines: int = 4) -> str:
    """The raw crash symptoms a sweep capture carries when the localizer
    finds nothing — the lines naming a signal, an abort or an overflow.

    Recorded so an unlocalizable crash leaves SOMETHING checkable behind:
    `error::gen_tests_4::test_no_backtrace_fallback` crashed every sweep of
    run `2219473` (2026-08-10) and after four "no native fault under gdb, no
    ASAN report" lines the logs held no record of what the sweep binary
    actually died of."""
    _marks = ("signal:", "SIGSEGV", "SIGABRT", "SIGBUS", "SIGILL",
              "stack overflow", "core dumped", "process didn't exit "
              "successfully", "misaligned pointer")
    keep: list[str] = []
    for line in (output or "").splitlines():
        if any(m in line for m in _marks):
            _s = line.strip()
            if _s and _s not in keep:
                keep.append(_s)
        if len(keep) >= max_lines:
            break
    return " | ".join(keep)


def crate_file_for_test_file(test_file: str) -> str | None:
    """The crate source file a test file's tests answer for, or None.

    `src/error.gen_tests_4.rs` -> `src/error.rs` (the pipeline's own generated
    sibling, which is where nearly every unit test lives); `src/error.rs` ->
    itself (an in-source `#[cfg(test)] mod`); `tests/error_gen_3.rs` ->
    `src/error.rs` (the pipeline's own generated INTEGRATION test, named
    `tests/<stem>_gen_<N>.rs` by the writer — `parse_gen_tests_integration_filename`
    is that convention read back; filename occupancy only, an authored
    `tests/proc_gen_2.rs` anchors the same way); `tests/test_downcast.rs` ->
    None, because an upstream integration test answers for the crate's
    public surface and anchors no single file."""
    if not test_file.endswith(".rs"):
        return None
    head, _, base = test_file.rpartition("/")
    if test_file.startswith("tests/"):
        parsed = parse_gen_tests_integration_filename(base)
        return f"src/{parsed[0]}.rs" if parsed is not None else None
    if not test_file.startswith("src/"):
        return None
    if is_gen_tests_sibling_name(base):
        stem = base.split(".gen_tests_", 1)[0]
        return f"{head}/{stem}.rs" if head else f"{stem}.rs"
    return test_file


def vanished_subject_fns(
    vanished: frozenset[str],
    reverse_map: Mapping[str, Collection[tuple[str, str]]],
    *, max_subjects: int = 3,
    test_files: Mapping[str, str] | None = None,
) -> dict[FnLocator, frozenset[str]]:
    """The SUBJECT fns of vanished tests, for the case native localization
    returned nothing: `{fn: tests naming it}`, narrowed hard.

    A vanished test statically reaches most of the crate, so the reverse map
    alone must never become blame — that is the mass-revert mistake one
    level down. Two narrowings make the result a subject rather than a
    suspect list: only tests whose name carries a module path contribute
    (``error::gen_tests_4::test_x`` names ``src/error.rs``; a bare
    ``test_downcast`` anchors nothing), and only reverse-map hits in that
    module's own file survive. A test whose narrowed set still exceeds
    `max_subjects` contributes nothing — a subject that wide is not a
    subject. The output arms the crash-evidence channel (prompt section,
    exhaustion void, keep-on-non-regression); it deliberately produces no
    BLAME, so nothing here can crash-block or mass-revert anyone."""
    out: dict[FnLocator, set[str]] = {}
    for test in vanished:
        parts = test.split("::")
        bare = parts[-1]
        # The anchor, from the INVENTORY when the caller can supply one. The
        # module-path parse below was the only anchor for five days and it
        # never fired once, because the one production caller passes
        # `SweepOracle.vanished` and that set is bare by construction
        # (`reconcile_sweep` builds it through `bare_test_name`, and the bare
        # form is load-bearing for its duplicate-name dedup). A green unit test
        # over an input the caller never produces is the shape this is.
        anchor = (crate_file_for_test_file(test_files.get(bare, ""))
                  if test_files else None)
        if anchor is None and len(parts) < 2:
            continue
        module_file = anchor or f"src/{parts[0]}.rs"
        hits = [FnLocator(*k) for k in reverse_map.get(bare, ())]
        subjects = [loc for loc in hits if str(loc.file) == module_file]
        if not subjects or len(subjects) > max_subjects:
            continue
        for loc in subjects:
            out.setdefault(loc, set()).add(test)
    return {loc: frozenset(tests) for loc, tests in out.items()}


def _parse_asan_frame_line(line: str) -> dict[str, object] | None:
    """``#N 0xADDR in SYMBOL FILE:LINE[:COL]`` → frame record, else None."""
    toks = line.split()
    if len(toks) < 5 or toks[2] != "in":
        return None
    loc = toks[-1]
    symbol = " ".join(toks[3:-1])
    parts = loc.rsplit(":", 2)
    file_ = parts[0]
    line_no = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
    return {"func": symbol, "file": file_, "line": line_no}


@dataclass(frozen=True, slots=True)
class AsanReport:
    """One ``ERROR: AddressSanitizer:`` … ``SUMMARY:`` report of a sanitized
    sweep, tagged with the cargo target it came from.

    ``label`` is the nearest preceding ``Running <label> (<path>)`` line's
    label — the same string :func:`test_results.parse_binary_results` gives
    that binary, so a report joins its crashed binary (and that binary's
    dangling test) by equality. ``text`` is the report's own lines, header
    through ``SUMMARY:``, for the excerpt/origin-stack renderers, which
    read "the first report" of whatever text they are handed.
    ``origin_stack_lines`` counts the ``#N`` lines printed under the origin
    introducer whether or not they symbolized: an origin of zero parsed
    frames over zero lines is a report that carries NO origin stack (a
    SEGV), and over N lines it is an origin whose frames have no file —
    two different starvations the localizer's verdict line must not
    conflate."""

    label: str
    kind: str
    access: list[dict[str, object]]
    origin: list[dict[str, object]]
    text: str
    origin_stack_lines: int = 0
    #: The `previously allocated by` stack of a report whose origin is the
    #: `freed by` stack (a use-after-free). Kept apart: it is not the
    #: origin, but when the free stack's only workspace frames are the
    #: test's own it is the only stack that names the crate's constructor
    #: (run `486447-5207101c`: `Error::construct_from_context` and
    #: `Error::construct_from_std` sat there while the localizer blamed
    #: NOBODY).
    alloc: tuple[dict[str, object], ...] = ()


def origin_or_allocation(
    origin: list[dict[str, object]],
    alloc: Sequence[dict[str, object]],
    is_test_path: Callable[[str], bool],
) -> tuple[list[dict[str, object]], bool]:
    """The stack a use-after-free is blamed on: the free stack, unless its
    only workspace frames are the test's own, in which case the allocation
    stack's crate frames name the body that built the memory. Frames are
    workspace-relative (`relativize_frame_files`); an absolute path is
    outside the workspace. Returns ``(frames, from_allocation)``.

    A test's own free is usually the legitimate one (it dropped its value),
    so the free stack says nothing about the crate; the constructor on the
    allocation stack is the crate's last word on that memory. Run
    `486447-5207101c` (2026-10-07): `tests/test_context.rs::test_downcast_high`
    freed in the test at `:124`, allocated through
    `Error::construct_from_context` → `Error::context` → `ext_context`;
    `tests/test_downcast.rs::test_drop` freed at `:83`, allocated through
    `Error::construct_from_std` → `Error::new`. Both were blamed on nobody."""
    def _ws(frames: Sequence[dict[str, object]]) -> list[str]:
        return [str(f.get("file") or "") for f in frames
                if str(f.get("file") or "")
                and not str(f.get("file") or "").startswith("/")]

    free_ws = _ws(origin)
    if not free_ws or not all(is_test_path(f) for f in free_ws):
        return origin, False
    crate = [f for f in alloc
             if (fl := str(f.get("file") or ""))
             and not fl.startswith("/") and not is_test_path(fl)]
    if not crate:
        return origin, False
    return [dict(f) for f in alloc], True


def parse_asan_reports(output: str) -> list[AsanReport]:
    """Every AddressSanitizer error report in ``output``, in order, each
    tagged with the test binary whose section it appeared in.

    An ASAN sweep of `cargo test` is ONE PROCESS PER TEST BINARY.
    `halt_on_error=1` aborts THAT process on its first error; cargo then
    starts the next binary. The combined output therefore holds one
    AddressSanitizer report per crashed binary, and LeakSanitizer reports
    from the binaries that completed. Measured on run `1577437-c93d5c26`
    (2026-09-06): `logs/asan/asan-20260906T204443251879.txt` (iter 2) holds
    3 AddressSanitizer reports (`tests/error_gen_3.rs` SEGV,
    `tests/test_context.rs` and `tests/test_downcast.rs` heap-use-after-free)
    and 21 LeakSanitizer reports; `…T221545829298.txt` (iter 4) holds 2.
    :func:`parse_asan_report` returned the SEGV alone — an empty origin —
    and the two reports with crate frames in their free stacks were never
    read, so the localizer blamed NOBODY and chain-derived synthesis armed
    on an origin that report 2 had already placed in-crate.

    Frame lines are ``#N 0xADDR in SYMBOL FILE:LINE[:COL]``. The faulting
    ACCESS stack follows the ``ERROR:`` header; the ORIGIN of the invalid
    memory follows a ``freed by …here:`` / ``allocated by …here:`` /
    ``is located in stack of…`` introducer. Stacks are identified by their
    INTRODUCING LINE, never by position, and a report ends at its
    ``SUMMARY:`` (or at the next ``ERROR:`` header). Taking ``stacks[0]``
    and ``stacks[1]`` blindly was wrong on real output: LeakSanitizer runs
    at exit and prints an allocation stack per leak regardless of
    ``halt_on_error``, and the positional read walked out of the
    use-after-free report and into a leak's `malloc` stack, then labelled
    it "origin" — the reason `downcast` was blamed on every sweep from
    07-30 (`anyhow/nightly`). ``ERROR: LeakSanitizer:`` headers open no
    report here.
    """
    reports: list[AsanReport] = []
    label = "?"
    chunk: list[str] | None = None
    for raw in output.splitlines():
        line = raw.strip()
        if line.startswith("Running "):
            desc = line[len("Running "):].partition(" (")[0].strip()
            label = desc or label
            if chunk is not None:
                reports.append(_parse_asan_chunk(chunk))
                chunk = None
            continue
        if "ERROR: AddressSanitizer:" in line:
            if chunk is not None:
                reports.append(_parse_asan_chunk(chunk))
            chunk = [label, line]
            continue
        if chunk is None:
            continue
        chunk.append(line)
        if line.startswith("SUMMARY:"):
            reports.append(_parse_asan_chunk(chunk))
            chunk = None
    if chunk is not None:
        reports.append(_parse_asan_chunk(chunk))
    return reports


def _parse_asan_chunk(chunk: list[str]) -> AsanReport:
    """``chunk`` is ``[label, header, ...lines through SUMMARY:]``."""
    label, header, *rest = chunk
    tail = header.split("ERROR: AddressSanitizer:", 1)[1].strip()
    kind = "AddressSanitizer: " + (tail.split(" on ")[0].split(" at ")[0]
                                   or "error")
    access: list[dict[str, object]] = []
    origin: list[dict[str, object]] = []
    alloc: list[dict[str, object]] = []
    origin_lines = 0
    # Which bucket the stack currently being read belongs to. None = discard
    # (a stack under an introducer this parser does not recognise).
    target: list[dict[str, object]] | None = access
    in_stack = False
    for line in rest:
        if line.startswith("SUMMARY:"):
            break
        if line.startswith("#"):
            in_stack = True
            if target is origin:
                origin_lines += 1
            rec = _parse_asan_frame_line(line)
            if rec is not None and target is not None:
                target.append(rec)
            continue
        if in_stack:
            in_stack = False
            target = None                # a stack ends at its first prose line
        low = line.lower()
        if ("freed by" in low or "allocated by" in low
                or "is located in stack of" in low):
            # The origin is whichever of these appears first: for a
            # use-after-free that is `freed by`, for a use-after-return
            # `is located in stack of`, for a leak `allocated by`. An
            # allocation stack after the origin is kept apart (`alloc`).
            if not origin_lines and not origin:
                target = origin
            elif "allocated by" in low and not alloc:
                target = alloc
            else:
                target = None
    return AsanReport(label=label, kind=kind, access=access, origin=origin,
                      text="\n".join([header, *rest]),
                      origin_stack_lines=origin_lines, alloc=tuple(alloc))


def parse_asan_report(
    output: str,
) -> tuple[str, list[dict[str, object]], list[dict[str, object]]] | None:
    """``(error_kind, access_frames, origin_frames)`` of the FIRST
    AddressSanitizer report in ``output`` — :func:`parse_asan_reports`'s
    first element, kept as the single-report reader for callers that hold
    one report's text (the checkpointed fixtures under
    `tests/fixtures/crash/`). A multi-binary sweep is read with
    :func:`parse_asan_reports`; taking the first report of one was the
    defect that function's docstring records. Returns None when no
    AddressSanitizer error is present."""
    reports = parse_asan_reports(output)
    if not reports:
        return None
    first = reports[0]
    return first.kind, first.access, first.origin


def relativize_frame_files(
    frames: list[dict[str, object]], workspace_root: str,
) -> list[dict[str, object]]:
    """ASAN emits absolute paths; strip the workspace prefix so records match
    the extractor's repo-relative ``src/…`` convention (gdb already emits
    relative paths for crate-local code)."""
    root = workspace_root.rstrip("/") + "/"
    out: list[dict[str, object]] = []
    for rec in frames:
        f = str(rec.get("file") or "")
        if f.startswith(root):
            rec = {**rec, "file": f[len(root):]}
        out.append(rec)
    return out


def vanish_is_stub_confounded(
    vanished: Iterable[str],
    test_reverse_map: Mapping[str, set[FnLocator]],
    current_stubs: Iterable[FnLocator],
) -> bool:
    """True when a vanished-test sweep is explained by remaining stubs, so
    native crash localization would only burn a rebuild to find nothing.

    An `unimplemented!()` panic reached inside drop/vtable/constructor code
    aborts the whole test binary exactly like a bad-pointer scheme — every
    later test in that binary reports no verdict and "vanishes". But a clean
    Rust panic-abort raises no hardware fault for gdb to catch and produces no
    ASAN report, so the localizer can only conclude "no native fault" after
    paying for the gdb attach + an ASAN-instrumented rebuild. While the fns the
    vanished tests statically reach are still ≥half stubs, the vanish evidence
    is confounded (which stub aborts is indistinguishable from a filled-body
    crash) and localization must wait for fills — the same ≥half rule and
    reasoning as ``layout_retrigger.crash_evidence_confounded``, applied at
    localizer scope instead of per frozen component.

    Matches suspects to stubs by full ``FnLocator`` (both come from the one
    extractor over the same src tree): a stale path would only shrink the
    stub overlap and fall back to localizing — never suppress a real crash.
    """
    suspects: set[FnLocator] = set()
    for vt in vanished:
        bare = vt.split("::")[-1]
        suspects |= (test_reverse_map.get(bare)
                     or test_reverse_map.get(vt) or set())
    if not suspects:
        return False
    stub_set = set(current_stubs)
    stubbed = sum(1 for s in suspects if s in stub_set)
    return stubbed * 2 >= len(suspects)
