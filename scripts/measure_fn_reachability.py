#!/usr/bin/env python3
"""WHY: task0c must not spend GPU hours testing functions no test can execute.
`domain.fn_reachability` PROVES that set from rustc's own generic MIR and
public index. A proof is only worth acting on if it is never
wrong, so this script is the offline validation of the oracle on a real
workspace: no model, no pipeline run, one cargo build per cluster world.

For every cluster world of the workspace it runs the production reader
(`fn_criterion.read_verdicts`, the same code task0c calls at cluster entry) on
a scratch copy of the sources and prints:
  - the functions proved unreachable by construction, with the proof;
  - with --why, every other candidate and the fact that keeps it testable;
and then checks SOUNDNESS against everything that is known to have executed:
  - the stored fn records (`.cluster_augmented/*.fns.json` `executed`);
  - with --measure-executed, a fresh `cargo llvm-cov` run of the workspace's
    INTEGRATION tests (`tests/*.rs` targets only: the proof speaks for
    `tests/`, and an in-crate unit test may legitimately run a private fn).
A proved function that appears in either set is a counterexample: the script
prints it and exits 1.

USAGE: .venv/bin/python scripts/measure_fn_reachability.py [workspace]
           [--why] [--measure-executed] [--cluster NAME] [--expect FILE]
       workspace defaults to tmp/anyhow; tests/fixtures/microcrate and
       tests/fixtures/hardcrate are two more corpora with integration tests.
       --expect FILE  a JSON {"flagged": [labels]} (label = `file::fn
                      [container]`); the proved set must equal it per cluster
                      that lists it (see tests/fixtures/fn_reachability/
                      expected/verdicts.json for the shape).

HOW TO INTERPRET: exit 0 and an empty "COUNTEREXAMPLES" section is the
reading that licenses the oracle. On tmp/anyhow the reading 2026-10-03 is the
three `Clone` impls of `ptr.rs` (`Own`, `Ref`, `Mut`) and `_assert_send_sync`
in both worlds, and NOT `DisplayError`/`ErrorImpl` `Debug` nor
`thiserror_provide` (they are kept by a `dyn` coercion and a public
signature). Missing from the proved set is never an error (the oracle may be
too small); present in the executed set is always one. Exit 2 means a world
had no reading (cargo failed): nothing was proved there, and the reason is
printed.
"""
from __future__ import annotations

import argparse
import functools
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline_core.adapters.cargo import cluster_cargo_env  # noqa: E402
from pipeline_core.adapters.extractor import CliExtractor  # noqa: E402
from pipeline_core.adapters.fn_reachability import measure_world_reachability  # noqa: E402
from pipeline_core.adapters.structure import load_baked_clusters  # noqa: E402
from pipeline_core.application.tasks.fn_criterion import (  # noqa: E402
    FnCriterionInputs,
    read_function_execution,
    read_verdicts,
)
from pipeline_core.domain.fn_execution import FnCoverageRecord, FunctionExecution, site_label  # noqa: E402
from pipeline_core.domain.models import BuildCluster, FnSite  # noqa: E402

_EXTRACTOR = ROOT / "tools/skeleton_extractor/target/release/skeleton-extractor"
_COPY = ("src", "tests", "build.rs", "Cargo.toml", "Cargo.lock")


def _scratch(ws: Path) -> Path:
    dst = Path(tempfile.mkdtemp(prefix="fn_reach_"))
    for name in _COPY:
        src = ws / name
        if src.is_dir():
            shutil.copytree(src, dst / name)
        elif src.is_file():
            shutil.copy2(src, dst / name)
    return dst


def _lcov_functions(lcov: str, root: Path) -> dict[tuple[str, int], int]:
    """`(src/file.rs, first line) -> max count`, as the coverage adapter keys it."""
    out: dict[tuple[str, int], int] = {}
    file = ""
    lines: dict[str, int] = {}
    hits: dict[str, int] = {}
    for raw in lcov.splitlines():
        if raw.startswith("SF:"):
            file, lines, hits = raw[3:], {}, {}
        elif raw.startswith("FN:"):
            n, _, name = raw[3:].partition(",")
            if n.strip().isdigit():
                lines[name] = int(n)
        elif raw.startswith("FNDA:"):
            c, _, name = raw[5:].partition(",")
            if c.strip().isdigit():
                hits[name] = max(hits.get(name, 0), int(c))
        elif raw == "end_of_record" and file:
            try:
                rel = Path(file).resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                continue
            if rel.startswith("src/"):
                for name, ln in lines.items():
                    out[(rel, ln)] = max(out.get((rel, ln), 0), hits.get(name, 0))
    return out


