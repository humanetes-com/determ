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
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass

from pipeline_core.domain.generation import is_gen_tests_sibling_name
from pipeline_core.domain.models import FnLocator


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


def is_memory_fault_signal(signal: str) -> bool:
    """Whether ``signal`` names a use-of-invalid-memory class."""
    up = signal.upper()
    return any(s in up for s in _MEMORY_FAULT_SIGNALS)


# Blame provenance. A frame-derived blame is an OBSERVED fault location — a
# stack frame a detector printed. A chain-derived blame is an INFERENCE: the
# fns on the construction chain the crashed test exercises, synthesized only
# when an origin-capable detector looked and placed nothing in-crate. The two
# must never be confusable, because they warrant different force: frame blame
# may convict and crash-block; chain blame only prompts, voids exhaustion and
# re-aims the layout walk. Measured case for the second kind: the
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

    THE BARE NAME IS MATCHED TOO, because the symbol marks only reach ASAN.
    A gdb backtrace carries no `as core::ops::drop::Drop>` in its symbol — the
    debugger prints the function, not the trait impl that owns it — so a
    mark-only test skips the destructor on the sanitized site and misses it on
    every native one. Measured on run `1790133-2181e509` (2026-08-30 05:20:18),
    which is this filter's first live sweep: the ASAN site correctly reported
    `blamed: null, origin_frames: [["src/error.rs", 705, "drop"]]`, and two
    SIGSEGV siblings built from gdb frames still read
    `blamed: ["src/error.rs", "drop"]` from

        frames: [["src/error.rs", 705, "drop"],
                 ["src/error.rs", 610,
                  "reallocate_into_boxed_dyn_error_without_backtrace"]]

    In Rust a method named `drop` is `Drop::drop` — the trait's only method,
    and the name is reserved by convention for it; a free `drop` is
    `core::mem::drop`, which is a destructor by another route. Neither is ever
    the body that owns an ownership bug, so the name alone is a safe test.

    What it buys on that stack is the whole point of the rule: skipping the
    destructor promotes `reallocate_into_boxed_dyn_error_without_backtrace`,
    the frame that CALLED it — a conversion in the same family as
    `into_boxed_dyn_error`, which is where this crash actually lives. The
    caller was in the evidence the whole time, one frame out.
    """
    return tuple(f for f in frames
                 if f.fn_name != "drop"
                 and not any(m in f.symbol for m in _DROP_SYMBOL_MARKS))


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

# Conversions that take Error by value and return Box<dyn Error + Send + Sync>.
# A use-after-free whose READ stack is drop glue for that Box wrapping ErrorImpl
# is this family's defect: the body copies the pointer, hands it to a Box, and
# lets self drop. Named from the recorded READ stack of run `1618155-b60a8c4c`
# (2026-08-30), fixture `tests/fixtures/crash/uaf_into_boxed_dyn_error_1618155_2026-08-30.txt`.
CONVERSION_FAMILY: frozenset[str] = frozenset({
    "into_boxed_dyn_error",
    "object_boxed",
    "reallocate_into_boxed_dyn_error_without_backtrace",
    "object_reallocate_boxed",
})
_BOX_DYN_ERROR_MARKS = (
    "Box<dyn core::error::Error",
    "Box<dyn std::error::Error",
)
_ERROR_IMPL_MARK = "ErrorImpl<"
_CONVERSION_CALL_RE = re.compile(
    r"(?:\.|::)\s*("
    + "|".join(sorted((re.escape(n) for n in CONVERSION_FAMILY),
                      key=len, reverse=True))
    + r")\s*\("
)
# The instruction a conversion-crash fill was missing. Run `1790133-2181e509`'s
# error.rs file-first prompt (sha 667e2060fd67e07451e68f24bb4790186067bd541caa7ac22ffbe432dea50c15)
# quoted `into_boxed_dyn_error` in the file and named it in CRASH_EVIDENCE and
# still instructed `drop`. The live builder is `build_file_fill_prompt`; the
# slot that would have converted that fill is `_plan_line`. Without this
# sentence a correctly aimed conversion fill re-emits `let own = self.inner`.
OWNERSHIP_TRANSFER_MUST = (
    "MUST: after the pointer is handed to a Box, self must not drop "
    "(ManuallyDrop, or ptr::read plus mem::forget). A `let own = self.inner` "
    "copy leaves two owners and the destructor frees what the Box holds."
)


def _raw_is_destructor(func: str) -> bool:
    return (bare_fn_name(func) == "drop"
            or any(m in func for m in _DROP_SYMBOL_MARKS))


def _raw_is_toolchain(file_: str) -> bool:
    return any(m in file_ for m in _TOOLCHAIN_PATH_MARKS)


def _has_boxed_dyn_error_send_sync(symbols: str) -> bool:
    """True when a demangled symbol names `Box<dyn Error + Sync + Send>`.

    The window is the Box type itself, not the whole stack: every ASAN
    backtrace also carries `Box<dyn FnOnce + Send>` in thread start, and
    matching Send on the concatenated symbols would fire on an Arc
    double-drop too.
    """
    for mark in _BOX_DYN_ERROR_MARKS:
        start = 0
        while True:
            i = symbols.find(mark, start)
            if i < 0:
                break
            window = symbols[i:i + 160]
            if "Sync" in window and "Send" in window:
                return True
            start = i + len(mark)
    return False


def conversion_family_from_access(
    raw_frames: list[dict[str, object]],
) -> frozenset[str]:
    """The conversion family iff the ACCESS (READ) stack's type chain is
    `Box<dyn Error + Sync + Send>` wrapping `ErrorImpl`.

    That type chain is the suspect filter so a mis-parse cannot promote an
    unrelated fn. An Arc double-drop (`test_downcast::test_drop`, run
    `1064759-0f23fb68`) has neither mark and returns empty — recovery stays
    off, last-resort blame stays `drop`, and the walk that injects a
    hardcoded drop site is untouched.
    """
    symbols = "\n".join(str(r.get("func") or "") for r in raw_frames)
    if _ERROR_IMPL_MARK not in symbols:
        return frozenset()
    if not _has_boxed_dyn_error_send_sync(symbols):
        return frozenset()
    return CONVERSION_FAMILY


def crate_fn_named_on_source_line(
    source_line: str,
    family: frozenset[str],
) -> str | None:
    """The unique family name the source line calls, or None.

    Unique is the gate: a line that names two conversions is not a
    resolution, and a line that names none (`downcast_ref`, `drop`) is not
    one either. The recorded line is `let boxed = err.into_boxed_dyn_error();`.
    """
    hits = [m.group(1) for m in _CONVERSION_CALL_RE.finditer(source_line)
            if m.group(1) in family]
    uniq = list(dict.fromkeys(hits))
    return uniq[0] if len(uniq) == 1 else None


def first_test_file_after_destructor(
    origin: list[dict[str, object]],
) -> tuple[str, int] | None:
    """If the first in-crate origin frame is a destructor and the next
    non-toolchain frame is a test file, return ``(file_rel, line)``.

    That is the inlined-conversion shape: `Drop::drop` at `src/error.rs:706`
    called from `tests/behaviour_stable_gen_1.rs:27`. A next frame still in
    `src/` is left for :func:`prefer_non_destructor` — that is how
    `reallocate_into_boxed_dyn_error_without_backtrace` is already promoted
    on the gdb siblings.
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
        if file_.startswith("tests/"):
            raw_line = rec.get("line")
            if isinstance(raw_line, int) and raw_line >= 1:
                return (file_, raw_line)
            return None
        return None
    return None


