"""Deterministic import repair from rustc suggestions (E0425-class fills).

A body-only splice cannot add file-level `use` items, so a model body naming a
real in-crate item unqualified fails compile forever while rustc prints the
exact fix. These pins cover: parsing unambiguous "consider importing"
suggestions out of cargo JSON, refusing ambiguous ones, byte-span application,
Diagnostic round-tripping, and the application step that patches the target
file and re-checks.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.application.reconstruct.cargo_errors import (
    diagnostics_from_json,
)
from pipeline_core.application.reconstruct.import_repair import (
    repair_missing_imports,
)
from pipeline_core.domain.import_repair import (
    ImportFix,
    apply_import_fixes,
    import_fixes_from_json,
)
from pipeline_core.domain.models import Diagnostic


def _cargo_msg(children: list[dict], code: str = "E0425") -> str:
    return json.dumps({
        "reason": "compiler-message",
        "message": {
            "level": "error",
            "code": {"code": code},
            "message": "cannot find function, tuple struct or tuple variant "
                       "`MessageError` in this scope",
            "rendered": "error[E0425]: cannot find ...",
            "spans": [{"is_primary": True, "file_name": "src/error.rs",
                       "line_start": 184}],
            "children": children,
        },
    })


def _import_child(replacement: str, file: str = "src/error.rs",
                  byte: int = 20, message: str = "consider importing this tuple struct"):
    return {"message": message, "level": "help",
            "spans": [{"file_name": file, "byte_start": byte, "byte_end": byte,
                       "suggested_replacement": replacement,
                       "suggestion_applicability": "MaybeIncorrect"}]}


def test_parses_single_candidate_import_suggestion() -> None:
    raw = _cargo_msg([_import_child("use crate::wrapper::MessageError;\n")])
    fixes = import_fixes_from_json(raw)
    assert fixes == (ImportFix("src/error.rs", 20, 20,
                               "use crate::wrapper::MessageError;\n"),)


def test_skips_ambiguous_and_non_import_suggestions() -> None:
    # Two candidate spans in one child → ambiguous → never guess.
    ambiguous = {"message": "consider importing one of these items",
                 "level": "help",
                 "spans": [
                     {"file_name": "src/error.rs", "byte_start": 20,
                      "byte_end": 20,
                      "suggested_replacement": "use a::X;\n"},
                     {"file_name": "src/error.rs", "byte_start": 20,
                      "byte_end": 20,
                      "suggested_replacement": "use b::X;\n"},
                 ]}
    # A non-import replacement under an importing-ish message.
    not_use = _import_child("let x = 1;\n")
    # A child that is not an import suggestion at all.
    other = {"message": "the trait `Copy` is not implemented", "level": "note",
             "spans": []}
    raw = _cargo_msg([ambiguous, not_use, other])
    assert import_fixes_from_json(raw) == ()


def test_apply_fixes_inserts_dedupes_and_skips_present() -> None:
    src = "// header\nuse core::fmt;\n\nfn f() {}\n"
    at = len(b"// header\n")
    fix = ImportFix("src/e.rs", at, at, "use crate::w::M;\n")
    out = apply_import_fixes(src, [fix, fix])   # duplicate collapses
    assert out is not None
    assert out.count("use crate::w::M;") == 1
    assert out.startswith("// header\nuse crate::w::M;\nuse core::fmt;")
    # Already present in source → nothing to do.
    assert apply_import_fixes(out, [fix]) is None
    # Span outside the file → refuse.
    bad = ImportFix("src/e.rs", 10_000, 10_000, "use x::Y;\n")
    assert apply_import_fixes(src, [bad]) is None


def test_diagnostics_from_json_carries_and_roundtrips_fixes() -> None:
    raw = _cargo_msg([_import_child("use crate::wrapper::MessageError;\n")])
    diags = diagnostics_from_json(raw)
    assert diags is not None and len(diags) == 1
    d = diags[0]
    assert d.code == "E0425"
    assert d.import_fixes and d.import_fixes[0].replacement.startswith("use ")
    assert Diagnostic.from_dict(d.to_dict()) == d


def test_repair_step_patches_file_and_rechecks(tmp_path: Path) -> None:
    f = tmp_path / "src" / "error.rs"
    f.parent.mkdir()
    f.write_text("// hdr\nfn f() {}\n", encoding="utf-8")
    at = len(b"// hdr\n")
    diag = Diagnostic(code="E0425", file="src/error.rs", line=2,
                      import_fixes=(ImportFix("src/error.rs", at, at,
                                              "use crate::w::M;\n"),))
    rechecks: list[str] = []

    def _check(cmd, cwd, env):
        rechecks.append(cmd)
        return 0, "ok", []

    logs: list[str] = []
    res = repair_missing_imports(_check, "cargo check", tmp_path, {},
                                 "src/error.rs", [diag], logs.append, "[t]")
    assert res == (0, "ok", [])
    assert "use crate::w::M;" in f.read_text(encoding="utf-8")
    assert rechecks == ["cargo check"]
    assert any("auto-imported" in ln for ln in logs)
    # Fixes for OTHER files are out of scope (snapshot revert wouldn't cover
    # them) → no-op.
    other = Diagnostic(code="E0425", file="src/other.rs",
                       import_fixes=(ImportFix("src/other.rs", 0, 0,
                                               "use x::Y;\n"),))
    assert repair_missing_imports(_check, "cargo check", tmp_path, {},
                                  "src/error.rs", [other], logs.append,
                                  "[t]") is None


def test_parse_edge_cases_are_skipped() -> None:
    # Non-dict child, missing byte offsets, non-error level, non-JSON lines,
    # non-compiler-message reason — all skipped without error.
    from pipeline_core.domain.import_repair import import_fixes_from_message
    assert import_fixes_from_message({"children": ["not-a-dict"]}) == ()
    no_bytes = _import_child("use a::B;\n")
    no_bytes["spans"][0].pop("byte_start")
    assert import_fixes_from_message({"children": [no_bytes]}) == ()
    warn = json.dumps({"reason": "compiler-message",
                       "message": {"level": "warning", "children": []}})
    artifact = json.dumps({"reason": "compiler-artifact"})
    raw = "not json {\n" + warn + "\n" + artifact
    assert import_fixes_from_json(raw) == ()


def test_apply_fixes_utf8_and_replace_span() -> None:
    # A replacing (non-insertion) span and multi-byte source both apply
    # cleanly on byte offsets.
    src = "// é\nuse old::X;\nfn f() {}\n"
    start = len("// é\n".encode())
    end = start + len(b"use old::X;\n")
    fix = ImportFix("src/e.rs", start, end, "use new::X;\n")
    out = apply_import_fixes(src, [fix])
    assert out == "// é\nuse new::X;\nfn f() {}\n"


def test_invalid_json_line_and_mid_char_span() -> None:
    # A line that LOOKS like JSON but isn't parses to nothing.
    assert import_fixes_from_json("{ not valid json") == ()
    # A span landing inside a multi-byte character would corrupt the file —
    # the decode check refuses it.
    fix = ImportFix("src/e.rs", 1, 1, "use x::Y;\n")
    assert apply_import_fixes("é fn f() {}", [fix]) is None
