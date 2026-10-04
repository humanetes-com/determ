"""The pipeline's phase registry: every phase and sub-phase, in run order.

One list, imported by both halves of the same fact. The pipeline announces
`phase.enter` / `phase.exit` events by id through `pipeline_core.phases`; the
monitor draws its phase strip, its sub-step line and its detailed view from
this list and those events. Before it existed the strip was a hard-coded
tuple of ten names driven by the `current:` line of `pipeline_state.md`, and
most phases never wrote that line: during the strip/bake/behaviour-book stretch
of an augmented restart (run `34607-00cca24e`, 2026-09-30) the state file still
read `current: task0c/nightly / starting`, so the monitor showed augmentation
for the 24 minutes the book spent making model calls.

A phase that is not in this list cannot be announced (`phases` refuses an
unknown id and the completeness test scans every literal id at every call
site), and a `pipeline_state.md` key that maps to no phase fails the same
test, so a new phase cannot ship without a place on the strip and a written
description.

Ids: a top-level phase is `t0c`; its sub-step is `t0c.augment`. Order in
`PHASES` is run order; a child sits after its parent and children keep the
order the code executes them in. `per_cluster` marks phases the pipeline runs
once per compiler cluster (stable, nightly, ...): their events carry a
`cluster` and the monitor shows one status per cluster.

`state_keys` are the `pipeline_state.md` keys a phase writes; `done_keys`
(default: the same) are the ones whose `done` status proves it finished. A
trailing `/*` matches every key under that prefix and needs all of them done
(`task0d/*` covers one key per cluster). Any one pattern satisfying is enough,
so a phase the combined `task2-4` marker also closes lists both. The state file, not the event stream, is the authority for "done
in an earlier process"; the events say what THIS process ran, skipped or is
running.
"""
from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "PHASE_ENTER", "PHASE_EXIT", "PHASE_PROGRESS", "OUTCOMES", "Phase", "PHASES", "TOP_LEVEL",
    "phase", "children_of", "parent_of", "state_key_owner", "known_ids",
    "phase_satisfied_by_state",
]

PHASE_ENTER = "phase.enter"
PHASE_EXIT = "phase.exit"
# A step that stays open for a long time (the behaviour book) reports how
# much of its own plan is done without closing. The monitor draws that as
# the bar under the step row. It is not a phase and it has no registry id.
PHASE_PROGRESS = "phase.progress"

# What a `phase.exit` reports. `skipped` = the phase's own checkpoint said it
# was already done, so nothing ran; `aborted` = the process is stopping.
OUTCOMES: tuple[str, ...] = ("ok", "error", "skipped", "aborted")


@dataclass(frozen=True, slots=True)
class Phase:
    """One phase or sub-phase: its id, its label on the strip, and the
    written description the detailed view shows."""

    id: str
    label: str
    title: str
    what: str
    why: str = ""
    reads: str = ""
    writes: str = ""
    skipped_when: str = ""
    per_cluster: bool = False
    # Every `pipeline_state.md` key this phase writes (ownership: the
    # completeness test fails on a key no phase claims).
    state_keys: tuple[str, ...] = field(default=())
    # The subset whose `done` proves the phase finished, any one sufficing.
    # Empty = the same as `state_keys`.
    done_keys: tuple[str, ...] = field(default=())

    @property
    def parent(self) -> str | None:
        head, dot, _ = self.id.partition(".")
        return head if dot else None


def _p(id: str, label: str, title: str, what: str, **kw: object) -> Phase:
    return Phase(id, label, title, what, **kw)  # type: ignore[arg-type]


