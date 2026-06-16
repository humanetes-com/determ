"""L3-RECOVERY fixture health — CODE_ARCHITECTURE.md R10.7.

Confirms the hardcrate is a well-formed, FULLY TEST-DETERMINED reconstruction
target: it compiles and every test passes under real cargo in BOTH clusters (the
ground-truth oracle is satisfiable), AND each trap carries the distinguishing test
case that pins its rule uniquely (so a non-recovery is a pipeline gap, never an
unsolvable ambiguity — FR-022). This does NOT run the pipeline; the full real-model
recovery run is `run_hardcrate.sh` (on-demand/nightly, non-gating). cargo-gated.
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

_CRATE = Path(__file__).parent / "fixtures" / "hardcrate"


@needs_cargo
def test_hardcrate_compiles_and_passes() -> None:
    """Default cluster: cap module + its gated test are excluded."""
    res = subprocess.run(
        ["cargo", "test"], cwd=_CRATE, capture_output=True, text=True, timeout=180,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    # 11 unit (3 lib + 2 round + 3 merge + 2 textsum + 1 counter) + 5 integration.
    passed = res.stdout.count(" ... ok")
    assert passed == 16, f"expected 16 passing tests, saw {passed}\n{res.stdout}"


@needs_cargo
def test_hardcrate_extra_cap_cluster_passes() -> None:
    """Second cluster (--cfg extra_cap): the cap trap + its tests compile and pass."""
    env = dict(os.environ, RUSTFLAGS="--cfg extra_cap -A warnings")
    res = subprocess.run(
        ["cargo", "test", "--target-dir", "target/extra_cap"],
        cwd=_CRATE, capture_output=True, text=True, timeout=180, env=env,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    # +1 unit (cap) +1 integration over the default cluster.
    passed = res.stdout.count(" ... ok")
    assert passed == 18, f"expected 18 passing tests, saw {passed}\n{res.stdout}"


def test_each_trap_carries_its_distinguishing_case() -> None:
    """Every trap must be pinned by a case that a NAIVE impl gets wrong — proof the
    crate is test-determined and the traps are real (no cargo needed; reads source)."""
    src = lambda rel: (_CRATE / rel).read_text(encoding="utf-8")  # noqa: E731

    round_src = src("src/round.rs")
    # half-to-even distinguishers: half-UP would give 1 and 3, truncate would give 1 and 3.
    assert "divide_round(1, 2), 0" in round_src   # vs half-up → 1
    assert "divide_round(5, 2), 2" in round_src   # vs half-up → 3
    assert "divide_round(3, 2), 2" in round_src   # vs truncate → 1

    merge_src = src("src/merge.rs")
    # first-wins distinguisher: last-wins (the habit) would give (5, 2).
    assert "merge_first_wins(&[(5, 1)], &[(5, 2)]), vec![(5, 1)]" in merge_src

    textsum_src = src("src/textsum.rs")
    # empty vs bad-token: naive split-and-parse returns BadToken for "".
    assert 'sum_csv(""), Err(SumError::Empty)' in textsum_src
    assert 'sum_csv("1,x,3"), Err(SumError::BadToken)' in textsum_src

    cap_src = src("src/cap.rs")
    # wrapping distinguisher: a plain sum overflow-panics; 200+100 must wrap to 44.
    assert "wrapping_total(&[200, 100]), 44" in cap_src
