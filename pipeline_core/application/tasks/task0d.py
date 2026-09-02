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
from collections.abc import Callable, Mapping
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
    captured_spec,
    class_prompts,
    class_targets,
    exact_format_ask_rejects,
    is_probe_spec,
    parse_behaviour_programs,
    parse_behaviour_specs,
    probe_coverage_class,
    probe_targets,
    program_for,
    render_probe_program,
    value_bearing_reason,
)
from pipeline_core.domain.behaviour_select import behaviour_class_name
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
    harvest: Callable[..., BehaviourSuite]  # harvest_behaviour_suite
    save_suite: Callable[[Path, BehaviourSuite], Path]
    load_suite: Callable[[Path, str], BehaviourSuite]
    ask: Callable[[str], str]  # prompt → reply (the model)
    public_surface: Callable[[Path, str], str]  # (project_dir, cluster) → text
    symbol_inventory: Callable[[Path, str], str]
    enumerate_fns: Callable[[Path], list[FnRecord] | None]  # over src_dir
    # `mod` declarations + type declarations over src_dir — the probe
    # selector's reachability authority (a pub method on a pub struct behind
    # a private `mod ptr;` is not callable; `impl Error` in the private
    # `mod error;` is, because `Error` is declared at the root).
    enumerate_mods: Callable[[Path], list[ModRecord] | None]
    enumerate_types: Callable[[Path], list[TypeRecord] | None]
    run_probe: Callable[..., ProbeResult]
    exercises_by_coverage: Callable[..., object]
    # The uncapturable record. A probe that fails to compile, fails to run,
    # times out, prints nothing, is refused as not value-bearing, or does not
    # reproduce used to leave a single log line and nothing else — the source
    # file is unlinked in a `finally`, so the set of things we know we cannot
    # observe was invisible. These are NOT admitted as specs: a `then=None`
    # row is classified OTHER, ranked last and unpinnable, which is the same
    # silent-hole class under another name.
    record_misses: Callable[[Path, str, list[dict[str, str]]], object] = lambda _p, _c, _m: None
    # Writes the replayable captures as `tests/behaviour_<cluster>_gen_1.rs`,
    # after compiling and running them against the original.
    emit_tests: Callable[..., tuple[object, tuple[str, ...], list[dict[str, str]], tuple[BehaviourSpec, ...]]] = (
        lambda *_a, **_k: (None, (), [], ())
    )
    # Compiles and runs ONE emitted test binary under this cluster's env plus
    # any extra: (project_dir, cluster, stem, extra_env) -> (ok, output). The
    # emitted file is part of the reconstruction oracle, so it is held to the
    # same rule every other pipeline-authored test is — it must have been seen
    # to pass against the original before it can fail against a fill.
    verify_tests: Callable[..., tuple[bool, str]] = lambda *_a, **_k: (True, "")
    # The environment flip that exposes an env-dependent observable, e.g.
    # `RUST_BACKTRACE`. Run-twice cannot see this axis: the value is stable at
    # any fixed environment and still not a property of the crate.
    perturb_env: Callable[[BuildCluster], dict[str, str]] = lambda _c: {}
    # `#[cfg(...)]` predicate gating a cluster's emitted tests. `tests/` is
    # shared across every cluster's filled tree by a symlink, so an ungated
    # file is ACTIVE in clusters it was never captured under.
    cluster_cfg_attr: Callable[[BuildCluster], str] = lambda _c: ""
    PROBE_MAX_PER_CLUSTER: int = 12
    # Blocks honoured per reply. The target cap bounds how many units are
    # ASKED about; without this a single reply could spend an unbounded number
    # of cargo runs.
    PROBE_BLOCKS_PER_REPLY: int = 8
    # Behaviour classes requested per cluster, scarcest first.
    PROBE_CLASSES_PER_CLUSTER: int = 4
    EXERCISES_ENABLED: bool = True
    # Pin env-moving public Debug via two-world captures (BEHAVIOUR_PIN_ENV_DEBUG).
    # Off restores withdraw-the-mover, harvest-class ranking, and Display-or-Debug asks.
    PIN_ENV_DEBUG: bool = True


def _style_example(specs: tuple[BehaviourSpec, ...]) -> str:
    """One harvested test item, for the prompt's EXAMPLE_USAGES style block."""
    for s in specs:
        if is_test_assertion_spec(s) and s.given.strip():
            return s.given
    return ""


