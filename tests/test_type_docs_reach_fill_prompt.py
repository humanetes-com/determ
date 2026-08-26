"""A `///` contract line authored on a type reaches the fill prompt end to end.

The chain has three textual links and every one of them has to keep the line:
the t1.5 splice (`scaffold_rewrite.replace_surface_definitions`, textual), the
extractor's `definition` (`render_item_definition` prints doc comments FIRST),
and `prompts.sections.format_type_for_prompt`, which until 2026-08-26 read the
definition for its `repr`/`derive` only and dropped the docs — the live
`stable` tree's documented `Chain` rendered bare. This pins the extractor →
renderer half through the real binary, same skip rule as the real-binary merge
pin in `test_extractor_adapter.py`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline_core.adapters.extractor import CliExtractor
from pipeline_core.prompts.sections import format_type_for_prompt

_REAL_BIN = (Path(__file__).resolve().parent.parent / "tools" / "skeleton_extractor"
             / "target" / "release" / "skeleton-extractor")

_CONTRACT = ("/// The TypeId stored for a message error is the message's own "
             "type, so a user's downcast::<&str>() finds it.")


@pytest.mark.skipif(not _REAL_BIN.exists(),
                    reason="skeleton-extractor binary not built (bash setup.sh)")
def test_authored_doc_line_reaches_the_fill_prompt_through_the_real_extractor(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_text(
        f"{_CONTRACT}\n"
        "#[repr(transparent)]\n"
        "pub struct MessageError<M>(pub M);\n"
        "\n"
        "/// Iterator of a chain of source errors.\n"
        "///\n"
        "/// Second paragraph.\n"
        "#[derive(Clone)]\n"
        "pub struct Chain<'a> { state: &'a str }\n"
        "\n"
        "pub struct Bare { a: u8 }\n")
    recs = CliExtractor(_REAL_BIN).enumerate_types(tmp_path)
    assert recs is not None, "extractor returned nothing"
    by_name = {r.name: r for r in recs}
    assert {"MessageError", "Chain", "Bare"} <= set(by_name), sorted(by_name)

    out = format_type_for_prompt(by_name["MessageError"])
    lines = out.splitlines()
    assert lines[0] == _CONTRACT, out
    assert lines[1] == "#[repr(transparent)]", out
    # the extractor prints generics with spaces (`< M >`); the claim here is
    # the doc line and the repr, above the type line, not the spacing.
    assert lines[2].startswith("struct MessageError<"), out

    chain = format_type_for_prompt(by_name["Chain"])
    assert chain.splitlines()[:3] == [
        "/// Iterator of a chain of source errors.",
        "/// Second paragraph.",
        "#[derive(Clone)]",
    ], chain

    # negative control: an undocumented type still renders with no `///`.
    assert "///" not in format_type_for_prompt(by_name["Bare"])
