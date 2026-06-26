# Code Architecture — stance, reality, and the plan to fix it

> History note: the previous plan (P0–P8 phase log, R1–R13 backlog, slice journal) is
> preserved verbatim in `OLD_CODE_ARCHITECTURE.md`. It records *real work done* (the
> `pipeline_core/` scaffolding, ports, value objects, monitor/transport split). This file
> replaces it as the forward plan because the old one marked phases ✅ while the **headline
> goal was never met** — see §2.

---

## 0. Engineering stance (load this every session — don't restate it in chat)

You are a **senior software engineer**, expert in **idiomatic Python and idiomatic Rust**,
who builds to the highest standard of **Domain-Driven Design** and **Clean Architecture**.
Apply this lens to every change in this repo without being asked.

| Principle | What it means HERE |
|---|---|
| **Clean Architecture** | Dependencies point inward: `domain ← application ← adapters ← __main__`. Inner layers never import outer. `domain` is pure (stdlib only, no I/O). Enforced by import-linter. |
| **DDD** | Model the problem in the language of the domain: **value objects** (`Frontier`, `Stub`, `Cfg`), **use-cases** (`FixLoop`, `Attribution`, `PhaseCFill`), **ports** (Protocols) at boundaries. Behaviour lives in domain/application, not in a procedural god-file. |
| **Function size** | A function does ONE thing and reads on one screen (~≤40 lines). A 100-line function is a smell; a 500-line one is a defect. No exceptions for "the engine." |
| **DI, not monkeypatch** | Collaborators are **constructor-injected** (plain args / frozen dataclasses). Tests pass **fakes**, never `monkeypatch` module globals. The composition root (`pipeline.py`) builds real adapters and wires them; concretes live in `adapters/`, NOT as module-level `_func`s behind a shim. |
| **Idiomatic** | Pythonic: `@dataclass(slots=True)`, `Protocol`, `StrEnum`, `match`, no DI containers, no interface-per-class reflex. Rustacean: traits, `thiserror`, newtypes, `?`. |
| **No regex on source** | Rust source is parsed by the **syn extractor** (Python↔Rust JSON seam), never regex. Regex is allowed ONLY for model-output / cargo-text / filenames. (48 real `re.*` calls remain repo-wide; only **6** parse Rust source — see `P7_PLAN.md`.) |
| **Right > fast** | Deterministic > systemic > quality > ease. "Smaller change / less invasive / less code" is NOT a virtue on its own. Effort is a tiebreaker, never a criterion. |
| **Validate offline** | Engine changes are validated by the **offline replay harness** (§3), not "wait for a live run". The live run is a final confirmation, not the dev loop. |

Also binding: everything in `CLAUDE.md` (output style, commit/role rules, test-oracle immutability,
microcrate-first). This file is the *architecture* truth; `CLAUDE.md` is the *operating* truth.

---

## 1. The two codebases

- **Python** — `pipeline.py` + `pipeline_core/`: the LLM-driven reconstruction pipeline.
- **Rust** — `tools/skeleton_extractor/`: syn-based AST extraction + cross-cluster merge (the seam).

The Rust side is library-grade (split modules, `thiserror`, clippy-clean, 19 tests). The
**Python side is not** — see §2.

---

## 2. Current reality (measured 2026-06-16) — the problem this plan fixes

`pipeline.py` is a **15,682-line god-module** with **359 top-level symbols** (83% of lines are
inside function/class bodies). `pipeline_core/` is 20.7k lines of clean-layered scaffolding
**beside** it — but the hardest, most valuable code never moved in.

The headline goal from the old plan was **`pipeline.py` < ~800 lines (composition root + CLI
only)**. It is **20× over**. The old P-table marked P0–P8 ✅; those ticks were accurate for the
*slices in each phase*, but the end-state was never reached, and `§0a` of the old doc admits the
target "cannot [be reached] without microcrate/anyhow runs". Net: **scaffolding complete, engine
decomposition NOT done.** "All implemented" was wrong.

### 2.1 What's still in the god-file (the work that remains)

| Cluster | Lines | Worst offenders | Belongs in |
|---|---|---|---|
| Fill/fix **engine** | ~3,000 | `generate_fix_loop` 731, `_phase_c_fill_impl_blocks` 510, `_run_phase_c_tdd` 284, `_task3_inline_retry` 262, `_task2_fill_groups` 249, `_improvement_loop` 134 | `application/reconstruct/` + domain VOs |
| Failure **attribution** | ~600 | `_attribute_failures`, `_behavior_frontier`, `_extract_tdd_*frontier`, `_frontier_from_diags`, `_collect_impl_blocks` 107, `_parse_impl_block_ranges` 171, `_find_tests_for_fn` 116 | `application/reconstruct/attribution.py` + domain |
| Composition **god-adapters** | ~~630~~ **0 — ✅ ALL DISSOLVED 2026-06-22** | ~~`_PhaseCFillAdapter`, `_Task2Adapter`, `_ReconstructAdapter`, `_AugmentAdapter`~~ → focused ISP role adapters in `adapters/{reconstruct_roles,augment_engine,task2_roles,fill_engine_roles}.py`, constructor-injected via late-binding lambdas | DONE — `grep -E "^class _.*Adapter:" pipeline.py` = ∅ |
| cfg/cluster static analysis | ~650 | `task0_cfg_scan` 194 (**regex on source**), `_count_active_tests_for_cluster` 98, `_count_test_fns_cfg_aware`, `_file_active_for_cluster` | `domain/cfg.py` (`Cfg`/`CfgSet`) + syn extractor |
| Misc tasks/helpers | ~1,500 | `main` 481, `ask` 146, `_extract_crate_idioms` 156, `_bake_skeleton_artifacts` 134, `discover_test_command` 131, `clean_state` 138, `scan_downstream_api` 102 | `application/` use-cases / `adapters/` |
| **0 source-regex `re.*` sites** | — | ✅ all 6 migrated 2026-06-24 (group_fill/fix_diagnostics/attribution/macro-cfg/cfg-count/legacy-strip) + 1 dead regex deleted; `task0_cfg_scan` de-regexed earlier (P5) | done — see `P7_PLAN.md` |

### 2.2 Three root causes (why extraction stalled)

1. **The hard 20% was deferred.** Decomposition took the tractable part (pure helpers, parsers,
   VOs, thin task wrappers, monitor/transports). The engine — interleaved, stateful fill/fix/
   attribution/prompt logic — is the 20% that is 80% of the difficulty and value. It stayed.
2. **A self-imposed "needs a live run" gate.** Engine moves were tagged ●●● and deferred
   indefinitely because "the autopilot can't run the pipeline." But the **offline replay harness
   exists** (§3) — the gate is largely obsolete. The doc should have said *blocked*, not *done*.
3. **The test-seam strategy fights the architecture.** Every extracted unit kept its concrete as a
   module-level `_func` in pipeline.py that "late-binds so `monkeypatch pipeline._x` still
   intercepts," plus 4 `*Adapter` god-objects (630 lines) re-exposing those funcs to the ports. So
   extraction *moved* logic to `pipeline_core` but *left a shim/adapter behind* → the god-file
   barely shrank. **This pattern must be reversed before the rest can move (§4 Phase 1).**

---

### 2.3 Anti-patterns that kept the god-file alive — DO NOT repeat

These are the concrete failure modes from the prior effort (the reason ~6 months of "P0–P8 done"
left pipeline.py at 15.7k). Each is a trap; the RIGHT column is the rule.

| # | WRONG (what we did) | RIGHT (the rule) |
|---|---|---|
| A | **Shim-leaving.** Move a body to `pipeline_core`, then leave a `def _func(...): return _module.func(...)` shim in pipeline.py *plus* a `*Adapter` method re-exporting it. Net shrink ≈ 0. | A move DELETES the code from pipeline.py. The concrete lives in `adapters/`; the use-case takes a port; the test injects a fake. If a "shim" survives, the move isn't done. |
| B | **Monkeypatch-global test seams.** Keep concretes as module-level `_func`s *because* tests do `monkeypatch.setattr(pipeline, "_func", fake)`, and the docstring brags the method "late-binds so the seam still works." The test strategy dictated the architecture. | Constructor injection. Tests pass fakes to the use-case/adapter. `monkeypatch pipeline._x` for engine seams is itself the smell to remove. |
| C | **`*Deps` / `*Adapter` god-objects.** Relocate a blob behind one 70-field `Task0cDeps` or a 200-line `_Task2Adapter`. That's relocation, not decomposition — SRP/ISP still fail. | Narrow, role-based ports (ISP). If a deps object has >~8 fields, it's a blob hiding a missing boundary. |
| D | **Moving a god-function whole.** Relocate a 700-line fn to another file and call it "extracted." | Decompose first: one function = one responsibility, ≤~60 lines. A big function is split into cohesive units, THEN homed. |
| E | **"●●● needs a live run" as a blanket defer.** Tag every engine change live-gated and skip it forever, while the offline harness sat unused. | Validate on the replay/harness (§3). Live run = final confirm only. "Can't run the pipeline" is not a reason to defer behaviour-preserving refactors. |
| F | **Goal-drift ✅.** Mark phases done on *slice* completion while never measuring against the *headline goal* (LOC). 8 phases "done", goal 20× missed. | Track the GOAL, not the slices. "Done" = pipeline.py shrank toward ≤300 and a function-size/seam invariant held. A slice that doesn't shrink the god-file isn't progress. |
| G | **Gaming / inflating the ratchet.** Land a feature INTO pipeline.py, then trim comments to squeak under the LOC ratchet (or want to raise the baseline). Both hide growth. | New engine code lands in `pipeline_core` from the start, not in pipeline.py "for now". The ratchet only ever ratchets DOWN; if a change can't shrink the file, it's extracting wrong. |
| H | **Bug-fixes grow the god-file.** Every new fix lands in pipeline.py (the easy place) → the file grows even while "decomposing". | A fix goes in the module that owns the concern. If that module doesn't exist yet, creating it IS the fix. Adding to pipeline.py needs a reason it can't live elsewhere. |

