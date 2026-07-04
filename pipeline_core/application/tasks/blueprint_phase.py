"""Blueprint phase use-case (july_plan P3): generate + validate + persist the
model-authored design document, once per crate, after TASK 1 and before any
fill. Idempotent — an existing non-empty blueprint is kept (its lifecycle is
the FR's: wiping the FR wipes it, `--clean` regenerates both).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.domain.blueprint import (
    Blueprint,
    missing_topics,
    parse_blueprint,
)
from pipeline_core.prompts.blueprint import build_blueprint_prompt


@dataclass(frozen=True, slots=True)
class BlueprintDeps:
    ask: Callable[..., str]
    log: Callable[[str], None]
    emit: Callable[..., None]
    model: str | None    # None → the ask client's default model
    # Dispatch-family enumeration over the baked skeleton (syn extractor;
    # None/failed → file-stem topics only). Families are cross-impl schemes —
    # exactly what the blueprint must pin down (FR-027).
    dispatch_families: Callable[..., Any] | None = None


def ensure_blueprint(
    path: Path,
    *,
    module: str,
    skeleton_sigs: str,
    tests_block: str,
    functional_req: str,
    topics: tuple[str, ...],
    deps: BlueprintDeps,
    skeleton_dir: Path | None = None,
    max_attempts: int = 2,
) -> Blueprint:
    """Return the crate blueprint, generating it if absent. Validation:
    every topic must have a `## ` section; one retry names the gaps. A still-
    incomplete document is kept (partial design beats none) with the gaps
    logged."""
    if path.exists():
        existing = parse_blueprint(path.read_text(encoding="utf-8",
                                                  errors="replace"))
        if not existing.empty:
            deps.log(f"  [blueprint] {path.name} exists "
                     f"({len(existing.sections)} section(s)) — keeping.")
            return existing

    # Dispatch families are cross-impl schemes (FR-027): each one is a
    # REQUIRED topic, keyed by its dispatch method name.
    if deps.dispatch_families is not None and skeleton_dir is not None:
        try:
            fam_topics = [fam.method
                          for fam in (deps.dispatch_families(skeleton_dir)
                                      or []) if fam.method]
            topics = tuple(dict.fromkeys([*topics, *fam_topics]))
        except Exception as e:
            deps.log(f"  [blueprint] family enumeration failed ({e}) — "
                     "file topics only.")

    missing: tuple[str, ...] = ()
    bp = Blueprint()
    text = ""
    for attempt in range(1, max_attempts + 1):
        prompt = build_blueprint_prompt(
            module=module, skeleton_sigs=skeleton_sigs,
            tests_block=tests_block, functional_req=functional_req,
            topics=topics, missing=missing)
        text = deps.ask(prompt, model=deps.model)
        bp = parse_blueprint(text)
        missing = missing_topics(bp, frozenset(topics))
        deps.log(f"  [blueprint] attempt {attempt}: "
                 f"{len(bp.sections)} section(s), "
                 f"{len(missing)} missing topic(s)"
                 f"{' — ' + ', '.join(missing[:5]) if missing else ''}.")
        if not missing and not bp.empty:
            break
    if bp.empty:
        deps.log("  [blueprint] model produced no sections — proceeding "
                 "without a blueprint.")
        deps.emit("blueprint.done", data={"module": module, "sections": 0,
                                          "missing": len(topics)})
        return bp
    path.write_text(text, encoding="utf-8")
    deps.emit("blueprint.done", data={
        "module": module, "sections": len(bp.sections),
        "missing": len(missing), "chars": len(text)})
    deps.log(f"  [blueprint] wrote {path.name} "
             f"({len(bp.sections)} section(s), {len(text)} chars).")
    return bp
