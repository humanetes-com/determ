"""Failure→fn attribution use-case (P2) — Clean Architecture.

Folds the runtime-panic / compile-error / classifier / recently-filled / reverse-map
attribution paths into one application context over an injected `AttributionDeps`
(StructureReader + cargo-check + LLM classifier + source locator). Behaviour is the
verbatim relocation of pipeline.py's `_attribute_failures` family; the composition root
binds the concrete collaborators. The `#[test]`-oracle filter (FR-018) is applied by the
callers (same as before), via `reconstruct.test_targets.filter_test_targets`.

cfg/test-output regexes here parse CARGO OUTPUT and TEST files (not the crate's Rust
source through the no-regex rule), so they are legitimate (R7); migrating the test-file
scan to syn is P7.
"""
from __future__ import annotations

import os
import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.test_targets import (
    is_test_fn_record,
    test_fn_keys,
)
from pipeline_core.domain.attribution import (
    AttributionResult,
    FailureKind,
    line_to_fn_record,
)
from pipeline_core.domain.models import Diagnostic, FnLocator, Frontier
from pipeline_core.domain.rust_idents import ascii_word_tokens, test_fn_names
from pipeline_core.domain.test_results import (
    build_test_name_index,
    failing_test_names,
    identifiers_in,
    parse_failed_tests,
    parse_rustc_compile_errors,
)


@dataclass(frozen=True, slots=True)
class AttributionDeps:
    """Collaborators the attribution paths need, injected by the composition root."""

    structure: Any                       # ports.structure.StructureReader (enumerate_fns)
    classify: Callable[..., dict[str, dict[str, Any]]]   # LLM per-test classifier
    locate_fn: Callable[..., Any]              # regex source locator (R7.6, stays in pipeline.py)
    run_check_diag: Callable[..., Any]         # cargo check → (rc, rendered, [Diagnostic])
    derive_check_only_cmd: Callable[[str], str]
    is_gen_tests_file: Callable[[str], bool]
    log: Callable[[str], None]


_PANIC_HEADER_RE = re.compile(
    r"thread '([^']+)'(?:\s+\(\d+\))?\s+panicked at ([^:]+):(\d+)(?::(\d+))?:\s*\n([^\n]*)"
)
_TEST_STDOUT_HEADER_RE = re.compile(r"^----\s+(\S+)\s+stdout\s+----\s*$", re.MULTILINE)


def extract_tdd_frontier(deps: AttributionDeps,
    test_output: str, src_dir: Path,
) -> Frontier:
    """Parse cargo test output → frontier dict.

    Keys are (file_rel, fn_name), values are sets of test names that
    panicked inside that fn. Only `not yet implemented` panics are
    included by default — they map cleanly to a still-unfilled fn.
    Assertion-failure attribution is handled in a separate pass (see
    `_extract_tdd_assertion_frontier`).

    Returns {} when no panics could be parsed (e.g. cargo build failed
    before any test ran).
    """
    frontier: Frontier = {}
    if not test_output:
        return frontier
    # Per-file fn records cache.
    fn_records_by_file: dict[str, list[dict[str, Any]]] = {}
    file_text_cache: dict[str, str] = {}
    # Split output into per-test stdout sections. Each section holds the
    # panic header for that test's failures.
    sections = _TEST_STDOUT_HEADER_RE.split(test_output)
    # _TEST_STDOUT_HEADER_RE.split returns [pre, test1, body1, test2, body2, ...].
    if len(sections) < 3:
        # No per-test sections — try parsing the whole output as a single
        # block (some cargo versions don't emit `---- N stdout ----` headers
        # for single-test runs).
        sections = ["", "unknown_test", test_output]
    for i in range(1, len(sections) - 1, 2):
        test_name = sections[i].strip()
        body = sections[i + 1]
        for m in _PANIC_HEADER_RE.finditer(body):
            _thread, file_path, line_str, col_str, _panic_msg = m.group(
                1, 2, 3, 4, 5)
            # Accept every panic whose path resolves to a fn in src/. The
            # skeleton extractor replaced every fn body with
            # `unimplemented!()`, so any panic inside src/ IS from a stub.
            # Test-file panics (tests/foo.rs:N) get rejected later by the
            # fn-lookup, since we only enumerate src/ fns.
            try:
                line_no = int(line_str)
            except ValueError:
                continue
            # Resolve file_path relative to src_dir.
            p_abs = Path(file_path)
            if not p_abs.is_absolute():
                p_abs = (src_dir / file_path).resolve()
            try:
                rel = str(p_abs.relative_to(src_dir.resolve()))
            except ValueError:
                # File outside src_dir — skip (likely a panic from std).
                continue
            # Look up fn at this line.
            if rel not in fn_records_by_file:
                _all_recs = deps.structure.enumerate_fns(src_dir) or []
                fn_records_by_file[rel] = [r for r in _all_recs
                                            if r.get("file") == rel]
            recs = fn_records_by_file[rel]
            if not recs:
                continue
            if rel not in file_text_cache:
                try:
                    file_text_cache[rel] = p_abs.read_text(encoding="utf-8")
                except Exception:
                    continue
            col_no = int(col_str) if col_str else None
            rec = line_to_fn_record(file_text_cache[rel], line_no, recs,
                                      col_no=col_no)
            if rec is None:
                continue
            key = FnLocator(rel, rec["fn_name"])
            frontier.setdefault(key, set()).add(test_name)
    return frontier

