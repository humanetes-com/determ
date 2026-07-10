"""THE single fix-loop driver (P3 unification, FINAL-RESULT PLAN).

One loop engine — :func:`run_fix_loop` — owns the shape every fix loop used to
hand-roll: iterate up to a hard cap, run one attempt, and exit on
accept / dead-end / stall-budget / repeated-signature. Two *views* ride it,
because the pipeline genuinely needs both and the union loses neither:

* the **outcome view** (:func:`drive_fix_loop`): an attempt reports a
  :class:`FixOutcome` (``accepted`` / ``stop`` / ``failed`` + an error
  signature). Built for inner compile loops where the signal is structured —
  a ``stop`` is a protocol dead-end (retrying can't help) and two identical
  error signatures in a row is churn worth bailing on. The gauge is the
  lower-is-better :class:`FloorStall`.

* the **metric view** (:func:`pipeline_core.application.refine_loop.run_refine_loop`):
  an attempt reports a higher-is-better metric (coverage %, pass count,
  −error count) and the loop layers retrospective-on-stall + the
  :class:`LoopState` meta-awareness narrative. Built for outer "keep improving
  a number" loops. The gauge is :class:`LoopState` itself.

Both views are thin facades; the iterate/stall/repeated/budget policy lives
once, in :func:`run_fix_loop`. The gauge is injected (a :class:`StallGauge` —
both ``FloorStall`` and ``LoopState`` satisfy it); the engine only *reads*
``stalls`` / ``repeated`` and owns the exit arithmetic, while each view's
``attempt`` closure owns the mutation (``FloorStall.observe`` /
``LoopState.record``) and the I/O.

`check_stop` is an explicit per-call hook (#14: was a module-global wired via
`configure()`): each caller passes its graceful-stop check. The default no-op
keeps the loop import-safe and tests hermetic.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from pipeline_core.application.reconstruct.fix_roles import (
    FixCheckerAdapter,
    FixModelAdapter,
    FixPromptsAdapter,
    FixSplicerAdapter,
    FrontierBuilderAdapter,
)
from pipeline_core.application.reconstruct.pull_resolver import (
    PullSources,
    workspace_file_reader,
)
from pipeline_core.domain.fix_report import FixReport
from pipeline_core.domain.fix_session import FixSession
from pipeline_core.domain.refine import FloorStall
from pipeline_core.domain.std_api_sigs import std_api_def


def _no_stop(_context: str) -> None:
    """Default graceful-stop hook: never interrupts."""


def _noop(_attempt: int) -> None:
    """Default per-attempt hook: does nothing."""


class StallGauge(Protocol):
    """The minimal gauge :func:`run_fix_loop` reads to make its exit decisions.

    Both :class:`FloorStall` (lower-is-better count gauge) and
    :class:`LoopState` (higher-is-better metric history) satisfy it. The engine
    never mutates the gauge — the view's ``attempt`` closure does (observe /
    record) — it only reads these two fields after each attempt."""

    @property
    def stalls(self) -> int:
        """Consecutive non-improving attempts so far."""
        ...

    @property
    def repeated(self) -> bool:
        """True when the last two attempts carried the same non-None signature."""
        ...


class StepStatus(StrEnum):
    """What one attempt tells the engine to do next.

    CONTINUE  — a failed/measured attempt; the engine consults the gauge for
                stall / repeated exits, else loops.
    ACCEPTED  — success (compiles / tests pass / metric target reached); the
                metric view has already recorded this attempt before returning.
    DEAD_END  — protocol dead-end (empty response, splice rejected, race lost):
                exit now, retrying cannot help.
    """

    CONTINUE = "continue"
    ACCEPTED = "accepted"
    DEAD_END = "dead_end"


@dataclass(frozen=True, slots=True)
class LoopOutcome:
    """Why :func:`run_fix_loop` stopped, and after how many attempts."""

    attempts: int
    reason: str  # "accepted" | "dead_end" | "stalled" | "repeated" | "hard_cap" | "done"


def run_fix_loop(
    attempt: Callable[[int], StepStatus],
    gauge: StallGauge,
    *,
    hard_cap: int,
    max_stalls: int,
    before_attempt: Callable[[int], None] = _noop,
    initial_done: bool = False,
) -> LoopOutcome:
    """Run ``attempt`` until it accepts / dead-ends / the gauge stalls out /
    the gauge reports a repeated signature / the hard-cap backstop fires.

    `initial_done` short-circuits with zero attempts (the metric view's
    "already at target" case). `before_attempt(n)` runs once per attempt before
    it (graceful-stop check for the outcome view; pending-retro setup for the
    metric view). The stall / repeated checks read the gauge AFTER the attempt —
    in that order, matching the legacy per-fn/per-impl loops — so the attempt's
    own mutation (observe / record) is visible to the exit decision."""
    if initial_done:
        return LoopOutcome(0, "done")
    n = 0
    while n < hard_cap:
        n += 1
        before_attempt(n)
        status = attempt(n)
        if status is StepStatus.DEAD_END:
            return LoopOutcome(n, "dead_end")
        if status is StepStatus.ACCEPTED:
            return LoopOutcome(n, "accepted")
        if gauge.stalls >= max_stalls:
            return LoopOutcome(n, "stalled")
        if gauge.repeated:
            return LoopOutcome(n, "repeated")
    return LoopOutcome(n, "hard_cap")


@dataclass(frozen=True, slots=True)
class FixOutcome:
    """One fix-loop attempt's result, returned by a unit's attempt callable.

    kind: "accepted" — the unit accepted and finalized the fill;
          "stop"     — protocol dead-end (empty response, splice rejected,
                       race lost, LLM error): retrying cannot help;
          "failed"   — compile/test failure: the driver's gauge decides
                       whether to retry (err_sig/err_count feed FloorStall).
    """
    kind: str
    # None = "this unit has no error signature" → the driver's repeated-
    # signature exit never fires (loop C / f4: failing-test counts carry no
    # rustc sig). An EMPTY frozenset is a real signature: two consecutive
    # empty sigs (rc!=0, zero parsed diagnostics) trigger the repeated exit,
    # as the hand-rolled per-fn/per-impl loops always did.
    err_sig: frozenset[str] | None = None
    err_count: int = 0


def drive_fix_loop(
    one_attempt: Callable[[int], FixOutcome],
    *,
    hard_cap: int,
    max_stalls: int,
    stop_context: str,
    on_failure: Callable[[int, FixOutcome, FloorStall], None] | None = None,
    on_stall: Callable[[int, FloorStall], None] | None = None,
    on_repeated: Callable[[int, FloorStall], None] | None = None,
    initial_floor: int | None = None,
    check_stop: Callable[[str], None] = _no_stop,
) -> tuple[bool, int]:
    """Outcome-view facade over :func:`run_fix_loop`. Run `one_attempt` until
    accepted / dead-end / stall / repeated sig / hard cap; returns
    (accepted, attempts_used). The :class:`FloorStall` gauge is seeded with
    `initial_floor` (the metric the loop ENTERED with — a first attempt that
    fails to beat it counts as a stall, not a free baseline). `check_stop` is
    invoked once per attempt with the stop context (graceful-shutdown hook)."""
    gauge = FloorStall()
    if initial_floor is not None:
        gauge.observe(initial_floor)
    accepted = False

    def attempt(n: int) -> StepStatus:
        nonlocal accepted
        outcome = one_attempt(n)
        if outcome.kind == "accepted":
            accepted = True
            return StepStatus.ACCEPTED
        if outcome.kind == "stop":
            return StepStatus.DEAD_END
        gauge.observe(outcome.err_count, signature=outcome.err_sig)
        if on_failure is not None:
            on_failure(n, outcome, gauge)
        return StepStatus.CONTINUE

    result = run_fix_loop(
        attempt, gauge, hard_cap=hard_cap, max_stalls=max_stalls,
        before_attempt=lambda n: check_stop(f"{stop_context} attempt {n}"),
    )
    if result.reason == "stalled" and on_stall is not None:
        on_stall(result.attempts, gauge)
    if result.reason == "repeated" and on_repeated is not None:
        on_repeated(result.attempts, gauge)
    return accepted, result.attempts


@dataclass(frozen=True)
class GenerateFixLoopDeps:
    """Collaborators for the per-fn / whole-file iterative fix loop (S28).
    Composition root (pipeline.py) binds these to resident shims + pipeline_core
    natives + constants; the loop body lives here, injected for testability."""
    BUILD_TIMEOUT: Any
    FILE_FORMAT_INSTRUCTION: Any
    MODEL: Any
    settings_now: Callable[..., Any]   # getter — tests monkeypatch pipeline.SETTINGS
    SINGLE_CALL_CHAR_LIMIT: Any
    SOURCE_EXTENSIONS: Any
    ASK_CLIENT: Any
    FIX_ERR_CAP: Any
    FIX_THINKING_DIRECTIVE: Any
    REPO_SPEC_CAP_WIDE: Any
    SRC_SNIP_CAP: Any
    TASK2_VERBOSITY_GUARD: Any
    TYPES_SECTION_CAP: Any
    behavior_frontier: Any
    cargo_env: Any
    categorize_compile_error: Any
    collect_impl_blocks: Any
    compile_error_frontier: Any
    derive_check_only_cmd: Any
    emit: Any
    emit_per_file_test_overview: Any
    enumerate_fns_via_extractor: Any
    extract_tdd_frontier: Any
    filter_test_targets: Any
    fix_diag: Any
    frontier_from_diags: Any
    gotchas_for_error: Any
    gotchas_summary: Any
    is_pub_fn: Any
    locate_fn: Any
    log: Any
    parse_cmd: Any
    render_impl_block: Any
    repo_spec_section: Any
    run_cargo_check_diag: Any
    restore_frozen_signature: Any
    splice_fn_body: Any
    splice_fn_with_where: Any
    strip_ansi: Any
    tag_block: Any
    whole_tree_retrospective: Callable[..., str]
    build_whole_file_fix_prompt: Any
    compile_patterns_write: Any
    extract_files: Any
    is_compiler_output_line: Any
    run_generate_fix: Callable[..., tuple[bool, str]]
    write: Any
    # P2 pull protocol (optional — None keeps the plain-ask behavior):
    enumerate_types: Any = None          # src_dir -> list[TypeRecord] | None
    types_section_for_fix: Any = None    # FixContextAssembler.types_section_for_fix
    # Optional live-status writer (project_dir, label) — keeps the monitor's
    # 'current:' line truthful during the loop's long-running iterations.
    set_current: Any = None
    check_stop: Callable[[str], None] = _no_stop


def generate_fix_loop(
    deps: GenerateFixLoopDeps,
    project_dir: Path,
    check_cmd: str,
    source_paths: list[Path],
    existing_test_files: set[str],
    extra_context: str = "",
    model: str | None = None,
    max_fix_attempts: int = 2,
    timeout: int | None = None,
    allowed_dir_prefix: str = "",
    functional_req: str = "",
    fr_path: Path | None = None,
    check_fn: Callable[[], tuple[bool, str]] | None = None,
) -> tuple[bool, str]:
    """
    Shared iterative fix loop for any code generation step.

    check_fn, when given, REPLACES check_cmd as the verdict authority — the
    post-task3 fix loop passes the per-cluster strict runner here so a fix is
    validated under EVERY baked cluster's toolchain/env_extra. A bare
    check_cmd sees only the default cluster and instantly "passes" while a
    cfg-gated cluster is failing (the 2026-06-12 false SUCCESS).

    fr_path (functional_requirements_{module}.md) keys the Repository
    Specifications section (FR + genesis + API) injected into every fix
    prompt this loop builds — the per-fn/per-impl inner loops and the
    whole-file rewrite path.

    Pattern:
      1. Run check_cmd (compile check or test run).
      2. If it passes → return (True, output).
      3. If it fails → show LLM the error + current source files, ask it to fix
         ONLY the broken files (preserve correct ones).
      4. Write fixed files, re-run check.
      5. Repeat up to max_fix_attempts times.

    Used by:
    - TASK 0c: fix unit tests that don't compile (compile check).
    - TASK 2 inner loop: fix generated code that doesn't compile (build check).
    - Post-TASK 3: fix generated code whose tests fail at runtime (test run).

    Returns (passed, final_output).
    """
    if model is None:
        model = deps.MODEL
    if timeout is None:
        timeout = deps.BUILD_TIMEOUT
    def _log_compile_overview(output: str, attempt_n: int) -> None:
        """Log per-file error table and emit compile.file_stats event."""
        err_by_file = deps.fix_diag.errors_by_file(output)
        fstats = deps.fix_diag.file_stats(source_paths, project_dir)
        all_files = sorted({*fstats.keys(), *err_by_file.keys()})
        clean = sum(1 for f in all_files if err_by_file.get(f, 0) == 0 and f in fstats)
        total = len(all_files)
        deps.log(f"  [fix loop] File overview — {clean}/{total} clean, attempt {attempt_n}:")
        max_name = max((len(f.split('/')[-1]) for f in all_files), default=10)
        rows_data = []
        for f in sorted(all_files, key=lambda f: (-err_by_file.get(f, 0), f)):
            errs = err_by_file.get(f, 0)
            st = fstats.get(f, {})
            lines = st.get("lines", 0)
            fns = st.get("fns", 0)
            short = f.split("/")[-1]
            bar_s = "█" * min(errs, 8) if errs else "✓"
            deps.log(f"    {short.ljust(max_name)}  {str(lines).rjust(4)}L  {str(fns).rjust(3)}fn  {bar_s} {errs or 'clean'}")
            rows_data.append({"file": f, "lines": lines, "fns": fns, "errors": errs})
        deps.emit("compile.file_stats", data={
            "attempt": attempt_n, "clean": clean, "total": total, "files": rows_data,
        })
        # When the check command runs tests (t3 fix loop), publish per-file
        # test results alongside the compile stats.
        deps.emit_per_file_test_overview(
            project_dir, output, context=f"fix attempt {attempt_n}")

    env = deps.cargo_env()
    _fl_max_stalls = int(os.getenv("GENERATE_FIX_LOOP_MAX_STALLS", "3"))
    # `max_fix_attempts` retained as a kwarg for backward-compat but is no
    # longer a counter cap — it's used only as a logging hint. Real exit is
    # the floor-stall in `_improvement_loop`.
    _ = max_fix_attempts

    def _run_check() -> tuple[bool, str, int]:
        # Graceful-stop check at the top of each iter — routed through the
        # shared, thread-safe check (pipeline_core.domain.graceful_stop)
        # rather than an inline sentinel read, so every caller sees the same
        # stop signal regardless of which thread or loop first noticed it.
        deps.check_stop("fix loop")
        try:
            if check_fn is not None:
                ok, output = check_fn()
                if ok:
                    return True, output, 0
            else:
                result = subprocess.run(
                    deps.parse_cmd(check_cmd), cwd=project_dir,
                    capture_output=True, text=True, timeout=timeout, env=env,
                )
                output = result.stdout + (
                    "\n--- stderr ---\n" + result.stderr if result.stderr else ""
                )
                if result.returncode == 0:
                    return True, output, 0
        except subprocess.TimeoutExpired:
            return False, f"TIMEOUT after {timeout}s", 10**9
        # Floor metric: compile-error sigs while it won't compile, then failing
        # -test count (so the stall budget senses behavioral repair). See docstring.
        return False, output, deps.fix_diag.floor_metric(output)

    def _do_fix(output: str, history: list[dict[str, Any]], *, retro: str) -> FixReport:
        """Legacy whole-file rewrite strategy. Returns a typed FixReport (the
        composition root passes this as generate_fix's `whole_file_fix`)."""
        if output.startswith("TIMEOUT after "):
            return FixReport.aborted(
                reason="check_timeout", summary="check timed out")
        _log_compile_overview(output, len(history))
        # Build source context — read current content from disk in full.
        source_block = ""
        for p in source_paths:
            rel = str(p.relative_to(project_dir))
            if rel in existing_test_files or not p.exists():
                continue
            content = p.read_text(encoding="utf-8")
            source_block += f'\n<FILE path="{rel}">\n{content}\n</FILE>\n'

        # Repository Specifications complement: genesis rules + methodology
        # (the FR is already in the spec block; no target identifiers here, so
        # the FR-excerpt/API parts of the section stay empty by design).
        _repo_block = deps.tag_block(
            "REPOSITORY_SPECIFICATIONS",
            deps.repo_spec_section(fr_path, char_budget=deps.REPO_SPEC_CAP_WIDE))
        prompt = deps.build_whole_file_fix_prompt(
            thinking_directive=deps.FIX_THINKING_DIRECTIVE,
            file_format_instruction=deps.FILE_FORMAT_INSTRUCTION,
            verbosity_guard=deps.TASK2_VERBOSITY_GUARD,
            single_call_char_limit=deps.SINGLE_CALL_CHAR_LIMIT,
            output=output, history=history, retro=retro,
            source_block=source_block, extra_context=extra_context,
            functional_req=functional_req,
            gotchas_brief=deps.gotchas_summary(),
            gotchas_specific=deps.gotchas_for_error(output),
            repo_block=_repo_block)
        deps.log(f"  [fix loop] iter {len(history) + 1} — asking LLM to fix specific failures.")
        response = deps.ASK_CLIENT.ask(prompt, model=model)
        fixed = deps.extract_files(response)
        if not fixed:
            deps.log(f"  [fix loop] LLM produced no fixes on iter {len(history) + 1}.")
            _err_preview = output[-deps.FIX_ERR_CAP:] if len(output) > deps.FIX_ERR_CAP else output
            return FixReport.unchanged(
                summary="no_fixes_produced", error_preview=_err_preview)
        # Whole-file rewrites (legacy strategy — the model returns each broken
        # file in full and we overwrite it).
        for rel, content in fixed.items():
            if rel in existing_test_files:
                continue
            if Path(rel).suffix not in deps.SOURCE_EXTENSIONS:
                continue
            if allowed_dir_prefix and not rel.startswith(allowed_dir_prefix):
                deps.log(f"  [fix loop] REJECTED {rel}: outside allowed dir '{allowed_dir_prefix}'.")
                continue
            stripped = content.lstrip()
            if deps.is_compiler_output_line(stripped):
                deps.log(f"  [fix loop] REJECTED {rel}: content looks like compiler output, not code.")
                continue
            deps.write(project_dir / rel, content)
            deps.log(f"  [fix loop] Fixed: {rel}")

        _all_changed = sorted(fixed.keys())
        _err_preview = output[-deps.FIX_ERR_CAP:] if len(output) > deps.FIX_ERR_CAP else output

        # BL-025: log fix patterns for future prompts.
        if _all_changed:
            _bl025_module = allowed_dir_prefix.rstrip('/').split('/')[-2] if allowed_dir_prefix else ""
            if _bl025_module:
                _error_category = deps.categorize_compile_error(output)
                _fix_hint = "; ".join(_all_changed[:3])
                deps.compile_patterns_write(
                    project_dir, _bl025_module, _error_category,
                    example=output[:200],
                    fix_hint=f"fixed in {_fix_hint}",
                )
        return FixReport.changed(
            summary=f"rewrote={len(_all_changed)}",
            files_changed_str=", ".join(_all_changed[:4]) if _all_changed else "no files changed",
            error_preview=_err_preview, error_sig=_err_preview)

    def _do_retro(output: str, history: list[dict[str, Any]]) -> str:
        return deps.whole_tree_retrospective(
            error_excerpt=output, history=history, name="generate_fix_loop",
        )

    # --- shared helpers for the per_fn strategy (unit = fn | impl) ----------
    def _tests_for_names(names: set[str]) -> str:
        """Test files referencing any of `names`, budget-capped."""
        parts: list[str] = []
        used = 0
        for trel in sorted(existing_test_files):
            tp = project_dir / trel
            if not tp.exists():
                continue
            try:
                tc = tp.read_text(encoding="utf-8")
            except OSError:
                continue
            if not any(n and n in tc for n in names):
                continue
            blk = f'<TEST path="{trel}">\n{tc}\n</TEST>'
            if used + len(blk) > deps.TYPES_SECTION_CAP:
                break
            parts.append(blk); used += len(blk)
        return ("\nTests this code must satisfy:\n" + "\n".join(parts) + "\n") if parts else ""

    def _gotchas_block_for(out: str) -> str:
        gd = deps.gotchas_for_error(out)
        block = f"\n<RUST_GOTCHAS>\n{deps.gotchas_summary()}\n</RUST_GOTCHAS>\n"
        if gd:
            block += f"<RELEVANT_GOTCHAS_DETAIL>\n{gd}\n</RELEVANT_GOTCHAS_DETAIL>\n"
        return block

    # chunk H: the ISP role adapters (CompileChecker/FixModel/FixSplicer/
    # FixPrompts/FrontierBuilder) now live in adapters/fix_loop_roles.py; the
    # loop policy + prompt/splice live in application.reconstruct.{fix_unit,
    # generate_fix}. pipeline.py is the composition root — it wires the resident
    # check/model/splice/prompt/frontier helpers in via late-binding lambdas
    # (preserves the _run_cargo/_splice_fn_body/ask monkeypatch seams).
    _fix_checker = FixCheckerAdapter(
        run_cargo_check_diag=lambda c, cwd, e, t: deps.run_cargo_check_diag(c, cwd, e, t),
        strip_ansi=deps.strip_ansi,
        frontier_from_diags=lambda diags: deps.frontier_from_diags(diags, project_dir))
    _fix_model = FixModelAdapter(
        ask_fn=lambda prompt: deps.ASK_CLIENT.ask(prompt, model=model))
    _fix_splicer = FixSplicerAdapter(
        locate_fn_fn=deps.locate_fn, is_pub_fn_fn=deps.is_pub_fn,
        render_impl_block_fn=deps.render_impl_block,
        splice_fn_fn=deps.splice_fn_with_where, splice_body_fn=deps.splice_fn_body,
        restore_signature_fn=deps.restore_frozen_signature)
    _fix_prompts = FixPromptsAdapter(
        repo_spec_block_fn=lambda *, file_rel="", fn_name="", container="":
            deps.tag_block("REPOSITORY_SPECIFICATIONS", deps.repo_spec_section(
                fr_path, file_rel=file_rel, fn_name=fn_name, container=container)),
        gotchas_block_fn=_gotchas_block_for,
        tests_block_fn=lambda names: _tests_for_names(set(names)))
    def _behavior_frontier_filtered(output: str) -> tuple[Any, Any]:
        fr, srcs = deps.behavior_frontier(output, project_dir, existing_test_files)
        return deps.filter_test_targets(fr, project_dir), srcs
    _frontiers = FrontierBuilderAdapter(
        compile_frontier_fn=lambda output: deps.filter_test_targets(
            deps.compile_error_frontier(output, project_dir, check_cmd, env, timeout),
            project_dir),
        tdd_frontier_fn=lambda output: deps.filter_test_targets(
            deps.extract_tdd_frontier(output, project_dir), project_dir),
        behavior_frontier_fn=_behavior_frontier_filtered)

    _fix_session = FixSession.for_check_cmd(
        project_dir=project_dir, check_cmd=check_cmd,
        derive_check_only=deps.derive_check_only_cmd, env=env, timeout=timeout,
        model=model, fr_path=fr_path,
        existing_test_files=frozenset(existing_test_files),
        allowed_dir_prefix=allowed_dir_prefix)
    # P2 pull sources for the fix units — all post-strip-legal (HR 11):
    # the reconstruction tree, the crate's tests, the enumerated type
    # records, and (rebound per attempt) the full check output.
    def _pull_type_def(name: str) -> str:
        if deps.enumerate_types is None or deps.types_section_for_fix is None:
            return std_api_def(name)
        return str(deps.types_section_for_fix(
            name, "", "", "", "", deps.enumerate_types(project_dir) or [])
            # Crate records win; well-known unstable std APIs answer as a
            # last fallback so the pull never starves on a std type.
            or std_api_def(name))

    _pull_sources = PullSources(
        read_file=workspace_file_reader(project_dir),
        find_tests=lambda name: _tests_for_names({name}),
        type_def=_pull_type_def,
        full_error=lambda: "",   # rebound per attempt by _BaseFixUnit._ask
    )
    from pipeline_core.application.reconstruct.fix_unit import FixUnitDeps
    from pipeline_core.application.reconstruct.generate_fix import GenerateFixDeps
    _fix_unit_deps = FixUnitDeps(
        checker=_fix_checker, model=_fix_model, splicer=_fix_splicer,
        prompts=_fix_prompts, log=deps.log, do_retro=_do_retro,
        thinking_directive=deps.FIX_THINKING_DIRECTIVE, err_cap=deps.FIX_ERR_CAP,
        per_fn_max_stalls=int(os.getenv("FIX_LOOP_PER_FN_MAX_STALLS", "2")),
        pull_sources=_pull_sources, emit=deps.emit,
        max_pulls=int(os.getenv("CONTEXT_PULLS_MAX", "2")))

    _gen_fix_deps = GenerateFixDeps(
        unit=_fix_unit_deps,
        frontiers=_frontiers,
        enumerate_fns=lambda: deps.enumerate_fns_via_extractor(project_dir) or [],
        collect_impl_blocks=lambda: deps.collect_impl_blocks(project_dir) or [],
        log_overview=_log_compile_overview,
        whole_file_fix=lambda o, h, r: _do_fix(o, h, retro=r),
        behavior_max_fns=int(os.getenv("FIX_BEHAVIOR_MAX_FNS", "8")),
        src_snip_cap=deps.SRC_SNIP_CAP)

    _set_cur = deps.set_current
    _on_iter = None
    if _set_cur is not None:
        def _on_iter(ev: dict[str, Any]) -> None:
            _set_cur(project_dir,
                     f"task2-4/fix-loop iter {ev['iter']} — "
                     f"{ev['metric_new']} err(s), best {ev['metric_best']}")
    return deps.run_generate_fix(
        session=_fix_session, deps=_gen_fix_deps,
        strategy=deps.settings_now().fix_loop_strategy, unit=deps.settings_now().fix_loop_unit,
        run_check=_run_check, do_retro=_do_retro,
        max_stalls=_fl_max_stalls, log=deps.log, on_iter=_on_iter)
