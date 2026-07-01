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

from typing import Any, NamedTuple


class CoverageMeasurement(NamedTuple):
    """One coverage run's aggregate result.

    ``pct`` is the overall line-coverage percent; ``files`` holds the per-file
    records (tarpaulin-compatible schema); ``covered``/``coverable`` are the
    line totals. An all-zero ``CoverageMeasurement(0.0, [], 0, 0)`` is the
    "measurement unavailable / failed" sentinel the backends return."""

    pct: float
    files: list[dict[str, Any]]
    covered: int
    coverable: int
