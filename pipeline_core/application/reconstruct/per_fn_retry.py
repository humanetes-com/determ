"""Per-fn retry use-case — extracted from _task3_inline_retry (V1 / §7.0b).

`fix_one_ref` re-asks the model for one (file, fn) body and splices it in.
`apply_per_fn_fixes` drives per-fn isolation: one fn's exception is logged and
skipped, never aborting the improvement pass or masking the other fns.

Extracted from the closure nest so the per-fn fault-isolation behaviour is
harness-testable without monkeypatching module globals.
"""
from __future__ import annotations

import json
import os
import shlex
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from pipeline_core.domain.models import FnFixContext

from pipeline_core.application.reconstruct.attribution import (
    not_body_repairable_tests,
)
from pipeline_core.application.reconstruct.import_repair import (
    repair_missing_imports,
)
from pipeline_core.application.reconstruct.pull_resolver import (
    PullSources,
    workspace_file_reader,
)
from pipeline_core.application.reconstruct.tree_txn import (
    PreImage,
    capture,
    restore,
)
from pipeline_core.application.reconstruct.unit_conversation import TranscriptBound
from pipeline_core.application.refine_loop import run_callback_loop
from pipeline_core.domain.attribution import AttributionResult
from pipeline_core.domain.cluster_env import overlay_cluster_env
from pipeline_core.domain.cluster_plan import cfg_world_rules, merged_view_cluster
from pipeline_core.domain.last_error import resolve_kind
from pipeline_core.domain.models import FnLocator, Frontier
from pipeline_core.domain.reconstruction import SpliceOutcome
from pipeline_core.domain.retry_outcome import (
    BodyVerdict,
    ClusterReading,
    RetryMode,
    RetryVerdict,
    TreeReading,
    body_verdict,
    build_verdict,
    failing_test_verdict,
    failure_metric,
)
from pipeline_core.domain.source_snapshot import SourceSnapshot
from pipeline_core.domain.std_api_sigs import std_api_def
from pipeline_core.domain.test_cmd import build_test_cmd
from pipeline_core.domain.test_results import (
    compile_error_digest,
    parse_failed_tests,
    parse_test_counts,
    strip_ansi,
)
from pipeline_core.domain.type_refs import types_referenced_in_text
from pipeline_core.prompts.context import PerFnFixSections
from pipeline_core.prompts.sections import render_types_section


@dataclass(frozen=True, slots=True)
class RetryContext:
    """Per-run data previously closed over by _task3_inline_retry locals."""
    project_dir: Path
    src_root: Path
    cluster_label: str
    containers: dict[FnLocator, str]
    type_recs: list[Any]
    fr_path: Path | None
    tests_dir_files: list[Path]
    # Every definition a frontier key stands for: (container, sig_text) per
    # occurrence, in record order. A key is (file, bare name), and this crate
    # holds three `kind.rs::new` and four `error.rs::from` — without this the
    # loop can only ever address the first of each.
    occurrences: dict[FnLocator, tuple[tuple[str, str], ...]] = field(
        default_factory=dict)
    # What the cluster NAME in the prompt header actually means: which cfg
    # atoms are on, and which arm of a multi-variant macro is live — for every
    # cluster that compiles this tree, not just the one being repaired.
    invariants_section: str = ""
    macros_section: str = ""
    # What the per-cluster loops already learned about a fn before giving up on
    # it, keyed `file::fn`. A cluster that plateaus with failing tests is
    # checkpointed and never re-opened, so this is the only channel by which the
    # attempts it spent reach the loop that inherits the work.
    prior_effort: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RetryDeps:
    """Collaborators injected by the composition root (pipeline.py)."""
    locate_fn: Callable[[str, str], tuple[Any, ...] | None]
    # Every occurrence of a name in a file, in source order — what makes a
    # non-first definition addressable (`kind.rs` holds three `new`).
    locate_all_fns: Callable[[str, str], list[tuple[str, int, int, int]]] | None
    gather_fix_context: Callable[..., FnFixContext]
    build_per_fn_fix_prompt: Callable[..., str]
    ask_per_fn_body: Callable[..., tuple[str | None, str]]
    # (label, *, route, sources, max_pulls) -> the body's conversation
    # (`PromptContext.open_conversation`).
    open_conversation: Callable[..., Any]
    splice_fn_body: Callable[..., SpliceOutcome]
    # The collision-safe write. `splice_fn_body` locates by bare name and
    # refuses `ambiguous_name` the moment a file holds two fns of that name,
    # which on this crate is the common case, not the corner: `error.rs::from`
    # occurs 4x, `kind.rs::new` 3x, `context.rs::source` / `context.rs::context`
    # / `ptr.rs::new` 2x each. The scoped entry point re-resolves the target
    # through the syn record using the container and signature this module
    # already holds, so the body lands at the exact fn the prompt was built
    # from instead of being thrown away.
    splice_body_scoped: Callable[..., SpliceOutcome]
    find_tests_for_fn: Callable[..., list[Any]]
    focus_cargo_errors: Callable[[str, str], str]
    is_gen_tests_file: Callable[[str], bool]
    log: Callable[[str], None]
    # The splice gate's parse check (`Mutator.parse_validator()`), handed to
    # the deterministic import repair so its `use` insertion is refused —
    # not merely re-checked later — when it would leave the file
    # unparseable. None disables the refusal (fixture path).
    parse_ok: Callable[[Path], bool] | None = None


class RepairTarget(NamedTuple):
    """One definition to re-author: a frontier key plus WHICH occurrence of that
    name in that file. `occurrence=0` is the first, which is all a (file, name)
    key could ever express before."""
    file: str
    fn: str
    occurrence: int = 0


