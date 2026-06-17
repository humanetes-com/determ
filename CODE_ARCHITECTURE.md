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
| **No regex on source** | Rust source is parsed by the **syn extractor** (Python↔Rust JSON seam), never regex. Regex is allowed ONLY for model-output / cargo-text / filenames. (161 `re.*` sites remain in pipeline.py — a standing violation, see §4 Phase 5.) |
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
| Composition **god-adapters** | 630 | `_PhaseCFillAdapter` 185, `_Task2Adapter` 204, `_ReconstructAdapter` 170, `_AugmentAdapter` | DELETE — replace with constructor DI (§4 Phase 1) |
| cfg/cluster static analysis | ~650 | `task0_cfg_scan` 194 (**regex on source**), `_count_active_tests_for_cluster` 98, `_count_test_fns_cfg_aware`, `_file_active_for_cluster` | `domain/cfg.py` (`Cfg`/`CfgSet`) + syn extractor |
| Misc tasks/helpers | ~1,500 | `main` 481, `ask` 146, `_extract_crate_idioms` 156, `_bake_skeleton_artifacts` 134, `discover_test_command` 131, `clean_state` 138, `scan_downstream_api` 102 | `application/` use-cases / `adapters/` |
| **161 `re.*` call sites** | — | `task0_cfg_scan` parses Rust source with regex (Hard-Rule violation, still live) | syn extractor |

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

**Rule for this plan:** every engine slice ships with a characterization test on the replay/harness
that pins behaviour BEFORE the refactor and proves it AFTER. A live run confirms at the end of a
series, never per-slice. If a slice can't be covered offline, that gap is itself the first task.

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

Effort: S<M<L. Risk: ● offline-provable · ●● replay-provable · ●●● needs a live confirm at series end.
Acceptance for EVERY slice: ruff + mypy-strict + import-linter green, **LOC ratchet shrinks
`pipeline.py`**, characterization test on harness/replay, no new monkeypatch-global seam.

