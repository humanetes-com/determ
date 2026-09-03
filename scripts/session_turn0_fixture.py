#!/usr/bin/env python3
"""Build the red-slice session's opening turn on a COPY of the live cluster
tree and report what it carries — the plan's fixture, computed.

WHY. The session route (application/reconstruct/slice_session.py) claims
its turn 0 carries what the stateless pair prompts of run `3003-93e9a9bf`
(2026-09-02) did not: a type rendering that names private fields as not
accessible from the writing file, the external std types' API, every
target test's panic line WITH the probe's observed values, and the whole
executed slice as the editable set. "Validated on the live cohort" is not
a validation; this script runs the real route on the real records with a
model stub that answers nothing, so the tree is never changed, and prints
the checks. Run it before a launch that is meant to read the session.

USAGE.
    python scripts/session_turn0_fixture.py \
        --project tmp/anyhow --cluster stable \
        --events <a run's events.jsonl slice> [--iter 1] \
        [--scratch /tmp/determ_turn0_fixture] [--keep]

`--events` must hold the run's `phase_c.coverage.owner_map` event (the
failing tests and their owner units) — `grep -F '"run_id": "<id>"'
logs/events.jsonl > <file>` is enough. The workspace is COPIED to
`--scratch` (cluster src, Cargo files, tests, the FR file); the copy gets
its own retro store and its own probe stage, so nothing under `--project`
is read-modified. Needs cargo (one build of the copied crate, ~1 min cold)
and NO GPU: the model stub records the transcript and replies with nothing.
Never point --scratch at the workspace.

HOW TO INTERPRET THE OUTPUT.
  slice            — the session the planner forms from the owner map: its
                     tests, unit count, files. Compare with the entry's
                     prediction (owners ∪ attributed pair, one session).
  prompt_chars     — turn 0 size; the recorded pair prompt was 87,995.
  editable_units   — every slice unit named in a FILE header (must equal
                     the slice size; a miss means a record did not resolve).
  private_fields   — the count of "NOT accessible from" headers: >0 means
                     the visibility-aware rendering reached the prompt.
  backtrace_api    — whether `BacktraceStatus` / `status(` reached the
                     prompt (the external-type records); 0/0 means rust-src
                     is missing or the records are off.
  observed_lines   — the probe's `observed at line` count; 0 means the
                     probe could not build or the tests were not found.
  transcript_turns — how many messages the stub saw (system + user = 2).
The decision rule: launch only when editable_units == slice size,
private_fields > 0, observed_lines > 0. backtrace_api is the third
contract and should be > 0 on this crate; a 0 there names the
external-type path as the thing to fix first.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _copy_workspace(project: Path, cluster: str, scratch: Path) -> Path:
    if scratch.exists():
        shutil.rmtree(scratch)
    dst = scratch / project.name
    (dst / ".skeleton-filled" / cluster).mkdir(parents=True)
    src_root = project / ".skeleton-filled" / cluster
    shutil.copytree(src_root / "src", dst / ".skeleton-filled" / cluster / "src")
    for name in ("Cargo.toml", "Cargo.lock"):
        if (src_root / name).exists():
            shutil.copy2(src_root / name, dst / ".skeleton-filled" / cluster / name)
    shutil.copytree(project / "tests", dst / "tests", symlinks=False)
    (dst / ".skeleton-filled" / cluster / "tests").symlink_to(dst / "tests")
    (dst / ".skeleton").mkdir()
    shutil.copy2(project / ".skeleton" / "_clusters.json", dst / ".skeleton" / "_clusters.json")
    # The workspace root's manifest and build script decide the cluster's
    # cfg-set (`feature = "std"` and friends); without them the cfg filter
    # cannot drop a crate's no-std placeholder type and it shadows the std
    # record of the same name.
    for name in ("rust-toolchain.toml", "rust-toolchain", "Cargo.toml",
                 "Cargo.lock", "build.rs"):
        if (project / name).exists():
            shutil.copy2(project / name, dst / name)
    for fr in project.glob("functional_requirements_*.md"):
        shutil.copy2(fr, dst / fr.name)
    (dst / ".phase_c_logs").mkdir()
    return dst


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--cluster", default="stable")
    ap.add_argument("--events", required=True)
    ap.add_argument("--iter", type=int, default=1)
    ap.add_argument("--scratch", default="/tmp/determ_turn0_fixture")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    project = Path(args.project).resolve()
    scratch = Path(args.scratch).resolve()
    if scratch == project or project in scratch.parents:
        print("refusing: --scratch must not be inside the workspace")
        return 2
    owners = None
    with open(args.events, encoding="utf-8") as fh:
        for line in fh:
            e = json.loads(line)
            if (e.get("event") == "phase_c.coverage.owner_map"
                    and e["data"].get("iter") == args.iter):
                owners = e["data"]["owners"]
                break
    if not owners:
        print("no phase_c.coverage.owner_map event for that iter in --events")
        return 2
    os.environ.setdefault("ASSERT_PROBE_STAGE_ROOT", str(scratch / "probe_root"))
    os.environ["SLICE_SESSION_MAX_TURNS"] = "1"
    dst = _copy_workspace(project, args.cluster, scratch)
    import model_interact
    import pipeline
    from pipeline_core.adapters.structure import load_baked_clusters
    from pipeline_core.domain.attribution import FailureFrontier
    from pipeline_core.domain.attribution import locator_of, site_from_payload
    from pipeline_core.domain.test_report import failure_evidence_by_test

    transcripts: list[list[dict]] = []

    def _ask_turns(messages, **_k):  # noqa: ANN001, ANN202
        transcripts.append([dict(m) for m in messages])
        return ""

    model_interact.ask_turns = _ask_turns  # type: ignore[assignment]
    pipeline._ask_raw = lambda *a, **k: ""  # type: ignore[assignment]
    clusters = {c.name: c for c in load_baked_clusters(dst)}
    cluster = clusters.get(args.cluster)
    root = dst / ".skeleton-filled" / args.cluster
    env = pipeline._cargo_env()
    rc, out = pipeline._run_cargo("cargo test --offline", root, env, 600)
    evidence = failure_evidence_by_test(out)
    tests = sorted(owners)
    # Owner-map events carry structured sites (`{"file","fn","container"}`);
    # a legacy `file::fn` string reads as a locator-only site, which the
    # session expands over the tree's containers exactly as the loop does.
    units = {t: frozenset(site_from_payload(u) for u in us) for t, us in owners.items()}
    dispatch = {locator_of(u): {t for t, us in units.items() if u in us}
                for us in units.values() for u in us}
    sources = {}
    for t in tests:
        for p in (dst / "tests").glob("*.rs"):
            txt = p.read_text(encoding="utf-8", errors="replace")
            if f"fn {t}(" in txt:
                sources[t] = txt
                break
    frontier = FailureFrontier.build(dispatch, dispatch, evidence=evidence, sources=sources)
    test_files = sorted((dst / "tests").glob("*.rs"))
    pipeline._phase_c_fill_impl_blocks(
        project_dir=dst, target_src_dir=root, tests_dir_files=test_files,
        module_name=project.name, cluster_name=args.cluster, cluster=cluster,
        check_cmd="cargo check --offline", cluster_env=env,
        fr_path=next(dst.glob("functional_requirements_*.md"), None),
        tdd_frontier_keys=set(dispatch), tdd_frontier_tests=frontier,
        tdd_test_cmd="cargo test --offline",
        session_slices={t: units[t] for t in tests})
    if not transcripts:
        print("no session ran (transcript empty) — see the log above")
        return 1
    turn0 = next(m["content"] for m in transcripts[0] if m["role"] == "user")
    headers = [ln for ln in turn0.splitlines() if ln.startswith("<FILE ")]
    editable = sum(h.count(" (") for h in headers)
    print(f"slice            : tests={tests} units={len(set().union(*units.values()))} files={len(headers)}")
    print(f"prompt_chars     : {len(turn0):,}")
    print(f"editable_units   : {editable}")
    print(f"private_fields   : {turn0.count('NOT accessible from')}")
    print(f"backtrace_api    : BacktraceStatus={turn0.count('BacktraceStatus')} status(={turn0.count('status(')}")
    print(f"observed_lines   : {turn0.count('observed at line')}")
    print(f"transcript_turns : {len(transcripts[0])}")
    (scratch / "turn0.txt").write_text(turn0, encoding="utf-8")
    print(f"turn 0 saved to {scratch / 'turn0.txt'}")
    if not args.keep:
        shutil.rmtree(dst, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