def _run_probes(
    deps: Task0dDeps,
    project_dir: Path,
    module_name: str,
    cluster: BuildCluster,
    suite: BehaviourSuite,
    records: list[FnRecord],
    mods: list[ModRecord],
    types: list[TypeRecord],
    default_cluster: str,
) -> tuple[list[BehaviourSpec], int, int, int, list[dict[str, str]]]:
    """(admitted probe specs, admitted count, proposed count, cargo runs, misses)."""
    misses: list[dict[str, str]] = []
    if deps.PROBE_MAX_PER_CLUSTER <= 0:
        return [], 0, 0, 0, misses
    files = frozenset(r.file for r in records) | frozenset(t.file for t in types)
    reachable = externally_reachable_files(mods, files)
    type_files = {t.name: t.file for t in types if t.is_pub and t.is_exactly_pub}
    targets = probe_targets(
        records,
        suite.specs,
        max_targets=deps.PROBE_MAX_PER_CLUSTER,
        reachable_files=reachable,
        type_files=type_files,
        cfg_set=CfgSet.of(set(cluster.cfg_true), set(cluster.cfg_false)),
    )
    # Ask by CLASS, not by unit — and ask even when there is no leftover unit.
    # Behaviours are transversal ("an error built from a string downcasts to
    # `&str`" spans the constructor, the vtable, `object_downcast` and
    # `Error::downcast`), so a unit-shaped ask cannot reach one. Gating the
    # whole phase on `probe_targets` being non-empty left the unit axis as the
    # ON-SWITCH of the mechanism built to leave it: a crate whose harvest
    # already names every public fn — harvest SUCCEEDING — would never ask by
    # class at all. `PROBE_MAX_PER_CLUSTER` stays the kill switch, checked
    # above, so the documented `0` still turns probes off entirely.
    class_of = probe_coverage_class if deps.PIN_ENV_DEBUG else behaviour_class_name
    class_asks = class_targets(
        suite.specs,
        class_of,
        max_targets=deps.PROBE_CLASSES_PER_CLUSTER,
        prompts=class_prompts(pin_env_debug=deps.PIN_ENV_DEBUG),
    )
    if not targets and not class_asks:
        return [], 0, 0, 0, misses
    surface = deps.public_surface(project_dir, cluster.name)
    inventory = deps.symbol_inventory(project_dir, cluster.name)
    example = _style_example(suite.specs)
    # One prompt carrying every class ask, with the leftover unit names as the
    # callable inventory beside it — NOT the same class asks repeated once per
    # unit batch, which is what `for batch in batches(targets)` did: with a cap
    # of 12 and a batch of 6 that was two identical class requests and twice
    # the GPU for one cohort. Without class asks the old per-unit batching is
    # still what there is to do.
    rounds: list[tuple[tuple[tuple[str, str], ...], tuple[str, ...]]] = (
        [(class_asks, tuple(targets))] if class_asks else [((), tuple(b)) for b in batches(targets, PROBE_BATCH_SIZE)]
    )
    admitted: list[BehaviourSpec] = []
    proposed = 0
    cargo_runs = 0
    per_unit: dict[str, int] = {}
    for asks, batch in rounds:
        prompt = build_behaviour_synthesis_prompt(
            module_name=module_name,
            cfg_cluster=cluster.name,
            target_units=batch,
            public_surface=surface,
            symbol_inventory=inventory,
            example_usages=example,
            target_classes=asks,
        )
        reply = deps.ask(prompt)
        # Bound the WORK, not just the targets. `PROBE_MAX_PER_CLUSTER` caps how
        # many units are asked about; the number of blocks a reply may contain
        # was unbounded, and every block costs its own `cargo run` at up to the
        # probe timeout. A reply that emits fifty blocks used to buy fifty
        # compile-and-run cycles against a cap of twelve.
        all_parsed = parse_behaviour_specs(reply)
        parsed = all_parsed[: deps.PROBE_BLOCKS_PER_REPLY]
        if len(all_parsed) > len(parsed):
            over = len(all_parsed) - len(parsed)
            misses.append(
                {
                    "unit": batch[0] if batch else "reply",
                    "reason": f"over the blocks-per-reply cap: {over} block(s) "
                    f"past {deps.PROBE_BLOCKS_PER_REPLY} not run",
                }
            )
            deps.log(
                f"  [task0d] cluster {cluster.name}: {over} behaviour block(s) past the per-reply cap were not run."
            )
        if not all_parsed:
            # A reply that parses to nothing used to leave no log, no miss and
            # no event — `proposed` simply stayed 0. That is the answer-dropped
            # class: the model may well have answered. Name which way it failed,
            # because a legacy-shaped reply says the prompt did not land while
            # an unparseable one says the reply did not.
            legacy = parse_behaviour_programs(reply)
            why = (
                "reply arrived in the legacy free-form shape (no <WHEN>)"
                if legacy
                else "reply carried no parseable <BEHAVIOUR> block"
            )
            misses.append({"unit": batch[0] if batch else "reply", "reason": why})
            deps.log(f"  [task0d] cluster {cluster.name}: {why}.")
            continue
        asked = {c for c, _ in asks}
        for probe in parsed:
            proposed += 1
            unit = probe.name or (batch[0] if batch else "behaviour")
            n = per_unit.get(unit, 0)
            per_unit[unit] = n + 1
            # A `when` that compares observes `true`/`false` and throws away the
            # value it was supposed to specify. Rejected BEFORE the cargo run:
            # the run would succeed and store a Then carrying no information.
            if (bad := value_bearing_reason(probe.when)) is not None:
                misses.append({"unit": unit, "reason": f"not value-bearing: {bad}", "when": probe.when})
                deps.log(f"  [task0d] cluster {cluster.name}: behaviour for {unit} refused ({bad}).")
                continue
            if (
                deps.PIN_ENV_DEBUG
                and "EXACT_FORMAT" in asked
                and (fmt_why := exact_format_ask_rejects(probe))
            ):
                misses.append({"unit": unit, "reason": fmt_why, "when": probe.when})
                deps.log(f"  [task0d] cluster {cluster.name}: behaviour for {unit} refused ({fmt_why}).")
                continue
            program = render_probe_program(probe.given, probe.when, probe.observe, crate=module_name)
            result = deps.run_probe(project_dir, cluster, program, f"{cluster.name}_{proposed}")
            cargo_runs += 1
            if not (result.ok and result.stdout.strip()):
                why = "no stdout" if result.ok else f"failed: {result.stderr[-300:].strip() or 'no stderr'}"
                misses.append({"unit": unit, "reason": why, "when": probe.when, "program": program})
                deps.log(f"  [task0d] cluster {cluster.name}: probe for {unit} dropped ({why}).")
                continue
            # Run TWICE and require the same output. One run cannot tell a
            # deterministic observable from a lucky one, and a captured Then
            # that does not reproduce becomes a flaky test in the oracle for
            # the life of the project. Only for a probe that produced something
            # to compare: re-running one that failed to COMPILE pays the most
            # expensive cargo call there is for an answer already known.
            confirm = deps.run_probe(project_dir, cluster, program, f"{cluster.name}_{proposed}_r")
            cargo_runs += 1
            if confirm.stdout != result.stdout:
                misses.append({"unit": unit, "reason": "not reproducible", "when": probe.when, "program": program})
                deps.log(
                    f"  [task0d] cluster {cluster.name}: behaviour for "
                    f"{unit} dropped (output differed between two runs)."
                )
                continue
            admitted.append(
                captured_spec(
                    cfg_cluster=cluster.name, probe=probe, n=n, stdout=result.stdout, default_cluster=default_cluster
                )
            )
    return admitted, len(admitted), proposed, cargo_runs, misses