def recover_owner_from_caller_source(
    origin_raw: list[dict[str, object]],
    access_raw: list[dict[str, object]],
    source_line: str,
) -> CrashFrame | None:
    """Name the conversion the test line called, gated by the READ stack.

    The destructor's file supplies :class:`CrashFrame`.file_rel so this does
    not need an extractor map. ``line`` is 0: the conversion was inlined and
    has no crate frame of its own.
    """
    family = conversion_family_from_access(access_raw)
    if not family:
        return None
    name = crate_fn_named_on_source_line(source_line, family)
    if name is None:
        return None
    dest_file = ""
    for rec in origin_raw:
        file_ = str(rec.get("file") or "")
        func = str(rec.get("func") or "")
        if file_.startswith("src/") and _raw_is_destructor(func):
            dest_file = file_
            break
    if not dest_file:
        return None
    return CrashFrame(file_rel=dest_file, line=0, fn_name=name, symbol=name)


def ownership_transfer_must(names: Collection[str]) -> str:
    """The plan-line MUST when a crashed refill is a conversion, else empty.

    File-first appends this to `_plan_line`'s CRASHED clause. Per-fn and
    per-impl prepend it to the CRASH_EVIDENCE prefix so a conversion fill
    cannot be dispatched by a sibling route that omits the sentence.
    """
    if CONVERSION_FAMILY.intersection(names):
        return OWNERSHIP_TRANSFER_MUST
    return ""


