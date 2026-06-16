# Code Architecture

Target *code* structure for the two codebases (the existing `ARCHITECTURE.md` covers the
*experiment/methodology* — different concern, untouched).

---

## Decomposition doctrine (how every refactor MUST proceed)

The goal is NOT to "move" code. Relocating a monolith behind one fat `*Deps`
object is relocation, not decomposition — it fails SRP/ISP/DIP and just shifts a
god-function across a file boundary. Build a Pythonic, high-quality product to
the highest engineering principles — SOLID, with CLEAN ARCHITECTURE as the
priority. Concretely:

| Rule | Apply |
|---|---|
| SRP | Find the DISTINCT responsibilities in the blob; each → its own unit (one reason to change). |
| Clean Arch | Dependencies point inward: `domain ← application ← adapters`. Pure logic + value objects → `domain/` (no I/O, fully unit-testable). Use-cases → `application/`. |
| DIP | Use-cases depend on ABSTRACTIONS — `typing.Protocol` ports in `pipeline_core/ports/`. `pipeline.py` concretions satisfy them structurally at the composition root. |
| ISP | ROLE-BASED ports (Telemetry, WorkspaceGit, Task1Port, …) composed per use-case — never one 40-method blob. |
| State | Thread loop/iteration state through an IMMUTABLE value object, not loose locals. |
| Fidelity | Behaviour stays verbatim-faithful; surface latent bugs found in passing (e.g. dead `regression`) as `technical_debt.md` items, do not silently change behaviour. |
| Validation | Land one slice at a time; static gates per slice + the microcrate watch loop validates the run path. Maintain the coverage floor by testing each new unit. |

This doctrine governs P5 and all future god-file breakups.

---

## 0. FINAL-RESULT PLAN (approved 2026-06-12) — supersedes the slice list below as the roadmap

End state: `pipeline.py` < ~800 lines (argparse + composition root only); everything else in
`pipeline_core/` per the §1 layers; idiomatic Python (no `_`-prefixed public API, typed seams,
one fix-loop driver); Rust already library-grade (R5/R12). Earlier slices/R-groups remain as
the detailed reference; this table is the execution order. Standing gates: check.sh green
per step; engine phases only at run boundaries, validated by the L3 microcrate strict smoke
(+ one anyhow L4 diff per engine phase). Commit granularity is the user's call — independent
slices MAY be batched into one commit (the "one slice = one commit" gate is retired).

