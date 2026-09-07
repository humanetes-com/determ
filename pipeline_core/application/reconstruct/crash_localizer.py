"""Crash localization orchestration (application layer).

Given a TDD sweep's raw cargo output in which one or more test binaries died
mid-run, produce typed :class:`CrashSite` evidence:

  1. gdb fast path (seconds): re-run each crashed binary under gdb; catches
     crashes that fault natively on this run.
  2. ASAN fallback (a nightly build + serial run, minutes): when gdb sees no
     fault — dangling-pointer crashes are stack-layout-dependent and flip
     between invocations — ASAN with ``detect_stack_use_after_return=1``
     faults deterministically and additionally reports the ORIGIN stack of
     the invalid memory (the fn that must actually change).

The ASAN sweep is ONE PROCESS PER TEST BINARY: ``halt_on_error=1`` aborts a
binary on its first error and cargo starts the next, so the combined output
carries one AddressSanitizer report per crashed binary. Every report becomes
its own site; nothing here treats the sweep as one heap.

The result feeds three consumers: the frontier (crash-blamed fns outrank
test-name attribution), the fill prompts (a CRASH_EVIDENCE section with the
concrete frames), and the exhaustion gate (crash evidence voids a prior
"nothing left to try" verdict).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pipeline_core.domain.crash_site import (
    AsanReport,
    CrashSite,
    SourceWindow,
    asan_stack_excerpt,
    conversion_family_from_access,
    conversion_family_from_origin,
    enclosing_fn_window,
    first_test_file_after_destructor,
    first_test_file_on_access,
    is_memory_fault_signal,
    parse_asan_reports,
    relativize_frame_files,
    site_from_frames,
)
from pipeline_core.domain.oracle_immutability import is_oracle_path
from pipeline_core.domain.test_results import parse_binary_results

# adapter signatures (composition injects the concrete fns)
GdbLocalize = Callable[..., "tuple[str, list[dict[str, object]]] | None"]
AsanSweep = Callable[..., "tuple[int, str] | None"]


@dataclass(frozen=True, slots=True)
class CrashLocalizerDeps:
    gdb_localize: GdbLocalize          # adapters.gdb_backtrace.localize_crashed_binary
    asan_sweep: AsanSweep | None       # adapters.asan_sweep.run_asan_sweep (None: unavailable)
    log: Callable[[str], None]


# The memory-fault classification is the domain's (`crash_site`), so the
# blame rule and the escalation rule below can never drift apart.
_is_memory_fault = is_memory_fault_signal


def _read_lines(workspace_root: Path, file_rel: str) -> list[str] | None:
    path = workspace_root / file_rel
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None


def _caller_source_line(
    origin: list[dict[str, object]],
    workspace_root: Path,
) -> tuple[str | None, tuple[str, int] | None]:
    """The caller line the free stack points at after an in-crate
    destructor, and its (file, line), or (None, None).

    I/O lives here: :func:`first_test_file_after_destructor` is pure and
    names the path; this reads it. A missing file, a line past EOF, or no
    recoverable caller returns None and blame is unchanged — destructor
    last resort, which is what the existing localizer tests
    (workspace ``/w``, no tree on disk) rely on.
    """
    loc = first_test_file_after_destructor(origin)
    if loc is None:
        return None, None
    file_rel, line_no = loc
    lines = _read_lines(workspace_root, file_rel)
    if lines is None or not (1 <= line_no <= len(lines)):
        return None, None
    return lines[line_no - 1], loc


def _access_source_window(
    access: list[dict[str, object]],
    workspace_root: Path,
) -> SourceWindow | None:
    """The enclosing test fn's lines up to the innermost ACCESS test frame,
    with their coordinates, or None.

    The access-side sibling of :func:`_caller_source_line`, read only when
    origin recovery named nothing: a SEGV has no free/alloc stack, so the
    origin walk has nothing to read, while the READ stack still ends in
    the test line that dereferenced the conversion's result. The window
    is :func:`enclosing_fn_window` — the same test fn, bounded — so the
    unique-name gate sees the conversion on the line above the assert
    (`error_gen_3.rs:32` under the access at `:33`, run
    `1577437-c93d5c26`) and never another test's conversions.
    """
    loc = first_test_file_on_access(access)
    if loc is None:
        return None
    file_rel, line_no = loc
    lines = _read_lines(workspace_root, file_rel)
    if lines is None:
        return None
    start, window = enclosing_fn_window(lines, line_no)
    if not window:
        return None
    return SourceWindow(file_rel=file_rel, first_line=start, lines=tuple(window))


def _site_for_report(
    deps: CrashLocalizerDeps,
    report: AsanReport,
    dangling: str | None,
    workspace_root: Path,
) -> CrashSite | None:
    """One :class:`CrashSite` for one AddressSanitizer report."""
    kind = report.kind
    access = relativize_frame_files(report.access, str(workspace_root))
    origin = relativize_frame_files(report.origin, str(workspace_root))
    label = report.label
    source_line, source_loc = _caller_source_line(origin, workspace_root)
    source_window: SourceWindow | None = None
    if source_line is None and (conversion_family_from_access(access)
                                or conversion_family_from_origin(origin)):
        # Origin recovery has nothing to read; the family is licensed, so
        # the access side may name the conversion. The licence is the same
        # gate that keeps an Arc double-drop off this path.
        source_window = _access_source_window(access, workspace_root)
    site = site_from_frames(
        label, dangling, kind, access, origin_raw_frames=origin,
        raw_report=asan_stack_excerpt(report.text), origin_capable=True,
        source_line=source_line, source_loc=source_loc,
        source_window=source_window)
    if site.origin_unattributed:
        # Say WHICH starvation this is — the verdict alone left "no origin
        # stack at all", "unsymbolized raw addresses" and "symbolized but
        # genuinely foreign" indistinguishable, and telling them apart cost
        # an archive dig per instance. Measured 2026-08-18 on
        # `logs/asan/asan-20260816T194054947258.txt` (the deterministic
        # `test_downcast::test_drop` heap-use-after-free): all 27 origin
        # frames symbolize, and the only workspace frames are the TEST's own
        # (`tests/test_downcast.rs:83` frees the Arc legitimately) — so no
        # build flag can put a crate frame on that stack, and the culprit for
        # this class lives on the ACCESS/retention side. And a SEGV (run
        # `1577437-c93d5c26`, `tests/error_gen_3.rs`) prints no free or
        # allocation stack at all: zero origin lines is not a symbolizer
        # fault, and telling the operator to check llvm-symbolizer there
        # sent the diagnosis at the wrong tool. The raw stacks stay on the
        # site so the next reader sees who freed the memory without paying
        # for another instrumented build.
        _sym = [f for f in origin if str(f.get("file") or "")]
        _ws = [str(f.get("file") or "") for f in _sym
               if not str(f.get("file") or "").startswith("/")]
        _oracle = [f for f in _ws if is_oracle_path(f)]
        if _sym:
            detail = (f"{len(_sym)} of {len(origin)} frame(s) symbolized"
                      + (f"; the only workspace frame(s) are {len(_oracle)} "
                         "test-file frame(s) (oracle — not authorable, and a "
                         "test's own free is usually the legitimate one: "
                         "suspect the crate's access/retention path)"
                         if _ws and len(_oracle) == len(_ws)
                         else f"; {len(_ws)} workspace frame(s)"))
        elif report.origin_stack_lines:
            detail = (f"NO frame symbolized — {report.origin_stack_lines} raw "
                      "address line(s); check llvm-symbolizer on the sweep's "
                      "PATH")
        else:
            detail = (f"this report carries no origin stack ({kind}): the "
                      "access path is recorded")
        deps.log("  [phase-c/crash-localizer] "
                 f"{kind} in {label}: no in-crate frame in the origin stack "
                 f"({len(origin)} raw frame(s)) — blaming NOBODY. "
                 + detail
                 + ". The access path is recorded; the fn that invalidated "
                 "the memory is not identified. Raw report archived under "
                 "logs/asan/.")
    # An origin-unattributed site survives even with ZERO in-crate frames:
    # it still names the binary, the in-flight test and the raw report, and
    # "an origin-capable detector looked and placed nothing in-crate" is the
    # precondition the TDD driver's chain-derived blame synthesis keys on.
    # Dropping it here is what starved that channel on the deterministic
    # `test_downcast` heap-use-after-free (run `635043-5f7c61f5`, 2026-08-14):
    # every stack resolved to std/tests frames, `all_frames` was empty, the
    # driver saw no site at all, and the walk closed with the crash's only
    # evidence living in a log line. A frame-less site that is NOT
    # origin-unattributed is still dropped — it carries no evidence of any
    # grade.
    return site if (site.all_frames or site.origin_unattributed) else None


def _asan_sites(
    deps: CrashLocalizerDeps,
    test_cmd: str,
    workspace_root: Path,
    env: dict[str, str],
) -> tuple[CrashSite, ...]:
    """Run the sanitized sweep once and build one ORIGIN-bearing site per
    AddressSanitizer report — one per crashed binary.

    Each report is joined to its binary by label: the report's label is the
    nearest preceding `Running <label>` line, and `parse_binary_results`
    gives the same label to the section that never printed its trailer, so
    the in-flight (dangling) test is that binary's own. The pre-fix reader
    took the FIRST report and the FIRST incomplete binary and called them
    the sweep: on run `1577437-c93d5c26` (iter 2) that was the
    `tests/error_gen_3.rs` SEGV with an empty origin, while the
    `tests/test_context.rs` and `tests/test_downcast.rs` use-after-frees —
    both with `Error::drop` in-crate on their free stacks — were never read.
    """
    if deps.asan_sweep is None:
        return ()
    asan = deps.asan_sweep(test_cmd, workspace_root, env)
    if asan is None:
        return ()
    _rc, out = asan
    reports = parse_asan_reports(out)
    if not reports:
        return ()
    crashed_by_label = {b.label: b for b in parse_binary_results(out)
                        if not b.completed}
    sites: list[CrashSite] = []
    for report in reports:
        hit = crashed_by_label.get(report.label)
        site = _site_for_report(
            deps, report, hit.dangling if hit is not None else None,
            workspace_root)
        if site is not None:
            sites.append(site)
    return tuple(sites)


def localize_crashes(
    deps: CrashLocalizerDeps,
    sweep_output: str,
    test_cmd: str,
    workspace_root: Path,
    env: dict[str, str],
    *,
    max_binaries: int = 3,
) -> tuple[CrashSite, ...]:
    """Localize every crashed binary visible in ``sweep_output`` (capped).

    gdb runs first because it is cheap and confirms WHICH binary/test dies,
    but its backtrace names only the faulting frame. For a memory fault that
    frame is usually not the fn to fix, so the sanitized sweep runs too and
    its ORIGIN-bearing site supersedes gdb's FOR THAT BINARY. gdb's result is
    kept for every binary the sanitizer did not report (no nightly, sanitizer
    unsupported, crash not reproducible under instrumentation, or — live on
    run `1577437-c93d5c26` iter 4 — `tests/test_context.rs` aborting on a
    panic in a destructor with no AddressSanitizer report at all).

    Returns () when nothing crashed or nothing could be localized — callers
    must treat an empty result on a shrunken oracle as "unlocalized", never
    as health."""
    crashed = [b for b in parse_binary_results(sweep_output) if not b.completed]
    if not crashed:
        return ()
    considered = crashed[:max_binaries]
    sites: list[CrashSite] = []
    for b in considered:
        if not b.exe_path:
            # A crashed binary with no executable path cannot be re-run, so it
            # gets no backtrace and no ASAN blame — and the sweep still counts
            # its tests as failing. Silence here read as "localized nothing",
            # which is a different fact from "could not name the target".
            deps.log(f"  [phase-c/crash-localizer] {b.label} crashed but cargo "
                     "printed no executable path for it — cannot re-run it, so "
                     "this binary gets no crash evidence.")
            continue
        exe = Path(b.exe_path)
        if not exe.is_absolute():
            exe = workspace_root / exe
        got = deps.gdb_localize(exe, workspace_root, env)
        if got is None:
            continue
        signal, frames = got
        site = site_from_frames(b.label, b.dangling, signal, frames)
        if site.frames:
            sites.append(site)
    if deps.asan_sweep is None:
        return tuple(sites)
    # Escalate to the sanitizer whenever origin evidence is missing and could
    # change the blame: a crashed binary gdb could not reproduce (precisely
    # the layout-dependent memory bugs), or a memory-fault backtrace whose
    # innermost frame may be an innocent reader.
    unreproduced = len(sites) < len(considered)
    needs_origin = unreproduced or any(
        _is_memory_fault(s.signal) and not s.origin_frames for s in sites)
    if not needs_origin:
        return tuple(sites)
    deps.log("  [phase-c/crash-localizer] "
             + ("native re-run showed no fault (layout-dependent crash)"
                if unreproduced else
                "backtrace names only the faulting frame")
             + " — running ASAN sweep for the memory's origin.")
    asan = _asan_sites(deps, test_cmd, workspace_root, env)
    if not asan:
        return tuple(sites)
    # Each test binary is its own process and its own heap. A sanitized site
    # supersedes the gdb site of the SAME binary — same label — because it
    # carries the origin gdb cannot see. It says nothing about any other
    # binary: `halt_on_error` stops one process, cargo starts the next, and
    # the three incomplete binaries of run `1577437-c93d5c26` were three
    # separate faults. The previous consolidation dropped every other
    # memory-fault backtrace as a "downstream reader of the same corruption"
    # — a one-heap premise that is false across processes — and that is
    # how a sweep with three reports produced one nobody site.
    asan_labels = {s.binary_label for s in asan}
    kept = [s for s in sites if s.binary_label not in asan_labels]
    superseded = len(sites) - len(kept)
    if superseded:
        deps.log(f"  [phase-c/crash-localizer] {superseded} gdb backtrace(s) "
                 "superseded by the sanitized site of the same binary; "
                 f"{len(kept)} kept for binaries the sanitizer did not report.")
    return (*asan, *kept)