def crash_evidence_prefix(base: str, names: Collection[str]) -> str:
    """``base`` with :data:`OWNERSHIP_TRANSFER_MUST` in front when ``names``
    intersects the conversion family. Empty evidence still renders empty
    because :func:`tag_block` drops a blank content regardless of prefix.
    """
    extra = ownership_transfer_must(names)
    return f"{extra}\n{base}" if extra else base


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
        return is_memory_fault_signal(self.signal)

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
        where = f" while running `{self.test_name}`" if self.test_name else ""
        lines = [
            f"Test binary `{self.binary_label}` died with {self.signal}"
            f"{where} — it never reported its remaining tests.",
            "Faulting access (innermost first, crate frames only):",
        ]
        lines += [f"  #{i} {fr.symbol} at {fr.file_rel}:{fr.line}"
                  for i, fr in enumerate(self.frames)]
        if self.origin_frames:
            lines.append("The invalid memory it reads was CREATED by:")
            lines += [f"  #{i} {fr.symbol} at {fr.file_rel}:{fr.line}"
                      for i, fr in enumerate(self.origin_frames)]
        raw_origin = origin_stack_text(self.raw_report)
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
        f = self.blamed
        if f is not None:
            lines.append(
                f"Fix the pointer/lifetime scheme in `{f.fn_name}` "
                f"({f.file_rel}:{f.line}) — this class of silent crash means "
                "memory that outlived its owner (dangling 'static from a "
                "stack local, wrong cast target, double free). Do not patch "
                "the calling test's fn.")
        elif self.origin_unattributed:
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
        elif self.all_frames and not authorable_frames(self.all_frames):
            # Every surviving frame is in a generated test sibling, so there is
            # no body here to name. Without this the site would render its
            # frames and then say nothing at all about what to do with them.
            lines.append(
                "Every frame above is in a generated test file, which no fill "
                "may rewrite — so this crash names no fn to change. Treat the "
                "frames as the access path only: the defect is in whatever "
                "crate code produced the memory those tests touch.")
        return "\n".join(lines)

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
    not acquire a frame's authority by riding its type."""
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


def site_from_frames(
    binary_label: str,
    test_name: str | None,
    signal: str,
    raw_frames: list[dict[str, object]],
    *,
    origin_raw_frames: list[dict[str, object]] | None = None,
    crate_file_prefix: str = "src/",
    max_frames: int = 6,
    raw_report: str = "",
    origin_capable: bool = False,
    source_line: str | None = None,
) -> CrashSite:
    """Build a :class:`CrashSite` from debugger frame records
    (``{"file": …, "line": …, "func": …}``), keeping only frames whose file
    lies inside the crate source tree (relative ``src/…`` paths — the
    compiler embeds workspace-relative paths for crate-local code, absolute
    paths for std/deps). ``origin_raw_frames`` (ASAN's allocation stack)
    localizes the code that created the invalid memory.

    ``source_line`` is the test-file line the free stack's next frame points
    at, when that frame is not in ``src/``. Recovery prepends a synthetic
    origin frame for the conversion that line uniquely names, gated by the
    ACCESS stack's type chain. Omitted (the default) leaves blame unchanged
    — destructor last resort when nothing else survives. That default is
    what keeps the recorded walk
    ``tests/walks/test_walk_mech_budget_lockout.py`` green: it injects a
    hardcoded drop site and never passes a source line.
    """
    origin_raw = origin_raw_frames or []
    origin = _crate_frames(origin_raw, crate_file_prefix, max_frames)
    if source_line:
        recovered = recover_owner_from_caller_source(
            origin_raw, raw_frames, source_line)
        if recovered is not None:
            origin = (recovered,) + tuple(
                f for f in origin if f.fn_name != recovered.fn_name)
    return CrashSite(
        binary_label=binary_label, test_name=test_name,
        signal=signal or "fatal signal",
        frames=_crate_frames(raw_frames, crate_file_prefix, max_frames),
        origin_frames=origin,
        raw_report=raw_report, origin_capable=origin_capable)


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
    """The crate's OWN destruction/ownership-transfer family, from extractor
    records (`file`, `fn_name`, `body_text`).

    Two rules, in this order, because the second is a heuristic and the first
    is not:

    1. a fn whose bare name carries `drop` as a token (`drop`, `object_drop`,
       `object_drop_front`, `context_chain_drop_rest`) — the crate declared
       those names, so this is reading the crate's own vocabulary, not
       guessing at one;
    2. if and only if rule 1 finds nothing, a fn whose BODY holds an
       ownership-transfer primitive.

    Rule 2 is second on purpose. A name-token match on the wrong set is this
    project's recurring collapse, so the body evidence is the fallback for a
    crate that does not name its drop family `drop`, never a widening of a set
    that already resolved.

    A result wider than `max_subjects` contributes NOTHING, on
    `vanished_subject_fns`' precedent: the point is a subject, and a subject
    that names half the crate names none of it. On the live anyhow tree rule 1
    yields five — `Error::drop`, `object_drop`, `object_drop_front`,
    `context_drop_rest`, `context_chain_drop_rest` — against the 50 fns the
    construction chain named on 2026-08-15, none of which was any of them."""
    named: set[FnLocator] = set()
    bodied: set[FnLocator] = set()
    for rec in fn_records:
        file_ = str(rec.get("file") or "")
        fn_name = str(rec.get("fn_name") or "")
        if not file_ or not fn_name:
            continue
        if not _is_authorable_file(file_):
            continue
        loc = FnLocator(file_, fn_name)
        if "drop" in fn_name.split("_") or fn_name == "drop":
            named.add(loc)
            continue
        body = str(rec.get("body_text") or "")
        if any(p in body for p in _OWNERSHIP_PRIMITIVES):
            bodied.add(loc)
    picked = named or bodied
    return frozenset(picked) if 0 < len(picked) <= max_subjects else frozenset()


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
    for site in sites:
        text = site.render_evidence()
        keys = [FnLocator(fr.file_rel, fr.fn_name)
                for fr in site.all_frames] + list(site.chain_blamed)
        for key in keys:
            if key in out and text in out[key]:
                continue
            out[key] = (out[key] + "\n\n" + text) if key in out else text
    return out


