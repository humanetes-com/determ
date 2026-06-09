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
    res = subprocess.run(
        ["cargo", "test"], cwd=_CRATE, capture_output=True, text=True, timeout=180,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    # 3 unit + 3 integration tests, all green.
    passed = res.stdout.count(" ... ok")
    assert passed == 6, f"expected 6 passing tests, saw {passed}\n{res.stdout}"
