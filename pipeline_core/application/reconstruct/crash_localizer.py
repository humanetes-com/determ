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
    Returns () when nothing crashed or nothing could be localized — callers
    must treat an empty result on a shrunken oracle as "unlocalized", never
    as health."""
    crashed = [b for b in parse_binary_results(sweep_output) if not b.completed]
    if not crashed:
        return ()
    sites: list[CrashSite] = []
    for b in crashed[:max_binaries]:
        if not b.exe_path:
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
    if sites or deps.asan_sweep is None:
        return tuple(sites)
    # gdb saw no native fault — the crash is layout-dependent. ASAN is the
    # deterministic detector; halt_on_error yields the FIRST error's site,
    # later sweeps localize the next one once this is fixed.
    deps.log("  [phase-c/crash-localizer] native re-run showed no fault "
             "(layout-dependent crash) — running ASAN sweep.")
    asan = deps.asan_sweep(test_cmd, workspace_root, env)
    if asan is None:
        return ()
    _rc, out = asan
    rep = parse_asan_report(out)
    if rep is None:
        return ()
    kind, access, origin = rep
    access = relativize_frame_files(access, str(workspace_root))
    origin = relativize_frame_files(origin, str(workspace_root))
    # binary label + in-flight test from the ASAN run's own cargo output.
    a_crashed = [b for b in parse_binary_results(out) if not b.completed]
    label = a_crashed[0].label if a_crashed else "?"
    test = a_crashed[0].dangling if a_crashed else None
    site = site_from_frames(label, test, kind, access,
                            origin_raw_frames=origin)
    return (site,) if site.all_frames else ()
