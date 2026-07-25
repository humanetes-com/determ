"""Failure-diagnosis use-cases (P6 engine-relocation) — the attribution cluster's
LLM-classifier, the TDD-regression note hook, and the cargo-error fn implicator.

Verbatim relocation of pipeline.py's `_classify_phase_c_test_failures`,
`_record_tdd_regression`, and `_implicated_fns_from_cargo_errors`. Each collaborator
the originals reached as a module global (the LLM `ask`, the `log` sink, the structure
reader, the model-note writer, the ctx-budget caps) is now an explicit injected
parameter; the composition root binds them at the call site. All Rust parsing goes
through the `domain.*` structured scanners / the syn extractor (no regex on source);
the `-->` and `error[...]` parses read CARGO OUTPUT (legitimate, R7).
"""
from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.cargo_errors import strip_cargo_warnings
from pipeline_core.domain.attribution import line_to_fn_record
from pipeline_core.domain.models import Diagnostic, FnLocator
from pipeline_core.domain.rust_idents import first_fn_decl_name
from pipeline_core.domain.test_results import (
    parse_phase_c_line,
    rustc_diagnostic_locations,
)

# Classifier output schema — categories the phase-c test-failure classifier may emit.
CLASSIFIER_CATEGORIES = (
    "BAD_FILL", "TEST_PRECONDITION_MISSING",
    "CFG_BOUNDARY", "PANIC_OR_TIMEOUT", "ENV_DEPENDENT",
)


def classify_phase_c_test_failures(
    test_out: str,
    filled_this_pass: list[FnLocator],
    failed_tests: list[str],
    cluster_name: str,
    *,
    ask: Callable[..., str],
    log: Callable[[str], None],
    model: str,
    err_cap: int,
) -> dict[str, dict[str, Any]]:
    """Ask the LLM to classify each failing test. Returns
        {test_name: {"category": str, "fn": "file::name" | "", "reason": str}}.
    Empty dict on failure / no failing tests / classifier disabled.

    This is a single short LLM call; one inference for the whole set, not
    one per test. Output schema is strictly enforced by line-level parsing.
    Unknown categories or unparseable lines are dropped (silently — we'd
    rather fall back to the naive "retry everything" path than block the loop).
    """
    if not failed_tests:
        return {}
    if os.getenv("PHASE_C_TEST_FIX_USE_CLASSIFIER", "1") == "0":
        return {}

    # Budget the test_out excerpt: classifier needs to see error blocks but
    # has no need for the full multi-MB cargo output. Ctx-budget-aware (scales
    # with the model's context, floored for small windows) — not a frozen literal.
    excerpt = test_out[-err_cap:] if len(test_out) > err_cap else test_out

    filled_lines = "\n".join(f"  - {r}::{f}" for r, f in filled_this_pass[:60])
    failing_lines = "\n".join(f"  - {t}" for t in failed_tests[:30])

    prompt = (
        f"You are diagnosing a Rust test failure in cluster `{cluster_name}` "
        f"of a source-reconstruction pipeline. The fns listed below were filled "
        f"this pass; the tests listed below failed. For each failing test, "
        f"classify the most likely cause and (when applicable) name the fn that "
        f"should be re-implemented.\n\n"
        f"Categories:\n"
        f"  BAD_FILL                  — a fn just filled this pass has a wrong "
        f"body; name the fn (file::name).\n"
        f"  TEST_PRECONDITION_MISSING — the failing test depends on a DIFFERENT "
        f"fn that hasn't been filled correctly yet; name the suspect fn if you "
        f"can identify it from the error.\n"
        f"  CFG_BOUNDARY              — the failure points at code excluded "
        f"from this cluster's cfg-set (different cluster owns it).\n"
        f"  PANIC_OR_TIMEOUT          — the test compiled but panicked at runtime, "
        f"or timed out. Include the panic site/frame if visible.\n"
        f"  ENV_DEPENDENT             — failure stems from environment (missing "
        f"file, env var, network), not the fill body.\n\n"
        f"FNs filled this pass:\n{filled_lines}\n\n"
        f"Failing tests:\n{failing_lines}\n\n"
        f"Cargo test output (tail):\n```\n{excerpt}\n```\n\n"
        f"Output one line PER failing test, EXACT format:\n"
        f"  TEST <name>: <CATEGORY> [fn=<file::name>] — <one-sentence reason>\n"
        f"The fn= field is required for BAD_FILL and TEST_PRECONDITION_MISSING; "
        f"omit the [...] block for the other categories.\n"
        f"Do not output anything else."
    )

    try:
        # Use global MAX_TOKENS default — hardcoded caps risked content
        # truncation under tight thinking-budget conditions.
        result = ask(prompt, model=model, think=None)
    except Exception as e:
        log(f"  [phase-c/classifier] LLM call failed: {e}")
        return {}

    out: dict[str, dict[str, Any]] = {}
    for ln in result.splitlines():
        parsed = parse_phase_c_line(ln)
        if parsed is None:
            continue
        test, cat, fn, reason = parsed
        if cat not in CLASSIFIER_CATEGORIES:
            continue
        out[test] = {
            "category": cat,
            "fn": (fn or "").strip(),
            "reason": reason,
        }
    return out


