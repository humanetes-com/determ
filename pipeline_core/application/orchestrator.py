"""Orchestrator — the pipeline's composition/phase plumbing extracted from the
`main()` god-function. Grown strangler-fig: pure, engine-independent pieces
cross first (CLI parser), then the phase blocks behind explicit ports as their
dependency surface shrinks. `pipeline.py:main()` stays the entrypoint and calls
into here.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Protocol

from pipeline_core import phases
from pipeline_core.application.cluster_augment import AugmentContext
from pipeline_core.application.reconstruct.run_context import RunContext
from pipeline_core.application.tasks.task0d import phase_key as task0d_phase_key
from pipeline_core.application.tasks.task0d import run_task0d
from pipeline_core.domain.attribution import canon_site
from pipeline_core.domain.cfg import CfgSet, unreachable_under_every_cfg_set
from pipeline_core.domain.coverage import CoverageMeasurement
from pipeline_core.domain.end_verdict import EndVerdict
from pipeline_core.domain.enums import Backend
from pipeline_core.domain.fn_execution import (
    FnCoverageRecord,
    UnreachedFn,
    classify_stub_sites,
    site_label,
)
from pipeline_core.domain.generation import GenerationRequest
from pipeline_core.domain.measurement import code_identity
from pipeline_core.domain.models import BuildCluster, FnLocator, FnSite
from pipeline_core.domain.oracle_guard import OracleDrift
from pipeline_core.domain.pipeline_crash import PIPELINE_CODE_CRASH_EXIT, PipelineCodeCrash
from pipeline_core.domain.reference_floor import orphaned_stubs
from pipeline_core.domain.run_state import RunPlan
from pipeline_core.domain.stub_inventory import StubInventory
from pipeline_core.domain.workspace_layout import (
    BEHAVIOUR_TAG_PREFIX,
    PRE_AUGMENT_TAG_PREFIX,
    augmentation_has_started,
    plan_behaviour_tags,
    plan_pre_augment_tags,
)
from pipeline_core.phases import phase_scope, phase_skipped


@dataclass(frozen=True, slots=True)
class StubVerdict:
    """The five states a still-hollow fn can be in at completion time.

    Only `orphaned` is a defect. The other four exist because "every
    `unimplemented!()` is a hole we failed to fill" is false about real crates,
    and holding it cost a run its verdict after the whole cost was paid.

    `unreached_by_tests` is the one augmentation MEASURED: its targeted
    attempts wrote test blocks for the function that compiled and ran, and the
    coverage run after them still counted 0 executions. No test in the suite
    enters it, so no fill for it could be verified — the same standing as a
    body no cfg-set compiles. `unreached_evidence` carries the attempts per
    entry, in order. `executed_upstream` marks the orphaned sites the
    augmentation record says the original's suite DID execute: the loudest
    kind, because the reconstruction's green suite never reaches a body the
    original's suite entered.

    `dead_in_tree` is the one the COMPILER decided on the finished tree: in
    every baked world that compiles the stub, the crate and every test target
    still build with the stub's smallest removable item deleted, and the
    unmodified MIR names no call to it (`domain.stub_removal`). Nothing the
    crate or its oracle has can call it, so no test can enter it and no fill
    of it could be judged — a private body the reconstructed callers do not
    reach, trait-impl methods included. It is not counted;
    the target is the crate's tested behaviour, not the original's call graph.
    A site here that is also in `executed_upstream` is reported loudest: the
    original's suite entered a body this tree cannot reach, which is what a
    lost call path looks like.
    """

    orphaned: tuple[FnLocator, ...] = ()
    # The same stubs as `orphaned`, by site (container kept): what the end
    # gate's retro needs to look a body's public routes up and to tell two
    # same-named impl methods apart.
    orphaned_sites: tuple[FnSite, ...] = ()
    unreachable: tuple[FnLocator, ...] = ()
    upstream_hollow: tuple[FnLocator, ...] = ()
    unreached_by_tests: tuple[FnLocator, ...] = ()
    unreached_evidence: tuple[str, ...] = ()
    executed_upstream: frozenset[FnLocator] = frozenset()
    dead_in_tree: tuple[FnLocator, ...] = ()
    # Labels of the `dead_in_tree` sites the original's suite executed, and of
    # the rest: the two lines the end gate prints them on.
    dead_executed_upstream: tuple[str, ...] = ()
    dead_unexecuted: tuple[str, ...] = ()


def classify_remaining_stubs(
    stubs: Iterable[tuple[FnLocator | FnSite, tuple[str, ...] | None]],
    cfg_sets: Iterable[CfgSet],
    reference_hollow: Iterable[tuple[str, str]] = (),
    fn_records: Sequence[FnCoverageRecord] = (),
    unreached_proofs: Mapping[FnSite, str] | None = None,
) -> StubVerdict:
    """Split the fns still holding `unimplemented!()` into un-attempted,
    cfg-unreachable, hollow-upstream-too, unreached-by-tests, and dead in
    the finished tree.

    Two filters, and the ORDER matters. First reachability: a stub the union of
    the baked cfg-sets compiles out was never reachable by any cluster's Phase C
    and no fill could ever have been attempted. Then the reference: a body the
    ORIGINAL also leaves hollow is not a hole this pipeline dug.

    Live on `anyhow`, all three of `src/nightly.rs`'s stubs leave at the first
    filter — they sit under `#[cfg(anyhow_build_probe)]`, false in both baked
    clusters — so the second never fires there. It still earns its place, and
    the evidence is in the same file: upstream writes `unimplemented!()` itself
    in two of those three bodies (`Debug::fmt` and `Display::fmt` on the probe's
    `MyError`), so the reconstruction is byte-correct and a reachability-only
    gate would have been right by luck. A `todo!()` a crate ships, a cluster
    does compile, and no test exercises hits the second filter and nothing else.

    Both call sites use this. The single-module completion (the anyhow/hardcrate
    path, where these crates actually finish) previously ran a cfg-BLIND scan
    with no unreachable verdict at all, so a fully green suite still exited 1 on
    those three bodies while the multi-module FINAL — which those crates never
    reach — excused them correctly.

    The third filter reads augmentation's per-cluster function records
    (`fn_records`): a reachable, not-hollow-upstream stub every measured record
    speaking about it lists as UNREACHED leaves as `unreached_by_tests`. A stub
    a record lists as executed stays orphaned and is marked
    `executed_upstream`. With no record the gate is exactly the two-filter form
    — run `301146-2d8a5f06` (2026-09-17) exited 1 on six `provide`-path stubs
    with no record to read. Stubs may be named by site (container kept) or by
    locator; a locator reads the record's sites for it conservatively
    (`FnCoverageRecord.classify_stub`).

    The fourth filter reads the compiler's removal reading of THIS tree
    (`unreached_proofs`, site -> proof, from
    `unreached_stub_reading.read_unreached_stub_proofs`): a stub the earlier
    filters leave orphaned, which nothing can call in any world that compiles
    it, leaves as `dead_in_tree` and its line carries the proof. Run
    `292191-d60a5308` (2026-10-02) exited 1 on two private helpers the filled
    callers route around; run `3323407-984b07f2` (2026-11-08) exited 1 on a
    trait-impl method rustc's `dead_code` lint, the previous reader, never
    reports. Last in the order on purpose: a stub augmentation measured
    unreached keeps that better-evidenced bucket.
    """
    sets = list(cfg_sets)
    unreachable: list[FnLocator] = []
    reachable: list[FnSite] = []
    for key, preds in stubs:
        site = key if isinstance(key, FnSite) else FnSite(key[0], key[1], "")
        loc = FnLocator(site.file, site.fn_name)
        if preds and unreachable_under_every_cfg_set(preds, sets):
            unreachable.append(loc)
        else:
            reachable.append(site)
    hollow_upstream = frozenset(reference_hollow)
    orphaned_keys = orphaned_stubs(
        [(s.file, s.fn_name) for s in reachable], hollow_upstream)
    candidates = [s for s in reachable if (s.file, s.fn_name) in orphaned_keys]
    account = classify_stub_sites(candidates, fn_records)
    unreached: dict[FnSite, UnreachedFn] = dict(account.unreached)
    executed = frozenset(account.executed)
    proofs = {canon_site(k): v for k, v in (unreached_proofs or {}).items()}
    dead = [s for s in candidates
            if s not in unreached and canon_site(s) in proofs]
    return StubVerdict(
        orphaned=tuple(FnLocator(s.file, s.fn_name) for s in candidates
                       if s not in unreached and s not in dead),
        orphaned_sites=tuple(s for s in candidates
                             if s not in unreached and s not in dead),
        dead_in_tree=tuple(FnLocator(s.file, s.fn_name) for s in dead),
        dead_executed_upstream=tuple(
            f"{site_label(s)} — {proofs[canon_site(s)]}" for s in dead if s in executed),
        dead_unexecuted=tuple(
            f"{site_label(s)} — {proofs[canon_site(s)]}" for s in dead if s not in executed),
        unreachable=tuple(unreachable),
        upstream_hollow=tuple(FnLocator(s.file, s.fn_name) for s in reachable
                              if (s.file, s.fn_name) not in orphaned_keys),
        unreached_by_tests=tuple(FnLocator(s.file, s.fn_name) for s in candidates
                                 if s in unreached),
        unreached_evidence=tuple(
            f"{site_label(s)}"
            + (" (compiled only under its cluster's cfg)" if unreached[s].cluster_gated else "")
            + (f" — unreachable by construction: {unreached[s].proof}"
               if unreached[s].proof else
               " — targeted attempt(s) "
               + (", ".join(str(a) for a in unreached[s].attempts) or "none recorded"))
            for s in candidates if s in unreached),
        executed_upstream=frozenset(FnLocator(s.file, s.fn_name) for s in executed),
    )


def remaining_stub_verdict(
    project_dir: Path,
    *,
    scan_unimplemented_functions: Callable[..., StubInventory],
    load_baked_clusters: Callable[[Path], list[Any]],
    reference_hollow: Callable[[Path], frozenset[tuple[str, str]]],
    augmentation_fn_records: Callable[[Path], Sequence[FnCoverageRecord]],
    unreached_stub_proofs: Callable[[Path, Sequence[Any]], Mapping[FnSite, str]],
) -> tuple[StubVerdict, int]:
    """The end gate's verdict on the tree at `project_dir`, and how many baked
    cfg-sets it was judged under. BOTH completion paths call this — the
    single-module `lock_or_complete` and the multi-module FINAL — so the two
    cannot drift: they once ran different scans, and a fully green crate
    exited 1 on one path and 0 on the other.

    The removal reading costs a few `cargo` runs per cluster, so it is taken
    only when the cheaper filters leave an orphan it could move."""
    clusters = load_baked_clusters(project_dir) or []
    cfg_sets = [CfgSet.of(cfg_true=c.cfg_true, cfg_false=c.cfg_false)
                for c in clusters]
    # One pair PER SITE: at completion every hollow site is its own finding,
    # and the inventory names that choice.
    stubs = scan_unimplemented_functions(project_dir).site_preds()
    hollow = reference_hollow(project_dir)
    records = augmentation_fn_records(project_dir)
    verdict = classify_remaining_stubs(stubs, cfg_sets, hollow, records)
    if verdict.orphaned:
        proofs = unreached_stub_proofs(project_dir, clusters)
        if proofs:
            verdict = classify_remaining_stubs(
                stubs, cfg_sets, hollow, records, proofs)
    return verdict, len(cfg_sets)


def report_stub_verdict(verdict: StubVerdict, log: Callable[[str], None],
                        cfg_set_count: int) -> bool:
    """Log every bucket and return True when the run may claim success."""
    if verdict.unreachable:
        log(f"\nNOTE: {len(verdict.unreachable)} fn(s) remain unimplemented!() "
            f"but are UNREACHABLE under the union of all {cfg_set_count} baked "
            "cfg-set(s) — no cluster's build can compile them, so no fill could "
            "ever have been attempted. Not counted against this run:")
        for loc in verdict.unreachable:
            log(f"    {loc.file}::{loc.fn_name}")
    if verdict.upstream_hollow:
        log(f"\nNOTE: {len(verdict.upstream_hollow)} fn(s) remain "
            "unimplemented!() and the ORIGINAL crate leaves them hollow too — "
            "reproducing a stub is not failing to fill one. Not counted "
            "against this run:")
        for loc in verdict.upstream_hollow:
            log(f"    {loc.file}::{loc.fn_name}")
    if verdict.unreached_by_tests:
        log(f"\nNOTE: {len(verdict.unreached_by_tests)} fn(s) remain "
            "unimplemented!() and no test can execute them: augmentation either "
            "MEASURED them unreached (test blocks written for each compiled and "
            "ran, and the coverage run after them still counted 0 executions) or "
            "PROVED them unreachable by construction from the compiler's MIR and "
            "public index. No test in this suite enters them, so no fill for "
            "them could have been verified. Not counted against this run:")
        for line in verdict.unreached_evidence:
            log(f"    {line}")
    # Decided 2026-10-17: a crate-private body no rebuilt caller and no
    # oracle test reaches does not count against green, and one the
    # original's suite executed is said on its own named line, never folded
    # into the note. "Private" is the public index's: a fn an outside crate
    # can name or hold is never proved, and the removal build compiles the
    # in-crate test modules and every test target (`domain.stub_removal`),
    # so a public stub or a reached one stays in `orphaned` and fails the
    # run below.
    if verdict.dead_unexecuted:
        log(f"\nNOTE: {len(verdict.dead_unexecuted)} fn(s) remain "
            "unimplemented!() and NOTHING CAN CALL THEM in any baked cluster "
            "that compiles them: the crate and its tests build with them "
            "deleted, and the unmodified MIR names no call — so no fill for "
            "them could have been judged. Not counted against this run:")
        for label in verdict.dead_unexecuted:
            log(f"    {label}")
    if verdict.dead_executed_upstream:
        log(f"\nUNREACHED PRIVATE BODY, EXECUTED BY THE ORIGINAL: "
            f"{len(verdict.dead_executed_upstream)} fn(s) remain "
            "unimplemented!(); no rebuilt caller and no oracle test reaches "
            "them, and the original's suite executed them at augmentation. "
            "The call path to them was not reconstructed. Not counted "
            "against this run:")
        for label in verdict.dead_executed_upstream:
            log(f"    {label}")
    if verdict.orphaned:
        log(f"\nWARNING: all tests passed, but {len(verdict.orphaned)} fn(s) "
            "are STILL unimplemented!() — reachable under a baked cfg-set and "
            "implemented in the original:")
        for loc in verdict.orphaned:
            note = (" — the original's suite EXECUTED this body at augmentation; "
                    "nothing this run's suite runs reaches it in the reconstruction"
                    if loc in verdict.executed_upstream
                    else " — no augmentation record accounts for it")
            log(f"    {loc.file}::{loc.fn_name}{note}")
        # Not "never attempted": a body can be dispatched and deferred, or
        # asked for by the end gate's pinning pass, and still be here (run
        # `3991945-1b7ae3e9`, 2026-10-19: six pin attempts, then this line).
        log("         This is NOT a clean success — no test reaches the fns "
            "above in the reconstruction, so no body for them could be judged.")
        return False
    return True


def end_verdict(
    project_dir: Path,
    *,
    scan_unimplemented_functions: Callable[..., StubInventory],
    load_baked_clusters: Callable[[Path], list[Any]],
    reference_hollow: Callable[[Path], frozenset[tuple[str, str]]],
    augmentation_fn_records: Callable[[Path], Sequence[FnCoverageRecord]],
    unreached_stub_proofs: Callable[[Path, Sequence[Any]], Mapping[FnSite, str]],
    log: Callable[[str], None],
) -> EndVerdict:
    """The one end gate: classify the remaining stubs, report every bucket,
    and return what blocks success. Every completion path takes its decision
    from this value — the single-module completion returns it to the module
    loop (`ReconstructionStep`), which runs a retro and re-enters t2a; the
    multi-module FINAL, which has no loop around it, exits on it."""
    verdict, n_sets = remaining_stub_verdict(
        project_dir,
        scan_unimplemented_functions=scan_unimplemented_functions,
        load_baked_clusters=load_baked_clusters,
        reference_hollow=reference_hollow,
        augmentation_fn_records=augmentation_fn_records,
        unreached_stub_proofs=unreached_stub_proofs)
    report_stub_verdict(verdict, log, n_sets)
    return EndVerdict(
        blocking=verdict.orphaned_sites,
        executed_upstream=frozenset(
            s for s in verdict.orphaned_sites
            if FnLocator(s.file, s.fn_name) in verdict.executed_upstream))

# The --phase-*-only / --skeleton-backend standalone subcommands, in dispatch
# order. Each maps an `args` flag attribute to a handler keyed by the same name.
_PHASE_SUBCOMMAND_FLAGS = (
    "phase_a_only", "phase_b_only", "phase_c_only", "phase_d_only", "skeleton_backend",
)


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the pipeline CLI parser. Pure — no parsing, no I/O."""
    parser = argparse.ArgumentParser(description="Deterministic process pipeline.")
    parser.add_argument("--project", default="agave",
                        help="Project subfolder name (default: agave)")
    parser.add_argument("--clean", action="store_true",
                        help="Delete all generated state and start from scratch")
    parser.add_argument("--progress", action="store_true",
                        help="Compact terminal progress bar (\\r-rewriting) instead of one line per chunk")
    parser.add_argument("--nolog", action="store_true",
                        help="Print to stdout only — do not write to run.log")
    parser.add_argument("--augment-tests", action="store_true",
                        help=(
                            "Force TASK 0c even if .coverage_augmented already exists. "
                            "Normally TASK 0c runs automatically when the sentinel is absent."
                        ))
    parser.add_argument("--repo", default=None,
                        help="Git URL or local path — cloned/copied into tmp/, git-managed")
    parser.add_argument("--best-models-list", action="store_true",
                        help="Fetch and display top open-source coding models from HuggingFace")
    parser.add_argument("--vram-gb", type=float, default=32.0,
                        help="Total GPU VRAM in GB used to annotate model fit (default: 32)")
    parser.add_argument("--from-spec", action="store_true",
                        help=(
                            "Forward (spec-driven) mode. Skip task0a/0b/0c/strip/task1. "
                            "Requires functional_requirements.md at the project root. "
                            "API surface (if present at .original_structure/{name}.api.md) "
                            "is used by TASK 2. Goes directly to module iteration loop "
                            "(TASK 2 → TASK 3 → TASK 4)."
                        ))
    parser.add_argument("--restart-from-augmented", action="store_true",
                        help=(
                            "Restore the workspace to the task0c augmentation commit "
                            "(tag: task0c-augmented-{module}) and clear only iteration "
                            "state (strip sentinels, FR file, tests_output, manifest, "
                            "failure log). Keeps task0a/0b/0c outputs. Strip + task1+ "
                            "re-run from scratch with the augmented test suite. "
                            "Single-module workspaces only for now."
                        ))
    parser.add_argument("--restart-from-pre-augment", action="store_true",
                        help=(
                            "Restore the workspace to the commit BEFORE task0c "
                            "(tag: task0c-pre-{module}, written right before the cluster "
                            "augmentation) and forget everything task0c and later wrote: "
                            "generated tests, augmentation sentinels, strip/task1+ state. "
                            "Keeps task0a/0b outputs. task0c + strip + task1+ re-run — the "
                            "lever for a changed test generator or admission rule, which "
                            "otherwise needs --repo <URL> --clean. The reconstruction dirs "
                            "are archived beside the workspace first. When the workspace "
                            "has the behaviour restore point (tag: task0d-done-{module}, "
                            "written after task0d) the restore lands there and keeps the "
                            "task0d harvest; see --reharvest-behaviours."
                        ))
    parser.add_argument("--reharvest-behaviours", action="store_true",
                        help=(
                            "With --restart-from-pre-augment only: go back to "
                            "task0c-pre-{module} (before task0d), delete the "
                            "task0d-done-{module} tag and re-run the behaviour harvest "
                            "(task0d) as well as task0c. Without it a task0c restart keeps "
                            "the harvest — the lever for a changed task0d."
                        ))
    parser.add_argument("--audit", action="store_true",
                        help=(
                            "Self-audit: run the flow auditor (pipeline_core.audit — "
                            "doc-channel / floor / plateau / no-wipe / ratchet "
                            "invariants) at every iteration boundary and every "
                            "AUDIT_INTERVAL_SEC (default 900) in the background; "
                            "verdicts go to the log ([audit] lines) and to "
                            "events.jsonl (flow_audit.report) for the monitor."
                        ))
    parser.add_argument("--phase-a-only", action="store_true",
                        help=(
                            "Standalone bake: discover clusters from live source, "
                            "run the Rust skeleton extractor per cluster, save the "
                            "skeleton + leaf-first ordering under "
                            "tmp/{project}/.skeleton/{cluster}/. Refuses to run if "
                            "strip has already happened (no source to read)."
                        ))
    parser.add_argument("--phase-b-only", action="store_true",
                        help=(
                            "TASK 2 skeleton backend, Phase A + B: run Phase A then ask "
                            "the LLM to group the project's test files into cohesive units. "
                            "Writes the validated group manifest to "
                            "tmp/{project}/.skeleton/groups.json and reports group counts. "
                            "For testing the planner before wiring up Phase C/D."
                        ))
    parser.add_argument("--phase-c-only", action="store_true",
                        help=(
                            "TASK 2 skeleton backend, Phase A + B + C: run A and B, then "
                            "fill the skeleton group-by-group (leaf-first). Filled source "
                            "lands at tmp/{project}/.skeleton-filled/. Reports bytes filled "
                            "per group. No final compile/test run."
                        ))
    parser.add_argument("--phase-d-only", action="store_true",
                        help=(
                            "TASK 2 skeleton backend, Phase A + B + C + D: full skeleton "
                            "pipeline including the residual-cleanup pass. Reports stubs "
                            "filled vs left untouched."
                        ))
    parser.add_argument("--skeleton-backend", action="store_true",
                        help=(
                            "Invoke StaticSkeletonTask2Generator.generate() directly "
                            "(per-cluster flow). Reads functional_requirements.md "
                            "and genesis.md from the project, runs Phase A→D per cluster, "
                            "then merges into project_dir/src/. Standalone test path."
                        ))
    return parser


