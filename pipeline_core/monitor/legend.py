"""Phase legend for the monitor TUI: one line per pipeline phase, what it does.

Content lives here (pure, testable) so monitor.py only wires the [l] key and
the box rendering. Keep each description to ONE terminal line of intent —
the operator glances at this mid-run to decode a log tag, not to read docs.
"""

from __future__ import annotations

import textwrap

__all__ = ["PHASES", "legend_lines"]

# (tag as it appears in the log, what the phase performs)
PHASES: list[tuple[str, str]] = [
    ("task0a", "Project profile: reads README+tests, writes project_profile.md (what the crate is)."),
    ("task0a-context", "Project context: writes project_context.md — architecture notes injected into prompts."),
    ("task0b", "Discovery: writes coverage_strategy.md — how to build, test and measure this crate."),
    ("cfg-scan", "Cluster identification (static, no LLM): cfg/feature/toolchain directives → build clusters (stable, nightly, …); each downstream phase is scoped per cluster."),
    ("task0c", "Coverage augmentation: per cluster, LLM adds unit tests (gen_tests) until coverage reaches COVERAGE_AUGMENTATION_TARGET; every original + added test is preserved as the reconstruction oracle."),
    ("bake", "Pre-strip artifact capture (the ONLY read of original bodies): skeleton with unimplemented!() stubs, API surface, type/pub-item manifests, cluster manifest, leaf-first fn order."),
    ("strip", "Deletes the original implementation. From here on: tests + baked artifacts only (Hard Rule 11)."),
    ("task1", "Functional requirements: from tests+skeleton writes functional_requirements_{crate}.md, incl. the model's own 'Confusions to Avoid' notes."),
    ("task2a", "Per-cluster reconstruction. A: cargo-check the skeleton. B/C: per-fn / per-impl LLM fill — splice body, cargo gate, regression baseline, retrospectives on failure, skeleton-validate self-heal. D: residual stubs."),
    ("task2b", "Merge: per-cluster filled trees combined into src/ with cfg gating; shared fns deduped."),
    ("task2c", "Merged-tree fill: fills stubs that only exist (or only compile) in the merged tree."),
    ("task3", "Test run: cluster-scoped cargo tests, strict aggregate pass/fail counts (the oracle verdict)."),
    ("task4", "Knowledge + ratchet: analyzes failures into genesis.md rules; best-tree ratchet snapshots the highest-passing tree; loops back into task2 fix passes."),
    ("fix loop / per-fn", "Inner compile-fix loop for one fn: re-prompt with focused rustc errors, prior attempts, model notes; revert on exhaustion."),
    ("fix loop/impl", "Same loop at impl-block granularity: all members of one impl fixed together."),
    ("retro", "Retrospective after a failed fix loop: LLM diagnoses the failure class (GENUINELY_HARD, FROZEN_BOUND_MISMATCH, DOWNSTREAM, …); improving error floor re-enters the loop, downstream-only never exhausts the fn."),
    ("skeleton-validate", "End-of-cluster cargo check: implicated fns reverted to stubs (all same-name occurrences); if nothing is revertible the broken file is restored wholesale from the baked skeleton."),
    ("genesis audit", "Periodic anti-corruption check of genesis.md (dedupes, drops stale rules)."),
]


def legend_lines(width: int) -> list[str]:
    """Wrapped `tag  description` rows fitting `width` visible columns."""
    tag_w = max(len(t) for t, _ in PHASES) + 2
    out: list[str] = []
    body_w = max(20, width - tag_w)
    for tag, desc in PHASES:
        wrapped = textwrap.wrap(desc, body_w) or [""]
        out.append(f"{tag.ljust(tag_w)}{wrapped[0]}")
        out.extend(" " * tag_w + cont for cont in wrapped[1:])
    return out