PHASES: tuple[Phase, ...] = (
    # ------------------------------------------------------------ setup
    _p("setup", "setup", "Setup",
       "Finds the compiler clusters the crate builds under (cfg, feature and toolchain "
       "combinations such as stable and nightly) and the command that runs its tests. "
       "Static analysis, no model calls.",
       why="Every later phase is scoped per cluster. After the bake the frozen cluster "
           "list on disk is used instead of rescanning, because a rescan would walk the "
           "reconstructed code and mint clusters from cfgs the reconstruction exposes.",
       reads="Cargo manifests, README, .skeleton/_clusters.json when baked",
       writes="pipeline_state.md keys cfg_scan and test_cmd",
       skipped_when="the test command is already recorded in pipeline_state.md",
       state_keys=("cfg_scan", "test_cmd"), done_keys=("test_cmd",)),
    _p("setup.cfg_scan", "cfg-scan", "Cluster scan",
       "Identifies the build clusters, or loads the baked list."),
    _p("setup.test_cmd", "test-cmd", "Test command",
       "Discovers or loads the cargo test command."),
    # -------------------------------------------------------------- t0a
    _p("t0a", "t0a", "Project understanding",
       "Reads the README and the tests and writes two documents: a profile of what the "
       "crate is, and a behavioural context summary that later prompts carry.",
       why="Later phases run on tests alone once the source is stripped; these are the "
           "big picture they are given.",
       reads="README, test files",
       writes="project_profile.md, project_context.md",
       skipped_when="the documents are already recorded as done",
       state_keys=("task0a", "task0a-context"), done_keys=("task0a-context",)),
    _p("t0a.profile", "profile", "Project profile", "Language, build system, crate layout."),
    _p("t0a.context", "context", "Project context",
       "Behavioural summary synthesised from the test content."),
    # -------------------------------------------------------------- t0b
    _p("t0b", "t0b", "Discovery",
       "Writes the coverage strategy: import idioms, how to construct the crate's types, "
       "feature flags, test conventions.",
       why="Injected into the augmentation, requirements and fill prompts so the model "
           "does not rediscover how to build and test this crate.",
       reads="README, tests, profile",
       writes="coverage_strategy.md",
       skipped_when="coverage_strategy.md is already recorded as done",
       state_keys=("task0b",)),
    _p("t0b.discovery", "discovery", "Coverage strategy", "The model writes coverage_strategy.md."),
    # -------------------------------------------------------------- t0d
    _p("t0d", "t0d", "Behaviour harvest",
       "Per cluster, before augmentation: turns the ORIGINAL crate's tests, doc-tests and "
       "layout pins into a behaviour suite, runs probes against the original and records "
       "their output as the expected result, records which functions each example "
       "exercises, emits the captures as real tests, and groups the examples into a "
       "behaviour book of checked rules.",
       why="It is the specification that augmentation and layout authoring read. This is "
           "the last legal read of original bodies for behaviours: after strip only tests "
           "and baked artefacts remain.",
       reads="original source (pre-strip), tests",
       writes=".skeleton/_behaviours_<cluster>.json, .skeleton/_heldout_<cluster>.json "
              "(captures held out for the agreement reading), emitted behaviour tests, "
              "feature files",
       skipped_when="the cluster is already harvested and the module's book stage is done, "
                    "or once task0c has started (an augmented restart included), where the "
                    "strip's bake builds a missing suite or book instead",
       per_cluster=True, state_keys=("task0d/*",)),
    _p("t0d.harvest", "harvest", "Harvest", "Suite from assertions, doc-tests and layout pins.", per_cluster=True),
    _p("t0d.probes", "probes", "Probes", "Probes run on the original; stdout is the Then.", per_cluster=True),
    _p("t0d.exercises", "exercises", "Exercises",
       "Which functions each example enters, by coverage.", per_cluster=True),
    _p("t0d.emit", "emit", "Emit tests", "Captured values become real, executed tests.", per_cluster=True),
    _p("t0d.doc_examples", "doc examples", "Doc examples",
       "Once per module, after the clusters' passes: each runnable doc example of the "
       "original is rendered as a test, run on the original, and kept in the worlds "
       "where it passes."),
    _p("t0d.book", "book", "Behaviour book",
       "Once for every build world, after the clusters' passes: rules drafted and "
       "checked by the model over the union of the suites, projected into each "
       "cluster's store; chunks run in parallel lanes."),
    _p("t0d.rule_probes", "rule probes", "Rule probes",
       "After the book: each kept rule is tested on new programs run against the "
       "original; confirmed captures join the rule, a rule refuted on all of them is "
       "dropped, and the captures are emitted as tests."),
    # -------------------------------------------------------------- t0c
    _p("t0c", "t0c", "Coverage augmentation",
       "Per cluster and module, the model adds unit tests until coverage reaches the "
       "target. Every original and added test is kept as the reconstruction oracle.",
       why="The oracle is what the fill loop is scored against; an untested function "
           "cannot be reconstructed by evidence.",
       reads="original source, tests, coverage strategy",
       writes="generated tests (gen_tests), coverage sentinels",
       skipped_when="the cluster's sentinel still binds under the current code identity",
       per_cluster=True, state_keys=("task0c/*",)),
    _p("t0c.precheck", "pre-check", "Pre-check",
       "The base library must compile under this cluster's environment; a nightly "
       "feature on stable is promoted to a nightly toolchain.", per_cluster=True),
    _p("t0c.augment", "augment", "Augment",
       "Attempts, compile fixes, coverage measurement, per module.", per_cluster=True),
    # ------------------------------------------------------------- bake
    _p("bake", "bake", "Bake",
       "The only read of original bodies after augmentation. Moves inline tests to "
       "sibling files, captures the API surface, then per cluster bakes a skeleton with "
       "unimplemented stubs, classifies which private items may be re-authored, harvests "
       "behaviours, then builds one book for every world if a suite has none, and writes the cluster "
       "manifest and the gen-tests origin manifest.",
       why="After strip these artefacts are the only source-derived inputs any later "
           "phase may read (no original source reaches a prompt).",
       reads="original source",
       writes=".skeleton/, .original_structure/",
       skipped_when="the module is already stripped",
       state_keys=("strip/*",)),
    _p("bake.preserve", "inline-tests", "Preserve inline tests",
       "Moves any inline test module into a sibling file so it is not writable text."),
    _p("bake.api", "api", "API surface", "Public API, type and pub-item manifests."),
    _p("bake.extract", "skeleton", "Extract skeleton",
       "The extractor stubs every body under this cluster's cfg.", per_cluster=True),
    _p("bake.surface", "surface", "Private surface",
       "Frozen public items versus replaceable private items.", per_cluster=True),
    _p("bake.harvest", "harvest", "Behaviour harvest",
       "Behaviours from live source, merged with the suite on disk.", per_cluster=True),
    _p("bake.book", "book", "Behaviour book",
       "Built here, once for every build world, when a suite on disk has none; "
       "chunks run in parallel lanes."),
    _p("bake.manifest", "manifest", "Cluster manifest", "Cluster list and gen-tests origin cfg."),
    # ------------------------------------------------------------- t1.5
    _p("layout", "t1.5", "Layout authoring",
       "The model re-authors the private layout over the baked skeleton, cluster by "
       "cluster: which struct fields and helper types exist. Public items stay frozen. Each "
       "component is gated, probed against the oracle and adopted or dropped.",
       why="The fill loop only writes function bodies, so the shape those bodies live in "
           "has to be authored first.",
       reads="skeleton, private surface, behaviour suite, tests",
       writes=".skeleton/<cluster>/src layout, layout registry and outcomes",
       skipped_when="the module is already stripped; a failure here is non-fatal and "
                    "leaves the frozen skeleton untouched",
       state_keys=("strip/*",)),
    # ------------------------------------------------------------ strip
    _p("strip", "strip", "Strip",
       "Commits the baked tree, tags it as the augmented restore point, deletes the "
       "original implementation bodies, records the phase done, and seals the oracle.",
       why="From here the loop sees tests and baked artefacts only.",
       reads="baked tree", writes="git commit and tag, stripped src/, oracle seal",
       skipped_when="the module is already stripped",
       state_keys=("strip/*",)),
    _p("strip.commit", "commit", "Commit and tag", "Augmentation commit and the restore tag."),
    _p("strip.source", "source", "Delete bodies", "Removes the module's implementation source."),
    _p("strip.seal", "seal", "Seal oracle", "Fingerprints the test oracle so later drift is refused."),
    # --------------------------------------------------------------- t1
    _p("t1", "t1", "Requirements",
       "Writes the functional requirements from the tests, the README and the baked API "
       "surface, validates them, and retries or escalates on failure.",
       why="The fill prompts carry these requirements next to the tests.",
       reads="tests, API surface, behaviour suite",
       writes="functional_requirements_<crate>.md",
       skipped_when="the requirements file already validates",
       state_keys=("task1/*",)),
    # -------------------------------------------------------------- t2a
    _p("t2a", "t2a", "Reconstruction",
       "Per cluster, serially with the default cluster first: cargo-check the skeleton, "
       "group the tests, then fill the stubs in a test-driven loop (sweep, grade, aim, "
       "frontier, fill) until the stops fire, then clean up residual stubs and validate. "
       "Later clusters seed shared bodies from earlier ones.",
       why="This is the reconstruction itself and where the hours go.",
       reads="baked skeleton, tests, requirements, behaviour suite",
       writes=".skeleton-filled/<cluster>/src",
       skipped_when="the cluster's checkpoint is valid and its filled tree is on disk",
       per_cluster=True, state_keys=("task2a/*", "task2-4/*")),
    _p("t2a.blueprint", "blueprint", "Blueprint",
       "Top-down design of the crate that per-block fills consume slices of."),
    _p("t2a.check", "check", "Skeleton check", "Cargo check of the baked skeleton.", per_cluster=True),
    _p("t2a.tests", "tests", "Group tests", "The tests that apply to this cluster.", per_cluster=True),
    _p("t2a.fill", "fill", "Fill loop", "The test-driven outer loop over the frontier.", per_cluster=True),
    _p("t2a.residual", "residual", "Residual stubs",
       "Cleanup pass over stubs the loop left.", per_cluster=True),
    _p("t2a.validate", "validate", "Validate",
       "Cargo check of the filled tree; implicated bodies are reverted to stubs.", per_cluster=True),
    # -------------------------------------------------------------- t2b
    _p("t2b", "t2b", "Merge",
       "Combines the per-cluster filled trees into src/ with cfg gating; bodies shared "
       "across clusters are deduplicated.",
       why="The crate is one tree; the clusters only exist to build it under each cfg.",
       reads=".skeleton-filled/<cluster>/src", writes="src/",
       skipped_when="the merge marker is valid and the merged tree is intact",
       state_keys=("task2b/*", "task2-4/*")),
    # -------------------------------------------------------------- t2c
    _p("t2c", "t2c", "Merged-tree fill",
       "Fills the stubs that exist, or only compile, in the merged tree, and accepts "
       "bodies by test result.",
       reads="src/, tests", writes="src/",
       skipped_when="the checkpoint is valid",
       state_keys=("task2c/*", "task2-4/*")),
    # --------------------------------------------------------------- t3
    _p("t3", "t3", "Test run",
       "Runs the cluster-scoped cargo tests and reads a strict aggregate verdict; "
       "a failure enters the repair loop.",
       why="The verdict against the oracle is the score.",
       reads="src/, tests", writes="test results",
       state_keys=("task2-4/*",)),
    # --------------------------------------------------------------- t4
    _p("t4", "t4", "Failure analysis",
       "Analyses the failures into genesis rules for later fill prompts and snapshots "
       "the best tree by passing count; then the loop returns to a fix pass.",
       reads="test results", writes="genesis.md, best-tree snapshot",
       state_keys=("task2-4/*",)),
    # -------------------------------------------------------------- lock
    _p("lock", "lock", "Lock",
       "The final stub verdict over the whole tree, then the module is locked (several "
       "modules) or the run completes (one module).",
       reads="src/", writes="git commit, lock",
       state_keys=("task2-4/*",)),
)

