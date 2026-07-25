"""Field glossary for the monitor TUI: what each terse stat/label means.

Complements legend.py (which explains pipeline *phases*) — this explains the
individual fields/symbols printed inside the Pipeline/Generation panels
(e.g. `in-flight`, `notes=`, `hot fn`, `frontier:`). Content lives here (pure,
testable) so monitor.py only wires the [f] key, the inline hint strip, and
the box rendering.
"""

from __future__ import annotations

import textwrap

__all__ = ["FIELDS", "CORE_TAGS", "field_glossary_lines", "field_glossary_window", "hint_lines"]

# (label as it appears on screen, what it means) — full reference, [f] view.
FIELDS: list[tuple[str, str]] = [
    ("cov=", "Aggregate grcov coverage % for the crate; parenthesised (→ N%) is a live projection from quick-cov updates newer than the committed value."),
    ("age=", "Seconds since the pipeline log's last line — high age while RUNNING is the stall signal."),
    ("cluster", "Level-0 loop: which cfg/feature cluster (stable, nightly, …) is active. ✓ done  ▶ active  · pending; bar = clusters completed."),
    ("attempt", "Level-1 loop: retry attempt count within the current cluster (bar = attempt/max_attempt)."),
    ("file", "Level-2 loop: file being processed this attempt (bar = files done this pass/total)."),
    ("retry", "Level-3 loop: inner per-file retry count — row only appears while a retry is in flight."),
    ("reconstruct", "Phase C (task2a) header: active cluster, the LIVE outer loop's own counter (iter=N under the TDD driver, pass=n/N under the per-fn driver), seconds since the last phase_c event."),
    ("clusters", "Per-cluster status marks across the whole crate: ✓ done  ▶ active  · not started yet."),
    ("tdd", "Per-cluster TDD trajectory: pass=/fail= test counts, ↑/↓ change vs previous iteration, iter=iteration count, stall=iterations with no forward progress, trailing arrow chain = pass-count history."),
    ("stall=", "Consecutive TDD iterations with no improvement in this cluster's passing-test count — a rising stall is the stuck-loop signal."),
    ("err", "Most recent rustc error code the fill loop hit, its plain-English name, and the top 4 error codes by frequency this run."),
    ("progress", "Cumulative bar: fraction of the cluster's fns filled (not stub) out of its total."),
    ("frontier:", "A/B on the progress row = fill attempts made this pass (A) out of stubs queued for this pass (B)."),
    ("fills", "Outcome counts this run: ✓first = passed on the first attempt, ↻recovered = passed after a fix-loop retry, ✗failed = exhausted retries and reverted to stub, ⊘skip = not attempted."),
    ("in-flight", "Fns/impls the LLM is generating right now, up to PIPELINE_PARALLELISM at once, longest-running first. (Ns) = seconds since dispatch. ↻ = behavioral re-fill, ⊞ = whole impl block, ▤ = whole file, no tag = single fn."),
    ("notes=", "Count of 'Model Notes — Confusions to Avoid' entries the model has written to functional_requirements_{crate}.md this run — its own anti-hallucination notes, re-fed into later prompts."),
    ("hot fn", "The single fn that has burned the most fix-loop attempts so far this run — the most likely candidate for GENUINELY_HARD / revert."),
    ("recent", "Last few completed fills: fn:body-char-count×attempts-used."),
    ("test", "Outcome of the last cargo test run for this cluster: PASSED, or rc=<code> failed=<n>."),
    ("audit:", "Flow self-audit verdicts (pipeline --audit), one icon per check: ✓ pass  ✗ fail  ! warn  · skip; first FAIL's evidence is printed on the line below."),
    ("docs", "docs.richness event: fr=<chars> functional_requirements size, notes=<n> model-notes count, rules=<n> genesis.md rule count, pass=<prev>→<cur> test-pass trend, ▲ = improved vs = unchanged."),
    ("compile overview", "Per-file compile-error snapshot during the fix phase: clean/total files compiling, a red block bar sized by error count per file, optional trailing tests N/M."),
    ("files overview", "Per-file coverage snapshot during task0c augmentation: bar = coverage % per file, trailing history, ✓ = file at/above the coverage target."),
    ("vLLM", "Local model backend health: ✓ healthy, or ◑ loading (tail of the vLLM docker logs shown while it loads)."),
    ("≈ tokens/s", "Estimated throughput, computed from GPU memory bandwidth ÷ loaded model size — not a measured rate."),
]

# Subset shown in the always-on inline hint strip: fields with no self-evident
# meaning from the label alone (unlike cluster/attempt/file/test/docs, which
# read fine cold). Everything is still covered in FIELDS for the [f] view.
CORE_TAGS: set[str] = {
    "in-flight", "notes=", "hot fn", "frontier:", "stall=", "fills", "err", "≈ tokens/s",
}


def field_glossary_lines(width: int) -> list[str]:
    """Wrapped `label  description` rows fitting `width` visible columns."""
    tag_w = max(len(t) for t, _ in FIELDS) + 2
    out: list[str] = []
    body_w = max(20, width - tag_w)
    for tag, desc in FIELDS:
        wrapped = textwrap.wrap(desc, body_w) or [""]
        out.append(f"{tag.ljust(tag_w)}{wrapped[0]}")
        out.extend(" " * tag_w + cont for cont in wrapped[1:])
    return out


def field_glossary_window(width: int, inner: int, scroll_from_bottom: int) -> list[str]:
    """The visible slice of the field glossary for a viewport of `inner` rows,
    scrolled `scroll_from_bottom` rows up from the end (0 = bottom)."""
    rows = field_glossary_lines(width)
    off = min(scroll_from_bottom, max(0, len(rows) - inner))
    top = max(0, len(rows) - inner - off)
    return rows[top:top + inner]


def hint_lines(plain_text: str, width: int, max_entries: int = 6) -> list[str]:
    """Compact `label — description` strip for CORE_TAGS labels present in `plain_text`.

    Context-sensitive tooltip equivalent: only surfaces fields actually on
    screen this frame, so the strip changes as the run moves through phases
    instead of repeating a fixed reference block.
    """
    matched = [(t, d) for t, d in FIELDS if t in CORE_TAGS and t in plain_text]
    if not matched:
        return []
    shown = matched[:max_entries]
    out = []
    for t, d in shown:
        line = f"{t} — {d}"
        out.append(line if len(line) <= width else line[:max(0, width - 1)] + "…")
    if len(matched) > max_entries:
        out.append(f"… +{len(matched) - max_entries} more — [f] full field glossary")
    return out
