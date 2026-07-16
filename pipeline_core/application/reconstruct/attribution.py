"""Failure→fn attribution use-case — Clean Architecture.

Folds the runtime-panic / compile-error / classifier / recently-filled / reverse-map
attribution paths into one application context over an injected `AttributionDeps`
(StructureReader + cargo-check + LLM classifier + source locator). Behaviour is the
verbatim relocation of pipeline.py's `_attribute_failures` family; the composition root
binds the concrete collaborators. The `#[test]`-oracle filter is applied by the
callers (same as before), via `reconstruct.test_targets.filter_test_targets`.

Cargo-output parsing goes through the structured `domain.test_report` parser
(no regex); the remaining identifier scans use `domain.rust_idents`.
"""
from __future__ import annotations

import os
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
from pipeline_core.domain.test_report import FailureKind as TestFailureKind
from pipeline_core.domain.test_report import parse_test_output
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
    locate_fn: Callable[..., Any]              # regex source locator (stays in pipeline.py)
    run_check_diag: Callable[..., Any]         # cargo check → (rc, rendered, [Diagnostic])
    derive_check_only_cmd: Callable[[str], str]
    is_gen_tests_file: Callable[[str], bool]
    log: Callable[[str], None]


def make_fn_locator(
    structure: Any, src_dir: Path,
) -> Callable[[str, int], FnLocator | None]:
    """Build a ``(panic_file, panic_line) -> FnLocator | None`` resolver over
    `src_dir`'s fn records, caching enumeration + file text. `panic_file` is a
    path as libtest prints it (crate-root-relative, e.g. ``src/error.rs``, or
    absolute); it is normalised to `src_dir`-relative exactly as
    :func:`extract_tdd_frontier` does. Used by the U2 blame partition (F3) and
    any consumer that maps a panic span back to the fn it fired in."""
    recs_by_file: dict[str, list[dict[str, Any]]] = {}
    text_by_file: dict[str, str] = {}
    all_recs: list[dict[str, Any]] | None = None

    def locate(panic_file: str, panic_line: int) -> FnLocator | None:
        nonlocal all_recs
        p_abs = Path(panic_file)
        if not p_abs.is_absolute():
            p_abs = (src_dir / panic_file).resolve()
        try:
            rel = str(p_abs.relative_to(src_dir.resolve()))
        except ValueError:
            return None
        if rel not in recs_by_file:
            if all_recs is None:
                all_recs = structure.enumerate_fns(src_dir) or []
            recs_by_file[rel] = [r for r in all_recs if r.get("file") == rel]
        recs = recs_by_file[rel]
        if not recs:
            return None
        if rel not in text_by_file:
            try:
                text_by_file[rel] = p_abs.read_text(encoding="utf-8")
            except Exception:
                return None
        rec = line_to_fn_record(text_by_file[rel], panic_line, recs)
        if rec is None:
            return None
        return FnLocator(rel, rec["fn_name"])

    return locate


def extract_tdd_frontier(deps: AttributionDeps,
    test_output: str, src_dir: Path,
) -> Frontier:
    """Parse cargo test output → frontier dict.

    Keys are (file_rel, fn_name), values are the test names that panicked in a
    still-unfilled `unimplemented!()`/`todo!()` stub inside that fn. Built on
    the shared structured parser (`parse_test_output`): only STUB_PANIC
    failures are included — they map cleanly to a still-unfilled fn.
    Assertion-failure attribution is a separate pass (`behavior_frontier`).

    Returns {} when no stub panics resolve (build failed before any test ran,
    or every failure is an assertion).
    """
    frontier: Frontier = {}
    if not test_output:
        return frontier
    locate = make_fn_locator(deps.structure, src_dir)
    for f in parse_test_output(test_output).failures:
        if f.kind is not TestFailureKind.STUB_PANIC:
            continue
        if not (f.panic_file and f.panic_line is not None):
            continue
        key = locate(f.panic_file, f.panic_line)
        if key is None:
            continue
        frontier.setdefault(key, set()).add(f.test_name)
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
    if not failing_test_names(test_output):
        return {}, {}
    texts: dict[str, str] = {}
    for trel in sorted(existing_test_files):
        tp = project_dir / trel
        if tp.exists():
            with suppress(Exception):
                texts[trel] = tp.read_text(encoding="utf-8", errors="replace")
    return behavior_frontier_from_texts(deps, test_output, texts, project_dir)


