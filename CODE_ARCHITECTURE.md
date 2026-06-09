# Code Architecture

Target *code* structure for the two codebases (the existing `ARCHITECTURE.md` covers the
*experiment/methodology* — different concern, untouched).

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
| R1.1b | **B023** (82, loop-var capture) — DEFERRED & ignored-with-note: almost all in the threaded fill ENGINE; audit needs the R4 test net. Also `SIM102`/`SIM115` ignored (stylistic / legit long-lived handles). | P1 | M | ●●● | ⬜ after R4 |
| R1.3b | mypy `disallow_untyped_defs` on `pipeline_core` (26 missing annotations, mostly trivial inner closures) — deferred; completeness not bugs. | P2 | M | ● | ⬜ |
| R1.x | import-linter contract (layer rules) → folded into **R2.3**. pytest adoption → **R4.1**. | — | — | — | — |

### R4 — Behavioral test suite  🔄 MOSTLY DONE  (P0 · unblocks safe engine refactors)
| # | step | Pri | Eff | Risk | status |
|---|---|---|---|---|---|
| R4.1 | **pytest** adopted; the drift-prone hand-rolled `main()` call-list replaced by a thin pytest delegator (one source of truth = collection); cargo coverage tests gated `@needs_cargo` (SMOKE_FULL=1); root `conftest.py` for imports; config in pyproject. check.sh + CI run `pytest`. | P0 | S | ● | ✅ 72 pass / 2 skip |
| R4.2 | `tests/test_domain.py` — 15 behavioural input→output tests for the pure parsers (`text`, `llm_output`, `test_results`). Lifted the **domain layer to 98.6%** (text/model_caps/enums 100%). | P0 | L | ● | ✅ |
| R4.4 | coverage gate: **`pipeline_core.domain` floored at 95%** in check.sh + CI (currently 98.6%). The application/ engine is intentionally not gated (mid-migration). | P2 | S | ● | ✅ |
| R4.3 | **DONE** (at the achievable granularity) — `tests/test_fill_replay.py`: characterisation tests for the fill engine's *deterministic core*. A faithful end-to-end replay of `_process_one_stub` (542L) is infeasible because it's a **closure capturing the whole `_task2_fill_fns_one_by_one` scope** + interleaves LLM/cargo/retro — extracting it from the closure IS the R3 work. So the net pins the transform that R3 must preserve: canned `<FN>` response → `extract_fn_bodies` → `_splice_fn_with_where` → filled stub (impl-method, where-clause-tighten, multi-member-one-response). No model, no cargo. | P1 | M | ●● | ✅ |