def run_phase_subcommand(
    plan: RunPlan,
    project_dir: Path,
    handlers: Mapping[str, Callable[[Path], int]],
) -> int | None:
    """If a `--phase-*-only` / `--skeleton-backend` flag is set, run its handler
    and return the process exit code; otherwise return None (normal pipeline).

    Dispatch order is fixed (`_PHASE_SUBCOMMAND_FLAGS`); each handler takes the
    project dir and returns an exit code — the routing the orchestrator owns,
    the work stays in the injected handlers.
    """
    for flag in _PHASE_SUBCOMMAND_FLAGS:
        if getattr(plan, flag):
            return handlers[flag](project_dir)
    return None


@dataclass(frozen=True)
class PhaseSubcommandDeps:
    """Collaborators the standalone `--phase-*-only` / `--skeleton-backend`
    handlers need. The composition root binds these to the resident pipeline
    shims; the handlers below own only the phase-chain orchestration."""
    log: Callable[[str], None]
    bake_skeleton_artifacts: Callable[[Path, str], dict[str, Any]]
    collect_test_files: Callable[[Path], Iterable[str]]
    group_tests: Callable[..., list[dict[str, Any]]]
    fill_groups: Callable[..., dict[str, Any]]
    cleanup_residuals: Callable[..., dict[str, Any]]
    new_skeleton_generator: Callable[[], Any]
    manifest_name: str


