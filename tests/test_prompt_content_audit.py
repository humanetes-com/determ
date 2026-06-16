"""Per-task prompt-content audit (CODE_ARCHITECTURE.md line 887; docs/PROMPT_CONTENT_AUDIT.md).

Pins, per task, that the spec docs the matrix claims reach a prompt ACTUALLY reach it —
using unique sentinels through the real prompt builders, so a dropped injection fails
here instead of silently shrinking the model's context. The RepoSpec→per-fn channel is
already pinned by test_repo_spec.py; this file covers the direct-interpolation channels
(TASK 1) and the per-impl RepoSpec channel, plus the deliberate non-injections.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pipeline_core.application.generators.task1_synthesizer import StaticTask1Synthesizer
from pipeline_core.domain.generation import Task1Config

# --- sentinels: each doc gets a string that can appear nowhere else ----------
_CS = "SENTINEL_COVERAGE_STRATEGY_xyz"
_GEN = "SENTINEL_GENESIS_xyz"
_API = "SENTINEL_API_SURFACE_xyz"
_CTX = "SENTINEL_PROJECT_CONTEXT_xyz"
_PROFILE = "SENTINEL_PROFILE_xyz"


@dataclass
class _Task1Fake:
    """Satisfies the PromptShaping methods _single_prompt touches; format_api_surface_section
    ECHOES its input so we can prove api_surface is threaded into the prompt."""
    def format_api_surface_section(self, api_surface: str) -> str:
        return f"\n--- API ---\n{api_surface}\n" if api_surface.strip() else ""

    def format_test_files(self, test_files: object) -> str:
        return ""


def _task1_cfg() -> Task1Config:
    return Task1Config(
        model="m", task1_model=None, thinking_directive="", file_format_instruction="<<FMT>>",
        single_call_char_limit=100_000, synthesis_group_size=20, readme_cap=2_000,
        strategy_cap=2_000, chunk_cache_dir=".chunk_cache",
    )


def _task1_synth() -> StaticTask1Synthesizer:
    fake = _Task1Fake()
    return StaticTask1Synthesizer(llm=fake, shaping=fake, progress=fake,  # type: ignore[arg-type]
                                  requirements=fake, cfg=_task1_cfg())


def test_task1_prompt_carries_cs_genesis_api_context() -> None:
    """TASK 1 prompt directly interpolates coverage_strategy, genesis, api_surface,
    project_context, project_profile (docs/PROMPT_CONTENT_AUDIT.md row 1)."""
    synth = _task1_synth()
    prompt = synth._single_prompt(
        tests_section="// tests", readme_text="readme",
        profile_section=f"\n--- profile ---\n{_PROFILE}\n",
        existing_genesis=_GEN, coverage_strategy=_CS, api_surface=_API,
        context_section=f"\n--- context ---\n{_CTX}\n",
    )
    for sentinel in (_CS, _GEN, _API, _CTX, _PROFILE):
        assert sentinel in prompt, f"TASK 1 prompt dropped {sentinel!r}"


def test_task1_prompt_omits_empty_optional_docs() -> None:
    """Empty coverage_strategy / api_surface produce no section (their headers must
    not appear) — so a blank doc can't masquerade as content."""
    synth = _task1_synth()
    prompt = synth._single_prompt(
        tests_section="", readme_text="r", profile_section="", existing_genesis="",
        coverage_strategy="   ", api_surface="", context_section="",
    )
    assert "Coverage strategy" not in prompt
    assert "--- API ---" not in prompt
    # genesis is ALWAYS shown (placeholder when empty) — the audit's one always-on doc.
    assert "genesis.md (methodology" in prompt


def test_per_impl_prompt_carries_repo_spec_bundle(tmp_path: Path) -> None:
    """TASK 2 per-impl fill + TASK 3 inline-retry route FR/genesis/API through the
    RepoSpec bundle (complements test_repo_spec.py which pins the per-fn builder)."""
    import pipeline  # noqa: F401 — binds the real prompts runtime facade
    from pipeline_core.domain.models import FnRecord, ImplBlock
    from pipeline_core.prompts.context import PerImplSections
    from pipeline_core.prompts.per_impl import build_per_impl_prompt

    (tmp_path / "genesis.md").write_text("Gen rule: validate inputs.")
    fr = tmp_path / "functional_requirements_mycrate.md"
    # FR text must share identifiers with the block (fmt / Display / X) so the
    # relevance selector in render_repo_spec keeps the paragraph.
    fr.write_text("FR-077: fmt writes the Display representation of X.\n")
    stub = FnRecord(file="src/lib.rs", fn_name="fmt", container="impl Display for X",
                    sig_text="fn fmt(&self)", has_unimplemented_body=True)
    block = ImplBlock(file="src/lib.rs", container="impl Display for X",
                      members=(stub,), stub_members=(stub,))
    empty = {"types": "", "invariants": "", "notes": "", "crate_idioms": "",
             "dispatch_family": "", "macros": "", "siblings": "", "retrospective_guidance": ""}
    prompt = build_per_impl_prompt(block, [], [], "mycrate", "stable",
                                   PerImplSections(**empty), fr_path=fr)
    assert "<REPOSITORY_SPECIFICATIONS>" in prompt
    assert "FR-077" in prompt, "per-impl prompt dropped the relevance-matched FR paragraph"
    # Non-injection: no fr_path → no RepoSpec bundle (the inert facade default 0c relies on).
    bare = build_per_impl_prompt(block, [], [], "mycrate", "stable",
                                 PerImplSections(**empty), fr_path=None)
    assert "<REPOSITORY_SPECIFICATIONS>" not in bare
