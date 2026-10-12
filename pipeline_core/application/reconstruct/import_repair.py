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
from pipeline_core.domain.import_repair import apply_import_fixes, repair_keeps
from pipeline_core.domain.models import Diagnostic
from pipeline_core.domain.rust_use_items import bound_names, names_bound

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

    Returns the fresh (rc, rendered, diagnostics) triple when the re-check
    earned the inserted imports, None when no suggestion was applied (the
    name was already bound, the re-check did not earn the write, or the
    file could not be read). An unreadable or unwritable file skips the
    repair. A re-check that raises is restored to the original bytes and
    re-raised."""
    fixes = [f for d in diagnostics for f in d.import_fixes
             if f.file == file_rel]
    if not fixes:
        return None
    path = cwd / file_rel
    try:
        raw = path.read_bytes()
        source = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    patched = apply_import_fixes(source, fixes)
    if patched is None:
        blocked = sorted({
            f.replacement.strip() for f in fixes
            if names_bound(f.replacement) & bound_names(source)})
        if blocked:
            log(f"  {tag} {file_rel}: import repair left "
                f"{len(blocked)} suggestion(s) unapplied — the name is "
                f"already bound: {'; '.join(blocked)}")
        return None
    # A MECHANICAL write (rustc's own suggestion, no model in the loop):
    # parse-verified through the transaction and undone by byte restore.
    # The re-check is the other half of that contract. A suggestion that
    # defines a name the file already binds, or that leaves the error set
    # unchanged, is not a repair, and the bytes go back before any later
    # keep can land on the broken tree.
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
    try:
        rc, rendered, diags = check_diag(check_cmd, cwd, env)
    except Exception:
        path.write_bytes(raw)
        raise
    before = [(d.code, d.message) for d in diagnostics]
    after = [(d.code, d.message) for d in diags]
    added = sorted({f.replacement.strip() for f in fixes})
    if rc != 0 and not repair_keeps(before, after):
        path.write_bytes(raw)
        log(f"  {tag} {file_rel}: import repair restored the file — the "
            f"re-check did not earn {'; '.join(added)}")
        return None
    log(f"  {tag} {file_rel}: auto-imported {len(added)} use item(s) from "
        f"rustc suggestion(s): {'; '.join(added)}")
    return rc, rendered, diags