@dataclass(frozen=True, slots=True)
class ClusterProbe:
    """One cluster's test invocation. The retry measures the tree through every
    probe, not only the cluster whose failure opened it: the merged tree is one
    artifact that both clusters compile, and ten bodies were rewritten in files
    both build on 2026-08-05 while only `nightly` was ever re-run."""
    name: str
    command: str
    env: dict[str, str]


@dataclass(frozen=True, slots=True)
class BodyGate:
    """The per-body accept gate.

    One retry iteration used to be: attribute, re-ask the model for all N
    bodies, splice them all, then run `cargo test` ONCE. On 2026-08-05 that
    spent 17 model calls over 42 minutes, one of the bodies broke the build,
    and the whole batch was reverted — including the bodies that were right.
    The feedback costs ~1 s per cluster against a ~150 s model call, so it is
    bought per BODY here, and a body that regresses any cluster is reverted on
    its own while the rest of the frontier proceeds.
    """
    read_tree: Callable[[], TreeReading]
    baseline: TreeReading
    log: Callable[[str], None]


@dataclass(frozen=True, slots=True)
class Task3Cargo:
    """The cluster-scoped cargo seam (env builder + test runner + compile-only
    diagnostic runner)."""
    env: Callable[[], dict[str, str]]
    run: Callable[..., tuple[int, str]]
    check_diag: Callable[..., tuple[int, str, list[Any]]]
    # Bound to cluster_cargo_env in composition. None builds it from env()
    # through the same overlay rule.
    cluster_env: Callable[..., dict[str, str]] | None = None


def env_for_cluster(cargo: Task3Cargo, cluster: Any | None) -> dict[str, str]:
    """The cargo env for one named cluster, through the injected owner when
    present, else by the same overlay rule over the base env. A dict.update
    here dropped `-A warnings` and skipped RUSTUP_TOOLCHAIN on a live nightly
    cluster (run 216855-03b2a21b)."""
    if cargo.cluster_env is not None and cluster is not None:
        return cargo.cluster_env(cluster)
    e = cargo.env()
    overlay_cluster_env(e, cluster)
    return e


@dataclass(frozen=True, slots=True)
class Task3Attribution:
    """Failure → (file, fn) repair-target attribution (test-failure classifier,
    test reverse-map, compile-diagnostic frontier, test-oracle filter)."""
    attribute_failures: Callable[..., AttributionResult]
    build_test_reverse_map: Callable[..., dict[str, set[FnLocator]]]
    frontier_from_diags: Callable[..., Frontier]
    filter_test_targets: Callable[..., Frontier]
    # Widen an attributed frontier along callee edges, with each key's hop
    # distance from the seed. Every other path here stops at fns the failing
    # test NAMES; this is what can reach a defect one call deeper.
    expand_frontier_by_calls: Callable[..., tuple[Frontier, dict[FnLocator, int]]]


@dataclass(frozen=True, slots=True)
class Task3Enumerate:
    """Cluster-aware type/container enumeration for the per-fn fix prompts."""
    cluster_filter_type_members: Callable[..., list[Any]]
    enumerate_types: Callable[..., list[Any] | None]
    fn_container_index: Callable[..., dict[FnLocator, str]]
    fn_occurrence_index: Callable[
        ..., dict[FnLocator, tuple[tuple[str, str], ...]]]
    # The crate-level context the OTHER fill routes have always rendered and
    # this one passed as empty strings.
    collect_crate_macros: Callable[[Path], str]
    compute_cluster_macro_invariants: Callable[[Path, Any], str]
    detect_crate_no_std_invariant: Callable[[Path], str]


@dataclass(frozen=True, slots=True)
class Task3RetryDeps:
    """Composed collaborators for the task3 inline-retry orchestration. Each
    field is a narrow ISP role (cargo / attribution / enumeration / per-fn fix)
    so this is composition, not a flat god-deps."""
    cargo: Task3Cargo
    attribution: Task3Attribution
    enumerator: Task3Enumerate
    retry_fix: RetryDeps
    log: Callable[[str], None]
    test_timeout: int