def behavior_frontier(
    deps: AttributionDeps,
    test_output: str, project_dir: Path, existing_test_files: set[str],
) -> tuple[Frontier, dict[str, tuple[str, str]]]:
    """Attribute behavioral test failures (harness compiles, no stub panics —
    assertion failures etc.) to the src fns they exercise, so the fix loop can
    repair fn bodies instead of falling back to whole-file rewrites.

    For each FAILED test: locate its source via the test-name index, collect
    the identifiers its text mentions, and intersect with the extractor's fn
    records. A fn name shared by several records is attributed only to records
    whose container also shares a token with the test text.

    Returns ({(file_rel, fn): {test names}}, {test name: (test_rel, test_src)}).
    """
    failed = failing_test_names(test_output)
    if not failed:
        return {}, {}
    texts: dict[str, str] = {}
    for trel in sorted(existing_test_files):
        tp = project_dir / trel
        if tp.exists():
            with suppress(Exception):
                texts[trel] = tp.read_text(encoding="utf-8", errors="replace")
    if not texts:
        return {}, {}
    index = build_test_name_index(texts)
    by_name: dict[str, list[dict[str, Any]]] = {}
    for r in deps.structure.enumerate_fns(project_dir) or []:
        f_rel = r.get("file", "")
        if f_rel in existing_test_files or deps.is_gen_tests_file(Path(f_rel).name):
            continue
        if is_test_fn_record(r):  # inline #[cfg(test)] = oracle, never a target (FR-018)
            continue
        by_name.setdefault(r.get("fn_name", ""), []).append(r)
    frontier: Frontier = {}
    test_srcs: dict[str, tuple[str, str]] = {}
    for tname in failed:
        bare = tname.split("::")[-1].strip()
        trel = index.get(bare, "")
        if not trel or trel not in texts:
            continue
        loc = deps.locate_fn(texts[trel], bare)
        if loc is None:
            continue
        sig_text, _ss, _bs, _be = loc
        tsrc = f"{sig_text} {{{texts[trel][_bs:_be]}}}"
        idents = identifiers_in(tsrc)
        test_srcs[tname] = (trel, tsrc)
        for fn_name in idents & set(by_name):
            recs = by_name[fn_name]
            for rec in recs:
                if len(recs) > 1 and not (
                        identifiers_in(rec.get("container", "")) & idents):
                    continue
                frontier.setdefault(FnLocator(rec["file"], fn_name), set()).add(tname)
    return frontier, test_srcs

