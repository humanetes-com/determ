"""TASK 0a — project profile + behavioral context docs (LLM doc-generation use cases).

Extracted from pipeline.py (slice 5b, Option 1). The incidental fence-stripping regex in the
fallback path is replaced by the relocated `strip_markdown_fences` helper (no regex enters the
clean layer). Infra (log/retry) and the pure text/extraction helpers are imported; the
pipeline-coupled collaborators (phase tracking, ask, write, prompt budgeting, caps, model ids)
are injected via :class:`Task0DocsDeps`. No import of pipeline.py.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...domain.llm_output import extract_files
from ...domain.text import strip_markdown_fences, trim_to_line
from ...log import log
from ...retry import with_retry


@dataclass(frozen=True, slots=True)
class Task0DocsDeps:
    """Collaborators the task0 doc tasks need, injected by the composition root."""

    phase_valid: Callable[[Path, str], bool]
    mark_phase: Callable[..., Any]
    ask: Callable[..., str]
    write: Callable[[Any, str], None]
    prompt_budget: Callable[..., int]
    task0_model: str
    model: str                 # global MODEL (used in a warning message)
    readme_cap: int
    profile_cap: int
    project_context_name: str  # filename invalidated when the profile regenerates


def task0_project_profile(profile_path: Path, test_files: "Any", readme_text: str, deps: Task0DocsDeps) -> str:
    """
    Run once at startup. Identifies the project's language, build system, and
    workspace/module structure from README + test file paths + content excerpts.
    Produces project_profile.md. Skip is governed by pipeline_state.md — once
    "task0a" is marked done there, this is treated as finalized; tests changing
    later does not invalidate it. Returns the profile content.
    """
    project_dir = profile_path.parent
    if deps.phase_valid(project_dir, "task0a"):
        content = profile_path.read_text().strip() if profile_path.exists() else ""
        log(f"  task0a already done — loading {profile_path.name}.")
        return content
    if profile_path.exists():
        content = profile_path.read_text().strip()
        if content:
            # Artifact predates pipeline_state.md tracking — backfill and skip.
            deps.mark_phase(project_dir, "task0a", "done",
                            detail=f"{len(content)} chars (existing artifact)")
            log(f"  task0a already done — loading {profile_path.name}.")
            return content

    log("\nTASK 0a: Identifying project structure (language, build system, workspace layout)")

    readme_section = f"README.md:\n{readme_text[:deps.readme_cap]}\n\n" if readme_text.strip() else ""
    _PROFILE_STATIC = 800  # static instruction text estimate
    _sample_files = test_files[:25]
    _per_file_budget = deps.prompt_budget(readme_section, " " * _PROFILE_STATIC) // max(1, len(_sample_files))
    _per_file_budget = max(200, _per_file_budget)

    test_entries = "\n\n".join(
        f"  {rel}\n  (excerpt):\n  {trim_to_line(content, _per_file_budget).replace(chr(10), chr(10) + '  ')}"
        for rel, content in _sample_files
    )

    prompt = f"""You are identifying the structure of a software project from its test files and README.

{readme_section}Test files (path + content excerpt):
{test_entries}

Produce a concise project_profile.md that answers the following:
1. Primary language and version (if specified or detectable from test imports).
2. Build system and test runner (e.g. "Cargo + cargo-nextest", "npm + jest").
3. Project layout: is it a monorepo / workspace? List each top-level module/crate/package
   with its directory path and type (library, binary, proc-macro, or both).
4. Minimum required files for each module so the build system accepts it
   (e.g. for Rust: Cargo.toml with [lib] section + src/lib.rs).
5. Structural conventions visible from the test files (e.g. tests in <crate>/tests/).

Infer crate types from test content: `extern crate proc_macro` or `#[proc_macro_derive]`
patterns indicate a proc-macro crate. `fn main()` or binary test patterns indicate a bin.

Be specific and concrete. This profile is used to generate source files — accuracy matters.
Do not mention the experiment or iteration process. Output only the profile document.
Keep the output under 2,000 characters — be concise.

