"""TASK 0d — behaviour harvest, per cluster, BEFORE t0c.

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

import hashlib
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from pipeline_core.application.behaviour_book_build import BookBudget, build_behaviour_book, feature_files
from pipeline_core.application.emit_behaviour_tests import FailingAlone, Recapture, Settle, Verify
from pipeline_core.application.lanes import on_lanes
from pipeline_core.application.test_repair import repair_until_built
from pipeline_core.domain.behaviour import (
    BehaviourSpec,
    BehaviourSuite,
    GlossaryEntry,
    example_code,
    merge_suites,
)
from pipeline_core.domain.behaviour_book import (
    Prediction,
    glossary_entries,
    parse_predictions,
    private_names,
)
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
    ProbeBuild,
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
from pipeline_core.domain.fn_reachability import PublicIndex
from pipeline_core.domain.fn_routes import WorldFacts
from pipeline_core.domain.held_out import capture_key, split_held_out
from pipeline_core.domain.models import BuildCluster, FnRecord, ModRecord, TypeRecord
from pipeline_core.domain.module_reachability import externally_reachable_files
from pipeline_core.domain.reach_probe import (
    PROBE_FAILED,
    PROBE_MISSED,
    REACHED,
    ReachOutcome,
    ReachTarget,
    reach_targets,
)
from pipeline_core.domain.rule_probe import (
    DROP_REASON,
    RuleAsk,
    RuleTrial,
    apply_trials,
    judge_rule_on_captures,
    rule_probe_asks,
    without_examples,
)
from pipeline_core.domain.test_results import is_compiler_output_line
from pipeline_core.domain.use_cases import UseCaseEdge, edges_to_ask
from pipeline_core.domain.workspace_layout import (
    BEHAVIOUR_PHASE_PREFIX,
    behaviour_book_key,
    behaviour_harvest_key,
)
from pipeline_core.domain.world_book import project_book, union_glossary, union_worlds
from pipeline_core.phases import phase_scope, phase_skipped
from pipeline_core.ports.behaviour_probe import RunProbe
from pipeline_core.prompts.behaviour_rules import (
    build_probe_repair_prompt,
    build_rule_probe_prompt,
    build_then_prediction_prompt,
)
from pipeline_core.prompts.behaviour_synthesis import build_behaviour_synthesis_prompt, build_reach_prompt, reach_name

PHASE_PREFIX = BEHAVIOUR_PHASE_PREFIX


def phase_key(cluster_name: str) -> str:
    return behaviour_harvest_key(cluster_name)


class EmitTests(Protocol):
    """The behaviour-test emitter's signature
    (`application.emit_behaviour_tests.emit_behaviour_tests`), typed so a
    keyword added to the call is checked against the bound implementation."""

    def __call__(
        self, project_dir: Path, cfg_cluster: str,
        specs: Iterable[BehaviourSpec], *, crate: str, verify: Verify,
        failing_alone: FailingAlone, settle: Settle,
        perturb_env: Mapping[str, str] = ..., cfg_attr: str = ...,
        recapture: Recapture | None = ...,
    ) -> tuple[tuple[Path, ...], tuple[str, ...], list[dict[str, str]],
               tuple[BehaviourSpec, ...]]: ...


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
    run_probe: RunProbe
    exercises_by_coverage: Callable[..., object]
    # The uncapturable record. A probe that fails to compile, fails to run,
    # times out, prints nothing, is refused as not value-bearing, or does not
    # reproduce used to leave a single log line and nothing else — the source
    # file is unlinked in a `finally`, so the set of things we know we cannot
    # observe was invisible. These are NOT admitted as specs: a `then=None`
    # row is classified OTHER, ranked last and unpinnable, which is the same
    # silent-hole class under another name.
    record_misses: Callable[[Path, str, list[dict[str, str]]], object] = lambda _p, _c, _m: None
    # What `record_misses` wrote, so a later stage of the same pass adds its
    # rows instead of rewriting the file without the harvest's.
    load_misses: Callable[[Path, str], list[dict[str, str]]] = lambda _p, _c: []
    # Writes the replayable captures as `tests/behaviour_<cluster>_gen_1.rs`
    # plus one `tests/behaviour_<cluster>_world_…_gen_1.rs` per pinned
    # environment, after compiling and running them against the original.
    emit_tests: EmitTests = (
        lambda *_a, **_k: ((), (), [], ())
    )
    # Compiles and runs ONE emitted test binary under this cluster's env plus
    # any extra: (project_dir, cluster, stem, extra_env) -> (ok, output). The
    # emitted file is part of the reconstruction oracle, so it is held to the
    # same rule every other pipeline-authored test is — it must have been seen
    # to pass against the original before it can fail against a fill.
    verify_tests: Callable[..., tuple[bool, str]] = lambda *_a, **_k: (True, "")
    # (project_dir, cluster, crate, stem, names) -> the names of an emitted
    # file that fail alone in a process of their own
    # (`adapters.test_worlds.failing_alone`). Emission refuses a missing one:
    # skipping the check admitted the file as written.
    tests_failing_alone: Callable[..., Sequence[str]] | None = None
    # (project_dir, crate, test file, body, cluster) -> the measured gate, or
    # None (`application.test_worlds.settle_test_gate`). Emission refuses a
    # missing one for the same reason.
    settle_test_gate: Callable[..., str | None] | None = None
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
    # Repair asks for a probe that did not compile: the probe and the
    # compiler's diagnostics go back to the model (`application.test_repair`).
    # 0 drops a non-building probe as before.
    PROBE_REPAIR_ATTEMPTS: int = 0
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
    # prompt → reply for the book AUTHOR's calls (rule drafts, Given phrases),
    # bound with `BEHAVIOUR_BOOK_THINK`. None sends them through `ask`, which
    # the probe and rule-probe PROGRAM calls keep: those write Rust that must
    # compile, the book writes sentences and verdicts.
    book_ask: Callable[[str], str] | None = None
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
    # Model calls of one stage that read no other call's reply (the probe
    # rounds, the reach batches, the rule asks, the rule judges) run on this
    # many lanes (`application/lanes.py`), results in item order, so 1 is the
    # serial run byte for byte. The server's own bound is the process slot set
    # every call takes (`llm/slots.py`). Bound to `BEHAVIOUR_BOOK_LANES`, else
    # `PIPELINE_PARALLELISM`. Cargo stays serial: every capture of a world
    # builds into that world's one target directory.
    LANES: int = 1
    HELD_OUT_SHARE: int = 0
    # (project_dir, cluster name) -> the ORIGINAL crate's construction facts
    # for that world (`domain.fn_routes`), None without them: the reach
    # round's public routes. None runs no reach round.
    load_construction_facts: Callable[[Path, str], WorldFacts | None] = lambda _p, _c: None
    # Writes the reach round's outcomes (`.skeleton/_reach_<cluster>.json`),
    # which t0c reads to quote a measured miss beside the route.
    save_reach: Callable[[Path, str, list[ReachOutcome]], object] = lambda _p, _c, _o: None
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


def _public_index(deps: Task0dDeps, project_dir: Path,
                  cluster: str) -> PublicIndex | None:
    """The world's measured public index (rustdoc's nameable items by
    defining file), from the construction facts t0b stored; None when none
    were measured — the mod-chain walk then stands in."""
    try:
        facts = deps.load_construction_facts(project_dir, cluster)
    except Exception:  # noqa: BLE001 — a missing reading is "not measured"
        return None
    names = getattr(facts, "item_names", None)
    return PublicIndex.of(names) if names else None


def _union_text(parts: Sequence[tuple[str, str]]) -> str:
    """One text when every world produced the same one, otherwise each world's
    text under a `// <world>` mark. A single world, or worlds that agree, keeps
    the text the one-world prompt already carried."""
    kept: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, text in parts:
        if text in seen:
            continue
        seen.add(text)
        kept.append((name, text))
    if len(kept) <= 1:
        return kept[0][1] if kept else ""
    return "\n".join(f"// {name}\n{text}" for name, text in kept)


def _cluster_label(names: Sequence[str]) -> str:
    return "+".join(names)


@dataclass
class _ProbePlan:
    """What one world would have asked, before any model call."""

    cluster: BuildCluster
    targets: tuple[str, ...]
    class_asks: tuple[tuple[str, str], ...]
    edges: tuple[UseCaseEdge, ...]
    by_name: dict[str, GlossaryEntry]
    surface: str
    inventory: str
    example: str
    edge_reading: tuple[int, int]


@dataclass(frozen=True)
class _AskedProbe:
    asks: tuple[tuple[str, str], ...]
    batch: tuple[str, ...]
    edge_batch: tuple[UseCaseEdge, ...]
    reply: str
    contributors: frozenset[str]


def _probe_plan(
    deps: Task0dDeps,
    project_dir: Path,
    cluster: BuildCluster,
    suite: BehaviourSuite,
    records: list[FnRecord],
    mods: list[ModRecord],
    types: list[TypeRecord],
    sources: Sequence[tuple[str, str]] = (),
) -> _ProbePlan:
    """The probe ask this world would make, with no model call and no cargo.

    An empty plan (the kill switch, or nothing to ask) still carries the
    edge reading, which is zero when edges are off."""
    files = frozenset(r.file for r in records) | frozenset(t.file for t in types)
    reachable = externally_reachable_files(mods, files)
    type_files = {t.name: t.file for t in types if t.is_pub and t.is_exactly_pub}
    cfg_set = CfgSet.of(set(cluster.cfg_true), set(cluster.cfg_false))
    index = _public_index(deps, project_dir, cluster.name)
    targets: tuple[str, ...] = ()
    if deps.PROBE_MAX_PER_CLUSTER > 0:
        targets = probe_targets(
            records, suite.specs, max_targets=deps.PROBE_MAX_PER_CLUSTER,
            reachable_files=reachable, type_files=type_files, cfg_set=cfg_set,
            public_index=index)
    # Compositions. The vocabulary is built HERE, from the same records and
    # sources the book reads later: the suite carries no glossary yet (the
    # book is the last stage of the pass), and a target kind that read the
    # suite's would read an empty one.
    edges: tuple[UseCaseEdge, ...] = ()
    edge_reading = (0, 0)
    by_name: dict[str, GlossaryEntry] = {}
    if deps.PROBE_MAX_PER_CLUSTER > 0 and deps.PROBE_EDGES_PER_CLUSTER > 0:
        glossary = glossary_entries(records, types, sources, reachable_files=reachable,
                                    cfg_set=cfg_set, public_index=index)
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
    # class at all. `PROBE_MAX_PER_CLUSTER` stays the kill switch, so the
    # documented `0` still turns probes off entirely.
    class_of = probe_coverage_class if deps.PIN_ENV_DEBUG else behaviour_class_name
    class_asks: tuple[tuple[str, str], ...] = ()
    if deps.PROBE_MAX_PER_CLUSTER > 0:
        class_asks = class_targets(
            suite.specs, class_of, max_targets=deps.PROBE_CLASSES_PER_CLUSTER,
            prompts=class_prompts(pin_env_debug=deps.PIN_ENV_DEBUG))
    return _ProbePlan(
        cluster, targets, class_asks, edges, by_name,
        deps.public_surface(project_dir, cluster.name),
        deps.symbol_inventory(project_dir, cluster.name),
        _style_example(suite.specs), edge_reading)


def _ask_probe_rounds(
    deps: Task0dDeps, module_name: str, plans: Sequence[_ProbePlan],
) -> list[_AskedProbe]:
    """One prompt per round over the union of the plans. A world that
    contributed nothing to a round is not a contributor, and does not run
    that reply."""
    active = [p for p in plans if p.targets or p.class_asks or p.edges]
    if not active:
        return []
    class_asks: list[tuple[str, str]] = []
    seen_class: set[str] = set()
    targets: list[str] = []
    seen_t: set[str] = set()
    edges: list[UseCaseEdge] = []
    seen_e: set[UseCaseEdge] = set()
    for plan in active:
        for ask in plan.class_asks:
            if ask[0] not in seen_class:
                seen_class.add(ask[0])
                class_asks.append(ask)
        for name in plan.targets:
            if name not in seen_t:
                seen_t.add(name)
                targets.append(name)
        for edge in plan.edges:
            if edge not in seen_e:
                seen_e.add(edge)
                edges.append(edge)
    # One prompt carrying every class ask, with the leftover unit names as the
    # callable inventory beside it — NOT the same class asks repeated once per
    # unit batch, which is what `for batch in batches(targets)` did: with a cap
    # of 12 and a batch of 6 that was two identical class requests and twice
    # the GPU for one cohort. Without class asks the per-unit batching is
    # still what there is to do.
    rounds: list[tuple[tuple[tuple[str, str], ...], tuple[str, ...],
                       tuple[UseCaseEdge, ...], tuple[str, ...]]] = []
    if class_asks:
        who = tuple(p.cluster.name for p in active if p.class_asks or p.targets)
        rounds.append((tuple(class_asks), tuple(targets), (), who))
    else:
        for batch in batches(tuple(targets), PROBE_BATCH_SIZE):
            who = tuple(p.cluster.name for p in active if any(t in batch for t in p.targets))
            rounds.append(((), batch, (), who))
    # One more prompt for the compositions: a different question (two items
    # and the order they are used in), answered in the same block shape.
    for edge_batch in _edge_batches(edges, deps.PROBE_BLOCKS_PER_REPLY):
        who = tuple(p.cluster.name for p in active
                    if any(e in edge_batch for e in p.edges))
        rounds.append(((), (), tuple(edge_batch), who))
    built: list[tuple[tuple[tuple[str, str], ...], tuple[str, ...],
                      tuple[UseCaseEdge, ...], tuple[str, ...], str]] = []
    for asks, batch, edge_batch, who in rounds:
        if not who:
            continue
        involved = [p for p in active if p.cluster.name in who]
        label = _cluster_label(tuple(p.cluster.name for p in involved))
        edge_arg: list[tuple[str, str, str, str]] = []
        for edge in edge_batch:
            for plan in involved:
                prod = plan.by_name.get(edge.producer)
                cons = plan.by_name.get(edge.consumer)
                if prod is not None and cons is not None:
                    edge_arg.append((edge.producer, prod.signature,
                                     edge.consumer, cons.signature))
                    break
        prompt = build_behaviour_synthesis_prompt(
            module_name=module_name, cfg_cluster=label, target_units=batch,
            public_surface=_union_text([(p.cluster.name, p.surface) for p in involved]),
            symbol_inventory=_union_text([(p.cluster.name, p.inventory) for p in involved]),
            example_usages=_union_text([(p.cluster.name, p.example) for p in involved]),
            target_classes=asks, target_edges=tuple(edge_arg))
        built.append((asks, batch, edge_batch, who, prompt))
    # Every round's prompt reads only the plans, never another round's reply,
    # so the rounds are asked side by side; replies come back in round order.
    replies = on_lanes(lambda b: deps.ask(b[4]), built, deps.LANES, name="t0d-probe")
    asked = [_AskedProbe(asks, batch, edge_batch, reply, frozenset(who))
             for (asks, batch, edge_batch, who, _prompt), reply in zip(built, replies, strict=True)]
    if len({p.cluster.name for p in active}) > 1:
        deps.log(f"  [task0d] worlds {_cluster_label(tuple(p.cluster.name for p in active))}: "
                 "probes asked once")
    return asked


def _admit_probe_rounds(
    deps: Task0dDeps,
    project_dir: Path,
    module_name: str,
    plan: _ProbePlan,
    rounds: Sequence[_AskedProbe],
    default_cluster: str,
) -> tuple[list[BehaviourSpec], int, int, int, list[dict[str, str]], tuple[int, int]]:
    """(admitted probe specs, admitted count, proposed count, cargo runs,
    misses, (public-API edges an example asserts, edges)) for the `rounds`
    this world contributed to, run on this world's cargo.

    The exact-format refusal applies to a round that asked `EXACT_FORMAT`
    for this world: the class must be in the round's asks AND in this
    world's. A composition round asks no class, so its blocks are never
    refused as format answers."""
    misses: list[dict[str, str]] = []
    mine = [r for r in rounds if plan.cluster.name in r.contributors]
    if not mine:
        return [], 0, 0, 0, misses, plan.edge_reading
    cluster = plan.cluster
    admitted: list[BehaviourSpec] = []
    proposed = 0
    cargo_runs = 0
    per_unit: dict[str, int] = {}
    world_classes = {c for c, _ in plan.class_asks}
    for rnd in mine:
        batch = rnd.batch
        # Bound the WORK, not just the targets. `PROBE_MAX_PER_CLUSTER` caps how
        # many units are asked about; the number of blocks a reply may contain
        # was unbounded, and every block costs its own `cargo run` at up to the
        # probe timeout. A reply that emits fifty blocks used to buy fifty
        # compile-and-run cycles against a cap of twelve.
        all_parsed = parse_behaviour_specs(rnd.reply)
        parsed = all_parsed[: deps.PROBE_BLOCKS_PER_REPLY]
        round_classes = world_classes & {c for c, _ in rnd.asks}
        if len(all_parsed) > len(parsed):
            over = len(all_parsed) - len(parsed)
            misses.append({
                "unit": batch[0] if batch else "reply",
                "reason": f"over the blocks-per-reply cap: {over} block(s) "
                f"past {deps.PROBE_BLOCKS_PER_REPLY} not run",
            })
            deps.log(f"  [task0d] cluster {cluster.name}: {over} behaviour block(s) "
                     "past the per-reply cap were not run.")
        if not all_parsed:
            # A reply that parses to nothing used to leave no log, no miss and
            # no event — `proposed` simply stayed 0. That is the answer-dropped
            # class: the model may well have answered. Name which way it failed,
            # because a legacy-shaped reply says the prompt did not land while
            # an unparseable one says the reply did not.
            legacy = parse_behaviour_programs(rnd.reply)
            why = (
                "reply arrived in the legacy free-form shape (no <WHEN>)"
                if legacy
                else "reply carried no parseable <BEHAVIOUR> block"
            )
            misses.append({"unit": batch[0] if batch else "reply", "reason": why})
            deps.log(f"  [task0d] cluster {cluster.name}: {why}.")
            continue
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
            if (deps.PIN_ENV_DEBUG and "EXACT_FORMAT" in round_classes
                    and (fmt_why := exact_format_ask_rejects(probe))):
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
            if not rnd.edge_batch:
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
    return admitted, len(admitted), proposed, cargo_runs, misses, plan.edge_reading


#: Tail of a non-building probe's stderr handed back with it: the
#: diagnostics, not the cargo preamble.
_REPAIR_DIAG_CHARS = 4000


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
    runs = 0

    def _run(p: CapturedProbe) -> tuple[str, Any]:
        nonlocal runs
        runs += 1
        prog = render_probe_program(p.given, p.when, p.observe, crate=module_name)
        return prog, deps.run_probe(project_dir, cluster, prog, tag)

    def _diagnostics(ran: tuple[str, Any]) -> str | None:
        _prog, res = ran
        err = str(getattr(res, "stderr", "") or "")
        if res.ok or not any(is_compiler_output_line(ln.strip())
                             for ln in err.splitlines()):
            return None
        return err[-_REPAIR_DIAG_CHARS:]

    def _ask_repair(p: CapturedProbe, diag: str) -> CapturedProbe | None:
        prog = render_probe_program(p.given, p.when, p.observe, crate=module_name)
        reply = deps.ask(build_probe_repair_prompt(
            crate=module_name, given=p.given, when=p.when, observe=p.observe,
            program=prog, diagnostics=diag))
        blocks = parse_behaviour_specs(reply)
        if not blocks:
            return None
        fixed = replace(p, given=blocks[0].given, when=blocks[0].when)
        return None if value_bearing_reason(fixed.when) is not None else fixed

    # A primary probe that does not build is asked back with the compiler's
    # diagnostics (`application.test_repair`); an extra rendering
    # (`misses=None`) failing to compile is expected and is not repaired.
    probe, (program, result), repairs = repair_until_built(
        probe, run=_run, diagnostics=_diagnostics, ask=_ask_repair,
        attempts=deps.PROBE_REPAIR_ATTEMPTS if misses is not None else 0)
    if repairs and result.ok and result.stdout.strip():
        deps.log(f"  [task0d] cluster {cluster.name}: probe for {unit} "
                 f"repaired after {repairs} ask(s) with the compiler's "
                 "diagnostics.")
    if not (result.ok and result.stdout.strip()):
        if misses is not None:
            why = "no stdout" if result.ok else f"failed: {result.stderr[-300:].strip() or 'no stderr'}"
            misses.append({"unit": unit, "reason": why, "when": probe.when, "program": program})
            deps.log(f"  [task0d] cluster {cluster.name}: probe for {unit} dropped ({why}).")
        return None, runs
    confirm = deps.run_probe(project_dir, cluster, program, f"{tag}_r")
    runs += 1
    if confirm.stdout != result.stdout:
        if misses is not None:
            misses.append({"unit": unit, "reason": "not reproducible", "when": probe.when, "program": program})
            deps.log(f"  [task0d] cluster {cluster.name}: behaviour for "
                     f"{unit} dropped (output differed between two runs).")
        return None, runs
    return captured_spec(cfg_cluster=cluster.name, probe=probe, n=n, stdout=result.stdout,
                         default_cluster=default_cluster), runs


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

    def _recapture(spec: BehaviourSpec, extra: Mapping[str, str], build: ProbeBuild) -> str | None:
        program = program_for(spec, crate=module_name)
        result = deps.run_probe(project_dir, cluster, program, f"{cluster.name}_env",
                                extra_env=dict(extra), build=build)
        if result.ok and result.stdout.strip():
            return result.stdout
        return None

    alone_check = deps.tests_failing_alone
    settle_gate = deps.settle_test_gate
    if alone_check is None or settle_gate is None:
        raise TypeError(
            "behaviour emission requires the alone check and the world settle")
    _paths, withdrawn, misses, emitted = deps.emit_tests(
        project_dir,
        cluster.name,
        suite.specs,
        crate=module_name,
        verify=lambda stem, extra: deps.verify_tests(project_dir, cluster, module_name, stem, extra),
        failing_alone=lambda stem, names, extra: alone_check(
            project_dir, cluster, module_name, stem, names, extra),
        settle=lambda dest, body: settle_gate(
            project_dir, module_name, dest, body, cluster),
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
    sites: dict[str, frozenset[str]] | None = None,
) -> tuple[tuple[BehaviourSpec, ...], int, int]:
    """Specs with `exercises` filled from coverage; (specs, filled count,
    cargo invocations). A collaborator that still returns a bare dict
    (the pre-count signature) is treated as zero invocations. `sites`, when
    given, receives the same hits as site labels per spec (which `fmt` of a
    file ran), for the reach round."""
    runnable = [s for s in specs if is_test_assertion_spec(s) or is_probe_spec(s)]
    if not runnable:
        return specs, 0, 0
    raw = deps.exercises_by_coverage(project_dir, cluster, runnable, records,
                                     **({"sites_out": sites} if sites is not None else {}))
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


def _reach_unit(label: str) -> str:
    """A spec unit for a reach probe that names no internal: the label's hash."""
    return "reach_" + hashlib.sha1(label.encode()).hexdigest()[:8]


