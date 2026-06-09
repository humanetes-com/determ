"""L2 cargo memoization — CODE_ARCHITECTURE.md R10.4.

Replay reproduces byte-identical source trees (the cassette feeds identical model
output → identical fills), so cargo's verdict on a given tree is reproducible and
cacheable. Keying on `(tree_hash, cmd)` turns a replayed run's compile/test cycles
into dict lookups → a full replay is near-pure-CPU.

`tree_hash` digests every `.rs` under `src/` + `tests/` (path + bytes), so any fill
that changes the tree changes the key → a cache miss is a real "this tree is new"
signal, never a stale hit. Record mode populates the cache from a real runner;
replay mode serves it.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

import pipeline

_TREE_DIRS = ("src", "tests")


def tree_hash(project_dir: str | Path) -> str:
    """Stable digest of the crate's source tree (`src/` + `tests/` *.rs)."""
    root = Path(project_dir)
    h = hashlib.sha256()
    files: list[Path] = []
    for sub in _TREE_DIRS:
        d = root / sub
        if d.is_dir():
            files.extend(d.rglob("*.rs"))
    for p in sorted(files, key=lambda x: str(x.relative_to(root))):
        h.update(str(p.relative_to(root)).encode("utf-8"))
        h.update(b"\0")
        try:
            h.update(p.read_bytes())
        except Exception:
            h.update(b"<unreadable>")
        h.update(b"\0")
    return h.hexdigest()


class CargoCache:
    """`(tree_hash, cmd) → (rc, out)` memo. `wrap(real)` returns a `_run_cargo`-shaped
    callable that serves cached verdicts and (in record mode) fills from `real`."""

    def __init__(self, cache: dict[tuple[str, str], tuple[int, str]] | None = None,
                 *, record: bool = True):
        self.cache: dict[tuple[str, str], tuple[int, str]] = dict(cache or {})
        self.record = record
        self.hits = 0
        self.misses = 0

    def wrap(self, real: Callable[..., tuple[int, str]]) -> Callable[..., tuple[int, str]]:
        def _memoized(cmd, project_dir, *a, **k):
            key = (tree_hash(project_dir), cmd)
            if key in self.cache:
                self.hits += 1
                return self.cache[key]
            self.misses += 1
            if not self.record:
                raise KeyError(f"cargo cache miss (replay): {key[0][:12]} {cmd!r}")
            rc, out = real(cmd, project_dir, *a, **k)
            self.cache[key] = (rc, out)
            return rc, out
        return _memoized


def install_cargo_cache(monkeypatch, cache: CargoCache) -> CargoCache:
    """Patch `pipeline._run_cargo` to go through `cache` (wrapping the live runner)."""
    real = pipeline._run_cargo
    monkeypatch.setattr(pipeline, "_run_cargo", cache.wrap(real))
    return cache
