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
    failed" sentinel the backends return.

    ``functions`` is the function-execution half of the same run:
    ``(crate-relative file, first line of the body) -> execution count``, max
    over instantiations and binaries (`domain.fn_execution`). ``None`` means
    the backend reported no function records, which is NOT the same as every
    function unexecuted — the llvm-cov lcov carries them, tarpaulin's JSON and
    grcov's coveralls output do not. Augmentation stopped on the line fields
    alone until 2026-09-17, and a function no test entered was
    indistinguishable from a missed branch (run `301146-2d8a5f06`)."""

    pct: float
    files: list[PerFileCoverage]
    covered: int
    coverable: int
    functions: dict[tuple[str, int], int] | None = None

    def file_lines(self, project_dir: str | os.PathLike[str]) -> dict[str, tuple[int, int]]:
        """``{path relative to project_dir: (covered, coverable)}`` for every
        file of the measurement, with ``/`` separators — the key task0c names
        a file by and the monitor's files overview shows."""
        root = os.fspath(project_dir)
        return {os.path.relpath(f.abs_path, root).replace(os.sep, "/"): (f.covered, f.coverable)
                for f in self.files}

    def file_row(self, project_dir: str | os.PathLike[str],
                 rel_path: str) -> tuple[int, int] | None:
        """One file's ``(covered, coverable)``, or None when the measurement
        does not hold it (a failed run holds no file at all)."""
        key = os.path.normpath(rel_path).replace(os.sep, "/")
        return self.file_lines(project_dir).get(key)


def coverage_pass_outcome(*, actionable_lines: int, had_model_call: bool) -> str:
    """Why a task0c coverage pass ended, from what it measured.

    - ``"ceiling"`` — no uncovered line survived filtering, so every line that
      remains is inside an excluded cfg block, an LLVM artifact, inside a
      function proved unreachable from tests, or inside a function whose
      targeted-attempt budget is spent, and no prompt will be aimed at it. This
      is the ONLY outcome that may mark a cluster augmented without reaching
      the target.
    - ``"no_model_call"`` — reachable lines remain and the pass made no model
      call at all. A generation or routing defect; never a ceiling.
    - ``"progressed"`` — reachable lines remain and the pass worked on them.

    The distinction is the point. The ceiling used to be inferred from
    ``not _pass_had_work``, which was set only on a committed block. On
    2026-08-27, run `2881435-34981ab0` logged 63 `llm.request` events and
    extracted nothing (fenced ``#[test]`` replies), so the flag stayed false and both
    passes over anyhow's `stable` cluster reported a ceiling — against their
    own post-filter counts, 119 actionable uncovered lines across 8 files
    (4 backtrace.rs, 3 ensure.rs, 4 lib.rs, 9 ptr.rs, 8 fmt.rs, 14 wrapper.rs,
    23 context.rs, 54 error.rs). Two such passes are
    `ModuleAugmentState.CEILING_CONFIRM`, so the cluster was accepted at 83.9%
    against a ~98.1% ceiling estimate for that same cluster.
    """
    if actionable_lines <= 0:
        return "ceiling"
    return "progressed" if had_model_call else "no_model_call"
