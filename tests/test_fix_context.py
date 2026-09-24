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


def test_a_cap_inside_a_complete_impl_list_omits_the_list() -> None:
    """A first record longer than the cap used to be sliced with the
    COMPLETE sentence left above a partial list. The cut stops before that
    sentence and says the list was omitted."""
    from pipeline_core.prompts.sections import TRAIT_IMPLS_HEADER, join_type_blocks
    block = (
        "struct Box\n  fields (access with .name):\n    x: u8\n"
        + TRAIT_IMPLS_HEADER
        + "\n    impl Show for Box\n    impl Send for Box")
    # Inside the header, with room for the omission note and the type line.
    cap = block.index(TRAIT_IMPLS_HEADER) + 160
    out = join_type_blocks([block], cap)
    assert TRAIT_IMPLS_HEADER not in out
    assert "impl Show" not in out
    assert "not a statement that any impl is absent" in out
    assert len(out) <= cap
    assert "struct Box" in out
    # A cap that fits the first block and not the second still drops the
    # second whole, list included.
    whole = join_type_blocks([block, "struct Other"], len(block) + 5)
    assert whole == block


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


# --- blueprint slice derivation ------------------------------------------


def test_gather_includes_blueprint_slice(tmp_path: Path) -> None:
    fr = tmp_path / "functional_requirements_x.md"
    fr.write_text("FR")
    (tmp_path / "blueprint_x.md").write_text(
        "## Overview\nplan\n## ptr\nOwn<T> wraps NonNull; boxed() allocates.\n"
        "## error\nvtable at offset zero.\n")
    ctx = _assembler().gather(
        file_text="impl<T> Own<T> { fn boxed(){} }", file_rel="src/ptr.rs",
        fn_name="boxed", sig_text="fn boxed(self) -> Box<T>",
        body_text="", error_excerpt="", container="impl<T> Own<T>",
        type_records=[], fr_path=fr)
    assert "NonNull" in ctx.blueprint_section       # ptr section selected
    assert "offset zero" not in ctx.blueprint_section
    assert "plan" in ctx.blueprint_section          # anchor kept


def test_gather_blueprint_cache_reparses_on_change(tmp_path: Path) -> None:
    import os as _os
    fr = tmp_path / "functional_requirements_x.md"
    fr.write_text("FR")
    bp = tmp_path / "blueprint_x.md"
    bp.write_text("## Overview\nfirst\n")
    a = _assembler()
    kw: dict[str, Any] = {
        "file_text": "", "file_rel": "src/a.rs", "fn_name": "f",
        "sig_text": "fn f()", "body_text": "", "error_excerpt": "",
        "container": "", "type_records": [], "fr_path": fr}
    assert "first" in a.gather(**kw).blueprint_section
    bp.write_text("## Overview\nsecond\n")
    _os.utime(bp, (1e9, 1e9))                      # force distinct mtime
    assert "second" in a.gather(**kw).blueprint_section


def test_gather_without_blueprint_file_is_empty(tmp_path: Path) -> None:
    fr = tmp_path / "functional_requirements_x.md"
    fr.write_text("FR")
    ctx = _assembler().gather(
        file_text="", file_rel="src/a.rs", fn_name="f", sig_text="fn f()",
        body_text="", error_excerpt="", container="", type_records=[],
        fr_path=fr)
    assert ctx.blueprint_section == ""


def test_slice_identifiers_tokenises_and_drops_noise() -> None:
    from pipeline_core.domain.blueprint import slice_identifiers
    got = slice_identifiers("impl<T> Own<T> for Box<T>", "fn by_ref(&self)")
    assert "Own" in got and "Box" in got and "by_ref" in got
    assert "impl" not in got and "for" not in got and "T" not in got


def test_types_the_units_file_names_from_another_file_rank_last() -> None:
    """A stub's signature names none of the types its body will build; the
    file's sibling bodies do, as paths into the declaring file. From
    2026-09-01 to 2026-09-24 per-fn and per-impl fills asked 65 times for a
    type definition the prompt did not render. The file's crate types
    declared ELSEWHERE rank after everything the unit names; ones declared in
    the unit's own file are already in its file view and stay out."""
    recs = [
        TypeRecord(name="Holder", kind="struct", file="src/a.rs",
                   definition="struct Holder;"),
        TypeRecord(name="Wrapped", kind="struct", file="src/wrap.rs",
                   definition="pub struct Wrapped<M>(M);", is_pub=True),
        TypeRecord(name="Local", kind="struct", file="src/a.rs",
                   definition="struct Local;"),
    ]
    file_text = ("fn sibling() -> Holder { let w = crate::wrap::Wrapped(1); "
                 "Local; todo!() }\nfn target(h: Holder) {}")
    a = _assembler()
    with_file = a.types_section_for_fix(
        "fn target(h: Holder)", "", "", "", "", recs,
        file_rel="src/a.rs", file_text=file_text)
    assert "struct Holder" in with_file and "Wrapped" in with_file
    assert with_file.index("struct Holder") < with_file.index("Wrapped")
    assert "struct Local" not in with_file
    # Negative control: the pre-change selection, no file text.
    without = a.types_section_for_fix(
        "fn target(h: Holder)", "", "", "", "", recs, file_rel="src/a.rs")
    assert "Wrapped" not in without
