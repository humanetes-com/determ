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
    ("iter", "Inside the CURRENT TDD iteration: pace bar = elapsed vs the median sweep-to-sweep gap this run (caps at full — overrunning is legitimate; a pace reference, not an ETA). fills = completed fill units this iteration (per-fn, impl-block members, whole-file fns; re-fills included), `last N ago` = age of the newest fill — a growing age while RUNNING is the finest-grain stall signal. frontier = fns this iteration works through; fills routinely exceed it (file-first touches non-frontier fns)."),
    ("sweep", "Fill units completed against the frontier batch this sweep opened with (the pipeline log's N/M counter): bar = units/batch, ✓ = landed, ✗ = kept-or-reverted after exhausting fixes. File/impl passes count members, so the count can pass the batch size — the excess is shown as `+K beyond frontier`, never folded into the bar."),
    ("score", "Tests passing out of the oracle the last sweep COUNTED (pass+fail), the signed delta vs the previous sweep, the failing count, and — when the cluster declares more tests than the sweep counted — what sits outside that count: verdicts a crashed binary printed before it aborted (the counters are summed from libtest `test result:` trailers, which an aborted binary never prints) plus tests that never ran here (cfg-ignored, or a static over-count). The crash's OWN lost tests are NOT in that number: they are counted as failing."),
    ("step", "Where inside one TDD iteration the loop is: sweep (cargo test) → grade (ratchet, checkpoint, floor, ledger on that reading) → aim (attribute failing tests to units, crash blame, escalation) → frontier (re-admit, rank, choose this iteration's batch) → fill (dispatch it: file-first, impl blocks, per-fn). ✓ done this iteration  ▶ current  · not reached. The trailing time is time-in-step: minutes in `fill` are normal, minutes in `sweep` or `grade` are a finding."),
    ("fill", "Where inside the `fill` step the loop is. A fill walks three ROUTES, each filling a unit of a different size: file (one prompt designs every body in a file), impl (one prompt per impl block), per-fn (one prompt per body). A unit that fails one route falls through to the next, so the routes are counted SEPARATELY and never summed — summing would count the iteration's hardest units two or three times. ✓ route finished  ▶ running  · not run. `2/4` is units closed of the unit count THAT route published (phase_c.fill.route_plan; phase_c.pass.start for per-fn); `1/1 seen` is a route whose records carry no plan, showing closed-of-opened rather than borrowing a denominator. `batch N` is the frontier units the sweep dispatched — context, not the denominator."),
    ("unit", "The body being written right now, one row per unit in flight. ↻ = behavioral re-fill, ⊞ = impl block, ▤ = whole file, no tag = single fn. `attempt N` counts model attempts that FAILED on this unit (so a unit on its first answer shows none); only the file route has a ceiling to show as `N/3`. `floor` is the best metric the fix loop has reached and `stall` the attempts since it last improved — that PAIR, not the elapsed time, says whether the minutes are being spent well: a falling floor is the model closing on the body, a climbing stall at a fixed floor is it re-buying the same wrong answer and ends in a revert."),
    ("bodies", "Fns with a body out of the cluster's FILLABLE total (fns cfg-gated to another cluster are excluded from both sides), from the last pass.start snapshot — a whole-cluster coverage reading, not iteration progress. Reaching the total ends no iteration and no pass: the row simply disappears, because from there the loop re-fills bodies that already compile but whose tests fail. `N hollow` = fns still unimplemented!() in this cluster."),
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
    ("llm cut", "LLM_CUT_STOP gauge: share of the last window's model calls that returned empty or reasoning-capped, replayed live from events.jsonl. Bar fills toward the stop threshold (X%/Y%) — full+red means the next cut call takes the run's graceful stop at the next unit boundary. cut = calls that returned no decided answer, empty = calls with zero content chars, 'of N call(s)' = the window folded, 'seen M+' = calls this reading has replayed (a floor on the run's true cumulative count — only the log's recent tail is read)."),
    ("vLLM", "Local model backend health: ✓ healthy, or ◑ loading (tail of the vLLM docker logs shown while it loads)."),
    ("≈ tokens/s", "Estimated throughput, computed from GPU memory bandwidth ÷ loaded model size — not a measured rate."),
]

# Subset shown in the always-on inline hint strip: fields with no self-evident
# meaning from the label alone (unlike cluster/attempt/file/test/docs, which
# read fine cold). Everything is still covered in FIELDS for the [f] view.
CORE_TAGS: set[str] = {
    "in-flight", "notes=", "hot fn", "stall=", "fills", "err", "≈ tokens/s",
    "fill", "unit", "llm cut",
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