| Phase | Goal | Key moves | Eff | Risk |
|---|---|---|---|---|
| **P1 — reverse the test-seam** (PREREQUISITE) | DI instead of monkeypatch so concretes can leave the god-file | Pick one engine collaborator family; build the real adapter in `adapters/`, give the use-case a constructor port, convert its tests to **fake injection**; delete the `pipeline._func` shim + the matching `*Adapter` re-export. Prove the pattern on ONE family before scaling. | M | ●● — **✅ DONE 2026-06-16 (pattern proven on the StructureReader family)** |
| **P2 — Attribution context** | Extract failure→fn attribution as a use-case | `domain`: `Frontier` (exists) + `FailureKind` enum + `AttributionResult`. `application/reconstruct/attribution.py`: `Attribution` use-case folding `_attribute_failures` + `_behavior_frontier` + `_extract_tdd_*frontier` + `_frontier_from_diags` over an `Extractor`/`TestIndex` port. `_filter_test_targets` (done) lives here. Kill the source-text scans where syn suffices. | L | ●● — **✅ DONE 2026-06-16.** `domain/attribution.py` (`FailureKind` StrEnum, `AttributionResult` NamedTuple, pure `line_to_fn_record`) + `application/reconstruct/attribution.py` (the 7-fn use-case over `AttributionDeps`: structure/classify/locate_fn/run_check_diag/derive_check_only_cmd/is_gen_tests_file/log). pipeline.py keeps thin name-preserving shims binding `_ATTR_DEPS` (collaborators late-bound for backward-compat seams; `AttributionDeps` is the clean DI rail a fake fills). **pipeline.py 15678→15353 (−325).** Consumes the P1 `StructureReader` (via `_LateStructure`). Tests: behavior/compile-frontier/oracle-guard + the moved-fn unit tests green; mypy-strict(95)/import-linter/ruff/LOC all green. NOTE: source-text scans in `build_test_reverse_map` (test-file `#[test] fn` regex) are R7-migrate-to-syn = P7; the cfg/cargo-output regexes are legitimate. |
| **P3 — FixLoop unification** | ONE fix-loop driver; dissolve `generate_fix_loop` (731) | `application/fix_loop.py` exists (`drive_fix_loop`) — make it the SINGLE driver. Port `generate_fix_loop`, `_improvement_loop`, `_task3_inline_retry`, the per-fn/per-impl inner loops onto it via a `FixUnit` protocol (`locate/prompt/apply/verify/accept/revert/exhaust`). Domain VOs: `FixAttempt`, `AttemptHistory`, `LoopState`, `StallBudget`. Each ported loop = a `FixUnit`, ~≤60 lines. | L | ●●● |
| **P4 — PhaseC fill engine** | Decompose `_phase_c_fill_impl_blocks` 510 + `_run_phase_c_tdd` 284 + `_task2_fill_groups` 249 | **✅ DONE 2026-06-17.** → `application/reconstruct/phase_c_tdd.py` (`run_phase_c_tdd`/`PhaseCTddDeps`, 15 deps, domain helpers imported), `group_fill.py` (`GroupFillDeps`, rebind-prelude keeps the 2 closures byte-identical), `impl_fill.py` (`ImplFillDeps`, rebind-prelude, 8 closures byte-identical). pipeline.py = thin shims binding `_PHASE_C_TDD_DEPS`/`_GROUP_FILL_DEPS`/`_IMPL_FILL_DEPS` (collaborators late-bound for monkeypatch seams). + P3-slice-1 `fix_diagnostics.py` (error_sigs/errors_by_file/file_stats, unit-tested). **pipeline.py 15318→14361 (−957).** Validated: 45 phase-c e2e (impl/tdd/group/stub/behavior routing) green; mypy-strict(99)/import-linter/ruff/LOC all green. **FOLLOW-UPS:** `ImplFillDeps`=43 fields (the per-impl engine's real coupling) → partition into the existing `ports/fill_engine.py` role ports (reuse `_PhaseCFillAdapter`); closures stay nested (method-promotion later); `file_stats`/`group_fill` source-text regexes → R7/P7. | L | ●●● |
| **P5 — cfg/cluster domain** | Kill the source-regex cluster scan | **✅ DONE 2026-06-17.** `domain/cfg.py` = typed algebra: `CfgAtom` + `CfgPred` tree (`Leaf/Not/All/Any`) + `CfgSet` (`evaluate`/`excludes_for_cargo`/`normalize`, conservative-undecidable, cargo-faithful exclusion never over-excludes builtins). `domain/cluster_plan.py` = pure planner (`predicate_counts`/`mod_gate_map`/`plan_clusters`; noise-threshold, default-feature, build-target-facet, nightly-gate rules explicit). `domain/models.py`+Rust `model.rs` `CfgOccurrence`/`CfgAtomWire`. Rust `enumerate.rs` `enumerate_cfgs_in_tree` (syn `Visit` walk reaches every attr) + `cfg.rs` `cfg_meta_atoms` + `--enumerate-cfgs` CLI + `visit` syn feature. `ports/cluster_facts.py` `ClusterFactReader` (ISP) + `Extractor.enumerate_cfgs` + `adapters/extractor.py`. `application/tasks/cfg_scan.py` `scan_build_clusters` use-case. `pipeline.task0_cfg_scan` = thin orchestrator over it; **the 4 source regexes (`_PREDICATE_RE/_FEATURE_VAL_RE/_BARE_CFG_RE/_MOD_CFG_RE`) DELETED**; the cfg-eval helpers (`_eval_cfg_pred/_normalize_cfg_pred/_cfg_false_for_cargo`) are thin domain shims, the 4 dead ones (`_split_top_level_commas/_cfg_leaf_atoms/_cfg_atom_is_off_for_cargo/_BUILTIN_CFG_IDENTS`) removed. Tests: `test_cfg_domain.py` (9) + `test_cluster_plan.py` (11) pure-domain + repointed `test_cfg_test_count`/`test_cfg_scan`. e2e smoke verified (Rust→JSON→domain→counts). ruff/mypy-strict/import-linter(KEPT)/extraction-invariants(8)/LOC all green. **FOLLOW-UP (P7):** the remaining cfg *source* regexes (`cfg_inline_re`@2402, `pred_re`@3508, `_file_top_cfg_excluded`@3602, `cfg_attr_re`@3940, `_CFG_ATTR_RE/_CFG_INLINE_RE`@8320, block-strip @12290+) live inside test-count/strip fns — migrate those to syn AND thread a typed `CfgSet` through their `(cfg_true,cfg_false,default_features)` triples in one P7 pass (touch once, not twice). | M | ●● |
| **P6 — main()/adapters dissolve** | `main` 481 → orchestrator; delete the 4 `*Adapter` god-objects | 🔄 **IN PROGRESS — reframed 2026-06-17.** FINDING: the 4 `*Adapter`s are NOT god-objects with logic — they're thin **binding shims** (each method = 1–3 line delegation to a still-resident pipeline.py free fn). The engine BODIES already moved (task1-4/inner/fix are 14–69-line shims → `*_impl` in pipeline_core). So dissolving an adapter = moving its target fns into real `pipeline_core/adapters/` classes + injecting them directly. main() can't move to `application/orchestrator.py` (inner layer; import-linter forbids `application→pipeline`) until its ~30 callees move. **Sequencing decision (user, 2026-06-17): do the prerequisite engine/IO extraction FIRST, LEAF-FIRST** — a role is cleanly extractable only when its target fns are used *solely* by the binding adapter. **DONE this session:** `FailureJournal` → `domain/failure_journal.py` (`FailureEntry` VO + `JOURNAL_HEADER`) + `adapters/failure_journal.py` (`FileFailureJournal`), injected into RequirementsPhase+FailureAnalysisStep, pipeline.py free fns deleted; `Ratchet` → `adapters/ratchet.py` (`BestTreeRatchet` wrapping the already-extracted `workspace_lifecycle` fns), injected into FailureAnalysisStep, binding methods + imports dropped. CORRECTION to the earlier "gated" note: pipeline.py is the COMPOSITION ROOT, so a moved fn can be **re-imported** by name (`from pipeline_core… import X`) for any still-resident callers — that's a bare import, not a logic-shim, and is how pipeline.py already consumes pipeline_core. So "many callers" is NOT a blocker; the real blocker is only (a) the moved fn calling a still-resident helper that can't move, or (b) cross-cutting global state. **DONE this session (slices 1–4):** (1) `FailureJournal` → `domain/failure_journal.py` (`FailureEntry` VO) + `adapters/failure_journal.py` (`FileFailureJournal`). (2) `Ratchet` → `adapters/ratchet.py` (`BestTreeRatchet`). (3) `Artifacts` → `domain/workspace_layout.py` (`LOCKED_MODULES_DIR` vocabulary) + `adapters/artifacts.py` (`WorkspaceArtifacts` — load_manifest/load_api_surface/load_locked_source_context/format_whole_project_context/collect_source_context/api_surface_path); resident callers re-import the names; all 6 `_ReconstructAdapter` Artifacts methods dropped, `_WORKSPACE_ARTIFACTS` injected into all 3 reconstruction use-cases. (4) `classify_failure` → `domain/failure_classification.py` (`FailureCategory` StrEnum — typed, was a bare-string return). (5) `Telemetry` → `adapters/telemetry.py` (`LogTelemetry` routes log/emit through the `pipeline_core.log` ambient facade wired by `set_sinks` + `state.set_current`), injected into all 4 reconstruction use-cases. (6) `ModelNotes` → `domain/model_notes.py` (`slice_model_notes`/`parse_note_bullets`/`preserve_model_notes`/`render_with_notes` — the FR self-correction section; the `re.search(\\n##\\s)` boundary scan **migrated to a byte-equivalent structured char-scan**, R7 win); pipeline.py `_load/_save_model_notes` now thin wrappers. Tests: `test_failure_journal`(4)/`test_artifacts_adapter`(5)/`test_failure_classification`(5)/`test_model_notes`(6); reconstruct+notes_conduit suites green; composition `isinstance`-verified. **pipeline.py 14066→13829.** **REMAINING — the genuine ●●● core (needs LIVE validation; autopilot can't run the pipeline):** the task*-shim roles (RequirementsService/Generator/TestRunner/Analyst/Fixer) are gated on relocating the `Task1/2/3/4Deps` + their deep web of ~40 resident engine helpers (`_cargo_env`, `_task3_legacy_single`, `_task3_inline_retry`, `_count_active_tests_for_cluster`, …) — which read the mutable cluster MODULE-GLOBALS (`_current_cluster`/`_all_clusters`) on the hot cargo/LLM path. Relocating them is a real DDD job (introduce an explicit `ClusterContext` VO threaded through, not a global) + must be validated by the live microcrate/hardcrate run, NOT offline. Also gated: Telemetry-of-`_AugmentAdapter` (its port adds `mark_phase`), PhaseGate (`_commit_and_mark` needs the `_git_managed` runtime global), FlowAudit/DocsMetrics (`_run_flow_audit`/`genesis_rules_count` bind runtime/bootstrap globals + sqlite), validators (`validate_task1/2/4_output` tangled with `_extract_identifiers` regex → **P7**). Then `_AugmentAdapter`/`_Task2Adapter`/`_PhaseCFillAdapter`, finally main()→Orchestrator. **All cleanly-offline-extractable roles are now DONE; the rest is the engine core that must move under live validation.** **(7) ClusterScope keystone — DONE + LIVE-GREEN 2026-06-17** (`pipeline.py=f92e563c`, microcrate `complete — all tests pass`, both clusters 30/30): `domain/cluster_scope.py` (`ClusterScope`: `active`+`all`, `set_active`/`get_active`/`reset`) replaces the mutable `_current_cluster`/`_all_clusters` module globals; satisfies `ports.augment.ClusterScope` directly so `_AugmentAdapter.set_active` was dropped and `scope=_SCOPE` injected into ClusterAugmentor; ~9 read sites → `_SCOPE.active`/`.all`, the two getter-lambdas → `_SCOPE.get_active`. `test_cluster_scope`(4). This is the keystone: cluster state is now an injectable object. **(8) CargoEnv — DONE + LIVE-GREEN (hardcrate) 2026-06-17** (`pipeline.py=f4c97a4e`, `complete — all tests pass iter 3`, extra_cap 25/25 + stable 24/24): `_cargo_env` moved into `adapters/cargo.py` as `CargoEnv` (scope-injected callable) + explicit `base_cargo_env()`/`apply_cluster_env()` functions (the cluster RUSTFLAGS-append + nightly-toolchain layering made explicit); pipeline.py binds `_cargo_env = CargoEnv(_SCOPE)`. `test_cargo_env`(5). pipeline.py→13803. NEXT: relocate the Task3/Coverage/Task0c helper web (`_task3_legacy_single`, `_task3_inline_retry`, `_count_active_tests_for_cluster`, `_load_baked_clusters`, …) leaf-first to dissolve the TestRunner/Analyst/Generator roles. | M | ●●● |
| **P7 — regex purge** | 161 `re.*` → audit | Migrate every source-parsing regex to syn (most go with P5/P2); keep + justify model-output/cargo-text/filename ones (R7.3/R7.5/R7.6 classes). Pin equivalence per migration. | M | ● |
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

- `pipeline.py` ≤ ~300 lines: argparse + composition root only. No engine, no parsers, no shims.
- No function > ~60 lines anywhere; no `*Adapter` god-object.
- Tests inject fakes via constructors; zero `monkeypatch.setattr(pipeline, "_x", …)` for engine seams.
- Zero `re.*` on Rust source. import-linter + mypy-strict + LOC ratchet green.
- A hardcrate L3 run converges (the recovery audits PASS), proving behaviour preserved end-to-end.

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