def _measure_integration(scratch: Path, cluster: BuildCluster) -> dict[tuple[str, int], int] | str:
    tests = sorted(p.stem for p in (scratch / "tests").glob("*.rs")) if (scratch / "tests").is_dir() else []
    if not tests:
        return "the workspace has no tests/*.rs integration target"
    env = cluster_cargo_env(cluster)
    env["CARGO_TARGET_DIR"] = str(scratch / "target" / "fn-reach-cov" / cluster.name)
    out = scratch / f"lcov_{cluster.name}.info"
    cmd = ["cargo"] + ([f"+{cluster.toolchain}"] if cluster.toolchain else [])
    cmd += ["llvm-cov", "--lcov", "--output-path", str(out)]
    for t in tests:
        cmd += ["--test", t]
    run = subprocess.run(cmd, cwd=scratch, env=env, capture_output=True, text=True, timeout=1800)
    if not out.exists():
        tail = (run.stderr or "").strip().splitlines()[-1:] or [""]
        return f"cargo llvm-cov produced no lcov (rc={run.returncode}): {tail[0][:160]}"
    return _lcov_functions(out.read_text(encoding="utf-8", errors="replace"), scratch)


def _stored_executed(ws: Path, cluster: str) -> set[FnSite]:
    out: set[FnSite] = set()
    for p in sorted((ws / ".cluster_augmented").glob("*.fns.json")):
        rec = FnCoverageRecord.from_json(p.read_text(encoding="utf-8"))
        if rec is not None and rec.measured and p.name.startswith(f"{cluster}__"):
            out |= set(rec.executed)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("workspace", nargs="?", default="tmp/anyhow")
    ap.add_argument("--why", action="store_true")
    ap.add_argument("--measure-executed", action="store_true")
    ap.add_argument("--cluster")
    ap.add_argument("--expect")
    a = ap.parse_args()
    ws = (ROOT / a.workspace) if not Path(a.workspace).is_absolute() else Path(a.workspace)
    if not _EXTRACTOR.exists():
        print(f"extractor binary missing: {_EXTRACTOR}")
        return 2
    clusters = load_baked_clusters(ws) if (ws / ".skeleton" / "_clusters.json").exists() else []
    if not clusters:
        clusters = [BuildCluster("stable", {}, [], [], [], "")]
    if a.cluster:
        clusters = [c for c in clusters if c.name == a.cluster]
    expect = json.loads(Path(a.expect).read_text(encoding="utf-8")) if a.expect else {}
    scratch = _scratch(ws)
    status = 0
    try:
        extractor = CliExtractor(_EXTRACTOR)
        enumerate_fns = extractor.enumerate_fns
        inputs = FnCriterionInputs(
            enumerate_fns=enumerate_fns, enumerate_mods=extractor.enumerate_mods,
            measure_reachability=functools.partial(
                measure_world_reachability, cluster_env=cluster_cargo_env, timeout=1800))
        for cluster in clusters:
            print(f"== cluster {cluster.name} ({ws})")
            verdicts, reason = read_verdicts(scratch, cluster, inputs)
            if reason:
                print(f"  NO READING: {reason}")
                status = max(status, 2)
                continue
            proved = {s: v.proof for s, v in verdicts.items() if v.proof}
            print(f"  candidates judged {len(verdicts)}, proved unreachable by construction {len(proved)}")
            for site in sorted(proved):
                print(f"  PROVED {site_label(site)}\n         {proved[site]}")
            if a.why:
                for site in sorted(verdicts):
                    if site not in proved:
                        print(f"  kept   {site_label(site)}  |  {verdicts[site].kept}")
            if cluster.name in expect.get("by_cluster", {}) or (expect and "flagged" in expect):
                want = set(expect.get("by_cluster", {}).get(cluster.name, expect.get("flagged", [])))
                got = {site_label(s) for s in proved}
                print(f"  expectation: {'MET' if got == want else 'NOT MET'}"
                      + ("" if got == want else f" (missing {sorted(want - got)}, extra {sorted(got - want)})"))
                if got != want:
                    status = max(status, 1)
            executed = _stored_executed(ws, cluster.name)
            sources = {"stored fn records": executed}
            if a.measure_executed:
                counts = _measure_integration(scratch, cluster)
                if isinstance(counts, str):
                    print(f"  integration run: NO READING ({counts})")
                else:
                    reading = read_function_execution(scratch, cluster, counts, inputs)
                    if isinstance(reading, FunctionExecution):
                        sources["integration tests (llvm-cov)"] = set(reading.executed)
                    else:
                        print(f"  integration run: NO READING ({reading})")
            print("  COUNTEREXAMPLES (proved but executed):")
            bad = False
            for name, ran in sources.items():
                hit = sorted(set(proved) & ran)
                print(f"    {name}: {len(ran)} executed function(s) checked, {len(hit)} counterexample(s)")
                for site in hit:
                    bad = True
                    print(f"      !! {site_label(site)}")
            if bad:
                status = max(status, 1)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
