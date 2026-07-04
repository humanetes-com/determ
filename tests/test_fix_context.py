"""FixContextAssembler — the single derivation point for per-fn fix-prompt
sections, extracted from pipeline.py's `_gather_fn_fix_context` family
(god-file shrink slice 2026-07-04)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.fix_context import FixContextAssembler
from pipeline_core.domain.models import FnFixContext, TypeRecord
from pipeline_core.domain.prompt_budget import PromptBudgets


def _assembler(guard_log: list[tuple[str, str]] | None = None,
               notes: str = "", budgets: PromptBudgets | None = None,
               notes_calls: list[dict[str, Any]] | None = None) -> FixContextAssembler:
    def _notes(fr_path: Path, **kw: Any) -> str:
        if notes_calls is not None:
            notes_calls.append({"fr_path": fr_path, **kw})
        return notes

    sink = guard_log if guard_log is not None else []
    return FixContextAssembler(
        budgets=budgets or PromptBudgets.derive(120_000),
        filter_notes=_notes,
        guard=lambda loc, msg: sink.append((loc, msg)),
    )


_RECS = [TypeRecord(name="Error", kind="struct", definition="struct Error;")]


def test_types_section_selects_from_all_fix_time_sources() -> None:
    a = _assembler()
    out = a.types_section_for_fix("fn f()", "Error::new()", "", "", "", _RECS)
    assert "Error" in out
    # Nothing names a type → empty section.
    assert a.types_section_for_fix("fn f()", "1 + 1", "", "", "", _RECS) == ""


def test_types_section_cap_fires_guard() -> None:
    from dataclasses import replace
    guard_log: list[tuple[str, str]] = []
    tiny = replace(PromptBudgets.derive(120_000), types_section=10)
    a = _assembler(guard_log=guard_log, budgets=tiny)
    out = a.types_section_for_fix("fn f(e: Error)", "", "", "", "", _RECS)
    assert len(out) == 10
    assert guard_log and guard_log[0][0] == "phase-c/types_truncated"


def test_bound_methods_empty_cases() -> None:
    a = _assembler()
    assert a.bound_methods_section_for("", _RECS) == ""
    assert a.bound_methods_section_for("where E: Debug", []) == ""


def test_gather_assembles_vo_and_notes_gated_on_fr_path(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    a = _assembler(notes="NOTE", notes_calls=calls)
    src = "impl Wrap for Error\nwhere\n    E: Clone,\n{ fn f() {} }\n"
    kw: dict[str, Any] = {
        "file_text": src, "file_rel": "src/lib.rs", "fn_name": "f",
        "sig_text": "fn f()", "body_text": "", "error_excerpt": "",
        "container": "impl Wrap for Error", "type_records": _RECS,
    }
    no_fr = a.gather(**kw, fr_path=None)
    assert isinstance(no_fr, FnFixContext)
    assert no_fr.notes_section == "" and not calls
    assert no_fr.impl_where == "E: Clone"
    assert "Error" in no_fr.types_section

    with_fr = a.gather(**kw, fr_path=tmp_path / "fr.md")
    assert with_fr.notes_section == "NOTE"
    assert calls and calls[0]["fn_name"] == "f"