def extract_tdd_compile_frontier(deps: AttributionDeps,
    build_output: str, src_dir: Path,
) -> Frontier:
    """When cargo test fails because cargo BUILD failed (compile errors,
    not panics), parse error[Exxxx] messages → fn records. Same shape as
    `_extract_tdd_frontier` but with test names replaced by "<compile>"
    so the caller can distinguish.
    """
    frontier: Frontier = {}
    if not build_output:
        return frontier
    fn_records_by_file: dict[str, list[dict[str, Any]]] = {}
    file_text_cache: dict[str, str] = {}
    for _msg, file_path, line_no in parse_rustc_compile_errors(build_output):
        p_abs = Path(file_path)
        if not p_abs.is_absolute():
            p_abs = (src_dir / file_path).resolve()
        try:
            rel = str(p_abs.relative_to(src_dir.resolve()))
        except ValueError:
            continue
        if rel not in fn_records_by_file:
            _all_recs = deps.structure.enumerate_fns(src_dir) or []
            fn_records_by_file[rel] = [r for r in _all_recs
                                        if r.get("file") == rel]
        recs = fn_records_by_file[rel]
        if not recs:
            continue
        if rel not in file_text_cache:
            try:
                file_text_cache[rel] = p_abs.read_text(encoding="utf-8")
            except Exception:
                continue
        rec = line_to_fn_record(file_text_cache[rel], line_no, recs)
        if rec is None:
            continue
        frontier.setdefault(FnLocator(rel, rec["fn_name"]), set()).add("<compile>")
    return frontier

def frontier_from_diags(deps: AttributionDeps,
    diagnostics: list[Diagnostic], src_dir: Path,
) -> Frontier:
    """Same (file, fn) → {"<compile>"} frontier as
    `_extract_tdd_compile_frontier`, but built from STRUCTURED cargo
    diagnostics (each has file/line/code) instead of regex-parsing rendered
    text. Use this when the diagnostics come from `_run_cargo_check_diag`,
    whose rendered text carries ANSI colour codes that the plain-text
    `_COMPILE_ERROR_RE` cannot match — re-parsing that text yields an empty
    frontier and silently defeats any "is this fn still broken?" check.
    """
    frontier: Frontier = {}
    fn_records_by_file: dict[str, list[dict[str, Any]]] = {}
    file_text_cache: dict[str, str] = {}
    for d in diagnostics or []:
        rel = d.file or ""
        line_no = int(d.line or 0)
        if not rel or line_no < 1:
            continue
        if rel not in fn_records_by_file:
            _all_recs = deps.structure.enumerate_fns(src_dir) or []
            fn_records_by_file[rel] = [r for r in _all_recs if r.get("file") == rel]
        recs = fn_records_by_file[rel]
        if not recs:
            continue
        if rel not in file_text_cache:
            try:
                file_text_cache[rel] = (src_dir / rel).read_text(encoding="utf-8")
            except Exception:
                continue
        rec = line_to_fn_record(file_text_cache[rel], line_no, recs)
        if rec is None:
            continue
        frontier.setdefault(FnLocator(rel, rec["fn_name"]), set()).add("<compile>")
    return frontier

def compile_error_frontier(
    deps: AttributionDeps,
    output: str, project_dir: Path, check_cmd: str,
    env: dict[str, str], timeout: int,
) -> Frontier:
    """Structured-first compile-error frontier (R8.3): re-run the derived
    check-only command for JSON diagnostics and attribute errors→fns via
    `_frontier_from_diags`. Regex-parsing `output` remains only as the
    fallback when no structured diagnostics came back (cargo unavailable /
    JSON parse failure). rc==0 means the crate compiles — no compile
    frontier, regardless of what `output` (e.g. test failures) contains.
    """
    try:
        rc, _rendered, diags = deps.run_check_diag(
            deps.derive_check_only_cmd(check_cmd), project_dir, env, timeout)
        if rc == 0:
            return {}
        if diags:
            return frontier_from_diags(deps, diags, project_dir)
    except Exception as e:
        deps.log(f"  [fix loop] structured frontier failed ({e}) → text fallback")
    return extract_tdd_compile_frontier(deps, output, project_dir)

