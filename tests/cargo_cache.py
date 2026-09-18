"""L2 cargo memoization.

Replay reproduces byte-identical source trees (the cassette feeds identical model
output → identical fills), so cargo's verdict on a given tree is reproducible and
cacheable. Keying on `(tree_hash, cmd)` turns a replayed run's compile/test cycles
into dict lookups → a full replay is near-pure-CPU.

Two cargo seams shell out and so must both be memoised for a FULL-engine replay:
  * `_run_cargo`            → `(rc, out)`                — plain build/test;
  * `_run_cargo_check_diag` → `(rc, rendered, [Diagnostic])` — the structured
    `--message-format=json` check the per-fn fill loop drives its frontier from.
(The syn extractor is deterministic — same source → same records — so it needs no
memo; it runs live in replay or falls back when the binary is absent.)

`tree_hash` digests every `.rs` under `src/` + `tests/` (path + bytes), so any fill
that changes the tree changes the key → a cache miss is a real "this tree is new"
signal, never a stale hit. Record mode populates the cache from a real runner;
replay mode serves it.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pipeline
from pipeline_core.domain.models import Diagnostic

_TREE_DIRS = ("src", "tests")

RunKey = tuple[str, str]
RunVal = tuple[int, str]
CheckVal = tuple[int, str, list[Diagnostic]]


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
    """`(tree_hash, cmd) → verdict` memo for BOTH cargo seams. `wrap`/`wrap_check`
    return `_run_cargo`- / `_run_cargo_check_diag`-shaped callables that serve cached
    verdicts and (in record mode) fill from the real runner."""

    def __init__(self, cache: dict[RunKey, RunVal] | None = None,
                 *, record: bool = True,
                 check_cache: dict[RunKey, CheckVal] | None = None):
        self.cache: dict[RunKey, RunVal] = dict(cache or {})
        self.check_cache: dict[RunKey, CheckVal] = dict(check_cache or {})
        self.record = record
        self.hits = 0
        self.misses = 0

    def wrap(self, real: Callable[..., RunVal]) -> Callable[..., RunVal]:
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

    def wrap_check(self, real: Callable[..., CheckVal]) -> Callable[..., CheckVal]:
        def _memoized(cmd, cwd, *a, **k):
            key = (tree_hash(cwd), cmd)
            if key in self.check_cache:
                self.hits += 1
                return self.check_cache[key]
            self.misses += 1
            if not self.record:
                raise KeyError(f"cargo check-diag cache miss (replay): {key[0][:12]} {cmd!r}")
            rc, rendered, diags = real(cmd, cwd, *a, **k)
            self.check_cache[key] = (rc, rendered, list(diags))
            return rc, rendered, diags
        return _memoized


def install_cargo_cache(monkeypatch, cache: CargoCache) -> CargoCache:
    """Route BOTH cargo operations through `cache` (wrapping the live runner)
    at the App port, so a full reconstruction replays offline."""
    import dataclasses as _dc
    real = pipeline._APP.cargo_runner
    run_memo = cache.wrap(real.run)
    check_memo = cache.wrap_check(real.check_diag)

    class _CachedRunner:
        def run(self, cmd, cwd, env, timeout=180):
            return run_memo(cmd, cwd, env, timeout)

        def check_diag(self, cmd, cwd, env, timeout=180):
            return check_memo(cmd, cwd, env, timeout)

    monkeypatch.setattr(pipeline, "_APP",
                        _dc.replace(pipeline._APP, cargo_runner=_CachedRunner()))
    return cache


def save_cargo_cache(cache: CargoCache, path: str | Path) -> None:
    """Persist both memos as JSON (auditable/diffable rows). Diagnostics serialise
    via `Diagnostic.to_dict`; tuple keys become leading row columns."""
    payload: dict[str, Any] = {
        "run": [[h, cmd, rc, out] for (h, cmd), (rc, out) in cache.cache.items()],
        "check": [[h, cmd, rc, rendered, [d.to_dict() for d in diags]]
                  for (h, cmd), (rc, rendered, diags) in cache.check_cache.items()],
    }
    Path(path).write_text(json.dumps(payload), encoding="utf-8")


def load_cargo_cache(path: str | Path) -> CargoCache:
    """Inverse of :func:`save_cargo_cache` — a replay-mode `CargoCache` with both
    memos restored. (Back-compat: a bare list payload is read as run-only rows.)"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, list):                      # legacy run-only format
        run = {(h, cmd): (rc, out) for h, cmd, rc, out in payload}
        return CargoCache(run, record=False)
    run = {(h, cmd): (rc, out) for h, cmd, rc, out in payload.get("run", [])}
    check = {(h, cmd): (rc, rendered, [Diagnostic.from_dict(d) for d in diags])
             for h, cmd, rc, rendered, diags in payload.get("check", [])}
    return CargoCache(run, record=False, check_cache=check)