def run_task3_inline_retry(
    project_dir: Path,
    test_command: str,
    iteration: Any,
    initial_test_output: str,
    cluster: Any,
    *,
    all_clusters: list[Any] | None = None,
    max_stalls: int | None = None,
    hard_cap: int | None = None,
    deps: Task3RetryDeps,
) -> tuple[bool, str]:
    """Improvement-driven retry over a failed task3. Returns (passed, final_output).
    If the retry cannot beat the initial failure floor, restores the source tree
    from snapshot and returns (False, initial_output).

    Extracted from pipeline.py's `_task3_inline_retry` glue (chunk H): all engine
    pieces (per-fn fix, attribution, verdicts, snapshot, improvement loop) are
    already in pipeline_core; this composes them. The composition root keeps a
    thin shim binding the resident collaborators (preserves the scripted-cargo /
    scripted-model monkeypatch seams).
    """
    _ = iteration  # signature-compat; not used by the retry policy
    # Env knobs read per call (tests set them via monkeypatch.setenv).
    if os.getenv("TASK3_INLINE_RETRY", "1") == "0":
        return False, initial_test_output
    if max_stalls is None:
        max_stalls = int(os.getenv("TASK3_INLINE_MAX_STALLS", "3"))
    if hard_cap is None:
        hard_cap = int(os.getenv("TASK3_INLINE_HARD_CAP", "10"))
    # Be CLUSTER-AWARE. task3 runs each cluster with its own toolchain
    # + env_extra (RUSTFLAGS/cfg); the failing cluster is threaded in so the retry
    # reproduces THAT cluster's build under THAT env — the global cargo env would
    # use whatever cluster is stale-set, so a cluster-cfg-specific compile error
    # (e.g. nightly-only) would not reproduce and BUILD mode would silently bail.
    _cluster_label = cluster.name if cluster is not None else "task3"
    # The frontier's admission filter runs under the merged view: the retry
    # rewrites one tree every cluster compiles, so a site is dropped only
    # when no baked cluster compiles it.
    _repair_cluster = merged_view_cluster(
        all_clusters or ([cluster] if cluster is not None else []))
    if cluster is not None:
        _parts = ["cargo"]
        if cluster.toolchain:
            _parts.append(f"+{cluster.toolchain}")
        _parts += ["test", "--no-fail-fast"]
        test_command = shlex.join(_parts)

    def _env() -> dict[str, str]:
        return env_for_cluster(deps.cargo, cluster)

    def _probe(c: Any) -> ClusterProbe:
        # Through the shared renderer, so the gate measures with the SAME command
        # shape task3's own per-cluster run used. No stems and no `--lib` is the
        # unscoped `cargo [+toolchain] test --no-fail-fast` that stage issues.
        return ClusterProbe(name=c.name, command=build_test_cmd([], c),
                            env=env_for_cluster(deps.cargo, c))

    # Every cluster that compiles the files this pass may touch. The primary
    # stays the repair signal (its failures aim the frontier); the others are
    # here so the accept gate can see a body that fixes one cfg world and
    # breaks another. Falls back to the primary alone when the caller has no
    # cluster list — the old single-cluster behaviour, explicitly.
    _probes: tuple[ClusterProbe, ...] = tuple(
        _probe(c) for c in (all_clusters or ([cluster] if cluster is not None else []))
    )
    # The failures this loop is allowed to count as its own. A trybuild harness
    # is excluded from the frontier, so counting it here too would leave the
    # gate's "every cluster passes" condition permanently false on any crate that
    # has one — `nightly` fails `ui` on every sweep of this crate — and the
    # early stop that justifies ordering the frontier nearest-hop-first could
    # never fire. Comparisons are unaffected either way: a constant cancels.
    _unrepairable_all = not_body_repairable_tests(
        list((project_dir / "tests").rglob("*.rs")))

    def _read_tree() -> TreeReading:
        """One sweep of every cluster: ran-state and failing-test count each.

        A cluster that produced no verdicts additionally carries its DISTINCT
        compile-diagnostic count, read from the same captured output — the
        probe already ran cargo under that cluster's env and a broken lib's
        diagnostics are in that output, so no second cargo invocation is spent.
        The dedup in `compile_error_digest` is load-bearing: a cluster-wide
        break reprints one lib error per test binary, so a raw occurrence
        count moves with binary coverage while the distinct set is stable —
        and equal-vs-worse is exactly what the accept gate compares."""
        readings: list[ClusterReading] = []
        for p in _probes:
            _rc, out = deps.cargo.run(p.command, project_dir, p.env,
                                      timeout=deps.test_timeout)
            p_n, f_n, _ign = parse_test_counts(out)
            _fails = {t for t in parse_failed_tests(out)
                      if t.split("::")[-1] not in _unrepairable_all}
            _ran = (p_n + f_n) > 0
            _diags = 0 if _ran else compile_error_digest(strip_ansi(out))[0]
            readings.append(ClusterReading(
                cluster=p.name, ran=_ran, failures=len(_fails),
                diag_count=_diags))
        return TreeReading(per_cluster=tuple(readings))

    # Two modes. TEST mode (some test FAILED) attributes via the test
    # reverse-map/classifier. BUILD mode (zero FAILED lines because the tree did
    # not COMPILE) attributes via structured cargo diagnostics and fixes the fns
    # the compile errors point at — TASK 4 (a test-failure analyzer) cannot, since
    # it has no failing-test identifiers. compile_cmd compiles the tests without
    # running them so a 0-test "incomplete" cluster yields real error diagnostics.
    failed = sorted(parse_failed_tests(initial_test_output))
    compile_cmd = test_command if "--no-run" in test_command else test_command + " --no-run"
    mode = RetryMode.BUILD if not failed else RetryMode.TEST
    build_mode = mode is RetryMode.BUILD
    if build_mode:
        _, _, _init_diags = deps.cargo.check_diag(
            compile_cmd, project_dir, _env(), timeout=deps.test_timeout)
        if not _init_diags:
            # No FAILED tests AND no reproducible compile errors under this env
            # (e.g. the failure is cluster-cfg-specific) — nothing to act on here.
            return False, initial_test_output
        initial_count = len(_init_diags)
        deps.log(f"  [task3-retry] build-failure mode: {initial_count} compile error(s).")
    else:
        initial_count = len(failed)
        deps.log(f"  [task3-retry] entering inline retry: {initial_count} failing test(s).")

    # Snapshot every .rs under src/ (the files a retry might touch; tests/ is
    # excluded by the root → cheap) so a regressing retry can be reverted.
    src_root = project_dir / "src"
    snapshot = SourceSnapshot.capture(src_root)

    # Fix prompts here previously carried no TYPES_IN_SCOPE and
    # no container — the model had to guess struct fields. Enumerate from the
    # current (reconstructed) src; cluster-filter so only methods that compile
    # under THIS cluster are advertised.
    _retry_type_recs = deps.enumerator.cluster_filter_type_members(
        deps.enumerator.enumerate_types(src_root) or [], cluster)
    _retry_containers = deps.enumerator.fn_container_index(src_root)
    _retry_occurrences = deps.enumerator.fn_occurrence_index(src_root)

    # Say what the cluster NAME means. The prompt has always opened
    # `Crate: … | Cluster: nightly | File: … | Fn: …` and then carried no
    # CLUSTER_INVARIANTS block at all, so the model was told a word and left to
    # guess which cfg atoms it implies; a per-impl trace on this crate reasoned
    # "from the CLUSTER_INVARIANTS, `error_generic_member_access` is active"
    # while a route without the section reasoned "I assume it does".
    #
    # And it is not one cfg world here. The tree task3 repairs is ONE artifact
    # that every baked cluster compiles, which is why task3 runs it twice; a
    # body that satisfies the cluster whose failure opened the retry and breaks
    # the other is the regression the accept gate exists to refuse. So every
    # cluster's invariants are rendered, each under its own name, rather than
    # picking one and calling it "the" cluster.
    _inv_parts: list[str] = []
    _no_std = deps.enumerator.detect_crate_no_std_invariant(project_dir)
    if _no_std:
        _inv_parts.append(_no_std)
    _ctx_clusters = all_clusters or ([cluster] if cluster is not None else [])
    if len(_ctx_clusters) > 1:
        _inv_parts.append(
            "This file is compiled by EVERY cluster below. A body must hold "
            "under all of them: "
            + ", ".join(sorted(c.name for c in _ctx_clusters)) + ".")
    # ...and what those names MEAN, as cfg atoms. The names alone left the
    # model inferring which predicates are active; the recorded burn is an
    # unguarded read of a `#[cfg(all(not(error_generic_member_access),
    # feature = "std"))]` field — green in the default world, E0609 under the
    # nightly cluster's RUSTFLAGS, re-spliced identically for three rounds
    # with the mismatch never named (run `3579062-476abc3b`, 2026-08-19).
    _world_rules = cfg_world_rules(_ctx_clusters)
    if _world_rules:
        _inv_parts.append(_world_rules)
    for c in sorted(_ctx_clusters, key=lambda c: c.name):
        _macro_inv = deps.enumerator.compute_cluster_macro_invariants(
            src_root, c)
        if _macro_inv:
            _inv_parts.append(f"[cluster: {c.name}]\n{_macro_inv}")
    _retry_invariants = "\n\n".join(_inv_parts)
    _retry_macros = deps.enumerator.collect_crate_macros(src_root)
    _retry_prior = _prior_effort_notes(project_dir)
    # Model-notes recall + relevant tests: resolve the module FR doc when
    # unambiguous (single-module case: exactly one functional_requirements_*.md;
    # multi-module would need a per-file module map — left as today's None).
    _fr_candidates = sorted(project_dir.glob("functional_requirements_*.md"))
    _retry_fr_path = _fr_candidates[0] if len(_fr_candidates) == 1 else None
    _retry_tests_dir_files = list((project_dir / "tests").rglob("*.rs"))

    _retry_ctx = RetryContext(
        project_dir=project_dir,
        src_root=src_root,
        cluster_label=_cluster_label,
        containers=_retry_containers,
        type_recs=_retry_type_recs,
        fr_path=_retry_fr_path,
        tests_dir_files=_retry_tests_dir_files,
        occurrences=_retry_occurrences,
        invariants_section=_retry_invariants,
        macros_section=_retry_macros,
        prior_effort=_retry_prior,
    )

    def _run_check_test() -> tuple[bool, str, int]:
        rc, out = deps.cargo.run(test_command, project_dir, _env(),
                                 timeout=deps.test_timeout)
        # Ran-state first. A tree that compiles nothing prints no `FAILED`
        # lines, so a raw failure count reads 0 — the best score possible — and
        # the loop's ratchet records a floor no working tree can beat. A tree
        # that produced no verdicts is ordered by its distinct compile-error
        # count on top of the sentinel, so a broken tree gaining errors reads
        # worse instead of identical.
        p_n, f_n, _ign = parse_test_counts(out)
        _ran = (p_n + f_n) > 0
        return rc == 0, out, failure_metric(
            ran=_ran, failures=len(parse_failed_tests(out)),
            diag_count=0 if _ran else compile_error_digest(strip_ansi(out))[0])

    def _run_check_build() -> tuple[bool, str, int]:
        rc, rendered, diags = deps.cargo.check_diag(
            compile_cmd, project_dir, _env(), timeout=deps.test_timeout)
        # ok = the tree COMPILES; metric = remaining compile errors (lower better).
        return rc == 0, strip_ansi(rendered), len(diags)

    def _do_fix_test(output: str, history: list[dict[str, Any]]) -> dict[str, Any]:
        # Full attribution chain. The classifier is one step in a
        # longer chain — when it returns no actionable refs, fall back to
        # the test-reverse-map. Previously this gave up at classifier
        # exhaustion; now it has more candidates to try.
        fails_now = sorted(parse_failed_tests(output))
        if not fails_now:
            # Distinguish the two silences: every test passing, versus a tree
            # that produced no verdicts at all. Both print zero FAILED lines and
            # the second one used to be logged as `no_failed_tests`, which reads
            # like success in the log of a run that had just broken the build.
            _p, _f, _i = parse_test_counts(output)
            return {"abort": True,
                    "reason": "no_failed_tests" if (_p + _f) > 0
                    else "tree_produced_no_test_verdicts"}
        tests_dir_files = list((project_dir / "tests").rglob("*.rs"))
        try:
            test_reverse_map = deps.attribution.build_test_reverse_map(
                tests_dir_files, project_dir)
        except Exception:
            test_reverse_map = {}
        # The failing tests' OWN sources. Without these the deterministic
        # behavioral path is skipped and attribution falls through to a token
        # match over the whole test FILE: on 2026-08-05 that gave `test_boxed_str`
        # a frontier of 16 (file, fn) pairs from 6 tokens — five of them `new`
        # fns in unrelated files — while missing `construct_from_boxed`, the fn
        # that actually holds the defect.
        _texts: dict[str, str] = {}
        for tp in tests_dir_files:
            try:
                _texts[tp.relative_to(project_dir).as_posix()] = tp.read_text(
                    encoding="utf-8", errors="replace")
            except Exception:
                continue
        _unrepairable = _unrepairable_all
        _blocked = sorted(t for t in fails_now
                          if t.split("::")[-1] in _unrepairable)
        if _blocked:
            deps.log(f"  [task3-retry] not body-repairable, excluded from the "
                     f"frontier: {', '.join(_blocked)} (compile-fail harness — "
                     "its assertion is about rustc's own output).")
        frontier = deps.attribution.filter_test_targets(
            deps.attribution.attribute_failures(
                output, project_dir,
                recently_filled=[],  # task3 has no per-cluster fill history
                test_reverse_map=test_reverse_map,
                cluster_name=_cluster_label,
                test_texts=_texts,
                unrepairable_tests=_unrepairable,
            ).frontier, project_dir, cluster=_repair_cluster)
        # Then follow callee edges, so a defect the failing test does not name
        # is reachable at all. Three hops is what this crate needs and where it
        # saturates: replayed against the merged tree and the 2026-08-05 output,
        # `test_boxed_str` seeds 3 fns, and `error.rs::construct_from_boxed` —
        # the fn that holds the defect — enters at hop 3 in a frontier of 37
        # (hop 4 adds 2 more and finds nothing new). Off with
        # TASK3_RETRY_CALL_HOPS=0.
        _hops_max = int(os.getenv("TASK3_RETRY_CALL_HOPS", "3"))
        _cap = int(os.getenv("TASK3_RETRY_FRONTIER_CAP", "40"))
        hops: dict[FnLocator, int] = dict.fromkeys(frontier, 0)
        if _hops_max > 0 and frontier:
            widened, hops = deps.attribution.expand_frontier_by_calls(
                frontier, project_dir, max_hops=_hops_max, cap=_cap)
            widened = deps.attribution.filter_test_targets(
                widened, project_dir, cluster=_repair_cluster)
            if len(widened) > len(frontier):
                deps.log(f"  [task3-retry] frontier {len(frontier)} → "
                         f"{len(widened)} fn(s) over {_hops_max} call hop(s).")
            frontier = widened
        # Nearest first, then most-implicated: the gate can stop the frontier
        # the moment every cluster passes, so the order decides what a success
        # costs. Ties break on the name to keep the order reproducible.
        keys: list[FnLocator] = sorted(
            frontier.keys(),
            key=lambda k: (hops.get(k, 0), -len(frontier.get(k) or ()), k[0], k[1]))
        # One target per DEFINITION, not per name. First occurrences first —
        # they are what every previous run could address, so the cheap,
        # already-proven candidates are still attempted before the rest.
        refs: list[RepairTarget] = [RepairTarget(f, fn, 0) for f, fn in keys]
        _extra: list[RepairTarget] = []
        for f, fn in keys:
            _src_rel = f[4:] if f.startswith("src/") else f
            n_occ = len(_retry_occurrences.get(FnLocator(_src_rel, fn), ()))
            _extra += [RepairTarget(f, fn, i) for i in range(1, n_occ)]
        if _extra:
            deps.log(f"  [task3-retry] {len(_extra)} further definition(s) of "
                     f"{len({(t.file, t.fn) for t in _extra})} same-named fn(s) "
                     "are addressable this pass.")
        refs += _extra
        if not refs:
            return {"abort": True, "reason": "no_actionable_refs"}
        fixed_count = apply_per_fn_fixes(refs, output, _retry_ctx, deps.retry_fix,
                                         gate=_body_gate())
        return {
            "summary": f"fixed_{fixed_count}/{len(refs)}",
            "fns_touched": refs,
            "abort": fixed_count == 0,
            "reason": "no_fns_spliced" if fixed_count == 0 else "",
        }

    def _body_gate() -> BodyGate | None:
        """The accept gate for one frontier pass, or None when there is nothing
        to measure through (no cluster was threaded in)."""
        if not _probes:
            return None
        return BodyGate(read_tree=_read_tree, baseline=_read_tree(),
                        log=deps.log)

    def _do_fix_build(output: str, history: list[dict[str, Any]]) -> dict[str, Any]:
        _, _, diags = deps.cargo.check_diag(
            compile_cmd, project_dir, _env(), timeout=deps.test_timeout)
        if not diags:
            return {"abort": True, "reason": "no_compile_errors"}
        # Deterministic import repair first: a rustc single-candidate
        # "consider importing" suggestion needs no LLM attempt — the fill
        # paths already do this, and an E0425-class failure reaching task3
        # burned model calls here before.
        for _rel in sorted({f.file for d in diags for f in d.import_fixes}):
            _rep = repair_missing_imports(
                lambda cmd, cwd, env: deps.cargo.check_diag(
                    cmd, cwd, env, timeout=deps.test_timeout),
                compile_cmd, project_dir, _env(), _rel, diags,
                deps.log, "[task3-retry]",
                validate_parse=deps.retry_fix.parse_ok)
            if _rep is not None:
                _, _, diags = _rep
                if not diags:
                    return {"summary": "auto_import_repair", "abort": False}
        frontier = deps.attribution.filter_test_targets(
            deps.attribution.frontier_from_diags(diags, project_dir), project_dir,
            cluster=_repair_cluster)
        refs: list[FnLocator] = list(frontier.keys())
        if not refs:
            return {"abort": True, "reason": "unattributed_compile_errors"}
        fixed_count = apply_per_fn_fixes(refs, output, _retry_ctx, deps.retry_fix)
        return {
            "summary": f"fixed_{fixed_count}/{len(refs)}",
            "fns_touched": refs,
            "abort": fixed_count == 0,
            "reason": "no_fns_spliced" if fixed_count == 0 else "",
        }

    passed, last_out, history = run_callback_loop(
        name="task3.inline_retry",
        run_check=_run_check_build if build_mode else _run_check_test,
        do_fix=_do_fix_build if build_mode else _do_fix_test,
        max_stalls=max_stalls,
        hard_cap=hard_cap,
    )

    if build_mode:
        # Re-measure compile errors. Revert if the retry INCREASED them; recurse
        # (changed output) only on strict progress so task3's re-verification
        # terminates — fewer errors each round, or back to the unchanged input.
        _, _, fin_diags = deps.cargo.check_diag(
            compile_cmd, project_dir, _env(), timeout=deps.test_timeout)
        final_err = len(fin_diags)
        verdict = build_verdict(initial_errors=initial_count,
                                final_errors=final_err, has_snapshot=bool(snapshot))
        if verdict is RetryVerdict.REVERT:
            deps.log(f"  [task3-retry] build-fix worsened compile ({final_err} > "
                     f"{initial_count}) — reverting from snapshot.")
            snapshot.restore()
            return False, initial_test_output
        if verdict is RetryVerdict.RECURSE:
            deps.log(f"  [task3-retry] build-fix progress: {final_err} compile "
                     f"error(s) (initial {initial_count}) — re-running TASK 3.")
            return False, last_out
        deps.log(f"  [task3-retry] build-fix made no progress ({final_err} error(s)) "
                 "— escalating to TASK 4.")
        return False, initial_test_output

    final_failed = parse_failed_tests(last_out)
    if passed:
        deps.log(f"  [task3-retry] tests now PASS after {len(history)} iter(s).")
        return True, last_out
    # Decide whether to keep partial progress or revert. A non-compiling tree
    # emits NO `FAILED` lines, so `len(final_failed)` reads as 0 ("near success") even
    # when a re-splice broke the build. Compile/ran-state is the real signal — if the
    # tree ran tests before the retry but no longer does, the retry made it STRICTLY
    # WORSE (a tree that won't compile is worse than N failing tests) → revert.
    init_p, init_f, _ = parse_test_counts(initial_test_output)
    fin_p, fin_f, _ = parse_test_counts(last_out)
    init_ran = (init_p + init_f) > 0
    final_ran = (fin_p + fin_f) > 0
    verdict = failing_test_verdict(
        passed=False, initial_failures=initial_count,
        final_failures=len(final_failed), initially_ran=init_ran,
        finally_ran=final_ran, has_snapshot=bool(snapshot))
    if verdict is RetryVerdict.REVERT:
        reason = ("retry broke the build (tests no longer run)"
                  if (init_ran and not final_ran)
                  else f"result {len(final_failed)} > initial {initial_count}")
        deps.log(f"  [task3-retry] {reason} — reverting from snapshot.")
        snapshot.restore()
        return False, initial_test_output
    deps.log(f"  [task3-retry] stopped at {len(final_failed)} failing tests "
             f"(initial {initial_count}). Keeping partial progress.")
    return False, last_out


