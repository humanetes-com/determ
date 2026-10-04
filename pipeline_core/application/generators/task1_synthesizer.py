"""TASK 1 requirements-synthesizer engine (StaticTask1Synthesizer / LLMTask1Synthesizer).

Relocated verbatim from pipeline.py. These are the LLM use cases that turn the
test files + skeleton API surface into functional_requirements.md. Truly-importable
collaborators (domain text/output helpers, retry, io, log) are imported; the
pipeline-bound collaborators (the `ask` LLM call, the chunk-cache + prompt-budget +
type-name + api-surface + FR-preserving-write helpers, which carry pipeline DB / regex
deps) and the config constants are injected once at the composition root via configure().

Backend selection: TASK1_SYNTHESIZER in .env (static | llm) — see _task1_synthesizer_factory
in pipeline.py.
"""
from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ...chunk_cache import chunk_hash as _chunk_hash
from ...chunk_cache import load_cached_chunk as _load_cached_chunk
from ...chunk_cache import save_cached_chunk as _save_cached_chunk
from ...domain.generation import Task1Config
from ...domain.llm_output import extract_files
from ...domain.text import smart_truncate, trim_to_line
from ...io import read, write
from ...llm import InputTooLong
from ...log import log as _log
from ...ports.synthesizer import (
    Progress,
    PromptShaping,
    Requirements,
    Task1LLM,
)
from ...prompts.genesis_seed import GENESIS_SEED
from ...retry import with_retry
from ...threads import ContextPool


class Task1Synthesizer(ABC):
    """Analyze test files and produce functional_requirements.md (TASK 1)."""

    @abstractmethod
    def synthesize(
        self,
        project_dir: Path,
        genesis_path: Path,
        functional_req_path: Path,
        test_files: list[tuple[str, str]],
        readme_text: str,
        project_profile: str = "",
        project_context: str = "",
        coverage_strategy: str = "",
        api_surface: str = "",
        known_types: list[str] | tuple[str, ...] = (),
    ) -> bool:
        """Write functional_requirements.md (and genesis stub if absent). Returns True on success.

        api_surface (when provided) is the pre-strip structural skeleton of the module —
        public signatures, type definitions, function bodies elided. It is structural
        ground truth: TASK 1 should derive BEHAVIOR from the tests, but not invent
        signatures or types that conflict with this skeleton.

        known_types (when provided) is the pre-strip, syn-extracted list of every
        struct/enum/type-alias/trait name in the module — deterministic ground truth
        for "what types exist", used to stop the chunked path re-describing the same
        type in every chunk without needing to guess it from prior LLM output.
        """


