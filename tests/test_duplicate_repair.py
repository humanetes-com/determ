"""The one repair whose action is a deletion: `E0592`, both of its ends, and
the authored layout that says which end is the intruder.

The negative controls are the pre-fix behaviours: a `Diagnostic` carrying only
its primary span attributes ONE fn (so the second definition is unreachable),
and a fix loop with no structural pre-step re-checks to a byte-identical error
however many bodies it rewrites.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.attribution import frontier_from_diags
from pipeline_core.application.reconstruct.cargo_errors import (
    diagnostics_from_json,
    secondary_spans_from_spans,
)
from pipeline_core.application.reconstruct.duplicate_repair import (
    DuplicateRepairDeps,
    repair_duplicate_definitions,
)
from pipeline_core.application.reconstruct.splice import delete_fn_item
from pipeline_core.domain.models import Diagnostic, FnLocator

FMT_RS = """use crate::error::ErrorImpl;

impl ErrorImpl {
    /// Format the error for `{:?}`.
    #[allow(dead_code)]
    pub(crate) unsafe fn debug(this: Ref<Self>, f: &mut fmt::Formatter) -> fmt::Result {
        write!(f, "fmt")
    }
}
"""

ERROR_RS = """use crate::ptr::Ref;

impl ErrorImpl {
    pub(crate) unsafe fn debug(this: Ref<Self>, f: &mut fmt::Formatter) -> fmt::Result {
        write!(f, "error")
    }

    pub(crate) fn keep_me(&self) -> u8 {
        7
    }
}
"""


def _records(root: Path) -> list[dict[str, Any]]:
    """Byte-exact fn records for the fixture files, computed from the text —
    the shape `enumerate_fns` returns, without needing the extractor binary."""
    out: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.rs")):
        text = path.read_text(encoding="utf-8")
        raw = text.encode("utf-8")
        for name in ("debug", "keep_me"):
            marker = f"fn {name}("
            if marker not in text:
                continue
            sig_start = raw.rfind(b"pub(crate)", 0, raw.index(marker.encode()))
            body_start = raw.index(b"{", raw.index(marker.encode())) + 1
            depth, i = 1, body_start
            while depth and i < len(raw):
                depth += (raw[i] == 0x7B) - (raw[i] == 0x7D)
                i += 1
            out.append({
                "file": str(path.relative_to(root)),
                "fn_name": name,
                "container": "impl ErrorImpl",
                "sig_text": raw[sig_start:body_start - 1].decode(),
                "sig_byte_start": sig_start,
                "body_byte_start": body_start,
                "body_byte_end": i - 1,
                "body_text": raw[body_start:i - 1].decode(),
            })
    return out


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    """A merged `src/` holding the duplicate, and an authored layout that
    declares `debug` in fmt.rs only — the shape of the 2026-08-11 tree."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "fmt.rs").write_text(FMT_RS, encoding="utf-8")
    (src / "error.rs").write_text(ERROR_RS, encoding="utf-8")
    authored = tmp_path / ".skeleton" / "stable" / "src"
    authored.mkdir(parents=True)
    (authored / "fmt.rs").write_text(FMT_RS, encoding="utf-8")
    (authored / "error.rs").write_text(
        ERROR_RS.replace(ERROR_RS[ERROR_RS.index("    pub(crate) unsafe fn debug"):
                                  ERROR_RS.index("    pub(crate) fn keep_me")], ""),
        encoding="utf-8")
    return src, authored


def _diag(src_rel_primary: str, line_primary: int,
          src_rel_other: str, line_other: int) -> Diagnostic:
    return Diagnostic(
        code="E0592", file=src_rel_primary, line=line_primary,
        message="duplicate definitions with name `debug`",
        rendered="error[E0592]: duplicate definitions with name `debug`",
        secondary_spans=((src_rel_other, line_other),))


# --- the spans -------------------------------------------------------------

def test_secondary_spans_parsed_from_cargo_json() -> None:
    msg = {"reason": "compiler-message", "message": {
        "level": "error", "code": {"code": "E0592"},
        "message": "duplicate definitions with name `debug`",
        "rendered": "error[E0592]",
        "spans": [
            {"file_name": "src/error.rs", "line_start": 301, "is_primary": True},
            {"file_name": "src/fmt.rs", "line_start": 11, "is_primary": False},
            {"file_name": "src/fmt.rs", "line_start": 11, "is_primary": False},
        ]}}
    (diag,) = diagnostics_from_json(json.dumps(msg)) or []
    assert diag.file == "src/error.rs" and diag.line == 301
    assert diag.secondary_spans == (("src/fmt.rs", 11),)     # deduped
    # Round-trips over the wire (the walk fixtures and the event ledger).
    assert Diagnostic.from_dict(diag.to_dict()).secondary_spans == diag.secondary_spans