def _run_reach(
    deps: Task0dDeps,
    project_dir: Path,
    module_name: str,
    cluster: BuildCluster,
    suite: BehaviourSuite,
    records: list[FnRecord],
    executed: frozenset[str],
    *,
    default_cluster: str,
) -> tuple[list[BehaviourSpec], list[ReachOutcome], int]:
    """The reach round (`domain/reach_probe.py`): one probe along the MIR's
    public route to each function no example enters, captured twice on the
    original, then judged by its OWN site-level coverage. Returns
    (captured specs with `exercises` filled, outcome per target, cargo runs)."""
    targets = reach_targets(deps.load_construction_facts(project_dir, cluster.name), executed)
    if not targets:
        return [], [], 0
    # The per-cluster probe cap bounds this round too: one probe per target,
    # each captured twice. Targets past it are said, not dropped silently.
    cap = max(0, deps.PROBE_MAX_PER_CLUSTER)
    if len(targets) > cap:
        deps.log(f"  [task0d] cluster {cluster.name}: reach probes capped at {cap} of "
                 f"{len(targets)} routed function(s) (TASK0D_PROBE_MAX); the rest "
                 "are not asked this round.")
        targets = targets[:cap]
    surface = deps.public_surface(project_dir, cluster.name)
    example = _style_example(suite.specs)
    taken: list[tuple[ReachTarget, BehaviourSpec]] = []
    outcomes: dict[str, ReachOutcome] = {}
    cargo_runs = 0
    size = max(1, deps.PROBE_BLOCKS_PER_REPLY)
    starts = list(range(0, len(targets), size))
    # A batch's prompt reads its targets, the surface and the style example,
    # never a capture: every batch is asked first, side by side, and the
    # captures then run in batch order exactly as before (the `tag` numbering
    # and each target's outcome do not depend on when its reply arrived).
    replies = on_lanes(
        lambda st: deps.ask(build_reach_prompt(
            module_name, cluster.name, targets[st:st + size], surface, example)),
        starts, deps.LANES, name="t0d-reach")
    for start, reply in zip(starts, replies, strict=True):
        batch = targets[start:start + size]
        by_name = {p.name: p for p in parse_behaviour_specs(reply)}
        for i, tgt in enumerate(batch, 1):
            probe = by_name.get(reach_name(i))
            if probe is None:
                outcomes[tgt.label] = ReachOutcome(tgt.label, PROBE_FAILED,
                                                   reason="the reply carried no block for this line")
                continue
            # The observe kind is the route's, not the reply's: the line gave it.
            probe = replace(probe, name=_reach_unit(tgt.label), observe=tgt.observe)
            if (bad := value_bearing_reason(probe.when)) is not None:
                outcomes[tgt.label] = ReachOutcome(tgt.label, PROBE_FAILED, given=probe.given,
                                                   when=probe.when, observe=probe.observe,
                                                   reason=f"not value-bearing: {bad}")
                continue
            misses: list[dict[str, str]] = []
            spec, runs = _capture_twice(
                deps, project_dir, module_name, cluster, probe, unit=probe.name, n=0,
                tag=f"{cluster.name}_reach_{start + i}", default_cluster=default_cluster,
                misses=misses)
            cargo_runs += runs
            if spec is None:
                outcomes[tgt.label] = ReachOutcome(
                    tgt.label, PROBE_FAILED, given=probe.given, when=probe.when,
                    observe=probe.observe, reason=(misses[-1]["reason"] if misses else "no capture"))
                continue
            taken.append((tgt, spec))
    if not taken:
        return [], [outcomes[t.label] for t in targets if t.label in outcomes], cargo_runs
    sites: dict[str, frozenset[str]] = {}
    specs, _filled, ex_cargo = _fill_exercises(deps, project_dir, cluster,
                                               tuple(s for _t, s in taken), records, sites)
    cargo_runs += ex_cargo
    by_id = {s.behaviour_id: s for s in specs}
    for tgt, spec in taken:
        hit = tgt.label in sites.get(spec.behaviour_id, frozenset())
        outcomes[tgt.label] = ReachOutcome(
            tgt.label, REACHED if hit else PROBE_MISSED, behaviour_id=spec.behaviour_id,
            given=spec.setup, when=spec.when, observe=tgt.observe)
    return ([by_id[s.behaviour_id] for _t, s in taken],
            [outcomes[t.label] for t in targets if t.label in outcomes], cargo_runs)