def build_test_reverse_map(
    deps: AttributionDeps,
    test_files: list[Path],
    src_dir: Path,
) -> dict[str, set[FnLocator]]:
    """Return {test_name: set[(file_rel, fn_name)]} — for each #[test] fn in
    the test files, the set of fn records in `src_dir` whose names appear
    textually in the test's body. Best-effort static analysis used as the
    last-resort attribution path when panics/compile errors/classifier
    all fail to identify a fn to fix.

    Coarse: matches any identifier whose name equals a src fn name, even
    if it's a comment or string. Acceptable — over-attribution just
    queues a few extra fns for refill; the per-fn fix loop will skip
    fns that are already filled correctly.
    """
    fn_index = deps.structure.enumerate_fns(src_dir) or []
    if not fn_index:
        return {}
    # name → list[(file, fn_name)] for PRODUCTION fns only. A failing test is
    # only ever attributed to a fixable production fn, never to another test
    # (the test-oracle is immutable — FR-018), so test fns are excluded as
    # candidates and instead indexed as KEYS by the inline pass below.
    by_name: dict[str, list[FnLocator]] = {}
    src_test_recs: list[dict[str, Any]] = []
    for r in fn_index:
        if is_test_fn_record(r):
            src_test_recs.append(r)
            continue
        name = r.get("fn_name", "")
        f = r.get("file", "")
        if name and f:
            by_name.setdefault(name, []).append(FnLocator(f, name))
    out: dict[str, set[FnLocator]] = {}
    for tp in test_files:
        try:
            text = tp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        ids = set(ascii_word_tokens(text))
        # Match each #[test] fn's name; pair it with all fn records whose
        # identifier appears anywhere in the test file. This is file-level
        # not fn-body-level (cheaper, captures helper fns the test calls).
        for test_name in test_fn_names(text):
            candidates: set[FnLocator] = set()
            for ident in ids:
                if ident in by_name:
                    candidates.update(by_name[ident])
            out[test_name] = candidates
    # Inline `#[cfg(test)]` unit tests live in src/ (not under tests/), so the
    # loop above never sees them — without this pass a failing inline test like
    # `extra_cap_limit` has NO production target and the fix loop dies with
    # `no_actionable_refs` (the test-oracle filter strips its only frontier key).
    # Index each inline test by the production fns its OWN body references —
    # byte-span slice from the extractor + `identifiers_in` (structured, no regex).
    src_bytes: dict[str, bytes] = {}
    for r in src_test_recs:
        test_name = r.get("fn_name", "")
        f = r.get("file", "")
        s = r.get("sig_byte_start", 0)
        e = r.get("body_byte_end", 0)
        if not (test_name and f and e > s):
            continue
        if f not in src_bytes:
            try:
                src_bytes[f] = (src_dir / f).read_bytes()
            except Exception:
                src_bytes[f] = b""
        body = src_bytes[f][s:e].decode("utf-8", "replace")
        candidates = out.setdefault(test_name, set())
        for ident in identifiers_in(body):
            if ident in by_name:
                candidates.update(by_name[ident])
    return out

