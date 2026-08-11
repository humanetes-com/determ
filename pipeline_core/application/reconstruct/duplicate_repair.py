"""Repair the one compile error whose fix is a DELETION.

`E0592` — "duplicate definitions with name `X`" — names two definitions of one
item on one type. Every repair route the fix loop owns rewrites a body, and no
body rewrite changes the fact that a second definition exists in another file,
so the check comes back byte-identical however many attempts are spent. That is
attempt-invariant at attempt 1, and it is what happened on the merged tree of
2026-08-11: three `spliced src/error.rs::debug` attempts, an escalation to
whole-file rewrites of `lib.rs`, `kind.rs`, `chain.rs` and `context.rs` — none
of which held the duplicate — and clean-file counts cycling 9 → 6 → 10 → 7 → 7
→ 9 → 10 of 12 across two full passes.

This use-case runs BEFORE the model on any attempt whose check reports an
`E0592`, and it acts only where the evidence is unambiguous:

- both ends must resolve to real fn records (rustc's primary span plus the
  secondary spans the diagnostic model now carries — before that, the second
  definition was not addressable at all);
- the authored layouts under `.skeleton/<cluster>/src` must name exactly ONE of
  the two placements as unauthored. That one is the fix-loop-introduced copy,
  and the rule can never point at frozen scaffold. When the layouts do not
  disambiguate, this returns None and the model gets its turn as before;
- the deletion must measurably reduce the distinct diagnostic count, or the
  file goes back exactly as it was.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.splice import delete_fn_item
from pipeline_core.application.reconstruct.splice_txn import ParseValidator
from pipeline_core.domain.associated_items import (
    AssociatedItem,
    DuplicateIntroduction,
    items_by_file,
    removal_target,
)
from pipeline_core.domain.attribution import line_to_fn_record
from pipeline_core.domain.fix_report import FixReport
from pipeline_core.domain.models import Diagnostic
from pipeline_core.domain.rust_idents import impl_self_type, impl_trait_name

DUPLICATE_DEFINITION_CODE = "E0592"


@dataclass(frozen=True, slots=True)
class DuplicateRepairDeps:
    """Collaborators; the composition root binds them to the live ports."""

    probe: Callable[[], tuple[int, str, list[Diagnostic]]]
    enumerate_fns: Callable[[Path], list[dict[str, Any]] | None]
    log: Callable[[str], None]
    emit: Callable[..., None]
    validate_parse: ParseValidator | None = None


def _item_of(rec: dict[str, Any]) -> AssociatedItem | None:
    container = str(rec.get("container", "") or "")
    self_type = impl_self_type(container) if container.strip() else None
    if not self_type:
        return None
    return AssociatedItem(self_type, impl_trait_name(container) or "",
                          str(rec.get("fn_name", "") or ""))


def _distinct(diags: list[Diagnostic]) -> int:
    return len({(d.code, d.file, d.line, d.message) for d in diags})


def _authored_items(project_dir: Path, deps: DuplicateRepairDeps,
                    ) -> dict[str, frozenset[AssociatedItem]]:
    """Every item each authored cluster layout declares, unioned per file.

    Both layouts are read because a cfg-gated item can be authored in one
    cluster only; an item authored ANYWHERE is not the fix loop's invention."""
    merged: dict[str, set[AssociatedItem]] = {}
    for layout in sorted((project_dir / ".skeleton").glob("*/src")):
        for rel, items in items_by_file(deps.enumerate_fns(layout) or []).items():
            merged.setdefault(rel, set()).update(items)
    return {rel: frozenset(items) for rel, items in merged.items()}


def _resolve(src_root: Path, project_dir: Path, recs: list[dict[str, Any]],
             file_field: str, line: int) -> tuple[str, dict[str, Any]] | None:
    """A cargo span → (src-root-relative path, fn record) it sits inside."""
    if not file_field or line < 1:
        return None
    try:
        rel = str((project_dir / file_field).resolve().relative_to(src_root.resolve()))
    except (OSError, ValueError):
        return None
    in_file = [r for r in recs if r.get("file") == rel]
    if not in_file:
        return None
    try:
        text = (src_root / rel).read_text(encoding="utf-8")
    except OSError:
        return None
    rec = line_to_fn_record(text, line, in_file)
    return None if rec is None else (rel, rec)


def repair_duplicate_definitions(
    project_dir: Path, src_root: Path, deps: DuplicateRepairDeps,
) -> FixReport | None:
    """Delete the unauthored half of one duplicate definition, or return None.

    None means "nothing done, carry on with the model" for every reason: no
    E0592, unresolvable spans, an ambiguous authored layout, a refused delete,
    or a delete that did not reduce the diagnostics.
    """
    try:
        _rc, _rendered, diags = deps.probe()
    except Exception:
        return None
    dups = [d for d in diags if d.code == DUPLICATE_DEFINITION_CODE]
    if not dups:
        return None

    recs = deps.enumerate_fns(src_root) or []
    before = _distinct(diags)
    for diag in dups:
        primary = _resolve(src_root, project_dir, recs, diag.file, diag.line)
        if primary is None:
            continue
        item = _item_of(primary[1])
        others = []
        for s_file, s_line in diag.secondary_spans:
            found = _resolve(src_root, project_dir, recs, s_file, s_line)
            if found is not None and found[0] != primary[0] and _item_of(found[1]) == item:
                others.append(found)
        if item is None or not others:
            continue

        loser_rel = removal_target(
            DuplicateIntroduction(item, primary[0],
                                  tuple(sorted(o[0] for o in others))),
            _authored_items(project_dir, deps))
        if not loser_rel:
            deps.log(f"  [fix loop/duplicate] {item.render()} defined in "
                     f"{primary[0]} and {', '.join(o[0] for o in others)} — the "
                     f"authored layouts name neither uniquely; not deleting.")
            deps.emit("fix.duplicate_ambiguous", data={
                "item": item.render(),
                "files": [primary[0], *(o[0] for o in others)]})
            continue

        loser_rec = next(
            (rec for rel, rec in [primary, *others] if rel == loser_rel), None)
        if loser_rec is None:
            continue
        path = src_root / loser_rel
        pre_image = path.read_bytes()
        outcome = delete_fn_item(
            deps.enumerate_fns, path, str(loser_rec.get("fn_name", "")),
            str(loser_rec.get("container", "")),
            str(loser_rec.get("sig_text", "")), deps.validate_parse)
        if not outcome.ok:
            deps.log(f"  [fix loop/duplicate] delete refused for "
                     f"{loser_rel}::{item.item_name} ({outcome.reason}).")
            continue

        _rc2, _r2, after_diags = deps.probe()
        after = _distinct(after_diags)
        if after >= before:
            path.write_bytes(pre_image)
            deps.log(f"  [fix loop/duplicate] deleting {loser_rel}::"
                     f"{item.item_name} did not reduce diagnostics "
                     f"({before} → {after}) — restored.")
            deps.emit("fix.duplicate_delete_reverted", data={
                "item": item.render(), "file": loser_rel,
                "diags_before": before, "diags_after": after})
            continue

        deps.log(f"  [fix loop/duplicate] deleted {loser_rel}::{item.item_name}"
                 f" — the copy no authored layout declares; diagnostics "
                 f"{before} → {after}.")
        deps.emit("fix.duplicate_deleted", data={
            "item": item.render(), "file": loser_rel,
            "diags_before": before, "diags_after": after,
            "bytes": outcome.delta})
        return FixReport.changed(
            summary=f"deleted duplicate {item.render()}",
            files_changed_str=loser_rel,
            error_preview=_rendered[-2000:] if _rendered else "")
    return None
