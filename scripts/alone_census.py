#!/usr/bin/env python3
"""Run every generated oracle test alone, in its own process, on the original
crate, in every build world, and name the ones that fail alone.

WHY. An admitted generated test that passes only when another test in its
binary ran first (a `std::env::set_var` read once per process) can flip a
reading by thread scheduling: run `801510-bb22799f` (2026-10-20) read task 3
red on `tests/fmt_gen_1.rs::debug_basic_message_is_preserved` and green on the
re-run with no splice between them. The admission check now re-runs each
declared test with `--exact <name> --test-threads=1`
(`adapters.test_worlds.failing_alone`); it logs only refusals, so a run whose
log is silent cannot tell "checked and clean" from "never checked". This
census is that check on the admitted oracle, taken from the outside.

USAGE.
    python scripts/alone_census.py [--project tmp/anyhow] [--tag task0c-augmented-anyhow]
                                   [--glob 'tests/*_gen_*.rs'] [--worlds stable,nightly]

Extracts the tag's tree with `git archive` into a scratch dir (the workspace
is never touched), builds the test binaries once per world under their own
target dir (`cargo test --no-run`; nightly world = `cargo +nightly` with
`RUSTFLAGS=--cfg error_generic_member_access`, the anyhow cfg of
build_configs.md), then for each generated test binary runs the whole binary
once (the old file-level reading) and every test alone. CPU only, no GPU, no
model. A few minutes, dominated by the two builds.

HOW TO INTERPRET THE OUTPUT. Per world:
  `<file>: file=pass|fail|gated alone_fail=[...]`, printed only for files with
  an alone failure or a failing file run; then
  `world W: files=F tests=T file_fail=X alone_fail=Y gated=G`.
  gated = the binary has no test in this world (a cfg-gated file), not a
  failure. A test in alone_fail whose file run passes is exactly the
  order-dependent class: the oracle holds it and its verdict depends on
  scheduling. On-theory after the alone check ships: alone_fail=0 in every
  world. file_fail>0 means the original fails the file itself, which is the
  withdrawal question, not this one.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_WORLDS = {
    "stable": ([], {}),
    "nightly": (["+nightly"], {"RUSTFLAGS": "--cfg error_generic_member_access"}),
}


def _build(tree: Path, world: str, glob: str) -> dict[str, str]:
    tc, env_add = _WORLDS[world]
    env = dict(os.environ, CARGO_TARGET_DIR=str(tree / f"target-{world}"), **env_add)
    p = subprocess.run(["cargo", *tc, "test", "--no-run", "--message-format=json"],
                       cwd=tree, env=env, capture_output=True, text=True)
    bins: dict[str, str] = {}
    for line in p.stdout.splitlines():
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("reason") != "compiler-artifact" or not m.get("executable"):
            continue
        src = Path(m["target"]["src_path"]).relative_to(tree).as_posix()
        if fnmatch.fnmatch(src, glob):
            bins[src] = m["executable"]
    if p.returncode != 0:
        sys.stderr.write(f"[{world}] build rc={p.returncode}\n{p.stderr[-2000:]}\n")
    return bins


def _run(exe: str, tree: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([exe, *args], cwd=tree, capture_output=True, text=True,
                          timeout=120)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="tmp/anyhow")
    ap.add_argument("--tag", default="task0c-augmented-anyhow")
    ap.add_argument("--glob", default="tests/*_gen_*.rs")
    ap.add_argument("--worlds", default="stable,nightly")
    a = ap.parse_args()
    with tempfile.TemporaryDirectory(prefix="alone_census_") as d:
        tree = Path(d)
        arc = subprocess.run(["git", "-C", a.project, "archive", a.tag],
                             capture_output=True, check=True)
        subprocess.run(["tar", "-x", "-C", str(tree)], input=arc.stdout, check=True)
        for world in a.worlds.split(","):
            bins = _build(tree, world, a.glob)
            tests = file_fail = alone_fail = gated = 0
            for src, exe in sorted(bins.items()):
                names = [l[:-len(": test")] for l in
                         _run(exe, tree, ["--list", "--format", "terse"]).stdout.splitlines()
                         if l.endswith(": test")]
                if not names:
                    gated += 1
                    continue
                file_ok = _run(exe, tree, []).returncode == 0
                bad = [n for n in names
                       if _run(exe, tree, ["--exact", n, "--test-threads=1"]).returncode != 0]
                tests += len(names)
                file_fail += not file_ok
                alone_fail += len(bad)
                if bad or not file_ok:
                    print(f"  [{world}] {src}: file={'pass' if file_ok else 'fail'} alone_fail={bad}")
            print(f"world {world}: files={len(bins)} tests={tests} file_fail={file_fail} "
                  f"alone_fail={alone_fail} gated={gated}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
