"""L3 fixture health — CODE_ARCHITECTURE.md R10.1/R10.5.

Confirms the micro fixture crate is a well-formed, fast reconstruction target: it
compiles and all 6 tests (3 unit + 3 integration) pass under real cargo. This does
NOT run the pipeline — the full real-model reconstruction against this crate is the
on-demand L3 smoke (manual/nightly). Gated on cargo via SMOKE_FULL=1.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

needs_cargo = pytest.mark.skipif(
    os.environ.get("SMOKE_FULL") != "1",
    reason="needs cargo; set SMOKE_FULL=1",
)

_CRATE = Path(__file__).parent / "fixtures" / "microcrate"


@needs_cargo
def test_microcrate_compiles_and_passes() -> None:
    """Default cluster: cap module and gated tests are excluded."""
    res = subprocess.run(
        ["cargo", "test"], cwd=_CRATE, capture_output=True, text=True, timeout=180,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    # 7 unit (4 lib + 1 counter + 2 parse) + 3 integration tests, all green.
    passed = res.stdout.count(" ... ok")
    assert passed == 10, f"expected 10 passing tests, saw {passed}\n{res.stdout}"


@needs_cargo
def test_microcrate_extra_cap_cluster_passes() -> None:
    """Second cluster (--cfg extra_cap): cap module + gated tests compile and
    pass — the harness for the cluster the pipeline must augment separately."""
    env = dict(os.environ, RUSTFLAGS="--cfg extra_cap -A warnings")
    res = subprocess.run(
        ["cargo", "test", "--target-dir", "target/extra_cap"],
        cwd=_CRATE, capture_output=True, text=True, timeout=180, env=env,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    # 10 unit (4 lib + 2 counter + 2 parse + 2 cap) + 4 integration, all green.
    passed = res.stdout.count(" ... ok")
    assert passed == 14, f"expected 14 passing tests, saw {passed}\n{res.stdout}"