def test_primary_only_diagnostic_carries_no_secondary_spans() -> None:
    assert secondary_spans_from_spans(
        [{"file_name": "src/lib.rs", "line_start": 4, "is_primary": True}]) == ()


def test_frontier_reaches_both_definitions(tmp_path: Path) -> None:
    src, _ = _tree(tmp_path)
    err_line = ERROR_RS[:ERROR_RS.index("fn debug")].count("\n") + 1
    fmt_line = FMT_RS[:FMT_RS.index("fn debug")].count("\n") + 1

    class _Structure:
        def enumerate_fns(self, d: Path) -> list[dict[str, Any]]:
            return _records(d)

    deps = type("D", (), {"structure": _Structure(), "log": print})()
    frontier = frontier_from_diags(
        deps, [_diag("error.rs", err_line, "fmt.rs", fmt_line)], src)

    assert FnLocator("error.rs", "debug") in frontier
    # The negative control: before secondary spans existed this side was
    # invisible, so every attempt rewrote the body of the other one.
    assert FnLocator("fmt.rs", "debug") in frontier
    without = frontier_from_diags(
        deps, [Diagnostic(code="E0592", file="error.rs", line=err_line)], src)
    assert FnLocator("fmt.rs", "debug") not in without


def test_implicated_keys_carry_the_container_the_frontier_erases(
        tmp_path: Path) -> None:
    """`implicated_fn_keys` is the peel's implication identity: the SAME span
    resolution as `frontier_from_diags`, keeping the container. Two impls
    legally share a method name, and projecting to `FnLocator` welds them —
    which is how run `57025-6ba652f2`'s peel deleted the first correct-shaped
    generic `ext_context` alongside its doomed same-named sibling
    (`phase_c.file.fill.peeled` 17:40:17 UTC: reverted six, kept 0). Here two
    impls of one file declare `ext_context`; a diagnostic on one line must
    key exactly that impl's body, and the frontier projection of the same
    diag shows the weld the key exists to undo."""
    from pipeline_core.application.reconstruct.attribution import (
        implicated_fn_keys,
    )
    src = tmp_path / "src"
    src.mkdir()
    text = ("struct Error;\n"
            "impl StdError for E {\n"
            "    fn ext_context(self) -> Error {\n"
            "        Error\n"
            "    }\n"
            "}\n"
            "impl StdError for Error {\n"
            "    fn ext_context(self) -> Error {\n"
            "        Error\n"
            "    }\n"
            "}\n")
    (src / "context.rs").write_text(text, encoding="utf-8")

    def _recs(_d: Path) -> list[dict[str, Any]]:
        raw = text.encode("utf-8")
        out = []
        pos = 0
        for cont in ("impl StdError for E", "impl StdError for Error"):
            sig_start = raw.index(b"fn ext_context", pos)
            body_start = raw.index(b"{", sig_start) + 1
            depth, i = 1, body_start
            while depth and i < len(raw):
                depth += (raw[i] == 0x7B) - (raw[i] == 0x7D)
                i += 1
            out.append({
                "file": "context.rs", "fn_name": "ext_context",
                "container": cont,
                "sig_text": raw[sig_start:body_start - 1].decode(),
                "sig_byte_start": sig_start,
                "body_byte_start": body_start, "body_byte_end": i - 1,
            })
            pos = i
        return out

    class _Structure:
        def enumerate_fns(self, d: Path) -> list[dict[str, Any]]:
            return _recs(d)

    deps = type("D", (), {"structure": _Structure(), "log": print})()
    # Line 8 is inside the SECOND impl's ext_context.
    d = Diagnostic(code="E0277", file="context.rs", line=8)
    keys = implicated_fn_keys(deps, [d], src)
    assert keys == frozenset(
        {("context.rs", "ext_context", "impl StdError for Error")})
    # The frontier projection of the same diagnostic cannot tell the impls
    # apart — the weld the container key exists to undo.
    assert set(frontier_from_diags(deps, [d], src)) == {
        FnLocator("context.rs", "ext_context")}


# --- the deletion ----------------------------------------------------------