def _load_test_pairs(collect: Callable[[Path], Iterable[str]],
                     project_dir: Path) -> list[tuple[str, str]]:
    """Read each protected test file's (rel, content); skip unreadable ones."""
    pairs: list[tuple[str, str]] = []
    for rel in sorted(collect(project_dir)):
        p = project_dir / rel
        if p.exists():
            try:
                pairs.append((rel, p.read_text(encoding="utf-8")))
            except Exception:
                continue
    return pairs


def run_phase_a(deps: PhaseSubcommandDeps, project_dir: Path) -> int:
    """Standalone Phase A: run the pre-strip skeleton bake on live source.

    The bake MUST run while the original source is still on disk (before
    strip) — no original source is read after strip. Refuses to run if
    `src/` is missing or empty."""
    deps.log(f"\n=== Phase A (skeleton bake) for {project_dir.name} ===")
    src_dir = project_dir / "src"
    if not src_dir.exists() or not any(src_dir.rglob("*.rs")):
        deps.log(f"  [phase-a] no source at {src_dir} — strip already ran. "
                 "Cannot bake skeleton from absent source (no original source "
                 "is read after strip).")
        return 2
    res = deps.bake_skeleton_artifacts(project_dir, project_dir.name)
    if not res.get("ok"):
        deps.log(f"  [phase-a] bake FAILED: {res.get('reason', 'unknown')}")
        return 3
    deps.log(f"  [phase-a] baked {len(res['clusters'])} cluster(s): "
             f"{', '.join(res['clusters'])}")
    return 0


def run_phase_b(deps: PhaseSubcommandDeps, project_dir: Path) -> int:
    """Standalone Phase A + B: skeleton + LLM-driven test grouping."""
    rc = run_phase_a(deps, project_dir)
    if rc != 0:
        return rc
    deps.log(f"\n=== Phase B (test grouping) for {project_dir.name} ===")
    skeleton_dir = project_dir / ".skeleton" / "_standalone"
    pairs = _load_test_pairs(deps.collect_test_files, project_dir)
    if not pairs:
        deps.log("  [phase-b] no test files found.")
        return 6
    groups = deps.group_tests(skeleton_dir, pairs, cluster_name="default",
                              module_name=project_dir.name)
    deps.log(f"  [phase-b] manifest written to {skeleton_dir / 'groups.json'} "
             f"({len(groups)} groups, "
             f"{sum(len(g['test_files']) for g in groups)} test files covered).")
    for g in groups:
        deps.log(f"    - {g['name']}: {len(g['test_files'])} test(s); "
                 f"{len(g.get('target_symbols', []))} target symbol(s)")
    return 0


def run_phase_c(deps: PhaseSubcommandDeps, project_dir: Path) -> int:
    """Standalone Phase A + B + C: skeleton → grouping → per-group fill."""
    rc = run_phase_a(deps, project_dir)
    if rc != 0:
        return rc
    skeleton_dir = project_dir / ".skeleton" / "_standalone"
    pairs = _load_test_pairs(deps.collect_test_files, project_dir)
    if not pairs:
        deps.log("  [phase-c] no test files found.")
        return 6

    deps.log(f"\n=== Phase B (test grouping) for {project_dir.name} ===")
    groups = deps.group_tests(skeleton_dir, pairs, cluster_name="default",
                              module_name=project_dir.name)
    deps.log(f"  [phase-b] {len(groups)} groups produced.")

    deps.log(f"\n=== Phase C (per-group fill) for {project_dir.name} ===")
    target_src_dir = project_dir / ".skeleton-filled"
    if target_src_dir.exists():
        shutil.rmtree(target_src_dir)
    fr_path = project_dir / "functional_requirements.md"
    genesis_path = project_dir / "genesis.md"
    fr = fr_path.read_text(encoding="utf-8") if fr_path.exists() else ""
    genesis = genesis_path.read_text(encoding="utf-8") if genesis_path.exists() else ""

    result = deps.fill_groups(skeleton_dir, target_src_dir, groups, project_dir,
                              cluster_name="default", module_name=project_dir.name,
                              functional_req=fr, genesis=genesis)
    deps.log(f"  [phase-c] processed {result['groups_processed']} group(s), "
             f"filled {result['bytes_filled']:,} bytes; "
             f"{len(result['errors'])} error(s).")
    for e in result["errors"]:
        deps.log(f"    - {e}")
    # Always return 0 so downstream callers (Phase D, the backend class) can
    # perform residual cleanup regardless of per-group failures.
    return 0


def run_phase_d(deps: PhaseSubcommandDeps, project_dir: Path) -> int:
    """Standalone Phase A + B + C + D."""
    rc = run_phase_c(deps, project_dir)
    if rc != 0:
        return rc
    deps.log(f"\n=== Phase D (residual cleanup) for {project_dir.name} ===")
    target_src_dir = project_dir / ".skeleton-filled"
    pairs = _load_test_pairs(deps.collect_test_files, project_dir)
    fr_path = project_dir / "functional_requirements.md"
    fr = fr_path.read_text(encoding="utf-8") if fr_path.exists() else ""

    result = deps.cleanup_residuals(target_src_dir, project_dir, fr, pairs,
                                    module_name=project_dir.name)
    deps.log(f"  [phase-d] {result['stubs_total']} residual stubs, "
             f"{result['stubs_exercised']} reachable from tests; "
             f"{result['files_updated']} file(s) updated; "
             f"{result['stubs_remaining']} stub(s) still remaining.")
    return 0


def run_skeleton_backend(deps: PhaseSubcommandDeps, project_dir: Path) -> int:
    """Standalone harness invoking the static skeleton Task2 generator directly.
    Drives the full per-cluster flow without task0c/strip/task1."""
    deps.log(f"\n=== Skeleton backend (per-cluster) for {project_dir.name} ===")
    fr_path = project_dir / "functional_requirements.md"
    genesis_path = project_dir / "genesis.md"
    manifest_path = project_dir / deps.manifest_name
    if not fr_path.exists():
        # No FR yet — synthesize an empty one so the backend can still run.
        fr_path.write_text("# (no functional_requirements.md available)\n", encoding="utf-8")
    if not genesis_path.exists():
        genesis_path.write_text("# (no genesis.md available)\n", encoding="utf-8")
    backend = deps.new_skeleton_generator()
    ok = backend.generate(GenerationRequest(
        project_dir=project_dir,
        genesis_path=genesis_path,
        functional_req_path=fr_path,
        manifest_path=manifest_path,
        existing_test_files=deps.collect_test_files(project_dir),
        current_module=project_dir.name,
    ))
    return 0 if ok else 8


class DepGraphView(Protocol):
    """The slice of the dependency graph the per-module loop needs (structural —
    every `workspace_scan` graph backend satisfies it without inheritance)."""

    def crate_dir(self, module_name: str) -> Path: ...
    def crate_type(self, module_name: str) -> str: ...
    def workspace_deps(self, module_name: str) -> set[str]: ...
    def topological_order(self) -> list[str]: ...
    def module_test_command(self, test_command: str, module_name: str) -> str: ...
    def module_build_check_command(
        self, build_cmd: str | None, module_name: str) -> str | None: ...


@dataclass(frozen=True, slots=True)
class ModuleDescriptor:
    """Per-module loop-invariant facts derived once at the top of each iteration."""
    module_dir: Path
    crate_type: str
    workspace_deps: set[str]
    module_test_cmd: str
    module_manifest: Path
    module_dir_rel: str


def module_descriptor(
    module_name: str,
    *,
    dep_graph: DepGraphView,
    is_multi_module: bool,
    project_dir: Path,
    test_command: str,
) -> ModuleDescriptor:
    """Resolve a module's dir/type/deps/test-cmd/manifest/rel-path. Single-module
    workspaces short-circuit to the project root + `lib` defaults (no graph)."""
    module_dir = dep_graph.crate_dir(module_name) if is_multi_module else project_dir
    crate_type = dep_graph.crate_type(module_name) if is_multi_module else "lib"
    workspace_deps = dep_graph.workspace_deps(module_name) if is_multi_module else set()
    module_test_cmd = (dep_graph.module_test_command(test_command, module_name)
                       if is_multi_module else test_command)
    module_dir_rel = (
        str(module_dir.relative_to(project_dir)) + "/"
        if is_multi_module and module_dir != project_dir else ""
    )
    return ModuleDescriptor(
        module_dir=module_dir,
        crate_type=crate_type,
        workspace_deps=workspace_deps,
        module_test_cmd=module_test_cmd,
        module_manifest=project_dir / f".generated_files_{module_name}.txt",
        module_dir_rel=module_dir_rel,
    )


