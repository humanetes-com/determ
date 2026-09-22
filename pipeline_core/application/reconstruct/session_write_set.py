"""A slice session's write set at open: the planned slice plus the table
complement of its files (`installable_fns.table_complement`).

The planner reads the write set off the failing tests' coverage. A free fn
that fits a field of a fn-pointer table an editable unit builds can be out of
that set for two reasons, and neither says the fn is not the defect: no table
names it yet, so nothing executes it (run `195091-72b92f1e`, 2026-09-21: the
session held the producer, not the two table fns it had to install), or the
editable builder installs it on a path the failing test does not reach (run
`92187-f71f12bf`, 2026-09-22: the model rewrote the drop slot the editable
builder's literal names and the splicer refused it). Either way the builder
is writable and its slot is not, so no reply can write the pair.

The slice is rebuilt with `dataclasses.replace`, never field by field: the
open used to rebuild it as `(tests, units)` and dropped the planner's
`file_chars`, so a session the planner had fitted by condensing its files
opened rendered at the widest threshold (found in review, 2026-09-21).
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

from pipeline_core.domain.attribution import locator_of, record_site
from pipeline_core.domain.installable_fns import table_complement
from pipeline_core.domain.models import FnRecord, FnSite, TypeRecord
from pipeline_core.domain.rust_idents import fn_reference_counts
from pipeline_core.domain.slice_session import SessionSlice


@dataclass(frozen=True, slots=True)
class SessionWriteSet:
    slice_: SessionSlice
    records: dict[FnSite, FnRecord]
    installable: dict[FnSite, str]     # admitted site -> "Table.field"


def session_write_set(
    slice_: SessionSlice,
    records: Mapping[FnSite, FnRecord],
    tree_records: Sequence[FnRecord],
    type_records: Iterable[TypeRecord],
    read_text: Callable[[str], str | None],
) -> SessionWriteSet:
    """The slice's resolved records plus every slice file's table
    complement, as records, as units of the returned slice, and as the
    installable marks the turn-0 header renders. `read_text(file)` is the
    live file text (None when unreadable: that file admits nothing)."""
    out = dict(records)
    marks: dict[FnSite, str] = {}
    types = list(type_records)
    writable = frozenset(locator_of(u) for u in out)
    for f in dict.fromkeys(u.file for u in out):
        text = read_text(f)
        if text is None:
            continue
        for inst in table_complement(
                tree_records, types, writable, f,
                reference_counts=fn_reference_counts(text)):
            for r in tree_records:
                if r.file == f and r.fn_name == inst.fn_name and not r.container:
                    site = record_site(r)
                    out[site] = r
                    marks[site] = f"{inst.type_name}.{inst.field}"
                    break
    units = tuple(sorted(out, key=lambda u: (u.file, u.fn_name, u.container)))
    return SessionWriteSet(replace(slice_, units=units), out, marks)
