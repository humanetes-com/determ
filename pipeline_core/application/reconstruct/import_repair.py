"""Apply rustc's unambiguous import suggestions after a failed fill compile.

Composition of the pure domain rules (:mod:`pipeline_core.domain.import_repair`)
with file I/O and a re-check: when a spliced body fails `cargo check` with
`cannot find ...` errors for which rustc itself supplies a single-candidate
"consider importing" replacement span, insert those `use` items into the
TARGET file and re-run the check. Scoped to the file the fill owns so the
caller's snapshot revert covers the added imports too.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pipeline_core.application.reconstruct.tree_txn import ParseValidator, TreeTxn
from pipeline_core.domain.import_repair import apply_import_fixes
from pipeline_core.domain.models import Diagnostic

CheckDiag = Callable[..., "tuple[int, str, list[Diagnostic]]"]


def repair_missing_imports(
    check_diag: CheckDiag,
    check_cmd: str,
    cwd: Path,
    env: dict[str, str],
    file_rel: str,
    diagnostics: list[Diagnostic],
    log: Callable[[str], None],
    tag: str,
    *,
    validate_parse: ParseValidator | None = None,
) -> tuple[int, str, list[Diagnostic]] | None:
    """Try the deterministic import repair for `file_rel` and re-check.

    Returns the fresh (rc, rendered, diagnostics) triple when imports were
    inserted, None when no unambiguous suggestion targeted `file_rel` (caller
    keeps its original result). Never raises: an unreadable/unwritable file
    just skips the repair."""
    fixes = [f for d in diagnostics for f in d.import_fixes
             if f.file == file_rel]
    if not fixes:
        return None
    path = cwd / file_rel
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return None
    patched = apply_import_fixes(source, fixes)
    if patched is None:
        return None
    # A MECHANICAL write (rustc's own suggestion, no model in the loop):
    # parse-verified through the transaction and undone by byte restore.
    txn = TreeTxn(cwd, validate_parse=validate_parse)
    try:
        txn.write(file_rel, patched)
    except OSError:
        return None
    verdict = txn.close()
    if not verdict.ok:
        log(f"  {tag} {file_rel}: import repair left the file unparseable "
            f"({verdict.reason}) — restored.")
        return None
    added = sorted({f.replacement.strip() for f in fixes})
    log(f"  {tag} {file_rel}: auto-imported {len(added)} use item(s) from "
        f"rustc suggestion(s): {'; '.join(added)}")
    return check_diag(check_cmd, cwd, env)
