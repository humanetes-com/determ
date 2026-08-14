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

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass

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
        `unimplemented!()` abort), where the faulting frame IS the site."""
        if self.origin_frames:
            return self.origin_frames[0]
        if self.origin_unattributed:
            return None
        return self.frames[0] if self.frames else None

    @property
    def origin_unattributed(self) -> bool:
        """A memory fault whose origin a detector that LOOKED for one could not
        place in-crate. The site is still evidence — it names the binary, the
        test and the access path — it simply convicts nobody."""
        return (self.is_memory_fault and self.origin_capable
                and not self.origin_frames)

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
) -> CrashSite:
    """Build a :class:`CrashSite` from debugger frame records
    (``{"file": …, "line": …, "func": …}``), keeping only frames whose file
    lies inside the crate source tree (relative ``src/…`` paths — the
    compiler embeds workspace-relative paths for crate-local code, absolute
    paths for std/deps). ``origin_raw_frames`` (ASAN's allocation stack)
    localizes the code that created the invalid memory."""
    return CrashSite(
        binary_label=binary_label, test_name=test_name,
        signal=signal or "fatal signal",
        frames=_crate_frames(raw_frames, crate_file_prefix, max_frames),
        origin_frames=_crate_frames(origin_raw_frames or [],
                                    crate_file_prefix, max_frames),
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


def vanished_subject_fns(
    vanished: frozenset[str],
    reverse_map: Mapping[str, Collection[tuple[str, str]]],
    *, max_subjects: int = 3,
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
        if len(parts) < 2:
            continue
        module_file = f"src/{parts[0]}.rs"
        bare = parts[-1]
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
