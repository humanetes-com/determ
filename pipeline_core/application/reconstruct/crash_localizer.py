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
    CrashSite,
    asan_stack_excerpt,
    is_memory_fault_signal,
    parse_asan_report,
    relativize_frame_files,
    site_from_frames,
)
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


def _asan_site(
    deps: CrashLocalizerDeps,
    test_cmd: str,
    workspace_root: Path,
    env: dict[str, str],
) -> CrashSite | None:
    """Run the sanitized sweep and build the ORIGIN-bearing site, or None."""
    if deps.asan_sweep is None:
        return None
    asan = deps.asan_sweep(test_cmd, workspace_root, env)
    if asan is None:
        return None
    _rc, out = asan
    rep = parse_asan_report(out)
    if rep is None:
        return None
    kind, access, origin = rep
    access = relativize_frame_files(access, str(workspace_root))
    origin = relativize_frame_files(origin, str(workspace_root))
    # binary label + in-flight test from the ASAN run's own cargo output.
    a_crashed = [b for b in parse_binary_results(out) if not b.completed]
    label = a_crashed[0].label if a_crashed else "?"
    test = a_crashed[0].dangling if a_crashed else None
    site = site_from_frames(label, test, kind, access, origin_raw_frames=origin,
                            raw_report=asan_stack_excerpt(out),
                            origin_capable=True)
    if site.origin_unattributed:
        # The report HAD a free/alloc stack (that is what stacks[1] is); every
        # frame in it was filtered out as non-crate. Say so, and keep the raw
        # stacks on the site so the next reader can see who freed the memory
        # without paying for another instrumented build.
        deps.log("  [phase-c/crash-localizer] "
                 f"{kind} in {label}: no in-crate frame in the origin stack "
                 f"({len(origin)} raw frame(s)) — blaming NOBODY. The access "
                 "path is recorded; the fn that invalidated the memory is not "
                 "identified.")
    return site if site.all_frames else None


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
    its ORIGIN-bearing site supersedes gdb's for that binary. gdb's result is
    kept when the sanitizer produces nothing (no nightly, sanitizer
    unsupported, crash not reproducible under instrumentation).

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
    asan = _asan_site(deps, test_cmd, workspace_root, env)
    if asan is None:
        return tuple(sites)
    # Once ONE origin is known, the other memory-fault backtraces of the same
    # round are almost certainly downstream readers of that same corruption
    # (one dangling pointer kills every binary that dereferences it). Blaming
    # their faulting frames would hand the model correct fns to "fix" — the
    # misattribution this localizer exists to end. Keep only origin-bearing
    # blame plus crashes of a different class; the sanitizer halts on the
    # first error, so the next sweep localizes the next origin once this one
    # is fixed.
    #
    # That consolidation is conditional on an origin ACTUALLY being known.
    # When the sanitizer named no in-crate origin the premise is absent —
    # nothing was localized, so discarding the other backtraces would trade
    # real evidence for a blame that does not exist.
    if asan.origin_unattributed:
        return (asan, *[s for s in sites if s.binary_label != asan.binary_label])
    kept = [s for s in sites
            if s.binary_label != asan.binary_label
            and not (_is_memory_fault(s.signal) and not s.origin_frames)]
    dropped = len(sites) - len(kept) - sum(
        1 for s in sites if s.binary_label == asan.binary_label)
    if dropped > 0:
        deps.log(f"  [phase-c/crash-localizer] {dropped} further memory-fault "
                 "backtrace(s) not blamed — same corruption read downstream; "
                 f"blame stays on {asan.blamed_locator}.")
    return (asan, *kept)
