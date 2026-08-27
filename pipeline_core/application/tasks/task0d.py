"""TASK 0d — behaviour harvest, per cluster, BEFORE t0c (F10).

The one phase that reads the original crate for what it PROMISES rather than
what it contains: test assertions, doc-tests and layout pins are lifted as
behaviours (`extract_behaviours.harvest_behaviour_suite`); public fns no
harvested spec names get a model-authored PROBE, run against the original
with its stdout recorded as the Then (never a guessed literal); and each
runnable spec's coverage fills `exercises`, the unit set a mismatch escalates
through. The suite is what t0c's augmentation and t1.5's layout authoring
read, and what the bake later merges into rather than overwriting.

Why it runs before t0c: t0c adds generated tests that name the private
layout; a harvest that sees them would pin the reconstruction to the
original's internals. Reading the tree before augmentation keeps every
behaviour black-box by construction.

Every stage after the harvest is ADDITIVE: an exception in the probes or the
exercises is logged and the phase still completes with the harvested suite.
The oracle's absence must never break a run; its presence is measured by the
ONE reading line this phase logs per cluster (`[task0d] cluster …`), whose
three defect-observable flags use the same patterns the census script does.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from pipeline_core.domain.behaviour import BehaviourSpec, BehaviourSuite, merge_suites
from pipeline_core.domain.behaviour_census import (
    defect_observable_flags,
    klass_histogram,
    render_counts,
    runtime_then_counts,
)
from pipeline_core.domain.behaviour_coverage import is_test_assertion_spec
from pipeline_core.domain.behaviour_probe import (
    PROBE_BATCH_SIZE,
    ProbeResult,
    batches,
    is_probe_spec,
    normalize_stdout,
    parse_behaviour_programs,
    probe_spec,
    probe_targets,
)
from pipeline_core.domain.cfg import CfgSet
from pipeline_core.domain.models import BuildCluster, FnRecord, ModRecord, TypeRecord
from pipeline_core.domain.module_reachability import externally_reachable_files
from pipeline_core.prompts.behaviour_synthesis import build_behaviour_synthesis_prompt

PHASE_PREFIX = "task0d"


def phase_key(cluster_name: str) -> str:
    return f"{PHASE_PREFIX}/{cluster_name}"


@dataclass(frozen=True)
class Task0dDeps:
    """Collaborators, all injected (composition binds the resident adapters)."""

    log: Callable[[str], None]
    emit: Callable[..., None]
    mark_phase: Callable[..., None]
    phase_valid: Callable[[Path, str], bool]
    commit_and_mark: Callable[..., None]
    # (src_dir) → (rel, text) pairs of the crate's own sources; (project_dir)
    # → the same for tests/. Both skip `*.gen_tests_*.rs` by contract.
    read_sources: Callable[[Path], list[tuple[str, str]]]
    read_test_sources: Callable[[Path], list[tuple[str, str]]]
    harvest: Callable[..., BehaviourSuite]          # harvest_behaviour_suite
    save_suite: Callable[[Path, BehaviourSuite], Path]
    load_suite: Callable[[Path, str], BehaviourSuite]
    ask: Callable[[str], str]                       # prompt → reply (the model)
    public_surface: Callable[[Path, str], str]      # (project_dir, cluster) → text
    symbol_inventory: Callable[[Path, str], str]
    enumerate_fns: Callable[[Path], list[FnRecord] | None]   # over src_dir
    # `mod` declarations + type declarations over src_dir — the probe
    # selector's reachability authority (a pub method on a pub struct behind
    # a private `mod ptr;` is not callable; `impl Error` in the private
    # `mod error;` is, because `Error` is declared at the root).
    enumerate_mods: Callable[[Path], list[ModRecord] | None]
    enumerate_types: Callable[[Path], list[TypeRecord] | None]
    run_probe: Callable[[Path, BuildCluster | None, str, str], ProbeResult]
    exercises_by_coverage: Callable[..., object]
    PROBE_MAX_PER_CLUSTER: int = 12
    EXERCISES_ENABLED: bool = True


def _style_example(specs: tuple[BehaviourSpec, ...]) -> str:
    """One harvested test item, for the prompt's EXAMPLE_USAGES style block."""
    for s in specs:
        if is_test_assertion_spec(s) and s.given.strip():
            return s.given
    return ""


