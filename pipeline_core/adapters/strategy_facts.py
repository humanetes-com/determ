"""The construction facts of the ORIGINAL crate, and the strategy file they render into (adapter).

`domain.fn_routes` reads them from rustc's MIR and public index; this adapter
owns where they come from and where they go:

- `construction_facts.json` holds one `WorldFacts` per cluster world, taken
  while `src/` is still the original crate. After strip the tree is a
  reconstruction, so a fact re-measured then would describe the wrong crate;
  `facts()` measures only when the caller says the tree is original and the
  stored file is missing or from another generator version, and otherwise
  returns what is stored (a stale file is returned with its staleness logged,
  never re-measured on a stripped tree).
- `coverage_strategy.md` gets the computed part first (`domain.strategy_render`)
  and `coverage_strategy.inputs_hash` the part's hash. `rendered()` is called
  on every load of the strategy text, both skip branches included, so the
  text the t0c, task1 and fill prompts receive carries the computed part even
  when t0b's model call ran months ago.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.application.tasks.fn_criterion import WorldRead
from pipeline_core.domain.fn_routes import (
    ROUTES_VERSION,
    WorldFacts,
    facts_from_json,
    facts_source,
    facts_to_json,
)
from pipeline_core.domain.measurement import tree_content_sha
from pipeline_core.domain.strategy_render import computed_block, computed_hash, render_strategy
from pipeline_core.domain.workspace_layout import (
    CONSTRUCTION_FACTS_NAME,
    COVERAGE_STRATEGY_HASH_NAME,
    COVERAGE_STRATEGY_NAME,
)


@dataclass(frozen=True)
class StrategyFacts:
    """`read_world(project_dir, cluster) -> WorldRead` is the one reading
    t0c also takes (`fn_criterion.read_world_facts`)."""

    read_world: Callable[[Path, Any], WorldRead]
    log: Callable[[str], None]

    def load(self, project_dir: Path) -> tuple[str, tuple[WorldFacts, ...]] | None:
        try:
            text = (project_dir / CONSTRUCTION_FACTS_NAME).read_text(encoding="utf-8")
        except OSError:
            return None
        return facts_from_json(text)

    def _stored_source(self, project_dir: Path) -> str:
        try:
            return facts_source(
                (project_dir / CONSTRUCTION_FACTS_NAME).read_text(encoding="utf-8"))
        except OSError:
            return ""

    def facts(self, project_dir: Path, clusters: Sequence[Any], *,
              may_measure: bool) -> tuple[WorldFacts, ...]:
        """The worlds' facts: stored when current, measured when the tree is
        original and nothing current is stored, else whatever is stored."""
        stored = self.load(project_dir)
        source_now = tree_content_sha(project_dir / "src")
        if (stored is not None and stored[0] == ROUTES_VERSION
                and (not may_measure or self._stored_source(project_dir) == source_now)):
            return stored[1]
        if stored is not None and stored[0] == ROUTES_VERSION and may_measure:
            self.log("  [task0b] construction facts were measured on another source "
                     "(src/ changed under the same generator version) — remeasuring.")
        if not may_measure:
            if stored is not None:
                self.log(f"  [task0b] construction facts are from generator version {stored[0]!r}, "
                         f"not {ROUTES_VERSION!r}, and src/ is no longer the original crate — "
                         "using them as stored.")
                return stored[1]
            self.log("  [task0b] no construction facts stored and src/ is no longer the "
                     "original crate — the strategy file keeps its model text only.")
            return ()
        worlds: list[WorldFacts] = []
        for cluster in clusters:
            read = self.read_world(project_dir, cluster)
            if read.facts is None:
                self.log(f"  [task0b] cluster {getattr(cluster, 'name', '?')}: no construction "
                         f"facts ({read.reason}).")
                continue
            worlds.append(read.facts)
        if worlds:
            (project_dir / CONSTRUCTION_FACTS_NAME).write_text(
                facts_to_json(worlds, source=source_now), encoding="utf-8")
            self.log(f"  [task0b] construction facts measured for {len(worlds)} world(s): "
                     + "; ".join(f"{w.cluster}: {len(w.routes)} routed fn(s), "
                                 f"{len(w.unnameable)} unnameable type(s)" for w in worlds))
        return tuple(worlds)

    def computed_part(self, project_dir: Path) -> str:
        """The computed part as stored, for the discovery prompt ("" without one)."""
        stored = self.load(project_dir)
        return computed_block(stored[1]) if stored is not None else ""

    def rendered(self, project_dir: Path, text: str, worlds: Sequence[WorldFacts]) -> str:
        """`text` with the computed part rendered in. Writes the strategy file
        and its hash when either differs from what is on disk; a match
        leaves both alone."""
        if not worlds:
            return text
        out, dropped = render_strategy(text, worlds)
        want = computed_hash(worlds)
        hash_path = project_dir / COVERAGE_STRATEGY_HASH_NAME
        try:
            have = hash_path.read_text(encoding="utf-8").strip()
        except OSError:
            have = ""
        strategy_path = project_dir / COVERAGE_STRATEGY_NAME
        try:
            on_disk = strategy_path.read_text(encoding="utf-8").strip()
        except OSError:
            on_disk = ""
        if have != want or on_disk != out.strip():
            strategy_path.write_text(out, encoding="utf-8")
            hash_path.write_text(want + "\n", encoding="utf-8")
            self.log(f"  [task0b] coverage_strategy.md: computed part rendered "
                     f"({sum(len(w.unnameable) for w in worlds)} unnameable type line(s) over "
                     f"{len(worlds)} world(s)); {len(dropped)} model line(s) it contradicts dropped"
                     + (": " + " | ".join(d.strip() for d in dropped[:6]) if dropped else "") + ".")
        return out.strip()