def _fill_exercises(
    deps: Task0dDeps,
    project_dir: Path,
    cluster: BuildCluster,
    specs: tuple[BehaviourSpec, ...],
    records: list[FnRecord],
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
    out = tuple(replace(s, exercises=by_id[s.behaviour_id]) if s.behaviour_id in by_id else s for s in specs)
    return out, sum(1 for s in out if s.exercises), cargo_n


def _reading_line(
    cluster: str,
    suite: BehaviourSuite,
    filled: int,
    admitted: int,
    proposed: int,
    *,
    elapsed_s: float,
    cargo_invocations: int,
) -> tuple[str, dict[str, object]]:
    n = len(suite.specs)
    runtime = runtime_then_counts(suite.specs)
    flags = defect_observable_flags(suite.specs)
    flag_text = ", ".join(f"{k} {'yes' if v else 'no'}" for k, v in flags.items())
    line = (
        f"  [task0d] cluster {cluster}: {n} behaviours — runtime then: "
        f"{render_counts(runtime)}; exercises filled: {filled}/{n}; "
        f"probes admitted {admitted} / proposed {proposed}; "
        f"elapsed {elapsed_s:.0f}s; {cargo_invocations} cargo invocation(s); "
        f"defect observables: {flag_text}"
    )
    payload: dict[str, object] = {
        "cluster": cluster,
        "behaviours": n,
        "runtime_then": dict(runtime),
        "exercises_filled": filled,
        "probes_admitted": admitted,
        "probes_proposed": proposed,
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
    default_cluster = next((c.name for c in clusters if c.is_default), clusters[0].name if clusters else "default")
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
                sources, cfg_cluster=cluster.name, test_sources=test_sources, default_cluster=default_cluster
            )
        except Exception as e:
            deps.log(f"  [task0d] cluster {cluster.name}: harvest FAILED: {e} — phase left open.")
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
            deps.log(f"  [task0d] cluster {cluster.name}: fn enumeration failed (probes and exercises skipped): {e}")
        admitted = proposed = 0
        cargo_n = 0
        misses: list[dict[str, str]] = []
        admitted_ids: set[str] = set()
        try:
            probes, admitted, proposed, probe_cargo, misses = _run_probes(
                deps, project_dir, module_name, cluster, suite, records, mods, types, default_cluster
            )
            cargo_n += probe_cargo
            admitted_ids = {p.behaviour_id for p in probes}
            if probes:
                suite = merge_suites(suite, BehaviourSuite(cfg_cluster=cluster.name, specs=tuple(probes)))
        except Exception as e:
            deps.log(f"  [task0d] cluster {cluster.name}: probes failed (non-fatal, harvested suite kept): {e}")
        filled = 0
        if deps.EXERCISES_ENABLED:
            try:
                specs, filled, ex_cargo = _fill_exercises(deps, project_dir, cluster, suite.specs, records)
                cargo_n += ex_cargo
                suite = BehaviourSuite(cfg_cluster=cluster.name, specs=specs)
            except Exception as e:
                deps.log(f"  [task0d] cluster {cluster.name}: exercises failed (non-fatal, harvested suite kept): {e}")
        # Captures become REAL tests here, or they stay notes about the crate.
        # Emitted from the observed value, never re-asked of the model, and
        # deliberately NOT put through t0c's admission contract: that contract
        # exists to catch a guessed literal, and this value was printed by the
        # crate itself. It IS compiled and run against the original first —
        # emission without execution puts an entry no fill can ever satisfy
        # into the oracle, which is the class t0c's runtime gate exists for.
        # A capture that fails that gate is withdrawn from the SUITE too: a
        # value that cannot be replayed is not a specification, and leaving it
        # listed would hand t1.5 a MUST no tree can meet.
        try:

            def _recapture(spec: BehaviourSpec, extra: Mapping[str, str], _cl: BuildCluster = cluster) -> str | None:
                program = program_for(spec, crate=module_name)
                result = deps.run_probe(project_dir, _cl, program, f"{_cl.name}_env", extra_env=dict(extra))
                if result.ok and result.stdout.strip():
                    return result.stdout
                return None

            _dest, withdrawn, emit_misses, emitted = deps.emit_tests(
                project_dir,
                cluster.name,
                suite.specs,
                crate=module_name,
                verify=lambda stem, extra, _cl=cluster: deps.verify_tests(project_dir, _cl, module_name, stem, extra),
                perturb_env=deps.perturb_env(cluster),
                cfg_attr=deps.cluster_cfg_attr(cluster),
                recapture=_recapture if deps.PIN_ENV_DEBUG else None,
            )
            misses.extend(emit_misses)
            if withdrawn:
                dropped = set(withdrawn)
                suite = BehaviourSuite(
                    cfg_cluster=cluster.name, specs=tuple(s for s in suite.specs if s.behaviour_id not in dropped)
                )
                # Only THIS pass's captures count against this pass's
                # reading; the emitter also withdraws specs merged in from a
                # previous pass's suite.
                admitted = len(admitted_ids - dropped)
            if emitted:
                by_id = {s.behaviour_id: s for s in suite.specs}
                for spec in emitted:
                    by_id[spec.behaviour_id] = spec
                suite = BehaviourSuite(cfg_cluster=cluster.name, specs=tuple(by_id[k] for k in sorted(by_id)))
        except Exception as e:
            deps.log(f"  [task0d] cluster {cluster.name}: behaviour-test emission failed (non-fatal, suite kept): {e}")
        deps.save_suite(project_dir, suite)
        # Written on every pass, empty list included: the file states what THIS
        # pass could not capture, so leaving the previous pass's file in place
        # when nothing was missed would report a stale set as current.
        deps.record_misses(project_dir, cluster.name, misses)
        line, payload = _reading_line(
            cluster.name, suite, filled, admitted, proposed, elapsed_s=time.monotonic() - t0, cargo_invocations=cargo_n
        )
        deps.log(line)
        deps.emit("task0d.harvest", **payload)
        deps.commit_and_mark(
            project_dir,
            key,
            "done",
            commit_message=f"pipeline: {key} — {len(suite.specs)} behaviour(s)",
            detail=(
                f"{len(suite.specs)} behaviours, runtime then "
                f"{sum(runtime_then_counts(suite.specs).values())}, "
                f"exercises {filled}, probes {admitted}/{proposed}"
            ),
        )