def behavior_frontier_from_texts(
    deps: AttributionDeps,
    test_output: str, texts: dict[str, str], fns_root: Path,
) -> tuple[Frontier, dict[str, tuple[str, str]]]:
    """Core of :func:`behavior_frontier` over already-loaded test texts
    ({rel_path: content}) — the phase-c TDD loop holds the cluster's test pairs
    in memory, so it attributes without re-reading files. `fns_root` is the
    tree whose fn records are the attribution candidates (the cluster's target
    tree in phase-c; the project dir in the fix loop)."""
    failed = failing_test_names(test_output)
    if not failed or not texts:
        return {}, {}
    index = build_test_name_index(texts)
    by_name: dict[str, list[dict[str, Any]]] = {}
    for r in deps.structure.enumerate_fns(fns_root) or []:
        f_rel = r.get("file", "")
        if f_rel in texts or deps.is_gen_tests_file(Path(f_rel).name):
            continue
        if is_test_fn_record(r):  # inline #[cfg(test)] = oracle, never a target
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
    test_texts: dict[str, str] | None = None,
) -> tuple[Frontier, str]:
    """Robust failure-to-fn attribution chain. Returns (frontier, kind). Callers
    pass the result through `_filter_test_targets` (FR-018) — same as the fix
    dispatchers — so a test-oracle fn is never spliced.
    Tries each path until one yields a non-empty frontier:
      1. Runtime panics (unimplemented stubs) — `_extract_tdd_frontier`
      2. Compile errors — `_extract_tdd_compile_frontier`
      3. Behavioral attribution — `behavior_frontier_from_texts` over the
         caller's test texts (deterministic: failing test body → identifier
         intersection with fn records; Hard Rule 9a — runs BEFORE the LLM
         classifier; skipped when the caller has no test texts)
      4. LLM classifier — `_classify_phase_c_test_failures` (P4.5)
      5. Recently-filled fallback — most recent fills are likely culprits
         for new assertion failures
      6. Test-reverse-map fallback — every fn the failing tests could reach

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

    # Paths 1-3 are DETERMINISTIC and DISJOINT by failure mode: a test that
    # stub-panics (1), one whose harness fails to compile (2), and one that
    # assertion-fails on a clean build (3) attribute to DIFFERENT fns. The old
    # "return the first non-empty path" starved 2 and 3 whenever ANY stub still
    # panicked — a handful of unfillable exhausted stubs kept path 1 non-empty
    # every iter, so the wrong-BODY behavioral failures were never surfaced and
    # never re-filled (the anyhow 14-ceiling). Fix: UNION the three, so stub
    # fills AND wrong-body re-fills are dispatched together. Over-attribution is
    # already tolerated downstream (splice guards reject non-stub overwrites;
    # behavioral re-fill WANTS the non-stub keys). The classifier (4) and the
    # coarse fallbacks (5,6) stay as fallbacks — only reached if 1-3 are empty.
    contributors: list[FailureKind] = []

    def _merge(dst: Frontier, add: Frontier, kind: FailureKind) -> None:
        if add:
            contributors.append(kind)
            for k, v in add.items():
                dst.setdefault(k, set()).update(v)

    merged: Frontier = {}
    _merge(merged, _strip(extract_tdd_frontier(deps, test_output, src_dir)),
           FailureKind.RUNTIME)
    _merge(merged, _strip(extract_tdd_compile_frontier(deps, test_output, src_dir)),
           FailureKind.COMPILE)
    if test_texts:
        _bf, _srcs = behavior_frontier_from_texts(
            deps, test_output, test_texts, src_dir)
        _merge(merged, _strip(_bf), FailureKind.BEHAVIOR)
    if merged:
        # One contributor → keep its kind (preserves per-path stats); several →
        # MERGED so the union is visible in logs/events.
        kind = contributors[0] if len(contributors) == 1 else FailureKind.MERGED
        return AttributionResult(merged, kind)
    # 4. LLM classifier — turn off via PHASE_C_TDD_USE_CLASSIFIER=0.
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
    # 5. Recently-filled fallback. If new assertion failures appeared
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
    # 6. Test-reverse-map. For each failing test, queue every fn it
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


# --- Futility escalation (debt #82, 2026-07-10) ------------------------------
# When every fn attributed to a failing test has been rewritten to the same
# dead end, the defect usually sits in the CONSTRUCTION path of the type those
# fns read (observed live: test_len blamed on Chain::len/size_hint — accessors
# that merely COUNT a source chain that context()/source() built wrongly).
# Escalate the frontier to the producers of the exercised types.

def container_type_name(container: str) -> str:
    """The Self type name of an impl container: `impl Debug for Quoted<C>` →
    `Quoted`; `impl<'a> Chain<'a>` → `Chain`; free fns ("") → ""."""
    text = container.strip()
    if not text.startswith("impl"):
        return ""
    rest = text[4:].lstrip()
    if rest.startswith("<"):
        depth = 0
        for i, ch in enumerate(rest):
            if ch == "<":
                depth += 1
            elif ch == ">":
                depth -= 1
                if depth == 0:
                    rest = rest[i + 1:].lstrip()
                    break
    marker = rest.find(" for ")
    if marker != -1:
        rest = rest[marker + 5:].lstrip()
    name = ""
    for ch in rest:
        if ch.isalnum() or ch == "_":
            name += ch
        else:
            break
    return name


def producers_for_types(
    fn_records: list[dict[str, Any]],
    type_names: set[str],
    *,
    exclude: set[FnLocator] | None = None,
) -> list[FnLocator]:
    """Fns that PRODUCE one of `type_names`: return it, or construct it in the
    body (`Type {`, `Type::`). Token containment scan — no regex on source."""
    exclude = exclude or set()
    out: list[FnLocator] = []
    seen: set[FnLocator] = set()
    for r in fn_records:
        key = FnLocator(r.get("file", ""), r.get("fn_name", ""))
        if key in exclude or key in seen or not key[0] or not key[1]:
            continue
        sig = r.get("sig_text", "") or ""
        body = r.get("body_text", "") or ""
        ret = sig.split("->", 1)[1] if "->" in sig else ""
        for tn in type_names:
            if not tn:
                continue
            if tn in ret or f"{tn} {{" in body or f"{tn}::" in body:
                out.append(key)
                seen.add(key)
                break
    return out


def escalate_frontier_to_producers(
    deps: AttributionDeps,
    src_dir: Path,
    test_name: str,
    futile_units: set[FnLocator],
) -> Frontier:
    """Frontier additions for `test_name`: producers of the types whose
    accessors proved futile. Containers come from the fn records (the
    frontier key is a bare (file, fn_name) pair). Fail-safe: empty on any
    missing input."""
    try:
        recs = deps.structure.enumerate_fns(src_dir) or []
    except Exception:
        recs = []
    containers = {(r.get("file", ""), r.get("fn_name", "")): r.get("container", "")
                  for r in recs}
    type_names = {container_type_name(containers.get((k[0], k[1]), ""))
                  for k in futile_units}
    type_names.discard("")
    if not type_names:
        return {}
    producers = producers_for_types(recs, type_names, exclude=futile_units)
    if not producers:
        return {}
    deps.log(f"  [attribution/escalate] {test_name}: accessors of "
             f"{sorted(type_names)} are futile — escalating to "
             f"{len(producers)} producer fn(s): "
             f"{', '.join(f'{f}::{fn}' for f, fn in producers[:5])}"
             + ("..." if len(producers) > 5 else ""))
    return {key: {test_name} for key in producers}
