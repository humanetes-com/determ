"""Per-task prompt-content audit (docs/PROMPT_CONTENT_AUDIT.md).

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
             "dispatch_family": "", "macros": "", "siblings": "", "retrospective_guidance": "",
             "bound_methods": ""}
    prompt = build_per_impl_prompt(block, [], [], "mycrate", "stable",
                                   PerImplSections(**empty), fr_path=fr)
    assert "<REPOSITORY_SPECIFICATIONS>" in prompt
    assert "FR-077" in prompt, "per-impl prompt dropped the relevance-matched FR paragraph"
    # Non-injection: no fr_path → no RepoSpec bundle (the inert facade default 0c relies on).
    bare = build_per_impl_prompt(block, [], [], "mycrate", "stable",
                                 PerImplSections(**empty), fr_path=None)
    assert "<REPOSITORY_SPECIFICATIONS>" not in bare


def test_per_impl_prompt_carries_the_previous_attempts_errors() -> None:
    """The fix loop's retry must SEE what the last attempt broke. Until this
    section existed the error text reached only TYPES_IN_SCOPE re-selection,
    so an error naming no new types rebuilt attempt 1's prompt byte-for-byte
    and attempt 2 was a temperature resample — both E0277 units of run
    `275772-425da59c` (2026-08-19) sent the same prompt sha on both attempts
    and burned their whole fix loop on one unchanged question."""
    import pipeline  # noqa: F401 — binds the real prompts runtime facade
    from pipeline_core.domain.models import FnRecord, ImplBlock
    from pipeline_core.prompts.context import PerImplSections
    from pipeline_core.prompts.per_impl import build_per_impl_prompt

    stub = FnRecord(file="src/lib.rs", fn_name="fmt", container="impl Display for X",
                    sig_text="fn fmt(&self)", has_unimplemented_body=True)
    block = ImplBlock(file="src/lib.rs", container="impl Display for X",
                      members=(stub,), stub_members=(stub,))
    empty = {"types": "", "invariants": "", "notes": "", "crate_idioms": "",
             "dispatch_family": "", "macros": "", "siblings": "",
             "retrospective_guidance": "", "bound_methods": ""}
    _err = "SENTINEL_LAST_ERROR_E0277_xyz"
    with_err = build_per_impl_prompt(
        block, [], [], "mycrate", "stable",
        PerImplSections(**empty, last_error=_err), fr_path=None)
    assert "<LAST_ERROR>" in with_err and _err in with_err
    # attempt 1 has no previous error: the section must not appear, so the
    # attempt-2 prompt (which carries it) can never equal attempt 1's.
    without = build_per_impl_prompt(block, [], [], "mycrate", "stable",
                                    PerImplSections(**empty), fr_path=None)
    assert "<LAST_ERROR>" not in without
    assert with_err != without


def test_per_impl_prompt_states_refill_evidence_and_does_not_ask_for_cfg_dead_stubs() -> None:
    """Two claims the per-impl prompt used to make without support, both from
    run `902902-daee577d` (2026-08-20): that a re-fill member "FAILS" (when
    the frontier named it by inference and the tests were not in the prompt),
    and that a cfg-dead `provide` was to be filled "in this response" (the
    splice then refused the body)."""
    from pipeline_core.domain.models import FnRecord, ImplBlock
    from pipeline_core.prompts.context import PerImplSections
    from pipeline_core.prompts.per_impl import build_per_impl_prompt

    container = "impl<C, E> StdError for ContextError<C, E>"
    source = FnRecord(file="src/context.rs", fn_name="source", container=container,
                      sig_text="fn source(&self) -> Option<&(dyn StdError + 'static)>",
                      body_text="Some(&self.error)", has_unimplemented_body=False)
    provide = FnRecord(file="src/context.rs", fn_name="provide", container=container,
                       sig_text="fn provide<'a>(&'a self, request: &mut Request<'a>)",
                       has_unimplemented_body=True)
    block = ImplBlock(file="src/context.rs", container=container,
                      members=(source, provide), stub_members=(), refill_members=(source,))
    sibling_container = "impl<C> StdError for ContextError<C, Error>"
    sib = FnRecord(file="src/context.rs", fn_name="source", container=sibling_container,
                   sig_text="fn source(&self) -> Option<&(dyn StdError + 'static)>",
                   body_text="None", has_unimplemented_body=False)
    sibling = ImplBlock(file="src/context.rs", container=sibling_container,
                        members=(sib,), refill_members=(sib,))
    empty = {"types": "", "invariants": "", "notes": "", "crate_idioms": "",
             "dispatch_family": "", "macros": "", "siblings": "", "retrospective_guidance": "",
             "bound_methods": ""}
    prompt = build_per_impl_prompt(
        block, [sibling], [], "anyhow", "stable",
        PerImplSections(**empty,
                        refill_evidence="`source` — INFERRED: nothing singles out THIS body.\n"
                                        "  test_len said:\n    left: 4\n    right: 1",
                        inferred_refill_names=frozenset({"source"})),
        fr_path=None)
    assert "[REFILL?]" in prompt and "return it unchanged" in prompt
    assert "its test below FAILS" not in prompt
    assert "<REFILL_TARGETS>" in prompt and "test_len said:" in prompt and "left: 4" in prompt
    prov = next(ln for ln in prompt.splitlines() if "fn provide" in ln and "[STUB]" in ln)
    assert "NOT in this response" in prov and "fill in this response" not in prov
    # The same-named candidate in the related block is shown as such.
    assert "same-named re-fill candidate" in prompt
    # Legacy: a DIRECT re-fill keeps the certainty tag.
    legacy = build_per_impl_prompt(block, [], [], "anyhow", "stable",
                                   PerImplSections(**empty), fr_path=None)
    assert "[REFILL] compiles but its test below FAILS" in legacy


def test_per_impl_sibling_reference_names_the_contradiction_with_last_error() -> None:
    """When the sibling body carries the expression LAST_ERROR rejected, the
    SIBLING_REFERENCE lead-in must say so instead of "follow the same
    structural pattern" — on attempt 2 of `ContextError<C, Error>::source`
    (run `902902-daee577d`, 2026-08-20, prompt `11899c17478d`) the two
    sections contradicted each other and the sibling won, E0277 twice."""
    import pipeline  # noqa: F401 — binds the real prompts runtime facade
    from pipeline_core.domain.models import FnRecord, ImplBlock
    from pipeline_core.prompts.context import PerImplSections
    from pipeline_core.prompts.per_impl import build_per_impl_prompt

    stub = FnRecord(file="src/context.rs", fn_name="source",
                    container="impl<C> StdError for ContextError<C, Error>",
                    sig_text="fn source(&self)", has_unimplemented_body=True)
    block = ImplBlock(file="src/context.rs",
                      container="impl<C> StdError for ContextError<C, Error>",
                      members=(stub,), stub_members=(stub,))
    sib = ("fn source(&self) -> Option<&(dyn StdError + 'static)> {\n"
           "Some(&self.error)\n}")
    base = {"types": "", "invariants": "", "notes": "", "crate_idioms": "",
            "dispatch_family": "", "macros": "", "siblings": sib,
            "retrospective_guidance": "", "bound_methods": "",
            "last_error": "error[E0277]: ...\n148 |         Some(&self.error)\n"}
    flipped = build_per_impl_prompt(
        block, [], [], "mycrate", "stable",
        PerImplSections(**base, sibling_rejected=("Some(&self.error)",)),
        fr_path=None)
    section = flipped.split("<SIBLING_REFERENCE>", 1)[1].split(
        "</SIBLING_REFERENCE>", 1)[0]
    assert "CAUTION" in section and "`Some(&self.error)`" in section
    assert "Do NOT follow" in section
    assert "follow the same structural pattern" not in section
    assert "Some(&self.error)\n}" in section, "the sibling body is still shown"
    # Negative control: no rejected expression → the original endorsement.
    plain = build_per_impl_prompt(block, [], [], "mycrate", "stable",
                                  PerImplSections(**base), fr_path=None)
    assert "follow the same structural pattern" in plain
    assert "CAUTION" not in plain


def test_per_impl_and_per_fn_prompts_carry_identity_must_as_instruction() -> None:
    """Identity Then sits after the stub list / signature, not in a
    USAGE_EXAMPLES tag. Empty keeps the prompt byte-identical at that slot."""
    from pipeline_core.domain.models import FnRecord, ImplBlock
    from pipeline_core.prompts.context import PerFnSections, PerImplSections
    from pipeline_core.prompts.per_fn import build_per_fn_prompt
    from pipeline_core.prompts.per_impl import build_per_impl_prompt

    must = (
        "MUST: `is::<&str>()` / `downcast::<&str>()` on that value is true "
        "— the TypeId a user downcasts to is the payload's type, never a "
        "wrapper's"
    )
    stub = FnRecord(file="src/error.rs", fn_name="msg", container="impl Error",
                    sig_text="fn msg<M>(message: M) -> Self", has_unimplemented_body=True)
    block = ImplBlock(file="src/error.rs", container="impl Error",
                      members=(stub,), stub_members=(stub,))
    empty = {"types": "", "invariants": "", "notes": "", "crate_idioms": "",
             "dispatch_family": "", "macros": "", "siblings": "",
             "retrospective_guidance": "", "bound_methods": ""}
    with_must = build_per_impl_prompt(
        block, [], [], "anyhow", "stable",
        PerImplSections(**empty, behaviour_must=must), fr_path=None)
    without = build_per_impl_prompt(
        block, [], [], "anyhow", "stable",
        PerImplSections(**empty), fr_path=None)
    assert "Stubs to fill in this response: msg" in with_must
    stub_at = with_must.index("Stubs to fill in this response: msg")
    gotchas_at = with_must.index("<RUST_GOTCHAS>")
    assert must in with_must[stub_at:gotchas_at]
    assert "<USAGE_EXAMPLES>" not in with_must
    assert must not in without
    assert without.count("<RUST_GOTCHAS>") == with_must.count("<RUST_GOTCHAS>")

    fn_empty = {"macros": "", "notes": "", "container": "impl Error",
                "types": "", "invariants": "", "siblings": "",
                "crate_idioms": "", "bound_methods": ""}
    fn_common = {
        "file_text": "fn msg() { unimplemented!() }", "fn_name": "msg",
        "sig_text": "fn msg<M>(message: M) -> Self", "test_blocks": [],
        "module_name": "anyhow", "cluster_name": "stable",
        "file_rel": "src/error.rs", "fr_path": None}
    fn_with = build_per_fn_prompt(
        **fn_common, sections=PerFnSections(**fn_empty, behaviour_must=must))
    fn_without = build_per_fn_prompt(
        **fn_common, sections=PerFnSections(**fn_empty))
    sig_at = fn_with.index("fn msg<M>(message: M) -> Self")
    fn_gotchas = fn_with.index("<RUST_GOTCHAS>")
    assert must in fn_with[sig_at:fn_gotchas]
    assert must not in fn_without