Standing guardrails (some exist, some to add): LOC ratchet (down-only, exists); import-linter (exists);
**add**: a check that flags NEW top-level engine `def`/`class` in pipeline.py (composition root only);
**add**: a function-length lint (warn >60, fail >120) so god-functions can't reappear.

---

## 3. The unlock — offline validation (kills the "●●● live run" excuse)

Engine refactors are behaviour-preserving; they need a **deterministic replay**, not a live model.
The assets already exist:

| Asset | What it gives |
|---|---|
| `tests/harness.py` | scripted model + cargo + attribution seams (L1) — drive any failure mode deterministically |
| `tests/cassette.py` + `tests/replay.py` | record→replay a real run by `prompt_sha`; a prompt-changing edit shows MISSES at exactly the altered prompts (L2 blast-radius) |
| `tests/cargo_cache.py` | memoise cargo by tree-hash so replay needs no cargo |
| `run_microcrate.sh` / `run_hardcrate.sh` | L3 real-model smoke (happy path / recovery path) — the *final* confirmation only |

**Rule for this plan (binding):** every engine slice — INCLUDING the hot-path core (fill/fix/
attribution/task0c augment/cluster-state) — is validated OFFLINE: a characterization test on the
replay/harness that pins behaviour BEFORE the refactor and proves it AFTER, plus the static gates
and `check.sh`. **There is no live-validation gate.** A live `run_microcrate.sh`/`run_hardcrate.sh`
pass is an OPTIONAL final confirmation a human may choose to run; it is NEVER a precondition for a
slice, never a per-slice gate, and never a reason to defer an engine relocation. "Needs a live run"
is the rejected anti-pattern E. If a slice's behaviour can't be pinned offline, building that
harness coverage IS the first task of the slice — not deferral.

---

## 4. Target architecture + the decomposition plan

### 4.1 End state

```
pipeline.py            ~300 lines: argparse + build Settings + build adapters + wire use-cases + run.
pipeline_core/
  domain/              pure VOs + rules: Frontier, Stub, ImplBlock, Cfg/CfgSet, FixAttempt,
                       AttemptHistory, LoopState, IterationState … (stdlib only)
  ports/               Protocols the application depends on (LLM, Cargo, Extractor, Coverage,
                       Workspace, Fixer, Attribution, …)
  application/
    reconstruct/       the engine as use-cases: FixLoop, PhaseCFill, Attribution, Task3Retry …
    tasks/             task0/1/2/3/4 orchestration (thin)
    orchestrator.py    the run state machine (mostly done)
  adapters/            concrete port impls — the ONLY layer doing I/O (cargo, vllm, extractor,
                       coverage, git, state). Real classes; tests inject fakes.
  prompts/             pure prompt assembly (mostly done)
```

### 4.2 Layer rule (import-linter, already enforced)

`adapters > application > prompts > ports > domain`. Inner never imports outer. `log`/`retry`/`io`
are foundational facades domain may use. **No new `pipeline.py` shim per extraction** — the concrete
moves to `adapters/`, the test injects a fake.

### 4.3 Plan — phased, each phase independently shippable behind green `check.sh` + replay

Effort: S<M<L. Risk: ● offline-provable · ●● replay-provable · ●●● engine core — replay/harness-provable (NO live gate: a live run is optional final confirmation, never a per-slice or series blocker — see §3 / anti-pattern E).
Acceptance for EVERY slice: ruff + mypy-strict + import-linter green, **LOC ratchet shrinks
`pipeline.py`**, characterization test on harness/replay, no new monkeypatch-global seam.