def record_tdd_regression(
    fr_path: Path,
    src_dir: Path,
    filled_keys: list[FnLocator],
    broken_tests: set[str],
    prev_best: int,
    now_pass: int,
    *,
    enumerate_fns: Callable[[Path], list[dict[str, Any]] | None],
    append_note: Callable[[Path, str], object],
    body_cap: int,
) -> str:
    """Hook 3 — turn a `14→11` pass-count regression into an ACTIONABLE note.

    For each fn filled in the pass that just broke previously-passing tests,
    write a concrete WRONG-body Model Note (file + fn + the broken test names +
    the body that was applied) into the FR Model Notes channel, which the next
    fill prompt for that fn injects automatically (relevance-ranked by file/fn).
    The bare metric is unactionable; the body+broken-tests pair is the WRONG
    example the model can react to.

    Best-effort: never raises into the TDD loop. Returns a compact note (e.g.
    "regressed t1, t2") to stamp on the LoopState trajectory, or "" if none.
    """
    try:
        recs = enumerate_fns(src_dir) or []
    except Exception:
        recs = []
    by_name: dict[str, dict[str, Any]] = {}
    for r in recs:
        nm = r.get("fn_name", "")
        if nm and nm not in by_name:
            by_name[nm] = r
    # Causality guard: a note that blames a body is only trustworthy when the
    # AGGREGATE actually dropped. Flips with an equal/rising aggregate mean
    # the batch traded tests — blaming every filled fn poisons the notes
    # channel (observed live: correct `source`/`root_cause` bodies marked
    # WRONG by "dropped 22→22" notes; the model then avoided them).
    if int(now_pass) >= int(prev_best):
        return ""
    broken = ", ".join(sorted(broken_tests)[:6])
    batch_caveat = ("" if len(filled_keys) == 1 else
                    f" ({len(filled_keys)} bodies were filled this pass — the"
                    " culprit is ONE of them, treat this as a suspect, not a"
                    " verdict)")
    for key in filled_keys:
        fn = key[1] if isinstance(key, tuple) and len(key) > 1 else str(key)
        rec = by_name.get(fn)
        if not rec:
            continue
        file_rel = rec.get("file", "")
        body = (rec.get("body_text", "") or "").strip()
        note = (
            f"[regression] In {Path(file_rel).name or '?'}, after `{fn}` was "
            f"written, previously-passing test(s) FAILED: {broken} (cluster "
            f"pass count dropped {int(prev_best)}→{int(now_pass)})"
            f"{batch_caveat}. Body applied:\n{body[:body_cap]}\n"
            f"Re-implement `{fn}` so {broken} keep passing."
        )
        with suppress(Exception):
            append_note(fr_path, note)
    return f"regressed {broken}" if broken else ""


def implicated_fns_from_cargo_errors(
    cargo_output: str,
    target_src_dir: Path,
    *,
    enumerate_fns: Callable[[Path], list[dict[str, Any]] | None],
) -> list[FnLocator]:
    """Parse cargo error output and return the (file_rel, fn_name) pairs the
    errors implicate.

    For each `error[...]` block: find the `--> file.rs:line:col` marker, then
    walk backward in that file from `line` to find the enclosing fn. Dedup
    by (file, fn). Used by P3.12 end-of-cluster fix loop to identify which
    fns to revert + retry.
    """
    if not cargo_output:
        return []
    cargo_output = strip_cargo_warnings(cargo_output)
    return _locations_to_implicated_fns(
        rustc_diagnostic_locations(cargo_output), target_src_dir,
        enumerate_fns=enumerate_fns)


