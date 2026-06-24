"""Characterization for the relocated downstream-API scanner adapter (P6).
Pins the use-import inference and the empty-result contract over a tmp workspace."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.adapters.downstream_api import scan_downstream_api


def test_empty_when_no_imports(tmp_path: Path) -> None:
    (tmp_path / "a.rs").write_text("fn main() {}\n", encoding="utf-8")
    assert scan_downstream_api(tmp_path, "mycrate") == ""


def test_infers_direct_and_submodule_imports(tmp_path: Path) -> None:
    (tmp_path / "consumer.rs").write_text(
        "use mycrate::{TopType};\n"
        "use mycrate::parser::ParseError;\n",
        encoding="utf-8",
    )
    out = scan_downstream_api(tmp_path, "mycrate")
    assert "REQUIRED PUBLIC API for `mycrate`" in out
    assert "TopType" in out
    assert "pub mod parser;" in out and "ParseError" in out
