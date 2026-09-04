"""Apply a repair's file writes as one transaction that can be taken back.

The whole-file rewrite path used to write straight to disk: its only
pre-conditions were "the path is under the allowed directory" and "the content
does not look like compiler output". Nothing read the result, so a rewrite that
tripled the crate's diagnostics or introduced a duplicate definition was
indistinguishable, on disk, from a repair.

This use-case gives that write a verdict. Sequence: open a :class:`TreeTxn`
over the target files (pre-images captured on first touch), apply, probe,
enumerate; hand the two readings to the domain rules
(`domain/write_verdict.py`, `domain/associated_items.py`); roll the
transaction back if they refuse.

Two properties worth stating because they were choices:

- **All-or-nothing.** A refusal restores every file in the write, not the
  worst one. Diagnostics attribute to spans, not to authorship — when a rewrite
  of three files raises the count there is no evidence saying which of them did
  it, and restoring the guilty-looking file alone leaves a tree neither the
  model nor the pipeline has ever checked.
- **The probe is the default cluster.** The count compared is `cargo check`'s
  under one env, before and after the same write, which is a sound comparison
  but blind to a cfg-gated cluster the write breaks alone. The per-cluster
  reading that sees those lives in the t3 repair path; this gate is deliberately
  the cheap one that runs on every write.

The rollback is the transaction's byte restore, the same primitive every
mechanical writer uses; this module owns only the verdict rule.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.tree_txn import ParseValidator, TreeTxn
from pipeline_core.domain.associated_items import (
    AssociatedItem,
    introduced_duplicates,
    items_by_file,
)
from pipeline_core.domain.write_verdict import (
    WriteOutcome,
    WriteVerdict,
    judge_write,
)


@dataclass(frozen=True, slots=True)
class WriteGateDeps:
    """Collaborators the gate needs; the fix loop binds them from its own."""

    # None means "the probe could not answer" (cargo unavailable, timeout) —
    # the count rule then abstains rather than refusing every write on a
    # reading it does not have. The duplicate rule still applies: it reads the
    # tree, not the compiler.
    probe_diag_count: Callable[[], int | None]
    enumerate_fn_records: Callable[[], list[dict[str, Any]]]
    log: Callable[[str], None]
    emit: Callable[..., None]
    # The structure reader's parse check. An authored whole-file rewrite is
    # allowed to be compile-red (the fix loop owns that), never unparseable.
    validate_parse: ParseValidator | None = None


def _items(deps: WriteGateDeps) -> dict[str, frozenset[AssociatedItem]]:
    try:
        return items_by_file(deps.enumerate_fn_records() or [])
    except Exception:
        # The extractor is a subprocess; if it cannot answer, the duplicate
        # rule simply does not fire this round. An empty map makes every item
        # look new, so returning one here would refuse every write instead.
        return {}


def guarded_write(
    writes: Mapping[str, str], project_dir: Path, deps: WriteGateDeps,
) -> WriteVerdict:
    """Apply `writes` (rel path → content), judge, restore on refusal.

    Returns the verdict; `verdict.kept` is False when the tree was put back the
    way it was. The caller keeps its own logging of what it wrote — this only
    logs the refusal, which is the event nobody could see before.
    """
    if not writes:
        return judge_write(before=0, after=0)

    before_count = deps.probe_diag_count()
    before_items = _items(deps)

    txn = TreeTxn(project_dir, validate_parse=deps.validate_parse)
    for rel, content in writes.items():
        txn.write(rel, content)

    parse = txn.verify()
    if not parse.ok:
        txn.rollback()
        deps.log(f"  [fix loop/gate] REFUSED {', '.join(sorted(writes))}: "
                 f"{parse.reason}")
        deps.emit("fix.write_judged", data={
            "outcome": "parse_broken", "files": sorted(writes),
            "diags_before": before_count or 0, "diags_after": before_count or 0,
            "duplicates": [], "broken": list(parse.broken),
        })
        return WriteVerdict(WriteOutcome.REFUSE_PARSE_BROKEN,
                            before_count or 0, before_count or 0)

    after_count = deps.probe_diag_count()
    counted = before_count is not None and after_count is not None
    verdict = judge_write(
        before=before_count if before_count is not None and counted else 0,
        after=after_count if after_count is not None and counted else 0,
        duplicates=introduced_duplicates(before_items, _items(deps)))

    if not verdict.kept:
        txn.rollback()
        deps.log(f"  [fix loop/gate] REFUSED {', '.join(sorted(writes))}: "
                 f"{verdict.render()}")
    else:
        txn.commit()
    deps.emit("fix.write_judged", data={
        "outcome": verdict.outcome.value,
        "files": sorted(writes),
        "diags_before": verdict.before,
        "diags_after": verdict.after,
        "duplicates": [d.render() for d in verdict.duplicates],
    })
    return verdict
