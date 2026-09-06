"""Where the route ledger lives: append-only, outside the workspace.

The same storage argument as the body ledger (`body_ledger_store.py`): every
workspace-level destruction (`--restart-from-augmented`, `redo.sh`) can reach
inside `tmp/<project>`, and the fill schedule's evidence must survive exactly
those events — a route that bought nothing on a tree state is still a re-buy
after the tree is restored to that state. `logs/route_ledger/<project>/` is
ignored by this repo's `.gitignore` and never tracked.

Rows are small (no bodies — a row carries the identity of the bodies it was
tried on, `bodies_sha`, not the bodies) so there is no blob directory. The
index is append-only; the fold to "what stands for this identity" happens at
read time in `domain/fill_schedule.schedule`.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

from pipeline_core.domain.fill_schedule import RouteLedgerRow

_ROOT = Path("logs") / "route_ledger"
_INDEX = "index.jsonl"


def default_root() -> Path:
    """The repo root, derived from this file rather than from the cwd."""
    return Path(__file__).resolve().parents[2]


def ledger_dir(repo_root: Path, project: str) -> Path:
    return Path(repo_root) / _ROOT / project


def index_path(repo_root: Path, project: str) -> Path:
    return ledger_dir(repo_root, project) / _INDEX


def append_rows(repo_root: Path, project: str,
                rows: Iterable[RouteLedgerRow]) -> int:
    """Append rows. Returns how many were written; an unwritable directory
    costs nothing but the evidence (the schedule then ranks without it)."""
    items = list(rows)
    if not items:
        return 0
    d = ledger_dir(repo_root, project)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return 0
    written = 0
    try:
        with (d / _INDEX).open("a", encoding="utf-8") as fh:
            for r in items:
                fh.write(r.to_json() + "\n")
                written += 1
    except OSError:
        return written
    return written


def load_rows(repo_root: Path, project: str, *,
              cluster: str | None = None) -> list[RouteLedgerRow]:
    """Every parseable row, in file order. A malformed row is skipped: this
    runs at fill start over a file a killed process may have left with a
    half-written last line."""
    return list(_iter_rows(index_path(repo_root, project), cluster))


def _iter_rows(path: Path, cluster: str | None) -> Iterator[RouteLedgerRow]:
    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except ValueError:
                continue
            if not isinstance(raw, dict):
                continue
            row = RouteLedgerRow.from_dict(raw)
            if row is None:
                continue
            if cluster is not None and row.cluster != cluster:
                continue
            yield row


def store_size(repo_root: Path, project: str) -> tuple[int, int]:
    """`(rows, bytes)` for the cluster-entry line."""
    idx = index_path(repo_root, project)
    rows = 0
    try:
        with idx.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.strip():
                    rows += 1
        return rows, idx.stat().st_size
    except OSError:
        return 0, 0


class RouteLedgerStore:
    """`fill_routes.RouteLedger` over the append-only index under
    `logs/route_ledger/<project>/`."""

    def __init__(self, repo_root: Path | None = None) -> None:
        self._root = repo_root if repo_root is not None else default_root()

    def load(self, project: str, cluster: str) -> list[RouteLedgerRow]:
        return load_rows(self._root, project, cluster=cluster)

    def append(self, project: str, rows: Sequence[RouteLedgerRow]) -> int:
        return append_rows(self._root, project, rows)