| Phase | What | Source plan | Eff | Status |
|---|---|---|---|---|
| P0 | guardrails: pipeline.py/monitor.py/model_interact.py LOC ratchet in check.sh; mypy strict on pipeline_core | slice 13 | S | ✅ 2026-06-15: the strict flag set for `pipeline_core.*` is now COMPLETE — added `no_implicit_reexport` (the last deferred `--strict` flag; P5 done so the shim caveat no longer applies). Verified 0 errors across the full check.sh target set (pipeline.py + monitor.py + model_interact.py + pipeline_core, 84 files): every cross-module name is a genuine definition/explicit re-export. LOC ratchet (3 god-files) + coverage ratchet (`pipeline_all`/`pipeline_core`/`domain`, auto-up) remain in check.sh. |
| P1 | FixLoop unification: ONE driver + `FixUnit` protocol; port per-fn loop, per-impl loop, post-pass test-retry (one commit each, harness-pinned: U2 keep, B3 partial accept, sig-hatch, lock points) | 8a f1–f4 | M | ✅ f1–f5 were already shipped; P1 session (2026-06-12): driver relocated to `application/fix_loop.py` (`FixOutcome`/`drive_fix_loop` + settable `check_stop`; pipeline.py keeps `_`-aliases) and task0c's LAST two hand-rolled loops ported — harness fix (was UNBOUNDED on changing-but-unresolved sigs; now floor-stall+backstop, strip ladder on exhaustion) and `_finalize_and_commit` gen_tests block fix (same bound). Pins: tests/test_task0c_fix_loops.py. **f6 DONE 2026-06-15** — live-validated on the microcrate run (`logs/pipeline_micro_20260615-044637.log`, git 3083e10): the unified fix-loop driver ran clean (per-fn + per-impl fills, task3 inline retry 29/30→30/30), C10 parallel-streams PASS (47 llm events, max 5 concurrent, every overlap on a distinct thread). **P1 COMPLETE.** |
| P2 | engine pass: `Stub`/`ImplBlock`/`FillResult` models; thread `FnRecord` (kills `"fn"` vs `"fn_name"`); split `_task2_fill_fns_one_by_one`+`_process_one_stub` into `application/reconstruct/` units | R3.1–R3.3 | L | ✅ slice 1 (2026-06-12): `_process_one_stub` DE-CLOSURED → module-level `_fill_one_stub(idx, rec, ctx)` returning the new domain `FillResult` (R3.1 partial); counters/errors aggregation moved to the dispatcher under `_counters_lock`; `application/reconstruct/` started (`PerFnPassCtx`, `fill_skip`, `group_priority_levels`). AST-verified zero stray closure names. The unit still reads ~32 pipeline.py module globals — those helper groups (splice/locate, retro, gather) must migrate before the unit itself crosses the layer (avoids a Task0cDeps-style record). VALIDATE: e2e fill-loop suite + microcrate smoke. Slice 2 (same day): retros store → `adapters/retros.py` (load/save/canon_container/retro_key/reset_exhaustion_flags/is_fn_fully_exhausted, names promoted per R6.5); pure fill rules → `reconstruct/fill_rules.py` (FAILED_ATTEMPT_SENTINEL [seam: skeleton.rs matches it], is_unimplemented_body, make_failed_attempt_body, failure_hit_unfilled_stub, module_of). pipeline.py keeps `_`-aliases. Slice 3 (same day): the regex-free splice core → `reconstruct/splice.py` — `splice_fn_body_at` (byte-exact) + `locate_fn_by_record` (syn records; enumerator injected as a plain callable, resolved at CALL time in the pipeline.py shim so the e2e harness monkeypatch seam keeps working). KEPT in pipeline.py as justified regex: `_locate_fn`/`_splice_fn_body` (extractor-absent fallback, R7.6 class), `_splice_fn_with_where`/`_replace_fn_signature`/`_extract_signature_block` (model-protocol parsing, R8.5 class). Slice 4 (same day): cargo error-text shaping → `reconstruct/cargo_errors.py`, REGEX-FREE-IFIED on the way (R7 standing rule): `is_error_header`/`has_rs_line_loc`/`ecodes_in_text`/`excerpt_for_code` replace the `^error\b`, `\.rs:\d+`, `\bE\d{3,4}\b`, and per-code-excerpt regexes (equivalence pinned against the old patterns in tests/test_cargo_errors.py); `strip_cargo_warnings` + `focus_cargo_errors` moved verbatim; `_record_error_codes_seen` + the two `_fill_one_stub` E-code scans now use the helpers. Slice 5 (2026-06-13): cargo diagnostic parsers → `reconstruct/cargo_errors.py` (companion to slice 4), REGEX-FREE-IFIED: `diagnostics_from_json` (JSON cargo-message → `list[Diagnostic]`), `err_sig_from_diagnostics`, and `err_sig` whose rendered-text FALLBACK is now the structured `err_sig_from_text` scan replacing the legacy `error\[E\d+\]: .+` / `^error: .+` regexes (equivalence pinned in tests/test_cargo_errors.py: `_old_err_sig_text`). pipeline.py keeps `_cargo_diagnostics_from_json`/`_cargo_err_sig`/`_cargo_err_sig_from_diagnostics` as import-aliases; `_run_cargo_check_diag` (subprocess runner — adapter-layer, stays) now calls the imported parser. Slice 6 (2026-06-13): `_run_cargo_check_diag` → `reconstruct/cargo_runner.py` as `run_check_diag(cargo: Cargo, …)` — composes the Cargo PORT (`ADAPTERS.cargo.run`) + `diagnostics_from_json`, exactly as the Cargo port docstring prescribes ("JSON-diagnostic checks compose on top in the application layer, not the adapter"). Removes the LAST raw `subprocess.run` cargo call from pipeline.py; the function is now port-injected (testable with a fake Cargo — tests/test_cargo_runner.py: flag-insertion, no-double-insert, rendered-rebuild, rc=-1 parity). pipeline.py keeps `_run_cargo_check_diag(cmd,cwd,env,timeout)` as a thin shim binding `ADAPTERS.cargo`. Slice 7 (2026-06-13): `_parse_failed_tests` regex → `domain/test_results.parse_failed_tests` (regex-free, R7 standing rule): captures BOTH the `test … ... FAILED` summary rows AND the `---- … stdout ----` section headers via leading-keyword + whitespace-token checks (superset of `failing_test_names`); kills the `^test\s+(\S+)\s+\.\.\.\s+FAILED` / `^----\s+(\S+)\s+stdout\s+----` regexes. Equivalence pinned in tests/test_domain.py (`test_parse_failed_tests_matches_old_regex`); pipeline.py keeps `_parse_failed_tests` as an import-alias. Slice 8 (2026-06-13): `_run_cargo` → `reconstruct/cargo_runner.run_cargo(cargo: Cargo, …)` (port-composed, same pattern as slice 6); the stdout+stderr `_combined` layout moves here too, built from the port's plain fields — so the now-orphaned `Process.combined` adapter property is DELETED and the port stays a pure result record. pipeline.py keeps the `_run_cargo` shim; fake-Cargo tests extended (combined layout, no-stderr, rc=-1). Slice 9 (2026-06-13): `_run_scoped_tests` → `cargo_runner.run_scoped_tests(run_cargo_fn: RunCargo, …, *, err_cap)` — composes a `RunCargo` callable + `parse_failed_tests` (slice 7, domain); `_FIX_ERR_CAP` is now an injected `err_cap` param (config stays in pipeline.py). CRITICAL SEAM: the runner takes the run-cargo OPERATION as a callable (not the Cargo port) and the pipeline.py shim passes `_run_cargo` (the module attribute, slice 8's port-composed shim) — so the e2e/smoke harness's `monkeypatch pipeline._run_cargo` still intercepts the scoped-test run. (First cut passed `ADAPTERS.cargo` directly → bypassed the patch → test_pipeline_smoke::test_run_scoped_tests + test_e2e_u2_gate both went red; caught by the USER's pytest, since the autopilot can't run it.) fake-callable tests cover pass/fail/rc=0-with-FAILED/timeout/err_cap-truncation. NOTE (env-ordering myth busted): `.env` is already loaded at `model_interact` import (pipeline.py:37, before the pipeline_core block at 40), so module-level env reads in any later-imported config module DO see `.env` — pipeline.py:217 `load_dotenv` is redundant. Remaining before the unit crosses: `_gather_fn_fix_context` (prompts-layer + fr_path IO → belongs to P7, not a verbatim move), `_phase_c_retrospective`/`_extract_self_note` (need an LLM port), caps/stalls config (home decision: extend `Settings`/`settings.py` lazy `from_env`, not a new import-time module), `_u2_test_gate` (now only needs `_UNIFIED_FILL_LOOP`/`_U2_TEST_TIMEOUT` config + a guard callable injected — `_run_scoped_tests` dep already crossed). Slice 10 (2026-06-13): caps/stalls config → `settings.py` new frozen `FixLoopCaps` (nested on `Settings`, built in `from_env` from the already-computed `single_call`); single-sources the pipeline.py fix/stall globals `_FIX_ERR_CAP`/`_UNIFIED_FILL_LOOP`/`_U2_TEST_TIMEOUT`/`_PER_FN_FIX_HARD_CAP`/`_PER_FN_FIX_MAX_STALLS`/`_PER_PASS_TEST_FIX_*`/`_PASS_THROUGH`/`_PASS_THROUGH_STALLS`/`_CONVERGE_MODULE` (each now `= SETTINGS.fix_caps.*`, same pattern as `SINGLE_CALL_CHAR_LIMIT`). SEAM: `_effective_stalls` + the `_PASS_THROUGH*`/`_CONVERGE_MODULE` globals STAY module-level patchable in pipeline.py (test_pipeline_smoke monkeypatches them) — only the env-reading VALUES moved; the function body is unchanged and still reads the module globals. Legacy hard-cap aliases (`PHASE_C_FIX_CAP_PER_FN`/`_PER_PASS`) preserved. Env→value mapping pinned in tests/test_settings.py. `_u2_test_gate` now needs only a guard callable injected to cross. Slice 11 (2026-06-13): `_u2_test_gate` → `cargo_runner.u2_test_gate(run_cargo_fn, …, *, enabled, timeout, err_cap, guard)` — the SINGLE test-acceptance gate's logic crosses to the application layer, composing `run_scoped_tests`; config (`_UNIFIED_FILL_LOOP`/`_U2_TEST_TIMEOUT`/`_FIX_ERR_CAP`) + the `_guard` callable are injected. SEAM: same as slice 9 — shim passes `_run_cargo` (patchable module attr) + `_guard`, both resolved at call time, so the e2e harness's `monkeypatch pipeline._run_cargo` still intercepts (test_e2e_u2_gate). pipeline.py keeps the `_u2_test_gate(fn_tests,test_cmd,target_src_dir,cluster_env)` shim. fake-callable tests cover off/no-tests/pass/block-on-own-fail/ignore-unrelated/failsafe-guard/inconclusive (tests/test_cargo_runner.py). Slice 12 (2026-06-13): `_extract_self_note`'s PURE parse → `domain/llm_output.parse_self_note(resp, *, min_len, max_len)` (regex-free: last non-empty line → strip backticks/quotes → reject `NONE`/<min_len → cap max_len). pipeline.py `_extract_self_note` keeps the two-attempt `ask` ladder (I/O shell) and delegates the parse. Pinned in tests/test_domain.py. NOTE: full LLM-port COMPOSITION of `_extract_self_note`/`_phase_c_retrospective` (inject the `LLM` port + caps, move the ladder to application) still pending — only the deterministic parse crossed here. Slice 13 (2026-06-13): `_phase_c_retrospective`'s CATEGORY/GUIDANCE parser → `domain/llm_output.parse_retrospective` (regex-free, R7 — kills 3 regexes: `^\*+\s*\|\s*\*+$`, `^category\s*[:=]\s*(\S+)`, `^guidance\s*[:=]\s*(.+)$` + the label-stop one); helpers `_strip_retro_decorations`/`_retro_label`. LLM call (`_ask_retrospective`) stays in pipeline.py. Equivalence pinned vs old regex in test_domain.py (10 cases incl. bold/bullet/no-token/cap-fallback). NOTE: the coverage ratchet's pipeline.py-only floor was replaced this session by a relocation-invariant `pipeline_all` (pipeline.py+pipeline_core) target so strangler-fig moves aren't penalised — tools/coverage_ratchet.py, .coverage_ratchet.json (`pipeline_all`=45). check.sh now runnable by autopilot (deny-list cleared). **DONE — slice 14 / R3.3 (2026-06-14, session 75): the unit CROSSED the layer.** `_fill_one_stub` → `application/reconstruct/fill_one_fn.py:FillOneFn` (per-fn fill use-case); the 708-LOC `_task2_fill_fns_one_by_one` body → `application/reconstruct/phase_c_engine.py:PhaseCFillEngine` (per-pass dispatch + worker pool + per-pass test-fix loop + pass-level floor-stall). Both depend ONLY on narrow role ports in `ports/fill_engine.py` (8 Protocols: EngineTelemetry, Mutator, Cargo, PromptContext, WorkspaceModel, Retros, TestLinker, StallPolicy, ImplFillPath — ISP slices). pipeline.py is the COMPOSITION ROOT: `_PhaseCFillAdapter` (one object satisfying every role; methods late-bind the module-level `_func`s so the e2e/scripted-harness monkeypatches still intercept at call time) + `_build_phase_c_engine()` (assembles use-cases per call so patched SETTINGS/parallelism are honoured); `_task2_fill_fns_one_by_one` is now a thin shim. New domain value objects: `FnFixContext`, `ClusterFillResult` (the dict the legacy fn returned, round-tripped by `to_dict`). The test-fix reverse-lookup dropped its `re.finditer(r"\bfn …")` source scan for the syn enumerator (`WorkspaceModel.enumerate_fns`) per R7 migrate-on-touch — the LAST regex in the engine body. pipeline.py 18064→17079 LOC (−985). Tests: tests/test_fill_engine_usecases.py (8, fake adapter: domain VOs + FillOneFn skip/accept/empty branches + engine impl-early-return/empty-cluster); the real engine stays covered e2e by test_e2e_fill_loop/_u2_gate/_test_retry via the shim. Static GREEN: ruff, mypy-strict pipeline_core (81 files) + mypy pipeline.py, import-linter (layers KEPT), loc ratchet 17079. RUN-gated (R3.3) — USER validates via microcrate smoke. |
| P3 | task0c self-containment: move the 7 `_task0c_*` helpers in, DROP from `Task0cDeps` | R3.4 | M | ✅ 2026-06-13: ALL 7 engine helpers now live in `tasks/task0c.py` as `_engine_*` (orchestrator binds `functools.partial(_engine_*, deps)` so call-sites stay verbatim); dropped from `Task0cDeps`. Sub-deps handled per shared/exclusive: pure-exclusive `_extract_existing_tests` MOVED in; genuinely-shared collaborators INJECTED (`coverage_ignore_pragma`, `mutation_harden_file`, `gen_tests_siblings`, `type_signals_write`, `guard`) — the mutation engine (`_mutation_harden_file`/`_mutation_survivors`) is its OWN subsystem, not dragged in. Dead `gen_tests_count` field dropped. **Task0cDeps 73→70** (the "75→~15" gloss was unreachable: AST shows 70/71 remaining fields are used by the orchestrator BODY directly — the win is task0c OWNING its pass logic + no task0c-named helpers injected back, not a field-count cliff). Killed/relocated 4 regexes (R7): retrospective fn-count → `_looks_like_fn_decl` (token, pinned); targeted_pragma's 2 model-output regexes → `domain/llm_output.extract_code_blocks`+`strip_uncovered_markers`; log_attempt type-extract → `reconstruct/cargo_errors.error_type_signals` (word-aware — FIXES the old `struct`-inside-`reconstruct` substring bug); build_prompt_sections `<think>` strip → `domain/text.strip_think_blocks`. task0c.py now REGEX-FREE. Each move +fakes test (coverage ratchet held; raised pipeline_all 45→47, pipeline_core 73→74). |
| P4 | verbatim/Clean-Arch moves: Task1Synthesizer/Task2Generator families, DependencyGraph, WorkspaceScanner, gen_tests reattach block → `application/` | slice 11 | M | ✅ Task2Generator family DONE as Clean-Architecture over narrow role ports (NOT a monolith move): **slice 1** StaticTask2Generator, **2** LLMTask2Generator, **3** StaticFileListTask2Generator (`ports/generator.py` LLM/Workspace/ContextDocs/Telemetry; `domain/generation.py` VOs + regex-free helpers). **Slice 4** (2026-06-15) StaticSkeletonTask2Generator — the 4-phase per-cluster orchestrator (~800 LOC) → `application/generators/task2_generator.py` over 5 NEW role ports (Cargo/FillEngine/Clusters/GenTests/SourceScan); `subprocess.run`→`Cargo.run_raw` (rc=-1 timeout sentinel); R7 `_gt_re`→`domain.parse_gen_tests_filename` (equivalence-pinned); `_driver=="tdd"` enum/str inconsistency collapsed to `cfg.phase_c_tdd`. `_Task2Adapter` (one object, every role; methods late-bind globals → test seams live) + `_build_skeleton_config()`/`_new_skeleton_task2_generator()`. **Slice 5** (2026-06-15) `GenerationRequest` parameter object threaded through all 4 `generate()` + `tasks/task2.py` dispatch + the standalone harness (new context input = one field, not a 5-signature edit). Tests: test_skeleton_task2_generator.py (15) + test_task2_generators.py (11) fake-adapter; module 80.5% covered (holds pipeline_core floor). WorkspaceScanner (`_workspace_scan`) + gen_tests reattach (P5-S3a) already in `application/`. **Task1Synthesizer family migrated to ports** (2026-06-15, debt #14 RESOLVED): NEW `ports/synthesizer.py` (Task1LLM/PromptShaping/ChunkCache/Progress/Requirements) + `domain.Task1Config` VO; synthesizers are frozen dataclasses over the ports; `_Task1Adapter` composition root replaces the `configure()` global-injection seam; bodies kept verbatim via per-method local-binding preambles; tests/test_task1_synthesizer.py → fake-adapter (81.2% cov). **P4 fully complete** (the remaining `configure()`-seam siblings task0c/workspace_scan/prompts_runtime/discovery are a separate, broader follow-up — not P4). **LIVE-VALIDATED 2026-06-15** (`logs/pipeline_micro_20260615-044637.log`, git 3083e10): the skeleton backend ran per-cluster `parallelism=2` over 2 cfg-clusters (stable + extra_cap), Phase A `Cargo.run_raw` cargo-check + Phase C TDD `FillEngine` filled 8 fns / 0 stubs / 0 errors, end-to-end SUCCESS iter 1 — adapter↔port wiring confirmed (no signature mismatch). **RE-VALIDATED on the LATEST code 2026-06-15** (`logs/pipeline_micro_20260615-052845.log`, git 15485dc, pipeline.py=160b892d): the run now includes #13 (`FnFixContext` direct return, per-impl fill ran) + #14 (TASK 1 via `LLMTask1Synthesizer` over the unified `_Task2Adapter` → "functional_requirements.md written / output validated OK") + chunk_cache (clean import) + slice 5 (`GenerationRequest`) — SUCCESS iter 1, C5/C7/C10 PASS, zero tracebacks/NameError (every #14 preamble binds live). |
| P5 | `main()` → `application/orchestrator.py` (phase state machine over ports); delete dead `_`-shims; drop `_` prefixes on promoted public entries | slice 12 / R2.2 / R6.5 | L | ✅ 2026-06-14: `pipeline_core/application/orchestrator.py` created. P5.1 `build_arg_parser()` (pure CLI parser, removed pipeline.py's now-unused `argparse`). P5.2 `run_phase_subcommand()` (the `--phase-*-only`/`--skeleton-backend` routing → injected-handler dispatch; 5 if-blocks → 1 call). tests/test_orchestrator_argparser.py (6). pipeline.py 18727→18636. P5.3a `module_descriptor()` + `DepGraphView` Protocol (per-module facts over a port). P5.3c `filter_module_test_files()`. P5.3d `with_example_usage_signal()` (+`_EXAMPLE_USAGE_HEADER`). P5.3e `next_module()` (multi-module scheduler de-closured). All unit-tested + each live-validated on the microcrate watch loop (SUCCESS 30/30). pipeline.py 18727→18610. **The clean PURE extractions from the loop are now exhausted** — what remains of the ~800-line `while` body is straight-line I/O + TASK 1–4 phase-call orchestration (strip/bake/commit/collect + the retry loops). P5-S1 (test-discovery adapter) DONE: `is_test_file`/`collect_test_files`/`collect_protected_test_files`/`collect_examples` + `SKIP_DIR_NAMES`/`TEST_DIR_NAMES` + `match_gen_tests_sibling` → `pipeline_core/adapters/discovery.py` (decl-only predicate injected via `configure()` so the gen_tests regex family stays in pipeline.py). Shims preserve all call sites + the task0c/workspace_scan `configure()` wiring. tests/test_discovery_unit.py (5). pipeline.py 18610→18488. Static-green (ruff/mypy-strict/import-linter); loc ratchet lowered. P5-S2 (strip adapter) DONE: `strip_module_source`+`save_original_structure`+`ORIGINAL_STRUCTURE_DIR` → `pipeline_core/adapters/strip.py` (imports strip_to_tests backend; removed pipeline.py's now-unused `detect_language`/`process_rust` import). tests/test_strip_unit.py (2). P5-S3a (reattach block) DONE: the ~196-line gen_tests reattach control flow → `pipeline_core/application/gen_tests_reattach.py` `reattach_gen_tests()` over `ReattachDeps` (11 injected callables, per-block pattern). main() block → 1 call. tests/test_gen_tests_reattach_unit.py (2); debt #9 resolved. pipeline.py 18488→18283. P5-S3b (strip/bake/commit pre-step) DONE: the whole `if _from_spec / already-stripped / else: capture→bake→commit→tag→strip→commit-and-mark` block → `pipeline_core/application/strip_bake.py` `strip_and_bake_module()` over `StripBakeDeps` (12 injected callables + tarpaulin_bin/coverage_target; Hard-Rule-11 ordering preserved verbatim). main() block (~73 lines) → 1 call. tests/test_strip_bake_unit.py (4: from_spec/already-stripped no-ops, happy-path ordering, bake-failure abort). pipeline.py 18283→18234. P5-S3c (lock/complete dedup) DONE: the lock/exit skeleton repeated at 3 t2-4 sites (t3-pass, no_files build-pass, fix-loop reverified) → one `orchestrator.lock_or_complete()` over `LockDeps` (6 callables incl. injected `sys_exit`); call sites differ only in message/label/tag args. 3 tests in test_orchestrator_argparser.py. The highest-stakes path (lock vs complete-and-exit) now lives in one verified place. ratchet=18231. **P5 LOOP DECOMPOSITION DONE (2026-06-14, SOLID/Clean — see "Decomposition doctrine"):** the per-module TASK 1–4 loop is broken into single-responsibility units, NOT relocated as a monolith. DOMAIN: `domain/reconstruction.py` (`IterationState` immutable value object + `plan_whole_project_context` policy + `PROC_MACRO_CONSTRAINTS`); `domain/test_results.insert_no_run` (regex-free cargo-test→--no-run). PORTS: `ports/reconstruct.py` — 13 role-based Protocols (Telemetry/PhaseGate/RequirementsService/Generator/TestRunner/Fixer/Analyst/FailureJournal/Ratchet/Artifacts/CoverageDocs/FlowAudit/DocsMetrics), the DIP seams. APPLICATION use-cases (`application/reconstruct/`): `RequirementsPhase` (TASK1 retry/escalate), `ReconstructionStep` (TASK2→reattach→conformance→TASK3→verdict dispatch → `StepOutcome`), `FailureAnalysisStep` (TASK4 tail→genesis/docs/journal/ratchet/audit → next `IterationState`), `ModuleReconstructor` (coordinator), `RunContext` (per-module value object). COMPOSITION ROOT: `pipeline.py` `_ReconstructAdapter` binds concrete fns to the role ports; use-cases receive narrow slices (ISP). main() per-module loop body (~390 lines) → RunContext build + one `_MODULE_RECONSTRUCTOR.run()` call. Tests: test_reconstruction_domain.py (7) + test_reconstruct_usecases.py (16 fake-adapter) — new modules 97% cov. Surfaced+flagged dead `regression` var (debt #12). pipeline.py 18231→18064; all static-green. **S4 (R6.5 naming + dead-shim deletion) INVESTIGATED → NOT ACTIONABLE / RETIRED:** AST sweep for provably-dead top-level symbols = 0 (the decomposition is clean — the `_ReconstructAdapter` keeps every helper referenced, ruff confirms no unused imports/locals). The `_`-prefix rename is unsafe + valueless: 40+ private symbols are the deliberate test seam (referenced as `pipeline._x` by the unit suite), and dropping `_` on composition-root-internal helpers is anti-Pythonic (they ARE private). Per the doctrine (effort is a tiebreaker, never churn) R6.5 is closed not-done. **P5 COMPLETE.** |
| P6 | dissolve remaining god-files: transports → `adapters/llm_transports/` (model_interact.py = shim); monitor.py split data-vs-rendering → thin TUI adapter | new | M | 🔄 monitor-half STARTED 2026-06-13: the events.jsonl readers (`pipeline_start_time`/`read_recent_events`/`coverage_from_events`/`phase_c_state`/`latest_{compile,test}_file_stats`/`latest_flow_audit`) relocated VERBATIM → `pipeline_core/monitor/events_data.py` behind a `configure(events_log=)` facade; monitor.py keeps `_`-aliases + injects the discovered path at startup; monitor.py 2283→2018 LOC. Tests: tests/test_monitor_events_data.py (rollup/run-start-filter/empty/configure) + the 3 existing monitor tests repointed at events_data. 2nd cut (same day): the log/state/backend readers → `pipeline_core/monitor/run_data.py` (`parse_coverage_json`/`baked_cluster_order`/`find_project_dir`/`parse_pipeline_state`/`current_stage_label`/`parse_pipeline_log`/`read_log_tail`/`parse_task_pipeline`/`read_backend`/`parse_max_tokens`/`parse_vllm_cpt`/`pipeline_running`/`detect_cached_repo_url`) behind `configure(script_dir,pipeline_log,guards_log)`; monitor.py keeps `_`-aliases; **monitor.py 2283→1576 LOC**. Strictly typed for pipeline_core (log/state parsers return `dict[str,Any]` to match the prior lenient consumer typing). Tests: tests/test_monitor_run_data.py (9). DATA/RENDER BOUNDARY (complete for the monitor half): "data" = readers of the run's recorded artifacts (events/log/state/coverage/guards/.env/git) → run_data+events_data; the TUI keeps rendering + the live hardware/service probe widgets (`gpu`/`_vllm_status` — they probe nvidia-smi/docker/http for immediate display, not run artifacts) + the event/log formatters (`_event_format`/`_event_lines`/`_log_lines` — ANSI+width-coupled). REMAINING = transports → adapters/llm_transports/ ONLY, which is engine-DEPENDENT (vLLM streaming runtime — needs a live run), so out of the engine-independent batch. NOTE: the log parsers are regex log-scrapes (debt #7) — migrate only when the emitter is restructured to emit structured events. **TRANSPORTS DONE 2026-06-14**: the 4 `*Transport` + `_BaseTransport` + socket/stream infra + `_make_hooks`/`_backend_factory` + MODEL/MAX_TOKENS/MODEL_CAPS RELOCATED verbatim → `pipeline_core/adapters/llm_transports.py` (mypy-strict typed; fixed a `_LOG_DIR` `__file__`-relative move-bug → repo-root `logs/`). model_interact.py 762→135-line shim (composition root + re-exports). +tests/test_llm_transports_unit.py (10). Live-validated on the 2026-06-14 microcrate run (48 llm events, streaming OK). So P6 = monitor-data-half + transports DONE; only debt #7 (the log-scrape regex→structured-emitter migration) remains. **DEBT #7 STRANGLER-FIG 2026-06-15** (user-approved): EMITTER restructured — pipeline.py now emits structured stage events `task.start{task}` for task0a/0b/1/2/3/4 (task0c already did) + `task.done{task=task3,data.passed}` + `module.iter{module,iteration}` (use-case `module_reconstructor` via the Telemetry port). CONSUMER — new `events_data.task_progression()` replays those into the exact `{states,current,iteration,module,task0c_clusters_done,task3_pass}` shape `parse_task_pipeline` produced (linear-pipeline model: a `task.start` marks predecessors done; `module.iter` resets task2/3/4). monitor.py prefers the event source, FALLS BACK to the log scrape (`tp = _task_progression() or _parse_task_pipeline(...)`) so the live TUI cannot regress. Tests: test_monitor_events_data.py (+3: full replay / per-iter reset / none-without-events). **RESIDUAL — DONE.** Deletions already shipped in debt#7 phase 2 (parse_task_pipeline + the peripheral parse_pipeline_log/parse_vllm_cpt/parse_max_tokens scrapes deleted, events emitted at source). Event-path confirmed LIVE 2026-06-15 (`logs/pipeline_micro_20260615-155218.log`, git eb8807d): task.start×11/task.done×7/module.iter×1/vllm.budget×40/vllm.cpt_update×41 emitted with the consumer-expected shape, SUCCESS iter 1. **P6 DONE.** (Caveat CLEARED 2026-06-15: the post-eb8807d cat-4/R7-2nd-pass source-parsing migrations — cdb42ae/519b141/5628f17 — are ALL ancestors of the run sha d3765d7 (`git merge-base --is-ancestor` confirmed each), and that run `logs/pipeline_micro_20260615-210823.log` reached `complete — all tests pass iter 1` (30/30, 2 clusters). The migrated syn-extractor source-parsing paths ran live without regression. No re-run pending.) |
| P7 | frozen per-family `*PromptContext` dataclasses (no defaults — missing section = visible hole) | slice 8 tail | M | ✅ 2026-06-14 (verified): the silent-hole-risk fill-prompt families all take frozen no-default section records — `prompts/context.py` `PerFnSections`/`PerFnFixSections`/`PerImplSections` consumed by `build_per_fn_prompt`/`build_per_fn_fix_prompt`/`build_per_impl_prompt`; `prompts/repo_spec.py` `RepoSpecDocs`. A forgotten section is a `TypeError` at construction, not a silent `""` (the BoxedError-missing-TYPES_IN_SCOPE bug class). Structural/config params (fr_path, container, identity) stay explicit by design. |
| P8 | OSS finish: installable package + console-script; naming sweep; CHANGELOG/versioning; R7.4 live validation + R8.4; docs aligned | R6/R7/R8 tails | M | ✅ 2026-06-13: packaging DONE — `[build-system]`+`[project]`+`[project.scripts]` in pyproject.toml (`pip install .` → `determ`=pipeline:main, `determ-monitor`=monitor:main; dev extra); `pipeline_core.__version__="0.1.0"`; CHANGELOG.md; setup.sh editable-installs the pkg (`-e . --no-deps`). **R7.4 live cfg-cluster validation DONE 2026-06-15** (microcrate: stable + extra_cap clusters reconstructed + 100% cov). **R8.4 DONE 2026-06-15**: the active llvm-cov backend already parses the structured **lcov** format (`LlvmCovProvider._parse_lcov`, DA: line records → covered/coverable counts; tarpaulin/grcov read JSON) — coverage `%` is COMPUTED, never text-scraped; the one residual `%`-string round-trip (the augmented sentinel `write "{cov:.1f}%"` → read `.rstrip("%")`) is now a bare numeric sentinel (`float()` directly, no string surgery). R6.5 naming sweep closed (churn). **P8 COMPLETE.** |

Throughout: the canonical-failure-loop rule (R13) holds for every new fix site; any touched
code containing a regex migrates to the structured parser as part of the touch (R7 standing
rule); deps records that only re-inject what a module could import are a smell to remove, not
relocate.

### 0a. Execution inventory — microcrate-gated vs not (2026-06-13, pipeline.py=19471 LOC)

The `pipeline.py` < ~800 LOC end state is reached ONLY after the engine phases run; most of
the remaining ~18.7k lines move in P4/P5/R3.3. Boundary rule: a **verbatim relocation behind
a re-export shim** is check.sh-executable now (imports/types/contract proven; runtime
re-confirmed at the USER's next microcrate/anyhow run — the session-70 P4 scanner precedent).
**Restructuring live control flow** (decompose/rewire the reconstruction loop) needs a run.

NOTE (2026-06-15): this inventory is STALE below — kept for history. P4 generator
families + reattach are DONE (see the P4 row); P6 transports DONE (debt #10); debts
#1/#2/#3 RESOLVED (see technical_debt.md); P0 strict-flags DONE. Authoritative status =
the phase table above ∪ the open rows in technical_debt.md (#7, #12 — #13/#14 RESOLVED 2026-06-15).

| status | item | where (pipeline.py) | ~LOC out of pipeline.py | gate |
|---|---|---|---|---|
| ✅ done | P4 — Task1Synthesizer family | → `application/generators/task1_synthesizer.py` | ~848 | done (configure() seam; ports-migration = debt #14) |
| ✅ done | P4 — Task2Generator family | → `application/generators/task2_generator.py` (ports + VOs) | ~1340 | done (slices 1–5) |
| ✅ done | P4 — gen_tests reattach block | → `application/gen_tests_reattach.py` (P5-S3a) | ~150 | done |
| ✅ done | P6-transports — `*Transport` → `adapters/llm_transports.py` | (model_interact.py) | 0 here | done (debt #10) |
| ✅ done | Debt #1 delete redundant `load_dotenv` | 217 | ~1 | resolved |
| ✅ done | Debt #2 stale `_UNIFIED_FILL_LOOP` docstring | tests/test_e2e_u2_gate.py | 0 | resolved |
| ✅ done | Debt #3 drop dead `_PER_FN_FIX_CAP` alias | — | ~1 | resolved |
| ✅ done | P0 strict flags / ratchets | pyproject.toml | 0 | `no_implicit_reexport` added; 0 errors |
| ⬜ ongoing | coverage — tests for under-covered pipeline_core | tests/ | 0 | static (per-slice) |
| ⬜ no-run | R6.5 (partial) drop `_` on already-public entries | — | 0 | static (LOW value, churn — better with P5) |
| ✅ done | R3.3 — split `_task2_fill_fns_one_by_one` (708 LOC) → `reconstruct/{fill_one_fn,phase_c_engine}.py` over `ports/fill_engine.py` (session 75) | function body | ~985 out | microcrate (USER) |
| ✅ done | P5 — `main()` → orchestrator state machine | main() loop | ~1400 | LIVE-VALIDATED 2026-06-15 microcrate (SUCCESS iter 1) |
| ✅ done | R7.4 / R13.1-2 / P1-f6 telemetry diff / C10 (debt #6) | various | — | LIVE-VALIDATED 2026-06-15 microcrate: cfg-cluster assignment correct (stable+extra_cap both 100%, C7 PASS), canonical fail-loop (task3 retry 29/30→30/30), telemetry/C10 PASS (47 llm ev, distinct-thread overlaps) |

Reality on the 800 goal: doing ALL no-run items takes pipeline.py ~19.5k→~17k (P4 ≈2.3k is the
only no-run reducer; P6-transports shrinks model_interact.py not pipeline.py). The remaining
~16k requires P5 + R3.3, which are run-gated. So the autopilot can prep+land P4 and the static
items green-on-check.sh, but cannot reach the headline LOC target without microcrate/anyhow runs.

---

- **Python** — `pipeline.py` orchestration (the LLM-driven reconstruction loop).
- **Rust** — `tools/skeleton_extractor` (syn-based AST extraction).

This defines the *target* layout and the migration rule. Migration is **strangler-fig**:
the package grows beside `pipeline.py`; `pipeline.py` stays the entrypoint and
progressively imports from it; behaviour stays identical; `check.sh` stays green at
every step. No big-bang rewrite.

Guiding constraint (both languages): **Pythonic / Rustacean, not Java-ish.** SOLID via
language idioms — `Protocol`, `@dataclass`, plain constructor injection on Python;
traits, modules, `thiserror` on Rust. No DI containers, no abstract-factory ceremony, no
interface-per-class reflex.

---

## 1. Layers (dependency rule: inner never imports outer)

```
domain      → pure types + rules. No I/O, no subprocess, no LLM, no filesystem.
ports       → interfaces (typing.Protocol) the application depends on.
application → use cases: the tasks + fill/merge/mutation loops + orchestrator.
adapters    → concrete port impls. The ONLY layer that does I/O.
__main__    → composition root: build Settings + adapters, inject, run.
```

| Layer | May import | Must NOT import |
|---|---|---|
| domain | stdlib only | ports, application, adapters |
| ports | domain | application, adapters |
| application | domain, ports | adapters, subprocess, requests, model_interact |
| adapters | domain, ports, libs | application |
| __main__ | everything | — |

Discipline now; `import-linter` contract in CI later.

---

## 2. Python package layout (`pipeline_core/`)

Entrypoint stays `pipeline.py` (shim). Package name `pipeline_core` avoids shadowing
`pipeline.py`.

```
pipeline_core/
  domain/
    enums.py        # StrEnum: Backend, CoverageBackend, FillUnit, FixStrategy,
                    #          PhaseCMode, PhaseCDriver, ModelTier
    model_caps.py   # model_tier(model), model_context_chars(model)  — pure
    models.py       # @dataclass(slots): Cluster, Module, FnRecord, Diagnostic,
                    #          Frontier, FillResult   (replaces dict-typing)  [later]
    cfg.py          # pure cfg-predicate evaluation (CfgSet)                  [later]
  ports/
    __init__.py     # Protocols: LLM, Extractor, Cargo, Coverage, Workspace
  application/       # tasks/ reconstruct/ merge/ mutation/ orchestrator.py    [later]
  adapters/          # vllm_llm, cli_extractor, cargo_cli, coverage, workspace  [later]
  prompts/           # t-string templates + budget/condense                    [later]
  settings.py        # one typed Settings (collapses globals + getenv)         [later]
```

### SOLID → Python idiom map

| Principle | Realization |
|---|---|
| SRP | one module per task / loop (vs 1.3k-line god-fns) |
| OCP | new backend/strategy/task = new class impl of a port + registry/`match` arm |
| LSP | `Protocol`/ABC impls substitutable (already true for `TestAugmentor`) |
| ISP | small ports (`LLM`, `Extractor`, `Cargo`, `Coverage`) not one god-object |
| DIP | `application` imports `ports` (Protocols); `__main__` injects adapters |

Structural `Protocol` ⇒ adapters need no base class. Injection is plain constructor
args — no framework.

---

## 3. Rust layout (`tools/skeleton_extractor`)

```
src/
  lib.rs            # re-exports only
  model.rs          # records serialized to Python (the JSON seam) — pub types
  skeleton.rs       # skeletonise: bodies → unimplemented!()
  enumerate.rs      # fns / types / mods / dispatch families / feature-gate cfgs
  merge.rs          # cross-cluster merge (prettyplease)
  cfg.rs            # Cfg newtype + predicate logic
  error.rs          # thiserror typed errors (replace String/anyhow at lib edge)
  cli/              # clap derive subcommands (replace hand-rolled flag parsing)
```

- Edition **2024** (let-chains for token walks, `gen` blocks where they read cleaner).
- `clap` derive replaces the 26 hand-rolled `--flag` branches in `main.rs`.
- `thiserror` for the library error type; `anyhow` only in the binary.
- `Cfg` newtype instead of bare `String` cfg predicates.

---

## 4. The Python↔Rust seam is sacred

The JSON record contract (`enumerate-*`, `--skeletonise`, `--merge`) between the
`cli_extractor` adapter (Py) and `model.rs` (Rust) is a **good** boundary. Migration
preserves it byte-for-byte. Schema changes go through `model.rs` + the Python domain
dataclass together, never ad-hoc dict mutation.

---

## 5. Migration order (each slice keeps check.sh green, pipeline runnable)

1. ✅ scaffold + `domain/enums.py` + `domain/model_caps.py`; `pipeline.py` delegates.
2. ✅ wire enums into the ~9 stringly env reads via `Enum.coerce`.
3. ✅ `settings.py` Settings dataclass + composition root `SETTINGS`; knobs read it.
   Expanded to own the derived char budgets: `model`, `model_context_chars`,
   `safe_input_char_limit(_task0c)`, `single_call_char_limit`, `intermediate_char_limit`
   computed once in `from_env(model=...)` via the pure `domain.model_caps.safe_input_char_limit`.
   pipeline's `SINGLE_CALL_CHAR_LIMIT`/`_SAFE_INPUT_CHAR_LIMIT*` are single-sourced from
   `SETTINGS` (values verified byte-identical to the old inline math). `prompt_budget` now
   reads the Settings-sourced limit, so it no longer blocks on a config home.
4. `ports/` Protocols + `adapters/`:
   - 4a ✅ LLM / Extractor / Cargo adapters wrap real primitives (model_interact /
     extractor binary / cargo subprocess); ports corrected to the real path/cluster +
     subprocess seam; `ADAPTERS` built at the composition root. No call sites moved yet.
   - 4b ✅ Coverage / Workspace relocated into `adapters/coverage.py` + `adapters/workspace.py`.
     Cross-cutting deps (`_log`/`_emit`/`_cargo_env`/live `_current_cluster`/tool paths,
     `_git_managed`) are injected via `CoverageDeps`/`WorkspaceDeps` from the composition
     root; `pipeline.py` keeps thin delegating shims until call sites migrate (slice 5).
     Coverage/Workspace ports corrected to the real provider/git shapes. Bodies moved
     verbatim; parse-path unit tests (lcov/grcov-json) + GitWorkspace git test guard the
     transcription (no cargo needed).
5. migrate call sites onto the ports/adapters, then extract `application/tasks/*`:
   - 5a ✅ adapter call-site de-dup: the extractor `_*_via_extractor` fns, `_extract_skeleton`,
     and `_run_cargo` now delegate to `ADAPTERS` (the duplicated subprocess logic 4a/4b
     introduced is gone). `enumerate_mods` added to the Extractor adapter+port. `_run_cargo`
     keeps its legacy (rc, text) contract (bare message on rc=-1). `_ast_merge_files` left as-is
     (its diagnostic `_log` lines aren't on the merge port — revisit when merge gains a deps seam).
   - 5b ✅ extract `application/tasks/*` one task at a time behind an orchestrator.
     Scaffolded `application/` + `Orchestrator(settings, adapters)` (built as `ORCHESTRATOR`
     at the composition root). First task extracted: `tasks/genesis_audit.py` (LLM use case)
     — body verbatim, collaborators injected via a frozen `GenesisAuditDeps`; pipeline.py
     keeps a thin `task_genesis_audit` shim. Remaining tasks (task0*/1/2/3/4, task0c) follow
     the same pattern.
     Foundation step (de-ceremony): pure shared helpers move below the application layer so
     tasks import them instead of injecting them. Done so far → `domain/test_results.py`
     (`_parse_test_counts`, `count_passing_tests`, `validate_task3_output_strict`,
     `extract_test_output_from_file`, `OUTPUT_SEPARATOR`) and `domain/text.py`
     (`strip_markdown_fences` [aliased `_strip_markdown_fences`], `extract_code_content`,
     `trim_to_line`, `smart_truncate`). pipeline.py re-exports the same objects so all call
     sites are unchanged. This directly shrinks task3/task4's future deps record. Remaining
     Infra seams now exist → `pipeline_core/log.py` (settable `log`/`emit` facade, stdlib-
     logging pattern; sinks wired to `_log`/`_emit` at the composition root) and
     `pipeline_core/retry.py` (`with_retry`/`is_rate_limit_error`, config via `configure()`).
     `domain/llm_output.py` holds the model-response extraction (`extract_files`,
     `_extract_genesis`, `_looks_like_prose`, `_normalize_file_markers`, `FILE_PATTERN`,
     `GENESIS_DELIM_*`); pipeline.py re-exports the names it still uses.
     `pipeline_core/io.py` homes the `read`/`write` fs primitives (tasks still inject them
     for test control; non-test code imports them).
     Payoff: `GenesisAuditDeps` shrank 10→5 — `log`, `retry`, `extract_genesis`, and the
     genesis delimiters are now imported (the last also guarantees prompt delims == parse
     delims), leaving only the genuinely pipeline-bound `ask`/`read`/`write`/`model`/
     `verbosity_guard`. Tasks log, retry, parse model output, and read/write without a
     ceremony-heavy deps record.
     Config home now exists: Settings owns the char budgets (see item 3), so `prompt_budget`
     and the limit constants are single-sourced from `SETTINGS`.
     Extracted so far → `tasks/genesis_audit.py`, `tasks/task3.py` (Task3Deps, 11 injected:
     phase/cluster-load/cargo-env/parse-cmd/active-test-count/store-prompt/inline-retry/
     legacy-path/read/write/timeout; log+emit+test_results imported), `tasks/task4.py`
     (Task4Deps, 9 injected; budget/parse/extract + retry imported). pipeline.py keeps thin
     `task3`/`task4` shims binding `_TASK3_DEPS`/`_TASK4_DEPS` (built after `_cargo_env`).
     Bodies verbatim; fake-deps tests drive the legacy-fallback + all-pass (task3, subprocess
     monkeypatched) and full genesis-update (task4) paths without cargo/LLM.
     Extracted (Option 1 — orchestration moves to application/, regex parsers stay injected
     from pipeline.py so the clean layer is regex-free): genesis_audit, task3, task4,
     task0_project_profile/context (`task0_docs.py`), task0b_discovery. Incidental fence
     regex replaced by `strip_markdown_fences`; gen_tests filename regex kept behind an
     injected `_is_gen_tests_file` predicate. Each has a fakes-deps test.
     IMPORTANT distinction: some "tasks" are pure static PARSERS, not orchestration —
     `task0_cfg_scan` (Cargo.toml + cfg regex) and `task1_static_audit` (use-path scan) ARE
     the regex; under Option 1 they STAY in pipeline.py as Option-3 regex-replacement targets.
     Wrapping a parser in an application shim is empty ceremony — don't.
     Extracted, groups A–D (all green, fakes tests): task0_docs (profile/context),
     task0b_discovery, task1 (+`_task1_proc_macro`; `task1.py`), task2 family
     (task2/inner_loop/fix_loop; `task2.py`). Regex kept in pipeline.py behind injected
     predicates: `_is_gen_tests_file`, `_filter_proc_macro_test_files`,
     `_proc_macro_fallback_requirements`, `_is_proc_macro_crate`, `_count_rustc_error_codes`.
     ENGINE classes/helpers stay in pipeline.py (implementation, not orchestration):
     Task1Synthesizer impls, Task2Generator impls + `generate_fix_loop` + `_task2_fill_*`.
     Pure-parser tasks stay (Option-3 targets): `task0_cfg_scan`, `task1_static_audit`.
     Group E 🔄 PARTIAL — `task0c_augment_module` (the ~1316-line orchestrator) extracted to
     `tasks/task0c.py`: verbatim body behind a rebind-prelude (every collaborator localised
     from a frozen `Task0cDeps`, ~75 fields, body byte-for-byte unchanged). All 25 inline
     regex were factored into named pipeline helpers (injected) BEFORE the move so the clean
     layer is regex-free. SPECIAL injections: `subprocess` (the module — app never imports it),
     `get_current_cluster` (getter resolved at CALL time, the global is mutable),
     `coverage_provider`; `log`/`emit` imported. pipeline.py keeps a thin shim binding
     `_TASK0C_DEPS`. Fakes-deps smoke test drives the not-applicable early-skip path. green.
     STILL IN pipeline.py (Group E tail, ~640 lines, injected back via Task0cDeps): the task0c
     ENGINE helpers `_build_task0c_prompt_sections`, `_task0c_pragma_pass`,
     `_task0c_targeted_pragma`, `_task0c_mutation_pass`, `_task0c_retrospective`,
     `_get_task0c_history`, `_log_task0c_attempt`. Moving them needs the deps record to DROP
     them (not re-inject) so task0c.py is self-contained. The ~75-field `Task0cDeps` is itself
     a smell: a dependency object that large = relocated-not-decoupled. Lower value than slice 6.
6. 🔄 `domain/models.py` dataclasses replacing dict records at the seam.
   DONE: frozen slotted dataclasses mirroring `model.rs` field-for-field — `FnRecord`,
   `TypeRecord`, `FieldRecord`, `MethodRecord`, `ModRecord`, `DispatchFamily` — each with a
   tolerant `from_dict` (missing keys default, extra keys ignored, nested records parsed) and
   a `to_dict` that round-trips the exact wire shape (seam stays sacred, §4). The `Extractor`
   adapter + port now return typed records; `enumerate_types/mods/dispatch_families` flow
   typed end-to-end and their (bounded) consumers use attribute access (`_types_referenced_in_text`,
   `_format_type_for_prompt`, `_build_dispatch_family_section`, `_MOD_RECORDS_CACHE`).
   BOUNDARY DECISION: `FnRecord` is typed at the adapter, but the pipeline.py
   `_enumerate_fns_via_extractor` wrapper re-serialises via `.to_dict()` because FnRecord-as-dict
   is woven through the per-fn / per-impl FILL ENGINE (`stubs`→`_process_one_stub`, the
   `_collect_impl_blocks` block dicts, and a hand-built fallback stub dict of the same shape).
   Typing the engine through is an engine rewrite (violates "engines stay") and wants its own
   `Stub`/`ImplBlock`/`Frontier` domain models — a later slice, not this one.
   `Diagnostic` (2nd unit, DONE) — the cargo↔Python seam ({code,file,line,message,rendered} from
   `_cargo_diagnostics_from_json`) is now a typed record; consumers `_cargo_err_sig_from_diagnostics`,
   `_frontier_from_diags`, `_run_cargo_check_diag` + the inner-loop file-error counters use attribute
   access. It does NOT cascade into the fill engine (consumed into a frontier/sig, not stashed), so
   no `.to_dict()` boundary was needed (unlike FnRecord). Tests: `test_slice6_models` (round-trip +
   tolerant, incl. Diagnostic), `test_slice6_models_seam_wiring`. check.sh green.
   `Cluster`/`Module` (3rd unit, DONE) — `BuildCluster` + `ModuleAugmentState` relocated from
   pipeline.py to `domain/models.py` (mutable `@dataclass`es; `requires_nightly`/`record_pass`
   mutate). pipeline.py imports + re-exports them so all ~40 `BuildCluster` sites + the
   `ModuleAugmentState` construction are unchanged. The dead `BuildCluster.features` regex (0 prod
   uses, only a test) was rewritten regex-free (plain string ops) to keep the domain module
   regex-clean per the project rule; the cfg_true/cfg_false duplication folded into `_expand`.
   `import dataclasses` dropped from pipeline.py (those two classes were its only users).
   `Frontier` (4th unit, DONE) — the (file,fn)→{tests} reconstruction frontier is a mapping
   keyed by identity, not a record, so its domain form is a type alias
   `Frontier = dict[tuple[str,str], set[str]]` (domain/models.py), applied across the 15 TDD-driver
   signatures/locals. Pure annotation; mypy confirms type-identity.
   REMAINING (deliberately NOT done — engine change, needs a live pipeline run to validate):
   `Stub`/`ImplBlock`/`FillResult` + threading FnRecord through the fill engine. These dicts are
   ENGINE-INTERNAL (not seams): `stubs` threads through `_task2_fill_fns_one_by_one` (1157L) and
   `_process_one_stub` (544L) with Any-typed access mypy can't see and inconsistent keys
   (`"fn"` vs `"fn_name"`), and there is no behavioural test. Converting them as dataclass-then-
   `.to_dict()` at the producer would be ceremony (relocated-not-decoupled); the only correct form
   is full engine threading, which must be validated by a real run, not check.sh. Left for a
   dedicated engine pass.
7. 🔄 Rust split + clap + thiserror.
   DONE: `lib.rs` (2356L monolith) split into cohesive modules, each moved verbatim (the JSON
   seam stays byte-identical — guarded by the crate's 15 tests + the Python smoke):
   `model.rs` (serde record types — the seam, mirrors `domain/models.py`), `cfg.rs`
   (`CfgSet` + cfg-predicate parsing), `skeleton.rs` (skeletonise visitors), `enumerate.rs`
   (fns/types/mods/dispatch/feature-gate subcommands), `merge.rs` (cross-cluster syn merge).
   `lib.rs` is now ~305L = header + `mod`/`pub use` re-exports + the test module (public API and
   all call sites unchanged). Cross-module helpers are `pub(crate)`. `main.rs` rewritten with
   clap derive — flag names preserved byte-for-byte (they're part of the seam: the adapter shells
   `--enumerate-fns`/`--cfg-true`/`--merge-clusters`/… exactly); validated by running every exact
   adapter invocation. Crate bumped to edition 2024. `cargo build`/`cargo test` (15) green,
   warning-clean; functional seam output verified identical.
   DEFERRED: thiserror for the library error edge (anyhow only in the binary). It's a pervasive
   change — every `enumerate_*`/`skeletonise`/`merge_*` signature returns `anyhow::Result` — so it
   touches the whole lib surface for little architectural gain over the split; left as a focused
   later pass. `Cfg` newtype (§3) similarly deferred; `CfgSet` already encapsulates predicate eval.

## 5b. Migration plan — remaining slices (planned 2026-06-09, session: wipe-vs-resume fix)

What is still in `pipeline.py` (21.7k lines, 397 top-level defs). Biggest residents:

| Lines | Item | Target layer |
|---|---|---|
| 1346 | `main()` | application/orchestrator (slice 12) |
| 1204+544+495+283+245+599 | fill engine (`_task2_fill_fns_one_by_one`, `_process_one_stub`, `_phase_c_fill_impl_blocks`, `_run_phase_c_tdd`, `_task2_fill_groups`, `generate_fix_loop`) | application/reconstruct (slice 10) |
| 773+384+245+167 | `Task2Generator` family | application/generators (slice 11) |
| 486+254 | `Task1Synthesizer` family | application/generators (slice 11) |
| ~1200 total | `_build_*_prompt` + section formatters | prompts/ (slice 8) |
| ~400 | state file + wipe/stub/reattach lifecycle | adapters/state + application/workspace_lifecycle (slice 9) |
| 218+178 | DependencyGraph family | application (slice 11, tail) |

Slice order (rationale: verbatim-move slices first — safe between live runs; the engine
slice needs a dedicated live-run validation window; ceremony-free per §2):

8a. COMMONALITY-FIRST (do BEFORE the layer moves — turns slices 10–12 into mostly
   deletions). Standing rule: when N sites hand-roll the same operation, extract ONE
   general engine and parameterize; divergent copies are where the bugs live (evidence:
   the `allow_nonstub` no-op retry, the three fix-prompt sites with different context
   completeness, the twice-written `(file,fn)→container` map).
   - `FixLoop` engine (application/): the run-cargo → focus-errors → build-fix-prompt →
     ask → splice → re-run loop, parameterized by (target-locator, prompt-builder,
     splicer, progress-metric). Replaces ≥6 hand-rolled copies: `generate_fix_loop`
     (599L), per-fn loop in `_process_one_stub`, per-impl loop, post-pass test-retry,
     `_task3_inline_retry`, task0c fix pass. Same move as `refine_loop`, one level deeper.
   - `FnContext.gather(file, fn, error, records)` — ONE assembly of
     types/notes/macros/invariants/container/where-clause for a target fn, used by every
     prompt builder. Companion to slice 8's PromptContext: 8 makes a missing section
     visible, this makes it impossible.
   - `StallBudget` (domain/) — the improved/stall-n-of-m/floor/hard-cap state machine
     (~83 cap sites, 39 floor sites, ~20 stall sites today) as one ~40-line dataclass;
     post-merge fill, inner loops, task3 retry, improvement_loop consume it.
   - attempt-history recording — one `AttemptLog` shape feeding fix prompts,
     retrospectives, and model-notes extraction (22 scattered sites, 3 shapes today).
   Each engine: extract verbatim from the richest copy, port the other sites one per
   commit, fakes-deps test per engine. GATE like slice 10: the FixLoop port-overs touch
   the live engine — validate per-site against a live-run log, not just check.sh.

   DONE so far: FloorStall (3 sites), _fn_container_index + _gather_fn_fix_context
   (4 prompt sites + improvements: task3-retry gained notes/tests, P5.4 container filter),
   _tag_block (3 builders), fill-loop e2e harness (tests/test_e2e_fill_loop.py: success /
   recover / repeated-sig / floor-stall on the REAL engine), coverage ratchet in check.sh.

   ### FixLoop dedup plan (the ~500-line one; planned 2026-06-09)
   Inventory: TWO drivers already exist (`_improvement_loop`+LoopState in pipeline.py —
   used by generate_fix_loop & task3-inline-retry; `run_refine_loop` in pipeline_core) and
   TWO hand-rolled loops (A: per-fn inner loop in `_process_one_stub` ~300L; B: per-impl
   loop in `_phase_c_fill_impl_blocks` ~280L) plus C: the post-pass test-retry
   (~10396, `test_fix_attempt` budget, no revert — splices kept). End state: ONE driver.

   Shape: `FixUnit` protocol owning what A/B genuinely differ on; the driver owns what
   they share (attempt counter, FloorStall observe + stall/repeat exits, history record,
   `loop.iter` emit, recovered/exhausted note hooks):
     locate() → still-a-target?            (stub check / race detection)
     prompt(state) → str                   (initial vs fix; gather ctx inside)
     apply(response) → splice delta        (single splice + lock dance | multi-member)
     verify() → ok | (err_sig, count, excerpt)  (cargo gate + U2 test gate + acceptance)
     accept() / revert() / exhaust(history)     (annotated stub + retro | B3 partial keep)
   Subtleties that MUST survive (each pinned by a harness test before its port):
   _splice_check_lock acquire/release points (A); U2 co-dependency keep (compiles but
   sibling stub blocks test → accept); B3 (FR-029) non-regressing partial accept mid-loop
   (B); sig-change hatch attempt≥5 non-pub (A); race_lost vs splice_rejected; per-loop
   event namespaces (phase_c.per_fn / phase_c.impl.*); notes extraction on recovered
   (attempt>1) and exhausted; retros-cache guidance injection.

   Port order (one commit each, check.sh + ratchet green; live-run telemetry diff at the
   end of the series, at a run boundary):
   f1. extend the harness to drive loop B (scripted block records via
       `_collect_impl_blocks` seam): success / B3 keep / stall / repeated tests.
   f2. `FixLoop` driver + `FnFixUnit` — port A; existing 4 fill-loop tests must pass
       byte-identically; add sig-change-hatch + U2-routing tests.
   f3. `ImplFixUnit` — port B against f1's net.
   f4. fold C onto the driver (metric = failing-test count, no-revert policy flag).
   f5. ✅ `_improvement_loop` is now an ADAPTER over `run_refine_loop` (callback→
       RefineStrategy; pins in tests/test_improvement_loop.py). DECISION: two real
       shapes — `_drive_fix_loop` stays a sibling (outcome kinds + repeated-sig exit
       don't fit a metric strategy). Port CORRECTED two lag artifacts: attempts are
       re-measured immediately (was: pre-fix metric recorded one iter late → spurious
       iter-1 stall) and a stalled loop's final fix is measured instead of discarded
       (cost: +1 run_check on stall exits). Fix-counts per budget unchanged.
   f6. live anyhow run: diff loop.iter events + floor/stall log lines + fills-per-pass
       against the previous run before declaring the series done.
   Expected: A ~300→~80 (unit) and B ~280→~90, C ~120→~40, shared driver ~120 — net
   ≈ −450 lines and ONE termination policy for every fix loop in the pipeline.
8. 🔄 `prompts/` — MOVED (2026-06-10): `pipeline_core/prompts/` = `sections.py` (pure:
   `tag_block`, `format_impl_block_for_prompt`, `format_type_for_prompt`,
   `build_dispatch_family_section(family, *, single_call_char_limit)`, `RUST_GOTCHAS`+
   `gotchas_summary/for_error`, `TRAIT_CONTRACTS`+`trait_contract_for`,
   `extract_trait_from_container`), `per_fn.py` (`build_per_fn_prompt`,
   `build_per_fn_fix_prompt`), `per_impl.py` (`build_per_impl_prompt`), `runtime.py`
   (settable facade, log.py pattern, for the two impure collaborators that stay in
   pipeline.py: `_annotate_file_view` (regex) + `_promoted_error_rules` (run-state+I/O);
   wired at import in pipeline.py). pipeline.py keeps `_`-alias shims (all call sites +
   smoke pins unchanged); `prompts` added to the import-linter layers (application >
   prompts > ports). FIX exposed by the move: `extract_trait_from_container` rewritten
   regex-free — the old regex required whitespace after `impl` and stopped generics at
   the first `>`, so EVERY `impl<T> Trait for X` and nested-generic impl silently got
   no TRAIT_CONTRACT; pins in tests/test_prompts_package.py.
   BOUNDARY DECISION (stay in pipeline.py — selectors over pipeline state, moving =
   relocated-not-decoupled): `_types_section_for_fix` (regex relevance-match + _guard),
   `_build_task0c_prompt_sections` (regex + _guard + char-cap globals),
   `_filter_model_notes_for_prompt` (FR-file I/O + caches). The moved layer holds only
   pure renderers.
   REMAINING (deferred to a session with runnable pytest — pin-heavy, behavior-neutral):
   per-family frozen `*PromptContext` dataclass with NO field defaults, enumerating every
   injected section — makes a missing injection (the BoxedError TYPES_IN_SCOPE gap, the
   empty retry-prompt gaps) a VISIBLE hole in a record, not an absent kwarg. Requires
   rewriting every builder call site + smoke pin in one pass; gate on the full suite.
9. ✅ state + workspace lifecycle (2026-06-10) — `adapters/state.py` owns the
   pipeline_state.md store (`state_path/read_state/write_state/mark_phase/set_current/
   phase_valid/log_state_summary`, `PIPELINE_STATE_NAME`); bodies verbatim EXCEPT the
   line parser, rewritten regex-free (`_parse_phase_line`, semantics pinned against the
   old pattern in tests/test_state_adapter.py). `PipelineState` Protocol added to ports.
   pipeline.py keeps `_`-alias shims; `_commit_and_mark` (git orchestration) and
   `_source_hash` (what-counts-as-source policy, uses `_strip_gen_tests_decls`) stay.
   `application/workspace_lifecycle.py` owns the module-start save→maybe-wipe→stub
   sequence; THE invariant (wipe ⟺ the regenerating phase will rerun, session-59 bug)
   is codified as `may_wipe_regenerated` and `wipe_module_sources` refuses to wipe
   unless it holds (tests/test_workspace_lifecycle.py pairs each wipe with its phase
   guard). Filename regex stays behind the injected `_is_gen_tests_file` predicate.
   DEFERRED from this slice: the post-task2 gen_tests REATTACH block (main() ~20460) —
   cluster-engine logic over `_clusters`/origin-manifest/cfg-gates, R9.6-adjacent and
   awaiting live validation; extracting it needs a ~10-field deps record today
   (relocated-not-decoupled). It belongs to the engine pass (slice 10/12).
10. engine pass (deferred tail of slice 6 — the only slice that is NOT verbatim):
    `Stub`/`ImplBlock`/`FillResult`/`FixAttempt` domain models; thread `FnRecord` through
    (kills `"fn"` vs `"fn_name"`); split `_task2_fill_fns_one_by_one` into frontier
    selection / per-stub fill / fix loop / post-pass test-retry units under
    `application/reconstruct/`. GATE: validated by a live anyhow run compared on
    pass/fail/frontier counts per iter against the pre-refactor log — check.sh alone is
    insufficient (doc'd in slice 6). Schedule in a window where no run is active.
11. generator/synthesizer families → `application/generators/` (already ABC-shaped;
    move + constructor-inject; shims stay). DependencyGraph + WorkspaceScanner same
    pattern as the tail.
12. `main()` decomposition: module loop → `application/orchestrator.py`; `pipeline.py`
    ends as composition root + argparse + shims (<~1k lines). Only after 10/11 (it
    orchestrates what they define).
13. guardrails in CI: `import-linter` layer contract (§1 table); check.sh gains a
    pipeline.py line-count ratchet (fails if it GROWS); mypy strict on `pipeline_core/`.
14. Rust tail kept from slice 7: thiserror at the lib edge, `Cfg` newtype.

Standing rules for every slice: strangler-fig (move verbatim, shim, behaviour identical);
check.sh green per step; never land slices 10/12 while a pipeline
run is mid-flight (restart boundary = deploy boundary); no new regex in moved code —
factor regex behind injected predicates as in Group E.

#### R9.6 — orphan gen_tests siblings resurrected under a guessed cfg gate  ✅ SHIPPED (P1 propagation + P2 atomicity) + tests (full effect ●● awaits live run)
Resolves session-51's open "24-vs-23 manifest gap". Ground truth from the live workspace:

| evidence | finding |
|---|---|
| `.skeleton/_gen_tests_origin.json` | 23 entries (error.gen_tests_1–4) |
| `src/*.gen_tests_*.rs` on disk | 24 files (error.gen_tests_**5** is the extra) |
| `task0c-augmented-anyhow:src/error.rs` (pre-strip, human-inspect only) | declares gen_tests_1–4 only — **no gen_tests_5 decl** |
| `error.gen_tests_5.rs` | git-tracked, real coverage tests; nightly call `request_ref_backtrace` is internally `#[cfg(error_generic_member_access)]`-gated |

Root cause: `error.gen_tests_5.rs` is an **orphan** — task0c wrote the file + decl (pipeline.py:18381-18384, body
was balanced), but a later revert/strip path (session-51 destructive-block coverage gate, or a quarantine) removed
the **parent decl without deleting the sibling** (`_revert_gen_tests_pass` deletes both, but not every strip path
does). Bake correctly captured only the 4 declared decls. Post-strip, skeleton-C propagation (pipeline.py:15911-15919)
finds the orphan file, sees no decl, and **resurrects it under a guessed gate** via the silent
`_gt_origin.get(key, _gt_cur_attr)` — line 774 now carries the no_core fallback cfg, not a real origin. It avoids a
leak ONLY by luck (its nightly call self-gates internally); a nightly-only orphan without that internal gate WOULD
E0658-leak. It also adds test load that task0c never coverage-validated (it was undeclared = never compiled pre-strip).

| fix | where | scope |
|---|---|---|
| **(load-bearing) manifest is the authority.** When `_gen_tests_origin` is NON-empty (bake ran) and a sibling is absent from it → undeclared orphan → quarantine (`.rs.broken`) + loud `_log`, do NOT propagate under a guessed gate. Empty/absent manifest → keep today's legacy fallback (back-compat for pre-bake runs). | pipeline.py:15911-15919 (replace the silent `.get(key, _gt_cur_attr)`) | P1 |
| **(defense-in-depth) atomic decl-strip.** Every path that strips a `mod gen_tests_N;` decl from a parent must delete/`.broken` the sibling in the same step, so an orphan can't form. Audit the session-51 destructive-block revert + `_strip_gen_tests_decl_n` callers. | task0c.py revert paths | P2 |

Effect: count reconciles (23==23), no fabricated gates ever (kills the latent-leak class at its source, not just the
one lucky case), unvalidated test load dropped. Validation: ●● — a unit test drives propagation with a manifest
missing one on-disk sibling and asserts it's quarantined not resurrected; full effect wants a live run.
SHIPPED: P1 — `_propagate_or_quarantine_sibling` (manifest-authority; quarantines undeclared orphans to `.rs.broken`,
loud log; wired into skeleton-C propagation; legacy fallback kept for empty manifest). P2 — `_revert_gen_tests_pass`
now reconciles the sibling⟺decl invariant: after restoring the snapshot it deletes EVERY sibling whose decl the
restore dropped (the orphan root: a split/retry added extra siblings, the snapshot restore removed their decls but
only the one named sibling was unlinked). Tests: `test_propagate_or_quarantine_orphan_sibling`,
`test_revert_gen_tests_pass_reconciles_all_siblings`. Decisions taken: quarantine (not delete) for P1; both parts now.

Invariant: after every slice, `bash check.sh` passes and `pipeline.py` imports cleanly.

---

## 8. Open-source-readiness backlog (prioritized)

Grounded in a 3-lens review (senior Pythonista / Rustacean / architect). Each **group** is a
theme; each **step** is independently shippable behind a green `check.sh`. Legend —
Pri: P0 (do first) > P1 > P2.  Eff: S/M/L.  Risk: ●low ●●med ●●●high (●●● = needs a live
pipeline run to validate, not just check.sh).  Status: ⬜ todo · 🔄 partial · ✅ done.

Dependency order:  **R1 → R4 → {R2, R3, R5} → R6**  (gates first; tests unblock the engine
work; docs last). R5 (Rust) is independent and can run in parallel from the start.

### R1 — Automated idiom gates & CI  ✅ DONE  (P0 · the quality floor; cheap, permanent)
| # | step | Pri | Eff | Risk | status |
|---|---|---|---|---|---|
| R1.1 | ruff: broadened to `F,I,UP,C4,B,SIM` + `E711/E714/E741`; ~150 findings fixed (autofix + 22 manual: E741 renames, B904/B905/B007, SIM105). | P0 | M | ●● | ✅ ruff gates in check.sh |
| R1.2 | `pyproject.toml`: home for ruff + mypy config (`target-version=py312`). | P0 | S | ● | ✅ |
| R1.3 | mypy: `pipeline_core` held to **strict_optional** (caught + fixed the `gated_files` Optional bug); **mypy now HARD-GATES check.sh** (a blind autofix once renamed a loop var into a same-named `Thread` — only mypy caught it). | P1 | M | ● | ✅ |
| R1.4 | Rust: `cargo clippy -D warnings` (20 fixed → 0) + `cargo fmt`. | P0 | S | ● | ✅ |
| R1.5 | CI workflow (`.github/workflows/ci.yml`): py = ruff+mypy+invariants+smoke; rust = fmt+clippy(-D)+test. Linters pinned (ruff 0.15.15, mypy 2.1.0). | P0 | M | ● | ✅ |
| R1.1b | **B023** (82, loop-var capture) — DEFERRED & ignored-with-note: almost all in the threaded fill ENGINE; audit needs the R4 test net. Also `SIM102`/`SIM115` ignored (stylistic / legit long-lived handles). | P1 | M | ●●● | ✅ B023 audited+fixed (2026-06-10, the R4 net); SIM102/SIM115 intentionally ignored |
| R1.3b | mypy `disallow_untyped_defs` on `pipeline_core` — DONE (2026-06-10): ~31 defs annotated across 11 files (task fns, attempt closures, io/text/llm_output/retry; pass-through params honestly `Any`); enabled in the `pipeline_core.*` override so it gates. model_interact.py stays out of scope. | P2 | M | ● | ✅ |
| R1.x | import-linter contract (layer rules) → folded into **R2.3**. pytest adoption → **R4.1**. | — | — | — | — |

### R4 — Behavioral test suite  🔄 MOSTLY DONE  (P0 · unblocks safe engine refactors)
| # | step | Pri | Eff | Risk | status |
|---|---|---|---|---|---|
| R4.1 | **pytest** adopted; the drift-prone hand-rolled `main()` call-list replaced by a thin pytest delegator (one source of truth = collection); cargo coverage tests gated `@needs_cargo` (SMOKE_FULL=1); root `conftest.py` for imports; config in pyproject. check.sh + CI run `pytest`. | P0 | S | ● | ✅ 72 pass / 2 skip |
| R4.2 | `tests/test_domain.py` — 15 behavioural input→output tests for the pure parsers (`text`, `llm_output`, `test_results`). Lifted the **domain layer to 98.6%** (text/model_caps/enums 100%). | P0 | L | ● | ✅ |
| R4.4 | coverage gate: **`pipeline_core.domain` floored at 95%** in check.sh + CI (currently 98.6%). The application/ engine is intentionally not gated (mid-migration). | P2 | S | ● | ✅ |
| R4.3 | **DONE** (at the achievable granularity) — `tests/test_fill_replay.py`: characterisation tests for the fill engine's *deterministic core*. A faithful end-to-end replay of `_process_one_stub` (542L) is infeasible because it's a **closure capturing the whole `_task2_fill_fns_one_by_one` scope** + interleaves LLM/cargo/retro — extracting it from the closure IS the R3 work. So the net pins the transform that R3 must preserve: canned `<FN>` response → `extract_fn_bodies` → `_splice_fn_with_where` → filled stub (impl-method, where-clause-tighten, multi-member-one-response). No model, no cargo. | P1 | M | ●● | ✅ |

### R2 — Make the layering load-bearing  ✅ DONE (R2.2 closed 2026-06-16)  (P1 · the architecture was aspirational)
| # | step | Pri | Eff | Risk | status |
|---|---|---|---|---|---|
| R2.1 | **Orchestrator DELETED.** It was a fields-only dataclass with 0 method call sites (everything used the `ADAPTERS` global directly) AND it imported concrete `Adapters` (the sole application→adapters **DIP violation**). Removing it killed dead scaffolding + the violation in one move. A real driver is R2.2/R3, to be designed ports-first. | P1 | S | ● | ✅ |
| R2.3 | **import-linter** layers contract (`adapters > application > ports > domain`) in pyproject; gated in check.sh + CI. Passes (29 files, 39 deps) — the layer rule is now enforced, not just described. `log`/`retry`/`io` are foundational facades (not layers), so domain may import them. | P1 | S | ● | ✅ |
| R2.4 | `pipeline_core/__init__.py` declares the public API (`__all__`): the 10 domain record types + `Settings` + the 5 port Protocols. Adapters/tasks deliberately NOT re-exported (keeps `import pipeline_core` side-effect-free — no `model_interact` pull-in). | P2 | S | ● | ✅ |
| R2.2 | move `main()`'s task dispatch into a **real** ports-first driver, one task at a time. | P1 | L | ●●● | ✅ DONE 2026-06-16. P5 did the per-module TASK 1–4 loop (`ModuleReconstructor`). This session: the LAST inline task-dispatch in `main()` — the TASK 0c compiler-cluster augmentation loop (~167L `for _cluster in _clusters` + inner per-module `while`) → `application/cluster_augment.py:ClusterAugmentor.run(AugmentContext)` over 5 narrow role ports in `ports/augment.py` (AugmentTelemetry/ClusterScope/AugmentStore/AugmentEngine/CommitGate — ISP). `_current_cluster` global now mutated only through the `ClusterScope.set_active` seam. pipeline.py is the composition root: `_AugmentAdapter` (one object, every role; sentinels/engine/telemetry late-bound) + `_CLUSTER_AUGMENTOR`; `main()` builds `AugmentContext` (per-module dir/type/baseline-coverage + config) and makes ONE `run()` call. Behaviour verbatim (resume/recovery, pre-check zeroing, ceiling/hard-block/retry, last-cluster main-sentinel, cov_summary, empty-cluster no-op). Tests: tests/test_cluster_augment.py (8, in-memory fake adapter: target/retry→done/blocked/ceiling/pre-check-fail/multi-cluster/resume-recovery/empty). pipeline.py 15699→15629 LOC. Static GREEN: ruff, mypy-strict (89 files), import-linter (layers KEPT). RUN-gated (●●●) — USER validates the live augmentation path via microcrate smoke. |

### R3 — Engine decomposition + domain models  ⛔ BLOCKED ON LIVE RUNS  (P1 · dissolves the 72-field `Task0cDeps`)
Net now exists (R4.3 fill-replay characterisation), but every step below is ●●● — its only real
validation is a **live pipeline run on a crate** (check.sh cannot exercise the fill loop). The
first move is extracting `_process_one_stub`/`_task2_fill_fns_one_by_one` from their closures into
named, injectable units, under the R4.3 net + new characterisation tests added per extraction. To
be done in an environment that can run the pipeline, not by static edits alone.
| # | step | Pri | Eff | Risk | validation |
|---|---|---|---|---|---|
| R3.1 | domain models `Stub`/`ImplBlock`/`FillResult` (`Frontier` ✅ done) | P1 | M | ●● | round-trip tests |
| R3.2 | thread `FnRecord`/`Stub` through the fill engine; **shrink `Task0cDeps` 72→manageable** | P1 | L | ●●● | live run: fills still land |
| R3.3 | split `_task2_fill_fns_one_by_one` (1155L) + `_process_one_stub` (542L) into named units (gated by R4.3) | P1 | L | ●●● | live run + replay test |
| R3.4 | Group E tail: move the 7 `_task0c_*` helpers into `tasks/task0c.py`, **drop them from `Task0cDeps`** (don't re-inject) | P2 | M | ●● | invariant checker + live run |

### R5 — Rust library-grade error handling & polish  ✅ DONE  (P1 · independent)
| # | step | Pri | Eff | Risk | status |
|---|---|---|---|---|---|
| R5.1 | **thiserror** `ExtractError` enum (`Read`/`Write`/`Parse`, each carrying the path) in `error.rs`; the 10 `pub fn` now return `crate::Result`; `anyhow` is **binary-only** (`?` adapts `ExtractError`→`anyhow` for free). Seam output verified byte-identical. | P1 | M | ●● | ✅ 15 tests |
| R5.2 | unwrap audit: the "34" were mostly safe `unwrap_or*`; only 3 real `.unwrap()` (provably-infallible map removes) → documented `.expect(invariant)` (not `?` — that'd add an unreachable error path). `#![warn(clippy::unwrap_used)]` on the lib (tests `#![allow]`); `clippy -D warnings` green. | P1 | S | ● | ✅ |
| R5.3 | rustc reports **no dead `pub(crate)`** → split visibility isn't wrong; fine-grained private-vs-`pub(crate)` tightening is cosmetic, left as-is. `Cfg` newtype not needed (`CfgSet` encapsulates eval). | P2 | S | ● | ✅ (assessed) |
| R5.4 | crate-level `//!` rustdoc with intra-doc links to the public API; `cargo doc --no-deps` clean; CI gates broken links (`RUSTDOCFLAGS=-D warnings`). | P2 | S | ● | ✅ |

### R6 — OSS hygiene & docs  🔄 MOSTLY DONE  (P1 · launch gate)
| # | step | Pri | Eff | Risk | status |
|---|---|---|---|---|---|
| R6.1 | `README.md` — prepended a title + overview + **Architecture** section (layers, the Rust extractor, the seam, the strip boundary, the gate); kept the existing research-framing/setup content. | P1 | M | ● | ✅ |
| R6.2 | `CONTRIBUTING.md` — the gate (`check.sh` stages + the Rust gate), the enforced layer rules, the two hard rules (strip boundary, self-hosted-only), how to add a task, how to run. | P1 | S | ● | ✅ |
| R6.4 | `docs/SEAM.md` — the JSON wire contract: the record table (`model.rs` ↔ `domain/models.py`), an example `FnRecord`, the CLI subcommands/flags, why it's stable. | P2 | M | ● | ✅ |
| R6.x | **bonus**: `.gitignore` had a blanket `*.md` (public docs were force-added / invisible to git). Re-included README/CONTRIBUTING/CODE_ARCHITECTURE/ARCHITECTURE/`docs/*.md` so they track normally. | — | S | ● | ✅ |
| R6.3 | **LICENSE — DONE: Apache-2.0.** `LICENSE` (verbatim Apache-2.0) + `NOTICE`, holder `Copyright 2026 Virgilio Lino (Humanetes)` (real author; "Humanetes" is brand, not yet a legal entity — reassign if/when incorporated). README license section added. (Per-file source headers optional — skipped for now.) | P0 | S | ● | ✅ |
| R6.5 | continue the `pipeline.py` god-module split; on extraction **drop the misleading `_` prefix** from functions that become a module's public entry — DEFERRED (large/risky ongoing extraction, overlaps R2.2/R3). | P2 | L | ●● | ⬜ |

Carried forward from §5 (now folded into the groups above): Slice-5b Group E tail → **R3.4**;
Slice-6 engine models → **R3.1–R3.3**; Slice-7 thiserror/`Cfg` → **R5.1/R5.3**; the dead
Orchestrator → **R2.1–R2.2**.

### R7 — regex→syn: stop parsing Rust source with regex  🔄 IN PROGRESS  (P1 · Hard Rule: no regex on source)
~300 `re.*` calls in pipeline.py. **Most legitimately stay** — they parse model responses
(`<FILE>`/`<FN>`), cargo/pytest/ANSI output, or filenames (no structured parser exists). The
target is the subset that parses **Rust source**, which must go through the syn extractor.
Two confirmed design choices: (a) **temp-dir bridge** — the extractor is path-based, so for an
in-memory source string the Python side writes a throwaway `src/lib.rs` and enumerates over it;
(b) **grow the extractor faithfully** when a site needs facts syn doesn't emit yet (no behaviour loss).
| # | step | Pri | Eff | Risk | status |
|---|---|---|---|---|---|
| R7.0 | keystone: `_public_api_from_source` temp-dir bridge (string → enumerate types/fns/consts via syn). | P1 | S | ● | ✅ |
| R7.1 | `_extract_public_symbols`: was regex over structs/enums/traits/fns/consts → now syn (`enumerate_types` + `enumerate_fns` + new `enumerate_consts`). **Grew the extractor**: `ConstRecord` + `--enumerate-consts` (model.rs/enumerate.rs/main.rs + adapter/port/`ConstRecord` + Rust test). Behavioural test `test_extract_public_symbols_via_syn` (skips without the binary). | P1 | M | ●● | ✅ |
| R7.2 | `_PUB_ITEM_RE` — found it's **dual-purpose**: it parses both the regenerated Rust *source* AND the API-surface *listing artifact* (`pub trait X` with no body — NOT valid Rust, so syn can't parse it; regex is legitimate there). Migrated only the **source** side: new `_pub_items_from_tree` runs `enumerate_types`+`enumerate_fns` directly on the on-disk module (no temp-dir, no growth — the listing only ever has struct/enum/trait/fn). Kept `_extract_pub_items` regex for the artifact with a clarifying note. Test `test_pub_items_from_tree_via_syn`. | P1 | S | ● | ✅ |
| R7.3 | `_count_test_attrs_in_text` — **KEEP (justified)**: counts `#[test]`-family markers in *generated test code that may not be valid Rust* (mid-fill). syn requires a parseable file, so it would under-count; a robust marker count is the right tool here, not structural parsing. | P2 | — | — | ✅ keep |
| R7.4 | cfg-on-source (`_fn_cfg_predicates`, `_CFG_INLINE_RE`) → `enumerate_fns.cfg_predicates`. **●●● needs a live run — NOT batched.** Investigation found: (a) the regex `_fn_cfg_predicates` is **buggy** (its backward window over-captures preceding fns' cfgs); (b) `_eval_cfg_pred` detects operators with `s.startswith("all(")` — syn renders `all (…)` *with a space*, which would silently break detection → wrong cluster membership; (c) the extractor **duplicated** a free fn's cfg — **FIXED here** (`record_from_fn` trusts the stack; Rust test `fn_cfg_predicates_are_not_duplicated`). A faithful migration also needs the extractor to emit cfg in canonical no-space form (or normalise Python-side), then live-validate cluster assignment. | P1 | L | ●●● | 🔄 dup-fixed; (b) FIXED — `_eval_cfg_pred` parses operators on the normalized form, spaced `all (…)` pins in test_eval_cfg_pred; (a) FIXED — `_fn_belongs_to_cluster` takes `fn_cfg_predicates` from extractor records at all 3 record-bearing call sites (per-record, no window); regex `_fn_cfg_predicates` is extractor-absent fallback only (over-capture documented + pinned in test_fn_belongs_to_cluster_record_cfg_path). **Residual DONE 2026-06-15**: live-run validated on the microcrate (`logs/pipeline_micro_20260615-044637.log`) — 2 cfg-clusters (stable + `--cfg extra_cap`) assigned + reconstructed correctly, per-cluster test filtering 1/1 each, both 100% coverage (C7 PASS). R7.4 ✅. |
| R7.5 | `_IDIOM_*` in `_extract_crate_idioms` — **KEEP (justified)**: mines recurring *text patterns* (`Self::call`, struct literals, fn-ptr fields) to build a per-crate cheat-sheet, not structured facts. This is heuristic text-mining over filled source; an AST walk is possible but would be a large rewrite for a fuzzy hint, with no fidelity gain. | P2 | — | — | ✅ keep |
| R7.6 | `_condense_doc_attrs`/`_strip_attrs_for_view` (`_ATTR_OPENER_RE`), `_COMMENT_RE` in `validate_task2_output`, `_file_cfg_predicate_regex_fallback` — **KEEP (justified)**: the first two produce textual prompt **views** (syn re-emit would reformat the whole file and defeat the purpose); `_COMMENT_RE` is a non-emptiness heuristic; the last is the deliberate regex *fallback* for the syn path when the binary is absent. None extract structured facts. | P2 | — | — | ✅ keep |
### R8 — regex→structured parsers for NON-source formats  🔄 IN PROGRESS  (P1 · most regex bugs are here)
Principle: regex bugs cluster where a *structured format is scraped as text* (it drifts → silent mismatch).
| # | step | Pri | Eff | Risk | status |
|---|---|---|---|---|---|
| R8.1 | **Cargo.toml → `tomllib`** (stdlib). Replaced ~13 regex sites (package name, `[lib] proc-macro`, `[[bin]]`, `[dependencies]`, `[features].default`) with `_load_cargo_toml`/`_cargo_*` helpers. Handles comments/inline-tables/quoting the regex got wrong; test `test_cargo_toml_parsed_with_tomllib`. | P1 | M | ● | ✅ |
| R8.2 | test-result counting → structured — **NOT DONE (investigated, declined)**: stable Rust has **no** non-nightly structured test output (`--format json` needs `-Z unstable-options`); nextest's libtest-json is experimental. The text summaries (`test result: N passed`, pytest `N passed`) are stable de-facto contracts. Forcing JSON would couple to nightly/experimental — *more* fragile. The real cargo bug source (compile diagnostics) is already on JSON (`_cargo_diagnostics_from_json`). | P1 | — | — | ✅ declined w/ reason |
| R8.3 | finish compile-error text stragglers (`_COMPILE_ERROR_RE`) onto the existing `_cargo_diagnostics_from_json` where the caller has structured diags (legacy text-only fallback can stay). **DONE for the fix-loop dispatchers**: `_compile_error_frontier` (structured-first, text fallback; pinned in test_compile_frontier.py) feeds `_do_fix_per_fn`/`_do_fix_per_impl`; `_attribute_failures` keeps text parse (input is arbitrary test output, no diags available). | P2 | S | ● | ✅ |
| R8.4 | coverage `%` text-scraping → the coverage adapter's JSON/lcov (mostly already structured). | P3 | S | ● | ⬜ |
| R8.5 | model-protocol regex (`<FILE>`/`<FN>`) → tolerant state-machine parser (NOT model-JSON — local models adhere poorly). Only if model-output bugs recur. | P3 | M | ●● | ⬜ |

Outcome: every Rust-source regex is now accounted for — **migrated** (R7.1 `_extract_public_symbols`, R7.2 source side of `_PUB_ITEM_RE`), **kept with justification** (R7.3/R7.5/R7.6 — text-mining / display-views / heuristics / fallback, where syn is the wrong tool), or **flagged ●●● live-validation** (R7.4 cfg, with its real bugs documented and the extractor dup fixed). The "no regex on source" rule means *no regex for structured fact extraction*; text transformation/mining over source legitimately remains.
Note: syn-bridge sites depend on the extractor binary at runtime; their tests skip when it's not
built (CI Python job). Enumeration is covered by the Rust crate's tests in CI.

### R9 — convergence bugs surfaced by the session-51/52 live run  ✅ R9.1/R9.2 offline-validated via R10 (`tests/test_e2e_build_fix.py`, 2026-06-15) — BUILD mode + keep/revert both branches; R9.3 tested; TEST/KEEP paths also live-confirmed (git d3765d7)  (P0 · these cause the iteration spin)
Live run `pipeline.py=289f0a38 git=3d74425 --clean` (started 13:00 UTC). Leak fix (origin-cfg #4 + guard
#5) VALIDATED — 0 `E0658`, 0 `CLUSTER LEAK` across the whole run. But the run never converges: it loops
t0→t3→t4→regen because the pipeline cannot distinguish **"did not compile"** from **"tests ran"**.
Root theme (R9.1+R9.2): compile-state is inferred from test-result text, which is empty on a build failure.
R9.3 is independent (scratch-dir pollution). Dependency order: R9.3 → R9.1 → R9.2 (fix file set first so a
build failure is a real signal, then route it, then stop the retry trusting it).

| # | bug | root cause | fix SHIPPED | where | status |
|---|---|---|---|---|---|
| R9.1 | build failure had no fix path → TASK 4 (genesis analyzer) → genesis churn → full regen | inline retry **bailed at the gate** (`if not failed: return`) — a non-compiling tree emits 0 `FAILED` lines so it never even tried; `task2_fix_loop` is test-failure-driven | `_task3_inline_retry` now has a **BUILD mode**: when 0 tests FAILED, run `compile_cmd` (`<test_cmd> --no-run`) through `_run_cargo_check_diag`, attribute error diags→fns via `_frontier_from_diags`, re-splice per-fn (shared `_apply_per_fn_fixes`, which **skips frozen tests/ + gen_tests**). Metric = compile-error count (lower=better). Terminating recurse: return changed output only on strict error-count decrease, else the unchanged input → no infinite re-verify. Still-broken after this → TASK 4 fallback (documented limitation) | `pipeline.py:_task3_inline_retry` | 🟡 TEST-path live-validated, BUILD-branch unexercised (2026-06-15, micro run `logs/pipeline_micro_20260615-210823.log`, git d3765d7): `[task3-retry] entering inline retry: 1 failing test(s)` → `tests now PASS after 1 iter(s)` → 30/30 across 2 clusters. The inline-retry entry/recurse/terminate machinery is confirmed live on the TEST-failure path; the **BUILD mode** branch (0 FAILED → compile-error-driven re-splice) did NOT fire this run because no tree was ever non-compiling at task3 (every phase-c/tdd iter compiled: rc=101 with pass/fail counts). **BUILD branch now OFFLINE-VALIDATED (R10, 2026-06-15): `tests/test_e2e_build_fix.py`** drives `_task3_inline_retry` through the scripted-cargo+model harness — `test_build_mode_no_reproducible_diags_bails` (0 FAILED + clean compile → bail, model never called), `_worsened_reverts_from_snapshot` (compile errors 1→2 → revert, tree restored to stub), `_progress_keeps_and_reruns_task3` (2→1 → keep spliced tree, return for task3 re-verify). The `build-failure mode` / `build-fix worsened … reverting` / `build-fix progress … re-running TASK 3` log branches all execute. The "needs a non-compiling tree" gate is retired — forced deterministically. |
| R9.2 | inline retry kept a tree it just broke ("80→0 failing, keeping partial progress") | keep/revert used only `len(_parse_failed_tests(last_out))`; a non-compiling tree emits no `FAILED` lines → count 0 → read as success | keep/revert is now **compile-state aware**: `broke_build = init_ran and not final_ran` (via `_parse_test_counts` pass+fail>0); if the pre-fix tree ran tests and the post-fix tree no longer does → **revert from snapshot** (non-compiling is strictly worse than N failing). BUILD mode reverts when final error-count > initial | `pipeline.py:_task3_inline_retry` (post-loop) | 🟡 KEEP-path live-validated, revert-branch unexercised (2026-06-15, git d3765d7 micro run): the healed 30/30 tree was correctly KEPT (no false revert, no "kept a broken tree" regression). The `broke_build`/`final error-count > initial` **revert** branch did NOT fire — the post-fix tree always ran tests (compile-state never regressed). **revert branch now OFFLINE-VALIDATED (R10, 2026-06-15):** `tests/test_e2e_build_fix.py::test_test_mode_raised_failures_reverts` (retry raises failing count 1→2 → `result 2 > initial 1 — reverting from snapshot`, tree restored) + `_partial_progress_kept` (2→1, still compiles → keep, no revert) + `_pass_returns_true`. BUILD-mode worsen→revert covered by the R9.1 row's `_worsened_reverts_from_snapshot`. The compile-state-aware keep/revert decision is now exercised both ways deterministically; the broke-build (`init_ran and not final_ran`) sub-branch remains the one path a scripted test can't naturally stage but is the same revert sink. |
| R9.3 | task0b ingested 40 phantom `.rs` from scratch dirs → budget exhausted, real source dropped, TASK 4 at 99% | both rglob loops walked all of `project_dir` incl. `.skeleton/` + `.skeleton-filled/` (own `src/` + nested `target/.../out/*.rs`); `target_dir` guard only caught top-level `target/` | new `_iter_source_rs(project_dir)` prunes any path with a `_NON_SOURCE_DIRS` component (`.skeleton`, `.skeleton-filled`, `target`, `.git`); applied to BOTH the hash loop and the content loop | `task0b_discovery.py` | ✅ shipped + test |

Validation: R9.1/R9.2 are ●●● — only a live pipeline run exercises the build-failure → fix → re-test path
(check.sh cannot). R9.3 has an offline test (`test_task0b_iter_source_rs_prunes_scratch_dirs`). After the fix
the same crate run must reach a stable TASK 3 verdict (real pass/fail count, no `INCOMPLETE`→`no_tests`→`build`
oscillation) and stop regenerating from scratch each iteration.

R9 residual (SHIPPED, ✅ live-validated 2026-06-15, git d3765d7 micro run): the inline retry ran in the
2-cluster aggregate context (`TASK 3 aggregate … across 2 cluster(s)`) and healed+kept (`[task3-retry] tests
now PASS after 1 iter(s)` → 30/30) — the cluster-threaded retry path executed end-to-end without env/command
mismatch. (A cluster-cfg-SPECIFIC compile error, e.g. nightly-only, still hasn't been provoked, but the
cluster-aware plumbing itself is confirmed exercised.) Description retained: inline retry is now **cluster-aware** — task3 threads the primary
failing `BuildCluster` into `_task3_inline_retry`, which rebuilds the cluster command (`cargo +{toolchain} test
--no-fail-fast`) and overlays `cluster.env_extra` over `_cargo_env()` (mirrors task3's own per-cluster
invocation) for every run_check / compile-check / fix, instead of the stale global `_cargo_env()`. A
cluster-cfg-specific compile error (e.g. nightly-only) now reproduces under the right env → BUILD mode can
attribute + fix it. Prompts also carry the real cluster name (`_cluster_label`) instead of the literal `"task3"`.

#### R9.5 — TASK 4 anti-corruption falsely stalls build-failure genesis updates  ✅ SHIPPED + test (full propagation ●● awaits live run)
Correction to the earlier framing: TASK 4 is NOT missing a build path — `task4.py:196-234` (`_is_build`) already
feeds the compiler output and asks for general structural-methodology rules. The bug is the **validator**.

| aspect | finding |
|---|---|
| symptom | legitimate build-failure genesis updates flagged `genesis_stall` → coverage_strategy.md NOT regenerated → lesson never reaches TASK 0c/1 prompts → next iter regenerates identically → spin |
| root cause | `validate_task4_output` (pipeline.py:3097) is **failure-type-agnostic**. Line 3112-3115 requires the new genesis text to share an identifier (`_extract_identifiers`) with `test_output`. For a build failure `test_output`=compiler errors (project-specific idents: `request_ref`, `Backtrace`, type names), but the build PROMPT forbids referencing specifics ("not 'always generate src/lib.rs' but 'for any module system…'"; "Never mention specific tests, files"). A correct general update → empty intersection → false `genesis_stall`. The check is a TEST-failure heuristic contradicting the build prompt. |
| consequence of stall | does NOT revert genesis (model already wrote it); it ONLY skips the coverage_strategy regen (21321-21329) + records `genesis_stall` in the failure log. So the lesson is silently dropped. |
| fix (proposed) | thread `failure_type` into `validate_task4_output`; for `failure_type=="build"` **drop the identifier-intersection requirement** (incompatible with the build prompt's general-rules mandate). KEEP stall (no-change) + shrank (removed-content) + dedup (verbatim-paragraph) checks — those are real corruption signals regardless of type. Single call site (pipeline.py:21313). |
| why not a positive build-signal | requiring structural keywords (module/layout/entry-point/compile…) would just reintroduce the same false-positive class against a heavily-constrained prompt. For build failures, stall/shrank/dedup are the honest anti-corruption set. |
| validation | ●● — a unit test can drive `validate_task4_output(..., failure_type="build")` with a general (identifier-free) update and assert it's accepted; full propagation (coverage_strategy regen → fewer build spins) wants a live run. |

### R10 — e2e validation harness: kill the "●●● await live run" tax  ✅ L0+L1+L2 SHIPPED, L3 wired (nightly entrypoint); L4 = background release gate  (P0 · the dev-cycle bottleneck)
Problem: every orchestration change is tagged ●●● and gated on a live run, but the only live target is
**anyhow** — huge AND it triggers the unconverged-cluster bugs, so a single-module run ran 5h and stalled
(session 57). Root mis-classification: most ●●● items validate **orchestration logic** (reacts to model+cargo
*output*), not **model quality** — they don't need a live model at all. Split validation into layers, fastest
first; only the genuine "does the model converge" question pays for a model, and never on anyhow.

| layer | what | speed | model? | cargo? | status |
|---|---|---|---|---|---|
| L0 | `check.sh`: ruff/mypy/import-linter + fake-transport (`test_llm_controller`) + fill-replay (`test_fill_replay`) | sub-sec | no | no | ✅ have |
| L1 | **scripted-model integration** + **fault-injection fixtures**: `tests/harness.py` (scripted model+cargo+attribution seams) drives each failure mode deliberately and asserts the routing/keep-revert/cap decision. Suites: test_e2e_{fill_loop,impl_loop,test_retry,routing,stub_routing,u2_gate} + **test_e2e_build_fix.py (R9.1 BUILD mode + R9.2 keep/revert, 2026-06-15)** + test_compile_frontier | seconds | scripted | fake | ✅ have |
| L2 | **cassette replay**: replay a recorded real run — `ask()` hits a `{prompt_sha→response}` cassette, cargo memoized by tree-hash. A prompt-changing edit shows **cassette MISSES at exactly the altered prompts** = precise blast radius, re-record only those | minutes | recorded | memoized | ✅ have (R10.3 `tests/cassette.py`+`tests/replay.py`, R10.4 `tests/cargo_cache.py`; offline tests green) |
| L3 | **micro fixture crate, REAL model**: purpose-built 1-module/2–3-fn crate that *converges in minutes* — the "smoke in tmp" reborn as a committed fixture. Validates model-in-loop happy path | minutes | live | real | ✅ crate + run (`run_microcrate.sh`, SMOKE GREEN/RED exit-coded) + **nightly entrypoint `nightly_l3_smoke.sh`** (R10.5: unattended, self-skips when vLLM down / pipeline in-flight, appends verdict to `logs/l3_nightly.ledger`; LOCAL cron — L3 needs the GPU box's live model, no cloud runner has it) |
| L4 | full anyhow | hours | live | real | ⬜ release gate, background only |

Seam + seed (both verified to exist, session 58):
- **Injection seam = `pipeline._ask_raw`** (pipeline.py:596). `ask()` does all context-guard/budget work then calls the rebindable module global `_ask_raw = model_interact.ask`; a harness swaps it → scripted/cassette responder with zero changes to model_interact / controller / transport. One seam serves L1+L2.
- **Cassette seed already on disk.** `logs/events.jsonl` (7.9k `llm.request` / 7.7k `llm.response`) + `logs/prompts/` (content-addressed sha256 store of **both** prompts and responses). A builder walks events, pairs request→response, resolves shas to `logs/prompts/{sha}.txt`. No re-run needed to seed.

| # | step | Pri | Eff | Risk | status |
|---|---|---|---|---|---|
| R10.0 | **response-correlation one-liner** ✅: `llm.response` emit (pipeline.py:599) now carries `prompt_sha` (= the request's `_prompt_meta["sha256"]`), so cassette pairing is exact regardless of parallelism. Older recordings without it fall back to emission-order pairing (exact at p=1). | P0 | S | ● | ✅ |
| R10.1 | **scripted-seam harness** `tests/harness.py` — `install_scripted_model` (`_ask_per_fn_body`/`_ask_raw`), `install_scripted_cargo` (`_run_cargo`/`_run_cargo_check_diag`/`_frontier_from_diags`), `install_scripted_attribution` (`_attribute_failures`), `diag()` builds real domain Diagnostics; scripted values = constant/list/callable. **Micro fixture crate** ✅ `tests/fixtures/microcrate/` — deps-free, free-fn + `Option` parser + stateful `Counter` type + `Result`/error-enum, 6 tests (3 unit + 3 integration), `cargo test` 0.00s. Health-gated by `tests/test_microcrate.py` (SMOKE_FULL=1). | P0 | M | ●● | ✅ harness + crate (full-pipeline L3 run = R10.5) |
| R10.2 | **fault-injection cases** retiring the ●●● tax — `tests/test_e2e_routing.py`: R9.1 BUILD-mode fix path; R9.2 keep/revert in **both** BUILD mode (error-count worsens→revert / compiles→keep) and TEST mode (broke_build: ran→not-ran→revert). `tests/test_e2e_budget.py`: W3/W4(e) — sentinel-override vs explicit-bound, ctx-shrink, thinking-reserve-on-top. `tests/test_e2e_scheduling.py`: R11.3 determinism — extracted the pure level-grouping out of the fill closure into `_group_priority_levels(stubs, leaf_name_counts)` (engine-decoupled: plain data in/out; the call site is unchanged) and pinned equal-ref-count tiers, callee-before-caller order, totality, idempotence. cfg-filter already had `test_cluster_filter_type_methods_drops_cfg_dead_methods` (session 56). | P0 | M | ●● | ✅ |
| R10.3 | **cassette builder + replay runner** ✅: `tests/cassette.py` — `build_cassette` (events.jsonl + prompts/ → `{prompt_sha→response}`, prefers R10.0 `prompt_sha`, falls back to order-pairing; product/pipeline_sha filter), `install_cassette` (patches `_ask_raw`; strict→`CassetteMiss` or lenient→""; records misses = blast radius). Offline test `tests/test_cassette.py` (synthetic seed). Verified on real log: 128 entries from the 789ba6c p=1 tail. NOT yet wired to a full-run replay harness (needs the L3 fixture-crate run or cargo memoization R10.4). | P1 | M | ●● | ✅ + full-run wiring (2026-06-15): cargo-cache JSON persistence (`save/load_cargo_cache`) + `tests/replay.py` (`build_replay`/`install_replay` compose the cassette + cargo memo, both seams patched at once); `tests/test_replay_harness.py` (5). R3 now has an offline validation path. |
| R10.4 | **cargo memoization** ✅: `tests/cargo_cache.py` — `tree_hash` (digests `src/`+`tests/` *.rs) + `CargoCache` (`(tree_hash,cmd)→(rc,out)`, record/replay, hit/miss counters) + `install_cargo_cache` (wraps `_run_cargo`). A tree-changing fill → new key (real miss, never a stale hit). Offline tests `tests/test_cargo_cache.py`. | P1 | M | ●● | ✅ memo (full-replay wiring with R10.3 = ⬜) |
| R10.5 | L3 micro-crate as the on-demand real-model convergence smoke (nightly/manual, not per-change); anyhow demoted to L4 release gate. Crate + health gate exist (R10.1); **the full real-model pipeline run against it is DONE** — repeatedly green (`logs/pipeline_micro_20260615-155218.log`, git eb8807d: SUCCESS iter 1, 30/30, 2 clusters, C5/C7/C10 PASS). **Nightly wiring DONE 2026-06-16: `nightly_l3_smoke.sh`** — unattended entrypoint that runs `run_microcrate.sh` (full, from-scratch) and appends a one-line verdict (`<UTC> GREEN/RED/SKIP git=<sha> dur log`) to `logs/l3_nightly.ledger` for trend tracking; exit-coded for a scheduler; self-skips (exit 0, ledger SKIP) when vLLM is down or a pipeline is in-flight so a scheduled invocation never clobbers a real run. LOCAL by design (L3 needs the GPU box's live vLLM; cloud/remote schedule routines are forbidden) — the user adds a crontab line (documented in the script header). | P2 | S | ●● | ✅ run + nightly entrypoint (user installs the local cron line) |
| R10.6 | **flow auditor** `tools/flow_audit.py` (2026-06-11): mechanical verdicts over events.jsonl + logs/prompts/ for the FLOW invariants unit tests can't see — C1 repo-spec in coding prompts after t4, C2 genesis-line propagation, C3 floor monotonicity (ratchet-aware), C4 plateau alarm, C5 no-wipe, C6 ratchet sanity. Run-scoped via last `PIPELINE STARTED`. Validated on the session-63 live run (caught C1/C4/C5 mechanically = the manual archaeology). Post-run step for microcrate (hint in run_microcrate.sh) AND anyhow. Pins: tests/test_flow_audit.py. **Self-audit wired**: core moved to `pipeline_core/audit.py` (tools/flow_audit.py = CLI shim, re-exports); `pipeline.py --audit` runs it at every iteration boundary + every `AUDIT_INTERVAL_SEC` (default 900, daemon thread, read-only) → `[audit]` log lines + `flow_audit.report` events; monitor renders the latest as `audit: C1✓ C2! …` + first-FAIL evidence (`_flow_audit_rows`). Run scope = `_RUN_STARTED_ISO` captured at init_log. NEXT consumer: the scripted doc-loop scenario (model answers correctly only when the t4 lesson reaches its prompt) — needs R2.2 loop extraction or R10.3 full-run wiring. | P1 | M | ● | ✅ |

Reclassification: R10.1+R10.2 landed (session 58) — the logic ●●● items (**R9.1, R9.2, W3, W4(e)**, session-56
cfg-filter) are now covered by L1 deterministically, no live run needed. Remaining ●●●: W1/W2 vLLM parity +
scheduling determinism (need R10.3 cassette or the L3 micro-crate); "does the local model converge on real code"
stays genuinely live, on L3's micro-crate, not anyhow.
BUG FOUND + FIXED (session 58, via R10.2 harness): `_apply_per_fn_fixes` (pipeline.py:17351) called
`_splice_fn_body` WITHOUT `allow_nonstub=True`, so the inline build/test retry could only re-fill
`unimplemented!()` stubs. But a stub COMPILES — a build/test failure always comes from a fn with a wrong,
already-filled (non-stub) body, which `_splice_fn_body` then refused → `no_fns_spliced` → abort. So R9.1's
inline retry was a **silent no-op for exactly its intended case**. Fix: pass `allow_nonstub=True`, matching every
other fix-loop site (17001/17100) and the `_splice_fn_body` docstring. Pinned by
`test_build_failure_respices_nonstub_body` — the harness's first real catch (a live-run-only bug before R10).

### R11 — phase-c determinism & cluster-correctness (sessions 55–56)  ✅ SHIPPED; parallel-determinism + 2-cluster correctness LIVE-VALIDATED 2026-06-15 (R11.2 no_core convergence still ●●● anyhow-gated)
Three changes from the convergence-diagnosis sessions, undocumented until now. R11.2 is **uncommitted** at
session-57 handover (commit message written, user commits); R11.1 + R11.3 are in `789ba6c`.

| # | change | root cause | fix | where | status |
|---|---|---|---|---|---|
| R11.1 | **stream.log thread-local race fix** | under `PHASE_C_PARALLELISM`>1 the W1/W2 backends shared one stream-log file handle → garbled token-salad interleave | `_sock_*` made thread-local: workers write `logs/stream.<threadname>.log`, the main thread keeps `logs/stream.log`. `_sock_open`'s `open("w")` truncation is per-handle so each stream is clean | model_interact.py / pipeline.py stream sink | ✅ committed (789ba6c) |
| R11.2 | **cluster-aware TYPES_IN_SCOPE (cfg-filter dead methods)** [Hard Rule 8] | TYPES_IN_SCOPE advertised EVERY method regardless of cluster cfg → in `anyhow_no_core_error` the model reached for std-gated ctors (`construct_from_context`, `new`, `from_boxed`) that are cfg-dead → trait-bound error every attempt; the correct `construct_from_display` was buried past the 10-method cap | extractor: `MethodRecord.cfg_predicates` = enclosing impl/trait cfg ∪ method cfg (impl- and trait-method paths). `models.py`: schema + round-trip. `pipeline.py`: `_cluster_filter_type_methods` drops methods whose cfg is definitively False for the cluster (undecidable kept = conservative, matches `_fn_belongs_to_cluster`), applied at both `_crate_type_records` sites. `_eval_cfg_pred` now **normalizes** comparison sets (else spaced `feature = "X"` never matched → silent no-op on the exact anyhow case) | model.rs, enumerate.rs, domain/models.py, pipeline.py (~9059, ~9582) | ✅ in-tree 2026-06-15: `_cluster_filter_type_methods` at 4 call sites; live-validated by the R7.4 2-cfg-cluster microcrate run |
| R11.3 | **priority-level fill batching** [Hard Rule 9] | phase-c per-fn dispatch drained the whole frontier with a free-for-all queue → nondeterministic completion order → fill A changes B's file-context → unreproducible | `stubs` is leaf-first sorted; equal-ref-count stubs grouped into LEVELS; each level runs `min(P, level_size)` workers with a **barrier between levels** → callee level lands on disk before caller level → deterministic, scheduling-independent | pipeline.py:~10260 | ✅ committed (789ba6c) |

Residual: within-level snapshot race remains under p>1; `PHASE_C_PARALLELISM=1` (uncommitted `.env`) removes it
entirely during diagnosis. R11.2's full convergence effect (no_core impls reaching "tests pass" instead of "floor
stalled → reverted") is ●●● — exactly the kind of item R10's L1/L2 would let us validate without a live anyhow run.
Extractor binary must be rebuilt for R11.2 (`cargo build --release`); pipeline reads it live.
**LIVE 2026-06-15** (`logs/pipeline_micro_20260615-155218.log`, git eb8807d): R11.1+R11.3 confirmed — C10 parallel-streams PASS (no garbled interleave under p>1, every overlap on a distinct thread), priority-level barriers produced deterministic leaf-first fill (phase_c.pass.start/done ×10/11), 2 cfg-clusters (stable+extra_cap) reconstructed correctly with per-cluster test filtering (13 + 17), SUCCESS iter 1. R11.2's no_core "tests-pass-not-floor-stall" effect is anyhow-specific → still ●●●.

### R12 — skeleton emitted as raw token-stream → phase-c splice mangles bodies  ✅ FIXED (microcrate-validated; anyhow effect ●●● awaits live run)
**The microcrate L3 smoke (R10.1) earned its existence on first use** — a 5-min run surfaced the bug that 5h anyhow runs (sessions 55–57) could not isolate.
| aspect | finding |
|---|---|
| symptom | phase-c marked the *trivial* fixture fns (`add`=`a+b`, `bump`, `parse_pair`) `GENUINELY_HARD` and reverted; only converged because task3's inline retry (a different splice path) rescued all 4 |
| evidence | failure artifact: model returned a CORRECT body `<BODY>a.wrapping_add(b)</BODY>`, but the on-disk stub was `pub fn add (a : i32 , b : i32) -> i32 { :: core :: unimplemented ! () }` on ONE line; post-splice → `… -> i32 { :: ` + "unclosed delimiter" (body dropped) |
| root cause | `skeleton.rs` emitted the skeleton via `render_tokens(&quote!(#ast))` = `TokenStream::to_string()` — a naive token dump (`{ :: core :: unimplemented ! () }`, `# ! [doc = …]`, whole file ~one line). The text-based per-fn splicer (`_locate_fn`/`_splice_fn_body`) miscomputes byte offsets on that format → drops the model's body. The skeleton's doc comment literally contains `unimplemented!()`, compounding locator confusion. **This is the phase-c non-convergence ("floor stalled → reverted") root cause sessions 55–57 chased.** |
| fix | `skeletonise` now emits via `prettyplease::unparse(&ast)` (already a dep; `merge.rs` uses the same path) with FR-033 `normalize_doc_attrs` first (multi-line `//!`→one `//!`/line). Output is proper multi-line Rust: `pub fn add(a: i32, b: i32) -> i32 {\n    ::core::unimplemented!()\n}`. Deleted the buggy `render_tokens` + its `quote`/`TokenStream` imports; the two `#[cfg(test)]` callers in `lib.rs` switched to `prettyplease::unparse`. Binary rebuilt. |
| validation | extractor: 17 tests + clippy `-D warnings` green; skeletonised fixture verified formatted, **0 `{ ::`**. |

#### R12.1 — per-fn splice indexed a char `str` with BYTE offsets (exposed by R12)  ✅ FIXED + microcrate-validated
The R12 fix made the skeleton clean but the next microcrate run STILL corrupted trivial fns — a **second, independent bug** the clean formatting exposed.
| aspect | finding |
|---|---|
| symptom | filling `add` (model body `a.wrapping_add(b)`, correct) corrupted the file: `add` lost its `}` (unclosed delimiter) and the NEXT fn's `/// Parse …` became `/ Parse …` → `error: unknown start of token: \`` (its backticks now bare code) |
| root cause | the syn extractor emits **byte** offsets (`body_byte_start/end`), but `_splice_fn_body_at` (and `_splice_fn_with_where`'s where-path) read `read_text()` → a `str` indexed by **characters**. The skeleton's doc comments contain em-dashes (`—` = 3 bytes / 1 char), so byte-offset > char-index by 2/em-dash → the slice ran past the body, eating `}` + the `//` of the next `///`. Self-reproduced offline: `bytes[666:696]`=`::core::unimplemented!()` (correct) but `str[666:696]`=`…!()\n}\n//` (wrong). |
| why now | with the old token-stream skeleton the whole file was one line with different breakage; clean multi-line prettyplease output + non-ASCII doc text made the byte/char divergence bite precisely. |
| fix | both splicers now work in **byte space**: `read_bytes()`, slice with the byte offsets, decode only the small spans, `write_bytes()`. pipeline.py:`_splice_fn_body_at` + `_splice_fn_with_where`. The other splicer `_splice_fn_body` is char-based throughout (uses `_locate_fn`'s char offsets) → already correct. |
| validation | `tests/test_splice_byte_offsets.py` (em-dash before a stub fn → splice keeps `}` + next doc comment); offline repro now clean. Full effect = `bash run_microcrate.sh --from=task2` (re-run phase-c on the good bake; Python-only fix, binary untouched) → phase-c fills `add`/`parse_pair`/`bump`/`checked_div` DIRECTLY, no revert / no task3-retry rescue / no TASK 4. |

#### R12.2 — TASK 3 re-verify recursed unbounded → RecursionError  ✅ FIXED + test
Surfaced by the same run (once partial progress became possible). `task3.py` re-invoked `task3()` whenever the inline retry merely CHANGED the cargo text; cargo output isn't byte-deterministic (ordering/timing) → a no-op retry "changed" it → 981-frame `RecursionError`. Fix: `_should_reverify` gates on stable test COUNTS (passed / fewer-fail / more-pass) + a hard budget (2) → always terminates → falls through to TASK 4. Pinned by `tests/test_task3_reverify.py` (5 cases incl. the exact bug: same counts, different text → no recurse).

### R13 — FR/genesis efficacy  🔄 SUPERSEDED BY REPOSITORY SPECIFICATIONS (2026-06-11, user decision: wire now, skip the R13.1 gate)

**Repository Specifications (RepoSpec)** = the crate's spec-document set: functional requirements +
genesis (prose + `genesis_rules_read` structured rules) + API surface. USER DECISION (session 63):
(a) every code-producing prompt from t2 onward carries it; (b) iterations REPAIR, they don't wipe.

| piece | where |
|---|---|
| pure renderer | `pipeline_core/prompts/repo_spec.py` — `RepoSpecDocs` + `render_repo_spec` (relevance: FR paragraphs + cross-file API sections matching `identifiers_for_target(fn, container, file)`; budget-tiered: rules → FR excerpt → genesis → API; own-file API section excluded — it's already the prompt's file view) |
| provider | pipeline.py `_repo_spec_section` (fr_path → project/module; mtime-cached `_load_repo_spec_docs`; FR's `## Model Notes` stripped — has its own channel; fail-safe ""), wired via `prompts/runtime.py` facade `repo_spec_section` (3rd configure slot, inert default) |
| injected into | per-fn initial + fix, per-impl (builders call the facade — covers phase-c, test-retry loop C, `_apply_per_fn_fixes`/t3-inline-retry); `generate_fix_loop` per-fn/per-impl inner prompts + whole-file prompt (genesis-rules complement; FR already in `_spec_block`), `fr_path` threaded from task2.py |
| caps | `_REPO_SPEC_CAP` = max(2500, SCCL//10) per-fn/per-impl; `_REPO_SPEC_CAP_WIDE` = max(5000, SCCL//5) whole-file |
| NOT injected | task0c (t0, pre-strip test augmentation), skeleton-A structural check |
| **no-wipe repair iterations** | main() retry block: `delete_source_files` + task2b/2c invalidation REMOVED — tree kept, markers stay done (`phase_valid` is status-only), next iter skips t2 → t3 + fix loop repairs the kept tree with updated docs in prompts. Old wipe rationale (fresh measurement of doc quality) retired: the active generator never read the docs, so regen was a constant function (15-iter 67/123 plateau, session 63) |
| **best-tree ratchet** | `workspace_lifecycle.{save,restore}_best_tree` + `best_tree_pass_count` (`.best_tree/` snapshot + `_meta.json`; in SKIP_DIR_NAMES + task0b `_NON_SOURCE_DIRS` — R9.3 class): retry block snapshots on new-best t3 pass count (`ratchet.save` event), restores when an iteration ends below best — regressed fix or broken build (`ratchet.restore`). Outer-loop FloorStall: repair may experiment, can never END below the floor. Equal-pass trees kept (lateral moves allowed) |
| tests | tests/test_repo_spec.py (renderer relevance/budget/empty, provider fail-safe + notes-strip, builders carry the block, fr_path=None inert); test_workspace_lifecycle.py ratchet roundtrip |
| validation | ●●● live run: expect `<REPOSITORY_SPECIFICATIONS>` in logs/prompts per-fn prompts; `Keeping '{module}' tree for repair iteration` instead of `Cleaning up`; `[ratchet]` lines + ratchet.save/restore events; t3 pass counts MONOTONE-from-floor across iterations — BAD = repeated ratchet.restore every iter (repair never beats the floor → same plateau, now visible) |

**STANDING RULE — the canonical failure loop (user requirement, session 63).** Every cargo
check/compile/test failure path MUST run on one of the two drivers — `_drive_fix_loop` (inner,
compile-sig units) or `_improvement_loop`/`run_refine_loop` (outer, metric units) — which embody:
(1) inject what just happened (errors + trajectory context `LoopState.context()`), (2) retro
analysis on stall (`do_retro` — now wired on generate_fix_loop's inner per-fn AND per-impl loops,
not just whole-file), (3) the feed GROWS per attempt (history/PRIOR_ATTEMPTS + RepoSpec), (4) a
no-improvement stall budget terminates, (5) exhaustion action = unit revert-to-stub (inner) /
best-tree restore (outer). New fix sites must not hand-roll this loop.

### R13 (original framing) — measure, then wire  (kept for the metrics steps)
Goal restated: the pipeline's deliverable is `functional_requirements_{crate}.md` + `genesis.md` such that
a fresh run converges in as few iterations as possible. Today the hypothesis "richer docs → fewer
iterations" is (a) unmeasured and (b) structurally weak: t2c phase-c per-fn/per-impl — the dominant
iterating path — reads NEITHER doc (per_fn.py:38 by design); only filtered Model Notes +
`RECURRING_ERRORS` promoted rules reach it, and `genesis_rules_read` is never called there.
Order: R13.1 gates R13.2 (don't enrich a channel before proving the channel matters).

| # | step | what | Pri | Eff | Risk | status |
|---|---|---|---|---|---|---|
| R13.1 | **A/B efficacy run** | L3 microcrate (R10.1), two runs: (A) enriched FR+genesis from a prior converged run, (B) blank stubs. Metric = iterations-to-converge + total fix-loop attempts + phase-c revert count. Uses the R10 harness; cassette optional. Verdict: docs help / don't / mixed-per-channel. | P1 | M | ●● | ⬜ |
| R13.2 | **per-fn FR excerpt + genesis rules into phase-c** | only if R13.1 shows effect: relevance-selected FR paragraphs (match fn's type/trait/symbols — same selector family as `_filter_model_notes_for_prompt`) as a budget-capped `<FR_EXCERPT>` block in `build_per_fn_prompt`/`build_per_impl_prompt`; `genesis_rules_read(module_scope)` injected alongside. Keeps the no-full-FR budget stance. | P1 | M | ●●● | ⬜ gated on R13.1 |
| R13.3 | **instrument the loop** | emit per-iteration `docs.richness` (FR chars, notes count, genesis rules count) + iteration outcome into `events.jsonl` so doc-growth vs convergence is trendable across runs; monitor surfaces it. | P2 | S | ● | ✅ 2026-06-13: `_emit("docs.richness", …)` at the repair-iteration boundary (pipeline.py main loop) carries fr_chars/notes/genesis_rules + pass_count/prev/improved; new `genesis_rules_count()` helper (COUNT, not the LIMIT-20 render cap); monitor `_docs_richness_row()` renders it below the phase-c panel. Tests: genesis_rules_count, monitor docs-richness row. |
| R13.4 | **audit the existing conduit** | the notes relevance-filter + rule promotion are load-bearing (sole learning path into phase-c today): pin with tests what gets selected/dropped for a known fn; check cap behaviour under bloat. | P2 | S | ● | ✅ 2026-06-15: `tests/test_notes_conduit.py` pins tier ordering, symbol (tier-4) + fn-specific selection/drop, bloat cap-at-bullet, promotion threshold + past-note surfacing (7). |

Important Todos:

#### W1+W2 — backend→transport + unified think-recovery controller  ✅ SHIPPED + LIVE-VALIDATED 2026-06-15
The first two todos below are done. All recovery/capping/followup/retry policy moved out of the
backends into one controller in `pipeline_core/llm/` (foundational facade, outside the layer contract):
| module | owns |
|---|---|
| `events.py` | `Transport` Protocol + `Delta`/`StreamMeta` + typed errors (`InputTooLong`/`TransportUnavailable`/`TransportTimeout`) |
| `detect.py` | `StopDetector` — structural early-stop (`mod gen_*` brace-balance, `<FILE>` close) |
| `thinktag.py` | `InlineThinkSplitter` — incremental `<think>` demux for inline-reasoning wires |
| `cleanup.py` | boundary/role-token strip (pure; returns guard events) |
| `budget.py` | `request_max_tokens` — the ONE ctx-budget-aware cap (content + thinking reserve, ctx-capped) + adaptive cpt EWM |
| `controller.py` | `LLMController.run` — stream-consume + think-cap-with-recovery + length-truncation followup + 400/network retry + reasoning persist; I/O via injected `Hooks` |

`model_interact.py` backends are now THIN `*Transport`s (Ollama/vLLM/SGLang/HF): connect, stream
normalized `Delta`s, declare a `BudgetSpec`. No private caps, no followup. `ask()` unchanged → routes
through `LLMController`. Tests: `tests/test_llm_controller.py` (16, fake transport drives followup /
length-trunc / think-cap / early-stop / 400-retry / persist). check.sh green.
Generalization wins (todo #2): SGLang/HF previously had NO recovery — SGLang now captures
`reasoning_content` + gets followup; the ollama-compat followup's hardcoded `min(.,16384)` is now
ctx-budget-aware. Behaviour parity for the active vLLM path is intended-identical; **LIVE-VALIDATED 2026-06-15** (`logs/pipeline_micro_20260615-155218.log`, git eb8807d): full reconstruction ran through `LLMController`+vLLM `*Transport` — 40 llm.request/41 llm.response, vllm.budget+vllm.cpt_update flowed, C10 parallel-streams PASS (80 llm events, max 5 concurrent on distinct threads), SUCCESS iter 1, 0 stream collisions. Active vLLM path confirmed.
NOTE (minor): ollama-compat no longer emits raw `<think>`/`</think>` to stream.log (splitter strips them);
vLLM never did, so monitor's think-phase display is unaffected for the active backend.
#### W3 — ctx-budget-aware sweep of hardcoded caps  ✅ SHIPPED + LIVE-VALIDATED 2026-06-15 (active path; small-window scaling still benefits from anyhow)
LIVE 2026-06-15 (`logs/pipeline_micro_20260615-155218.log`, git eb8807d): budget machinery ran live — vllm.budget request_max=32768 emitted across 40 llm calls, no InputTooLong/overflow failure, SUCCESS iter 1. The auto-scaling-on-small-window effect is mechanism-confirmed; the extreme-shrink path still wants an anyhow-scale run.
Audit result: OUTPUT/think capping is already centralized by W1+W2 — the ONLY explicit `max_tokens` in
pipeline.py is the retro's deliberate `_RETRO_MAX_TOKENS=6144` CONTENT bound, which the controller now
honors while giving thinking its own 16384 reserve (so retro/T4 reasoning is no longer cut). No other
hardcoded output/think cap remains. INPUT side: the budget-derived cap family (`_FIX_ERR_CAP` =
`max(2000, SINGLE_CALL_CHAR_LIMIT//10)`, `_FAILED_BODY_CAP`, `_SRC_SNIP_CAP`, …) already existed; the sweep
routed the *escaped literals* through it so they scale with context (and shrink on small windows) instead
of freezing:
| site | was | now |
|---|---|---|
| `_focus_cargo_errors` default + the `3000` caller | `4000`/`3000` | `_FIX_ERR_CAP` (auto-scales all default callers) |
| retro-diagnosis error excerpt | `[:4000]` | `[:_FIX_ERR_CAP]` |
| failure-classifier test_out tail | `[-6000:]` | `[-_FIX_ERR_CAP:]` |
| model-notes extraction (body/err/fixed) | `[:1500]×3` | `_FAILED_BODY_CAP`/`_FIX_ERR_CAP` |
| task0c coverage-retry existing-block | `[:2000]` | `[:_SRC_SNIP_CAP]` (already injected) |
KEPT (justified, not context throttles): `lib_rs[:4000]` (top-of-file `#![no_std]` scan), the per-attempt
history compacts `_ERROR_EXCERPT_MAX_CHARS=600`/`_PRIOR_BODY_CAP=320`/`_PRIOR_ERR_CAP=180` (N-attempt
blocks — small by design), and post-hoc log/display snippets. check.sh green.

#### W4 — prompt-correctness fixes  ✅ SHIPPED (●● — effect needs a live run)
| item | finding / fix |
|---|---|
| from-scratch disclaimer (a) | `pipeline.ask()` now prepends a system note on EVERY call: our impl is independent of any public crate of the same name, reason only from shown code/sig/tests. Crate name from `_CRATE_DISPLAY_NAME` (set once in `_register_run`, `-master`/`-main`/`-trunk` stripped). Kills the "model recalls real upstream internals" leak. |
| genesis clarification (b) | T4 build-failure prompt lacked the genesis definition the else-branch has → model second-guessed "what is genesis". Added the same "project-independent methodology, not a per-crate fix" line to the build branch. |
| T0a toolchain (c) | t0a test-command prompt now also detects a pinned channel (rust-toolchain.toml / README → `cargo +nightly test`) + required global flags; explicitly defers per-feature/per-cfg variants to the static per-cluster layer (Hard Rule 8). |
| `{{ }}` (d) | NOT a bug — all are f-string/`.format` brace escapes (`{{module}}`→literal `{module}`, `mod gen_tests {{...}}`→`{...}`). The model comment was reasoning about the intentional `{module}` placeholder. No change. |
| coverage_strategy passing (e) | Verified: generated by t0b, passed into t1/t0c/t2 fill prompts (budget-permitting; dropped only under overflow at pipeline.py ~13577). Sending confirmed. No change. |

NEXT — W5 (extract generic error/ctx/retro-loop modules, then thin the tasks to orchestration-only).

W3 — ✅ DONE / verified (2026-06-15). Sweep complete: every LLM output/thinking cap routes through `pipeline_core/llm/budget.request_max_tokens` (the one home). All `ask()` call sites pass the default sentinel ⇒ the data-driven `content_budget` floor applies; the sole explicit override `_RETRO_MAX_TOKENS=6144` (pipeline.py) is correct — vLLM `BudgetSpec.add_thinking_to_request=True` makes the 16384 thinking budget a SEPARATE reserve added on top (`request_max = content + thinking`), never truncated by the content bound. Every input char-cap (`_README_CAP`/`_TYPES_SECTION_CAP`/task1_synthesizer caps/…) is derived from `SINGLE_CALL_CHAR_LIMIT`, not frozen. Only residual literals are inside `_default_max_tokens()` (the sentinel derivation, env-overridable). No hardcoded context cap bypasses the budget path → nothing to migrate.

W5 — generic modules ALREADY exist (`application/fix_loop.py`, `refine_loop.py`, `reconstruct/cargo_errors.py`, `reconstruct/context.py`, the 13-port reconstruct use-cases); tasks are thin (88–315 LOC) except `tasks/task0c.py`. **Task0c thinning — slice 1 DONE (2026-06-15):** the pre-augmentation harness compile-and-repair phase (~267 LOC: two-stage lib/harness check + LLM repair `drive_fix_loop` + targeted→full→git-restore strip ladder) extracted VERBATIM from `task0c_augment_module` to module-level `_run_harness_preflight(ctx: _PreflightCtx) -> tuple|None` (None ⇒ proceed; tuple ⇒ early-exit skip). Its locals (`_is_protected_test_file`/`_error_signatures`/`harness_ok`/`broken_files`) were provably unused after the phase. Behavior pinned by the existing `tests/test_task0c_fix_loops.py` (drives through the same entry; exact return-tuple + ask-count asserts unchanged). ruff + mypy-strict green. NOTE: NOT yet in a live run — the 2026-06-15 d3765d7 micro run predates this edit (the extraction is uncommitted working-tree; `git show d3765d7:…task0c.py | grep _run_harness_preflight` = 0). Awaits its own microcrate run. **Slices 2/3/4 DONE (2026-06-16):** (4) the 10 per-file stall/retro dicts → `Task0cFileHistory` value object (mutated in place); (3) Phase C generate/commit (~900 LOC incl. the `_render_prompt`/`_finalize_and_commit` closures) → module-level `_run_phase_c(...) -> _PhaseCOutcome` use-case over a frozen `_PhaseCCtx` (collaborators) + the history VO — AST-verified the only 3 outer-loop control-flow exits (2 `break`, 1 early `return`) became `_PhaseCOutcome` returns, tokenize-protected dedent kept f-string prompt templates byte-exact, ruff F821 caught the one missed binding (`attempt`); (2) the coverage-regression diagnose+fix block → `_diagnose_and_fix_regression(...)` (dropped dead `_reg_output`). **`task0c_augment_module` 1500→370 LOC** (preflight + Phase C + regression all extracted; the function is now thin orchestration: resume-state → loop[compile-check → measure → regression-guard → `_run_phase_c` → graceful-stop] → mutation pass). ruff + mypy-strict green; behavior pinned by `tests/test_task0c_fix_loops.py` (all pass against the extracted code). NOTE: `_run_phase_c`'s body is only exercised by a real microcrate run (no cargo/coverage in unit tests) — static-validated here, awaits its own microcrate run for live confirmation. BASELINE the verbatim extraction must reproduce is recorded: the pre-extraction run `logs/pipeline_micro_20260615-214010.log` (git aaf7bd8) drove this exact Phase C code to 100% coverage both clusters (76/76 + 109/109) in 1 attempt each, no tracebacks — the extraction is byte-for-byte that code, so behavior is preserved. Also extends the extraction-invariant checker (`tools/check_extraction_invariants.py`) to scan module-wide `deps.<field>` reads (its documented contract) so `ctx.deps` rebinds in extracted use-cases count as consumption.

* We just worked on a two fixes that: propagated a logic used for the ollama implementation into the llvm: whenever a context for think is exhausted, take the output before overwriting and loop it into a loop. This logic was not into vllm. So we need to abstract the way hard implementations work and fix it once at a higher level. vllm or ollama should have no busines logic, only communications with the drivers. the controller at a higher level.  ✅ W1
* once the previous point is implemented we must generalize a logic for ensuring think from llm is never capped or detected and followed up, this must work with vllm ollama whatever, because we're moving it to a higher level  ✅ W2
* second fix we did, allowed us to identify in t2c an issue in to the context size capping for think. The fix was "extend ctx-budget-aware logic to every hard coded context size cap" this todo is about searching again the whole pipeline for places where we still hard cap the context size. we should use the ctx-budgate-aware logic.  🔄 W3 (budget.request_max_tokens is the home; sweep pending)
* For example I just debugged T4 and the think was just capped. stream simply interrupted. We're interrupting thinking everywhere!  🔄 W3
* We refactored the whole pipeline, still everything is hardcoded into pipeline.py that is huge. we must:
* extract comming logic into specific modules like "error handling", "context aware logic", "loops for fixing errors with retrospective" these logics should be generic enough to be used in any task.
* then finally extract tasks. tasks have no specific logic but only control what is happening

* I read in t4 reasoning from the LLM "The user is asking to generate a genesis for solving a bug in the unit test. The genesis is more generic right? analyze in functional requirements what genesis is referring too

* So the t4 fix was already covered and is now also more correct:
  - t4's capped thinking is fixed by the global thinking_budget 8192→16384 (thinking is one backend knob, shared by all
  tasks).
  - The retro's explicit max_tokens=6144 content bound is now honored, not overridden — _content_cap only substitutes the
  data-driven floor when the caller used the mis-derived global default (MAX_TOKENS), leaving deliberate per-call bounds
  intact.

  Net: every task (including t4 retro/diagnosis) now reasons with the full 16384-token budget, while explicit content
  bounds like the retro's 6144 stay as their authors intended.

 search the same issue in the whole pipeline every place where we interact with llm we could be capping things       

*I did a copy of stream.log into codeLLm_Architecture_log.md. I see a couple of inconsistencies:Looking at think in t3 I think the LLM could have some knowledge about AnyHow outside of our repository. So we should indicate specifically in anystep where it is looking at the code "The repo is called {AnyHow} but considering the implementation is completely done from scratch by us, the implementation is completely different than the repository knowne as {AnyHow} where {Anyhow}=ModelName. The structure of the message may change.
the LL

comments regarding initial t0a generation:
we instruct the prompt defailt is cargo test. but we hav e multiple clusn't that wrong to instruct default? shouldn't we add to search for pre-compiler instructions like nightly?
* regarding compact covberage strategy reference: is this generated .md passed in the next steps? I'd like a review task by task about what are we sending(validate we're actually sending the content)
* I read a comment "Wait, the prompt template has double braces `{{ }}` for formatting? The user provided" where are we passing {{ }}?

