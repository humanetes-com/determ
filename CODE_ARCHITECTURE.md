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
