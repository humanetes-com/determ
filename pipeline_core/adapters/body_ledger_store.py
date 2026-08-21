"""Where the proven-body ledger lives: append-only, outside the workspace.

OUTSIDE THE WORKSPACE, and that is the whole storage argument. Every
workspace-level destruction can reach inside it: `--restart-from-augmented` is
`git reset --hard <task0c tag>`, the tag predates every fill, and the reset
DELETES tracked files — 5,137 of them on 2026-08-08, 5,067 of those
`.skeleton-filled`. `.phase_c_logs` is tracked (`git ls-files` in `tmp/anyhow`
lists `.phase_c_logs/retrospectives.json` among its 8,703 files), so a ledger
next to the retro store would be deleted by exactly the event it exists to
survive. It goes in `logs/body_ledger/<project>/`, which this repo's
`.gitignore` ignores (`logs/`, line 9).

The flip side, stated here rather than discovered later: the ledger is
untracked and machine-local. It survives every workspace destruction and it has
NO object store behind it — a `git clean -xfd` in this repo erases it. It is a
cache of provenance, never the only copy of anything: every body it holds also
exists in a tree, an archive, or a prompt log.

APPEND-ONLY, because a store that rewrites entries can lose the record of a
body that was proven and later refuted, and that is the one thing a reader must
be able to tell apart from a body that was never tried. Collapsing to "the
claim that stands" happens at read time, in `domain/body_ledger.latest_by_unit`,
where the rule is visible.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from contextlib import suppress
from pathlib import Path

from pipeline_core.domain.body_ledger import LedgerEntry, LedgerKey

_ROOT = Path("logs") / "body_ledger"
_INDEX = "index.jsonl"
_BODIES = "bodies"


def default_root() -> Path:
    """The repo root, derived from this file rather than from the cwd.

    `logs/` sits beside `pipeline_core/`, and a run is launched from the repo
    root but a test is not — deriving it here keeps the ledger in one place for
    both, and keeps it OUT of the workspace, which is the whole storage
    argument above."""
    return Path(__file__).resolve().parents[2]


def ledger_dir(repo_root: Path, project: str) -> Path:
    return Path(repo_root) / _ROOT / project


def index_path(repo_root: Path, project: str) -> Path:
    return ledger_dir(repo_root, project) / _INDEX


def body_path(repo_root: Path, project: str, sha: str) -> Path:
    return ledger_dir(repo_root, project) / _BODIES / f"{sha}.json"


def append_entries(repo_root: Path, project: str,
                   entries: Iterable[LedgerEntry]) -> int:
    """Append rows and their body blobs. Returns how many rows were written.

    The blob is a JSON list of the RAW occurrence texts, not a `.rs` file: a
    unit is an ordered tuple of occurrences (`src/context.rs::source` is two
    impls) and a single source file cannot hold that tuple unambiguously. The
    row carries a copy of the same tuple, so a lost blob directory costs
    nothing; the blob exists so the index stays scannable when bodies are long.
    """
    rows = list(entries)
    if not rows:
        return 0
    d = ledger_dir(repo_root, project)
    try:
        (d / _BODIES).mkdir(parents=True, exist_ok=True)
    except OSError:
        return 0
    written = 0
    with (d / _INDEX).open("a", encoding="utf-8") as fh:
        for e in rows:
            blob = body_path(repo_root, project, e.body_sha)
            if not blob.exists():
                # A blob that cannot be written costs nothing: the row carries
                # the same tuple, and the blob exists only to keep the index
                # scannable when bodies are long.
                with suppress(OSError):
                    blob.write_text(json.dumps(list(e.bodies)),
                                    encoding="utf-8")
            fh.write(json.dumps(e.to_dict(), sort_keys=True) + "\n")
            written += 1
    return written


def load_entries(repo_root: Path, project: str, *,
                 cluster: str | None = None) -> list[LedgerEntry]:
    """Every parseable row, in file order. A malformed row is skipped, not
    raised on: this runs at cluster entry over a file a killed process may have
    left with a half-written last line."""
    return list(_iter_entries(index_path(repo_root, project), cluster))


def _iter_entries(path: Path, cluster: str | None) -> Iterator[LedgerEntry]:
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
            entry = LedgerEntry.from_dict(raw)
            if entry is None:
                continue
            if cluster is not None and entry.key.cluster != cluster:
                continue
            yield entry


def read_body(repo_root: Path, project: str, sha: str) -> tuple[str, ...]:
    try:
        raw = json.loads(body_path(repo_root, project, sha)
                         .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    return tuple(str(b) for b in raw) if isinstance(raw, list) else ()


def store_size(repo_root: Path, project: str) -> tuple[int, int]:
    """`(rows, bytes)` — printed at cluster entry so growth is visible before
    it is a problem. The index is append-only and nothing prunes it; the whole
    non-generated Rust source set is well under a megabyte, so the number to
    watch is rows, not bytes."""
    d = ledger_dir(repo_root, project)
    idx = d / _INDEX
    rows = 0
    total = 0
    try:
        with idx.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.strip():
                    rows += 1
        total = idx.stat().st_size
    except OSError:
        return 0, 0
    try:
        for p in (d / _BODIES).glob("*.json"):
            total += p.stat().st_size
    except OSError:
        pass
    return rows, total


def unit_key(project: str, cluster: str, file: str, fn_name: str) -> LedgerKey:
    return LedgerKey(project=project, cluster=cluster, file=file,
                     fn_name=fn_name)