@dataclass(frozen=True, slots=True)
class StaticTask1Synthesizer(Task1Synthesizer):
    """
    Current implementation: extract behavioral requirements in natural language,
    then synthesize into functional_requirements.md.
    Uses a single LLM call for small projects and a chunked+hierarchical approach
    for large ones.
    """
    llm: Task1LLM
    shaping: PromptShaping
    progress: Progress
    requirements: Requirements
    cfg: Task1Config


    def synthesize(self, project_dir: Path, genesis_path: Path, functional_req_path: Path,
                   test_files: list[tuple[str, str]], readme_text: str,
                   project_profile: str = "", project_context: str = "",
                   coverage_strategy: str = "", api_surface: str = "",
                   known_types: list[str] | tuple[str, ...] = ()) -> bool:
        # Injected collaborators/config bound locally (bodies verbatim).
        SINGLE_CALL_CHAR_LIMIT = self.cfg.single_call_char_limit
        existing_genesis = read(genesis_path)
        total_chars = sum(len(c) for _, c in test_files)

        if total_chars <= SINGLE_CALL_CHAR_LIMIT:
            ok = with_retry(lambda: self._single(
                genesis_path, functional_req_path, test_files, readme_text,
                existing_genesis, project_profile,
                project_context=project_context,
                coverage_strategy=coverage_strategy,
                api_surface=api_surface,
            ))
        else:
            ok = self._chunked(
                genesis_path, functional_req_path, test_files, readme_text,
                existing_genesis, project_profile,
                project_context=project_context,
                coverage_strategy=coverage_strategy,
                api_surface=api_surface,
                known_types=known_types,
            )
        return ok

    def _single(self, genesis_path: Path, functional_req_path: Path,
                test_files: list[tuple[str, str]], readme_text: str, existing_genesis: str,
                project_profile: str = "", project_context: str = "",
                coverage_strategy: str = "", api_surface: str = "") -> bool:
        # Injected collaborators/config bound locally (bodies verbatim).
        MODEL = self.cfg.model
        SINGLE_CALL_CHAR_LIMIT = self.cfg.single_call_char_limit
        _TASK1_MODEL = self.cfg.task1_model
        _write_fr_preserving_notes = self.requirements.write_fr_preserving_notes
        ask = self.llm.ask
        format_test_files = self.shaping.format_test_files
        genesis_exists = bool(existing_genesis.strip())
        _full_tests_section = format_test_files(test_files)
        profile_section = (f"\n--- Project profile (language, build system, workspace layout) ---\n"
                           f"{project_profile}\n") if project_profile.strip() else ""
        context_section = (f"\n--- Project context (behavioral summary) ---\n"
                           f"{project_context}\n") if project_context.strip() else ""

        if not genesis_exists and not genesis_path.exists():
            write(genesis_path, GENESIS_SEED)

        # Budget the tests_section against the actual prompt overhead. The
        # single-call path is taken when tests are expected to fit, but
        # measure to be safe — overflow would otherwise hit the context
        # guard's middle-trim, which can split a test file mid-content.
        _probe = self._single_prompt(
            "", readme_text, profile_section, existing_genesis,
            coverage_strategy=coverage_strategy, api_surface=api_surface,
            context_section=context_section,
        )
        _tests_budget = max(2_000, SINGLE_CALL_CHAR_LIMIT - len(_probe))
        tests_section = smart_truncate(_full_tests_section, total_limit=_tests_budget,
                                       head=_tests_budget // 3,
                                       tail=2 * _tests_budget // 3)

        prompt = self._single_prompt(
            tests_section, readme_text, profile_section, existing_genesis,
            coverage_strategy=coverage_strategy, api_surface=api_surface,
            context_section=context_section,
        )
        response = ask(prompt, model=_TASK1_MODEL)
        files = extract_files(response)
        if "functional_requirements.md" in files:
            _write_fr_preserving_notes(functional_req_path, files["functional_requirements.md"])
            return True
        # Fallback: model returned content without FILE markers.
        stripped = response.strip()
        if stripped and len(stripped) > 200:
            _log("  WARNING: no FILE markers — using raw response as requirements.")
            _write_fr_preserving_notes(functional_req_path, stripped)
            return True
        _log(f"  ERROR: {MODEL} did not return functional_requirements.md.")
        _log(f"  Raw response snippet: {response[:500]}")
        return False

    def _single_prompt(self, tests_section: str, readme_text: str, profile_section: str,
                       existing_genesis: str, coverage_strategy: str = "",
                       api_surface: str = "", context_section: str = "") -> str:
        """Return the prompt for single-call requirements extraction (overridable)."""
        # Injected collaborators/config bound locally (bodies verbatim).
        FILE_FORMAT_INSTRUCTION = self.cfg.file_format_instruction
        _README_CAP = self.cfg.readme_cap
        _STRATEGY_CAP = self.cfg.strategy_cap
        _TASK1_THINKING_DIRECTIVE = self.cfg.thinking_directive
        _format_api_surface_section = self.shaping.format_api_surface_section
        strategy_section = (
            f"\n--- Coverage strategy (import patterns, type construction, test conventions) ---\n"
            f"{trim_to_line(coverage_strategy, _STRATEGY_CAP, "\n... (strategy truncated)")}\n"
        ) if coverage_strategy.strip() else ""
        api_section = _format_api_surface_section(api_surface)
        # genesis.md is always shown — when empty it carries a placeholder so the
        # LLM understands it is the document that will accumulate retrospective
        # findings across iterations and should be respected even before first fill.
        if existing_genesis.strip():
            genesis_section = (
                f"\n--- genesis.md (methodology — read-only here; do not output or modify) ---\n"
                f"{existing_genesis}\n"
            )
        else:
            genesis_section = (
                "\n--- genesis.md (methodology — empty for now; will be filled by retrospectives in later iterations) ---\n"
                "(empty)\n"
            )
        return f"""You are working on a deterministic AI-driven software reconstruction experiment.
{_TASK1_THINKING_DIRECTIVE}{FILE_FORMAT_INSTRUCTION}
Your output MUST be the file `functional_requirements.md` wrapped in the FILE markers above.

The goal: given a project's test files and README, produce functional requirements precise
enough that any implementation satisfying them will make all tests pass.

Use the methodology in genesis.md below to guide your analysis (it may be empty on the
first iteration — that is expected; it accumulates retrospective findings across loops).
Use the README, project profile, project context, and test files to understand the
language, framework, and project structure.

README.md:
{readme_text[:_README_CAP]}
{profile_section}{context_section}{strategy_section}{api_section}
Test files:
{tests_section}
{genesis_section}
Your task: produce functional_requirements.md. Requirements must be complete enough that
any implementation satisfying them will pass all tests.

For each public function, method, or trait implementation, capture ALL of the following
(not just the signature):
- What it returns: the format, structure, and content of the return value.
- Behavioral invariants: what must always be true of the output?
- Any concrete examples visible in test assertions (copy them verbatim if present).
A requirement like `fn generate() -> String` is insufficient. A requirement like
`fn generate() -> String — returns a shell completion script string; tests assert it
contains flag names and descriptions in the expected format` is actionable.

CRITICAL: Every requirement must trace directly to a test call or assertion in the files above.
Do NOT add items based on general knowledge of the crate, language, or domain.
If no test calls it, do not include it.

Test helpers vs. production API:
- Do NOT include `fn test_*` or `#[test]` functions — these are the tests, not the code under test.
  Extract the APIs that the test functions CALL (the production symbols being exercised).
- Do NOT include test fixtures, mock builders, or helper utilities defined in the test files.
  Only extract the real production API the tests depend on.
"""

    def _synthesize_group(self, group_partials: list[str], group_idx: int, total_groups: int,
                          readme_text: str, profile_section: str, genesis: str = "",
                          coverage_strategy: str = "", api_surface: str = "") -> str | None:
        """
        Stage-1 hierarchical synthesis: merge a group of raw chunk partials into a
        single condensed intermediate summary. Called once per group.
        Returns the summary text, or None on failure.
        """
        # Injected collaborators/config bound locally (bodies verbatim).
        SINGLE_CALL_CHAR_LIMIT = self.cfg.single_call_char_limit
        _README_CAP = self.cfg.readme_cap
        _TASK1_MODEL = self.cfg.task1_model
        _format_api_surface_section = self.shaping.format_api_surface_section
        ask = self.llm.ask
        genesis_section = (
            f"\n--- genesis.md (methodology — apply when merging requirements) ---\n"
            f"{genesis}\n"
        ) if genesis.strip() else ""
        api_section = _format_api_surface_section(api_surface)

        # Fill-down: render the scaffold with sized sections empty so the
        # overhead is exact (no hardcoded estimate).
        _readme_cap_str = readme_text[:_README_CAP]
        _merge_scaffold = f"""You are merging a group of partial functional requirements extracted from test files.

This is group {group_idx} of {total_groups}. Each entry was extracted independently from a chunk
of test files. Your task: produce a single, deduplicated, concise summary of all requirements
in this group. Preserve all specific types, function signatures, error conditions, and invariants.
Remove duplicate entries. Do not invent new requirements not present in the input.

README.md (for language/framework context):
{_readme_cap_str}

Partial requirements to merge:


Output a plain structured list of the merged requirements. No filler, no code, no headers.
"""
        _fixed_overhead = (
            len(_merge_scaffold) + len(profile_section)
            + len(genesis_section) + len(api_section)
        )
        _combined_cap = max(10_000, SINGLE_CALL_CHAR_LIMIT - _fixed_overhead)
        _combined_raw = "\n\n".join(group_partials)
        combined = smart_truncate(_combined_raw, total_limit=_combined_cap,
                                  head=_combined_cap // 3, tail=2 * _combined_cap // 3)

        prompt = f"""You are merging a group of partial functional requirements extracted from test files.

This is group {group_idx} of {total_groups}. Each entry was extracted independently from a chunk
of test files. Your task: produce a single, deduplicated, concise summary of all requirements
in this group. Preserve all specific types, function signatures, error conditions, and invariants.
Remove duplicate entries. Do not invent new requirements not present in the input.
{profile_section}{genesis_section}{api_section}
README.md (for language/framework context):
{_readme_cap_str}

Partial requirements to merge:
{combined}

Output a plain structured list of the merged requirements. No filler, no code, no headers.
"""

        def attempt() -> str | Literal[False]:
            result = ask(prompt, model=_TASK1_MODEL)
            if not result or not result.strip():
                return False
            return result

        raw = with_retry(attempt)
        return str(raw) if raw is not False else None

    def _chunked(self, genesis_path: Path, functional_req_path: Path,
                 test_files: list[tuple[str, str]], readme_text: str, existing_genesis: str,
                 project_profile: str = "", project_context: str = "",
                 coverage_strategy: str = "", api_surface: str = "",
                 known_types: list[str] | tuple[str, ...] = ()) -> bool:
        # Injected collaborators/config bound locally (bodies verbatim).
        CHUNK_CACHE_DIR = self.cfg.chunk_cache_dir
        SYNTHESIS_GROUP_SIZE = self.cfg.synthesis_group_size
        _TASK1_MODEL = self.cfg.task1_model
        _PARALLELISM = max(1, self.cfg.parallelism)
        ChunkProgress = self.progress.make_progress
        _write_fr_preserving_notes = self.requirements.write_fr_preserving_notes
        ask = self.llm.ask
        chunk_test_files_by_file = self.shaping.chunk_test_files_by_file
        format_test_files = self.shaping.format_test_files
        prompt_budget = self.shaping.prompt_budget
        # Deterministic, syn-extracted type manifest (baked pre-strip — see
        # _capture_api_surface) — known up front, so every chunk gets the SAME
        # header instead of an LLM-output-guessed one accumulated chunk-by-chunk.
        # That accumulation was the only thing forcing chunks to run in sequence;
        # a fixed header lets every chunk needing a model call run concurrently.
        type_manifest_header = ""
        if known_types:
            type_list = ", ".join(sorted(known_types)[:40])
            type_manifest_header = (
                f"\nTypes defined in this module (do NOT re-describe these in full — "
                f"only add NEW requirements not yet covered): {type_list}\n"
            )

        existing_genesis = read(genesis_path)
        genesis_hash = hashlib.sha256(existing_genesis.encode()).hexdigest()[:8]

        profile_header = (f"\nProject profile:\n{project_profile}\n") if project_profile.strip() else ""
        context_header = (f"\nProject context (behavioral summary):\n{project_context}\n"
                          ) if project_context.strip() else ""
        chunk_extra_header = profile_header + context_header + type_manifest_header

        # Budget against the REAL fixed prompt (everything except tests_section),
        # not a hand-maintained estimate: the old sum silently omitted api_surface
        # (capped ~SINGLE_CALL//2 chars) and undercounted the static template, so
        # chunks were sized as if the API section were empty → prompt overflowed
        # ctx (InputTooLong, whole-run abort). Rendering the fixed prompt once with
        # empty tests is deterministic and self-corrects when any section grows.
        _fixed_prompt = self._chunk_prompt(
            1, 1, readme_text, "", chunk_extra_header,
            genesis=existing_genesis, coverage_strategy=coverage_strategy,
            api_surface=api_surface,
        )
        _chunk_content_budget = prompt_budget(_fixed_prompt)
        chunks = chunk_test_files_by_file(test_files, _chunk_content_budget)
        total_chunks = len(chunks)
        _log(f"  Extracting requirements file-by-file: {total_chunks} file(s) → one call each "
             f"(parallelism={_PARALLELISM}).")
        _log("  Calibration in progress, progress estimations will be available soon...")

        root = genesis_path.parent
        partials_dir = root / ".task1_partials"
        cache_dir = root / CHUNK_CACHE_DIR
        partials_dir.mkdir(exist_ok=True)
        cache_dir.mkdir(exist_ok=True)

        progress = ChunkProgress(total=total_chunks)
        cache_hits = 0

        partial_requirements: list[str | None] = [None] * total_chunks
        pending: list[tuple[int, str, Path, str]] = []

        # Pass 1 (sequential, no LLM): resolve partial-file / cache hits — cheap,
        # and must run before dispatching model calls so cache state is settled.
        for i, chunk in enumerate(chunks, 1):
            partial_file = partials_dir / f"chunk_{i:03d}_of_{total_chunks:03d}.txt"
            tests_section = format_test_files(chunk)
            # Include genesis hash so cache is invalidated when TASK 4 updates genesis.md.
            h = _chunk_hash(tests_section + genesis_hash)

            if partial_file.exists():
                partial_requirements[i - 1] = partial_file.read_text()
                progress.finish_chunk(i)
                continue

            cached = _load_cached_chunk(cache_dir, h)
            if cached:
                cache_hits += 1
                partial_file.write_text(cached)
                partial_requirements[i - 1] = cached
                _log(f"  Chunk {i}/{total_chunks}: cache hit (skipping model call).")
                progress.finish_chunk(i)
                continue

            pending.append((i, tests_section, partial_file, h))

        # Pass 2 (concurrent, bounded): every remaining chunk needs a model call.
        # No cross-chunk data dependency now that the type manifest is fixed up
        # front, so these can run in parallel — same pattern as task2a cluster
        # concurrency (PIPELINE_PARALLELISM).
        overflow_chunks: set[int] = set()

        def _run_chunk(item: tuple[int, str, Path, str]) -> tuple[int, str | None]:
            i, tests_section, partial_file, h = item
            prompt = self._chunk_prompt(i, total_chunks, readme_text, tests_section,
                                       chunk_extra_header,
                                       genesis=existing_genesis, coverage_strategy=coverage_strategy,
                                       api_surface=api_surface)

            def chunk_attempt(p: str = prompt) -> str | Literal[False]:
                result = ask(p)
                if not result or not result.strip():
                    return False
                return result

            progress.start_chunk()
            try:
                partial = with_retry(chunk_attempt)
            except InputTooLong as exc:
                # A chunk that still overflows ctx after the controller's
                # replan degrades (skip it, keep the others) — never aborts
                # the whole pipeline mid-run.
                progress.finish_chunk(i)
                overflow_chunks.add(i)
                _log(f"  WARNING: chunk {i}/{total_chunks} exceeds the model context "
                     f"even after replan ({exc}) — skipped; its requirements are "
                     "missing from this iteration's FR.")
                return i, None
            progress.finish_chunk(i)
            if partial is False:
                _log(f"  ERROR: failed to extract requirements from chunk {i} after all retries.")
                return i, None

            entry = f"=== Requirements from chunk {i}/{total_chunks} ===\n{partial}"
            partial_file.write_text(entry)
            _save_cached_chunk(cache_dir, h, entry)
            return i, entry

        if pending:
            workers = min(_PARALLELISM, len(pending))
            with ContextPool(max_workers=workers, thread_name_prefix="task1-chunk") as pool:
                for i, entry in pool.map(_run_chunk, pending):
                    partial_requirements[i - 1] = entry

        if any(entry is None and (idx + 1) not in overflow_chunks
               for idx, entry in enumerate(partial_requirements)):
            return False
        final_requirements: list[str] = [e for e in partial_requirements if e is not None]

        progress.finish()

        if cache_hits:
            _log(f"  Cache: {cache_hits}/{total_chunks} chunks reused from previous iterations.")

        genesis_exists = bool(existing_genesis.strip())
        profile_section = (f"\n--- Project profile (language, build system, workspace layout) ---\n"
                           f"{project_profile}\n") if project_profile.strip() else ""
        context_section_chunked = (f"\n--- Project context (behavioral summary) ---\n"
                                   f"{project_context}\n") if project_context.strip() else ""
        # Concatenate profile + context so synthesizers downstream see both as one
        # passed-through section (avoids changing every call signature).
        profile_section = profile_section + context_section_chunked

        groups = [final_requirements[i:i + SYNTHESIS_GROUP_SIZE]
                  for i in range(0, len(final_requirements), SYNTHESIS_GROUP_SIZE)]
        total_groups = len(groups)
        intermediates: list[str] = []

        if total_groups > 1:
            _log(f"  Stage 1: merging {total_chunks} partials into {total_groups} intermediate groups...")
            for g_idx, group in enumerate(groups, 1):
                inter_file = partials_dir / f"intermediate_{g_idx:03d}_of_{total_groups:03d}.txt"

                if inter_file.exists():
                    _log(f"  Group {g_idx}/{total_groups}: already done, loading from disk.")
                    intermediates.append(inter_file.read_text())
                    continue

                _log(f"  Merging group {g_idx}/{total_groups} ({len(group)} partials)...")
                summary = self._synthesize_group(group, g_idx, total_groups, readme_text, profile_section,
                                                genesis=existing_genesis, coverage_strategy=coverage_strategy,
                                                api_surface=api_surface)
                if not summary:  # _synthesize_group returns str | None
                    _log(f"  ERROR: group {g_idx} merge failed after all retries.")
                    return False

                inter_file.write_text(summary)
                intermediates.append(summary)

            all_for_synthesis = "\n\n".join(
                f"=== Group {i+1}/{total_groups} ===\n{s}" for i, s in enumerate(intermediates)
            )
            _log(f"  Stage 1 complete. Intermediate total: {len(all_for_synthesis):,} chars.")
        else:
            all_for_synthesis = "\n\n".join(final_requirements)

        _log("  Stage 2: final synthesis into output documents...")

        if not genesis_exists and not genesis_path.exists():
            write(genesis_path, GENESIS_SEED)

        synthesis_prompt = self._synthesis_prompt(
            readme_text, profile_section, existing_genesis, all_for_synthesis,
            coverage_strategy=coverage_strategy,
            project_dir=genesis_path.parent,
            api_surface=api_surface,
        )

        def synthesis_attempt() -> bool:
            response = ask(synthesis_prompt, model=_TASK1_MODEL)
            files = extract_files(response)
            if "functional_requirements.md" in files:
                _write_fr_preserving_notes(functional_req_path, files["functional_requirements.md"])
                return True
            # Fallback: model returned content without FILE markers.
            # Accept the raw response if it's non-empty and looks like requirements
            # (not an error message). The model produces correct content — wrong format.
            stripped = response.strip()
            if stripped and len(stripped) > 200:
                _log("  WARNING: no FILE markers in synthesis response — using raw response as requirements.")
                _write_fr_preserving_notes(functional_req_path, stripped)
                return True
            _log("  ERROR: synthesis returned empty or unusable response.")
            _log(f"  Raw response snippet: {response[:300]}")
            return False

        ok = with_retry(synthesis_attempt)
        if ok:
            for f in partials_dir.glob("*.txt"):
                f.unlink()
            with suppress(OSError):
                partials_dir.rmdir()
        return ok

    def _synthesis_prompt(self, readme_text: str, profile_section: str,
                          existing_genesis: str, all_for_synthesis: str,
                          coverage_strategy: str = "",
                          project_dir: Path | None = None,
                          api_surface: str = "") -> str:
        """Return the Stage-2 synthesis prompt (overridable by subclasses)."""
        # Injected collaborators/config bound locally (bodies verbatim).
        FILE_FORMAT_INSTRUCTION = self.cfg.file_format_instruction
        SINGLE_CALL_CHAR_LIMIT = self.cfg.single_call_char_limit
        _README_CAP = self.cfg.readme_cap
        _STRATEGY_CAP = self.cfg.strategy_cap
        _format_api_surface_section = self.shaping.format_api_surface_section
        type_signals_read = self.requirements.type_signals_read
        strategy_section = (
            f"\n--- Coverage strategy (import patterns, type construction, test conventions) ---\n"
            f"{trim_to_line(coverage_strategy, _STRATEGY_CAP, "\n... (strategy truncated)")}\n"
        ) if coverage_strategy.strip() else ""
        api_section = _format_api_surface_section(api_surface)

        # BL-022: inject type difficulty signals so synthesis focuses on hard types.
        type_signals_section = ""
        if project_dir is not None:
            type_signals_text = type_signals_read(project_dir)
            if type_signals_text:
                type_signals_section = (
                    f"\n--- Type difficulty signals from test coverage (prioritize these) ---\n"
                    f"{type_signals_text}\n"
                )

        _synthesis_cap = int(SINGLE_CALL_CHAR_LIMIT * 0.60)
        _capped_synthesis = smart_truncate(all_for_synthesis, total_limit=_synthesis_cap,
                                           head=_synthesis_cap // 3, tail=2 * _synthesis_cap // 3)

        genesis_exists = bool(existing_genesis.strip())
        if genesis_exists:
            return f"""You are synthesizing requirements into a final requirements document.
{FILE_FORMAT_INSTRUCTION}
Your output MUST be the file `functional_requirements.md` wrapped in the FILE markers above.

Use the methodology in genesis.md below to guide synthesis.
Derive the language and framework from the README and requirements below.

README.md:
{readme_text[:_README_CAP]}
{profile_section}{strategy_section}{api_section}{type_signals_section}
--- genesis.md (read-only -- do not output or modify it) ---
{existing_genesis}

Requirements (merged from all test chunks):
{_capped_synthesis}

Your task: produce functional_requirements.md -- the complete, deduplicated, final
requirements for this specific project.

CRITICAL: Only include items present in the chunk extractions above.
Do NOT add requirements based on general knowledge of the project or language.
"""
        return f"""You are synthesizing requirements into a final requirements document.
{FILE_FORMAT_INSTRUCTION}
Your output MUST be the file `functional_requirements.md` wrapped in the FILE markers above.

Derive the language and framework from the README and requirements below.

README.md:
{readme_text[:_README_CAP]}
{profile_section}{strategy_section}{api_section}{type_signals_section}
Requirements (merged from all test chunks):
{_capped_synthesis}

Your task: produce functional_requirements.md -- the complete, deduplicated, final
requirements for this specific project.

CRITICAL: Only include items present in the chunk extractions above.
Do NOT add requirements based on general knowledge of the project or language.
"""

    def _chunk_prompt(self, chunk_idx: int, total_chunks: int, readme_text: str,
                      tests_section: str, profile_header: str, genesis: str = "",
                      coverage_strategy: str = "", api_surface: str = "") -> str:
        """Return the per-chunk extraction prompt (overridable by subclasses)."""
        # Injected collaborators/config bound locally (bodies verbatim).
        _README_CAP = self.cfg.readme_cap
        _STRATEGY_CAP = self.cfg.strategy_cap
        _TASK1_THINKING_DIRECTIVE = self.cfg.thinking_directive
        _format_api_surface_section = self.shaping.format_api_surface_section
        genesis_section = (
            f"\n--- genesis.md (methodology — apply when extracting requirements) ---\n"
            f"{genesis}\n"
        ) if genesis.strip() else ""
        strategy_section = (
            f"\n--- Coverage strategy (import patterns, type construction, test conventions) ---\n"
            f"{trim_to_line(coverage_strategy, _STRATEGY_CAP, "\n... (strategy truncated)")}\n"
        ) if coverage_strategy.strip() else ""
        api_section = _format_api_surface_section(api_surface)
        return f"""You are extracting functional requirements from a subset of test files for a software project.
{_TASK1_THINKING_DIRECTIVE}This is chunk {chunk_idx} of {total_chunks} -- there are more test files not shown here.
All chunks will be synthesized into one complete requirements document after all chunks are processed.
Focus only on what you can directly observe in the tests below -- do not invent behavior.
{profile_header}{genesis_section}{strategy_section}{api_section}
README.md:
{readme_text[:_README_CAP]}

Test files (chunk {chunk_idx}/{total_chunks}):
{tests_section}

From these tests extract every behavior, interface, data structure, error condition, and invariant
that a correct implementation must satisfy. Be exhaustive but grounded -- only extract what the
tests explicitly verify. Output a plain structured list only -- no code, no filler, just requirements.
Derive the language, types, and function signatures from what you read.
"""


@dataclass(frozen=True, slots=True)
class LLMTask1Synthesizer(StaticTask1Synthesizer):
    """
    Contract-first / TDD implementation.

    All three extraction points are upgraded to emit API contracts rather than
    behavioral descriptions:

      _single_prompt()      — small project path (one LLM call): extracts contracts
                              directly from the full test set.
      _chunk_prompt()       — chunked path per-chunk: extracts contracts per chunk.
      _synthesize_group()   — Stage-1 merge: deduplicates contracts, flags conflicts.
      _synthesis_prompt()   — Stage-2 final synthesis: produces functional_requirements.md
                              as a contract document (module paths, signatures, types,
                              invariants) rather than behavioral prose.

    The pipeline infrastructure (caching, chunking, retry, disk management)
    is inherited from StaticTask1Synthesizer unchanged.
    """

    def _single_prompt(self, tests_section: str, readme_text: str, profile_section: str,
                       existing_genesis: str, coverage_strategy: str = "",
                       api_surface: str = "", context_section: str = "") -> str:
        # genesis is always shown — empty placeholder on first iteration so the
        # LLM knows this is the document accumulating retrospective findings.
        # Injected collaborators/config bound locally (bodies verbatim).
        FILE_FORMAT_INSTRUCTION = self.cfg.file_format_instruction
        _README_CAP = self.cfg.readme_cap
        _STRATEGY_CAP = self.cfg.strategy_cap
        _TASK1_THINKING_DIRECTIVE = self.cfg.thinking_directive
        _format_api_surface_section = self.shaping.format_api_surface_section
        if existing_genesis.strip():
            genesis_section = (f"\n--- genesis.md (methodology — read-only here; do not output or modify) ---\n"
                               f"{existing_genesis}\n")
        else:
            genesis_section = ("\n--- genesis.md (methodology — empty for now; will be filled by retrospectives in later iterations) ---\n"
                               "(empty)\n")
        strategy_section = (
            f"\n--- Coverage strategy (import patterns, type construction, test conventions) ---\n"
            f"{trim_to_line(coverage_strategy, _STRATEGY_CAP, "\n... (strategy truncated)")}\n"
        ) if coverage_strategy.strip() else ""
        api_section = _format_api_surface_section(api_surface)
        return f"""You are performing contract extraction from test files as part of a
contract-first TDD reconstruction experiment.
{_TASK1_THINKING_DIRECTIVE}{FILE_FORMAT_INSTRUCTION}
Your output MUST be the file `functional_requirements.md` wrapped in the FILE markers above.
Do not output anything outside those markers.
{profile_section}{context_section}{strategy_section}{api_section}
README.md:
{readme_text[:_README_CAP]}
{genesis_section}
Test files:
{tests_section}

Your task: produce functional_requirements.md as a precise API contract document.
For every function, method, struct, enum, trait, or module that the tests call or assert:
  - Exact module path or import (e.g. `crate_name::function_name`, `crate_name::TypeName`)
  - Full function/method signature: name, argument names and types, return type
  - Struct/enum/trait definition: fields with types, variants, trait methods with signatures
  - Error types returned or matched
  - Invariants explicitly asserted (e.g. field equals X, output matches Y)
  - For functions returning strings: list every `assert!(output.contains("..."))` substring verbatim.
    A signature like `fn generate() -> String` is INCOMPLETE without the expected output content.
    Example of complete requirement:
      `Bash::generate(cmd_name, cmd) -> String`
      Output must contain: `cmd="<cmd_name>_<subcmd>"`, `opts="--flag -f"`, `cmds="sub1 sub2"`

Rules:
  - Only include what the tests literally call or assert — do not invent.
  - Use exact names, types, and paths as written in the tests.
  - Include concrete type parameters where generics appear in the tests.
  - Group by module/crate. Use concise structured notation, not prose.
  - Do NOT include `fn test_*` or `#[test]` functions — these are the tests themselves, not the code under test.
    Extract the APIs that the test functions CALL (e.g. `Bash::generate(...)`, not `fn test_bash_generator()`).
  - Do NOT include `make_mock_command` or other test helpers — extract the real production API being tested.
  - For every `assert!(result.contains("..."))` or `assert_eq!(result, "...")` in the tests,
    copy the expected string verbatim into the requirements as a behavioral constraint.
"""

    def _synthesis_prompt(self, readme_text: str, profile_section: str,
                          existing_genesis: str, all_for_synthesis: str,
                          coverage_strategy: str = "",
                          project_dir: Path | None = None,
                          api_surface: str = "") -> str:
        # Injected collaborators/config bound locally (bodies verbatim).
        FILE_FORMAT_INSTRUCTION = self.cfg.file_format_instruction
        SINGLE_CALL_CHAR_LIMIT = self.cfg.single_call_char_limit
        _README_CAP = self.cfg.readme_cap
        _STRATEGY_CAP = self.cfg.strategy_cap
        _format_api_surface_section = self.shaping.format_api_surface_section
        genesis_exists = bool(existing_genesis.strip())
        genesis_section = (f"\n--- genesis.md (read-only — do not output or modify it) ---\n"
                           f"{existing_genesis}\n") if genesis_exists else ""
        strategy_section = (
            f"\n--- Coverage strategy (import patterns, type construction, test conventions) ---\n"
            f"{trim_to_line(coverage_strategy, _STRATEGY_CAP, "\n... (strategy truncated)")}\n"
        ) if coverage_strategy.strip() else ""
        api_section = _format_api_surface_section(api_surface)
        _readme_cap_str = readme_text[:_README_CAP]
        _contract_synthesis_scaffold = f"""You are producing the final API contract document from contract extractions
across all test chunks. This is the last step of a contract-first TDD reconstruction.
{FILE_FORMAT_INSTRUCTION}
Your output MUST be the file `functional_requirements.md` wrapped in the FILE markers above.
Do not output anything outside those markers.

README.md:
{_readme_cap_str}

Contract extractions (merged from all test chunks):


Your task: produce functional_requirements.md as the complete, deduplicated API contract.
For every function, method, struct, enum, trait, or module extracted above:
  - Exact module path or import
  - Full signature: name, argument names and types, return type
  - Struct/enum/trait definitions with fields, variants, trait method signatures
  - Error types
  - Invariants explicitly asserted in the tests

Rules:
  - Deduplicate: same symbol + same signature = one entry.
  - Conflicts (same name, different signature): keep all variants, annotate "(conflict)".
  - Do not add, invent, or generalise beyond what is in the extractions above.
  - Group by module/crate. Use concise structured notation, not prose.
"""
        _fixed_contract_overhead = (
            len(_contract_synthesis_scaffold) + len(profile_section)
            + len(strategy_section) + len(api_section) + len(existing_genesis)
        )
        _synthesis_cap = max(10_000, SINGLE_CALL_CHAR_LIMIT - _fixed_contract_overhead)
        _capped_synthesis = smart_truncate(all_for_synthesis, total_limit=_synthesis_cap,
                                           head=_synthesis_cap // 3, tail=2 * _synthesis_cap // 3)
        return f"""You are producing the final API contract document from contract extractions
across all test chunks. This is the last step of a contract-first TDD reconstruction.
{FILE_FORMAT_INSTRUCTION}
Your output MUST be the file `functional_requirements.md` wrapped in the FILE markers above.
Do not output anything outside those markers.
{profile_section}{strategy_section}{api_section}
README.md:
{readme_text[:_README_CAP]}
{genesis_section}
Contract extractions (merged from all test chunks):
{_capped_synthesis}

Your task: produce functional_requirements.md as the complete, deduplicated API contract.
For every function, method, struct, enum, trait, or module extracted above:
  - Exact module path or import
  - Full signature: name, argument names and types, return type
  - Struct/enum/trait definitions with fields, variants, trait method signatures
  - Error types
  - Invariants explicitly asserted in the tests

Rules:
  - Deduplicate: same symbol + same signature = one entry.
  - Conflicts (same name, different signature): keep all variants, annotate "(conflict)".
  - Do not add, invent, or generalise beyond what is in the extractions above.
  - Group by module/crate. Use concise structured notation, not prose.
"""

    def _chunk_prompt(self, chunk_idx: int, total_chunks: int, readme_text: str,
                      tests_section: str, profile_header: str, genesis: str = "",
                      coverage_strategy: str = "", api_surface: str = "") -> str:
        # Injected collaborators/config bound locally (bodies verbatim).
        _README_CAP = self.cfg.readme_cap
        _STRATEGY_CAP = self.cfg.strategy_cap
        _TASK1_THINKING_DIRECTIVE = self.cfg.thinking_directive
        _format_api_surface_section = self.shaping.format_api_surface_section
        genesis_section = (
            f"\n--- genesis.md (methodology — apply when extracting contracts) ---\n"
            f"{genesis}\n"
        ) if genesis.strip() else ""
        strategy_section = (
            f"\n--- Coverage strategy (import patterns, type construction, test conventions) ---\n"
            f"{trim_to_line(coverage_strategy, _STRATEGY_CAP, "\n... (strategy truncated)")}\n"
        ) if coverage_strategy.strip() else ""
        api_section = _format_api_surface_section(api_surface)
        return f"""You are performing contract extraction from test files as the first step of
a contract-first TDD reconstruction experiment.
{_TASK1_THINKING_DIRECTIVE}This is chunk {chunk_idx} of {total_chunks}. All chunks will be combined into a single
implementation contract document.
{profile_header}{genesis_section}{strategy_section}{api_section}
README.md:
{readme_text[:_README_CAP]}

Test files (chunk {chunk_idx}/{total_chunks}):
{tests_section}

Your task: extract every concrete API element that these tests call or depend on.
For each item, record:
  - The exact module path or import (e.g. `argh::from_env`, `argh_derive::ArghParser`)
  - The full function/method signature: name, all argument names and types, return type
  - Any struct, enum, or trait definition used (fields, variants, trait methods)
  - Any error type returned or matched
  - Any invariant explicitly asserted (e.g. output equals X, field must be Y)

Rules:
  - Only extract what the tests literally call or assert — do not invent.
  - Use the exact names, types, and paths as written in the tests.
  - If a type is generic, include the concrete type parameter used in the test.
  - Output a plain structured list only. No prose, no examples, no filler.
"""

    def _synthesize_group(self, group_partials: list[str], group_idx: int, total_groups: int,
                          readme_text: str, profile_section: str, genesis: str = "",
                          coverage_strategy: str = "", api_surface: str = "") -> str | None:
        """
        Merge a group of contract partials into a deduplicated intermediate contract list.
        Overrides the behavioral merge prompt with a contract-aware one.
        """
        # Injected collaborators/config bound locally (bodies verbatim).
        SINGLE_CALL_CHAR_LIMIT = self.cfg.single_call_char_limit
        _README_CAP = self.cfg.readme_cap
        _TASK1_MODEL = self.cfg.task1_model
        _format_api_surface_section = self.shaping.format_api_surface_section
        ask = self.llm.ask
        genesis_section = (
            f"\n--- genesis.md (methodology — apply when merging contracts) ---\n"
            f"{genesis}\n"
        ) if genesis.strip() else ""
        api_section = _format_api_surface_section(api_surface)

        _readme_cap_str = readme_text[:_README_CAP]
        _contract_merge_scaffold = f"""You are merging contract extractions from multiple test file chunks.

This is group {group_idx} of {total_groups}. Each entry lists API contracts extracted
from a subset of test files. Your task: produce a single deduplicated contract list.

Rules:
  - Preserve exact module paths, function signatures, types, and invariants.
  - Merge duplicate entries (same symbol, same signature) into one.
  - Do not add, invent, or generalise — only what is present in the input.
  - If two entries conflict (same name, different signature), keep both and annotate with "(conflict)".

README.md (for language/framework context):
{_readme_cap_str}

Contract partials to merge:


Output a plain structured list of the merged contracts. No prose, no code blocks, no headers.
"""
        _fixed_overhead = (
            len(_contract_merge_scaffold) + len(profile_section)
            + len(genesis_section) + len(api_section)
        )
        _combined_cap = max(10_000, SINGLE_CALL_CHAR_LIMIT - _fixed_overhead)
        _combined_raw = "\n\n".join(group_partials)
        combined = smart_truncate(_combined_raw, total_limit=_combined_cap,
                                  head=_combined_cap // 3, tail=2 * _combined_cap // 3)

        prompt = f"""You are merging contract extractions from multiple test file chunks.

This is group {group_idx} of {total_groups}. Each entry lists API contracts extracted
from a subset of test files. Your task: produce a single deduplicated contract list.

Rules:
  - Preserve exact module paths, function signatures, types, and invariants.
  - Merge duplicate entries (same symbol, same signature) into one.
  - Do not add, invent, or generalise — only what is present in the input.
  - If two entries conflict (same name, different signature), keep both and annotate with "(conflict)".
{profile_section}{genesis_section}{api_section}
README.md (for language/framework context):
{readme_text[:_README_CAP]}

Contract partials to merge:
{combined}

Output a plain structured list of the merged contracts. No prose, no code blocks, no headers.
"""

        def attempt() -> str | Literal[False]:
            result = ask(prompt, model=_TASK1_MODEL)
            if not result or not result.strip():
                return False
            return result

        raw = with_retry(attempt)
        return str(raw) if raw is not False else None

