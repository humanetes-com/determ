"""Coverage measurement domain value objects (pure — no I/O).

Names the aggregate a coverage backend returns. Previously an anonymous
``tuple[float, list[dict], int, int]`` threaded through the CoverageProvider
port, its four adapters, and the consumers. :class:`CoverageMeasurement` is a
``NamedTuple`` so it stays a drop-in for the ``(pct, files, covered, coverable)``
tuple every caller already unpacks, while the positions are now named.

The per-file ``files`` entries are still the tarpaulin-compatible dicts
(``{"abs_path", "path", "traces", "covered", "coverable"}``); giving those their
own value object is a separate, wider change (they are read by key at ~40 sites).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, NamedTuple


@dataclass(frozen=True, slots=True)
class PerFileCoverage:
    """One source file's coverage record (tarpaulin-compatible schema).

    ``abs_path`` is the absolute file path; ``path`` its ``os.sep``-split
    components; ``traces`` the raw per-line hit list (``[{line, stats:{Line}}]``,
    kept as dicts — it is the external tarpaulin/grcov sub-schema, read by key);
    ``covered``/``coverable`` are line counts. :meth:`from_dict` is the
    anti-corruption boundary for the loosely-typed dicts tarpaulin and grcov
    emit — tolerant of a missing ``abs_path`` (derived from ``path``, as the old
    consumers did) and missing counts/traces."""

    abs_path: str
    path: list[str]
    traces: list[dict[str, Any]]
    covered: int
    coverable: int

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PerFileCoverage:
        path = d.get("path", [])
        return cls(
            abs_path=d.get("abs_path") or os.sep.join(path),
            path=path,
            traces=d.get("traces", []),
            covered=d.get("covered", 0),
            coverable=d.get("coverable", 0),
        )


class CoverageMeasurement(NamedTuple):
    """One coverage run's aggregate result.

    ``pct`` is the overall line-coverage percent; ``files`` holds the per-file
    records; ``covered``/``coverable`` are the line totals. An all-zero
    ``CoverageMeasurement(0.0, [], 0, 0)`` is the "measurement unavailable /
    failed" sentinel the backends return."""

    pct: float
    files: list[PerFileCoverage]
    covered: int
    coverable: int
