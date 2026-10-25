"""AddressSanitizer crash-localization adapter.

The gdb fast path only sees a crash when the stale pointer happens to fault
NATIVELY — a dangling stack slot often still contains plausible bytes, so
native reproduction flips with mmap/stack state (observed: the same binary
alternating 0/10 and 10/10 SIGSEGV across identical invocations). ASAN
instruments the code itself: with ``detect_stack_use_after_return=1`` every
escaped stack frame is quarantined, so a read through a dangling
stack-'static faults DETERMINISTICALLY with an exact report naming the
faulting frame and the frame that allocated the dead slot.

Cost: one nightly build into a dedicated ``target-asan`` dir + a serial test
run — bounded, and only paid when a sweep already observed vanished tests.

The report is parsed line-structurally (ASAN's ``#N 0x… in <func> <file>:
<line>`` frame lines) into the same ``{"func","file","line"}`` records the
gdb adapter emits, feeding one shared :func:`site_from_frames` path.
"""

from __future__ import annotations

import shlex
from datetime import UTC, datetime
from pathlib import Path

from pipeline_core.adapters.process import run_bounded
from pipeline_core.domain.test_results import join_capture

_HOST_TRIPLE = "x86_64-unknown-linux-gnu"


def asan_test_cmd(test_cmd: str) -> list[str]:
    """`cargo test …` → `cargo +nightly test --target <host> …` argv.
    The explicit ``--target`` keeps proc-macros/build-scripts UNsanitized
    (they run on the host during the build and must not link the ASAN
    runtime); RUSTFLAGS applies only to target artifacts then."""
    toks = shlex.split(test_cmd)
    out: list[str] = []
    for i, t in enumerate(toks):
        out.append(t)
        if t == "cargo" and (i + 1 >= len(toks) or not toks[i + 1].startswith("+")):
            out.append("+nightly")
        if t == "test" and "--target" not in toks:
            out.extend(["--target", _HOST_TRIPLE])
    return out


def asan_target_dir_name(world: str) -> str:
    """The sanitized target dir of `world` under the tree: `target-asan`
    for a one-world run, `target-asan-<world>` otherwise (a `/` in a world
    name, as in a feature world, becomes `-`)."""
    return f"target-asan-{world.replace('/', '-')}" if world else "target-asan"


def run_asan_sweep(
    test_cmd: str,
    cwd: Path,
    env: dict[str, str],
    *,
    world: str = "",
    timeout: int = 900,
) -> tuple[int, str] | None:
    """Run the cluster test command under ASAN in an isolated target dir.
    Returns (rc, combined output) — the output carries the ASAN report when
    anything faulted — or None when the run could not start (no cargo,
    timeout: diagnostics unavailable, caller falls back to gdb evidence).

    `env` is the env of the build world whose crash is localized (its
    RUSTFLAGS carry that world's cfg), and `world` names that world's own
    sanitized target dir: two worlds' flags in one target dir rebuild each
    other's artefacts on every sweep. "" is the one-world dir."""
    argv = asan_test_cmd(test_cmd)
    asan_env = {
        **env,
        "RUSTFLAGS": (env.get("RUSTFLAGS", "") + " -Zsanitizer=address").strip(),
        "RUSTDOCFLAGS": (env.get("RUSTDOCFLAGS", "") + " -Zsanitizer=address").strip(),
        "CARGO_TARGET_DIR": str(cwd / asan_target_dir_name(world)),
        # halt_on_error keeps the report tight; use-after-return detection is
        # the load-bearing flag for dangling stack-'static schemes.
        "ASAN_OPTIONS": "detect_stack_use_after_return=1:halt_on_error=1",
    }
    proc = run_bounded(argv, cwd=cwd, env=asan_env, timeout=float(timeout))
    if proc.failure is not None:
        return None
    # The one capture layout: a sanitized run's sections are read like the
    # sweep's, and a stderr line (trybuild's coloured progress, run
    # `12616-8345ae1d`) never enters a stdout section.
    out = join_capture(proc.stdout, proc.stderr)
    _archive_report(out)
    return proc.returncode, out


def _archive_report(output: str) -> None:
    """Keep the WHOLE sanitized run's output under `logs/asan/`.

    The report was parsed and dropped, so the only way to learn who freed the
    memory was to pay for another instrumented build — minutes of GPU-idle
    rebuild per question. A capped excerpt on the event is not enough either:
    it truncated mid-report and made a LeakSanitizer section look like the
    use-after-free's origin stack. Failures are swallowed — an archive that
    breaks the sweep it is documenting would be worse than no archive."""
    try:
        d = Path("logs/asan")
        d.mkdir(parents=True, exist_ok=True)
        # Microseconds, not seconds: two sweeps can land inside one second and
        # the later would silently overwrite the earlier.
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        (d / f"asan-{stamp}.txt").write_text(output, encoding="utf-8")
        for old in sorted(d.glob("asan-*.txt"))[:-20]:   # keep the last 20
            old.unlink(missing_ok=True)
    except OSError:
        pass