def test_delete_fn_item_removes_the_item_with_its_attributes(tmp_path: Path) -> None:
    src, _ = _tree(tmp_path)
    outcome = delete_fn_item(_records, src / "fmt.rs", "debug", "impl ErrorImpl")
    text = (src / "fmt.rs").read_text(encoding="utf-8")

    assert outcome.ok and outcome.delta < 0
    assert "fn debug" not in text
    # The doc comment and `#[allow]` went with it — a stranded attribute would
    # apply itself to whatever follows.
    assert "#[allow(dead_code)]" not in text and "Format the error" not in text
    assert "impl ErrorImpl {" in text          # the block itself survives


def test_delete_keeps_the_siblings(tmp_path: Path) -> None:
    src, _ = _tree(tmp_path)
    assert delete_fn_item(_records, src / "error.rs", "debug", "impl ErrorImpl").ok
    text = (src / "error.rs").read_text(encoding="utf-8")
    assert "fn keep_me" in text and "7" in text and "fn debug" not in text


def test_delete_refuses_a_name_it_cannot_locate(tmp_path: Path) -> None:
    src, _ = _tree(tmp_path)
    before = (src / "fmt.rs").read_text(encoding="utf-8")
    outcome = delete_fn_item(_records, src / "fmt.rs", "nosuchfn", "impl ErrorImpl")
    assert not outcome.ok and outcome.reason == "fn_not_found"
    assert (src / "fmt.rs").read_text(encoding="utf-8") == before


# --- the use-case ----------------------------------------------------------

def _deps(diag_sets: list[list[Diagnostic]], log: list[str]) -> DuplicateRepairDeps:
    return DuplicateRepairDeps(
        probe=lambda: (101, "rendered", diag_sets.pop(0)),
        enumerate_fns=_records,
        log=log.append,
        emit=lambda *a, **k: None)


def test_repair_deletes_the_unauthored_copy(tmp_path: Path) -> None:
    src, _ = _tree(tmp_path)
    err_line = ERROR_RS[:ERROR_RS.index("fn debug")].count("\n") + 1
    fmt_line = FMT_RS[:FMT_RS.index("fn debug")].count("\n") + 1
    log: list[str] = []
    report = repair_duplicate_definitions(
        tmp_path, src,
        _deps([[_diag("src/error.rs", err_line, "src/fmt.rs", fmt_line)], []], log))

    assert report is not None and "duplicate" in (report.summary or "")
    # error.rs is the copy no authored layout declares — fmt.rs keeps its own.
    assert "fn debug" not in (src / "error.rs").read_text(encoding="utf-8")
    assert "fn debug" in (src / "fmt.rs").read_text(encoding="utf-8")
    assert "fn keep_me" in (src / "error.rs").read_text(encoding="utf-8")
    assert any("deleted error.rs::debug" in line for line in log)


def test_repair_restores_when_the_deletion_does_not_help(tmp_path: Path) -> None:
    src, _ = _tree(tmp_path)
    err_line = ERROR_RS[:ERROR_RS.index("fn debug")].count("\n") + 1
    fmt_line = FMT_RS[:FMT_RS.index("fn debug")].count("\n") + 1
    before = (src / "error.rs").read_bytes()
    stuck = [_diag("src/error.rs", err_line, "src/fmt.rs", fmt_line)]
    log: list[str] = []
    report = repair_duplicate_definitions(tmp_path, src, _deps([stuck, stuck], log))

    assert report is None
    # Byte for byte: the restore is the transaction primitive over the
    # captured pre-image.
    assert (src / "error.rs").read_bytes() == before
    assert any("did not reduce diagnostics" in line for line in log)


def test_repair_refuses_to_choose_when_both_are_authored(tmp_path: Path) -> None:
    src, authored = _tree(tmp_path)
    (authored / "error.rs").write_text(ERROR_RS, encoding="utf-8")   # both authored
    err_line = ERROR_RS[:ERROR_RS.index("fn debug")].count("\n") + 1
    fmt_line = FMT_RS[:FMT_RS.index("fn debug")].count("\n") + 1
    log: list[str] = []
    report = repair_duplicate_definitions(
        tmp_path, src,
        _deps([[_diag("src/error.rs", err_line, "src/fmt.rs", fmt_line)]], log))

    assert report is None
    assert "fn debug" in (src / "error.rs").read_text(encoding="utf-8")
    assert any("name neither uniquely" in line for line in log)


def test_no_duplicate_error_is_a_no_op(tmp_path: Path) -> None:
    src, _ = _tree(tmp_path)
    report = repair_duplicate_definitions(
        tmp_path, src,
        _deps([[Diagnostic(code="E0308", file="src/lib.rs", line=4)]], []))
    assert report is None
