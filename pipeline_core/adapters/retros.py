"""Per-fn retrospectives store — the engine's compounding cross-iteration
learning record (`.phase_c_logs/retrospectives.json`), keyed by
(file, fn, canonical container). Relocated verbatim from pipeline.py
(P2 slice 2); the `_`-prefixes dropped on promotion (R6.5).
"""

from __future__ import annotations

import json
import os
from contextlib import suppress
from pathlib import Path
from typing import Any

from pipeline_core.domain.identity import RetroKey
from pipeline_core.domain.rust_idents import canon_container
from pipeline_core.domain.workspace_layout import PHASE_C_LOGS_DIR
from pipeline_core.log import emit, log

__all__ = [
    "canon_container", "is_fn_fully_exhausted", "load_retrospectives",
    "reset_exhaustion_flags", "retro_key", "retrospectives_path",
    "save_retrospectives",
]


def retrospectives_path(project_dir: Path) -> Path:
    """Path to the per-fn retrospectives JSON. Keyed by (file, fn, container)
    so same-named fns in different impls track independently. Survives
    across module iterations — that's the point: learning compounds."""
    d = project_dir / PHASE_C_LOGS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d / "retrospectives.json"


def load_retrospectives(project_dir: Path) -> dict[str, Any]:
    """Load the on-disk retrospective record. Returns {} on first run or
    when the file is malformed (caller treats missing entries as 'no
    prior retrospective'). Keys written before container canonicalisation
    are re-keyed on load so old records keep matching."""
    p = retrospectives_path(project_dir)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        rekeyed: dict[str, Any] = {}
        for key, rec in data.items():
            parts = key.split("::", 2)
            if len(parts) == 3:
                key = retro_key(parts[0], parts[1], parts[2])
            rekeyed[key] = rec
        return rekeyed
    except Exception:
        return {}


def save_retrospectives(project_dir: Path, data: dict[str, Any]) -> None:
    with suppress(Exception):
        retrospectives_path(project_dir).write_text(
            json.dumps(data, indent=2), encoding="utf-8")


# canon_container relocated to domain/rust_idents.py (debt #43 — the per-impl
# body routing needs the same identity); re-exported here so the retro-key
# call sites and the pipeline.py smoke-pin alias stay unchanged.


def retro_key(file_rel: str, fn_name: str, container: str) -> RetroKey:
    """Stable key for the retrospective dict — the ONE mint point of
    :class:`RetroKey`. Container is included so overloaded `fn new` impls
    (Adhoc/Trait/Boxed) track independently; it is canonicalised so different
    renderings of one impl share a key."""
    return RetroKey(f"{file_rel}::{fn_name}::{canon_container(container)}")


def reset_exhaustion_flags(project_dir: Path, reason: str) -> int:
    """Clear `fully_exhausted` flags (keep the lessons — category, guidance,
    history all stay) at a phase boundary where the evidence context changed
    (e.g. the merged tree landed dozens of fills). FR-024's cross-iter skip
    was written against a narrower tree; one fresh, note-informed attempt per
    boundary is exactly D3's re-entry rule applied across phases.

    Capped PER FN via `reset_attempts` (env `RETRO_RESET_MAX_ATTEMPTS`,
    default 2): a fn that has already been reset and immediately
    re-exhausted (no improvement) this many times stops being re-admitted,
    even while OTHER work in the cluster keeps progressing. Without this a
    genuinely-stuck fn pays one full LLM-call-plus-compile-cycle tax on
    EVERY future boundary for the rest of the run, for zero payoff each
    time — live-observed on anyhow (iter 3 re-admitted 16 fns, net
    `improved=False`, right after iter 2's 43-fn batch DID pay off) — the
    unconditional version can't tell "first informed retry" from "the same
    fn getting its Nth futile one." Returns the number of flags cleared."""
    max_attempts = int(os.getenv("RETRO_RESET_MAX_ATTEMPTS", "2"))
    retros = load_retrospectives(project_dir)
    cleared = 0
    capped = 0
    for rec in retros.values():
        if not (isinstance(rec, dict) and rec.get("fully_exhausted")):
            continue
        attempts = int(rec.get("reset_attempts", 0))
        if attempts >= max_attempts:
            capped += 1
            continue
        rec["fully_exhausted"] = False
        rec["reset_attempts"] = attempts + 1
        cleared += 1
    if cleared or capped:
        save_retrospectives(project_dir, retros)
        log(f"  [phase-c] reset {cleared} fully_exhausted flag(s) "
            f"({capped} at the {max_attempts}-attempt cap, left exhausted) — {reason}")
        emit("phase_c.exhaustion.reset",
             data={"cleared": cleared, "capped": capped, "reason": reason})
    return cleared



def is_fn_fully_exhausted(
    retros: dict[str, Any], file_rel: str, fn_name: str, container: str,
) -> bool:
    """FR-024 cross-iter skip: progress-gated. A fn is permanently skipped
    only when (a) the LLM classified it GENUINELY_HARD, or (b) a
    retrospective fired AND the next-best error count did NOT improve over
    the prior best (`fully_exhausted` flag set at write time). No hard
    retrospective-count cap — as long as each retro reduces errors, the
    loop is free to continue. `retrospective_count` is kept only for
    telemetry."""
    key = retro_key(file_rel, fn_name, container)
    rec = retros.get(key)
    if not rec:
        return False
    # A downstream_only record means every error was in ANOTHER file — the
    # fn itself is probably fine and was parked for a break it didn't cause.
    # Ignore the flag (records written before the writer stopped exhausting
    # on downstream_only stay on disk; reinterpreting heals them without a
    # state migration).
    if rec.get("signal") == "downstream_only":
        return False
    return bool(rec.get("fully_exhausted"))
