"""P1 proof — StructureReader is constructor-injected, not monkeypatched.

Demonstrates the pattern CODE_ARCHITECTURE §4 P1 establishes: the real
`CliStructureReader` wraps the `Extractor` seam (the `.to_dict()` logic that used to
live in pipeline.py shims), and `_PhaseCFillAdapter` takes a `StructureReader` by
CONSTRUCTOR so a test injects a fake — no `monkeypatch pipeline._enumerate_fns_via_extractor`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pipeline_core.adapters.structure import CliStructureReader
from pipeline_core.domain.models import FnRecord, TypeRecord


class _FakeExtractor:
    """Minimal Extractor: returns canned typed records for the two used methods."""

    def __init__(self, fns: list[FnRecord] | None, types: list[TypeRecord] | None) -> None:
        self._fns, self._types = fns, types

    def enumerate_fns(self, src_dir: Path, **_kw: Any) -> list[FnRecord] | None:
        return self._fns

    def enumerate_types(self, src_dir: Path, **_kw: Any) -> list[TypeRecord] | None:
        return self._types

    def enumerate_mods(self, *a: Any, **k: Any) -> None: return None
    def enumerate_consts(self, *a: Any, **k: Any) -> None: return None
    def enumerate_dispatch_families(self, *a: Any, **k: Any) -> None: return None


def test_cli_structure_reader_wraps_extractor_to_dicts() -> None:
    """enumerate_fns returns wire DICTS (the engine contract); enumerate_types passes
    typed records through; None propagates."""
    rec = FnRecord(file="src/lib.rs", fn_name="answer", sig_text="fn answer() -> u32")
    ty = TypeRecord(file="src/lib.rs", kind="struct", name="Foo")
    reader = CliStructureReader(_FakeExtractor([rec], [ty]))  # type: ignore[arg-type]
    fns = reader.enumerate_fns(Path("x"))
    assert fns == [rec.to_dict()] and isinstance(fns[0], dict)
    assert reader.enumerate_types(Path("x")) == [ty]
    # None propagation (binary-absent contract).
    assert CliStructureReader(_FakeExtractor(None, None)).enumerate_fns(Path("x")) is None  # type: ignore[arg-type]


@dataclass
class _FakeStructure:
    """A StructureReader fake that records calls — proves the injected instance is used."""
    fns: list[dict[str, Any]] | None = None
    types: list[TypeRecord] | None = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    def enumerate_fns(self, src_dir: Path) -> list[dict[str, Any]] | None:
        self.calls.append("fns"); return self.fns

    def enumerate_types(self, src_dir: Path) -> list[TypeRecord] | None:
        self.calls.append("types"); return self.types

    def enumerate_mods(self, src_dir: Path) -> None: return None
    def enumerate_consts(self, src_dir: Path) -> None: return None
    def enumerate_dispatch_families(self, src_dir: Path) -> None: return None


def test_phase_c_adapter_uses_constructor_injected_structure() -> None:
    """`_PhaseCFillAdapter(structure=fake)` routes enumerate reads through the FAKE —
    no module-global monkeypatch. This is the reversed test-seam (debt: anti-pattern B)."""
    import pipeline
    fake = _FakeStructure(fns=[{"fn_name": "answer", "file": "src/lib.rs"}])
    adapter = pipeline._PhaseCFillAdapter(structure=fake)
    assert adapter.enumerate_fns(Path("x")) == [{"fn_name": "answer", "file": "src/lib.rs"}]
    assert adapter.enumerate_types(Path("x")) == []
    assert fake.calls == ["fns", "types"]              # the injected instance was used
    # And the engine builder threads that adapter (the injection point for tests).
    engine = pipeline._build_phase_c_engine(adapter)
    assert engine.ws is adapter and engine.mut is adapter


def test_default_adapter_holds_the_real_reader() -> None:
    """Composition root wires the real CliStructureReader by default."""
    import pipeline
    assert isinstance(pipeline._PhaseCFillAdapter()._structure, CliStructureReader)
    assert isinstance(pipeline._STRUCTURE, CliStructureReader)
