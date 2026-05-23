# Implementation Plan

Derived from session-35 analysis of pipeline.py, prompt quality, and backlog audit.
Items ordered by phase; within each phase, lowest-risk first.

---

## Phase 1 — Bug fixes (no behaviour changes to LLM interaction)

| ID | File | Change |
|---|---|---|
| BL-026 | pipeline.py ~L9816 | Replace `sys.exit(1)` with `return (coverage, covered_lines, False)` — hard-block bypassed the outer cluster loop |
| BL-030 | pipeline.py ~L3377 | Add `ceiling_passes: int = 0` to `ModuleAugmentState`; require >= 2 consecutive `at_ceiling=True` before returning `'ceiling'`; reset on non-ceiling pass |
| BL-029 | pipeline.py ~L9833 | Delete dead `task0c_coverage_augmentation()` function (never called from main() since session 22) |
| BL-033 | pipeline.py ~L9810 | Add `file_retrospectives.get(sf, "")` to stalled-files log so hard-block report includes retrospective classification |
| BL-032 | pipeline.py ~L10742 | Use `"error"` status in `_mark_phase` for pre-check failure path; update monitor.py to render error state distinctly |

---

## Phase 2 — Portability / hallucination reduction

| ID | File | Change |
|---|---|---|
| backlog P0 | pipeline.py TASK 0b prompt ~L4369 | Remove three hardcoded anyhow examples from RULES section; replace with generic equivalents |
| backlog P1 | pipeline.py TASK 0c gen prompt ~L9292 | Remove INTERNAL MODULE RULES section (anyhow-specific no_std, alloc, error semantics); replace with runtime-derived facts from coverage_strategy.md |
| backlog P1 | pipeline.py `_PRELUDE_ITEMS` ~L9437 | Gate alloc prelude injection on `#![no_std]` detection in crate source |
| backlog P1 | pipeline.py `_FILE_CONSTRAINTS` ~L9180 | Move anyhow-specific per-file rules out of pipeline into a project-local config file (e.g. `tmp/argh-master/.pipeline_constraints.toml`) |
| backlog P1 | pipeline.py | After TASK 4 updates genesis.md, regenerate coverage_strategy.md from the updated genesis — currently it is written once by TASK 0b and never updated |

---

## Phase 3 — Context quality improvements

| ID | File | Change |
|---|---|---|
| BL-033 stalled log | pipeline.py | Surface full retrospective per stalled file in hard-block report (already partially in Phase 1) |
| TASK 4 genesis cap | pipeline.py L6656 | Increase `_t4_genesis_cap = 3_000` to `INTERMEDIATE_CHAR_LIMIT` so TASK 4 can see middle sections of growing genesis |
| TASK 1 guardrail | pipeline.py TASK 1 prompt | Add explicit "derive only from tests, never invent" guardrail to TASK 1 system prompt |
| TASK 2 blind | pipeline.py TASK 2 | Pass previous iteration's generated code as context so TASK 2 can compare and improve rather than starting blind |
| old_block placement | pipeline.py `_build_task0c_prompt_sections()` ~L4419 | Move "don't repeat previous block" instruction immediately adjacent to the previous block content |
| what_worked lines | pipeline.py ~L9719 | Record which specific lines/branches each successful pattern covered (currently stores only signatures) |

---

## Phase 4 — Structural / ownership improvements

| ID | Scope | Change |
|---|---|---|
| S1 TASK 0a blind | pipeline.py | Pass test file content (not just file paths) to TASK 0a profile builder |
| S2 profile stale | pipeline.py | Invalidate TASK 0a profile when test file content hash changes |
| S3 public symbols | pipeline.py `_extract_public_symbols` | Return full signatures + field shapes, not just names |
| S4 TASK 0c cross-file | pipeline.py TASK 0c | Provide cross-file type context when generating tests file-by-file |
| S5 task0c_augment_module | pipeline.py | Remove Rust-specific orchestration assumptions from TASK 0c module loop |

---

## Truncation backlog (from session-34 handover T1–T6)

These are all single-line edits replacing hardcoded slices with `MODEL_CONTEXT_CHARS`-derived limits:

| ID | Location | Current | Fix |
|---|---|---|---|
| T1 | L707, L1063 | `[:4000]` | remove cap; context-guard handles overflow |
| T2 | ~L3558 | `prev_content[:1500]` | remove cap |
| T3 | ~L3793 | `smart_truncate(total_limit=6000)` | `smart_truncate(total_limit=INTERMEDIATE_CHAR_LIMIT)` |
| T4 | ~L4510 | compile error `[:800]` | remove cap |
| T5 | ~L4514 | file content `[:6000]` | remove cap |
| T6 | L2544, L2921 | `smart_truncate` 8192-era values | replace with `INTERMEDIATE_CHAR_LIMIT` |

---

## Commit strategy

After each phase: `bash check.sh` must pass clean, then commit. Do not batch phases into a single commit.
