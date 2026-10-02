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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pipeline_core.application.behaviour_book_build import BookBudget, build_behaviour_book, feature_files
from pipeline_core.domain.behaviour import (
    BehaviourSpec,
    BehaviourSuite,
    GlossaryEntry,
    example_code,
    merge_suites,
)
from pipeline_core.domain.behaviour_book import glossary_entries, parse_predictions, private_names
from pipeline_core.domain.behaviour_census import (
    defect_observable_flags,
    klass_histogram,
    render_counts,
    runtime_then_counts,
)
from pipeline_core.domain.behaviour_coverage import is_test_assertion_spec
from pipeline_core.domain.behaviour_probe import (
    PROBE_BATCH_SIZE,
    CapturedProbe,
    ProbeResult,
    batches,
    captured_spec,
    class_prompts,
    class_targets,
    exact_format_ask_rejects,
    is_probe_spec,
    other_observes,
    parse_behaviour_programs,
    parse_behaviour_specs,
    probe_behaviour_id,
    probe_coverage_class,
    probe_targets,
    program_for,
    render_probe_program,
    value_bearing_reason,
)
from pipeline_core.domain.behaviour_select import behaviour_class_name
from pipeline_core.domain.cfg import CfgSet, cluster_cfg_set
from pipeline_core.domain.held_out import capture_key, split_held_out
from pipeline_core.domain.models import BuildCluster, FnRecord, ModRecord, TypeRecord
from pipeline_core.domain.module_reachability import externally_reachable_files
from pipeline_core.domain.rule_probe import (
    DROP_REASON,
    RuleTrial,
    apply_trials,
    judge_rule_on_captures,
    rule_probe_asks,
    without_examples,
)
from pipeline_core.domain.use_cases import UseCaseEdge, edges_to_ask
from pipeline_core.domain.world_book import project_book, union_glossary, union_worlds
from pipeline_core.phases import phase_scope, phase_skipped
from pipeline_core.prompts.behaviour_rules import build_rule_probe_prompt, build_then_prediction_prompt
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
    # (project_dir, module, clusters): render the original's doc examples as
    # tests under `tests/`, keep those that pass on it, gate each to the
    # worlds it passes in. None emits nothing (the pre-2026-10-02 phase).
    emit_doc_examples: Callable[[Path, str, list[BuildCluster]], Any] | None = None
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
    # The behaviour book's rule calls (BEHAVIOUR_BOOK_* in `.env`):
    # `rules=False` still builds the glossary and features, with no sentence.
    BOOK_BUDGET: BookBudget = BookBudget()
    # prompt → reply for the book JUDGE's calls (ablation, set, leave-one-out),
    # bound at `BEHAVIOUR_BOOK_PREDICT_TEMPERATURE`. None sends them through
    # `ask`, the author's sampled collaborator.
    book_predict: Callable[[str], str] | None = None
    # (project_dir, cluster, {file name: text}) — the generated, read-only
    # Gherkin view under `.skeleton/_features_<cluster>/`.
    write_features: Callable[[Path, str, dict[str, str]], object] = lambda _p, _c, _f: None
    # Percent of the pipeline's own captures set aside as the held-out
    # agreement reading (`domain/held_out.py`): not emitted, not in the suite
    # a prompt reads, not in the book. 0 holds nothing out. The store is
    # written here and read back only by the agreement measurement.
    # Compositions of two public items asked per cluster
    # (`domain/use_cases.py`): edges of the public API no example asserts.
    # 0 asks none and counts none.
    PROBE_EDGES_PER_CLUSTER: int = 0
    # Kept rules of the book tested on new captures per cluster
    # (`domain/rule_probe.py`), and programs asked per rule. 0 rules: the
    # stage does not run.
    RULE_PROBES_MAX: int = 0
    RULE_PROBE_PROGRAMS: int = 3
    HELD_OUT_SHARE: int = 0
    save_held_out: Callable[[Path, BehaviourSuite], object] = lambda _p, _s: None
    load_held_out: Callable[[Path, str], BehaviourSuite] = (
        lambda _p, c: BehaviourSuite(cfg_cluster=c))


