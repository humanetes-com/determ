"""What a reverted write over some units measured about the tests it broke,
kept on each of those units' records so every later route over them reads it.

A write that turned passing tests red is reverted, and the route that made it
keeps the measurement under its own key (`*pair*`, `*session*`, a unit key).
The next route over the same units read only its own key. Run
`2355041-eae39441` (2026-10-27): fill 7's pair over a dispatcher and its
formatter was reverted for reddening five tests; fill 8's session over the
same two units reddened the same five, and its turn-0 prompt named none of
them.

The stamp is a measurement, not a diagnosis: the tests, what each printed,
and which write it was. It lives under its own key (:data:`SIBLING_REDDENED`)
and not in the record's `regressed` list, which `retro_gate.regression_scoped`
reads to withhold a unit's own diagnosis; the stamp must not change that
decision. A later reverted write over the unit replaces it.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

#: The record key of the stamp.
SIBLING_REDDENED = "sibling_reddened"


def stamped(record: Mapping[str, Any] | None, *, tests: Iterable[str],
            outputs: Mapping[str, str], by: str) -> dict[str, Any]:
    """``record`` with the stamp of one reverted write (a copy)."""
    names = sorted({str(t) for t in tests})
    out = dict(record or {})
    out[SIBLING_REDDENED] = {
        "tests": names,
        "output": {str(k): str(v) for k, v in outputs.items()
                   if str(k) in names or str(k).rsplit("::", 1)[-1]
                   in {n.rsplit("::", 1)[-1] for n in names}},
        "by": str(by),
    }
    return out


def union_of(records: Iterable[Mapping[str, Any] | None], *,
             skip: Iterable[str] = ()) -> tuple[tuple[str, ...], dict[str, str], tuple[str, ...]]:
    """(tests, what each printed, the writes) stamped on ``records``, in
    first-seen order; tests in ``skip`` (bare or full names) are left out,
    because the caller already renders them."""
    skipped = {str(s).rsplit("::", 1)[-1] for s in skip}
    tests: list[str] = []
    outputs: dict[str, str] = {}
    writers: list[str] = []
    for rec in records:
        stamp = (rec or {}).get(SIBLING_REDDENED)
        if not isinstance(stamp, Mapping):
            continue
        for t in stamp.get("tests") or ():
            t = str(t)
            if t.rsplit("::", 1)[-1] in skipped or t in tests:
                continue
            tests.append(t)
        for k, v in dict(stamp.get("output") or {}).items():
            outputs.setdefault(str(k), str(v))
        by = str(stamp.get("by") or "")
        if by and by not in writers:
            writers.append(by)
    return tuple(tests), outputs, tuple(writers)
