"""stall-trip parser for scripts/watch_plateau.sh.

The plateau-loop skill wakes at stall>=3 on the judged-sweep summary, not
on a pipeline stop. Negative control: stall=2 waits; a non-summary line
waits. The trip line is stall=3 on the cargo-test summary.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "watch_plateau.sh"

_SUMMARY = (
    "  [stable] [phase-c/tdd] iter 5: cargo test rc=101 (0.8s); "
    "pass=122 fail=3 oracle=125 (improved=False, stall={stall}/0); "
    "frontier=4 fn(s) (behavior), 0 skipped as exhausted"
)


def _check(line: str, trip: str = "3") -> tuple[int, str]:
    r = subprocess.run(
        ["bash", str(_SCRIPT), "--check-line", line],
        capture_output=True,
        text=True,
        env={**os.environ, "STALL_TRIP": trip},
    )
    return r.returncode, r.stdout.strip()


def test_stall_3_trips_and_stall_2_waits():
    rc, out = _check(_SUMMARY.format(stall=3))
    assert rc == 0 and out.startswith("TRIP stall=3 iter=5")
    rc, out = _check(_SUMMARY.format(stall=2))
    assert rc == 1 and out.startswith("WAIT")


def test_non_summary_waits():
    rc, out = _check("  [stable] [phase-c/tdd] iter 5: running cargo test...")
    assert rc == 1 and out == "WAIT"


def test_stall_14_still_trips():
    rc, out = _check(_SUMMARY.format(stall=14))
    assert rc == 0 and "stall=14" in out
