"""Per-fn retrospectives store — the engine's compounding cross-iteration
learning record (`.phase_c_logs/retrospectives.json`), keyed by
(file, fn, canonical container). Relocated verbatim from pipeline.py
(P2 slice 2); the `_`-prefixes dropped on promotion (R6.5).
"""

from __future__ import annotations

import json
from contextlib import suppress
from pathlib import Path
from typing import Any

from pipeline_core.domain.workspace_layout import PHASE_C_LOGS_DIR
from pipeline_core.log import emit, log


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


def canon_container(container: str) -> str:
    """Canonical container form for keying. The extractor's fn records and
    the impl-block manifest render the same impl differently (token-spaced
    `impl Context < T , E > for Result < T , E >` vs pretty-printed
    `impl<T, E> Context<T, E> for Result<T, E>\\nwhere\\n    E: ...`), which
    used to split one impl across two retrospective keys. Reduce both to
    `<Trait>for<SelfTy>`: drop a leading impl-generics list, drop the
    where-clause, drop all whitespace."""
    s = " ".join(container.split())
    if s.startswith("impl"):
        s = s[4:].lstrip()
        if s.startswith("<"):
            depth = 0
            for i, ch in enumerate(s):
                if ch == "<":
                    depth += 1
                elif ch == ">":
                    depth -= 1
                    if depth == 0:
                        s = s[i + 1:]
                        break
    if s.endswith(" where"):
        s = s[: -len(" where")]
    s = s.split(" where ")[0]
    return "".join(s.split())


def retro_key(file_rel: str, fn_name: str, container: str) -> str:
    """Stable key for the retrospective dict. Container is included so
    overloaded `fn new` impls (Adhoc/Trait/Boxed) track independently;
    it is canonicalised so different renderings of one impl share a key."""
    return f"{file_rel}::{fn_name}::{canon_container(container)}"


def reset_exhaustion_flags(project_dir: Path, reason: str) -> int:
    """Clear every `fully_exhausted` flag (keep the lessons — category,
    guidance, history all stay) at a phase boundary where the evidence
    context changed (e.g. the merged tree landed dozens of fills). FR-024's
    cross-iter skip was written against a narrower tree; one fresh, note-
    informed attempt per boundary is exactly D3's re-entry rule applied
    across phases. Returns the number of flags cleared."""
    retros = load_retrospectives(project_dir)
    cleared = 0
    for rec in retros.values():
        if isinstance(rec, dict) and rec.get("fully_exhausted"):
            rec["fully_exhausted"] = False
            cleared += 1
    if cleared:
        save_retrospectives(project_dir, retros)
        log(f"  [phase-c] reset {cleared} fully_exhausted flag(s) — {reason}")
        emit("phase_c.exhaustion.reset", data={"cleared": cleared, "reason": reason})
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
    return bool(rec.get("fully_exhausted"))
