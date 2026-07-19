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
import subprocess
from pathlib import Path

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


def run_asan_sweep(
    test_cmd: str,
    cwd: Path,
    env: dict[str, str],
    *,
    timeout: int = 900,
) -> tuple[int, str] | None:
    """Run the cluster test command under ASAN in an isolated target dir.
    Returns (rc, combined output) — the output carries the ASAN report when
    anything faulted — or None when the run could not start (no cargo,
    timeout: diagnostics unavailable, caller falls back to gdb evidence)."""
    argv = asan_test_cmd(test_cmd)
    asan_env = {
        **env,
        "RUSTFLAGS": (env.get("RUSTFLAGS", "") + " -Zsanitizer=address").strip(),
        "RUSTDOCFLAGS": (env.get("RUSTDOCFLAGS", "") + " -Zsanitizer=address").strip(),
        "CARGO_TARGET_DIR": str(cwd / "target-asan"),
        # halt_on_error keeps the report tight; use-after-return detection is
        # the load-bearing flag for dangling stack-'static schemes.
        "ASAN_OPTIONS": "detect_stack_use_after_return=1:halt_on_error=1",
    }
    try:
        proc = subprocess.run(
            argv, cwd=cwd, env=asan_env, capture_output=True, text=True,
            timeout=timeout, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return proc.returncode, proc.stdout + "\n" + proc.stderr