<FILE path="project_profile.md">
content here
</FILE>
"""

    def attempt() -> "str | bool":
        response = deps.ask(prompt, model=deps.task0_model)
        files = extract_files(response)
        if "project_profile.md" not in files:
            # Fallback when the model ignored <FILE> markers: strip any code fences.
            stripped = strip_markdown_fences(response)
            if len(stripped) > 100:
                files = {"project_profile.md": stripped}
            else:
                log(f"  WARNING: {deps.model} did not return project_profile.md.")
                log(f"  Raw response snippet: {response[:500]}")
                return False
        content = files["project_profile.md"].strip()
        deps.write(profile_path, content)
        # Invalidate the context document whenever the profile is regenerated so
        # task0_project_context re-synthesizes against the updated inputs.
        (profile_path.parent / deps.project_context_name).unlink(missing_ok=True)
        deps.mark_phase(project_dir, "task0a-context", "pending", detail="profile regenerated")
        log(f"  project_profile.md written ({len(content):,} chars).")
        return content

    result = with_retry(attempt)
    if result is False:
        log("  WARNING: could not produce project_profile.md after all retries. Continuing without it.")
        return ""
    deps.mark_phase(project_dir, "task0a", "done", detail=f"{len(str(result))} chars")
    return str(result)


def task0_project_context(context_path: Path, test_files: "Any", readme_text: str,
                          profile_content: str, deps: Task0DocsDeps) -> str:
    """
    Synthesize a behavioral summary of the project from test content + README + profile.
    Produces project_context.md — the portable big-picture document injected into every
    task, especially TASK 0c which receives no other project-level context.

    Skip is governed by pipeline_state.md (phase key "task0a-context"). Returns the
    summary text. If the upstream profile is regenerated, task0a clears this phase
    so it re-runs against fresh inputs.
    """
    project_dir = context_path.parent
    if deps.phase_valid(project_dir, "task0a-context"):
        content = context_path.read_text().strip() if context_path.exists() else ""
        log(f"  task0a-context already done — loading {context_path.name}.")
        return content
    if context_path.exists():
        content = context_path.read_text().strip()
        if content:
            # Artifact predates pipeline_state.md tracking — backfill and skip.
            deps.mark_phase(project_dir, "task0a-context", "done",
                            detail=f"{len(content)} chars (existing artifact)")
            log(f"  task0a-context already done — loading {context_path.name}.")
            return content

    log("\nTASK 0a (context): Synthesizing behavioral project summary...")

    readme_section = f"README.md:\n{readme_text[:deps.readme_cap]}\n\n" if readme_text.strip() else ""
    profile_section = f"Project profile:\n{profile_content[:deps.profile_cap]}\n\n" if profile_content.strip() else ""
    _CONTEXT_STATIC = 600
    _sample_files = test_files[:25]
    _per_file_budget = deps.prompt_budget(readme_section, profile_section, " " * _CONTEXT_STATIC) // max(1, len(_sample_files))
    _per_file_budget = max(200, _per_file_budget)

    test_excerpts = "\n\n".join(
        f"--- {rel} ---\n{trim_to_line(content, _per_file_budget)}"
        for rel, content in _sample_files
    )

    prompt = f"""You are writing a concise behavioral summary of a software project.
This summary will be used by AI agents that must write code for this project without
access to the original implementation — they need to understand what the project does
and what each module's role is.

{readme_section}{profile_section}Test file excerpts:
{test_excerpts}

Write a project_context.md that answers:
1. What does this project do? (2-3 sentences, concrete and specific)
2. What is each module/crate/package responsible for? (one line per module)
3. What are the key public abstractions — the types, traits, or interfaces that most
   code in the project depends on? (names + one-line description each)
4. What conventions does this project follow for error handling, data formats, or
   important invariants? (inferred from test assertions and naming patterns)

Write in plain prose. Be specific — use actual type names, function names, and module
names observed in the test files. Do not be vague. Do not describe the pipeline process.
Keep the output under 2,000 characters — be concise.

<FILE path="project_context.md">
content here
</FILE>
"""

    def attempt() -> "str | bool":
        response = deps.ask(prompt, model=deps.task0_model)
        files = extract_files(response)
        if "project_context.md" not in files:
            stripped = strip_markdown_fences(response)
            if len(stripped) > 100:
                files = {"project_context.md": stripped}
            else:
                log("  WARNING: could not synthesize project_context.md.")
                return False
        content = files["project_context.md"].strip()
        deps.write(context_path, content)
        log(f"  project_context.md written ({len(content):,} chars).")
        return content

    result = with_retry(attempt)
    if result is False:
        log("  WARNING: could not produce project_context.md — using README/profile fallback.")
        fallback = readme_text or profile_content or ""
        return fallback
    deps.mark_phase(project_dir, "task0a-context", "done", detail=f"{len(str(result))} chars")
    return str(result)
