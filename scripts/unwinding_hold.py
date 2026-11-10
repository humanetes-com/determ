#!/usr/bin/env python3
"""Which still-stubbed functions hold a tree's failing tests: by first failure,
and by the panic during unwinding that aborts the test's process.

WHY. Since test isolation (2026-09-19) the loop holds a failing test only by
the stub its FIRST panic sits in (`domain/test_outcome.stub_hold_of`). When a
test first fails somewhere and then a stubbed destructor path panics while
unwinding, the process aborts (`panic in a destructor during cleanup`), and
that second stub is named by no hold. On run `336559-eb0222a5` (2026-11-08)
`Drop for Error` was the cleanup panic of nearly every aborting test, was
named as a "still-stubbed sibling" for about 8 h and never claimed; on run
`630503-f5ca6540` (2026-11-09) fill 2 spent 268 of 269 minutes inside the
abort window with 26.6 minutes of model time. While the window is open every
reading re-runs the aborted binaries one test per process
(`cargo_runner.isolate_aborted_binaries`), which is the judging cost. This
script measures both facts on a copy of a cluster tree, offline.

USAGE.
    python scripts/unwinding_hold.py <cluster_tree> [--out DIR]
        [--timeout 600] [--no-isolation-control] [--top 15]
        [--toolchain nightly --rustflags "--cfg X"] [--widths 1,4]
    python scripts/unwinding_hold.py --reread DIR [--top 15]

    --reread DIR re-reads DIR/capture.txt against DIR/tree (a previous run's
    --out), runs no cargo, and prints the hold and take-order lines only.
    --widths times one isolated reading per `PHASE_C_ISOLATION_WORKERS`
    value listed (the isolation pool; 1 is the serial loop).

    <cluster_tree> is e.g. tmp/anyhow/.skeleton-filled/stable. It is copied
    with symlinks followed (the tree's `tests` is a symlink into the sealed
    oracle; never run in place), `target/` excluded, into --out (default: a
    new temp dir). Runs `cargo test --no-fail-fast` through the pipeline's
    own `run_cargo` (isolation on, as every reading does), then once more with
    `PHASE_C_TEST_ISOLATION=0` unless --no-isolation-control. CPU only, no
    GPU, no model. Each run builds the copy cold the first time (a minute or
    two on anyhow). Needs the release extractor
    (tools/skeleton_extractor/target/release/skeleton-extractor).

HOW TO INTERPRET THE OUTPUT.
    reading: rc, wall seconds with isolation on (and off, the control). The
        difference is what one full-suite reading pays for isolation on this
        tree; a keep pays it in each full reading it takes, per world.
    failing / aborted / panic-in-panic: failing tests, how many of them had
        their own process abort, and how many of those aborts are the
        runtime's double panic.
    FIRST-FAILURE holders: per still-stubbed function, how many failing tests
        first fail inside it. This is today's hold (`StubHold.by_test`).
    UNWINDING holders: per still-stubbed function, how many panic-in-panic
        tests panicked in it AFTER their first failure, i.e. while unwinding.
        A function here and not in the first list is a stub no hold names.
    take order (pipeline): `fill_scheduler.unwritten_holders` over this
        capture's `StubHold` (first failure and unwinding), nothing planned,
        landed or asked: the order a fill takes these holders after a keep.
    take order, file sort: the order before 2026-11-09 (`sorted` by file and
        name, first failure only), the negative control. Decision rule: the
        stub that ends the abort window (the top unwinding holder) should be
        first in the pipeline order; if it is not, the holder order is
        delaying it.
    width N: wall seconds of one isolated reading at that pool width, and
        whether its verdicts equal the first width's. Equal verdicts and a
        falling wall are the isolation pool working; unequal verdicts mean the
        crate's tests interfere when run side by side (set
        `PHASE_C_ISOLATION_WORKERS=1` for it).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline_core.adapters.cargo import CargoCli  # noqa: E402
from pipeline_core.adapters.extractor import CliExtractor  # noqa: E402
from pipeline_core.adapters.structure import CliStructureReader  # noqa: E402
from pipeline_core.application.reconstruct.attribution import (  # noqa: E402
    make_fn_site_locator,
)
from pipeline_core.application.reconstruct.cargo_runner import run_cargo  # noqa: E402
from pipeline_core.application.reconstruct.fill_scheduler import (  # noqa: E402
    unwritten_holders,
)
from pipeline_core.domain.abort_cause import AbortCause  # noqa: E402
from pipeline_core.domain.fill_asks import FillAsks  # noqa: E402
from pipeline_core.domain.models import FnLocator, FnSite  # noqa: E402
from pipeline_core.domain.test_outcome import (  # noqa: E402
    outcomes_of,
    stub_hold_of,
)
from pipeline_core.domain.test_results import (  # noqa: E402
    TestStateReading,
    parse_binary_results,
)

EXTRACTOR = ROOT / "tools/skeleton_extractor/target/release/skeleton-extractor"


def copy_tree(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    tar = subprocess.Popen(["tar", "-ch", "--exclude=./target", "-C", str(src), "."],
                           stdout=subprocess.PIPE)
    subprocess.run(["tar", "-x", "-C", str(dst)], stdin=tar.stdout, check=True)
    if tar.wait() != 0:
        raise SystemExit(f"tar of {src} failed")


def reading(tree: Path, env: dict[str, str], cmd: str, timeout: int,
            isolation: bool) -> tuple[int, str, float]:
    run_env = dict(env)
    run_env["PHASE_C_TEST_ISOLATION"] = "1" if isolation else "0"
    old = os.environ.get("PHASE_C_TEST_ISOLATION")
    os.environ["PHASE_C_TEST_ISOLATION"] = run_env["PHASE_C_TEST_ISOLATION"]
    try:
        t0 = time.monotonic()
        rc, out = run_cargo(CargoCli(), cmd, tree, run_env, timeout=timeout)
        return rc, out, time.monotonic() - t0
    finally:
        if old is None:
            os.environ.pop("PHASE_C_TEST_ISOLATION", None)
        else:
            os.environ["PHASE_C_TEST_ISOLATION"] = old


def label(s: FnSite) -> str:
    return f"{s.file}::{s.fn_name}" + (f" ({s.container})" if s.container else "")


def report(tree: Path, cap: str, top: int) -> None:
    """The hold and the take order, read by the pipeline's own functions."""
    recs = CliStructureReader(CliExtractor(EXTRACTOR)).enumerate_fns(tree) or []
    stub = {(r["file"], r["fn_name"], r.get("container", "") or "")
            for r in recs if r.get("has_unimplemented_body")}
    locate = make_fn_site_locator(CliStructureReader(CliExtractor(EXTRACTOR)), tree)

    def is_stub(s: FnSite) -> bool:
        return (s.file, s.fn_name, s.container) in stub

    outs = outcomes_of(cap)
    failing = [o for o in outs.values() if not o.passed]
    aborted = sum(1 for o in failing if o.aborted)
    pip = sum(1 for o in failing if o.abort is not None
              and o.abort.cause is AbortCause.PANIC_IN_PANIC)
    hold = stub_hold_of(cap, locate, is_stub=is_stub)
    first: Counter[FnSite] = Counter(s for ss in hold.by_test.values() for s in ss)
    unwinding: Counter[FnSite] = Counter(
        s for ss in hold.unwinding.values() for s in ss)
    print(f"failing={len(failing)} aborted={aborted} panic-in-panic={pip}")
    print("FIRST-FAILURE holders (StubHold.by_test):")
    for s, c in first.most_common(top):
        print(f"  {c:4d} {label(s)}")
    print("UNWINDING holders (StubHold.unwinding):")
    for s, c in unwinding.most_common(top):
        print(f"  {c:4d} {label(s)}  (first-failure hold: {first.get(s, 0)})")
    order = unwritten_holders(hold.by_test, ahead=(), planned_held=(),
                              landed=(), opened=(), asked=FillAsks(),
                              unwinding=hold.unwinding)
    old = sorted({FnLocator(s.file, s.fn_name) for s in first},
                 key=lambda k: (k.file, k.fn_name))
    print("take order (pipeline)  :", ", ".join(
        f"{u.file}::{u.fn_name}" for u in order[:8]))
    print("take order, file sort  :", ", ".join(
        f"{u.file}::{u.fn_name}" for u in old[:8]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tree", type=Path, nargs="?")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--reread", type=Path)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--no-isolation-control", action="store_true")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--toolchain", default="")
    ap.add_argument("--rustflags", default="")
    ap.add_argument("--widths", default="")
    a = ap.parse_args()

    if a.reread is not None:
        report(a.reread / "tree", (a.reread / "capture.txt").read_text(), a.top)
        return 0
    if a.tree is None:
        ap.error("a cluster tree, or --reread DIR")
    out_dir = a.out or Path(tempfile.mkdtemp(prefix="unwinding_hold_"))
    tree = out_dir / "tree"
    if not tree.exists():
        copy_tree(a.tree.resolve(), tree)
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = str(out_dir / "target")
    if a.rustflags:
        env["RUSTFLAGS"] = (env.get("RUSTFLAGS", "") + " " + a.rustflags).strip()
    cmd = "cargo " + (f"+{a.toolchain} " if a.toolchain else "") + "test --no-fail-fast"
    # Warm the build so the timings below are test time, not compile time.
    subprocess.run(cmd.replace("--no-fail-fast", "--no-run").split(), cwd=tree,
                   env=env, capture_output=True)

    rc, cap, secs = reading(tree, env, cmd, a.timeout, isolation=True)
    print(f"tree copy: {tree}")
    print(f"reading, isolation on : rc={rc} wall={secs:.1f}s")
    if not a.no_isolation_control:
        rc0, _cap0, secs0 = reading(tree, env, cmd, a.timeout, isolation=False)
        print(f"reading, isolation off: rc={rc0} wall={secs0:.1f}s (control)")
    (out_dir / "capture.txt").write_text(cap)
    first_keyed = None
    for w in [x for x in a.widths.split(",") if x.strip()]:
        old_w = os.environ.get("PHASE_C_ISOLATION_WORKERS")
        os.environ["PHASE_C_ISOLATION_WORKERS"] = w.strip()
        try:
            _rc_w, cap_w, secs_w = reading(tree, env, cmd, a.timeout, isolation=True)
        finally:
            if old_w is None:
                os.environ.pop("PHASE_C_ISOLATION_WORKERS", None)
            else:
                os.environ["PHASE_C_ISOLATION_WORKERS"] = old_w
        keyed = TestStateReading(parse_binary_results(cap_w)).keyed()
        if first_keyed is None:
            first_keyed = keyed
        same = (keyed.passed, keyed.failed, keyed.aborted) == (
            first_keyed.passed, first_keyed.failed, first_keyed.aborted)
        print(f"width {w.strip()}: wall={secs_w:.1f}s verdicts equal to first width: {same}")
    report(tree, cap, a.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