class BodyDraft(NamedTuple):
    """A model answer for one definition, not yet on disk.

    Generation is the expensive half (~2.5 min per call, measured over 17 calls
    on 2026-08-05) and it touches nothing; the splice and its accept gate are
    cheap and must stay serial, because a body's verdict is only meaningful
    against a tree with the previous body already in it. Splitting them is what
    lets the calls overlap without letting the measurements overlap.
    """
    file: str
    fn: str
    container: str
    sig_text: str
    body: str


def splice_draft(draft: BodyDraft, ctx: RetryContext, deps: RetryDeps) -> bool:
    """Write a generated body through the container-scoped splice."""
    fp = ctx.project_dir / draft.file
    splice_out = deps.splice_body_scoped(fp, draft.fn, draft.container,
                                         draft.sig_text, draft.body,
                                         allow_nonstub=True)
    if splice_out.ok:
        deps.log(f"  [task3-retry] re-spliced {draft.file}::{draft.fn}.")
        return True
    deps.log(f"  [task3-retry] splice refused for {draft.file}::{draft.fn}: "
             f"{splice_out.reason}.")
    return False


def fix_one_ref(
    rel_f: str, fn: str, output: str, ctx: RetryContext, deps: RetryDeps,
    *, occurrence: int = 0,
) -> bool:
    """Re-ask the model for one (file, fn) body and splice it; True if spliced.

    `occurrence` selects WHICH definition of that name in that file — 0 is the
    first, which is what every caller meant before a frontier key could stand
    for more than one.

    Frozen tests/gen_tests files: return False (test oracle is immutable).
    Benign no-ops (missing file, fn not located, empty model body): False.
    Transport/model/splice failures: RAISE — the caller isolates per-fn.
    """
    draft = generate_one_body(rel_f, fn, output, ctx, deps,
                              occurrence=occurrence)
    return False if draft is None else splice_draft(draft, ctx, deps)


