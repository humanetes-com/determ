"""Per-fn pass context for the phase-c fill engine (de-closure of
`_process_one_stub` → `_fill_one_stub`)."""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pipeline_core.domain.attribution import FailureFrontier
from pipeline_core.domain.behaviour import BehaviourSpec
from pipeline_core.domain.cfg import CfgSet
from pipeline_core.domain.cfg_dead_sites import CfgDeadSites
from pipeline_core.domain.crash_site import CrashSites
from pipeline_core.domain.models import FillResult, FnLocator, ImplBlock, Stub
from pipeline_core.domain.retro_gate import RetroGateTally
from pipeline_core.ports.fill_engine import TreeLock


@dataclass(frozen=True, slots=True)
class PerFnPassCtx:
    """Read-only per-pass context for the per-fn fill unit: everything the
    unit used to capture from the enclosing engine scope, made explicit.
    Mutable members (`retros_cache`, `splice_check_lock`, the record lists)
    are SHARED with the pass by design — same objects the closure shared."""
    project_dir: Path
    target_src_dir: Path
    module_name: str
    cluster_name: str
    check_cmd: str
    cluster_env: dict[str, str]
    fr_path: Path | None
    tests_dir_files: list[Path]
    macros_section: str
    invariants_section: str
    crate_type_records: list[Any]
    src_for_macros: Path
    failures_dir: Path
    tdd_frontier_tests: FailureFrontier | None
    tdd_test_cmd: str | None
    splice_check_lock: TreeLock
    retros_cache: dict[str, Any]
    idiom_blocks: list[ImplBlock]
    pass_n: int
    total: int
    # Cluster cfg context for inactive-branch filtering at prompt-render time.
    cluster_cfg_set: CfgSet | None
    # Behaviours harvested for this cluster, for the few-shot USAGE_EXAMPLES
    # channel. Empty when no suite was baked (additive).
    behaviour_specs: tuple[BehaviourSpec, ...] = ()
    # Localized crash evidence keyed by (file, fn_name): rendered faulting/
    # origin stacks for fns implicated in a binary crash. Drives a
    # CRASH_EVIDENCE prompt section and overrides the fully-exhausted skip
    # (a localized crash is evidence prior attempts never saw).
    crash_evidence: dict[Any, str] | None = None
    # The ORIGIN blame only (FnLocator set) — where the localizer put the
    # fault. NOT derivable from `crash_evidence`, whose keys include access
    # frames: the innocent caller routing execution into the fault is a key
    # there, so key-membership reads the blame as inside the caller.
    crash_blamed: frozenset[Any] | None = None
    # Units reverted for a crash-only regression whose blame lies wholly
    # outside them, keyed retro-style (`file::container::fn`), value =
    # "<blamed file>@<content md5>" at mark time. SHARED and MUTATED across
    # iterations by the fill units (same sharing model as `retros_cache`):
    # while the blamed file's text is unchanged, re-attempting the unit buys
    # the same crash — skip it, charge nothing. Cleared per-unit the moment
    # the blamed file's content moves. In-process only, deliberately: the
    # measured burn (five reverts of `src/context.rs::StdErrorforE`,
    # 2026-08-10, ~4-6 model calls each) is intra-run, and a restart costs
    # at most one re-derivation cycle per unit.
    crash_blocked: dict[str, str] | None = None
    # Sieve counters for this pass (shared, mutated by the workers): how many
    # stored guidance strings were honoured / retired / unjudgeable. Logged
    # once per pass so "retires everything" and "retires nothing" are
    # distinguishable — both look like silence in per-unit lines alone.
    retro_gate: RetroGateTally = field(default_factory=RetroGateTally)
    # Pass-start stub keys from the cluster-active TREE scan, BEFORE the
    # frontier filter — exhaustion-skipped stubs must stay visible here (they
    # left the frontier but still abort the test binary). Read by the
    # sibling-blocked check before the U2 fix loop: a unit whose every
    # failing attributed test needs one of these is kept-and-deferred instead
    # of re-prompted (`domain/dispatch.sibling_blocked_u2_failures`).
    still_stubbed: frozenset[FnLocator] = frozenset()
    # The sites this cluster's build never compiles, stub or filled
    # (`cluster_membership.cluster_dead_sites`) — the dispatch-side cfg
    # verdict. Read by the retro gate: a conviction that recorded one of
    # these as a blocker can never see it fill, so it is not a tree premise
    # (`screen_guidance(cfg_dead=)`).
    cfg_dead: CfgDeadSites = CfgDeadSites()
    # True when the cluster-active tree held NO hollow body at pass start.
    # Read by the regression gate: a fill that newly crashes binaries in a
    # stub-free tree has no hollow sibling to blame and is reverted at once,
    # instead of being kept until the next sweep's ratchet while every later
    # unit in the batch is judged with those binaries excluded.
    stub_free: bool = False
    # The cluster's fn records at pass start (extractor dicts: `file`,
    # `fn_name`, `container`, `sig_text`, `cfg_predicates`), gen-tests
    # excluded: what the joint-write gate resolves a reply's extra `<FN>`
    # blocks against (`domain.joint_write.admit_joint`). Empty keeps the
    # unit single-body: the widening clause is not offered.
    fn_records: tuple[Any, ...] = ()
    # How many extra functions one reply may add to its write.
    joint_max: int = 0
    # (cargo test output) -> culprits still in it, read by
    # `crash_keep.SameCrashJudge` beside `crash_blamed`. None is "not
    # localized": a keep that wrote a crash_blamed unit whose aimed tests
    # still crash is then refused, never silently kept.
    culprits_of: Callable[[str], Collection[Any]] | None = None
    # Blame and evidence at the bodies the frames ran
    # (`crash_site.CrashSites`). The prompt's crash text is
    # `evidence_for` of this unit's container. None is the locator
    # reading: a caller with no site map, and every test that builds a
    # context without one.
    crash_sites: CrashSites | None = None


def fill_skip(rec: Stub, attempts: int) -> FillResult:
    """Early-exit result for the fill unit (skip/race/read-failure paths)."""
    return FillResult(file=rec.file, fn_name=rec.fn_name,
                      filled=False, attempts=attempts)


def _file_fingerprint(src_dir: Path, rel: str) -> str:
    import hashlib
    try:
        digest = hashlib.md5((src_dir / rel).read_bytes()).hexdigest()[:12]
    except OSError:
        digest = "unreadable"
    return f"{rel}@{digest}"


def crash_block_mark(src_dir: Path, blamed_files: list[str]) -> str:
    """The `crash_blocked` map's value: each blamed file with its content
    fingerprint at mark time. File-granular on purpose — 'the blamed fn's
    body text changed' is approximated by 'the file holding it changed',
    which re-admits strictly more often (the safe direction)."""
    return ";".join(_file_fingerprint(src_dir, rel)
                    for rel in sorted(set(blamed_files)))


def crash_block_stands(src_dir: Path, mark: str) -> bool:
    """True while every blamed file still matches its mark-time fingerprint —
    re-attempting the unit would buy the identical crash."""
    rels = [part.split("@", 1)[0] for part in mark.split(";") if part]
    return bool(rels) and mark == crash_block_mark(src_dir, rels)
