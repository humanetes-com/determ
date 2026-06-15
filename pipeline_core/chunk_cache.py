"""Content-addressed cache for TASK-1 per-chunk requirement extractions.

Pure stdlib (hashlib + filesystem) — a foundational util module (sibling of
`io`/`log`/`retry`, not a layer), so the application layer imports it directly.
Relocated from pipeline.py to keep the god-file shrinking; the `ChunkCache` port
in `ports.synthesizer` binds these at the composition root for test fakes.
"""
from __future__ import annotations

import hashlib
from pathlib import Path


def chunk_hash(chunk_text: str) -> str:
    """SHA-256 of the formatted chunk content used as cache key."""
    return hashlib.sha256(chunk_text.encode()).hexdigest()[:16]


def load_cached_chunk(cache_dir: Path, chunk_hash: str) -> str | None:
    """Return cached partial text if it exists, else None."""
    p = cache_dir / f"{chunk_hash}.txt"
    return p.read_text() if p.exists() else None


def save_cached_chunk(cache_dir: Path, chunk_hash: str, content: str) -> None:
    """Persist a chunk extraction result to the cache."""
    cache_dir.mkdir(exist_ok=True)
    (cache_dir / f"{chunk_hash}.txt").write_text(content)
