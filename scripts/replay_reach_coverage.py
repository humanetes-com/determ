#!/usr/bin/env python3
"""Replay recorded reach misses and see whether coverage names another label.

WHY. On run 42057-641e589c (2026-10-25) the reach round asked 12 functions
per world and reached 0. A row whose outcome is probe_missed ran, and its
hits did not name the routed label. Those hits can still name the function
under another label, which is a join between the route's label and the
coverage label, or they can name nothing, which means coverage cannot
answer the question. probe_failed rows never produced hit lines and are
not replayed. This script does not ask a model and does not change a prompt.

USAGE. .venv/bin/python scripts/replay_reach_coverage.py --project DIR
--origin DIR [--scratch DIR]
--project is the workspace that holds .skeleton/_reach_<world>.json.
--origin is the original crate. The script copies it with cp -rL into a
scratch directory and deletes any target in the copy, so the original is
not written and a previous build is not the measurement. Needs grcov on
PATH and the skeleton-extractor release binary. No model and no pipeline
process. One instrumented cargo test --no-run per world that has a
probe_missed row, then one cargo run --example and one grcov per row.
Worlds run one after another: the example file is written into the crate.

HOW TO INTERPRET. One block per probe_missed row. SAME means the routed
label was in this run's site labels, so the recorded miss did not
reproduce. JOIN means the routed label was absent and some other label was
present: that is the only reading that would change a reach prompt. NO-HIT
means coverage named nothing. The question is unanswerable from this
replay, and the printed prompt line is the one task 0c already quotes.
RUN-FAILED means the program did not run. The summary's decision line is
`asks unchanged` unless a JOIN row exists. A project with no reach file
prints that and leaves the asks as they are. probe_failed rows are counted
and not run.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pipeline_core.adapters.behaviour_exercises import (  # noqa: E402
    grcov_binary,
    grcov_command,
    instrumented_env,
    t0d_target_dir,
)
from pipeline_core.adapters.behaviour_probe import materialized_probe  # noqa: E402
from pipeline_core.adapters.extractor import CliExtractor  # noqa: E402
from pipeline_core.adapters.reach_store import reach_path  # noqa: E402
from pipeline_core.adapters.structure import load_baked_clusters  # noqa: E402
from pipeline_core.domain.behaviour import (  # noqa: E402
    BehaviourSpec,
    BehaviourSubKind,
    ObservableOutcome,
    OutcomeKind,
)
from pipeline_core.domain.behaviour_coverage import (  # noqa: E402
    SRC_PREFIX,
    coveralls_hit_lines,
    normalize_record_files,
    sites_for_hit_lines,
)
from pipeline_core.domain.behaviour_probe import program_for  # noqa: E402
from pipeline_core.domain.cargo_toml import cargo_package_name  # noqa: E402
from pipeline_core.domain.models import BuildCluster  # noqa: E402
from pipeline_core.domain.reach_probe import PROBE_FAILED, PROBE_MISSED, outcomes_from_json  # noqa: E402

EXTRACTOR = REPO / "tools" / "skeleton_extractor" / "target" / "release" / "skeleton-extractor"


def _cluster(project: Path, name: str) -> BuildCluster:
    try:
        found = load_baked_clusters(project)
    except Exception:
        found = []
    for cluster in found:
        if cluster.name == name:
            return cluster
    return BuildCluster(
        name=name, env_extra={}, tarpaulin_exclude_files=[], excluded_cfg=[],
        active_cfg=[], prompt_nightly_rule="", is_default=(name == "stable"))


def _spec(given: str, when: str, observe: str, world: str) -> BehaviourSpec:
    kind = {
        "debug": OutcomeKind.DEBUG_STRING,
        "alt_debug": OutcomeKind.ALT_DEBUG_STRING,
    }.get((observe or "").strip().lower(), OutcomeKind.DISPLAY_STRING)
    if kind is OutcomeKind.DEBUG_STRING:
        then = ObservableOutcome.debug("x")
    elif kind is OutcomeKind.ALT_DEBUG_STRING:
        then = ObservableOutcome.alt_debug("x")
    else:
        then = ObservableOutcome.display("x")
    return BehaviourSpec(
        "replay", given, when, then, subkind=BehaviourSubKind.OUTCOME, cfg_cluster=world)


def _copy(origin: Path, scratch: Path) -> None:
    if scratch.exists():
        shutil.rmtree(scratch)
    subprocess.run(["cp", "-rL", str(origin), str(scratch)], check=True)
    target = scratch / "target"
    if target.exists():
        shutil.rmtree(target)


def _run(cmd: list[str], cwd: Path, env: dict[str, str], timeout: int) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    ap = argparse.ArgumentParser(description="Replay probe_missed reach rows under coverage.")
    ap.add_argument("--project", type=Path, required=True)
    ap.add_argument("--origin", type=Path, required=True)
    ap.add_argument("--scratch", type=Path, default=None)
    args = ap.parse_args()
    project = args.project.resolve()
    origin = args.origin.resolve()
    skeleton = project / ".skeleton"
    files = sorted(skeleton.glob("_reach_*.json")) if skeleton.is_dir() else []
    if not files:
        print(f"no reach files under {skeleton}; asks unchanged")
        return 0
    grcov = grcov_binary()
    if grcov is None:
        print("grcov not found; asks unchanged")
        return 0
    if not EXTRACTOR.is_file():
        print(f"extractor binary missing at {EXTRACTOR}; asks unchanged")
        return 0
    if not origin.is_dir():
        print(f"origin is not a directory: {origin}; asks unchanged")
        return 0

    scratch_root = args.scratch
    owned = scratch_root is None
    if scratch_root is None:
        scratch_root = Path(tempfile.mkdtemp(prefix="reach-replay-"))
    counts = {"JOIN": 0, "NO-HIT": 0, "SAME": 0, "RUN-FAILED": 0, "NO-PROGRAM": 0}
    failed_rows = 0
    try:
        _copy(origin, scratch_root)
        crate = ""
        try:
            crate = cargo_package_name((scratch_root / "Cargo.toml").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            crate = ""
        records = CliExtractor(EXTRACTOR).enumerate_fns(scratch_root) or []
        recs = normalize_record_files(r.to_dict() for r in records)
        texts: dict[str, str] = {}
        for path in files:
            # _reach_<world>.json — the world is the stem after the prefix.
            world = path.name[len("_reach_"):-len(".json")]
            rows = outcomes_from_json(path.read_text(encoding="utf-8"))
            missed = [o for o in rows.values() if o.outcome == PROBE_MISSED]
            failed_rows += sum(1 for o in rows.values() if o.outcome == PROBE_FAILED)
            if not missed:
                print(f"world {world}: no probe_missed rows")
                continue
            cluster = _cluster(project, world)
            env = instrumented_env(cluster, scratch_root)
            world_target = t0d_target_dir(scratch_root, cluster)
            cov_root = world_target / "coverage" / "reach_replay"
            cov_root.mkdir(parents=True, exist_ok=True)
            build_env = dict(env)
            build_env["LLVM_PROFILE_FILE"] = str(cov_root / "build-%p-%m.profraw")
            rc, err = _run(["cargo", "test", "--no-run", "--quiet"], scratch_root, build_env, 600)
            if rc != 0:
                print(f"world {world}: instrumented build failed rc={rc}")
                print(err[-800:])
                for row in missed:
                    counts["RUN-FAILED"] += 1
                    print(f"world {world} RUN-FAILED {row.label}")
                continue
            for i, row in enumerate(missed):
                if not (row.given.strip() or row.when.strip()):
                    counts["NO-PROGRAM"] += 1
                    print(f"world {world} NO-PROGRAM {row.label}")
                    continue
                program = program_for(_spec(row.given, row.when, row.observe, world), crate=crate)
                profraw = cov_root / f"row_{i:03d}"
                if profraw.exists():
                    shutil.rmtree(profraw)
                profraw.mkdir(parents=True)
                spec_env = dict(env)
                spec_env["LLVM_PROFILE_FILE"] = str(profraw / "%p-%m.profraw")
                with materialized_probe(scratch_root, program, f"replay_{world}_{i}") as target:
                    rc, err = _run(
                        ["cargo", "run", "--quiet", "--example", target],
                        scratch_root, spec_env, 600)
                if rc != 0 or not list(profraw.glob("*.profraw")):
                    counts["RUN-FAILED"] += 1
                    print(f"world {world} RUN-FAILED {row.label}")
                    print(err[-400:])
                    continue
                out_json = profraw / "coverage.json"
                rc, err = _run(
                    grcov_command(grcov, profraw, scratch_root, out_json,
                                  binary_path=world_target / "debug"),
                    scratch_root, spec_env, 120)
                if rc != 0 or not out_json.exists():
                    counts["RUN-FAILED"] += 1
                    print(f"world {world} RUN-FAILED {row.label} grcov rc={rc}")
                    continue
                try:
                    data = json.loads(out_json.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    counts["RUN-FAILED"] += 1
                    print(f"world {world} RUN-FAILED {row.label} {exc}")
                    continue
                hits = {rel: lines for rel, lines in coveralls_hit_lines(data).items()
                        if rel.startswith(SRC_PREFIX)}
                for rel in hits:
                    if rel not in texts:
                        try:
                            texts[rel] = (scratch_root / rel).read_text(encoding="utf-8")
                        except OSError:
                            continue
                sites = sites_for_hit_lines(hits, texts, recs)
                if row.label in sites:
                    counts["SAME"] += 1
                    print(f"world {world} SAME {row.label}")
                elif sites:
                    counts["JOIN"] += 1
                    shown = ", ".join(sorted(sites))
                    print(f"world {world} JOIN {row.label}")
                    print(f"  hit under: {shown}")
                else:
                    counts["NO-HIT"] += 1
                    print(f"world {world} NO-HIT {row.label}")
                    line = row.prompt_line()
                    if line:
                        print(f"  {line}")
        print(
            "summary "
            + " ".join(f"{k}={counts[k]}" for k in ("JOIN", "NO-HIT", "SAME", "RUN-FAILED", "NO-PROGRAM"))
            + f" probe_failed_not_run={failed_rows}")
        if counts["JOIN"]:
            print("JOIN: coverage named a site the routed label is not. "
                  "That is the only reading that would change a reach prompt.")
        else:
            print("asks unchanged")
        # reach_path is the writer the files were read from; naming it keeps
        # a missing world visible beside the glob.
        print("read " + ", ".join(reach_path(project, p.name[len("_reach_"):-len(".json")]).name
                                   for p in files))
    finally:
        if owned:
            shutil.rmtree(scratch_root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