def _run_probes(
    deps: Task0dDeps, project_dir: Path, module_name: str, cluster: BuildCluster,
    suite: BehaviourSuite, records: list[FnRecord], mods: list[ModRecord],
    types: list[TypeRecord], default_cluster: str,
) -> tuple[list[BehaviourSpec], int, int, int]:
    """(admitted probe specs, admitted count, proposed count, cargo runs)."""
    files = frozenset(r.file for r in records) | frozenset(t.file for t in types)
    reachable = externally_reachable_files(mods, files)
    type_files = {t.name: t.file for t in types if t.is_pub and t.is_exactly_pub}
    targets = probe_targets(
        records, suite.specs, max_targets=deps.PROBE_MAX_PER_CLUSTER,
        reachable_files=reachable, type_files=type_files,
        cfg_set=CfgSet.of(set(cluster.cfg_true), set(cluster.cfg_false)))
    if not targets:
        return [], 0, 0, 0
    surface = deps.public_surface(project_dir, cluster.name)
    inventory = deps.symbol_inventory(project_dir, cluster.name)
    example = _style_example(suite.specs)
    admitted: list[BehaviourSpec] = []
    proposed = 0
    per_unit: dict[str, int] = {}
    for batch in batches(targets, PROBE_BATCH_SIZE):
        prompt = build_behaviour_synthesis_prompt(
            module_name=module_name, cfg_cluster=cluster.name,
            target_units=batch, public_surface=surface,
            symbol_inventory=inventory, example_usages=example)
        reply = deps.ask(prompt)
        for unit, program in parse_behaviour_programs(reply):
            proposed += 1
            unit = unit or batch[0]
            n = per_unit.get(unit, 0)
            per_unit[unit] = n + 1
            result = deps.run_probe(
                project_dir, cluster, program, f"{cluster.name}_{proposed}")
            stdout = normalize_stdout(result.stdout)
            if result.ok and stdout:
                admitted.append(probe_spec(
                    cfg_cluster=cluster.name, unit=unit, n=n, program=program,
                    stdout=stdout, default_cluster=default_cluster))
                continue
            why = ("no stdout" if result.ok else
                   f"failed: {result.stderr[-300:].strip() or 'no stderr'}")
            deps.log(f"  [task0d] cluster {cluster.name}: probe for {unit} "
                     f"dropped ({why}).")
    return admitted, len(admitted), proposed, proposed


def _fill_exercises(
    deps: Task0dDeps, project_dir: Path, cluster: BuildCluster,
    specs: tuple[BehaviourSpec, ...], records: list[FnRecord],
) -> tuple[tuple[BehaviourSpec, ...], int, int]:
    """Specs with `exercises` filled from coverage; (specs, filled count,
    cargo invocations). A collaborator that still returns a bare dict
    (the pre-count signature) is treated as zero invocations."""
    runnable = [s for s in specs if is_test_assertion_spec(s) or is_probe_spec(s)]
    if not runnable:
        return specs, 0, 0
    raw = deps.exercises_by_coverage(project_dir, cluster, runnable, records)
    cargo_n = 0
    by_id: dict[str, frozenset[str]]
    if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[0], dict):
        by_id, cargo_n = raw[0], int(raw[1])
    else:
        by_id = raw if isinstance(raw, dict) else {}
    if not by_id:
        return specs, 0, cargo_n
    out = tuple(replace(s, exercises=by_id[s.behaviour_id])
                if s.behaviour_id in by_id else s for s in specs)
    return out, sum(1 for s in out if s.exercises), cargo_n