def blamed_locators(sites: tuple[CrashSite, ...]) -> frozenset[FnLocator]:
    """The ORIGIN blame only — one locator per site, where the localizer put
    the fault (`CrashSite.blamed_locator`).

    Deliberately not the evidence keys: `crash_evidence_by_locator` keys by
    EVERY frame, access and origin, so the innocent caller that merely routes
    execution into the fault is a key too. Any 'is the blame outside this
    unit' test written against the keys reads the blame as inside the caller
    and never fires — live 2026-08-09/10, `ext_context` (src/context.rs) was
    an access frame of the very `object_drop` (src/error.rs) fault it was
    being charged for.

    Chain-derived sites contribute NOTHING here, by construction (their
    `blamed_locator` is None): this set carries a frame's authority — it
    convicts, crash-blocks and immunizes other units — and a blame inferred
    from test attribution must not acquire that authority."""
    return frozenset(
        s.blamed_locator for s in sites if s.blamed_locator is not None)


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
    itself (an in-source `#[cfg(test)] mod`); `tests/test_downcast.rs` -> None,
    because an integration test answers for the crate's public surface and
    anchors no single file."""
    if not test_file.startswith("src/") or not test_file.endswith(".rs"):
        return None
    head, _, base = test_file.rpartition("/")
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


def parse_asan_report(
    output: str,
) -> tuple[str, list[dict[str, object]], list[dict[str, object]]] | None:
    """Extract ``(error_kind, access_frames, origin_frames)`` from the FIRST
    AddressSanitizer error report in ``output``.

    Frame lines are ``#N 0xADDR in SYMBOL FILE:LINE[:COL]``. The faulting
    ACCESS stack follows the ``ERROR:`` header; the ORIGIN of the invalid
    memory follows a ``freed by …here:`` / ``allocated by …here:`` /
    ``is located in stack of…`` introducer.

    Stacks are identified by their INTRODUCING LINE, never by position, and
    collection stops at the report's ``SUMMARY:``. Taking ``stacks[0]`` and
    ``stacks[1]`` blindly was wrong on real output for two reasons, both seen
    live on `anyhow/nightly`:

    * A sanitized run emits MORE than one report — LeakSanitizer runs at exit
      and prints an allocation stack per leak regardless of ``halt_on_error``.
      The positional read walked straight out of the use-after-free report and
      into a leak's `malloc` stack, then labelled it "origin".
    * It produced `access=[]` with `origin=[src/error.rs::downcast]` — the
      reverse of what 93.2 inferred from the old event (which concatenated the
      two into one `frames` list, so nobody could tell which was empty), and
      the reason `downcast` has been blamed on every sweep since 07-30.

    Returns None when no ASAN error is present.
    """
    kind: str | None = None
    access: list[dict[str, object]] = []
    origin: list[dict[str, object]] = []
    # Which bucket the stack currently being read belongs to. None = discard
    # (a stack under an introducer this parser does not recognise, or one
    # belonging to a later report).
    target: list[dict[str, object]] | None = None
    in_stack = False
    for raw in output.splitlines():
        line = raw.strip()
        if kind is None:
            if "ERROR: AddressSanitizer:" in line:
                tail = line.split("ERROR: AddressSanitizer:", 1)[1].strip()
                kind = "AddressSanitizer: " + (tail.split(" on ")[0].split(" at ")[0]
                                               or "error")
                target = access          # the header introduces the access stack
            continue
        if line.startswith("SUMMARY:"):
            break                        # end of THIS report; leaks follow
        if line.startswith("#"):
            in_stack = True
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
            # `is located in stack of`, for a leak `allocated by`.
            target = origin if not origin else None
    if kind is None:
        return None
    return kind, access, origin


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