def generate_one_body(
    rel_f: str, fn: str, output: str, ctx: RetryContext, deps: RetryDeps,
    *, occurrence: int = 0,
) -> BodyDraft | None:
    """Build the prompt for one definition and ask the model. Writes nothing —
    split from the splice so the calls can overlap while the writes stay
    serial."""
    if rel_f.startswith("tests/") or deps.is_gen_tests_file(Path(rel_f).name):
        return None
    fp = ctx.project_dir / rel_f
    if not fp.exists():
        return None
    try:
        cur_text = fp.read_text(encoding="utf-8")
    except Exception:
        return None
    err_excerpt = deps.focus_cargo_errors(output, rel_f)
    _src_rel = rel_f[4:] if rel_f.startswith("src/") else rel_f
    _occ = ctx.occurrences.get(FnLocator(_src_rel, fn), ())
    if occurrence:
        # A non-first definition. Its container comes from the record index and
        # its sig/body from the matching occurrence in the file, paired by
        # SIGNATURE rather than by position so a record order and a text order
        # that disagree cannot show the model one definition and write another.
        if occurrence >= len(_occ):
            # REFUSE rather than fall through. The `else` branch below targets
            # the FIRST definition, so a caller asking for the Nth would have
            # been silently answered about a different fn and had the body
            # written there — the exact confusion this parameter exists to end.
            deps.log(f"  [task3-retry] {rel_f}::{fn} occurrence {occurrence} is "
                     f"not in the record index ({len(_occ)} known) — skipped.")
            return None
        container, rec_sig = _occ[occurrence]
        located = deps.locate_all_fns(cur_text, fn) if deps.locate_all_fns else []
        sloc = _pick_occurrence(located, rec_sig, occurrence)
        if sloc is None:
            return None
    else:
        sloc = deps.locate_fn(cur_text, fn)
        if sloc is None:
            return None
        # One container for both the prompt and the splice. `locate_fn` returns
        # the FIRST fn of this name and `fn_container_index` records the first
        # record's container, so the two agree: the body the model is shown and
        # the byte range it is written to are the same definition.
        container = ctx.containers.get(FnLocator(_src_rel, fn), "")
    sig_t = sloc[0]
    cur_body = cur_text[sloc[2]:sloc[3]]
    fix_ctx = deps.gather_fix_context(
        file_text=cur_text, file_rel=rel_f, fn_name=fn,
        sig_text=sig_t, body_text=cur_body,
        error_excerpt=err_excerpt,
        container=container,
        type_records=ctx.type_recs,
        fr_path=ctx.fr_path,
    )
    t_blocks = deps.find_tests_for_fn(
        ctx.tests_dir_files, fn, file_rel=rel_f,
        src_dir=ctx.src_root, container=fix_ctx.container,
    )
    prompt = deps.build_per_fn_fix_prompt(
        file_text=cur_text, fn_name=fn, sig_text=sig_t,
        test_blocks=t_blocks, module_name=ctx.project_dir.name,
        cluster_name=ctx.cluster_label, file_rel=rel_f,
        prev_body=cur_body, error_excerpt=err_excerpt,
        sections=PerFnFixSections(
            container=fix_ctx.container, types=fix_ctx.types_section,
            notes=fix_ctx.notes_section,
            macros=ctx.macros_section, invariants=ctx.invariants_section,
            retrospective_guidance=ctx.prior_effort.get(f"{_src_rel}::{fn}", ""),
            siblings="", crate_idioms="",
            bound_methods=fix_ctx.bound_methods_section,
            blueprint=fix_ctx.blueprint_section,
        ),
        fr_path=ctx.fr_path,
        kind=resolve_kind(None, err_excerpt),
    )
    # Context requests are answered on the body's conversation (the request
    # turn stays above its answer).
    _max_pulls = int(os.getenv("CONTEXT_PULLS_MAX", "2"))
    _sources: PullSources | None = None
    if os.getenv("CONTEXT_PULLS", "1") != "0":
        _sources = PullSources(
            read_file=workspace_file_reader(ctx.project_dir),
            find_tests=lambda name: "\n\n".join(
                f"// {p}\n{t}" for p, t in deps.find_tests_for_fn(
                    ctx.tests_dir_files, name)),
            type_def=lambda name: (render_types_section(
                types_referenced_in_text(
                    name, ctx.type_recs, visible_from=rel_f)[:3],
                viewer_file=rel_f)
                # Crate-local records win; well-known unstable std APIs
                # (std::error::Request et al.) answer as a last fallback so
                # the pull never starves on a std type the manifest can't have.
                or std_api_def(name)),
            full_error=lambda: err_excerpt,
        )
    _conv = deps.open_conversation(f"{rel_f}::{fn}", route="task3-per-fn",
                                   sources=_sources, max_pulls=_max_pulls)
    try:
        new_body, _ = deps.ask_per_fn_body(prompt, fn, [], conversation=_conv,
                                           sig_text=sig_t)
    except TranscriptBound:
        return None
    if not new_body:
        return None
    return BodyDraft(file=rel_f, fn=fn, container=container, sig_text=sig_t,
                     body=new_body)