def attribute_failures(
    deps: AttributionDeps,
    test_output: str,
    src_dir: Path,
    recently_filled: list[FnLocator],
    test_reverse_map: dict[str, set[FnLocator]],
    cluster_name: str,
) -> tuple[Frontier, str]:
    """Robust failure-to-fn attribution chain. Returns (frontier, kind). Callers
    pass the result through `_filter_test_targets` (FR-018) — same as the fix
    dispatchers — so a test-oracle fn is never spliced.
    Tries each path until one yields a non-empty frontier:
    Tries each path until one yields a non-empty frontier:
      1. Runtime panics (unimplemented stubs) — `_extract_tdd_frontier`
      2. Compile errors — `_extract_tdd_compile_frontier`
      3. LLM classifier — `_classify_phase_c_test_failures` (P4.5)
      4. Recently-filled fallback — most recent fills are likely culprits
         for new assertion failures
      5. Test-reverse-map fallback — every fn the failing tests could reach

    Empty frontier means all paths exhausted — caller should let the
    stall budget exit the loop. Kind tells the caller which path produced
    the frontier (for logs/events).

    The `#[test]`-oracle filter (FR-018) is applied to EACH path's frontier
    here, not just to the final result by the caller: an assertion failure
    panics at the test's OWN line, so path 1 (runtime panics) would otherwise
    resolve to the immutable test fn, short-circuit the chain, then be emptied
    by the caller → `no_actionable_refs`. Stripping test keys per-path lets a
    test-only frontier fall through to the reverse-map, which attributes the
    failure to the production fn the test exercises.
    """
    _test_keys = test_fn_keys(deps.structure.enumerate_fns(src_dir) or [])

    def _strip(fr: Frontier) -> Frontier:
        return {k: v for k, v in fr.items() if k not in _test_keys} if _test_keys else fr

    # 1. Runtime panics.
    frontier = _strip(extract_tdd_frontier(deps, test_output, src_dir))
    if frontier:
        return AttributionResult(frontier, FailureKind.RUNTIME)
    # 2. Compile errors.
    frontier = _strip(extract_tdd_compile_frontier(deps, test_output, src_dir))
    if frontier:
        return AttributionResult(frontier, FailureKind.COMPILE)
    # 3. LLM classifier — turn off via PHASE_C_TDD_USE_CLASSIFIER=0.
    if os.getenv("PHASE_C_TDD_USE_CLASSIFIER", "1") != "0":
        failing = sorted(parse_failed_tests(test_output))
        if failing:
            classification = deps.classify(
                test_out=test_output, filled_this_pass=[],
                failed_tests=failing, cluster_name=cluster_name,
            )
            synth: Frontier = {}
            for tname, info in classification.items():
                if info.get("category") not in (
                        "BAD_FILL", "TEST_PRECONDITION_MISSING"):
                    continue
                ref = info.get("fn") or ""
                if "::" in ref:
                    fp, fn = ref.split("::", 1)
                    key = FnLocator(fp.strip(), fn.strip())
                    if key[0] and key[1]:
                        synth.setdefault(key, set()).add(tname)
            synth = _strip(synth)
            if synth:
                return AttributionResult(synth, FailureKind.CLASSIFIER)
    # 4. Recently-filled fallback. If new assertion failures appeared
    # after recent fills, the recent fills are the most likely cause.
    # Queue them in reverse-filled order (newest first).
    if recently_filled:
        failing = sorted(parse_failed_tests(test_output))
        if failing:
            recent_synth: Frontier = {}
            for key in reversed(recently_filled):
                recent_synth.setdefault(key, set()).update(failing)
            recent_synth = _strip(recent_synth)
            if recent_synth:
                return AttributionResult(recent_synth, FailureKind.RECENTLY_FILLED)
    # 5. Test-reverse-map. For each failing test, queue every fn it
    # could reach according to the static reverse map. Over-attribution
    # is tolerable here — per-fn splice guards reject overwriting
    # non-stub bodies, so already-correct fills aren't clobbered.
    if test_reverse_map:
        failing = sorted(parse_failed_tests(test_output))
        if failing:
            reverse_synth: Frontier = {}
            for tname in failing:
                # Strip module prefix if cargo formats as `module::test_name`.
                bare = tname.split("::")[-1]
                cands = test_reverse_map.get(bare) or test_reverse_map.get(tname) or set()
                for key in cands:
                    reverse_synth.setdefault(key, set()).add(tname)
            reverse_synth = _strip(reverse_synth)
            if reverse_synth:
                return AttributionResult(reverse_synth, FailureKind.REVERSE_MAP)
    return AttributionResult({}, FailureKind.NO_FRONTIER)