_BY_ID: dict[str, Phase] = {p.id: p for p in PHASES}
TOP_LEVEL: tuple[Phase, ...] = tuple(p for p in PHASES if p.parent is None)


def known_ids() -> frozenset[str]:
    return frozenset(_BY_ID)


def phase(phase_id: str) -> Phase | None:
    return _BY_ID.get(phase_id)


def parent_of(phase_id: str) -> str | None:
    p = _BY_ID.get(phase_id)
    return p.parent if p else None


def children_of(phase_id: str) -> tuple[Phase, ...]:
    return tuple(p for p in PHASES if p.parent == phase_id)


def _pattern_matches(pattern: str, key: str) -> bool:
    if pattern.endswith("/*"):
        return key.startswith(pattern[:-1])
    return key == pattern


def state_key_owner(key: str) -> str | None:
    """The top-level phase id whose `state_keys` cover a `pipeline_state.md`
    key, or None. `strip/*` is claimed by bake, t1.5 and strip alike; the first
    in run order (`bake`) is the owner for this purpose, since the question is
    only "does the registry know this key"."""
    for p in TOP_LEVEL:
        if any(_pattern_matches(pat, key) for pat in p.state_keys):
            return p.id
    return None


def phase_satisfied_by_state(p: Phase, statuses: dict[str, str]) -> bool:
    """True when some `state_keys` pattern has at least one matching key and
    every matching key is `done`."""
    for pat in (p.done_keys or p.state_keys):
        matched = [s for k, s in statuses.items() if _pattern_matches(pat, k)]
        if matched and all(s == "done" for s in matched):
            return True
    return False
