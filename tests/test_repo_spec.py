"""Repository Specifications (2026-06-11): FR + genesis + API injected into
every code-producing prompt from t2 on, via the prompts runtime facade.

Pins: the pure renderer (relevance selection, budget, empties), the pipeline
provider (fr_path → docs, fail-safe), and the builder integration (the
REPOSITORY_SPECIFICATIONS block reaches per-fn / per-impl / fix prompts)."""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pipeline_core.prompts.repo_spec import (  # noqa: E402
    RepoSpecDocs,
    identifiers_for_target,
    render_repo_spec,
)


def _docs() -> RepoSpecDocs:
    return RepoSpecDocs(
        genesis_rules="- always run cargo check before tests",
        genesis="Methodology: derive behaviour from tests only.",
        functional_req=(
            "# FR\n\nFR-001: The Chain iterator yields sources in order.\n\n"
            "FR-002: Display for Error prints the message only.\n\n"
            "FR-003: unrelated paragraph about macros.\n"
        ),
        api_surface=(
            '<FILE path="src/chain.rs">pub struct Chain { /* ... */ }\n'
            "pub fn next_source() { /* ... */ }\n</FILE>\n"
            '<FILE path="src/error.rs">pub struct Error;\n</FILE>\n'
        ),
    )


def test_render_repo_spec_relevance_and_parts() -> None:
    idents = identifiers_for_target(
        fn_name="next", container="impl Iterator for Chain", file_rel="src/chain.rs")
    assert "Chain" in idents and "Iterator" in idents and "chain" in idents
    assert "impl" not in idents and "for" not in idents
    out = render_repo_spec(_docs(), file_rel="src/chain.rs",
                           identifiers=idents, char_budget=6000)
    assert "always run cargo check" in out            # rules always included
    assert "FR-001" in out                            # mentions Chain → kept
    assert "FR-002" not in out                        # Display/Error → dropped
    assert "Methodology" in out                       # genesis prose
    # API slice: other files only — own file excluded even though it matches.
    assert '<FILE path="src/chain.rs">' not in out
    # error.rs has no identifier overlap → excluded too.
    assert '<FILE path="src/error.rs">' not in out


def test_render_repo_spec_api_cross_file_and_budget() -> None:
    idents = {"Chain"}
    out = render_repo_spec(_docs(), file_rel="src/error.rs",
                           identifiers=idents, char_budget=6000)
    assert '<FILE path="src/chain.rs">' in out        # cross-file ground truth
    assert render_repo_spec(RepoSpecDocs(), identifiers=idents) == ""
    tight = render_repo_spec(_docs(), identifiers=idents, char_budget=120)
    assert len(tight) <= 240                          # trim_to_line + headers
    assert render_repo_spec(_docs(), identifiers=idents, char_budget=0) == ""


def test_pipeline_provider_loads_and_fails_safe(tmp_path: Path) -> None:
    import pipeline

    (tmp_path / "genesis.md").write_text("## Rules\nGen rule text.")
    (tmp_path / "functional_requirements_mycrate.md").write_text(
        "FR-010: Chain ordering.\n\n## Model Notes — Confusions to Avoid\nnote body\n")
    out = pipeline._repo_spec_section(
        tmp_path / "functional_requirements_mycrate.md",
        file_rel="src/chain.rs", fn_name="next", container="impl Chain")
    assert "FR-010" in out
    assert "note body" not in out                     # Model Notes stripped (own channel)
    # Fail-safe: no fr_path / wrong name / empty project → "".
    assert pipeline._repo_spec_section(None) == ""
    assert pipeline._repo_spec_section(tmp_path / "other.md") == ""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert pipeline._repo_spec_section(
        empty / "functional_requirements_ghost.md", fn_name="next") == ""


def test_builders_carry_repository_specifications(tmp_path: Path) -> None:
    import pipeline  # noqa: F401  — configures the prompts runtime facade
    from pipeline_core.prompts import runtime
    from pipeline_core.prompts.context import PerFnFixSections, PerFnSections
    from pipeline_core.prompts.per_fn import (
        build_per_fn_fix_prompt,
        build_per_fn_prompt,
    )

    (tmp_path / "genesis.md").write_text("Gen rule: check twice.")
    fr = tmp_path / "functional_requirements_mycrate.md"
    fr.write_text("FR-020: parse handles empty input.\n")
    common = {"file_text": "fn parse() { unimplemented!() }", "fn_name": "parse",
              "sig_text": "fn parse()", "test_blocks": [], "module_name": "mycrate",
              "cluster_name": "stable", "file_rel": "src/lib.rs", "fr_path": fr}
    empty = {"macros": "", "notes": "", "container": "", "types": "",
             "invariants": "", "siblings": "", "crate_idioms": ""}
    initial = build_per_fn_prompt(**common, sections=PerFnSections(**empty))
    fix = build_per_fn_fix_prompt(**common, prev_body="1", error_excerpt="E",
                                  sections=PerFnFixSections(**empty, retrospective_guidance=""))
    for prompt in (initial, fix):
        assert "<REPOSITORY_SPECIFICATIONS>" in prompt
        assert "FR-020" in prompt
    # No fr_path → section absent (inert default preserved for old pins).
    assert "<REPOSITORY_SPECIFICATIONS>" not in build_per_fn_prompt(
        **{**common, "fr_path": None}, sections=PerFnSections(**empty))
    # Facade restore safety: configure is idempotent with pipeline's provider.
    runtime.configure(
        annotate_file_view=pipeline._annotate_file_view,
        promoted_error_rules=pipeline._promoted_error_rules,
        repo_spec_section=pipeline._repo_spec_section,
    )