| Phase | Goal | Key moves | Eff | Risk |
|---|---|---|---|---|
| **P1 — reverse the test-seam** (PREREQUISITE) | DI instead of monkeypatch so concretes can leave the god-file | Pick one engine collaborator family; build the real adapter in `adapters/`, give the use-case a constructor port, convert its tests to **fake injection**; delete the `pipeline._func` shim + the matching `*Adapter` re-export. Prove the pattern on ONE family before scaling. | M | ●● — **✅ DONE 2026-06-16 (pattern proven on the StructureReader family)** |
| **P2 — Attribution context** | Extract failure→fn attribution as a use-case | `domain`: `Frontier` (exists) + `FailureKind` enum + `AttributionResult`. `application/reconstruct/attribution.py`: `Attribution` use-case folding `_attribute_failures` + `_behavior_frontier` + `_extract_tdd_*frontier` + `_frontier_from_diags` over an `Extractor`/`TestIndex` port. `_filter_test_targets` (done) lives here. Kill the source-text scans where syn suffices. | L | ●● — **✅ DONE 2026-06-16.** `domain/attribution.py` (`FailureKind` StrEnum, `AttributionResult` NamedTuple, pure `line_to_fn_record`) + `application/reconstruct/attribution.py` (the 7-fn use-case over `AttributionDeps`: structure/classify/locate_fn/run_check_diag/derive_check_only_cmd/is_gen_tests_file/log). pipeline.py keeps thin name-preserving shims binding `_ATTR_DEPS` (collaborators late-bound for backward-compat seams; `AttributionDeps` is the clean DI rail a fake fills). **pipeline.py 15678→15353 (−325).** Consumes the P1 `StructureReader` (via `_LateStructure`). Tests: behavior/compile-frontier/oracle-guard + the moved-fn unit tests green; mypy-strict(95)/import-linter/ruff/LOC all green. NOTE: source-text scans in `build_test_reverse_map` (test-file `#[test] fn` regex) are R7-migrate-to-syn = P7; the cfg/cargo-output regexes are legitimate. |
| **P3 — FixLoop unification** | ONE fix-loop driver; dissolve `generate_fix_loop` (731) | ✅ **DONE 2026-06-18.** ✅ Driver/VO half (pre-dates this row): `application/fix_loop.py` `drive_fix_loop` + `_improvement_loop`→`run_refine_loop` + `domain/refine.py`. ✅ **Slice 1 (`5a8c2e5`):** fix-loop diagnostics (`error_sigs`/`errors_by_file`/`file_stats`) → `application/reconstruct/fix_diagnostics.py`, nested closures deleted, unit-tested; pipeline.py 15353→15318. ✅ **Slice 2 / P3.A (2026-06-17):** the 4 inline fix prompts (per-fn, per-impl, behavior, tdd-stub) → pure `prompts/fix_prompts.py`; byte-output pinned by `test_fix_prompts.py`. **KEY FINDING (corrects this row's earlier framing):** `generate_fix_loop` has ~35 callable collaborators + ~12 constants (AST-measured). The P4 `*Deps`+rebind-prelude *relocation* is **anti-pattern C/D** (relocation, not decomposition) — explicitly rejected for this fn. So P3.C is a genuine **FixUnit decomposition**, NOT a GenerateFixDeps move: each strategy (whole-file / per-fn / per-impl / tdd-stub / behavior) + the two inner cargo-check loops (`_fix_single_fn`, `_fix_impl_block`) becomes a typed `FixUnit` (≤60 lines) over the existing `ports/fill_engine.py` role ports, returning a typed `FixReport` (replaces the `dict[str,Any]` record). ✅ **P3.B (2026-06-17):** the implicit `dict[str,Any]` fix record → typed `domain/fix_report.py` (`FixKind` StrEnum {APPLIED/NOOP/ABORT} + `FixReport` VO with `aborted`/`unchanged`/`changed` factories + `.as_record()` anti-corruption bridge). All 13 `generate_fix_loop` return sites construct via the factories; `as_record()` pinned byte-equal to each original dict by `test_fix_report.py` (5 cases). pipeline.py imports `_FixReport`; ruff/mypy/compile green. ✅ **P3.C (2026-06-18):** FixUnit decomposition done. `domain/fix_session.py` (`FixSession` — the per-loop context the inner loops captured by closure + `is_writable`), `domain/fix_target.py` (`FixTarget` union `FnTarget`/`ImplTarget` — verdict `is_clean(rc,frontier)` lives ON the data), `domain/compile_probe.py` (`CompileProbe`), `domain/fix_plan.py` (`FixPlan` + pure `plan_per_fn`/`plan_per_impl`). `ports/fix_engine.py` ISP roles (`CompileChecker`/`FixModel`/`FixSplicer`/`FixPrompts`/`FrontierBuilder`). `application/reconstruct/fix_unit.py` (`FixUnit` proto `verify`/`attempt` + `FnFixUnit`/`ImplFixUnit` + `one_shot_fill`) — the two ~80-line inner closures are now thin composition-root adapters. `application/reconstruct/generate_fix.py` (typed `WholeFileFix`/`PerFnFix`/`PerImplFix` + `select_strategy` + `run_generate_fix`) — replaces `_do_fix_per_fn`/`_do_fix_per_impl`/dispatch; `generate_fix_loop` is now a deps-binding shim. ✅ **P3.D (2026-06-18):** `domain/source_snapshot.py` (`SourceSnapshot`), `domain/retry_outcome.py` (`RetryMode` BUILD/TEST + `RetryVerdict` + pure `build_verdict`/`test_verdict`); `_task3_inline_retry` refactored IN PLACE to use them. `_task3_legacy_single` left in the composition root — pure I/O orchestration, no domain content (relocating = anti-pattern C/D glue-move). Tests: `test_fix_target_session`/`test_fix_unit`/`test_fix_plan`/`test_generate_fix`/`test_retry_outcome` (23). pipeline.py 13639→13373 (−266). ruff/mypy-strict(122)/import-linter(KEPT)/LOC green; imports clean. **Pending live validation (`check.sh` + hardcrate).** | L | ✅ |
| **P4 — PhaseC fill engine** | Decompose `_phase_c_fill_impl_blocks` 510 + `_run_phase_c_tdd` 284 + `_task2_fill_groups` 249 | **✅ DONE 2026-06-17.** → `application/reconstruct/phase_c_tdd.py` (`run_phase_c_tdd`/`PhaseCTddDeps`, 15 deps, domain helpers imported), `group_fill.py` (`GroupFillDeps`, rebind-prelude keeps the 2 closures byte-identical), `impl_fill.py` (`ImplFillDeps`, rebind-prelude, 8 closures byte-identical). pipeline.py = thin shims binding `_PHASE_C_TDD_DEPS`/`_GROUP_FILL_DEPS`/`_IMPL_FILL_DEPS` (collaborators late-bound for monkeypatch seams). + P3-slice-1 `fix_diagnostics.py` (error_sigs/errors_by_file/file_stats, unit-tested). **pipeline.py 15318→14361 (−957).** Validated: 45 phase-c e2e (impl/tdd/group/stub/behavior routing) green; mypy-strict(99)/import-linter/ruff/LOC all green. **FOLLOW-UPS:** `ImplFillDeps`=43 fields (the per-impl engine's real coupling) → partition into the existing `ports/fill_engine.py` role ports (reuse `_PhaseCFillAdapter`); closures stay nested (method-promotion later); `file_stats`/`group_fill` source-text regexes → R7/P7. | L | ●●● |
| **P5 — cfg/cluster domain** | Kill the source-regex cluster scan | **✅ DONE 2026-06-17.** `domain/cfg.py` = typed algebra: `CfgAtom` + `CfgPred` tree (`Leaf/Not/All/Any`) + `CfgSet` (`evaluate`/`excludes_for_cargo`/`normalize`, conservative-undecidable, cargo-faithful exclusion never over-excludes builtins). `domain/cluster_plan.py` = pure planner (`predicate_counts`/`mod_gate_map`/`plan_clusters`; noise-threshold, default-feature, build-target-facet, nightly-gate rules explicit). `domain/models.py`+Rust `model.rs` `CfgOccurrence`/`CfgAtomWire`. Rust `enumerate.rs` `enumerate_cfgs_in_tree` (syn `Visit` walk reaches every attr) + `cfg.rs` `cfg_meta_atoms` + `--enumerate-cfgs` CLI + `visit` syn feature. `ports/cluster_facts.py` `ClusterFactReader` (ISP) + `Extractor.enumerate_cfgs` + `adapters/extractor.py`. `application/tasks/cfg_scan.py` `scan_build_clusters` use-case. `pipeline.task0_cfg_scan` = thin orchestrator over it; **the 4 source regexes (`_PREDICATE_RE/_FEATURE_VAL_RE/_BARE_CFG_RE/_MOD_CFG_RE`) DELETED**; the cfg-eval helpers (`_eval_cfg_pred/_normalize_cfg_pred/_cfg_false_for_cargo`) are thin domain shims, the 4 dead ones (`_split_top_level_commas/_cfg_leaf_atoms/_cfg_atom_is_off_for_cargo/_BUILTIN_CFG_IDENTS`) removed. Tests: `test_cfg_domain.py` (9) + `test_cluster_plan.py` (11) pure-domain + repointed `test_cfg_test_count`/`test_cfg_scan`. e2e smoke verified (Rust→JSON→domain→counts). ruff/mypy-strict/import-linter(KEPT)/extraction-invariants(8)/LOC all green. **FOLLOW-UP (P7):** the remaining cfg *source* regexes (`cfg_inline_re`@2402, `pred_re`@3508, `_file_top_cfg_excluded`@3602, `cfg_attr_re`@3940, `_CFG_ATTR_RE/_CFG_INLINE_RE`@8320, block-strip @12290+) live inside test-count/strip fns — migrate those to syn AND thread a typed `CfgSet` through their `(cfg_true,cfg_false,default_features)` triples in one P7 pass (touch once, not twice). **ALSO found 2026-06-18 (live anyhow run) — a THIRD parallel regex-based cfg evaluator P5 never folded in:** `_filter_cfg_incompatible_lines`/`_filter_cfg_excluded_lines` (pipeline.py:11623-11726, regex `#[cfg(...)]` match + manual brace-depth block span, used by task0c.py:1466-1486 to drop unreachable uncovered lines per cluster). **Concrete bug from this duplication:** `_estimate_cluster_ceiling` (pipeline.py:3319) calls ONLY `_filter_cfg_excluded_lines` (the `excluded_cfg` side) — it never calls `_filter_cfg_incompatible_lines` (the `active_cfg`'s `#[cfg(not(X))]` side). So the printed "Cluster ceiling estimate" is systematically optimistic for any cluster with `active_cfg` set: live anyhow `anyhow_no_core_error` cluster printed ~97.7% but the real runtime filter (both functions) stops augmentation at 95.0% — a 2.7pt gap that reads as "coverage regressed" when it's actually a wrong estimate, not a falling number. Fix is two-fold: (1) short-term, make `_estimate_cluster_ceiling` call both filters (parity with task0c.py's runtime path); (2) real fix is P7 folding all three cfg-regex evaluators into the one `domain/cfg.py`/`CfgSet` so estimate and runtime read the same source of truth and can't re-diverge. | M | ●● |
| **P6 — main()/adapters dissolve** | `main` 481 → orchestrator; delete the 4 `*Adapter` god-objects | 🔄 **IN PROGRESS — reframed 2026-06-17.** FINDING: the 4 `*Adapter`s are NOT god-objects with logic — they're thin **binding shims** (each method = 1–3 line delegation to a still-resident pipeline.py free fn). The engine BODIES already moved (task1-4/inner/fix are 14–69-line shims → `*_impl` in pipeline_core). So dissolving an adapter = moving its target fns into real `pipeline_core/adapters/` classes + injecting them directly. main() can't move to `application/orchestrator.py` (inner layer; import-linter forbids `application→pipeline`) until its ~30 callees move. **Sequencing decision (user, 2026-06-17): do the prerequisite engine/IO extraction FIRST, LEAF-FIRST** — a role is cleanly extractable only when its target fns are used *solely* by the binding adapter. **DONE this session:** `FailureJournal` → `domain/failure_journal.py` (`FailureEntry` VO + `JOURNAL_HEADER`) + `adapters/failure_journal.py` (`FileFailureJournal`), injected into RequirementsPhase+FailureAnalysisStep, pipeline.py free fns deleted; `Ratchet` → `adapters/ratchet.py` (`BestTreeRatchet` wrapping the already-extracted `workspace_lifecycle` fns), injected into FailureAnalysisStep, binding methods + imports dropped. CORRECTION to the earlier "gated" note: pipeline.py is the COMPOSITION ROOT, so a moved fn can be **re-imported** by name (`from pipeline_core… import X`) for any still-resident callers — that's a bare import, not a logic-shim, and is how pipeline.py already consumes pipeline_core. So "many callers" is NOT a blocker; the real blocker is only (a) the moved fn calling a still-resident helper that can't move, or (b) cross-cutting global state. **DONE this session (slices 1–4):** (1) `FailureJournal` → `domain/failure_journal.py` (`FailureEntry` VO) + `adapters/failure_journal.py` (`FileFailureJournal`). (2) `Ratchet` → `adapters/ratchet.py` (`BestTreeRatchet`). (3) `Artifacts` → `domain/workspace_layout.py` (`LOCKED_MODULES_DIR` vocabulary) + `adapters/artifacts.py` (`WorkspaceArtifacts` — load_manifest/load_api_surface/load_locked_source_context/format_whole_project_context/collect_source_context/api_surface_path); resident callers re-import the names; all 6 `_ReconstructAdapter` Artifacts methods dropped, `_WORKSPACE_ARTIFACTS` injected into all 3 reconstruction use-cases. (4) `classify_failure` → `domain/failure_classification.py` (`FailureCategory` StrEnum — typed, was a bare-string return). (5) `Telemetry` → `adapters/telemetry.py` (`LogTelemetry` routes log/emit through the `pipeline_core.log` ambient facade wired by `set_sinks` + `state.set_current`), injected into all 4 reconstruction use-cases. (6) `ModelNotes` → `domain/model_notes.py` (`slice_model_notes`/`parse_note_bullets`/`preserve_model_notes`/`render_with_notes` — the FR self-correction section; the `re.search(\\n##\\s)` boundary scan **migrated to a byte-equivalent structured char-scan**, R7 win); pipeline.py `_load/_save_model_notes` now thin wrappers. Tests: `test_failure_journal`(4)/`test_artifacts_adapter`(5)/`test_failure_classification`(5)/`test_model_notes`(6); reconstruct+notes_conduit suites green; composition `isinstance`-verified. **pipeline.py 14066→13829.** **REMAINING — the genuine ●●● core (per-slice characterization on the §3 offline replay/harness; the user's hardcrate run is the END-OF-SERIES confirm, NOT a per-slice gate — the "autopilot can't run the pipeline" defer was anti-pattern E):** the task*-shim roles (RequirementsService/Generator/TestRunner/Analyst/Fixer) are gated on relocating the `Task1/2/3/4Deps` + their deep web of ~40 resident engine helpers (`_cargo_env`, `_task3_legacy_single`, `_task3_inline_retry`, `_count_active_tests_for_cluster`, …) — which read the mutable cluster MODULE-GLOBALS (`_current_cluster`/`_all_clusters`) on the hot cargo/LLM path. Relocating them is a real DDD job (introduce an explicit `ClusterContext` VO threaded through, not a global), validated OFFLINE via the §3 replay/harness + `check.sh` (NO live gate — a live run is optional final confirmation only; see §3 / anti-pattern E). Also gated: Telemetry-of-`_AugmentAdapter` (its port adds `mark_phase`), PhaseGate (`_commit_and_mark` needs the `_git_managed` runtime global), FlowAudit/DocsMetrics (`_run_flow_audit`/`genesis_rules_count` bind runtime/bootstrap globals + sqlite), validators (`validate_task1/2/4_output` tangled with `_extract_identifiers` regex → **P7**). Then `_AugmentAdapter`/`_Task2Adapter`/`_PhaseCFillAdapter`, finally main()→Orchestrator. **All cleanly-offline-extractable roles are now DONE; the rest is the engine core, which moves under OFFLINE replay/harness + `check.sh` validation (NO live gate).** **(7) ClusterScope keystone — DONE + LIVE-GREEN 2026-06-17** (`pipeline.py=f92e563c`, microcrate `complete — all tests pass`, both clusters 30/30): `domain/cluster_scope.py` (`ClusterScope`: `active`+`all`, `set_active`/`get_active`/`reset`) replaces the mutable `_current_cluster`/`_all_clusters` module globals; satisfies `ports.augment.ClusterScope` directly so `_AugmentAdapter.set_active` was dropped and `scope=_SCOPE` injected into ClusterAugmentor; ~9 read sites → `_SCOPE.active`/`.all`, the two getter-lambdas → `_SCOPE.get_active`. `test_cluster_scope`(4). This is the keystone: cluster state is now an injectable object. **(8) CargoEnv — DONE + LIVE-GREEN (hardcrate) 2026-06-17** (`pipeline.py=f4c97a4e`, `complete — all tests pass iter 3`, extra_cap 25/25 + stable 24/24): `_cargo_env` moved into `adapters/cargo.py` as `CargoEnv` (scope-injected callable) + explicit `base_cargo_env()`/`apply_cluster_env()` functions (the cluster RUSTFLAGS-append + nightly-toolchain layering made explicit); pipeline.py binds `_cargo_env = CargoEnv(_SCOPE)`. `test_cargo_env`(5). pipeline.py→13803. NEXT: relocate the Task3/Coverage/Task0c helper web (`_task3_legacy_single`, `_task3_inline_retry`, `_count_active_tests_for_cluster`, `_load_baked_clusters`, …) leaf-first to dissolve the TestRunner/Analyst/Generator roles. **(9) AugmentStore — DONE 2026-06-22:** `adapters/augment_store.py` `FileAugmentStore` (the 6 sentinel-persistence methods, logic moved verbatim) + dir vocabulary `CLUSTER_AUGMENTED_DIR`/`MODULE_AUGMENTED_DIR` → `domain/workspace_layout.py`. Injected as `store=_AUGMENT_STORE` into `ClusterAugmentor`; the 6 `_AugmentAdapter.AugmentStore` methods + the 6 pipeline.py free fns/path-helpers DELETED; the two resident callers + `StripBakeDeps.module_augmented_path` repointed to the store. `test_augment_store.py`(3). pipeline.py 13348→13302. The remaining `_AugmentAdapter` roles (AugmentEngine `task0c_augment_module`/`_cluster_pre_check`, CommitGate `_commit_and_mark`) are the engine/git-global core — own slices, harness-characterized. **(10) `_AugmentAdapter` + `_ReconstructAdapter` FULLY DISSOLVED — 2026-06-22** (`adapters/{augment_engine,augment_store,commit_gate,reconstruct_roles,test_runner}.py`). **(11) `_Task2Adapter` DISSOLVED — 2026-06-22:** 13 focused ISP role adapters in `adapters/task2_roles.py` (GeneratorTelemetry/LlmAsk/Workspace/ContextDocs/CargoBackend/FillEngine/Clusters/GenTests/SourceScan/PromptShaping/ProgressFactory/RequirementsDoc), constructor-injected via late-binding lambdas (preserves the `_run_cargo`/`ask`/`_attribute_failures` monkeypatch seams); 6 generator/synthesizer factories repointed; isinstance-verified vs all 13 ports. pipeline.py 12868→12757. **(12) `_PhaseCFillAdapter` DISSOLVED — 2026-06-22:** 9 focused adapters in `adapters/fill_engine_roles.py` (EngineTelemetry/Mutator/Cargo/PromptContext/WorkspaceModel/Retros/TestLinker/StallPolicy/ImplFillPath); the P1 StructureReader DI seam now lives on `WorkspaceModelAdapter.structure`, test-swapped via `_build_phase_c_engine(structure=fake)` (`dataclasses.replace`); `test_structure_reader.py` flipped to the new seam. pipeline.py 12757→12642. **ALL FOUR GOD-ADAPTERS NOW GONE — `grep -E "^class _.*Adapter:" pipeline.py` = ∅.** **(13) main()-callee extractions (offline-provable leaf group) — 2026-06-22:** the `--best-models-list` report family (`_extract_params_b`/`_fit_label`/`_print_model_table`/`cmd_best_models`/`_OLLAMA_MODELS`) → pure `domain/model_report.py` (`ModelRow` VO + fit arithmetic + ranking/dedup/render) + `adapters/model_catalog.py` (Ollama `/api/tags` + HF Hub I/O); `cmd_best_models` is now a ~25-line composition shim, output byte-identical. `_ensure_vllm_ready` → `adapters/vllm_bootstrap.py` (`ensure_vllm_ready(root, log)`, log injected). pipeline.py 12642→12452. **(14) task0c pure text/config helpers (offline group) — 2026-06-22:** `_parse_retrospective_categories` → `domain/refine.py` (next to `parse_retrospective_function`); `_prelude_import_names` → `domain/generation.py` (the `use a::{X, Y as Z}` regex MIGRATED to string ops — R7 win); `_load_file_constraints` → `adapters/file_constraints.py` (`FileConstraintsReader`, configs-dir + active-cluster predicate + log injected). Task0cDeps repointed; equivalence smoke-checked; dead `tomllib`/`parse_retrospective_function` imports dropped. pipeline.py 12452→12383. **DEFERRED (regex-on-source, P7 not P6):** `_derive_gentest_prelude` + `_partition_uncovered_lines` parse Rust source by regex/brace-scan — a behaviour-preserving move would carry the violation, so they wait for the syn migration. **(15) main() in-place split ATTEMPTED & REVERTED — 2026-06-22:** splitting the 476-line `main()` into 3 phase fns *inside* pipeline.py added ~74 lines of VO/header/unpack boilerplate → **the LOC ratchet (correctly) rejected it** (anti-pattern G: a decomposition only counts if lines LEAVE the god-file). Lesson recorded: the `main()` decomposition is INSEPARABLE from moving the phases to `application/orchestrator.py` — and that move is blocked on relocating the ~50 engine callees first (import-linter forbids `application→pipeline`), which is the offline-validated finale. So `main()` stays whole until that series. REMAINING for full P6 done-criteria (all offline-validated / P7): (a) relocate the ~50 engine callees (`task0_*`, `discover_test_command`, `_strip_and_bake_module`, the `task0a/0b/0c` engine, …) to `pipeline_core` — bulk LOC, hot-path cluster-state, validated OFFLINE (replay/harness + `check.sh`, NO live gate) — THEN main()→orchestrator becomes possible; (b) `re.*`-on-source purge = P7. **(16) engine-relocation series STARTED — 2026-06-22** (user-approved; syn-migrate regex per move; validated offline via replay/harness + `check.sh` — no live gate): `_capture_api_surface` → `adapters/api_surface.py` (`ApiSurfaceCapture`, path/condenser/extractor/log injected); `_measure_baseline_coverage` → `application/orchestrator.py` (next to `next_module` — same processing-order concern; tarpaulin/log injected). pipeline.py 12383→12300, ratchet ratcheted DOWN both slices (code LEFT the god-file — the correct direction the in-place split lacked). **(17) engine-relocation series CONTINUED — 2026-06-22 (live-gate REMOVED this session; engine core now offline-validated):** six more slices, all gated green (ruff/mypy-strict/import-linter KEPT/loc-ratchet DOWN), pipeline.py 12300→11495 (−805). (a) `discover_test_command` → `domain/test_command.py` (backtick `re.finditer`→structured char-scan, R7) + `application/tasks/discover_test_command.py` (`DiscoverTestCommandDeps`). (b) `restart_from_augmented` → `adapters/workspace_restore.py` (`RestoreFromAugmented`) + `domain/workspace_layout.py` (`MODULE_STRIPPED_DIR`/`FAILURE_LOG_NAME` vocab consolidated + `ITERATION_PHASE_PREFIXES` + pure `drop_iteration_phases`). (c) `_collect_impl_blocks` + `_parse_impl_block_ranges` → `domain/impl_block_ranges.py` (pure regex-free brace-lexer, typed `ImplRange` VO replacing the dict) + `application/reconstruct/impl_blocks.py` (`collect_impl_blocks`, extractor seam injected). (d) `_merge_cluster_outputs`+`_ast_merge_files`+`_cluster_cfg_predicate_for_gate` → `adapters/cluster_merge.py` (`ClusterMerger`) + `domain/cfg.py` (`cluster_gate_predicate`). (e) `_phase_c_retrospective` → `prompts/retrospective.py` (pure prompt) + `application/reconstruct/retrospective.py` (`diagnose_phase_c`/`PhaseCRetroDeps`). (f) mutation family (`_mutation_survivors`/`_mutation_kill_check`/`_mutation_harden_file`) → `adapters/mutation.py` (`MutationTester`). New char.-tests: `test_test_command`/`test_workspace_layout`/`test_impl_block_ranges`/`test_phase_c_retrospective` + flipped smoke `cluster_gate_predicate`. **AUDIT FINDING (precompiled-regex-aware, this session): the clean-P6 leaves are now EXHAUSTED.** The only remaining non-regex >60L top-level fns are `ask` (145L — LLM context-guard transport; clean but ~14 test-monkeypatch sites across 4 files + highest blast radius → its own isolated session) and `_task3_legacy_single` (87L — deliberately kept, P3.D, pure composition-root I/O). EVERY other >60L fn (main 477, generate_fix_loop 389, _task3_inline_retry 259, _extract_crate_idioms, clean_state, _find_tests_for_fn, scan_downstream_api, the cfg/test-count family, validators, …) is regex-entangled (literal or precompiled `*_RE` on source) → **P7 (source-regex→syn migration)**. So full P6 done-criteria (pipeline.py ≤300, main()→orchestrator) is gated on P7, NOT on live validation — the live-gate removal was correct but the binding blocker is the regex-on-source rule. NEXT: (i) the `ask` transport slice (isolated); (ii) begin P7 — migrate the source-regex helper mass to the syn extractor, which then unblocks the giant engine fns + main()→orchestrator. | M | ●●● |
| **P7 — regex purge** | ✅ **DONE 2026-06-24** → see `P7_PLAN.md` | Migrate every source-parsing regex to syn/structured; keep + justify model-output/cargo-text/filename ones (R7.3/R7.5/R7.6). Pin equivalence per migration. ✅ **COMPLETE 2026-06-24** — final 6 source-regex migrated byte-equivalently (pinned in `tests/test_p7_source_regex.py`: targeted + 40k fuzz, re-verified on 249 real `.rs`), 5 static gates GREEN. All remaining `re.*` are cargo/model/seam/filename/markdown KEEPs. pipeline.py source-regex count → 0. Single source of truth = `P7_PLAN.md` (48 real `re.*` calls repo-wide, only 6 parse Rust source; the deleted `P7_REGEX_LEDGER.md` was stale — its "171" were docstrings of already-migrated regex). New home for pure source tokenisers: `domain/rust_idents.py`. ✅ **Slice 1:** `_compute_fn_name_counts` (`\bfn\s+(\w+)`+`\b<name>\b` leaf-order map) → `fn_reference_counts` (maximal-`\w+`-run tokeniser, `\s+`-gap-aware so `fn(i32)` ptr types never match). ✅ **Slice 2:** `_select_relevant_siblings` (CamelCase `\b[A-Z][A-Za-z0-9]+\b` + `\b(?:struct\|enum\|trait\|type\|impl)\s+([A-Z]…)\b`) → `camelcase_type_names`+`defined_type_names`. Both byte-equivalence-pinned vs the original regex in `tests/test_rust_idents.py`. KEPT (R7.3, justified in-docstring): `_count_test_attrs_in_text`/`_count_test_fns_cfg_aware` marker scans (mid-fill code; cfg eval already via `domain/cfg.py`). REMAINING ~24 SOURCE? need pure tokenisers OR a syn-extractor extension (add `attrs`/`is_test` to `FnRecord` for the test-count family; macro-def + use-stmt enumeration). Leaf-first, byte-equivalence per slice. | M | ● |
| **P8 — Rust polish (low pri)** | finish the extractor edges | `Cfg` newtype in `cfg.rs` if P5 wants richer cfg facts; otherwise the Rust side is done. | S | ● |

### 4.4 Sequencing rationale

P1 first and non-negotiable: without DI-not-monkeypatch, every later extraction leaves a shim and
the god-file won't shrink. P2 (attribution) is the most self-contained engine slice and a good
second proof. P3 (FixLoop) is the biggest single win (731-line fn + dedups ≥4 hand-rolled loops).
P4 depends on P1/P3 ports. P5/P7 (regex/cfg) are parallelisable. P6 is the finish line — it only
becomes small once P1–P5 have emptied the adapters.

### 4.4a P1 — what shipped + a sequencing correction (2026-06-16)

Shipped: `ports/structure.py` (`StructureReader`) + `adapters/structure.py` (`CliStructureReader`,
wrapping the `Extractor` seam — the `.to_dict()` logic moved OUT of pipeline.py's shims);
`_STRUCTURE` built at the composition root; **`_PhaseCFillAdapter.__init__(*, structure=…)`** now
CONSTRUCTOR-injects it and its `enumerate_fns/types` use the injected reader (no late-bound
global); `_build_phase_c_engine(adapter=…)` + `_task2_fill_fns_one_by_one(adapter=…)` are the test
injection seam. Proof: `tests/test_structure_reader.py` injects a **fake** reader via constructor —
no `monkeypatch pipeline._enumerate_*`. All phase-c e2e suites (fill/impl/u2/test-retry/replay) +
fill-engine unit tests stay green; static gates + LOC ratchet green.

**Correction to the plan (important — don't repeat the wrong assumption):** P1 assumed it could
"delete the `pipeline._func` shim" up front. It can't, and that's not P1's failure — it's the
sequencing. The `_enumerate_*_via_extractor` globals have **19 raw call sites** in the
NOT-yet-decomposed engine (`_collect_impl_blocks`, `_behavior_frontier`, the fix dispatchers, …),
and engine helpers like `_collect_impl_blocks`/`_scan_unimplemented_records_for_cluster` reach the
extractor by calling those globals directly (they bypass the adapter — they're module functions, no
constructor). So a global **can only be deleted once its last raw consumer is decomposed** (P2–P4).
P1's real deliverable is therefore: the **DI rails exist and the pattern is proven** (real adapter,
constructor port, fake-injection, zero new monkeypatch). Each family's shim is then deleted
incrementally as P2–P4 move its consumers onto the injected reader. Treat P1 as "rails laid", not
"god-file emptied".

### 4.5 Definition of done (the real one)

- `pipeline.py` ≤ ~300 lines: argparse + composition root only. No engine, no parsers, no shims. (now ~11495 — the
  remaining bulk is the regex-entangled engine callees `main`'s phases invoke; relocating them is gated on P7
  (source-regex→syn), then the finale, validated offline via the §3 replay/harness + `check.sh` — NOT live-gated.
  Non-regex leaves are exhausted as of 2026-06-22 except `ask` (own session) — see P6 row note 17.)
- No function > ~60 lines anywhere; **no `*Adapter` god-object — ✅ MET 2026-06-22 (all four dissolved).**
  (`main()` 476L is still the largest fn — its split is blocked on the engine-callee relocation, see P6 row note 15.)
- Tests inject fakes via constructors; zero `monkeypatch.setattr(pipeline, "_x", …)` for engine seams.
  (Partial: the focused adapters delegate via late-binding lambdas so the resident-global monkeypatch
  seams still work for now; the StructureReader DI seam is the proven fake-injection path.)
- Zero `re.*` on Rust source. import-linter + mypy-strict + LOC ratchet green.
- Behaviour preserved end-to-end, proven OFFLINE by the §3 replay/harness characterization suite +
  `check.sh`. (An optional `run_hardcrate.sh` convergence check may be run as a final human confirmation,
  but it is not a gate — see §3.)

---

## 5. The Python↔Rust seam is sacred

The JSON record contract (`enumerate-*`, `--skeletonise`, `--merge-clusters`) between the
`cli_extractor` adapter (Py) and `model.rs` (Rust) is the good boundary. Schema changes go through
`model.rs` + the Python `domain/models.py` dataclass together, never ad-hoc dict mutation. Byte-for-
byte preserved across all refactors. (`docs/SEAM.md` documents it.)

---

## 6. What is genuinely DONE (don't redo it)

Scaffolding is real and should be built ON, not rebuilt: `pipeline_core/` layering + import-linter
contract; `domain/` value objects (incl. `Frontier`/`Stub`/`ImplBlock`/`Diagnostic`/`Cfg`-less);
ports for LLM/Cargo/Extractor/Coverage/Workspace + the reconstruct/fill_engine/augment role ports;
`prompts/` pure assembly; monitor data/render split; `model_interact.py` = 135-line transport shim;
the Rust extractor (split, thiserror, merge cfg-variant-correct — debt #16); the offline harness
(§3); the technical_debt.md bug fixes (#1–#18, all resolved). The gap is exclusively §2.1.

---

## 7. Next refactorings — engine CORRECTNESS (separate track from the P1–P8 decomposition)

P1–P8 reshape the god-file; these fix what the reconstruction LOOP *does*. Surfaced by live runs,
not by static structure. Same gates (ruff/mypy/import-linter/LOC/offline-replay; no live gate).

### 7.0 — repair loop ALIVE (run 2975 confirmed B1+B2) — residual is structural R1–R3

**Evidence — anyhow run `28654` (2026-06-25, `pipeline.py=be0de6c5`, `--repo dtolnay/anyhow --clean --audit`):**
FAILED after 2 iterations, plateaued (identical aggregate iter 1→2).
`TASK 3 aggregate: 107/218 passed, 11 failed, 2 ignored — FAIL`.

| Cluster | Result | Cause |
|---|---|---|
| `nightly` | **0/109** (`rc=101`, 0 tests reported) | ≥1 filled `error.rs` body won't compile → the whole test binary dies → 0 tests run |
| `stable` | 107/109 compile+pass, **11 fail** | failures all in `error.rs` pointer/vtable paths: `test_chain 1/5`, `test_fmt 1/4`, `test_context 2/4`, `test_downcast 1/3`, `test_boxed 2/3`, `lib.rs 14/15`, `test_ensure 17/18` |

**Two faults — the retrospective loop EXISTS and produces correct guidance, but cannot apply it:**

The retro loop is real and working at the diagnosis level. Per-fn: `application/reconstruct/retrospective.py::diagnose_phase_c` asks the model *"here is the fn / last body / error / N attempts — diagnose WHY, give a concrete `try X`"*, returns `{category, guidance, signal}`, tracks `prior_best/improved/exhausted`, and feeds `prior_retrospective` into the next attempt's prompt. Whole-run: TASK 4 (`Analyzing failure [build] → genesis.md` + Model Notes) re-enters the fix loop. This run produced *correct* diagnoses — `Replace ErrorImpl<E> with ErrorInner<E>`, `Replace Request{demand:…} with Request::new(...)`, `WRONG_DEREF_PATTERN → self.0.source()`. The loop fails for two reasons, not because the retro is missing:

| ID | Fault | Concrete evidence | Effect |
|---|---|---|---|
| **B1** | `ask()` rejects `temperature` — half-wired feature | `pipeline.py:4837` (`_ask_per_fn_body`) sets `kwargs["temperature"]=unsafe_temp` for **unsafe fns** then `ask(prompt,**kwargs)`; but `ask()` (`pipeline.py:880`) is `ask(prompt,system,model,max_tokens,think)` — no `temperature`, and `model_interact`/`llm_transports` have **zero** temperature support. Log: `do_fix raised: ask() got an unexpected keyword argument 'temperature'` (×4). | anyhow `error.rs` fns are `unsafe` (raw-ptr/vtable) ⇒ `is_unsafe=True` ⇒ every fix attempt on **exactly the hard fns** throws BEFORE reaching the model ⇒ `improvement_loop/task3.inline_retry] aborted at iter 1`. The repair loop is **dead on arrival** for the only fns that matter. NOT a refactor regression (single site 4837; `ask` never had the param). |
| **B2** | one fn's transport error aborts the WHOLE improvement pass | `[improvement_loop/task3.inline_retry] aborted at iter 1: do_fix_exception` — a single `do_fix` raise kills the pass; the other ~10 fixable stable failures are never attempted. | even with B1 fixed, any per-fn exception blinds the loop. No fault isolation. |

**Run 2975 (2026-06-26, `pipeline.py=b1f97d82`) — MEASUREMENT:** 0 temperature TypeErrors (B1 clean), 0 "aborted at iter 1" (B2 clean). Retry reaches the model and continues across multiple fns: `re-spliced error.rs::construct_from_adhoc/::msg/::context/::object_downcast/::as_ref/::error/::object_ref`. Build-fix progress real (22 errors→0, 6→0). BUT test floor stuck at 111 for 4 consecutive iters (C4 WARN). Smoking gun: `[improvement_loop/impl:src/error.rs::impl<E> ErrorImpl<E>] floor stalled at 1 for 2 iter(s)` — the `ErrorImpl<E>` frozen-bound mismatch is the genuine residual. Nightly: 35 failed / 74 passed (rc=101). Stable: 30 failed / 56 passed. **B1+B2 resolved. R1–R3 are now the active front.**

### 7.0a — detailed fix plan (B1 → B2 → R1–R4)

> **Root-cause re-analysis 2026-06-26** — Original R2/R3 (bound-incompat frontier redirect + canonical symbol co-design) were based on the wrong assumption that `ext::StdError` and `StdError` are the same trait rendered with different names. Skeleton inspection disproves this: `ext::StdError` (in `context.rs::mod ext`) is an **extension trait** (`fn ext_context<C>(self, context: C) -> Error`) with a blanket impl for all `E: crate::StdError + Send + Sync + 'static`. They are genuinely different traits. The actual failure modes (confirmed from skeleton) are: (a) wrong field name — `_object: E` generates `self.error` / `self.inner` → E0609; (b) cfg-gated triple `StdError` definition confusing the model about which is active; (c) interdependent unsafe vtable dispatch cluster (GENUINELY_HARD). Old R2/R3 replaced below.

**B1 — thread `temperature` end-to-end (the unblock).** Eff S · Risk ● (offline-provable).
1. `model_interact.py` / `pipeline_core/adapters/llm_transports.py`: add `temperature: float | None = None` to the transport `ask`/generate call; when not None, set it in the vLLM/Ollama sampling params (vLLM: `sampling_params.temperature`; Ollama: `options.temperature`). When None, preserve today's default (do NOT send the key → byte-identical request → existing cassettes unaffected).
2. `pipeline.py:880 ask(...)`: add `temperature: float | None = None`; forward to `_ASK_CLIENT.ask(prompt, system, model, max_tokens, think, temperature)`. Keep positional contract; update the `_ASK_CLIENT` port/Protocol signature in lockstep.
3. `pipeline.py:4837`: now valid — `_ask_per_fn_body`'s `unsafe_temp` path works (low-temp for rigid unsafe code, per Qwen3.6 guidance).
4. Validate OFFLINE: unit test `temperature=None` ⇒ request bytes unchanged (cassette replay no MISS); `temperature=0.1` ⇒ param present. Add a transport-level test asserting the kwarg reaches sampling params. mypy/ruff/import-linter/replay green. No live gate.

**B2 — per-fn fault isolation in the improvement loop.** Eff S · Risk ●● (replay-provable).
1. Locate the `task3.inline_retry` / `do_fix` driver (`pipeline_core/application/reconstruct/{generate_fix,fix_unit}.py` + `application/refine_loop.py`). Wrap the per-fn `do_fix` call in a try/except that, on transport/model exception, logs `[task3-retry] fn <name> skipped: <exc>` and CONTINUES to the next frontier fn — instead of aborting the pass.
2. A pass aborts only when ZERO fns made progress AND ZERO were skipped-for-other-reasons (genuine stall), not on the first exception.
3. Distinguish "exception while fixing" from "fix produced no improvement" in the stall accounting so one throwing fn doesn't mask a stalling-floor signal.
4. Validate: harness test driving a scripted `do_fix` that throws on fn #1 and succeeds on fn #2 ⇒ pass continues, fn #2 fixed, fn #1 logged-skipped. Replay green.

**R1 — retrospective is BODY-ONLY; frozen-bound mismatch ⇒ clean `GENUINELY_HARD`.** Eff S · Risk ● (offline).
- `prompts/retrospective.py`: forbid any guidance that edits a signature / `where` / bound / trait header (the skeleton freezes them); few-shot WRONG→RIGHT so the model proposes BODY edits only. When the only fix would change a frozen bound ⇒ classify `GENUINELY_HARD` (clean skip), not a 4-attempt grind. Pin prompt bytes (`tests/test_phase_c_retrospective.py`).
- Stops the wasted thrash that B1 currently hides (`request_ref` reached attempt 4; `error.rs::error` reverted after 4).

**R2 — `_object` field Model Note (the highest-signal quick fix).** Eff S · Risk ● (offline).
- Root cause: `ErrorImpl<E>` stores the error value in field `_object: E`. The underscore prefix is a Rust dead-code-suppression convention — the field is NOT unused. The model consistently generates `self.error.source()` / `self.inner.source()` → E0609 (no field `error` on `ErrorImpl<E>`).
- Fix: after the first `impl<E> StdError for ErrorImpl<E>` failure, inject to Model Notes: WRONG: `self.error.source()` / RIGHT: `self._object.source()`. Likewise for any other access pattern (`self.inner` / `self.value`). Two or more diverse WRONG→RIGHT pairs (few-shot variety).
- Also covers `impl ErrorImpl { unsafe fn error(this: Ref<Self>) }` — the erased-type accessor also needs to reach `_object` via the vtable cast: WRONG: `&this.ptr.as_ref().error` / RIGHT: chain through vtable dispatch (see R4).
- Gate: Model Notes are append-only at the prompt-build layer — no new module, no new test; the existing `model_notes.py` infrastructure handles it. Pin round-trip in `tests/test_model_notes.py`.

**R3 — cluster-scoped cfg rendering in per-fn prompts.** Eff M · Risk ●● (replay-provable).
- Root cause: `lib.rs` contains THREE mutually exclusive `StdError` definitions under `#[cfg]` guards: `use std::error::Error as StdError` (stable), `use core::error::Error as StdError` (no_std+core_error), `trait StdError: Debug + Display { fn source() ... }` (no_std+no_core_error). The skeleton includes all three. The model sees all three simultaneously and must evaluate cfg predicates to know which is active — which it does badly, leading to wrong method resolution.
- Fix: when building the per-fn prompt for a given cluster, apply the cluster's `CfgSet` to strip inactive cfg branches from the injected file context before sending to the model. For cluster `stable`: inject only `use std::error::Error as StdError`. For `anyhow_no_core_error`: inject only the local `trait StdError: Debug + Display` definition. This is the same `domain/cfg.py` / `CfgSet.evaluate()` logic already used at analysis time — apply it at prompt-render time too.
- Eliminates an entire class of cfg-ambiguity errors across ALL crates, not just anyhow. `ext::StdError` confusion is a subset.
- Gate: replay harness — diff the prompts sent for `stable` vs `nightly` clusters and assert only the expected cfg branch appears. Unit test: `test_per_fn_prompt_cfg_rendering.py`.

**R4 — fast GENUINELY_HARD for unsafe vtable dispatch clusters.** Eff S · Risk ● (offline).
- Root cause: `object_ref<E>` / `object_boxed<E>` / `object_drop<E>` / `vtable()` / `impl ErrorImpl { unsafe fn error / error_mut / chain / provide }` form an interdependent unsafe cluster. No single fn can compile until all are correct; they require raw-pointer casting via `Ref<ErrorImpl>.cast::<ErrorImpl<E>>()` and knowledge of the `#[repr(C)]` layout. The model currently grinds 4 attempts per fn (×8 fns = 32 wasted model calls per run) without converging.
- Fix: detect the pattern "unsafe fn + `Own<ErrorImpl>` or `Ref<ErrorImpl>` parameter + no `where E:` bound" → classify GENUINELY_HARD after ≤2 attempts, not 4. The fn-level retrospective (`diagnose_phase_c`) already tracks `exhausted`; add a fast-exit rule for this signature pattern.
- Alternatively: detect mutually-interdependent unsafe fns (all fail together, none succeed alone) and mark the whole cluster GENUINELY_HARD on first group stall.
- Gate: offline — harness scripted to drive 2 attempts on a vtable-pattern fn ⇒ GENUINELY_HARD logged, no attempt 3.

**Sequencing:** B1+B2 done → R1 (stop thrash, needs HR5) → R2 (`_object` note, needs HR5) → R3 (cfg rendering, needs HR5) → R4 (fast GENUINELY_HARD, needs HR5). Each is independently shippable; R2 has the highest signal-to-effort ratio. Detail: `technical_debt.md #28`.

Status 2026-06-26: **B1 + B2 CONFIRMED WORKING** by run 2975 (measurement above). R1 UNBLOCKED (evidence in hand). R2–R4 analysis-only, unapproved (Hard Rule 5). Old R2/R3 (bound-incompat redirect + canonical symbol) WITHDRAWN — superseded by skeleton-verified re-analysis above.
- B1: `temperature` threaded `pipeline.ask → AskClient → model_interact → LLMController → Transport` + all 5 backends (omitted when None ⇒ byte-identical request). `tests/test_ask_client.py` contract-pinned.
- B2: extracted `_fix_one_ref`; `_apply_per_fn_fixes` catches per-fn exceptions and continues. Limitation: both are still nested closures in `_task3_inline_retry` → not yet harness-testable (V1 fixes this).

### 7.0b — validation hardening (DO IN ORDER: V1 then V2)

**V1 — extract `_apply_per_fn_fixes` out of the `_task3_inline_retry` closure so B2 gets a harness test.** Eff M · Risk ●● (replay-provable). FIRST.
- Problem: `_fix_one_ref` + `_apply_per_fn_fixes` close over `_task3_inline_retry` locals (`project_dir`, `_retry_containers`, `_retry_type_recs`, `_retry_fr_path`, `_retry_tests_dir_files`, `src_root`, `_cluster_label`, `_env`, the model/splice/context collaborators) ⇒ unreachable from a test ⇒ B2's isolation is currently inspection-only.
- Move: a use-case `application/reconstruct/per_fn_retry.py` — `fix_one_ref(rel_f, fn, output, deps) -> bool` (benign no-ops → False; transport/model/splice errors RAISE) + `apply_per_fn_fixes(refs, output, deps) -> int` (the try/except-continue isolation). `deps` is a narrow injected role object (≤~8 fields, ISP — NOT a god-`Deps`): `locate_fn`, `gather_fix_context`, `build_per_fn_fix_prompt`, `ask_per_fn_body`, `splice_fn_body`, `find_tests_for_fn`, `focus_cargo_errors`, `is_gen_tests_file`, `log` + the per-run data VO (`RetryContext`: project_dir/src_root/cluster_label/containers/type_recs/fr_path/tests_dir_files). pipeline.py keeps a thin closure that binds the resident collaborators (preserves the `ask`/`_splice_fn_body` monkeypatch seams).
- B2 harness test (`tests/test_per_fn_retry.py`, fake-injected, no monkeypatch): scripted `fix_one_ref` throws on fn#1 / succeeds on fn#2 ⇒ pass CONTINUES, fixed_count==1, fn#1 logged `[task3-retry] fn …::… skipped`; all-throw ⇒ fixed_count==0 (caller's `abort` path, not a crash). Pins the isolation behaviour B2 added.
- Gates: ruff/mypy-strict/import-linter/LOC-ratchet (pipeline.py shrinks) + the new char test. Offline; no live gate.

**V2 — level-5 hardcrate `unsafe_ptr` trap (guards B1 end-to-end, anyhow-free).** Eff S · Risk ● (offline-provable). SECOND.
- `tools/hardcrate_select.py TRAPS`: add `"unsafe_ptr": {"level": 5, "src": "src/unsafe_ptr.rs", "test": "tests/unsafe_ptr.rs", "anchor": <lib.rs mod line>}`. `max_level()` ⇒ 5, `run_hardcrate.sh` default complexity ⇒ 5. `tests/test_hardcrate_select.py` is data-driven over TRAPS ⇒ passes unchanged.
- Fixture: a small, genuinely RECONSTRUCTABLE `unsafe fn` (raw-pointer deref/`offset`, transmute-free — e.g. `pub unsafe fn read_at<T: Copy>(p: *const T, i: isize) -> T`) + a safe wrapper + unit/integration tests pinning behaviour; must `cargo test` GREEN standalone like every other trap. Keep it a CLEAN unsafe fn (reconstructable, no vtable dispatch), NOT the hard vtable-dispatch cluster (that is R4's concern); this trap's job is to exercise the `is_unsafe_fn → _ask_per_fn_body` low-temp path.
- GOOD on `run_hardcrate.sh --complexity=5`: `unsafe_ptr` fills (low-temp suits rigid pointer code) and tests pass. BAD on a B1 regression (ask drops `temperature`): per-fn fill raises `TypeError: ask() got an unexpected keyword argument 'temperature'` ⇒ run RED.
- ⚠️ Scope of the guard: this trap catches only a CRASHING B1 regression. A SILENT regression ("temperature accepted but not forwarded to sampling params") still fills at default temp ⇒ run stays GREEN here. The exact-contract guard for that is `tests/test_ask_client.py` (kwarg reaches the transport). Keep BOTH; the trap is coarse end-to-end smoke, the unit test is the precise contract.

Status 2026-06-26: V1+V2 approved — **READY TO IMPLEMENT NOW** (in order). Both OFFLINE (no live gate). V1 first; V2 after V1 complete.

| # | Where | Problem (concrete) | Move | Eff | Risk |
|---|---|---|---|---|---|
| **R1 — retrospective body-only** (**UNBLOCKED** 2026-06-26) | `application/reconstruct/retrospective.py` `diagnose_phase_c` + `prompts/retrospective.py` | Classifier GUIDANCE proposes editing the frozen `where`/bound (`add E: StdError`), which body-fill cannot do ⇒ identical re-submit ⇒ "same error set after attempt N". Live: anyhow `context.rs::context` E0277, both clusters; category oscillates TRAIT_BOUND_VIOLATION↔API_HALLUCINATION. | Constrain category set + prompt to BODY-ONLY edits; forbid signature/bound/where suggestions (skeleton freezes them). Bound-mismatch ⇒ classify `GENUINELY_HARD` → clean skip, not thrash. Pin prompt bytes. | S | ● |
| **R2 — `_object` field Model Note** | `domain/model_notes.py` + per-fn prompt builder | `ErrorImpl<E>` stores the error in field `_object: E`. Model generates `self.error.source()` / `self.inner.source()` → E0609 on every attempt. Affects `impl<E> StdError for ErrorImpl<E>` + `impl ErrorImpl { unsafe fn error }`. | After first failure on any `ErrorImpl` body, append Model Note: WRONG: `self.error.source()` / RIGHT: `self._object.source()`. ≥2 WRONG→RIGHT pairs (few-shot variety). Existing `model_notes.py` infrastructure; no new module. | S | ● |
| **R3 — cluster-scoped cfg rendering** | per-fn prompt builder + `prompts/` | `lib.rs` has 3 mutually-exclusive `StdError` defs under `#[cfg]`. Skeleton includes all 3; model evaluates cfg predicates badly ⇒ wrong method resolution in generated bodies. | When building per-fn prompt for a cluster, apply `CfgSet.evaluate()` to strip inactive cfg branches from the injected file context. For `stable`: only `use std::error::Error as StdError`. For `anyhow_no_core_error`: only the local `trait StdError: Debug + Display` def. Same `domain/cfg.py` logic, applied at prompt-render time. Replay test: diff prompts for `stable` vs `nightly` clusters, assert single branch. | M | ●● |
| **R4 — fast GENUINELY_HARD for unsafe vtable dispatch** | `application/reconstruct/retrospective.py` `diagnose_phase_c` | `object_ref<E>` / `object_boxed<E>` / `vtable()` / `impl ErrorImpl { error/error_mut/chain }` form an interdependent unsafe cluster. No fn compiles until all do. Requires `Ref<ErrorImpl>.cast::<ErrorImpl<E>>()` + `#[repr(C)]` layout knowledge. Model grinds 4 attempts × ~8 fns = 32 wasted calls/run. | Detect: `unsafe fn` + `Own<ErrorImpl>` or `Ref<ErrorImpl>` param + no `where E:` bound → GENUINELY_HARD after ≤2 attempts. Or: detect mutually-interdependent unsafe fn cluster (all fail together) → mark whole cluster on first group stall. | S | ● |

Detail + evidence: technical_debt.md #28. R1: **UNBLOCKED** — run 2975 confirms thrash (`ErrorImpl<E>` stall, 4-iter C4 plateau at 111). R2–R4: skeleton-verified analysis, unapproved (Hard Rule 5).

### 7.1 — Ordered work chunks (2026-06-26, post run-2975)

A→B→C approved; D→F need HR5. Each chunk: offline gates (ruff/mypy/import-linter/LOC↓) + characterization test. No live gate per §3.

| Chunk | Work | Key files | Gate | Status |
|---|---|---|---|---|
| **A — V1** | Extract `_apply_per_fn_fixes`/`_fix_one_ref` closures → `application/reconstruct/per_fn_retry.py`; `RetryDeps` ISP role (≤8 fields: locate_fn/gather_fix_context/build_per_fn_fix_prompt/ask_per_fn_body/splice_fn_body/find_tests_for_fn/focus_cargo_errors/is_gen_tests_file/log) + `RetryContext` VO (project_dir/src_root/cluster_label/containers/type_recs/fr_path/tests_dir_files); `tests/test_per_fn_retry.py` (fake-injected: fn#1 throws→skip+continue, fn#2 succeeds; all-throw→count==0). Thin pipeline.py closure preserves monkeypatch seams. | `pipeline.py::_task3_inline_retry` | ruff/mypy/import-linter/LOC↓/char-test | **APPROVED — start now** |
| **B — V2** | `unsafe_ptr` level-5 hardcrate trap: `tools/hardcrate_select.py TRAPS` entry + fixture `src/unsafe_ptr.rs`+`tests/unsafe_ptr.rs` (clean raw-ptr deref/offset, no transmute, `cargo test` GREEN standalone). `test_hardcrate_select.py` data-driven → passes unchanged. Guards B1 regression end-to-end (crash path); `test_ask_client.py` guards the silent drop. | `tools/hardcrate_select.py` | ruff/mypy/cargo-test/data-driven suite | **APPROVED — after A** |
| **C — R1** | Retrospective body-only: constrain `prompts/retrospective.py` + `diagnose_phase_c` to forbid signature/where/bound guidance; frozen-bound mismatch → `GENUINELY_HARD` (clean skip, not 4-attempt thrash). ≥2 WRONG→RIGHT few-shot examples. Pin prompt bytes in `tests/test_phase_c_retrospective.py`. | `prompts/retrospective.py`, `application/reconstruct/retrospective.py` | ruff/mypy/import-linter/pin-bytes | **UNBLOCKED — needs HR5** |
| **D — R2** | `_object` field Model Note: after first `impl<E> StdError for ErrorImpl<E>` failure, append to Model Notes: WRONG: `self.error.source()` / WRONG: `self.inner.source()` / RIGHT: `self._object.source()`. Also: WRONG: `this.ptr.as_ref().error` / RIGHT: vtable dispatch via `Ref::cast`. ≥2 WRONG→RIGHT pairs. Existing `domain/model_notes.py` infra; pin round-trip in `tests/test_model_notes.py`. Highest signal-to-effort of R2–R4. | `domain/model_notes.py`, per-fn prompt builder | ruff/mypy/import-linter/note-round-trip | needs HR5; independent of C |
| **E — R3** | Cluster-scoped cfg rendering: apply `CfgSet.evaluate(cluster)` to strip inactive `#[cfg]` branches from the file context block injected into per-fn prompts. For `stable`: only `use std::error::Error as StdError`. For `anyhow_no_core_error`: only local `trait StdError: Debug + Display`. Same `domain/cfg.py` logic already used at analysis time, now applied at prompt-render time. Replay test: diff stable vs nightly per-fn prompts, assert single active branch. Generalises to all crates. | per-fn prompt builder, `domain/cfg.py` | ruff/mypy/import-linter/replay diff | needs HR5; independent of D |
| **F — R4** | Fast GENUINELY_HARD for unsafe vtable dispatch: in `diagnose_phase_c`, detect `unsafe fn` + `Own<ErrorImpl>` or `Ref<ErrorImpl>` param + no `where E:` bound → classify GENUINELY_HARD after ≤2 attempts (saves ~32 wasted model calls/anyhow run). Alternatively: detect mutually-interdependent unsafe fn cluster (all fail together) → mark whole cluster on first group stall. Harness test: 2 attempts on vtable-pattern fn ⇒ GENUINELY_HARD, no attempt 3. | `application/reconstruct/retrospective.py` | ruff/mypy/import-linter/harness | needs HR5; independent of D/E |
| **G — `ask` (P6)** | Extract `pipeline.ask` 145L → proper use-case; own isolated session: ~14 `monkeypatch.setattr(pipeline,"ask",…)` seams across 4 test files must convert to constructor injection before the fn can leave. Last clean P6 leaf. | `pipeline.py:880`, `tests/` | full static gates + check.sh | no blocker; own session |
| **H — P6 finale** | Relocate remaining regex-entangled engine callees (`generate_fix_loop` 389L, `_task3_inline_retry` residual, `_extract_crate_idioms`, validators, cfg/test-count family) → `pipeline_core` via syn (post-P7); then `main()`→`application/orchestrator.py`. Pipeline.py ≤300L. | `pipeline.py` bulk | ruff/mypy/import-linter/LOC↓/replay | after G; gated on P7 syn migration |

**Real sequence to P6 done:** G (`ask` slice, last clean leaf) → P7 (source-regex→syn migration, unblocks engine core: `generate_fix_loop`, `_task3_inline_retry`, validators, cfg/test-count family) → H (`main()`→orchestrator, pipeline.py ≤300L). H cannot start until P7 clears the regex-entangled engine fns that `main()` calls directly.