#: The `stage` of a miss row written by the rule probes (`run_rule_probes`).
RULE_PROBE_STAGE = "rule_probe"


def run_rule_probes(
    deps: Task0dDeps,
    project_dir: Path,
    module_name: str,
    cluster: BuildCluster,
    book: BehaviourSuite,
    *,
    default_cluster: str,
    memo: dict[str, str],
    prepared: Mapping[str, str] | None = None,
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
    `memo` maps a prompt to its reply, shared across worlds. `prepared`
    maps a rule id to a reply already asked over the worlds together;
    a rule in that map is not asked again. Absent, this call asks."""
    asks = rule_probe_asks(book.features, max_rules=deps.RULE_PROBES_MAX)
    if not asks:
        return book

    memo_lock = threading.Lock()

    def asked(call: Callable[[str], str], prompt: str) -> str:
        # Shared with other lanes of this stage. Two lanes never build the
        # same prompt inside one world (each capture belongs to one rule's
        # pool, and the program asks differ by sentence), so no single-flight
        # is needed: the lock only keeps the dict's reads and writes whole.
        with memo_lock:
            if prompt in memo:
                return memo[prompt]
        reply = call(prompt)
        with memo_lock:
            memo.setdefault(prompt, reply)
            return memo[prompt]

    by_id = {s.behaviour_id: s for s in book.specs}
    ids = set(by_id)
    programs = {capture_key(s) for s in book.specs}
    captures: dict[str, list[BehaviourSpec]] = {}
    misses: list[dict[str, str]] = []
    proposed = cargo_runs = 0

    def _program_reply(ask: RuleAsk) -> str:
        if prepared is not None and ask.rule_id in prepared:
            return prepared[ask.rule_id]
        return asked(deps.ask, build_rule_probe_prompt(
            crate=module_name, cfg_cluster=cluster.name, sentence=ask.sentence,
            glossary=book.glossary, shown=[by_id[i] for i in ask.shown if i in by_id],
            near=[by_id[i] for i in ask.near if i in by_id],
            programs=deps.RULE_PROBE_PROGRAMS))

    # The program asks read only the book: asked side by side. The captures
    # stay in rule order below, because a capture's number `n` and the
    # `programs` dedup are first-come.
    replies = on_lanes(_program_reply, list(asks), deps.LANES, name="t0d-rule")
    for ask, reply in zip(asks, replies, strict=True):
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

    def check(rules: Sequence[str], want: Sequence[str]) -> Mapping[str, Prediction]:
        return parse_predictions(asked(predict, build_then_prediction_prompt(
            crate=module_name, glossary=book.glossary, examples=[new[i] for i in want],
            rules=rules)))

    sentences = {f.item: [r.sentence for r in f.rules] for f in book.features}
    trials: dict[str, RuleTrial] = {}
    mine: dict[str, list[str]] = {}
    judged: list[tuple[RuleAsk, dict[str, BehaviourSpec]]] = []
    for ask in asks:
        pool = {s.behaviour_id: s for s in captures.get(ask.rule_id, ()) if s.behaviour_id in new}
        if pool:
            mine[ask.rule_id] = list(pool)
            judged.append((ask, pool))

    def _both(first: Callable[[], Mapping[str, Prediction]],
              second: Callable[[], Mapping[str, Prediction]],
              ) -> tuple[Mapping[str, Prediction], Mapping[str, Prediction]]:
        a, b = on_lanes(lambda f: f(), [first, second], min(2, deps.LANES), name="t0d-judge-pair")
        return a, b

    # Each rule is judged on its own pool against the frozen book, and its
    # ablation and together calls read nothing of each other: every rule on a
    # lane, both first calls of each together, `alone` after `together`.
    verdicts = on_lanes(
        lambda j: judge_rule_on_captures(
            j[0].sentence, [x for x in sentences.get(j[0].feature, ()) if x != j[0].sentence],
            j[1], predict=check, both=_both),
        judged, deps.LANES, name="t0d-judge")
    for (ask, _pool), trial in zip(judged, verdicts, strict=True):
        trials[ask.rule_id] = trial
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
    # The rule probes' own misses, beside the harvest's: the log line is
    # only the count, and `record_misses` rewrites the file whole, so a
    # write of these alone would erase the harvest rows. Earlier rule-probe
    # rows are replaced, including by a pass that missed nothing — leaving
    # the previous pass's rule rows standing would report misses this pass
    # did not have. A pass that asked no rule returns above and does not
    # touch the file.
    kept_rows = [m for m in deps.load_misses(project_dir, cluster.name)
                 if m.get("stage") != RULE_PROBE_STAGE]
    deps.record_misses(project_dir, cluster.name, kept_rows + [
        {**{k: str(v) for k, v in m.items()}, "stage": RULE_PROBE_STAGE}
        for m in misses])
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
                                 cfg_set=cluster_cfg_set(c),
                                 public_index=_public_index(deps, project_dir, c.name))
        for c in present})
    union = union_worlds({c.name: suites[c.name] for c in present})
    marks = union.marks()
    private = private_names(records, types, glossary, union.suite.specs)
    book, reading = build_behaviour_book(
        union.suite, crate=module_name, glossary=glossary, private=private,
        ask=deps.book_ask or deps.ask, budget=deps.BOOK_BUDGET, log=deps.log, worlds=marks,
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


@dataclass
class _Harvested:
    """One world harvested this launch, not yet run on cargo or committed."""

    cluster: BuildCluster
    key: str
    suite: BehaviourSuite
    records: list[FnRecord]
    mods: list[ModRecord]
    types: list[TypeRecord]
    sources: list[tuple[str, str]]
    harvest_s: float


def _harvest_world(
    deps: Task0dDeps,
    project_dir: Path,
    module_name: str,
    cluster: BuildCluster,
    src_dir: Path,
    default_cluster: str,
) -> _Harvested | None:
    """The harvest of one world under its own scope, closed when it returns:
    sources and tests read, no model call, no cargo. None when it failed,
    after the world's key is marked failed."""
    key = phase_key(cluster.name)
    with phase_scope("t0d", cluster=cluster.name, module=module_name) as ph:
        ph.step("t0d.harvest")
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
            ph.close("error")
            return None
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
        return _Harvested(cluster, key, suite, records, mods, types, list(sources),
                          time.monotonic() - t0)


def _ask_probes_once(
    deps: Task0dDeps,
    project_dir: Path,
    module_name: str,
    passes: Sequence[_Harvested],
) -> tuple[dict[str, _ProbePlan], list[_AskedProbe]]:
    """Every harvested world's probe plan and the one ask over them, under the
    module's scope: the model calls belong to no one world. A world whose plan
    failed, or every world when the ask failed, runs no probe."""
    plans: dict[str, _ProbePlan] = {}
    with phase_scope("t0d", module=module_name) as ph:
        ph.step("t0d.probes")
        for p in passes:
            try:
                plans[p.cluster.name] = _probe_plan(
                    deps, project_dir, p.cluster, p.suite, p.records, p.mods, p.types, p.sources)
            except Exception as e:
                deps.log(f"  [task0d] cluster {p.cluster.name}: probes failed "
                         f"(non-fatal, harvested suite kept): {e}")
        try:
            return plans, _ask_probe_rounds(deps, module_name, list(plans.values()))
        except Exception as e:
            for name in plans:
                deps.log(f"  [task0d] cluster {name}: probes failed "
                         f"(non-fatal, harvested suite kept): {e}")
            return {}, []


def _finish_world(
    deps: Task0dDeps,
    project_dir: Path,
    module_name: str,
    p: _Harvested,
    plan: _ProbePlan | None,
    rounds: Sequence[_AskedProbe],
    default_cluster: str,
) -> BehaviourSuite:
    """One world's cargo and its commit, under its own scope: the probe
    replies it contributed to, hold-out, exercises, its reach round, emission,
    then the suite, the misses and `task0d/<world>` are written. A world is
    committed before the next world's cargo starts, so a process that dies
    in one world's hours loses that world only, and each step's wall time in
    `logs/phases.jsonl` is that world's."""
    cluster = p.cluster
    suite = p.suite
    admitted = proposed = cargo_n = filled = 0
    edge_reading = plan.edge_reading if plan is not None else (0, 0)
    misses: list[dict[str, str]] = []
    admitted_ids: set[str] = set()
    t0 = time.monotonic()
    with phase_scope("t0d", cluster=cluster.name, module=module_name) as ph:
        ph.step("t0d.probes")
        if plan is not None:
            try:
                probes, admitted, proposed, probe_cargo, misses, edge_reading = _admit_probe_rounds(
                    deps, project_dir, module_name, plan, rounds, default_cluster)
                cargo_n += probe_cargo
                admitted_ids = {s.behaviour_id for s in probes}
                if probes:
                    suite = merge_suites(suite, BehaviourSuite(cfg_cluster=cluster.name, specs=tuple(probes)))
            except Exception as e:
                deps.log(f"  [task0d] cluster {cluster.name}: probes failed (non-fatal, harvested suite kept): {e}")
        try:
            suite, _held = _hold_out(deps, project_dir, cluster.name, suite)
        except Exception as e:
            deps.log(f"  [task0d] cluster {cluster.name}: holding captures out failed "
                     f"(non-fatal, every capture stays in the suite): {e}")
        ph.step("t0d.exercises")
        sites: dict[str, frozenset[str]] | None = None
        if deps.EXERCISES_ENABLED:
            try:
                sites = {}
                specs, filled, ex_cargo = _fill_exercises(deps, project_dir, cluster, suite.specs,
                                                          p.records, sites)
                cargo_n += ex_cargo
                suite = suite.with_specs(specs)
            except Exception as e:
                sites = None
                deps.log(f"  [task0d] cluster {cluster.name}: exercises failed (non-fatal, harvested suite kept): {e}")
        # The reach round needs the site-level executed set the exercises just
        # measured: without it every function would look unexecuted. It asks
        # per world: its targets exist only after this world's exercises, and
        # a union ask would hold every world's commit until the last world's
        # exercises had run.
        ph.step("t0d.reach")
        if sites is None or deps.PROBE_MAX_PER_CLUSTER <= 0:
            # Said, not silent: t0c's prompt quotes the reach outcomes, and a
            # skipped round leaves every routed function without one.
            deps.log(f"  [task0d] cluster {cluster.name}: reach probes skipped — "
                     + ("the exercises reading is off or failed, and the round "
                        "judges a probe by the site-level coverage it measures"
                        if sites is None else
                        "TASK0D_PROBE_MAX is 0"))
        else:
            try:
                executed = frozenset(x for v in sites.values() for x in v)
                reach_specs, reach_out, reach_cargo = _run_reach(
                    deps, project_dir, module_name, cluster, suite, p.records, executed,
                    default_cluster=default_cluster)
                cargo_n += reach_cargo
                if reach_specs:
                    suite = merge_suites(suite, BehaviourSuite(cfg_cluster=cluster.name,
                                                               specs=tuple(reach_specs)))
                    admitted_ids |= {s.behaviour_id for s in reach_specs}
                    filled += sum(1 for s in reach_specs if s.exercises)
                deps.save_reach(project_dir, cluster.name, reach_out)
                if reach_out:
                    counts = {k: sum(1 for o in reach_out if o.outcome == k)
                              for k in (REACHED, PROBE_MISSED, PROBE_FAILED)}
                    deps.log(f"  [task0d] cluster {cluster.name}: reach probes: {len(reach_out)} "
                             f"function(s) no example entered, asked along their public route — "
                             f"{counts[REACHED]} reached, {counts[PROBE_MISSED]} ran without entering "
                             f"it, {counts[PROBE_FAILED]} did not capture; {reach_cargo} cargo invocation(s)")
                    deps.emit("task0d.reach", cluster=cluster.name, **counts,
                              cargo_invocations=reach_cargo)
            except Exception as e:
                deps.log(f"  [task0d] cluster {cluster.name}: reach probes failed (non-fatal, suite kept): {e}")
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
        ph.step("t0d.emit")
        try:
            suite, emit_misses, withdrawn = _emit_captures(deps, project_dir, module_name, cluster, suite)
            misses.extend(emit_misses)
            # Only THIS pass's captures count against this pass's reading; the
            # emitter also withdraws specs merged in from a previous pass's suite.
            admitted = len(admitted_ids - set(withdrawn))
        except Exception as e:
            deps.log(f"  [task0d] cluster {cluster.name}: behaviour-test emission failed (non-fatal, suite kept): {e}")
        deps.save_suite(project_dir, suite)
        # Written on every pass, empty list included: the file states what THIS
        # pass could not capture, so leaving the previous pass's file in place
        # when nothing was missed would report a stale set as current.
        deps.record_misses(project_dir, cluster.name, misses)
        # This world's harvest and cargo; the shared probe ask is in the
        # module's scope and in no world's elapsed time.
        line, payload = _reading_line(
            cluster.name, suite, filled, admitted, proposed,
            elapsed_s=p.harvest_s + time.monotonic() - t0,
            cargo_invocations=cargo_n, edges=edge_reading,
        )
        deps.log(line)
        deps.emit("task0d.harvest", **payload)
        deps.commit_and_mark(
            project_dir,
            p.key,
            "done",
            commit_message=f"pipeline: {p.key} — {len(suite.specs)} behaviour(s)",
            detail=(
                f"{len(suite.specs)} behaviours, runtime then "
                f"{sum(runtime_then_counts(suite.specs).values())}, "
                f"exercises {filled}, probes {admitted}/{proposed}"
            ),
        )
    return suite


def run_task0d(
    deps: Task0dDeps,
    project_dir: Path,
    module_name: str,
    clusters: list[BuildCluster],
    src_dir: Path,
) -> None:
    """Harvest → probes → exercises → reach → emit → save. Skips a cluster
    whose phase is already marked done. Then the module's book stage (doc
    examples, ONE behaviour book for every world, rule probes; `_book_once`)
    under `task0d/<module>/book`.

    The order across worlds: every world due this launch is harvested first
    (no model call, no cargo), then ONE probe ask covers them, then each world
    in turn runs its cargo, its own reach ask and its emission and is
    committed before the next world starts. A world's probe, exercise and
    reach cargo, its suite, misses and reach outcomes stay that world's. A
    resume with one world's key done runs the other, and its prompt names
    that world alone.

    The book stage runs when any cluster was harvested in this launch or when
    its key is not done: a launch that died during the book (an hour and more
    of model calls) resumes it here over the suites on disk, instead of
    finding every cluster done and leaving the book to nobody. Re-harvesting a
    cluster first sets a done book key back to pending, so a launch that dies
    between that harvest and the rebuilt book cannot leave a book built over
    the old suite marked done. The caller runs this only before task0c."""
    default_cluster = next((c.name for c in clusters if c.is_default), clusters[0].name if clusters else "default")
    book_key = behaviour_book_key(module_name)
    book_was_done = book_done = deps.phase_valid(project_dir, book_key)
    passed: dict[str, BehaviourSuite] = {}
    passes: list[_Harvested] = []
    for cluster in clusters:
        if deps.phase_valid(project_dir, phase_key(cluster.name)):
            deps.log(f"  [task0d] cluster {cluster.name}: already harvested — skipping.")
            phase_skipped("t0d", "already harvested", cluster=cluster.name, module=module_name)
            continue
        if book_done:
            deps.mark_phase(project_dir, book_key, "pending",
                            detail=f"cluster {cluster.name} re-harvested; the book is rebuilt")
            book_done = False
        harvested = _harvest_world(deps, project_dir, module_name, cluster, src_dir, default_cluster)
        if harvested is not None:
            passes.append(harvested)
    if len(passes) > 1:
        deps.log(f"  [task0d] worlds {_cluster_label(tuple(p.cluster.name for p in passes))}: harvest")
    plans, rounds = _ask_probes_once(deps, project_dir, module_name, passes) if passes else ({}, [])
    for p in passes:
        passed[p.cluster.name] = _finish_world(
            deps, project_dir, module_name, p, plans.get(p.cluster.name), rounds, default_cluster)
    if book_done:
        deps.log(f"  [task0d] {module_name}: book stage already done — skipping.")
        phase_skipped("t0d", "book stage already done", module=module_name)
        return
    if not passed:
        deps.log(f"  [task0d] {module_name}: no cluster harvested in this launch and the book "
                 "stage is not done — running it over the suites on disk.")
    _doc_examples_once(deps, project_dir, module_name, clusters)
    # Rule probes are owed by every world when no completed book stage
    # covers any of them; after one did, only by the worlds harvested again.
    probe_worlds = frozenset(passed) if book_was_done else frozenset(c.name for c in clusters)
    status, detail = _book_once(deps, project_dir, module_name, clusters, passed, src_dir,
                                probe_worlds=probe_worlds)
    if status == "done":
        deps.commit_and_mark(project_dir, book_key, "done",
                             commit_message=f"pipeline: {book_key} — {detail}", detail=detail)
    else:
        deps.mark_phase(project_dir, book_key, status, detail=detail)


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


def _rule_replies(
    deps: Task0dDeps,
    module_name: str,
    clusters: Sequence[BuildCluster],
    books: Mapping[str, BehaviourSuite],
    probe_worlds: frozenset[str],
) -> dict[str, str]:
    """One reply per rule id when more than one world is probed this launch.

    A single world is left to `run_rule_probes`, so its prompt names that
    world. The shared prompt joins the worlds that carry the rule, unions
    the shown and near examples by id, and unions the glossary
    (`union_glossary`: a name some of those worlds lack is marked with the
    worlds that have it). No cargo."""
    present = [c for c in clusters if c.name in probe_worlds and c.name in books]
    if len(present) < 2:
        return {}
    grouped: dict[str, list[tuple[str, RuleAsk]]] = {}
    order: list[str] = []
    for c in present:
        for ask in rule_probe_asks(books[c.name].features, max_rules=deps.RULE_PROBES_MAX):
            if ask.rule_id not in grouped:
                order.append(ask.rule_id)
                grouped[ask.rule_id] = []
            grouped[ask.rule_id].append((c.name, ask))
    if not order:
        return {}
    prompts: list[tuple[str, str]] = []
    asked: list[str] = []
    for rule_id in order:
        pairs = grouped[rule_id]
        names = [name for name, _ask in pairs]
        for name in names:
            if name not in asked:
                asked.append(name)
        shown_ids: list[str] = []
        near_ids: list[str] = []
        for _name, ask in pairs:
            for i in ask.shown:
                if i not in shown_ids:
                    shown_ids.append(i)
            for i in ask.near:
                if i not in near_ids:
                    near_ids.append(i)
        by_id = {s.behaviour_id: s for name, _ask in pairs for s in books[name].specs}
        prompt = build_rule_probe_prompt(
            crate=module_name,
            cfg_cluster=_cluster_label(names),
            sentence=pairs[0][1].sentence,
            glossary=union_glossary({name: books[name].glossary for name, _ask in pairs}),
            shown=[by_id[i] for i in shown_ids if i in by_id],
            near=[by_id[i] for i in near_ids if i in by_id],
            programs=deps.RULE_PROBE_PROGRAMS,
        )
        prompts.append((rule_id, prompt))
    # Each prompt reads only the books: asked side by side, in rule order.
    replies = on_lanes(lambda rp: deps.ask(rp[1]), prompts, deps.LANES, name="t0d-rule")
    out = {rule_id: reply for (rule_id, _p), reply in zip(prompts, replies, strict=True)}
    if len(asked) > 1:
        deps.log(f"  [task0d] worlds {_cluster_label(asked)}: rule probes asked once")
    return out


def _book_once(
    deps: Task0dDeps,
    project_dir: Path,
    module_name: str,
    clusters: list[BuildCluster],
    passed: Mapping[str, BehaviourSuite],
    src_dir: Path,
    *,
    probe_worlds: frozenset[str],
) -> tuple[str, str]:
    """The book last, once for every world: its features group each pass's
    FINAL examples (a rule citing a capture the emitter withdrew would cite
    nothing), and a world that was harvested in an earlier pass contributes
    its suite from disk, so the union the book is built over is always every
    world's. Additive like every stage after the harvest: a failure keeps the
    suites without a book, says so, and the strip bake's book (which runs
    when a suite on disk has none) builds it. Returns the book stage's
    (status, detail) for its state entry."""
    suites = {c.name: passed[c.name] if c.name in passed else deps.load_suite(project_dir, c.name)
              for c in clusters}
    suites = {k: v for k, v in suites.items() if v.specs and not v.is_legacy}
    if not suites:
        return "done", "no suite to book"
    with phase_scope("t0d", module=module_name) as ph:
        ph.step("t0d.book")
        try:
            books = build_book_from_source(deps, project_dir, module_name, clusters, suites, src_dir)
        except Exception as e:
            deps.log(f"  [task0d] behaviour book failed (non-fatal, suites kept "
                     f"without rules): {e}")
            return "failed", f"behaviour book failed: {e}"[:200]
        # Rule probes: only for a world in `probe_worlds`, each judged on its
        # own store. The original is in front of every world here: the stage
        # runs only before task0c. Asks and judge calls are shared across
        # worlds by prompt: the one book projects the same sentences into
        # each.
        if deps.RULE_PROBES_MAX > 0:
            ph.step("t0d.rule_probes")
            memo: dict[str, str] = {}
            try:
                prepared_rules = _rule_replies(
                    deps, module_name, clusters, books, probe_worlds)
            except Exception as e:
                prepared_rules = {}
                deps.log(f"  [task0d] {module_name}: the shared rule-probe ask failed "
                         f"({e}); each world asks for itself.")
            default_cluster = next((c.name for c in clusters if c.is_default),
                                   clusters[0].name if clusters else "default")
            for c in clusters:
                if c.name not in probe_worlds or c.name not in books:
                    continue
                try:
                    books[c.name] = run_rule_probes(
                        deps, project_dir, module_name, c, books[c.name],
                        default_cluster=default_cluster, memo=memo,
                        prepared=prepared_rules or None)
                except Exception as e:
                    deps.log(f"  [task0d] cluster {c.name}: rule probes failed (non-fatal, "
                             f"the book is kept as built): {e}")
        for book in books.values():
            deps.save_suite(project_dir, book)
    probed = sorted(probe_worlds & set(books)) if deps.RULE_PROBES_MAX > 0 else []
    return "done", (f"{len(books)} world(s) booked ({'+'.join(sorted(books))}), rule probes on "
                    f"{'+'.join(probed) or 'none'}")