_EXAMPLE_USAGE_HEADER = (
    "// !!!! TASK 1 NOTE: This file is from the project's `examples/` directory.\n"
    "// !!!! It demonstrates idiomatic API usage. It is NOT a test and contains\n"
    "// !!!! no assertions to enforce. Use it to understand HOW the public API is\n"
    "// !!!! invoked, but do not derive requirements from it unless the same\n"
    "// !!!! behavior is also asserted by a test in the test suite.\n\n"
)


def with_example_usage_signal(
    t1_input: list[tuple[str, str]], examples: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Append `examples/*.rs` files to the TASK 1 input as usage signal — each
    prefixed with a header marking it non-assertional so the model doesn't derive
    requirements from example-only behaviour. Unchanged input when no examples."""
    if not examples:
        return t1_input
    return t1_input + [(rel, _EXAMPLE_USAGE_HEADER + content) for rel, content in examples]


def next_module(
    modules: list[str], locked_modules: set[str], module_coverage: Mapping[str, float],
) -> str | None:
    """The next unprocessed module to reconstruct: the unlocked one with the
    highest coverage (ties broken by `modules` order via a stable sort). No
    topological constraint — unprocessed modules keep real source on disk, so the
    crate compiles in any order. Returns None when all modules are locked."""
    candidates = [(module_coverage.get(m, 0.0), m)
                  for m in modules if m not in locked_modules]
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def measure_baseline_coverage(
    modules: list[str], dep_graph: DepGraphView, *,
    tarpaulin_available: bool,
    run_tarpaulin: Callable[[Path, str], tuple[Any, ...]],
    project_dir: Path,
    log: Callable[[str], None],
) -> dict[str, float]:
    """Measure each module's baseline coverage once to set processing order
    (descending coverage = richer spec first). proc-macro crates report 100%
    (tarpaulin can't instrument them). Returns {} when tarpaulin is unavailable
    or there is only one module."""
    result: dict[str, float] = {}
    if not tarpaulin_available or len(modules) <= 1:
        return result
    log("\nMeasuring baseline coverage to determine processing order...")
    for mod_name in modules:
        crate_type = dep_graph.crate_type(mod_name)
        cov = (100.0 if "proc-macro" in crate_type.lower()
               else run_tarpaulin(project_dir, mod_name)[0])
        log(f"  {mod_name}: {cov:.1f}%")
        result[mod_name] = cov
    return result


@dataclass(frozen=True)
class LockDeps:
    """Collaborators for the lock/complete transition (per-block injection)."""
    commit_and_mark: Callable[..., None]
    set_current: Callable[[Path, str], None]
    lock_module: Callable[[Path, str, Path, Path], object]  # returns int; ignored
    git_commit: Callable[[Path, str], bool]
    log: Callable[[str], None]
    sys_exit: Callable[[int], None]
    # The stub inventory (sites with their cfg predicates, and units) + the
    # baked cfg-sets + what the ORIGINAL leaves hollow. The last two are
    # defaulted so the many test fixtures that build a LockDeps keep
    # working; with no cfg-sets nothing is provably unreachable and with no
    # reference nothing is provably hollow-upstream, so the defaults degrade
    # to exactly the cfg-blind absolute this gate used to be.
    scan_unimplemented_functions: Callable[..., StubInventory]
    load_baked_clusters: Callable[[Path], list[Any]] = lambda _p: []
    reference_hollow: Callable[[Path], frozenset[tuple[str, str]]] = (
        lambda _p: frozenset())
    # Every cluster's function-execution record from augmentation; empty
    # degrades the gate to the reachability + reference form.
    augmentation_fn_records: Callable[[Path], Sequence[FnCoverageRecord]] = (
        lambda _p: ())
    # (project_dir, baked clusters) -> site -> proof for the stub sites
    # nothing can call in any cluster that compiles them
    # (`unreached_stub_reading`); empty takes no such reading.
    unreached_stub_proofs: Callable[[Path, Sequence[Any]], Mapping[FnSite, str]] = (
        lambda _p, _c: {})


def lock_or_complete(
    project_dir: Path,
    module_name: str,
    module_dir: Path,
    module_manifest: Path,
    *,
    is_multi_module: bool,
    locked_modules: set[str],
    src_hash: str | None,
    commit_message: str,
    detail: str,
    set_current_label: str,
    locked_commit_msg: str,
    success_log: str,
    tag: str | None = None,
    complete_commit_msg: str | None = None,
    deps: LockDeps,
) -> EndVerdict | None:
    """Commit+mark a passing module, then either LOCK it (multi-module: record
    in `locked_modules`, commit the lock, return None) or COMPLETE the run
    (single-module: the end gate first; clean, optionally commit the
    completion and exit 0). The three call sites (t3 pass, no_files
    build-pass, fix-loop reverified pass) differ only in the
    message/label/tag arguments; the skeleton is identical.

    A single-module end gate that is NOT clean returns its `EndVerdict`
    instead of exiting: the module is not marked done, the tree is
    committed as a checkpoint, and the caller treats the verdict as a failed
    iteration. Until 2026-10-19 this called `sys_exit(1)`, which put the one
    failure a green suite can still have outside the module loop: run
    `1680589-7a6824f8` ended with two reachable `provide` stubs and no retro,
    no re-entry, no second iteration."""
    with phase_scope("lock", module=module_name):
        if not is_multi_module:
            # The SAME verdict as the multi-module FINAL in `run_pipeline` —
            # single-module projects (the common case: anyhow, hardcrate,
            # most real crates) complete HERE, not there. It used to be the
            # same SCAN and not the same verdict: this path called the
            # cfg-blind `scan_unimplemented_functions` and had no notion of a
            # stub no baked cfg-set can compile, while the multi-module path
            # did — a fully green `anyhow` exited 1 on the three
            # `#[cfg(anyhow_build_probe)]` bodies in `src/nightly.rs`.
            verdict = end_verdict(
                project_dir,
                scan_unimplemented_functions=deps.scan_unimplemented_functions,
                load_baked_clusters=deps.load_baked_clusters,
                reference_hollow=deps.reference_hollow,
                augmentation_fn_records=deps.augmentation_fn_records,
                unreached_stub_proofs=deps.unreached_stub_proofs,
                log=deps.log)
            if not verdict.clean:
                deps.git_commit(
                    project_dir,
                    f"{commit_message} — end gate: {len(verdict.blocking)} "
                    "blocking body(ies)")
                deps.log(f"  [end-gate] {module_name}: not done — "
                         f"{len(verdict.blocking)} blocking body(ies); the "
                         "module loop takes this as a failed iteration.")
                return verdict
        deps.commit_and_mark(
            project_dir, f"task2-4/{module_name}", "done",
            commit_message=commit_message, tag=tag, detail=detail, source_hash=src_hash,
        )
        deps.set_current(project_dir, set_current_label)
        if is_multi_module:
            deps.lock_module(project_dir, module_name, module_dir, module_manifest)
            locked_modules.add(module_name)
            deps.git_commit(project_dir, locked_commit_msg)
            return None
        deps.log(success_log)
        if complete_commit_msg is not None:
            deps.git_commit(project_dir, complete_commit_msg)
        deps.sys_exit(0)
        return None


def filter_module_test_files(
    all_test_files: list[tuple[str, str]], module_dir_rel: str,
) -> list[tuple[str, str]]:
    """This module's `(rel_path, content)` test files: all of them filtered to the
    module's dir prefix, or all of them for a single-module/root workspace
    (``module_dir_rel == ""``)."""
    if not module_dir_rel:
        return all_test_files
    return [(rel, content) for rel, content in all_test_files
            if rel.startswith(module_dir_rel)]


@dataclass(frozen=True)
class RunDeps:
    """Every collaborator the top-level pipeline run (`run_pipeline`) drives.
    The composition root (pipeline.py:main) binds these to the resident shims +
    constants + run-scoped singletons; the orchestration sequence lives here."""
    # constants
    WORKSPACE_DIR: str
    MANIFEST_NAME: str
    MODEL: str
    COVERAGE_BACKEND: Any
    MODEL_CONTEXT_CHARS: int
    SINGLE_CALL_CHAR_LIMIT: int
    FILE_CHAR_LIMIT: int
    INTERMEDIATE_CHAR_LIMIT: int
    COVERAGE_AUGMENTATION_TARGET: float
    TARPAULIN_BIN: Path
    FAILURE_LOG_NAME: str
    PROJECT_PROFILE_NAME: str
    PROJECT_CONTEXT_NAME: str
    COVERAGE_STRATEGY_NAME: str
    SETTINGS: Any
    # run-scoped singletons / deps bundles
    run_state: Any
    scope: Any
    augment_store: Any
    cluster_augmentor: Any
    strip_bake_deps: Any
    task0d_deps: Any
    ws_scan_deps: Any
    phase_subcommand_deps: Any
    requirements_phase: Any
    module_reconstructor: Any
    # resident functions
    log: Callable[..., None]
    emit: Callable[..., None]
    setup_workspace: Callable[..., Path]
    is_git_repo: Callable[..., bool]
    init_log: Callable[..., None]
    init_events: Callable[..., None]
    start_audit_thread: Callable[..., None]
    quarantine_broken_gen_tests: Callable[..., int]
    seed_auto_operator_default: Callable[[], None]
    git_file_hash: Callable[..., str]
    ensure_vllm_ready: Callable[..., None]
    git_clean_reset: Callable[..., None]
    clean_state: Callable[..., None]
    state_path: Callable[..., Path]
    restart_from_augmented: Callable[..., None]
    restart_from_pre_augment: Callable[..., None]
    # Pre-augmentation restore point: `git_tag(project_dir, tag)` (a no-op off
    # git) + `list_git_tags(project_dir, glob)`, so the tag is never re-pointed.
    git_tag: Callable[..., None]
    list_git_tags: Callable[..., list[str]]
    # Behaviour restore point: commit the whole tree and say whether HEAD now
    # holds it, so `task0d-done-<module>` is never tagged over a partial commit.
    git_checkpoint: Callable[[Path, str], bool]
    source_hash: Callable[..., str]
    log_state_summary: Callable[..., None]
    read: Callable[..., str]
    task0_cfg_scan: Callable[..., list[BuildCluster]]
    load_baked_clusters: Callable[..., list[BuildCluster]]
    mark_phase: Callable[..., None]
    phase_valid: Callable[..., bool]
    read_state: Callable[..., dict[str, Any]]
    discover_test_command: Callable[..., str]
    parse_cmd: Callable[..., list[str]]
    collect_test_files: Callable[..., list[tuple[str, str]]]
    collect_protected_test_files: Callable[..., set[str]]
    task0_project_profile: Callable[..., str]
    task0_project_context: Callable[..., str]
    task0b_discovery: Callable[..., str]
    commit_and_mark: Callable[..., None]
    dependency_graph_factory: Callable[..., DepGraphView]
    load_locked_modules: Callable[..., set[str]]
    run_tarpaulin: Callable[..., CoverageMeasurement]
    strip_and_bake_module: Callable[..., None]
    task1_static_audit: Callable[..., list[str]]
    scan_workspace_structure: Callable[..., str]
    infer_build_check_command: Callable[..., str | None]
    wl_save_gen_tests_siblings: Callable[..., dict[str, list[tuple[int, str]]]]
    gen_tests_siblings: Callable[..., list[tuple[int, Path]]]
    is_gen_tests_file: Callable[..., bool]
    wl_wipe_module_sources: Callable[..., list[str]]
    write_workspace_stub: Callable[..., Path]
    collect_examples: Callable[..., list[tuple[str, str]]]
    task3: Callable[..., tuple[bool, str]]
    # The stub inventory: every site with the cfg predicates the extractor
    # attributes to it, so the final scan can tell "never attempted" from
    # "unreachable under every baked cfg-set". See the FINAL block.
    scan_unimplemented_functions: Callable[..., StubInventory]
    # `(file, fn_name)` the pristine tree leaves hollow. Empty when no reference
    # could be measured, which degrades this gate to the reachability-only form.
    reference_hollow: Callable[[Path], frozenset[tuple[str, str]]] = (
        lambda _p: frozenset())
    # Every cluster's function-execution record from augmentation (see
    # `classify_remaining_stubs`); empty degrades to the two-filter form.
    augmentation_fn_records: Callable[[Path], Sequence[FnCoverageRecord]] = (
        lambda _p: ())
    # Same contract as LockDeps.unreached_stub_proofs.
    unreached_stub_proofs: Callable[[Path, Sequence[Any]], Mapping[FnSite, str]] = (
        lambda _p, _c: {})
    # The oracle against its strip-time seal, `(project_dir, module) ->
    # OracleDrift | None` (None: unsealed). Defaults to "unguarded" so a
    # caller that wires none behaves as before the seal existed.
    oracle_drift: Callable[[Path, str], OracleDrift | None] = (
        lambda _p, _m: None)
    # The `current:` writer of pipeline_state.md, bound once per run into the
    # phase announcer (`pipeline_core.phases`) so a phase's entry and the state
    # file's "where the run stands" line are written by the same call.
    set_current: Callable[[Path, str], None] = lambda _p, _l: None
    # Start recording what each phase of this run costs, against the code,
    # configuration and starting workspace (`adapters.run_stats_recorder`):
    # `(root, project_dir, crate)`. A no-op unless the composition root binds it.
    start_run_stats: Callable[[Path, Path, str], None] = lambda _r, _p, _c: None
    # The ORIGINAL crate's construction facts and the strategy file they
    # render into (`adapters.strategy_facts.StrategyFacts`): `facts(project_dir,
    # clusters, may_measure=)`, `rendered(project_dir, text, worlds)`. None
    # keeps the strategy text exactly as the model wrote it.
    construction_facts: Any = None


def check_oracle_seal(deps: RunDeps, project_dir: Path,
                      modules: Sequence[str]) -> None:
    """Refuse to run on an oracle that is not the one strip sealed.

    Raises RuntimeError on drift, after logging `HEALTH ‼ ORACLE_DRIFT` with
    the paths and emitting `pipeline.oracle_drift`, so nothing downstream —
    task0c's commit, a fill, a ledger row — is produced against it. An
    unsealed stripped module is
    reported, not refused: workspaces stripped before the seal existed have
    no tag, and the next strip writes one."""
    for m in modules:
        if not deps.phase_valid(project_dir, f"strip/{m}"):
            continue
        drift = deps.oracle_drift(project_dir, m)
        if drift is None:
            deps.log(f"  [oracle] '{m}': no seal tag — the oracle is unguarded "
                     "until strip re-runs (redo.sh re-strips and seals).")
            continue
        if not drift.clean:
            deps.log(f"  [ABORT] HEALTH ‼ ORACLE_DRIFT '{m}': the oracle differs "
                     f"from its seal {drift.seal} — {drift.summary()}. Nothing "
                     "in the pipeline writes the oracle after strip. Restore "
                     f"the paths from the seal (git checkout {drift.seal} -- "
                     "<path>) or re-strip (redo.sh) — refusing to run.")
            deps.emit("pipeline.oracle_drift", data={
                "module": m, "seal": drift.seal,
                "changed": list(drift.changed), "added": list(drift.added),
                "removed": list(drift.removed)})
            raise RuntimeError(
                f"oracle drift for {m}: {', '.join(drift.paths())}")
        deps.log(f"  [oracle] '{m}': matches its seal {drift.seal}")


def run_pipeline(deps: RunDeps, plan: RunPlan, root: Path) -> None:
    """The deterministic per-module reconstruction run, extracted verbatim from
    pipeline.py:main. `main` now only parses args, resolves `root`, builds
    `RunDeps`, and calls here.

    A pipeline-code exception in a cluster's fill ends the run here, after
    task2a has scored every cluster: the `HEALTH ‼` line, each cluster's red
    list, the traceback, and a failed exit (`domain/pipeline_crash.py`)."""
    try:
        _run_pipeline(deps, plan, root)
    except PipelineCodeCrash as crash:
        for line in crash.report():
            deps.log(line)
        deps.emit("pipeline.code_crash", data={
            "cluster": crash.crash.cluster,
            "exception": crash.crash.exception,
            "traceback": crash.crash.traceback})
        sys.exit(PIPELINE_CODE_CRASH_EXIT)


def _run_pipeline(deps: RunDeps, plan: RunPlan, root: Path) -> None:
    workspace_dir = root / deps.WORKSPACE_DIR

    if plan.repo:
        # Full git-managed workspace: clone/copy into tmp/, checkout pipeline branch.
        project_dir = deps.setup_workspace(plan.repo, workspace_dir,
                                           force_fresh=plan.clean)
        deps.run_state.git_managed = True
    else:
        # --project: check tmp/ first, then root (backward compat).
        candidate = workspace_dir / plan.project
        project_dir = candidate if candidate.exists() else root / plan.project
        deps.run_state.git_managed = deps.is_git_repo(project_dir)

    genesis_path = project_dir / "genesis.md"
    functional_req_path = project_dir / "functional_requirements.md"
    manifest_path = project_dir / deps.MANIFEST_NAME  # legacy whole-workspace manifest

    if not project_dir.exists():
        print(f"ERROR: project directory '{project_dir}' not found.")
        sys.exit(1)

    deps.init_log(root, nolog=plan.nolog, progress_mode=plan.progress)
    deps.init_events(product=project_dir.name)

    # AUTO_DEFAULT=1 arms the auto-operator sentinel so stall-triggered
    # operator passes start live without the monitor's [a] toggle.
    deps.seed_auto_operator_default()

    # --audit: periodic + iteration-boundary flow self-audit (read-only).
    deps.run_state.audit_enabled = bool(plan.audit)
    deps.run_state.audit_project_dir = project_dir
    if deps.run_state.audit_enabled:
        deps.start_audit_thread()
        deps.log(f"  --audit: flow self-audit active (every iteration + every "
                 f"{os.getenv('AUDIT_INTERVAL_SEC', '900')}s).")

    # Scan gen_tests siblings inherited from prior runs; quarantine
    # any with unbalanced delimiters and strip their decls from the parent
    # source. Prevents a broken file (an LLM-corrupted task0c output) from
    # blocking every future cargo invocation. The systemic guard at write
    # time lives in _write_gen_tests_pass.
    try:
        deps.quarantine_broken_gen_tests(project_dir)
    except Exception as _e:
        deps.log(f"  [gen_tests] startup quarantine scan failed: {_e}")

    _sub_rc = run_phase_subcommand(plan, project_dir, {
        "phase_a_only": lambda pd: run_phase_a(deps.phase_subcommand_deps, pd),
        "phase_b_only": lambda pd: run_phase_b(deps.phase_subcommand_deps, pd),
        "phase_c_only": lambda pd: run_phase_c(deps.phase_subcommand_deps, pd),
        "phase_d_only": lambda pd: run_phase_d(deps.phase_subcommand_deps, pd),
        "skeleton_backend": lambda pd: run_skeleton_backend(deps.phase_subcommand_deps, pd),
    })
    if _sub_rc is not None:
        sys.exit(_sub_rc)

    # Forward (spec-driven) mode: skip reconstruction-phase tasks. The user
    # provides functional_requirements.md (+ optional API surface) and tests;
    # the pipeline goes straight to TASK 2 → TASK 3 → TASK 4.
    _from_spec = plan.from_spec
    if _from_spec:
        _fr_root = project_dir / "functional_requirements.md"
        if not _fr_root.exists():
            print(f"ERROR: --from-spec requires {_fr_root} (project-level FR file).")
            sys.exit(1)
        deps.log(f"FORWARD MODE: spec-driven generation. "
                 f"Skipping task0a/0b/0c/strip/task1. FR={_fr_root}")

    deps.emit("pipeline.start", data={
        "project_dir": str(project_dir),
        "clean": plan.clean,
        "augment_tests": plan.augment_tests,
        "from_spec": _from_spec,
        "model": deps.MODEL,
        "backend": deps.SETTINGS.backend,
        "coverage_backend": deps.COVERAGE_BACKEND,
        "context_chars": deps.MODEL_CONTEXT_CHARS,
    })

    _ph = deps.git_file_hash(root, "pipeline.py")
    _mh = deps.git_file_hash(root, "monitor.py")
    _params = []
    if plan.repo:
        _params.append(f"--repo {plan.repo}")
    if plan.project and not plan.repo:
        _params.append(f"--project {plan.project}")
    if plan.clean:
        _params.append("--clean")
    if plan.augment_tests:
        _params.append("--augment-tests")
    if plan.progress:
        _params.append("--progress")
    if plan.nolog:
        _params.append("--nolog")
    if plan.from_spec:
        _params.append("--from-spec")
    if plan.restart_from_augmented:
        _params.append("--restart-from-augmented")
    if plan.restart_from_pre_augment:
        _params.append("--restart-from-pre-augment")
    if plan.reharvest_behaviours:
        _params.append("--reharvest-behaviours")
    deps.log(f"  pipeline.py [{_ph}]  monitor.py [{_mh}]  params: {' '.join(_params) or '(none)'}")

    _backend = deps.SETTINGS.backend
    if _backend is Backend.VLLM:
        provider = f"vLLM ({os.getenv('VLLM_MODEL_NAME', deps.MODEL)})"
    elif _backend is Backend.SGLANG:
        provider = f"SGLang ({os.getenv('SGLANG_MODEL_NAME', deps.MODEL)})"
    elif _backend is Backend.HUGGINGFACE:
        provider = f"HuggingFace ({os.getenv('HF_MODEL', deps.MODEL)})"
    else:
        provider = f"Ollama ({deps.MODEL})"
    deps.log(f"Using model: {deps.MODEL}  |  Provider: {provider}")
    deps.log(f"Context budget: {deps.MODEL_CONTEXT_CHARS:,} chars  "
             f"(SINGLE_CALL={deps.SINGLE_CALL_CHAR_LIMIT:,}  FILE={deps.FILE_CHAR_LIMIT:,}  "
             f"INTERMEDIATE={deps.INTERMEDIATE_CHAR_LIMIT:,})")

    if _backend is Backend.VLLM:
        deps.ensure_vllm_ready(root, deps.log)

    if plan.clean and plan.restart_from_augmented:
        deps.log("\n-- ERROR: --clean and --restart-from-augmented are mutually exclusive --")
        sys.exit(2)
    if plan.restart_from_pre_augment and (plan.clean or plan.restart_from_augmented):
        deps.log("\n-- ERROR: --restart-from-pre-augment is mutually exclusive with "
                 "--clean and --restart-from-augmented --")
        sys.exit(2)
    if plan.reharvest_behaviours and not plan.restart_from_pre_augment:
        deps.log("\n-- ERROR: --reharvest-behaviours only applies with "
                 "--restart-from-pre-augment (restart_from.sh task0d) --")
        sys.exit(2)

    if plan.clean:
        deps.log("\n-- --clean requested: wiping all generated state --")
        if deps.run_state.git_managed:
            deps.git_clean_reset(project_dir)
        deps.clean_state(root, project_dir, manifest_path, genesis_path, functional_req_path)
        deps.state_path(project_dir).unlink(missing_ok=True)

    if plan.restart_from_augmented:
        deps.log("\n-- --restart-from-augmented: restoring workspace to task0c augmented state --")
        try:
            deps.restart_from_augmented(root, project_dir)
        except Exception as e:
            deps.log(f"  [ABORT] restart-from-augmented failed: {e}")
            sys.exit(2)

    if plan.restart_from_pre_augment:
        deps.log("\n-- --restart-from-pre-augment: restoring workspace to the commit before task0c --")
        try:
            deps.restart_from_pre_augment(root, project_dir,
                                          reharvest=plan.reharvest_behaviours)
        except Exception as e:
            deps.log(f"  [ABORT] restart-from-pre-augment failed: {e}")
            sys.exit(2)

    _src_hash = deps.source_hash(project_dir)
    deps.log(f"  Source hash: {_src_hash}")
    deps.log_state_summary(project_dir, _src_hash)
    phases.bind_current(lambda label: deps.set_current(project_dir, label))
    deps.start_run_stats(root, project_dir, project_dir.name)

    readme_path = project_dir / "README.md"
    if readme_path.exists():
        readme_text = deps.read(readme_path)
    else:
        deps.log("WARNING: README.md not found -- proceeding without it.")
        readme_text = ""

    # Cluster scan is pure static analysis (no LLM) — run first so all downstream
    # LLM tasks (discover_test_command, task0b) have cluster context available.
    # Cluster identity is FROZEN at bake time: once `.skeleton/_clusters.json`
    # exists the original source is gone (stripped), and a live re-scan walks
    # the RECONSTRUCTED tree instead — it minted phantom clusters from cfgs the
    # reconstruction itself exposes (build-probe/lint cfgs), whose task0c then
    # ran against half-broken generated code and stripped oracle gen_tests it
    # couldn't compile. Post-bake, the baked manifest is the only truth.
    _ph_setup = phase_scope("setup").open()
    _ph_setup.step("setup.cfg_scan")
    _clusters = deps.load_baked_clusters(project_dir)
    if _clusters:
        deps.log(f"\n  [cfg-scan] using {len(_clusters)} baked cluster(s) from "
                 ".skeleton/_clusters.json (post-bake: live re-scan would walk "
                 "reconstructed code, not original source).")
    else:
        _clusters = deps.task0_cfg_scan(project_dir)
    deps.scope.reset(_clusters)
    deps.log(f"\nCompiler clusters identified: {len(_clusters)}")
    for _c in _clusters:
        _env_desc = ", ".join(f"{k}+={v}" for k, v in _c.env_extra.items()) if _c.env_extra else "default build"
        deps.log(f"  cluster/{_c.name}  — {_env_desc}")
    deps.mark_phase(project_dir, "cfg_scan", "done",
                    detail=f"{len(_clusters)} clusters: {', '.join(c.name for c in _clusters)}",
                    source_hash=_src_hash)

    _ph_setup.step("setup.test_cmd")
    _test_cmd_cached = deps.phase_valid(project_dir, "test_cmd")
    if _test_cmd_cached:
        test_command = deps.read_state(project_dir)["phases"]["test_cmd"].get("detail", "")
        deps.log(f"Test command: {test_command} (from pipeline state)")
    else:
        test_command = deps.discover_test_command(readme_text, project_dir=project_dir)
        deps.mark_phase(project_dir, "test_cmd", "done", detail=test_command, source_hash=_src_hash)
    _ph_setup.close("skipped" if _test_cmd_cached else "ok")

    # Normalize: strip leading ./ from the executable so we use the PATH binary.
    parts_check = deps.parse_cmd(test_command)
    if parts_check and parts_check[0].startswith("./"):
        parts_check[0] = parts_check[0][2:]
        test_command = " ".join(parts_check)
        deps.log(f"  Normalized test command: {test_command}")

    deps.log("Collecting test files...")
    test_files = deps.collect_test_files(project_dir)
    existing_test_files = deps.collect_protected_test_files(project_dir)
    deps.log(f"  Found {len(test_files)} test file(s) ({len(existing_test_files)} write-protected).")

    # TASK 0a: identify project structure once; profile stored per-project (not pipeline root)
    # so running project B never loads project A's cached profile.
    # FORWARD MODE: task0a/0b/0c provide reconstruction-phase context (profile/
    # behavioral summary/coverage strategy). In spec-driven mode the user supplies
    # FR+API directly so this preprocessing is unnecessary — empty strings keep
    # downstream prompts well-formed without triggering a regeneration.
    if _from_spec:
        project_profile = ""
        project_context = ""
        coverage_strategy = ""
        deps.log("  [from-spec] Skipping task0a/0a-context/0b (FR+API are user-provided).")
    else:
        _t0a_cached = (deps.phase_valid(project_dir, "task0a")
                       and deps.phase_valid(project_dir, "task0a-context"))
        _ph_t0a = phase_scope("t0a").open()
        _ph_t0a.step("t0a.profile")
        profile_path = project_dir / deps.PROJECT_PROFILE_NAME
        project_profile = deps.task0_project_profile(profile_path, test_files, readme_text)

        # TASK 0a (context): synthesize behavioral project summary from test content.
        # This is the "big picture" document injected into every task — especially TASK 0c
        # which currently receives no project-level context. Works even without README.
        context_path = project_dir / deps.PROJECT_CONTEXT_NAME
        deps.emit("task.start", task="task0a")
        _ph_t0a.step("t0a.context")
        project_context = deps.task0_project_context(context_path, test_files, readme_text, project_profile)
        _ph_t0a.close("skipped" if _t0a_cached else "ok")

        # TASK 0b (discovery): produce coverage_strategy.md — import idioms, type construction
        # patterns, feature flags, test conventions. Injected into all TASK 0c, 1, and 2 prompts.
        # Skip if either pipeline_state marks task0b done, or the artifact already
        # exists from a prior run that predates state tracking (auto-backfill).
        strategy_path = project_dir / deps.COVERAGE_STRATEGY_NAME
        deps.emit("task.start", task="task0b")
        _ph_t0b = phase_scope("t0b").open()
        _ph_t0b.step("t0b.discovery")
        _t0b_cached = True
        if deps.phase_valid(project_dir, "task0b"):
            coverage_strategy = strategy_path.read_text().strip() if strategy_path.exists() else ""
            deps.log(f"  task0b already done — loading coverage_strategy.md ({len(coverage_strategy):,} chars).")
        elif strategy_path.exists() and strategy_path.read_text().strip():
            # Artifact predates pipeline_state.md tracking — backfill and skip.
            coverage_strategy = strategy_path.read_text().strip()
            deps.mark_phase(project_dir, "task0b", "done",
                            detail=f"{len(coverage_strategy)} chars (existing artifact)")
            deps.log(f"  task0b already done — loading coverage_strategy.md ({len(coverage_strategy):,} chars).")
        else:
            _t0b_cached = False
            # The tree is the original crate until t0b is done: measure the
            # construction facts first, so the discovery prompt is handed the
            # computed part as given instead of asked to restate it.
            if deps.construction_facts is not None:
                deps.construction_facts.facts(project_dir, _clusters, may_measure=True)
            coverage_strategy = deps.task0b_discovery(
                strategy_path, project_dir, test_files, readme_text, project_profile,
                clusters=_clusters,
            )
            deps.commit_and_mark(
                project_dir, "task0b", "done",
                commit_message="pipeline: task0b done",
                detail=f"{len(coverage_strategy)} chars",
            )
        _ph_t0b.close("skipped" if _t0b_cached else "ok")

    # TASK 0b: build dependency graph for bottom-up module integration.
    # Implementation selected by DEPENDENCY_GRAPH in .env (default: static).
    dep_graph = deps.dependency_graph_factory(project_dir, deps.ws_scan_deps)
    modules = dep_graph.topological_order()
    # The computed part of the strategy file, on EVERY path that bound the
    # text above (both skip branches included): what the t0c, task1 and fill
    # prompts receive is this variable, so a write to the file alone would
    # leave them on the model's text. Measured only while no module is
    # stripped; afterwards the stored facts of the original are what there is.
    if not _from_spec and deps.construction_facts is not None:
        _original = not any(deps.phase_valid(project_dir, f"strip/{m}") for m in modules)
        _worlds = deps.construction_facts.facts(project_dir, _clusters, may_measure=_original)
        coverage_strategy = deps.construction_facts.rendered(project_dir, coverage_strategy, _worlds)
    is_multi_module = len(modules) > 1

    if is_multi_module:
        deps.log(f"\nDependency graph: {len(modules)} module(s) in topological order:")
        for m in modules:
            module_deps = dep_graph.workspace_deps(m)
            dep_str = f"  deps: {', '.join(sorted(module_deps))}" if module_deps else "  leaf"
            deps.log(f"  [{dep_graph.crate_type(m):10s}]  {m}  ({dep_str})")
    else:
        deps.log("  Single module or no dependency graph detected — whole-workspace mode.")

    # -----------------------------------------------------------------------
    # Per-module pipeline.
    # For each module in coverage-descending order (topology respected):
    #   0c. Augment THIS module's tests until 100% coverage (real source present)
    #   Strip. Remove ONLY this module's source — all others remain on disk
    #   1.  Extract requirements from THIS module's tests
    #   2-4. Reconstruct loop until tests pass
    #   Lock. Only when passed > 0 and failed == 0
    # -----------------------------------------------------------------------
    if not modules:
        modules = [project_dir.name]

    # Pre-augmentation restore point (`task0c-pre-<module>`), consumed by
    # --restart-from-pre-augment. On a fresh git-managed run HEAD is the
    # `pipeline: task0b done` commit here — the last commit before task0c
    # writes a test — and this is the one place that knows it: the tag must be
    # written BEFORE the cluster augmentation (and before any phase inserted
    # ahead of it), never re-pointed on a resume (HEAD is then a post-t0c or
    # post-fill commit, and `git tag -f` would move the restore point there
    # silently), and never written into a workspace whose task0c already ran —
    # the decision refuses and its line names the by-hand command. `git_tag` is
    # a no-op off git; the tag listing is not, hence the guard.
    if not _from_spec and deps.run_state.git_managed:
        _tag_plan = plan_pre_augment_tags(
            modules, deps.list_git_tags(project_dir, f"{PRE_AUGMENT_TAG_PREFIX}*"),
            deps.read_state(project_dir)["phases"])
        for _tag in _tag_plan.to_write:
            deps.git_tag(project_dir, _tag)
        deps.log(f"  [task0c-pre] {_tag_plan.summary()}")

    locked_modules: set[str] = set()
    if is_multi_module:
        locked_modules = deps.load_locked_modules(project_dir)
        if locked_modules:
            deps.log(f"\nAlready locked: {', '.join(sorted(locked_modules))}")

    failure_log_path = project_dir / deps.FAILURE_LOG_NAME

    # Measure baseline coverage once to determine processing order.
    # Coverage-first: richest test spec → easiest reconstruction → best context for later modules.
    module_coverage = measure_baseline_coverage(
        modules, dep_graph, tarpaulin_available=deps.TARPAULIN_BIN.exists(),
        run_tarpaulin=deps.run_tarpaulin, project_dir=project_dir, log=deps.log)

    if is_multi_module and module_coverage:
        deps.log("\nProcessing order (coverage-descending, topology respected):")
        for m in modules:
            deps.log(f"  {m}: {module_coverage.get(m, 0.0):.1f}%")

    # -----------------------------------------------------------------------
    # TASK 0c — compiler cluster phase
    # _clusters and _current_cluster already set above (moved before task0b so
    # all LLM tasks receive cluster context). Loop below runs one pass per cluster.
    # -----------------------------------------------------------------------

    if _from_spec:
        deps.log("  [from-spec] Skipping task0c cluster loop (no test augmentation needed).")
        _clusters = []  # ensure the loop below is a no-op
    # TASK 0c cluster augmentation (application.cluster_augment.ClusterAugmentor).
    # Per-module crate dir/type resolved up front; _from_spec leaves _clusters empty so
    # the use-case is a no-op (it still clears the active cluster on exit).
    _module_dirs = {m: (dep_graph.crate_dir(m) if is_multi_module else project_dir)
                    for m in modules}
    _module_types = {m: (dep_graph.crate_type(m) if is_multi_module else "lib")
                     for m in modules}
    # TASK 0d — the behaviour specification, harvested from the ORIGINAL
    # before any generated test exists: test assertions, doc-tests and layout
    # pins, probes executed on the original with stdout recorded as the Then,
    # exercises by coverage. t0c admits generated tests by what they PIN in
    # this suite and t1.5 authors the layout against it; harvesting after
    # t0c would pull the generated tests' private-layout names into the spec
    # (24 % of the private surface was frozen that way on anyhow).
    # Refused once t0c has run for the module: the original bodies are gone
    # after strip (a doc-fence tier over the shell reads nothing, a probe
    # cannot compile) and the generated tests would enter the spec. Refused
    # too once ANY task0c entry exists, finished or not: the module sentinel
    # is written only when a module's augmentation completes, so a launch
    # resuming a half-augmented cluster has no sentinel but a tree holding
    # generated tests, and a book stage resumed there would read them. A
    # workspace restored to its pre-t0c tag has neither and harvests; one
    # restored to its behaviour restore point keeps the `task0d/*` entries,
    # and each cluster and the book stage skip as already done.
    if not _from_spec:
        _t0c_started = augmentation_has_started(deps.read_state(project_dir)["phases"])
        for _t0d_module in modules:
            if deps.augment_store.is_module_augmented(project_dir, _t0d_module):
                deps.log(f"  [task0d] '{_t0d_module}' already augmented — the "
                         "behaviour harvest runs only BEFORE task0c; skipping.")
                phase_skipped("t0d", "already augmented", module=_t0d_module)
                continue
            if _t0c_started:
                deps.log(f"  [task0d] '{_t0d_module}': task0c has started in this workspace — "
                         "the behaviour harvest and book run only BEFORE task0c; skipping.")
                phase_skipped("t0d", "task0c started", module=_t0d_module)
                continue
            run_task0d(deps.task0d_deps, project_dir, _t0d_module, list(_clusters),
                       _module_dirs[_t0d_module] / "src")
    # Behaviour restore point (`task0d-done-<module>`), consumed by
    # --restart-from-pre-augment: a task0c restart lands here and keeps the
    # harvest instead of paying task0d again. task0d commits each cluster's
    # harvest and the module's book stage, then marks each done AFTER its
    # commit, so the last mark is still uncommitted here: the tree is
    # committed whole first, and the tag is written only when HEAD holds all
    # of it. The restore rewrites pipeline_state.md from the tagged commit's
    # copy, so that last mark is what lets the restored run skip the book.
    if not _from_spec and deps.run_state.git_managed and _clusters:
        _behaviour_plan = plan_behaviour_tags(
            modules, deps.list_git_tags(project_dir, f"{BEHAVIOUR_TAG_PREFIX}*"),
            deps.read_state(project_dir)["phases"],
            [task0d_phase_key(c.name) for c in _clusters])
        if _behaviour_plan.to_write and not deps.git_checkpoint(
                project_dir, "pipeline: task0d done — behaviour restore point"):
            deps.log("  [task0d-done] NOT tagged: the working tree could not be committed "
                     "whole, so HEAD does not hold the harvest; a task0c restart re-harvests.")
        else:
            for _tag in _behaviour_plan.to_write:
                deps.git_tag(project_dir, _tag)
            deps.log(f"  [task0d-done] {_behaviour_plan.summary()}")
    # THE ORACLE SEAL, checked before task0c's per-launch pass: that pass ends
    # in a whole-workspace checkpoint commit, and on 2026-09-22 (run
    # `92187-f71f12bf`) it committed an edit made outside the pipeline into
    # `tests/test_context.rs` as `pipeline: task0c/stable`. After that commit
    # the edit is history and the fingerprint quietly names a new oracle.
    # Only a stripped module is sealed; one still before strip (a fresh run,
    # or a restore to the augmented tag) is authoring its oracle.
    if not _from_spec and deps.run_state.git_managed:
        check_oracle_seal(deps, project_dir, modules)
    deps.cluster_augmentor.run(AugmentContext(
        project_dir=project_dir, clusters=_clusters, modules=modules,
        module_coverage=module_coverage, module_dirs=_module_dirs,
        module_types=_module_types, src_hash=_src_hash,
        # `root`, not `Path.cwd()`: run_pipeline already receives the repo root
        # (`Path(__file__).parent` in pipeline.py) and code_identity returns the
        # empty-string digest e3b0c44298fc for a root holding neither pipeline.py
        # nor pipeline_core — indistinguishable from a real identity, and it would
        # bind every give-up verdict forever.
        code_id=code_identity(root),
        project_context=project_context, coverage_strategy=coverage_strategy,
        target_pct=deps.COVERAGE_AUGMENTATION_TARGET, tarpaulin_bin=deps.TARPAULIN_BIN,
        # Percent of the cluster's harvested behaviours the generated tests
        # must pin (BEHAVIOUR_COVERAGE_TARGET; 0 = off). Read from the typed
        # settings rather than a new RunDeps field: the value is a knob, not
        # a collaborator.
        behaviour_target_pct=float(
            getattr(deps.SETTINGS, "behaviour_coverage_target", 0.0) or 0.0),
        file_floor_pct=float(getattr(deps.SETTINGS, "coverage_file_floor", 0.0) or 0.0),
        pass_stall_limit=int(getattr(deps.SETTINGS, "task0c_pass_stall_limit", 10)),
    ))

    total_iters = 0
    # Plateaued (non-converged) modules never lock — without this exclusion the
    # loop re-picks them forever with a fresh IterationState (plateau counter
    # resets each re-entry → unbounded macro-loop; observed on anyhow run 30425).
    # Module -> why it stopped (`ModuleResult.stopped_because`).
    nonconverged_modules: dict[str, str] = {}
    while True:
        module_name = (next_module(modules, locked_modules | set(nonconverged_modules),
                                   module_coverage)
                       if is_multi_module else (modules[0] if modules else None))
        if module_name is None or module_name in nonconverged_modules:
            break

        _desc = module_descriptor(
            module_name, dep_graph=dep_graph, is_multi_module=is_multi_module,
            project_dir=project_dir, test_command=test_command)
        module_dir = _desc.module_dir
        crate_type = _desc.crate_type
        workspace_deps = _desc.workspace_deps
        module_test_cmd = _desc.module_test_cmd
        module_manifest = _desc.module_manifest
        module_dir_rel = _desc.module_dir_rel

        deps.log(f"\n{'=' * 60}")
        deps.log(f"MODULE: {module_name}  [{crate_type}]")
        ready_deps = workspace_deps & locked_modules
        if ready_deps:
            deps.log(f"  Locked deps  : {', '.join(sorted(ready_deps))}")
        deps.log(f"{'=' * 60}")

        # --- TASK 0c: all clusters complete — sentinel check only ---
        # All coverage augmentation (across all clusters) was done by the cluster
        # outer loop above. This block just verifies the sentinel and proceeds.
        # FORWARD MODE: skip — there is no augmentation phase in spec-driven mode.
        if not _from_spec:
            if not deps.augment_store.is_module_augmented(project_dir, module_name):
                deps.log(f"  WARNING: '{module_name}' has no augmentation sentinel — proceeding anyway.")
            deps.log(f"  TASK 0c: '{module_name}' all clusters done — proceeding to strip.")

        # Coverage check before strip — warn if below 100% but proceed anyway.
        # A hard stop here would abort the pipeline on coverage tool failures (bad sentinel,
        # grcov error, etc.) even when the test suite is intact.
        if crate_type != "proc-macro" and deps.TARPAULIN_BIN.exists():
            _final_cov = deps.augment_store.module_augmented_value(project_dir, module_name)
            if _final_cov < deps.COVERAGE_AUGMENTATION_TARGET:
                deps.log(
                    f"\n  WARNING: '{module_name}' coverage is {_final_cov:.1f}% — "
                    f"proceeding to strip anyway. Reconstruction quality may be lower."
                )
            # The aggregate hides where the oracle is thin: name every file
            # each cluster left below the per-file floor, with why.
            for _fl_cluster, _floor in deps.augment_store.module_file_floors(
                    project_dir, module_name).items():
                if _floor.below:
                    deps.log(f"  WARNING: '{module_name}' [{_fl_cluster}] {_floor.summary()}")

        # --- Collect test files BEFORE stripping ---
        # Must happen before strip so we can accurately identify which tests belong
        # to this module. Strip removes implementation source but test content in
        # lib.rs would also be gone if we checked after.
        test_files_pre_strip = deps.collect_test_files(project_dir)
        module_test_files = filter_module_test_files(
            test_files_pre_strip, module_dir_rel)
        module_has_tests = len(module_test_files) > 0
        deps.log(f"  Module test files: {len(module_test_files)} (before strip)")

        # Strip this module's source after baking Hard-Rule-11 artefacts
        # (extracted to application/strip_bake.py).
        deps.strip_and_bake_module(
            project_dir, module_name, module_dir, crate_type=crate_type,
            from_spec=_from_spec, src_hash=_src_hash, deps=deps.strip_bake_deps,
        )

        # Re-collect after strip — picks up any tests generated by TASK 0c.
        # test_files: full read set for TASK 1 (includes inline test blocks in src/)
        # existing_test_files: write-protection set for TASK 2 (pure test files only)
        test_files = deps.collect_test_files(project_dir)
        existing_test_files = deps.collect_protected_test_files(project_dir)
        deps.log(f"  Test files after strip: {len(test_files)} readable, "
                 f"{len(existing_test_files)} write-protected.")

        # --- TASK 1: extract requirements from this module's tests ---
        module_req_path = project_dir / f"functional_requirements_{module_name}.md"

        # Initialize iteration counters here so task1's task4 escalation path can use them.
        module_iter = 1
        prev_pass_count = 0
        prev_test_output = ""

        t1_input = module_test_files if module_has_tests else test_files
        if not module_has_tests:
            deps.log(f"  '{module_name}' has 0 tests — using full project tests for TASK 1.")

        # Append example files (examples/*.rs) as usage signal. They are NOT
        # assertions — the inline header makes that explicit so the LLM doesn't
        # derive requirements from example-only behavior.
        _t1_examples = deps.collect_examples(project_dir, module_dir=module_dir if is_multi_module else None)
        t1_input = with_example_usage_signal(t1_input, _t1_examples)
        if _t1_examples:
            deps.log(f"  Appended {len(_t1_examples)} example file(s) to TASK 1 input as usage signal.")

        # FORWARD MODE: copy the user-provided FR into the per-module slot so
        # TASK 2 picks it up via the existing module_req_path. Skip the full
        # TASK 1 inference loop — the user is the source of truth.
        if _from_spec:
            _fr_root = project_dir / "functional_requirements.md"
            if not module_req_path.exists():
                module_req_path.write_text(_fr_root.read_text(encoding="utf-8"),
                                           encoding="utf-8")
                deps.log(f"  [from-spec] Seeded {module_req_path.name} "
                         f"from {_fr_root.name} ({_fr_root.stat().st_size} chars).")
            else:
                deps.log(f"  [from-spec] {module_req_path.name} already present — leaving as-is.")

        # TASK 1: generate → validate → retry → escalate (RequirementsPhase use-case).
        t1_valid = deps.requirements_phase.run(
            project_dir, module_name=module_name, module_req_path=module_req_path,
            genesis_path=genesis_path, failure_log_path=failure_log_path,
            t1_input=t1_input, readme_text=readme_text, project_profile=project_profile,
            project_context=project_context, coverage_strategy=coverage_strategy,
            crate_type=crate_type, from_spec=_from_spec, src_hash=_src_hash,
            iteration=module_iter, prev_pass_count=prev_pass_count,
            prev_test_output=prev_test_output,
        )

        if not t1_valid:
            continue

        deps.task1_static_audit(module_req_path, module_test_files or test_files)

        # Workspace scan (done per-module since strip changes the file layout).
        workspace_structure = deps.scan_workspace_structure(project_dir)
        build_check_cmd = deps.infer_build_check_command(test_command, project_dir)
        module_build_check = (
            dep_graph.module_build_check_command(build_check_cmd, module_name)
            if is_multi_module else build_check_cmd
        )

        # Module-start lifecycle (save siblings → maybe-wipe → stub)
        # lives in application/workspace_lifecycle.py; the wipe is gated by
        # the codified wipe⟺rerun invariant (the t2c→t3 fall-through).
        saved_gen_blocks: dict[str, list[tuple[int, str]]] = \
            deps.wl_save_gen_tests_siblings(
                module_dir, project_dir, existing_test_files,
                gen_tests_siblings=deps.gen_tests_siblings,
                is_gen_tests_file=deps.is_gen_tests_file)
        deps.wl_wipe_module_sources(
            module_dir, project_dir, existing_test_files,
            regenerating_phase_done=deps.phase_valid(
                project_dir, f"task2b/{module_name}"),
            write_stub=partial(deps.write_workspace_stub, crate_type, module_dir))

        if not module_has_tests:
            deps.log(f"  '{module_name}' has 0 test files — will use whole-project context in TASK 2.")

        # --- TASK 2-4: reconstruction loop (ModuleReconstructor use-case). ---
        run_ctx = RunContext(
            project_dir=project_dir, module_name=module_name, module_dir=module_dir,
            crate_type=crate_type, module_manifest=module_manifest,
            module_dir_rel=module_dir_rel, module_test_cmd=module_test_cmd,
            module_build_check=module_build_check, module_has_tests=module_has_tests,
            module_req_path=module_req_path, genesis_path=genesis_path,
            failure_log_path=failure_log_path, is_multi_module=is_multi_module,
            src_hash=_src_hash, workspace_deps=workspace_deps, locked_modules=locked_modules,
            workspace_structure=workspace_structure, project_profile=project_profile,
            project_context=project_context, readme_text=readme_text,
            test_files=test_files, existing_test_files=existing_test_files,
            clusters=_clusters, saved_gen_blocks=saved_gen_blocks,
        )
        result = deps.module_reconstructor.run(
            run_ctx, total_iters=total_iters, coverage_strategy=coverage_strategy)
        total_iters, coverage_strategy = result.total_iters, result.coverage_strategy
        # A regenerated strategy (forced t0b after a genesis update) is model
        # text again: render the stored computed part back in. Never measured
        # here — the tree is a reconstruction by now.
        if deps.construction_facts is not None:
            coverage_strategy = deps.construction_facts.rendered(
                project_dir, coverage_strategy,
                deps.construction_facts.facts(project_dir, _clusters, may_measure=False))
        if not result.converged:
            nonconverged_modules[module_name] = (
                result.stopped_because or "the repair loop stopped")

    # -----------------------------------------------------------------------
    # Final: run full test suite to validate the integrated workspace.
    # Reached in multi-module mode (single-module exits on pass above) and by
    # any run with a plateaued module — that run reports FAIL and exits 1.
    # -----------------------------------------------------------------------
    for _name, _why in sorted(nonconverged_modules.items()):
        deps.log(f"\nNON-CONVERGED: {_name} — {_why}; best tree kept.")
    deps.log(f"\n{'=' * 60}")
    deps.log("FINAL: Running full test suite to validate integrated workspace")
    deps.log(f"{'=' * 60}")
    passed, _ = deps.task3(project_dir, test_command, "final", project_dir, genesis_path)
    if passed:
        # A full-suite pass only proves the ACTIVE cfg cluster's view compiles
        # and tests clean. Code gated behind a cfg predicate whose cluster got
        # dropped (too few distinguishing items to justify its own pass) is
        # invisible to every remaining cluster's build, so its stubs never
        # surface as a compile or test failure. This scan is CFG-BLIND — it
        # reads every .rs file's raw fn bodies, not just what the current
        # cluster compiles — and is the only thing that can still catch them
        # before claiming success.
        # ...but "cfg-blind" needs a THIRD verdict beside pass/fail. A stub the
        # UNION of every baked cfg-set compiles out was never reachable by any
        # cluster's Phase C and cannot be filled by anything — unreachable by
        # construction, not un-attempted. Live on `anyhow`: the three
        # `#[cfg(anyhow_build_probe)]` `MyError` impls in `nightly.rs` are
        # stubbed identically in BOTH cluster trees, so t2b's backfill has no
        # donor either, and exiting 1 on them converts a genuine success into a
        # failure at the last step, after the run's whole cost is paid.
        # ...and a THIRD verdict beside those two: a body the ORIGINAL leaves
        # hollow is not a hole this run failed to fill. `classify_remaining_stubs`
        # applies both filters in order and is shared with `lock_or_complete`,
        # which is where single-module crates actually finish.
        # No module loop surrounds this check (every module already locked),
        # so a blocking body here ends the run red. The single-module path
        # re-enters t2a on the same verdict instead (`lock_or_complete`).
        _end = end_verdict(
            project_dir,
            scan_unimplemented_functions=deps.scan_unimplemented_functions,
            load_baked_clusters=deps.load_baked_clusters,
            reference_hollow=deps.reference_hollow,
            augmentation_fn_records=deps.augmentation_fn_records,
            unreached_stub_proofs=deps.unreached_stub_proofs,
            log=deps.log)
        if not _end.clean:
            sys.exit(1)
        deps.log("\nSUCCESS: all tests passed.")
        sys.exit(0)
    elif nonconverged_modules:
        deps.log("\nFAIL: pipeline stopped — non-converged module(s); "
                 "final suite failing. Best tree kept for inspection.")
        sys.exit(1)
    else:
        deps.log("\nWARNING: all modules locked individually but full suite failed.")
        deps.log("         Integration issues between modules may require attention.")
        sys.exit(1)