### R2 — Make the layering load-bearing  🔄 MOSTLY DONE  (P1 · the architecture was aspirational)
| # | step | Pri | Eff | Risk | status |
|---|---|---|---|---|---|
| R2.1 | **Orchestrator DELETED.** It was a fields-only dataclass with 0 method call sites (everything used the `ADAPTERS` global directly) AND it imported concrete `Adapters` (the sole application→adapters **DIP violation**). Removing it killed dead scaffolding + the violation in one move. A real driver is R2.2/R3, to be designed ports-first. | P1 | S | ● | ✅ |
| R2.3 | **import-linter** layers contract (`adapters > application > ports > domain`) in pyproject; gated in check.sh + CI. Passes (29 files, 39 deps) — the layer rule is now enforced, not just described. `log`/`retry`/`io` are foundational facades (not layers), so domain may import them. | P1 | S | ● | ✅ |
| R2.4 | `pipeline_core/__init__.py` declares the public API (`__all__`): the 10 domain record types + `Settings` + the 5 port Protocols. Adapters/tasks deliberately NOT re-exported (keeps `import pipeline_core` side-effect-free — no `model_interact` pull-in). | P2 | S | ● | ✅ |
| R2.2 | move `main()`'s (1321L) task dispatch into a **real** ports-first driver, one task at a time — DEFERRED: it's the engine-coupled rewrite, needs the R4.3 replay net; folds into R3. | P1 | L | ●●● | ⬜ with R3 |

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
| R7.4 | cfg-on-source (`_fn_cfg_predicates`, `_CFG_INLINE_RE`) → `enumerate_fns.cfg_predicates`. **●●● needs a live run — NOT batched.** Investigation found: (a) the regex `_fn_cfg_predicates` is **buggy** (its backward window over-captures preceding fns' cfgs); (b) `_eval_cfg_pred` detects operators with `s.startswith("all(")` — syn renders `all (…)` *with a space*, which would silently break detection → wrong cluster membership; (c) the extractor **duplicated** a free fn's cfg — **FIXED here** (`record_from_fn` trusts the stack; Rust test `fn_cfg_predicates_are_not_duplicated`). A faithful migration also needs the extractor to emit cfg in canonical no-space form (or normalise Python-side), then live-validate cluster assignment. | P1 | L | ●●● | 🔄 dup-fixed; rest blocked |
| R7.5 | `_IDIOM_*` in `_extract_crate_idioms` — **KEEP (justified)**: mines recurring *text patterns* (`Self::call`, struct literals, fn-ptr fields) to build a per-crate cheat-sheet, not structured facts. This is heuristic text-mining over filled source; an AST walk is possible but would be a large rewrite for a fuzzy hint, with no fidelity gain. | P2 | — | — | ✅ keep |
| R7.6 | `_condense_doc_attrs`/`_strip_attrs_for_view` (`_ATTR_OPENER_RE`), `_COMMENT_RE` in `validate_task2_output`, `_file_cfg_predicate_regex_fallback` — **KEEP (justified)**: the first two produce textual prompt **views** (syn re-emit would reformat the whole file and defeat the purpose); `_COMMENT_RE` is a non-emptiness heuristic; the last is the deliberate regex *fallback* for the syn path when the binary is absent. None extract structured facts. | P2 | — | — | ✅ keep |
### R8 — regex→structured parsers for NON-source formats  🔄 IN PROGRESS  (P1 · most regex bugs are here)
Principle: regex bugs cluster where a *structured format is scraped as text* (it drifts → silent mismatch).
| # | step | Pri | Eff | Risk | status |
|---|---|---|---|---|---|
| R8.1 | **Cargo.toml → `tomllib`** (stdlib). Replaced ~13 regex sites (package name, `[lib] proc-macro`, `[[bin]]`, `[dependencies]`, `[features].default`) with `_load_cargo_toml`/`_cargo_*` helpers. Handles comments/inline-tables/quoting the regex got wrong; test `test_cargo_toml_parsed_with_tomllib`. | P1 | M | ● | ✅ |
| R8.2 | test-result counting → structured — **NOT DONE (investigated, declined)**: stable Rust has **no** non-nightly structured test output (`--format json` needs `-Z unstable-options`); nextest's libtest-json is experimental. The text summaries (`test result: N passed`, pytest `N passed`) are stable de-facto contracts. Forcing JSON would couple to nightly/experimental — *more* fragile. The real cargo bug source (compile diagnostics) is already on JSON (`_cargo_diagnostics_from_json`). | P1 | — | — | ✅ declined w/ reason |
| R8.3 | finish compile-error text stragglers (`_COMPILE_ERROR_RE`) onto the existing `_cargo_diagnostics_from_json` where the caller has structured diags (legacy text-only fallback can stay). | P2 | S | ● | ⬜ |
| R8.4 | coverage `%` text-scraping → the coverage adapter's JSON/lcov (mostly already structured). | P3 | S | ● | ⬜ |
| R8.5 | model-protocol regex (`<FILE>`/`<FN>`) → tolerant state-machine parser (NOT model-JSON — local models adhere poorly). Only if model-output bugs recur. | P3 | M | ●● | ⬜ |

Outcome: every Rust-source regex is now accounted for — **migrated** (R7.1 `_extract_public_symbols`, R7.2 source side of `_PUB_ITEM_RE`), **kept with justification** (R7.3/R7.5/R7.6 — text-mining / display-views / heuristics / fallback, where syn is the wrong tool), or **flagged ●●● live-validation** (R7.4 cfg, with its real bugs documented and the extractor dup fixed). The "no regex on source" rule means *no regex for structured fact extraction*; text transformation/mining over source legitimately remains.
Note: syn-bridge sites depend on the extractor binary at runtime; their tests skip when it's not
built (CI Python job). Enumeration is covered by the Rust crate's tests in CI.

### R9 — convergence bugs surfaced by the session-51/52 live run  🔄 IMPLEMENTED (R9.3 tested; R9.1/R9.2 ●●● await live run)  (P0 · these cause the iteration spin)
Live run `pipeline.py=289f0a38 git=3d74425 --clean` (started 13:00 UTC). Leak fix (origin-cfg #4 + guard
#5) VALIDATED — 0 `E0658`, 0 `CLUSTER LEAK` across the whole run. But the run never converges: it loops
t0→t3→t4→regen because the pipeline cannot distinguish **"did not compile"** from **"tests ran"**.
Root theme (R9.1+R9.2): compile-state is inferred from test-result text, which is empty on a build failure.
R9.3 is independent (scratch-dir pollution). Dependency order: R9.3 → R9.1 → R9.2 (fix file set first so a
build failure is a real signal, then route it, then stop the retry trusting it).

| # | bug | root cause | fix SHIPPED | where | status |
|---|---|---|---|---|---|
| R9.1 | build failure had no fix path → TASK 4 (genesis analyzer) → genesis churn → full regen | inline retry **bailed at the gate** (`if not failed: return`) — a non-compiling tree emits 0 `FAILED` lines so it never even tried; `task2_fix_loop` is test-failure-driven | `_task3_inline_retry` now has a **BUILD mode**: when 0 tests FAILED, run `compile_cmd` (`<test_cmd> --no-run`) through `_run_cargo_check_diag`, attribute error diags→fns via `_frontier_from_diags`, re-splice per-fn (shared `_apply_per_fn_fixes`, which **skips frozen tests/ + gen_tests**). Metric = compile-error count (lower=better). Terminating recurse: return changed output only on strict error-count decrease, else the unchanged input → no infinite re-verify. Still-broken after this → TASK 4 fallback (documented limitation) | `pipeline.py:_task3_inline_retry` | 🔄 ●●● await live |
| R9.2 | inline retry kept a tree it just broke ("80→0 failing, keeping partial progress") | keep/revert used only `len(_parse_failed_tests(last_out))`; a non-compiling tree emits no `FAILED` lines → count 0 → read as success | keep/revert is now **compile-state aware**: `broke_build = init_ran and not final_ran` (via `_parse_test_counts` pass+fail>0); if the pre-fix tree ran tests and the post-fix tree no longer does → **revert from snapshot** (non-compiling is strictly worse than N failing). BUILD mode reverts when final error-count > initial | `pipeline.py:_task3_inline_retry` (post-loop) | 🔄 ●●● await live |
| R9.3 | task0b ingested 40 phantom `.rs` from scratch dirs → budget exhausted, real source dropped, TASK 4 at 99% | both rglob loops walked all of `project_dir` incl. `.skeleton/` + `.skeleton-filled/` (own `src/` + nested `target/.../out/*.rs`); `target_dir` guard only caught top-level `target/` | new `_iter_source_rs(project_dir)` prunes any path with a `_NON_SOURCE_DIRS` component (`.skeleton`, `.skeleton-filled`, `target`, `.git`); applied to BOTH the hash loop and the content loop | `task0b_discovery.py` | ✅ shipped + test |

Validation: R9.1/R9.2 are ●●● — only a live pipeline run exercises the build-failure → fix → re-test path
(check.sh cannot). R9.3 has an offline test (`test_task0b_iter_source_rs_prunes_scratch_dirs`). After the fix
the same crate run must reach a stable TASK 3 verdict (real pass/fail count, no `INCOMPLETE`→`no_tests`→`build`
oscillation) and stop regenerating from scratch each iteration.

R9 residual (SHIPPED, 🔄 ●●● await live): inline retry is now **cluster-aware** — task3 threads the primary
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

### R10 — e2e validation harness: kill the "●●● await live run" tax  ⬜ DESIGN  (P0 · the dev-cycle bottleneck)
Problem: every orchestration change is tagged ●●● and gated on a live run, but the only live target is
**anyhow** — huge AND it triggers the unconverged-cluster bugs, so a single-module run ran 5h and stalled
(session 57). Root mis-classification: most ●●● items validate **orchestration logic** (reacts to model+cargo
*output*), not **model quality** — they don't need a live model at all. Split validation into layers, fastest
first; only the genuine "does the model converge" question pays for a model, and never on anyhow.

| layer | what | speed | model? | cargo? | status |
|---|---|---|---|---|---|
| L0 | `check.sh`: ruff/mypy/import-linter + fake-transport (`test_llm_controller`) + fill-replay (`test_fill_replay`) | sub-sec | no | no | ✅ have |
| L1 | **scripted-model integration** + **fault-injection fixtures**: monkeypatch `pipeline._ask_raw`→canned responder; tiny fixture crate; drive each failure mode deliberately (non-compiling `<FN>`, length-trunc, cfg-dead method) and assert the routing/keep-revert/cap decision | seconds | scripted | cached/real | ⬜ |
| L2 | **cassette replay**: replay a recorded real run — `ask()` hits a `{prompt_sha→response}` cassette, cargo memoized by tree-hash. A prompt-changing edit shows **cassette MISSES at exactly the altered prompts** = precise blast radius, re-record only those | minutes | recorded | memoized | ⬜ |
| L3 | **micro fixture crate, REAL model**: purpose-built 1-module/2–3-fn crate that *converges in minutes* — the "smoke in tmp" reborn as a committed fixture. Validates model-in-loop happy path | minutes | live | real | ⬜ |
| L4 | full anyhow | hours | live | real | ⬜ release gate, background only |

Seam + seed (both verified to exist, session 58):
- **Injection seam = `pipeline._ask_raw`** (pipeline.py:596). `ask()` does all context-guard/budget work then calls the rebindable module global `_ask_raw = model_interact.ask`; a harness swaps it → scripted/cassette responder with zero changes to model_interact / controller / transport. One seam serves L1+L2.
- **Cassette seed already on disk.** `logs/events.jsonl` (7.9k `llm.request` / 7.7k `llm.response`) + `logs/prompts/` (content-addressed sha256 store of **both** prompts and responses). A builder walks events, pairs request→response, resolves shas to `logs/prompts/{sha}.txt`. No re-run needed to seed.

| # | step | Pri | Eff | Risk | status |
|---|---|---|---|---|---|
| R10.0 | **response-correlation one-liner** ✅: `llm.response` emit (pipeline.py:599) now carries `prompt_sha` (= the request's `_prompt_meta["sha256"]`), so cassette pairing is exact regardless of parallelism. Older recordings without it fall back to emission-order pairing (exact at p=1). | P0 | S | ● | ✅ |
| R10.1 | **scripted-seam harness** `tests/harness.py` — `install_scripted_model` (`_ask_per_fn_body`/`_ask_raw`), `install_scripted_cargo` (`_run_cargo`/`_run_cargo_check_diag`/`_frontier_from_diags`), `install_scripted_attribution` (`_attribute_failures`), `diag()` builds real domain Diagnostics; scripted values = constant/list/callable. **Micro fixture crate** ✅ `tests/fixtures/microcrate/` — deps-free, free-fn + `Option` parser + stateful `Counter` type + `Result`/error-enum, 6 tests (3 unit + 3 integration), `cargo test` 0.00s. Health-gated by `tests/test_microcrate.py` (SMOKE_FULL=1). | P0 | M | ●● | ✅ harness + crate (full-pipeline L3 run = R10.5) |
| R10.2 | **fault-injection cases** retiring the ●●● tax — `tests/test_e2e_routing.py`: R9.1 BUILD-mode fix path; R9.2 keep/revert in **both** BUILD mode (error-count worsens→revert / compiles→keep) and TEST mode (broke_build: ran→not-ran→revert). `tests/test_e2e_budget.py`: W3/W4(e) — sentinel-override vs explicit-bound, ctx-shrink, thinking-reserve-on-top. `tests/test_e2e_scheduling.py`: R11.3 determinism — extracted the pure level-grouping out of the fill closure into `_group_priority_levels(stubs, leaf_name_counts)` (engine-decoupled: plain data in/out; the call site is unchanged) and pinned equal-ref-count tiers, callee-before-caller order, totality, idempotence. cfg-filter already had `test_cluster_filter_type_methods_drops_cfg_dead_methods` (session 56). | P0 | M | ●● | ✅ |
| R10.3 | **cassette builder + replay runner** ✅: `tests/cassette.py` — `build_cassette` (events.jsonl + prompts/ → `{prompt_sha→response}`, prefers R10.0 `prompt_sha`, falls back to order-pairing; product/pipeline_sha filter), `install_cassette` (patches `_ask_raw`; strict→`CassetteMiss` or lenient→""; records misses = blast radius). Offline test `tests/test_cassette.py` (synthetic seed). Verified on real log: 128 entries from the 789ba6c p=1 tail. NOT yet wired to a full-run replay harness (needs the L3 fixture-crate run or cargo memoization R10.4). | P1 | M | ●● | 🔄 builder/replay ✅, full-run wiring ⬜ |
| R10.4 | **cargo memoization** ✅: `tests/cargo_cache.py` — `tree_hash` (digests `src/`+`tests/` *.rs) + `CargoCache` (`(tree_hash,cmd)→(rc,out)`, record/replay, hit/miss counters) + `install_cargo_cache` (wraps `_run_cargo`). A tree-changing fill → new key (real miss, never a stale hit). Offline tests `tests/test_cargo_cache.py`. | P1 | M | ●● | ✅ memo (full-replay wiring with R10.3 = ⬜) |
| R10.5 | L3 micro-crate as the on-demand real-model convergence smoke (nightly/manual, not per-change); anyhow demoted to L4 release gate. Crate + health gate exist (R10.1); **the full real-model pipeline run against it + the nightly wiring are pending** (needs a live model env). | P2 | S | ●●● | ⬜ |

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

### R11 — phase-c determinism & cluster-correctness (sessions 55–56)  ✅ SHIPPED (●●● convergence effect awaits live run)
Three changes from the convergence-diagnosis sessions, undocumented until now. R11.2 is **uncommitted** at
session-57 handover (commit message written, user commits); R11.1 + R11.3 are in `789ba6c`.

| # | change | root cause | fix | where | status |
|---|---|---|---|---|---|
| R11.1 | **stream.log thread-local race fix** | under `PHASE_C_PARALLELISM`>1 the W1/W2 backends shared one stream-log file handle → garbled token-salad interleave | `_sock_*` made thread-local: workers write `logs/stream.<threadname>.log`, the main thread keeps `logs/stream.log`. `_sock_open`'s `open("w")` truncation is per-handle so each stream is clean | model_interact.py / pipeline.py stream sink | ✅ committed (789ba6c) |
| R11.2 | **cluster-aware TYPES_IN_SCOPE (cfg-filter dead methods)** [Hard Rule 8] | TYPES_IN_SCOPE advertised EVERY method regardless of cluster cfg → in `anyhow_no_core_error` the model reached for std-gated ctors (`construct_from_context`, `new`, `from_boxed`) that are cfg-dead → trait-bound error every attempt; the correct `construct_from_display` was buried past the 10-method cap | extractor: `MethodRecord.cfg_predicates` = enclosing impl/trait cfg ∪ method cfg (impl- and trait-method paths). `models.py`: schema + round-trip. `pipeline.py`: `_cluster_filter_type_methods` drops methods whose cfg is definitively False for the cluster (undecidable kept = conservative, matches `_fn_belongs_to_cluster`), applied at both `_crate_type_records` sites. `_eval_cfg_pred` now **normalizes** comparison sets (else spaced `feature = "X"` never matched → silent no-op on the exact anyhow case) | model.rs, enumerate.rs, domain/models.py, pipeline.py (~9059, ~9582) | 🔄 **uncommitted** |
| R11.3 | **priority-level fill batching** [Hard Rule 9] | phase-c per-fn dispatch drained the whole frontier with a free-for-all queue → nondeterministic completion order → fill A changes B's file-context → unreproducible | `stubs` is leaf-first sorted; equal-ref-count stubs grouped into LEVELS; each level runs `min(P, level_size)` workers with a **barrier between levels** → callee level lands on disk before caller level → deterministic, scheduling-independent | pipeline.py:~10260 | ✅ committed (789ba6c) |

Residual: within-level snapshot race remains under p>1; `PHASE_C_PARALLELISM=1` (uncommitted `.env`) removes it
entirely during diagnosis. R11.2's full convergence effect (no_core impls reaching "tests pass" instead of "floor
stalled → reverted") is ●●● — exactly the kind of item R10's L1/L2 would let us validate without a live anyhow run.
Extractor binary must be rebuilt for R11.2 (`cargo build --release`); pipeline reads it live.

Important Todos:

#### W1+W2 — backend→transport + unified think-recovery controller  ✅ SHIPPED (●●● awaits live run)
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

`model_interact.py` backends are now THIN `*Transport`s (Ollama/vLLM/SGLang/HF/Claude): connect, stream
normalized `Delta`s, declare a `BudgetSpec`. No private caps, no followup. `ask()` unchanged → routes
through `LLMController`. Tests: `tests/test_llm_controller.py` (16, fake transport drives followup /
length-trunc / think-cap / early-stop / 400-retry / persist). check.sh green.
Generalization wins (todo #2): SGLang/HF previously had NO recovery — SGLang now captures
`reasoning_content` + gets followup; the ollama-compat followup's hardcoded `min(.,16384)` is now
ctx-budget-aware. Behaviour parity for the active vLLM path is intended-identical; ●●● ⇒ needs a live run.
NOTE (minor): ollama-compat no longer emits raw `<think>`/`</think>` to stream.log (splitter strips them);
vLLM never did, so monitor's think-phase display is unaffected for the active backend.
#### W3 — ctx-budget-aware sweep of hardcoded caps  ✅ SHIPPED (●● tests; live run for full effect)
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