def _style_example(specs: tuple[BehaviourSpec, ...]) -> str:
    """One harvested example's code (helpers, setup, when), for the prompt's
    EXAMPLE_USAGES style block."""
    for s in specs:
        if is_test_assertion_spec(s) and s.setup.strip():
            return example_code(s)
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
    sources: Sequence[tuple[str, str]] = (),
) -> tuple[list[BehaviourSpec], int, int, int, list[dict[str, str]], tuple[int, int]]:
    """(admitted probe specs, admitted count, proposed count, cargo runs,
    misses, (public-API edges an example asserts, edges))."""
    misses: list[dict[str, str]] = []
    if deps.PROBE_MAX_PER_CLUSTER <= 0:
        return [], 0, 0, 0, misses, (0, 0)
    files = frozenset(r.file for r in records) | frozenset(t.file for t in types)
    reachable = externally_reachable_files(mods, files)
    type_files = {t.name: t.file for t in types if t.is_pub and t.is_exactly_pub}
    cfg_set = CfgSet.of(set(cluster.cfg_true), set(cluster.cfg_false))
    targets = probe_targets(
        records,
        suite.specs,
        max_targets=deps.PROBE_MAX_PER_CLUSTER,
        reachable_files=reachable,
        type_files=type_files,
        cfg_set=cfg_set,
    )
    # Compositions. The vocabulary is built HERE, from the same records and
    # sources the book reads later: the suite carries no glossary yet (the
    # book is the last stage of the pass), and a target kind that read the
    # suite's would read an empty one.
    edges: tuple[UseCaseEdge, ...] = ()
    edge_reading = (0, 0)
    by_name: dict[str, GlossaryEntry] = {}
    if deps.PROBE_EDGES_PER_CLUSTER > 0:
        glossary = glossary_entries(records, types, sources, reachable_files=reachable,
                                    cfg_set=cfg_set)
        by_name = {g.name: g for g in glossary}
        edges, asserted, total = edges_to_ask(glossary, suite.specs,
                                              max_edges=deps.PROBE_EDGES_PER_CLUSTER)
        edge_reading = (asserted, total)
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
    if not targets and not class_asks and not edges:
        return [], 0, 0, 0, misses, edge_reading
    surface = deps.public_surface(project_dir, cluster.name)
    inventory = deps.symbol_inventory(project_dir, cluster.name)
    example = _style_example(suite.specs)
    # One prompt carrying every class ask, with the leftover unit names as the
    # callable inventory beside it — NOT the same class asks repeated once per
    # unit batch, which is what `for batch in batches(targets)` did: with a cap
    # of 12 and a batch of 6 that was two identical class requests and twice
    # the GPU for one cohort. Without class asks the old per-unit batching is
    # still what there is to do.
    _Round = tuple[tuple[tuple[str, str], ...], tuple[str, ...], tuple[UseCaseEdge, ...]]
    rounds: list[_Round] = (
        [(class_asks, tuple(targets), ())] if class_asks
        else [((), tuple(b), ()) for b in batches(targets, PROBE_BATCH_SIZE)]
    )
    # One more prompt for the compositions: a different question (two items
    # and the order they are used in), answered in the same block shape.
    rounds.extend(((), (), tuple(b)) for b in _edge_batches(edges, deps.PROBE_BLOCKS_PER_REPLY))
    admitted: list[BehaviourSpec] = []
    proposed = 0
    cargo_runs = 0
    per_unit: dict[str, int] = {}
    for asks, batch, edge_batch in rounds:
        prompt = build_behaviour_synthesis_prompt(
            module_name=module_name,
            cfg_cluster=cluster.name,
            target_units=batch,
            public_surface=surface,
            symbol_inventory=inventory,
            example_usages=example,
            target_classes=asks,
            target_edges=tuple((e.producer, by_name[e.producer].signature,
                                e.consumer, by_name[e.consumer].signature)
                               for e in edge_batch),
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
            spec, runs = _capture_twice(
                deps, project_dir, module_name, cluster, probe, unit=unit, n=n,
                tag=f"{cluster.name}_{proposed}", default_cluster=default_cluster, misses=misses)
            cargo_runs += runs
            if spec is None:
                continue
            admitted.append(spec)
            if not edge_batch:
                continue
            # A composition is observed under every rendering its value has,
            # not only the one asked for: a tree right on `{}` and wrong on
            # `{:#?}` after the same two calls is the recorded class. A
            # rendering the value does not implement fails to compile and is
            # skipped (expected, so not a miss); each one kept is run twice
            # like any capture.
            for other in other_observes(probe):
                k = per_unit[unit]
                more, runs = _capture_twice(
                    deps, project_dir, module_name, cluster, replace(probe, observe=other),
                    unit=unit, n=k, tag=f"{cluster.name}_{proposed}_{other}",
                    default_cluster=default_cluster, misses=None)
                cargo_runs += runs
                if more is not None:
                    per_unit[unit] = k + 1
                    admitted.append(more)
    return admitted, len(admitted), proposed, cargo_runs, misses, edge_reading


def _capture_twice(
    deps: Task0dDeps,
    project_dir: Path,
    module_name: str,
    cluster: BuildCluster,
    probe: CapturedProbe,
    *,
    unit: str,
    n: int,
    tag: str,
    default_cluster: str,
    misses: list[dict[str, str]] | None,
) -> tuple[BehaviourSpec | None, int]:
    """Run `probe` against the original twice and return its capture, or
    None, with the cargo runs spent. The one capture path every probe kind
    goes through (unit, class, composition, rule).

    Run TWICE and require the same output. One run cannot tell a
    deterministic observable from a lucky one, and a captured Then that does
    not reproduce becomes a flaky test in the oracle for the life of the
    project. Only for a probe that produced something to compare: re-running
    one that failed to COMPILE pays the most expensive cargo call there is
    for an answer already known. `misses=None` records nothing for a failed
    run: an extra rendering the value does not implement is expected."""
    program = render_probe_program(probe.given, probe.when, probe.observe, crate=module_name)
    result = deps.run_probe(project_dir, cluster, program, tag)
    if not (result.ok and result.stdout.strip()):
        if misses is not None:
            why = "no stdout" if result.ok else f"failed: {result.stderr[-300:].strip() or 'no stderr'}"
            misses.append({"unit": unit, "reason": why, "when": probe.when, "program": program})
            deps.log(f"  [task0d] cluster {cluster.name}: probe for {unit} dropped ({why}).")
        return None, 1
    confirm = deps.run_probe(project_dir, cluster, program, f"{tag}_r")
    if confirm.stdout != result.stdout:
        if misses is not None:
            misses.append({"unit": unit, "reason": "not reproducible", "when": probe.when, "program": program})
            deps.log(f"  [task0d] cluster {cluster.name}: behaviour for "
                     f"{unit} dropped (output differed between two runs).")
        return None, 2
    return captured_spec(cfg_cluster=cluster.name, probe=probe, n=n, stdout=result.stdout,
                         default_cluster=default_cluster), 2


def _emit_captures(
    deps: Task0dDeps, project_dir: Path, module_name: str, cluster: BuildCluster,
    suite: BehaviourSuite,
) -> tuple[BehaviourSuite, list[dict[str, str]], tuple[str, ...]]:
    """Write `suite`'s replayable captures as this cluster's test file and
    return `(suite, misses, withdrawn ids)`: the suite without the captures
    the emitter withdrew and with the specs it emitted upserted (capture-world
    originals carrying their env, perturb-world twins).

    Captures become REAL tests here, or they stay notes about the crate.
    Emitted from the observed value, never re-asked of the model, and
    deliberately NOT put through t0c's admission contract: that contract
    exists to catch a guessed literal, and this value was printed by the
    crate itself. It IS compiled and run against the original first —
    emission without execution puts an entry no fill can ever satisfy into
    the oracle, which is the class t0c's runtime gate exists for. A capture
    that fails that gate is withdrawn from the SUITE too: a value that cannot
    be replayed is not a specification, and leaving it listed would hand
    t1.5 a MUST no tree can meet."""

    def _recapture(spec: BehaviourSpec, extra: Mapping[str, str]) -> str | None:
        program = program_for(spec, crate=module_name)
        result = deps.run_probe(project_dir, cluster, program, f"{cluster.name}_env", extra_env=dict(extra))
        if result.ok and result.stdout.strip():
            return result.stdout
        return None

    _dest, withdrawn, misses, emitted = deps.emit_tests(
        project_dir,
        cluster.name,
        suite.specs,
        crate=module_name,
        verify=lambda stem, extra: deps.verify_tests(project_dir, cluster, module_name, stem, extra),
        perturb_env=deps.perturb_env(cluster),
        cfg_attr=deps.cluster_cfg_attr(cluster),
        recapture=_recapture if deps.PIN_ENV_DEBUG else None,
    )
    if withdrawn:
        gone = set(withdrawn)
        suite = suite.with_specs(s for s in suite.specs if s.behaviour_id not in gone)
    if emitted:
        by_id = {s.behaviour_id: s for s in suite.specs}
        for spec in emitted:
            by_id[spec.behaviour_id] = spec
        suite = suite.with_specs(by_id[k] for k in sorted(by_id))
    return suite, list(misses), tuple(withdrawn)


def _edge_batches(edges: Sequence[UseCaseEdge], size: int) -> list[Sequence[UseCaseEdge]]:
    size = max(1, size)
    return [edges[i:i + size] for i in range(0, len(edges), size)]


def _hold_out(
    deps: Task0dDeps, project_dir: Path, cluster: str, suite: BehaviourSuite,
) -> tuple[BehaviourSuite, int]:
    """`suite` without its held-out captures, and how many were set aside.

    Applied to the whole merged suite, before exercises, emission and the
    book, so nothing downstream ever receives a held-out capture: a capture
    already emitted is in `tests/` and in the augmented tag and cannot be
    withdrawn later without moving the oracle. The store is written BEFORE
    the captures leave the suite; a store that cannot be written leaves the
    suite whole (the captures become tests, as before) and says so."""
    kept, held = split_held_out(suite.specs, deps.HELD_OUT_SHARE)
    if not held:
        return suite, 0
    prior = deps.load_held_out(project_dir, cluster)
    deps.save_held_out(project_dir, merge_suites(
        prior, BehaviourSuite(cfg_cluster=cluster, specs=held)))
    deps.log(f"  [task0d] cluster {cluster}: {len(held)} of {len(held) + sum(1 for s in kept if is_probe_spec(s))} "
             f"capture(s) held out of the suite for the agreement reading "
             f"(share {deps.HELD_OUT_SHARE}%; not emitted, not in any prompt, not in the book)")
    deps.emit("task0d.held_out", cluster=cluster, held=len(held), share=deps.HELD_OUT_SHARE)
    return suite.with_specs(kept), len(held)


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


def run_rule_probes(
    deps: Task0dDeps,
    project_dir: Path,
    module_name: str,
    cluster: BuildCluster,
    book: BehaviourSuite,
    *,
    default_cluster: str,
    memo: dict[str, str],
) -> BehaviourSuite:
    """`book` after each of its kept rules was tested on new captures
    (`domain/rule_probe.py`): confirmed captures join the rule, refuted ones
    are recorded on it, a rule refuted on every new capture is dropped, and
    every capture that replays becomes an example and an emitted test.

    Order. Capture (against the original, twice) → hold out the split's
    share → judge each rule on what is left → emit the whole suite again,
    which is what verifies a new capture replays and writes each test with
    the rules it is an example of → remove what the emitter withdrew from
    the book (a rule citing a withdrawn capture would cite nothing). The
    caller saves the returned suite; the Gherkin view is rewritten here.
    `memo` maps a prompt to its reply, shared across worlds."""
    asks = rule_probe_asks(book.features, max_rules=deps.RULE_PROBES_MAX)
    if not asks:
        return book

    def asked(call: Callable[[str], str], prompt: str) -> str:
        if prompt not in memo:
            memo[prompt] = call(prompt)
        return memo[prompt]

    by_id = {s.behaviour_id: s for s in book.specs}
    ids = set(by_id)
    programs = {capture_key(s) for s in book.specs}
    captures: dict[str, list[BehaviourSpec]] = {}
    misses: list[dict[str, str]] = []
    proposed = cargo_runs = 0
    for ask in asks:
        reply = asked(deps.ask, build_rule_probe_prompt(
            crate=module_name, cfg_cluster=cluster.name, sentence=ask.sentence,
            glossary=book.glossary, shown=[by_id[i] for i in ask.shown if i in by_id],
            near=[by_id[i] for i in ask.near if i in by_id],
            programs=deps.RULE_PROBE_PROGRAMS))
        for probe in parse_behaviour_specs(reply)[:deps.RULE_PROBE_PROGRAMS]:
            proposed += 1
            unit = probe.name or "rule"
            if (bad := value_bearing_reason(probe.when)) is not None:
                misses.append({"unit": unit, "reason": f"not value-bearing: {bad}", "when": probe.when})
                continue
            n = 0
            while probe_behaviour_id(cluster.name, unit, n) in ids:
                n += 1
            spec, runs = _capture_twice(
                deps, project_dir, module_name, cluster, probe, unit=unit, n=n,
                tag=f"{cluster.name}_rule_{proposed}", default_cluster=default_cluster, misses=misses)
            cargo_runs += runs
            # A program the suite already holds is not a new case.
            if spec is None or capture_key(spec) in programs:
                continue
            ids.add(spec.behaviour_id)
            programs.add(capture_key(spec))
            captures.setdefault(ask.rule_id, []).append(spec)
    taken = [s for specs in captures.values() for s in specs]
    kept, held = split_held_out(taken, deps.HELD_OUT_SHARE)
    if held:
        deps.save_held_out(project_dir, merge_suites(
            deps.load_held_out(project_dir, cluster.name),
            BehaviourSuite(cfg_cluster=cluster.name, specs=held)))
    new = {s.behaviour_id: s for s in kept}
    predict = deps.book_predict or deps.ask

    def check(rules: Sequence[str], want: Sequence[str]) -> Mapping[str, str]:
        return parse_predictions(asked(predict, build_then_prediction_prompt(
            crate=module_name, glossary=book.glossary, examples=[new[i] for i in want],
            rules=rules)))

    sentences = {f.item: [r.sentence for r in f.rules] for f in book.features}
    trials: dict[str, RuleTrial] = {}
    mine: dict[str, list[str]] = {}
    for ask in asks:
        pool = {s.behaviour_id: s for s in captures.get(ask.rule_id, ()) if s.behaviour_id in new}
        if not pool:
            continue
        mine[ask.rule_id] = list(pool)
        trials[ask.rule_id] = judge_rule_on_captures(
            ask.sentence, [x for x in sentences.get(ask.feature, ()) if x != ask.sentence],
            pool, predict=check)
    features, dropped = apply_trials(book.features, mine, trials)
    suite = replace(book, specs=(*book.specs, *kept), features=features)
    withdrawn: tuple[str, ...] = ()
    if kept:
        suite, emit_misses, withdrawn = _emit_captures(deps, project_dir, module_name, cluster, suite)
        misses.extend(emit_misses)
        suite = replace(suite, features=without_examples(suite.features, set(withdrawn)))
        deps.write_features(project_dir, cluster.name, feature_files(suite))
    gone = set(withdrawn)
    confirmed = sum(1 for t in trials.values() for i in t.confirmed if i not in gone)
    refuted = sum(1 for t in trials.values() for i in t.refuted if i not in gone)
    deps.log(f"  [task0d] cluster {cluster.name}: rule-probes: {len(asks)} rule(s) asked, "
             f"{proposed} program(s) proposed, {len(taken)} captured ({len(held)} held out, "
             f"{len(withdrawn)} withdrawn at emission), {confirmed} confirmed, {refuted} refuted, "
             f"{len(dropped)} rule(s) dropped ({DROP_REASON}); {len(misses)} miss(es), "
             f"{cargo_runs} cargo invocation(s)")
    deps.emit("task0d.rule_probes", cluster=cluster.name, rules_asked=len(asks),
              proposed=proposed, captured=len(taken), held_out=len(held),
              withdrawn=len(withdrawn), confirmed=confirmed, refuted=refuted,
              rules_dropped=list(dropped), misses=len(misses), cargo_invocations=cargo_runs)
    return suite


def build_world_book(
    deps: Task0dDeps,
    project_dir: Path,
    module_name: str,
    clusters: Sequence[BuildCluster],
    suites: Mapping[str, BehaviourSuite],
    records: list[FnRecord],
    mods: list[ModRecord],
    types: list[TypeRecord],
    sources: list[tuple[str, str]],
) -> dict[str, BehaviourSuite]:
    """ONE behaviour book over every world's suite, projected into each
    world's store: `{cluster name: suite with its book}` for every cluster in
    `suites`.

    The glossary is built under each world's cfg set (`cluster_cfg_set`, the
    one "is this compiled here" context) and unioned with each name marked by
    the worlds that compile it; the examples are unioned by content
    (`world_book.union_worlds`). The model is asked about each example once,
    whatever the number of worlds. Each store receives the book restricted to
    its world (`world_book.project_book`) and its Gherkin view. Logs the one
    `behaviour-book:` reading and emits `task0d.behaviour_book` once.

    Why one: built per cluster, the strip bake of run `72627-751534fe`
    (2026-09-30) asked the same 11 features twice, 10,090 s for stable and
    10,639 s for nightly, over 227 examples identical in every field but
    `cfg_cluster`; the two prompts differed by one glossary line."""
    present = [c for c in clusters if c.name in suites]
    files = frozenset(r.file for r in records) | frozenset(t.file for t in types)
    reachable = externally_reachable_files(mods, files) if mods else None
    glossary = union_glossary({
        c.name: glossary_entries(records, types, sources, reachable_files=reachable,
                                 cfg_set=cluster_cfg_set(c))
        for c in present})
    union = union_worlds({c.name: suites[c.name] for c in present})
    marks = union.marks()
    private = private_names(records, types, glossary, union.suite.specs)
    book, reading = build_behaviour_book(
        union.suite, crate=module_name, glossary=glossary, private=private,
        ask=deps.ask, budget=deps.BOOK_BUDGET, log=deps.log, worlds=marks,
        predict=deps.book_predict)
    reading = replace(reading, worlds=union.clusters, residue_examples=len(marks),
                      residue_names=sum(1 for g in glossary if g.worlds))
    out: dict[str, BehaviourSuite] = {}
    for c in present:
        projected = project_book(book, union, c.name, suites[c.name])
        deps.write_features(project_dir, c.name, feature_files(projected))
        out[c.name] = projected
    names = "+".join(union.clusters)
    deps.log(f"  [task0d] worlds {names}: {reading.line()}")
    deps.emit("task0d.behaviour_book", cluster=names, clusters=list(union.clusters),
              **reading.payload())
    return out


def build_book_from_source(
    deps: Task0dDeps,
    project_dir: Path,
    module_name: str,
    clusters: Sequence[BuildCluster],
    suites: Mapping[str, BehaviourSuite],
    src_dir: Path,
) -> dict[str, BehaviourSuite]:
    """`build_world_book` for a caller that holds the pre-strip source but
    not t0d's enumerations: the strip bake. The book reads the ORIGINAL
    public records and doc comments exactly as t0d's pass does (both run
    before strip), plus the suites; never a post-strip tree.

    Why the bake calls it: t0d's book is reached only from `run_task0d`,
    which returns at the augmented check, so a restart from the augmented
    tag re-harvested the suite in the bake and never wrote a book (run
    `273278-22529e44`, 2026-09-29: format 2, 227 specs, `features` 0,
    `glossary` 0)."""
    records = list(deps.enumerate_fns(src_dir) or [])
    mods = list(deps.enumerate_mods(src_dir) or [])
    types = list(deps.enumerate_types(src_dir) or [])
    return build_world_book(deps, project_dir, module_name, clusters, suites,
                            records, mods, types, deps.read_sources(src_dir))


def _reading_line(
    cluster: str,
    suite: BehaviourSuite,
    filled: int,
    admitted: int,
    proposed: int,
    *,
    elapsed_s: float,
    cargo_invocations: int,
    edges: tuple[int, int] = (0, 0),
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
        + (f"; {edges[0]} of {edges[1]} public-API edge(s) asserted by an example" if edges[1] else "")
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
        "edges_asserted": edges[0],
        "edges": edges[1],
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
    a later stage raises. Then, when any cluster ran, ONE behaviour book for
    every world (`_book_once`)."""
    default_cluster = next((c.name for c in clusters if c.is_default), clusters[0].name if clusters else "default")
    passed: dict[str, BehaviourSuite] = {}
    for cluster in clusters:
        key = phase_key(cluster.name)
        if deps.phase_valid(project_dir, key):
            deps.log(f"  [task0d] cluster {cluster.name}: already harvested — skipping.")
            phase_skipped("t0d", "already harvested", cluster=cluster.name, module=module_name)
            continue
        _ph = phase_scope("t0d", cluster=cluster.name, module=module_name).open()
        _ph.step("t0d.harvest")
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
            _ph.close("error")
            continue
        earlier = deps.load_suite(project_dir, cluster.name)
        if earlier.is_legacy and earlier.specs:
            deps.log(f"  [task0d] cluster {cluster.name}: discarding the format-"
                     f"{earlier.format_version} suite on disk ({len(earlier.specs)} "
                     f"spec(s)) — re-baked in the current format, never converted.")
        suite = merge_suites(earlier, harvested)
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
        edge_reading = (0, 0)
        cargo_n = 0
        misses: list[dict[str, str]] = []
        admitted_ids: set[str] = set()
        _ph.step("t0d.probes")
        try:
            probes, admitted, proposed, probe_cargo, misses, edge_reading = _run_probes(
                deps, project_dir, module_name, cluster, suite, records, mods, types, default_cluster,
                sources=sources,
            )
            cargo_n += probe_cargo
            admitted_ids = {p.behaviour_id for p in probes}
            if probes:
                suite = merge_suites(suite, BehaviourSuite(cfg_cluster=cluster.name, specs=tuple(probes)))
        except Exception as e:
            deps.log(f"  [task0d] cluster {cluster.name}: probes failed (non-fatal, harvested suite kept): {e}")
        try:
            suite, _held = _hold_out(deps, project_dir, cluster.name, suite)
        except Exception as e:
            deps.log(f"  [task0d] cluster {cluster.name}: holding captures out failed "
                     f"(non-fatal, every capture stays in the suite): {e}")
        filled = 0
        _ph.step("t0d.exercises")
        if deps.EXERCISES_ENABLED:
            try:
                specs, filled, ex_cargo = _fill_exercises(deps, project_dir, cluster, suite.specs, records)
                cargo_n += ex_cargo
                suite = suite.with_specs(specs)
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
        _ph.step("t0d.emit")
        try:
            suite, emit_misses, withdrawn = _emit_captures(deps, project_dir, module_name, cluster, suite)
            misses.extend(emit_misses)
            # Only THIS pass's captures count against this pass's reading; the
            # emitter also withdraws specs merged in from a previous pass's suite.
            admitted = len(admitted_ids - set(withdrawn))
        except Exception as e:
            deps.log(f"  [task0d] cluster {cluster.name}: behaviour-test emission failed (non-fatal, suite kept): {e}")
        deps.save_suite(project_dir, suite)
        passed[cluster.name] = suite
        # Written on every pass, empty list included: the file states what THIS
        # pass could not capture, so leaving the previous pass's file in place
        # when nothing was missed would report a stale set as current.
        deps.record_misses(project_dir, cluster.name, misses)
        line, payload = _reading_line(
            cluster.name, suite, filled, admitted, proposed, elapsed_s=time.monotonic() - t0,
            cargo_invocations=cargo_n, edges=edge_reading,
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
        _ph.close()
    if passed:
        _doc_examples_once(deps, project_dir, module_name, clusters)
        _book_once(deps, project_dir, module_name, clusters, passed, src_dir)


def _doc_examples_once(
    deps: Task0dDeps,
    project_dir: Path,
    module_name: str,
    clusters: list[BuildCluster],
) -> None:
    """The crate's doc examples become oracle tests, once for every world
    (`application.doc_example_tests`): the original is on disk here to verify
    each against, and it is the last phase where that is true before the
    augmentation measures coverage. Additive like every stage after the
    harvest: a failure keeps the suites and says so."""
    if deps.emit_doc_examples is None:
        return
    with phase_scope("t0d", module=module_name) as ph:
        ph.step("t0d.doc_examples")
        try:
            deps.emit_doc_examples(project_dir, module_name, clusters)
        except Exception as e:
            deps.log(f"  [task0d] doc examples were not emitted as tests "
                     f"(non-fatal, the suites are kept): {e}")


def _book_once(
    deps: Task0dDeps,
    project_dir: Path,
    module_name: str,
    clusters: list[BuildCluster],
    passed: Mapping[str, BehaviourSuite],
    src_dir: Path,
) -> None:
    """The book last, once for every world: its features group each pass's
    FINAL examples (a rule citing a capture the emitter withdrew would cite
    nothing), and a world that was harvested in an earlier pass contributes
    its suite from disk, so the union the book is built over is always every
    world's. Additive like every stage after the harvest: a failure keeps the
    suites without a book, says so, and the strip bake's book (which runs
    when a suite on disk has none) builds it."""
    suites = {c.name: passed[c.name] if c.name in passed else deps.load_suite(project_dir, c.name)
              for c in clusters}
    suites = {k: v for k, v in suites.items() if v.specs and not v.is_legacy}
    if not suites:
        return
    with phase_scope("t0d", module=module_name) as ph:
        ph.step("t0d.book")
        try:
            books = build_book_from_source(deps, project_dir, module_name, clusters, suites, src_dir)
        except Exception as e:
            deps.log(f"  [task0d] behaviour book failed (non-fatal, suites kept "
                     f"without rules): {e}")
            return
        # Rule probes: only for a world whose captures were taken THIS pass
        # (the original is in front of it, and its phase just ran), each
        # judged on its own store. Asks and judge calls are shared across
        # worlds by prompt: the one book projects the same sentences into
        # each.
        if deps.RULE_PROBES_MAX > 0:
            ph.step("t0d.rule_probes")
            memo: dict[str, str] = {}
            default_cluster = next((c.name for c in clusters if c.is_default),
                                   clusters[0].name if clusters else "default")
            for c in clusters:
                if c.name not in passed or c.name not in books:
                    continue
                try:
                    books[c.name] = run_rule_probes(
                        deps, project_dir, module_name, c, books[c.name],
                        default_cluster=default_cluster, memo=memo)
                except Exception as e:
                    deps.log(f"  [task0d] cluster {c.name}: rule probes failed (non-fatal, "
                             f"the book is kept as built): {e}")
        for book in books.values():
            deps.save_suite(project_dir, book)
