# Prompt-content audit — which spec docs reach which LLM prompt

Offline trace (CODE_ARCHITECTURE.md line 887). Answers: per task, which of the four
spec docs — `coverage_strategy.md` (CS), `functional_requirements.md` (FR),
`genesis.md` (GEN), `api_surface` (API) — actually reach the model prompt, and where
in code that injection happens. Pinned executable by `tests/test_prompt_content_audit.py`
(a missed injection = a failing assertion, not a silent drop).

Legend: `P`=produces the doc · `✓`=injected into this task's prompt · `R`=regenerates ·
`—`=not sent (by design). RepoSpec = the FR+GEN+API bundle rendered by
`prompts/repo_spec.py:render_repo_spec` (relevance-selected, budget-tiered).

## Matrix

| Task | CS | FR | GEN | API | RepoSpec bundle | Evidence (file:line) |
|---|---|---|---|---|---|---|
| 0b discovery | P | — | — | — | — | `tasks/task0b_discovery.py:248` emits CS |
| 0c augment | ✓ | — | — | — | — (by design) | `ports/augment.py:75` `augment(... coverage_strategy, project_context)`; RepoSpec NOT injected (CODE_ARCHITECTURE.md line 772) |
| 1 requirements | ✓ | P | ✓ | ✓ | — | `task1_synthesizer.py:171` CS, `:175` API via `format_api_surface_section`, `:179-188` GEN, FR is the output |
| 2 generate (group/whole-file) | ✓ | ✓ | ✓(rules) | ✓ | — (FR/CS interpolated directly) | `task2_generator.py:766` CS, `:774` FR; `genesis_rules` complement via `_spec_block` |
| 2 generate (per-fn / per-impl fill) | — | ✓ | ✓ | ✓ | ✓ | `prompts/per_fn.py:94`, `prompts/per_impl.py:99` `repo_spec_section(fr_path,...)` |
| 3 inline-retry | — | ✓ | ✓ | ✓ | ✓ | reuses per-fn/per-impl builders (same `repo_spec_section` path) |
| 4 failure analysis | R | ✓ | P/upd | ✓ | — | `tasks/task4.py:216` FR + `:254` shown as context; regenerates CS via `failure_analysis.py:87` |

## Channels (how each doc reaches a prompt)

| Channel | Carrier | Tasks served | Pinned by |
|---|---|---|---|
| direct interpolation | task-local prompt f-strings | 1 (`_single_prompt`), 2 (group/whole-file), 4 | `test_prompt_content_audit.py::test_task1_*`, `test_task2_*` |
| RepoSpec bundle | `prompts/runtime.repo_spec_section` facade → `render_repo_spec` | 2 (fill), 3 | `test_repo_spec.py`, `test_prompt_content_audit.py::test_per_fn_*` |
| port arg | `ports/augment.augment(... coverage_strategy=)` | 0c | `test_cluster_augment.py` |

## Non-injections (deliberate — asserted so they can't silently change)

- 0c (t0 pre-strip augmentation) does NOT carry the RepoSpec bundle — pre-strip phase,
  source still present (`test_per_fn_prompt_omits_repo_spec_when_unbound` covers the
  inert-facade default that 0c relies on).
- per-fn fill prompts do NOT carry the raw `coverage_strategy` doc — its useful content
  is folded into FR/genesis upstream; the fill prompt carries RepoSpec instead.