def _prior_effort_notes(project_dir: Path) -> dict[str, str]:
    """`file::fn` → what a per-cluster loop already tried on it and gave up.

    Read from the cluster-evidence store the task2a driver writes when a cluster
    finishes. A cluster that plateaus with failing tests is checkpointed with the
    same marker as one that passed and is never re-opened, so its attempts,
    reverted bodies and spent budgets stay behind at that boundary — `nightly`
    was retired 2026-08-04 at 100/104 with four tests still failing, and the loop
    that inherited two of them started from a bare failing-test name. This is the
    channel that carries the fact across.

    Keys are SRC-RELATIVE (`error.rs::from`) — the form this module's fn indexes
    use, since they enumerate over `project_dir / "src"`. The writer normalises;
    phase-c's own frontier keys are crate-root-relative (`src/error.rs::from`)
    and a store holding those made every lookup here miss without a trace.
    """
    raw = project_dir / ".skeleton" / "_cluster_evidence.json"
    try:
        store = json.loads(raw.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(store, dict):
        return {}
    out: dict[str, str] = {}
    for cluster, ev in store.items():
        if not isinstance(ev, dict) or ev.get("exit") != "exhausted":
            continue
        failed = ", ".join(sorted(ev.get("failed") or [])[:4])
        for ref in ev.get("exhausted_fns") or []:
            note = (f"Cluster '{cluster}' re-authored this fn while "
                    f"{failed or 'its tests'} were failing and could not make "
                    "them pass — its per-fn budget for it is spent. Treat a body "
                    "shaped like the one already here as tried: the defect may "
                    "be in what this fn CALLS rather than in this fn.")
            out[str(ref)] = (out.get(str(ref), "") + "\n" + note).strip()
    return out


def _pick_occurrence(
    located: list[tuple[str, int, int, int]], rec_sig: str, index: int,
) -> tuple[str, int, int, int] | None:
    """The text occurrence matching a record's signature, or the positional one.

    Signature first because the two orders are produced by different readers
    (the syn extractor walks the AST, `locate_all_fns` scans the text) and a
    nested definition could make them disagree. A signature that matches exactly
    one occurrence settles it; otherwise fall back to position, which is what
    the two readers agree on for every ordinary file.
    """
    if not located:
        return None
    if rec_sig:
        norm = " ".join(rec_sig.split())
        hits = [loc for loc in located if " ".join(loc[0].split()) == norm]
        if len(hits) == 1:
            return hits[0]
    return located[index] if index < len(located) else None


def apply_per_fn_fixes(
    refs: list[Any], output: str, ctx: RetryContext, deps: RetryDeps,
    *, gate: BodyGate | None = None,
) -> int:
    """Re-ask + splice each (file, fn). One fn's exception is logged and
    skipped — never aborts the pass, never masks the other fixable frontier fns.

    With a `gate`, each body is measured the moment it lands and reverted on its
    own if it regresses any cluster, so one bad body costs one body instead of
    the whole batch. Without one the bodies are spliced unmeasured and the
    caller judges the batch — the pre-2026-08-05 behaviour, kept only for
    callers that have no cluster to measure through.
    """
    fixed_count = 0
    baseline = gate.baseline if gate else None
    # A bare (file, fn) key means the first occurrence — every caller that
    # predates same-name targeting passes one of those.
    targets = [RepairTarget(*r) for r in refs]
    for _ref, _draft in _drafts_in_windows(targets, output, ctx, deps):
        rel_f, fn, _occurrence = _ref
        fp = ctx.project_dir / rel_f
        # The pre-image the gate's revert restores — bytes, through the
        # transaction primitive, so the revert cannot differ from what was
        # there by a newline translation.
        before: PreImage | None = None
        if gate is not None and fp.exists():
            try:
                before = capture(fp, None)
            except Exception:
                before = None
        try:
            if _draft is None or not splice_draft(_draft, ctx, deps):
                continue
            if gate is None or baseline is None:
                fixed_count += 1
                continue
            after = gate.read_tree()
            verdict = body_verdict(before=baseline, after=after)
            if verdict is BodyVerdict.KEEP:
                baseline = after
                fixed_count += 1
                if after.ran and after.failures == 0:
                    # Nothing left to repair — the remaining frontier entries
                    # would each cost a model call against a passing tree.
                    gate.log("  [task3-retry] all clusters pass — stopping the "
                             f"frontier after {rel_f}::{fn}.")
                    break
                continue
            if before is not None:
                restore(before)
            gate.log(f"  [task3-retry] reverted {rel_f}::{fn}: {verdict} "
                     f"({_reading_summary(baseline)} → {_reading_summary(after)}).")
        except Exception as e:
            deps.log(
                f"  [task3-retry] fn {rel_f}::{fn} skipped: {type(e).__name__}: {e}"
            )
    return fixed_count


def _drafts_in_windows(
    targets: list[RepairTarget], output: str, ctx: RetryContext,
    deps: RetryDeps,
) -> Iterator[tuple[RepairTarget, BodyDraft | None]]:
    """Yield (target, draft) with the model calls overlapped, the writes not.

    The retry ran every call on one thread while the fill phases ran four-way
    parallel against the same vLLM server — the run's own flow audit says so:
    `C10 parallel-streams PASS 653 llm event(s), max 4 concurrent` during
    task2c, `SKIP no concurrent llm calls observed` during the task3 retry, one
    `tid` on every request. At ~2.5 minutes per call a 15-target frontier is 37
    minutes sequential and roughly 10 at four.

    Generation happens a WINDOW at a time rather than all at once, because the
    accept gate can stop the frontier as soon as every cluster passes; a window
    bounds what an early stop throws away to the calls already in flight. One
    target's exception is its own — it yields no draft and the rest proceed.
    """
    width = max(1, int(os.getenv("TASK3_RETRY_PARALLELISM", "4")))
    if width == 1 or len(targets) == 1:
        for t in targets:
            yield t, _draft_or_none(t, output, ctx, deps)
        return
    for start in range(0, len(targets), width):
        window = targets[start:start + width]
        with ThreadPoolExecutor(max_workers=min(width, len(window)),
                                thread_name_prefix="task3-retry") as pool:
            drafts = list(pool.map(
                lambda t: _draft_or_none(t, output, ctx, deps), window))
        yield from zip(window, drafts, strict=True)


def _draft_or_none(
    target: RepairTarget, output: str, ctx: RetryContext, deps: RetryDeps,
) -> BodyDraft | None:
    """Per-fn isolation for the generation half: a transport/model failure on one
    target is logged and skipped, never aborting the pass."""
    try:
        return generate_one_body(target.file, target.fn, output, ctx, deps,
                                 occurrence=target.occurrence)
    except Exception as e:
        deps.log(f"  [task3-retry] fn {target.file}::{target.fn} skipped: "
                 f"{type(e).__name__}: {e}")
        return None


def _reading_summary(r: TreeReading) -> str:
    """`nightly 2 fail, stable 1 fail` — or `nightly NO VERDICTS (2 err)`.

    The diagnostic count is printed with the NO VERDICTS state because it is
    now what the accept gate compares between two broken readings — a revert
    logged on it is unreadable if the log renders both sides identically."""
    return ", ".join(
        f"{c.cluster} {c.failures} fail" if c.ran
        else f"{c.cluster} NO VERDICTS ({c.diag_count} err)"
        for c in r.per_cluster) or "no clusters"