def _reading_line(cluster: str, suite: BehaviourSuite, filled: int,
                  admitted: int, proposed: int, *,
                  elapsed_s: float, cargo_invocations: int,
                  ) -> tuple[str, dict[str, object]]:
    n = len(suite.specs)
    runtime = runtime_then_counts(suite.specs)
    flags = defect_observable_flags(suite.specs)
    flag_text = ", ".join(f"{k} {'yes' if v else 'no'}" for k, v in flags.items())
    line = (f"  [task0d] cluster {cluster}: {n} behaviours — runtime then: "
            f"{render_counts(runtime)}; exercises filled: {filled}/{n}; "
            f"probes admitted {admitted} / proposed {proposed}; "
            f"elapsed {elapsed_s:.0f}s; {cargo_invocations} cargo invocation(s); "
            f"defect observables: {flag_text}")
    payload: dict[str, object] = {
        "cluster": cluster, "behaviours": n,
        "runtime_then": dict(runtime), "exercises_filled": filled,
        "probes_admitted": admitted, "probes_proposed": proposed,
        "elapsed_s": round(elapsed_s, 3),
        "cargo_invocations": cargo_invocations,
        "defect_observables": flags,
        "klass": dict(klass_histogram(suite.specs)),
    }
    return line, payload


def run_task0d(
    deps: Task0dDeps,
    project_dir: Path,
    module_name: str,
    clusters: list[BuildCluster],
    src_dir: Path,
) -> None:
    """Harvest → probes → exercises → save, per cluster; skips a cluster whose
    phase is already marked done. The harvested part is committed even when
    a later stage raises."""
    default_cluster = next((c.name for c in clusters if c.is_default),
                           clusters[0].name if clusters else "default")
    for cluster in clusters:
        key = phase_key(cluster.name)
        if deps.phase_valid(project_dir, key):
            deps.log(f"  [task0d] cluster {cluster.name}: already harvested — skipping.")
            continue
        t0 = time.monotonic()
        sources = deps.read_sources(src_dir)
        test_sources = deps.read_test_sources(project_dir)
        try:
            harvested = deps.harvest(
                sources, cfg_cluster=cluster.name, test_sources=test_sources,
                default_cluster=default_cluster)
        except Exception as e:
            deps.log(f"  [task0d] cluster {cluster.name}: harvest FAILED: {e} — "
                     "phase left open.")
            deps.mark_phase(project_dir, key, "failed", detail=str(e)[:200])
            continue
        suite = merge_suites(deps.load_suite(project_dir, cluster.name), harvested)
        records: list[FnRecord] = []
        mods: list[ModRecord] = []
        types: list[TypeRecord] = []
        try:
            records = list(deps.enumerate_fns(src_dir) or [])
            mods = list(deps.enumerate_mods(src_dir) or [])
            types = list(deps.enumerate_types(src_dir) or [])
        except Exception as e:
            deps.log(f"  [task0d] cluster {cluster.name}: fn enumeration failed "
                     f"(probes and exercises skipped): {e}")
        admitted = proposed = 0
        cargo_n = 0
        try:
            probes, admitted, proposed, probe_cargo = _run_probes(
                deps, project_dir, module_name, cluster, suite, records, mods,
                types, default_cluster)
            cargo_n += probe_cargo
            if probes:
                suite = merge_suites(suite, BehaviourSuite(
                    cfg_cluster=cluster.name, specs=tuple(probes)))
        except Exception as e:
            deps.log(f"  [task0d] cluster {cluster.name}: probes failed "
                     f"(non-fatal, harvested suite kept): {e}")
        filled = 0
        if deps.EXERCISES_ENABLED:
            try:
                specs, filled, ex_cargo = _fill_exercises(
                    deps, project_dir, cluster, suite.specs, records)
                cargo_n += ex_cargo
                suite = BehaviourSuite(cfg_cluster=cluster.name, specs=specs)
            except Exception as e:
                deps.log(f"  [task0d] cluster {cluster.name}: exercises failed "
                         f"(non-fatal, harvested suite kept): {e}")
        deps.save_suite(project_dir, suite)
        line, payload = _reading_line(
            cluster.name, suite, filled, admitted, proposed,
            elapsed_s=time.monotonic() - t0, cargo_invocations=cargo_n)
        deps.log(line)
        deps.emit("task0d.harvest", **payload)
        deps.commit_and_mark(
            project_dir, key, "done",
            commit_message=f"pipeline: {key} — {len(suite.specs)} behaviour(s)",
            detail=(f"{len(suite.specs)} behaviours, runtime then "
                    f"{sum(runtime_then_counts(suite.specs).values())}, "
                    f"exercises {filled}, probes {admitted}/{proposed}"))
