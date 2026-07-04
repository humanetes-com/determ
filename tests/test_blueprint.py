"""P3 blueprint: parse, slice_for ranking, topic validation, ensure phase."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.tasks.blueprint_phase import (
    BlueprintDeps,
    ensure_blueprint,
)
from pipeline_core.domain.blueprint import (
    blueprint_path,
    missing_topics,
    parse_blueprint,
)

_DOC = """# design
The crate wraps errors in a single-word pointer.

## Overview
One heap slot, vtable at offset zero.

## error
struct ErrorImpl stores vtable then the payload; `vtable` fn reads offset 0.

## ptr
Own<T> is a NonNull wrapper; by_ref borrows it.

## ensure macro
Token-muncher rendering `lhs op rhs` operands via BothDebug/NotBothDebug.
"""


def test_parse_headings_and_preamble() -> None:
    bp = parse_blueprint(_DOC)
    assert [s.heading for s in bp.sections] == [
        "Overview", "Overview", "error", "ptr", "ensure macro"]
    # preamble prose kept as its own Overview section
    assert "single-word pointer" in bp.sections[0].body


def test_slice_for_ranks_by_identifier_overlap() -> None:
    bp = parse_blueprint(_DOC)
    out = bp.slice_for(frozenset({"Own", "by_ref"}), cap=4000)
    assert "## ptr" in out and "NonNull wrapper" in out
    assert "ensure macro" not in out          # zero overlap → excluded
    assert "single-word pointer" in out       # anchor always included


def test_slice_for_cap_and_empty() -> None:
    bp = parse_blueprint(_DOC)
    out = bp.slice_for(frozenset({"Own"}), cap=10)   # no room beyond anchor
    assert "NonNull" not in out
    assert parse_blueprint("").slice_for(frozenset({"x"}), cap=100) == ""


def test_missing_topics_by_heading_word() -> None:
    bp = parse_blueprint(_DOC)
    assert missing_topics(bp, frozenset({"error", "ptr", "context"})) == (
        "context",)


def test_blueprint_path_derivation() -> None:
    p = blueprint_path(Path("/x/functional_requirements_anyhow.md"))
    assert p == Path("/x/blueprint_anyhow.md")


def _deps(responses: list[str], log: list[str]) -> BlueprintDeps:
    seq = list(responses)
    return BlueprintDeps(
        ask=lambda prompt, model=None: seq.pop(0) if len(seq) > 1 else seq[0],
        log=log.append, emit=lambda *_a, **_k: None, model="m")


def test_ensure_blueprint_writes_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "blueprint_x.md"
    logs: list[str] = []
    bp = ensure_blueprint(
        path, module="x", skeleton_sigs="fn a();", tests_block="#[test]",
        functional_req="FR", topics=("error", "ptr"),
        deps=_deps([_DOC], logs))
    assert not bp.empty and path.exists()
    # second call keeps the file without asking
    bp2 = ensure_blueprint(
        path, module="x", skeleton_sigs="", tests_block="",
        functional_req="", topics=("error",),
        deps=_deps(["MUST NOT BE ASKED"], logs))
    assert "keeping" in logs[-1] and not bp2.empty


def test_ensure_blueprint_retries_with_missing_topics(tmp_path: Path) -> None:
    path = tmp_path / "blueprint_x.md"
    logs: list[str] = []
    incomplete = "## Overview\nplan\n## error\nstuff\n"
    bp = ensure_blueprint(
        path, module="x", skeleton_sigs="", tests_block="",
        functional_req="", topics=("error", "ptr"),
        deps=_deps([incomplete, _DOC], logs))
    assert not missing_topics(bp, frozenset({"ptr"}))
    assert any("1 missing topic(s) — ptr" in m for m in logs)


def test_ensure_blueprint_keeps_partial_after_retries(tmp_path: Path) -> None:
    path = tmp_path / "blueprint_x.md"
    incomplete = "## Overview\nplan\n"
    bp = ensure_blueprint(
        path, module="x", skeleton_sigs="", tests_block="",
        functional_req="", topics=("error",),
        deps=_deps([incomplete], []))
    assert not bp.empty and path.read_text() == incomplete


def test_build_blueprint_prompt_lists_topics_and_gaps() -> None:
    from pipeline_core.prompts.blueprint import build_blueprint_prompt
    p = build_blueprint_prompt(
        module="m", skeleton_sigs="fn a();", tests_block="#[test] fn t(){}",
        functional_req="FR text", topics=("error", "ptr"), missing=("ptr",))
    assert "  - error" in p and "  - ptr" in p
    assert "MISSED these required" in p and "fn a();" in p and "FR text" in p


def test_missing_family_topic_is_reasked() -> None:
    """A dispatch-family topic (method name) missing from the headings is a
    validation gap like any file topic."""
    bp = parse_blueprint("## Overview\np\n## error\ns\n")
    assert missing_topics(bp, frozenset({"error", "__dispatch_ensure"})) == (
        "__dispatch_ensure",)
    covered = parse_blueprint(
        "## Overview\np\n## error\ns\n## __dispatch_ensure family\nscheme\n")
    assert missing_topics(covered, frozenset({"__dispatch_ensure"})) == ()


def test_ensure_blueprint_adds_family_topics(tmp_path: Path) -> None:
    from dataclasses import replace as _replace
    from types import SimpleNamespace
    path = tmp_path / "blueprint_x.md"
    logs: list[str] = []
    full = (_DOC + "\n## __dispatch_ensure family\ntoken-muncher scheme\n")
    deps = _replace(
        _deps([_DOC, full], logs),
        dispatch_families=lambda _d: [SimpleNamespace(method="__dispatch_ensure")])
    bp = ensure_blueprint(
        path, module="x", skeleton_sigs="", tests_block="",
        functional_req="", topics=("error", "ptr"), deps=deps,
        skeleton_dir=tmp_path)
    assert not missing_topics(bp, frozenset({"__dispatch_ensure"}))
    assert any("__dispatch_ensure" in m for m in logs)   # gap named on retry