def implicated_fns_from_diagnostics(
    diagnostics: list[Diagnostic],
    target_src_dir: Path,
    *,
    enumerate_fns: Callable[[Path], list[dict[str, Any]] | None],
) -> list[FnLocator]:
    """Structured-diagnostics twin of :func:`implicated_fns_from_cargo_errors`.

    Attributes each error to a fn from its primary span AND every
    macro-invocation call-site (`Diagnostic.macro_call_sites`). The text form
    sees only the primary `-->` span, so an error that surfaces inside a macro
    body (primary span = the macro definition, which encloses no fn) is
    attributed to NOTHING and its invoking fn never gets reverted — the exact
    gap that let a seeded, layout-incompatible body survive the pre-fill heal
    and keep a whole cluster red. Feeding the expansion call-sites in reaches
    the fn that actually wrote the bad macro call."""
    locations: list[tuple[str, int]] = []
    for d in diagnostics or []:
        if d.file and d.line:
            locations.append((d.file, d.line))
        locations.extend(d.macro_call_sites)
    return _locations_to_implicated_fns(
        locations, target_src_dir, enumerate_fns=enumerate_fns)


def implicated_fns_from_check(
    diagnostics: list[Diagnostic],
    rendered_output: str,
    target_src_dir: Path,
    *,
    enumerate_fns: Callable[[Path], list[dict[str, Any]] | None],
) -> list[FnLocator]:
    """Who a failed cargo check implicates — the one attribution policy every
    revert gate uses.

    Structured diagnostics win because they carry the macro-expansion call-site
    chain: an error whose primary span sits in a macro DEFINITION encloses no
    fn, so the text scan attributes it to NOTHING and the gate reads "no
    implicated fns" — a green light for a body that keeps the cluster red. The
    rendered-text scan is the fallback for output carrying no JSON messages at
    all (cargo-level failures, older captures). Stated here once so a second
    gate cannot be wired to the weaker half."""
    if diagnostics:
        return implicated_fns_from_diagnostics(
            diagnostics, target_src_dir, enumerate_fns=enumerate_fns)
    return implicated_fns_from_cargo_errors(
        rendered_output, target_src_dir, enumerate_fns=enumerate_fns)


def _locations_to_implicated_fns(
    locations: list[tuple[str, int]],
    target_src_dir: Path,
    *,
    enumerate_fns: Callable[[Path], list[dict[str, Any]] | None],
) -> list[FnLocator]:
    """Map cargo diagnostic `(file, line)` locations to the (file_rel, fn_name)
    pairs whose bodies enclose them. Dedup by (file, fn), order-preserving."""
    pairs: list[FnLocator] = []
    seen: set[FnLocator] = set()
    # Enclosing-fn attribution via the syn extractor (byte-span lookup); the
    # backward `fn` scan is the no-binary fallback.
    _all_recs = enumerate_fns(target_src_dir)
    _recs_by_file: dict[str, list[dict[str, Any]]] = {}
    if _all_recs is not None:
        for _r in _all_recs:
            _recs_by_file.setdefault(_r.get("file", ""), []).append(_r)
    for file_str, line_no in locations:
        # Normalise to rel path under target_src_dir.
        try:
            abs_p = (target_src_dir / file_str).resolve()
            rel_p = str(abs_p.relative_to(target_src_dir.resolve()))
        except Exception:
            # cargo sometimes emits absolute paths; try suffix match.
            cand = Path(file_str)
            rel_p = None
            for p in target_src_dir.rglob("*.rs"):
                if str(p).endswith(file_str) or p.name == cand.name:
                    rel_p = str(p.relative_to(target_src_dir))
                    break
            if rel_p is None:
                continue
        assert rel_p is not None  # try sets str; except continues on None
        src_path = target_src_dir / rel_p
        if not src_path.exists():
            continue
        try:
            text = src_path.read_text(encoding="utf-8")
        except Exception:
            continue
        # Enclosing fn: prefer the extractor's byte-span records (handles impl
        # methods / cfg'd fns / inline decls); fall back to the backward scan.
        fn_name: str | None = None
        _recs = _recs_by_file.get(rel_p)
        if _recs:
            _rec = line_to_fn_record(text, line_no, _recs)
            if _rec:
                fn_name = _rec["fn_name"]
        if fn_name is None:
            lines = text.splitlines()
            for i in range(min(line_no, len(lines)) - 1, -1, -1):
                if nm := first_fn_decl_name(lines[i]):
                    fn_name = nm
                    break
        if not fn_name:
            continue
        key = FnLocator(rel_p, fn_name)
        if key in seen:
            continue
        seen.add(key)
        pairs.append(key)
    return pairs
