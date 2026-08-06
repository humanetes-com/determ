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
from collections.abc import Callable, Iterable
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
from pipeline_core.domain.models import Diagnostic, FnLocator, FnSite, Frontier
from pipeline_core.domain.rust_idents import (
    ascii_word_tokens,
    is_compile_fail_test_source,
    test_fn_names,
)
from pipeline_core.domain.test_report import FailureKind as TestFailureKind
from pipeline_core.domain.test_report import parse_test_output
from pipeline_core.domain.test_results import (
    build_test_name_index,
    failing_test_names,
    identifiers_in,
    parse_failed_tests,
    parse_rustc_compile_errors,
)
from pipeline_core.domain.type_production import EMPTY as EMPTY_PRODUCIBLE
from pipeline_core.domain.type_production import (
    ProducibleTypes,
    constructs_type,
    returns_type,
)


@dataclass(frozen=True, slots=True)
class AttributionDeps:
    """Collaborators the attribution paths need, injected by the composition root."""

    structure: Any                       # ports.structure.StructureReader (enumerate_fns)
    classify: Callable[..., dict[str, dict[str, Any]]]   # LLM per-test classifier
    locate_fn: Callable[..., tuple[str, int, int, int] | None]  # source locator
    run_check_diag: Callable[..., tuple[int, str, list[Diagnostic]]]  # cargo check
    derive_check_only_cmd: Callable[[str], str]
    is_gen_tests_file: Callable[[str], bool]
    log: Callable[[str], None]


def _make_record_locator(
    structure: Any, src_dir: Path,
) -> Callable[[str, int], tuple[str, dict[str, Any]] | None]:
    """Shared core: build a ``(panic_file, panic_line) -> (rel, fn_record) |
    None`` resolver over `src_dir`'s fn records, caching enumeration + file
    text. `panic_file` is a path as libtest prints it (crate-root-relative,
    e.g. ``src/error.rs``, or absolute); it is normalised to `src_dir`-relative
    exactly as :func:`extract_tdd_frontier` does. The two public locators below
    project the resolved record onto their own identity type."""
    recs_by_file: dict[str, list[dict[str, Any]]] = {}
    text_by_file: dict[str, str] = {}
    all_recs: list[dict[str, Any]] | None = None

    def locate(panic_file: str, panic_line: int) -> tuple[str, dict[str, Any]] | None:
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
        return (rel, rec)

    return locate


def make_fn_locator(
    structure: Any, src_dir: Path,
) -> Callable[[str, int], FnLocator | None]:
    """``(panic_file, panic_line) -> FnLocator | None`` — resolve a panic span to
    the (file, fn_name) it fired in. Frontier/dispatch key; drops container."""
    core = _make_record_locator(structure, src_dir)

    def locate(panic_file: str, panic_line: int) -> FnLocator | None:
        hit = core(panic_file, panic_line)
        return None if hit is None else FnLocator(hit[0], hit[1]["fn_name"])

    return locate


def make_fn_site_locator(
    structure: Any, src_dir: Path,
) -> Callable[[str, int], FnSite | None]:
    """``(panic_file, panic_line) -> FnSite | None`` — resolve a panic span to a
    CONTAINER-qualified identity. Used by the U2 blame partition so a panic in a
    same-named sibling (free `fn not` vs a `Bool` impl's `fn not`) is told apart
    from the unit's own fn."""
    core = _make_record_locator(structure, src_dir)

    def locate(panic_file: str, panic_line: int) -> FnSite | None:
        hit = core(panic_file, panic_line)
        if hit is None:
            return None
        rel, rec = hit
        return FnSite(rel, rec["fn_name"], rec.get("container", "") or "")

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
    unrepairable_tests: frozenset[str] = frozenset(),
) -> tuple[Frontier, dict[str, tuple[str, str]]]:
    """Core of :func:`behavior_frontier` over already-loaded test texts
    ({rel_path: content}) — the phase-c TDD loop holds the cluster's test pairs
    in memory, so it attributes without re-reading files. `fns_root` is the
    tree whose fn records are the attribution candidates (the cluster's target
    tree in phase-c; the project dir in the fix loop).

    `unrepairable_tests` (bare names) are dropped before anything is attributed.
    This path CONVICTS — its frontier may revert a unit — so a test whose
    assertion is about rustc's own output rather than about any body must not
    reach it. The body of `tests/compiletest.rs` is `trybuild::TestCases::new()`
    plus a glob, and its identifiers are the whole basis on which this path would
    name fns."""
    failed = failing_test_names(test_output)
    if unrepairable_tests:
        failed = [t for t in failed
                  if t.split("::")[-1] not in unrepairable_tests]
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
    for _diag in parse_rustc_compile_errors(build_output):
        file_path, line_no = _diag.file, _diag.line
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
    # (the test-oracle is immutable), so test fns are excluded as
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
        if is_compile_fail_test_source(text):
            # A compile-fail harness convicts by coincidence only: the tokens of
            # `tests/compiletest.rs` are `trybuild`, `TestCases::new` and the
            # `not(nightly)` of its own attribute, which on the anyhow tree
            # matched five unrelated `new` fns and `lib.rs::not`. Nothing it
            # names can be repaired by writing a body.
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
        # This pass indexes by RECORD, so it sees the compile-fail harness again
        # even when the file loop above skipped it — `src_dir` is the project
        # root for the task3 callers, and the extractor walks `tests/` too.
        if is_compile_fail_test_source(src_bytes[f].decode("utf-8", "replace")):
            continue
        body = src_bytes[f][s:e].decode("utf-8", "replace")
        candidates = out.setdefault(test_name, set())
        for ident in identifiers_in(body):
            if ident in by_name:
                candidates.update(by_name[ident])
    return out

def not_body_repairable_tests(test_files: list[Path]) -> frozenset[str]:
    """Names of failing tests no body rewrite can satisfy — today, the
    compile-fail harnesses (`trybuild`/`compiletest`).

    Reported rather than silently dropped: the test is a real requirement of the
    crate, it is just not one this repair loop can meet, and a run that ends
    with it still failing should say which failures were out of reach instead of
    looking like it gave up.
    """
    out: set[str] = set()
    for tp in test_files:
        try:
            text = tp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if is_compile_fail_test_source(text):
            out.update(test_fn_names(text))
    return frozenset(out)


def expand_frontier_by_calls(
    deps: AttributionDeps,
    frontier: Frontier,
    src_dir: Path,
    *,
    max_hops: int = 2,
    cap: int = 40,
) -> tuple[Frontier, dict[FnLocator, int]]:
    """Follow callee edges out of an attributed frontier, returning the widened
    frontier and each key's hop distance from the seed.

    Every attribution path this pipeline has stops at fns the failing TEST names
    — by panic location, by identifier intersection, or by token match. A defect
    one call deeper is invisible to all of them. Measured on the anyhow tree
    2026-08-05: `test_boxed_str` names `downcast_ref`, `error` and `from`; the
    fn that actually holds the defect is `error.rs::construct_from_boxed`
    (it builds the vtable with `object_downcast::<BoxedError>` where the upstream
    crate uses `object_downcast::<Box<dyn StdError + Send + Sync>>`), and it
    enters at hop 3. Re-measured 2026-08-06 by running this function over the
    merged tree from that 3-fn seed: 37 keys, distributed 3 / 14 / 9 / 11 over
    hops 0-3, and hop 4 adds 2 more and finds nothing new.

    The hop distance is returned rather than folded in because it is the
    ranking: a caller that pays a model call per key wants the seed first and
    the far edges last, and with an early stop on success it may never reach
    them. `cap` bounds the walk; the convicting tests of a callee are the
    caller's, since that is the evidence that led here.
    """
    hops: dict[FnLocator, int] = dict.fromkeys(frontier, 0)
    if max_hops < 1 or not frontier:
        return dict(frontier), hops
    recs = deps.structure.enumerate_fns(src_dir) or []
    by_name: dict[str, list[FnLocator]] = {}
    # EVERY occurrence's body, not the first. A frontier key is (file, bare
    # name) and `error.rs::from` is four different fns; reading only the first
    # would have the walk step out of `impl From<E> for Error` and never see
    # `From<Box<dyn StdError + Send + Sync>> for Error` call `from_boxed`, which
    # is the edge that leads to the defect. Reachability is a union over the
    # definitions a key can stand for.
    bodies_of: dict[FnLocator, list[tuple[str, int, int]]] = {}
    for r in recs:
        name, f = r.get("fn_name", ""), r.get("file", "")
        if not (name and f) or is_test_fn_record(r):
            continue
        key = FnLocator(f, name)
        if key not in bodies_of:
            by_name.setdefault(name, []).append(key)
        bodies_of.setdefault(key, []).append(
            (f, r.get("body_byte_start", 0), r.get("body_byte_end", 0)))
    text_of: dict[str, bytes] = {}

    def _body(key: FnLocator) -> str:
        parts: list[str] = []
        for f, s, e in bodies_of.get(key, ()):
            if not f or e <= s:
                continue
            if f not in text_of:
                try:
                    text_of[f] = (src_dir / f).read_bytes()
                except Exception:
                    text_of[f] = b""
            parts.append(text_of[f][s:e].decode("utf-8", "replace"))
        return "\n".join(parts)

    out: Frontier = {k: set(v) for k, v in frontier.items()}
    current = list(frontier)
    for hop in range(1, max_hops + 1):
        nxt: list[FnLocator] = []
        for key in current:
            if len(out) >= cap:
                break
            for ident in identifiers_in(_body(key)):
                for cand in by_name.get(ident, ()):
                    if cand in out or len(out) >= cap:
                        continue
                    out[cand] = set(out.get(key) or ())
                    hops[cand] = hop
                    nxt.append(cand)
        if not nxt:
            break
        current = nxt
    return out, hops


def attribute_failures(
    deps: AttributionDeps,
    test_output: str,
    src_dir: Path,
    recently_filled: list[FnLocator],
    test_reverse_map: dict[str, set[FnLocator]],
    cluster_name: str,
    test_texts: dict[str, str] | None = None,
    extra_failed: frozenset[str] = frozenset(),
    unrepairable_tests: frozenset[str] = frozenset(),
) -> AttributionResult:
    """Robust failure-to-fn attribution chain. Returns
    (frontier, kind, convicting): the dispatch map, the path that produced it,
    and the direct-evidence subset that may revert a unit (paths 1-3 only —
    see `FailureKind.convicts`). Callers pass the result through
    `_filter_test_targets` — same as the fix dispatchers — so a
    test-oracle fn is never spliced.
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

    The `#[test]`-oracle filter is applied to EACH path's frontier
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
    # tolerated for DISPATCH (splice guards reject non-stub overwrites;
    # behavioral re-fill WANTS the non-stub keys) — never for acceptance, which
    # is why paths 1-3 also build `convicting` and 4-6 do not. The classifier
    # (4) and the coarse fallbacks (5,6) stay fallbacks — reached only if 1-3
    # are empty.
    contributors: list[FailureKind] = []

    def _repairable(names: Iterable[str]) -> set[str]:
        """Drop the failures no body rewrite can satisfy — a `trybuild` harness
        asserts about rustc's own output, so nothing it names is answerable by
        writing a body.

        Applied at EVERY path, not only the fallbacks. The first cut filtered
        inside the `_failing()` helper, which paths 4-6 call and paths 1-3 do
        not — and path 3 is the one that CONVICTS, so an exclusion that skips it
        leaves the worst case uncovered."""
        if not unrepairable_tests:
            return set(names)
        return {n for n in names
                if n.split("::")[-1] not in unrepairable_tests}

    def _failing() -> list[str]:
        """The failing tests this loop can act on, for the fallback paths."""
        return sorted(_repairable(parse_failed_tests(test_output) | extra_failed))

    _vanished = _repairable(extra_failed)

    merged: Frontier = {}
    convicting: Frontier = {}

    def _merge(add: Frontier, kind: FailureKind) -> None:
        if add:
            contributors.append(kind)
            for k, v in add.items():
                merged.setdefault(k, set()).update(v)
                if kind.convicts:
                    convicting.setdefault(k, set()).update(v)

    _merge(_strip(extract_tdd_frontier(deps, test_output, src_dir)),
           FailureKind.RUNTIME)
    _merge(_strip(extract_tdd_compile_frontier(deps, test_output, src_dir)),
           FailureKind.COMPILE)
    if test_texts:
        _bf, _srcs = behavior_frontier_from_texts(
            deps, test_output, test_texts, src_dir, unrepairable_tests)
        _merge(_strip(_bf), FailureKind.BEHAVIOR)
    # Vanished tests (`extra_failed` — expected but absent from the output:
    # their binary segfaulted or stopped compiling) leave NO panic line, no
    # compile error, no behavioral evidence, so paths 1-3 can never see them
    # — and when those paths are non-empty the fallbacks below never run.
    # Route them through the static reverse map HERE, unioned like 1-3, or a
    # crashing filled body is never re-dispatched while ordinary stub panics
    # keep the merged frontier non-empty.
    if _vanished and test_reverse_map:
        vanished_fr: Frontier = {}
        for tname in sorted(_vanished):
            bare = tname.split("::")[-1]
            cands = test_reverse_map.get(bare) or test_reverse_map.get(tname) or set()
            for key in cands:
                vanished_fr.setdefault(key, set()).add(tname)
        _merge(_strip(vanished_fr), FailureKind.REVERSE_MAP)
    if merged:
        # One contributor → keep its kind (preserves per-path stats); several →
        # MERGED so the union is visible in logs/events.
        kind = contributors[0] if len(contributors) == 1 else FailureKind.MERGED
        return AttributionResult(merged, kind, convicting)
    # 4. LLM classifier — turn off via PHASE_C_TDD_USE_CLASSIFIER=0.
    if os.getenv("PHASE_C_TDD_USE_CLASSIFIER", "1") != "0":
        failing = _failing()
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
                return AttributionResult(synth, FailureKind.CLASSIFIER, {})
    # 5. Recently-filled fallback. If new assertion failures appeared
    # after recent fills, the recent fills are the most likely cause.
    # Queue them in reverse-filled order (newest first).
    if recently_filled:
        failing = _failing()
        if failing:
            recent_synth: Frontier = {}
            for key in reversed(recently_filled):
                recent_synth.setdefault(key, set()).update(failing)
            recent_synth = _strip(recent_synth)
            if recent_synth:
                return AttributionResult(
                    recent_synth, FailureKind.RECENTLY_FILLED, {})
    # 6. Test-reverse-map. For each failing test, queue every fn it
    # could reach according to the static reverse map. Over-attribution
    # is tolerable here — per-fn splice guards reject overwriting
    # non-stub bodies, so already-correct fills aren't clobbered.
    if test_reverse_map:
        failing = _failing()
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
                return AttributionResult(
                    reverse_synth, FailureKind.REVERSE_MAP, {})
    return AttributionResult({}, FailureKind.NO_FRONTIER, {})


# --- Futility escalation (2026-07-10) ------------------------------
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
    """Fns that PRODUCE one of `type_names`: hand the value back (`-> Type`, or
    `-> Self` from that type's own impl) or build it literally (`Type { … }`).
    Token containment scan — no regex on source.

    Calling an associated item (`Type::foo()` in a body) is deliberately NOT a
    producer: that is how CONSUMERS use a type, and matching it turned an
    escalation meant to name a handful of constructors into a third of the
    crate — including the `#[test]` fns that call `Error::msg` on the way to
    asserting something.

    The matches are on TOKENS (`domain.type_production`), which the docstring
    above claimed before the code did: `tn in ret` made the one-letter `E` of
    `impl<E> StdError for E` match `-> Result<T, Error>`, and `f"{tn} {{"`
    made `Error` match `MyError { … }`."""
    exclude = exclude or set()
    out: list[FnLocator] = []
    seen: set[FnLocator] = set()
    for r in fn_records:
        key = FnLocator(r.get("file", ""), r.get("fn_name", ""))
        if key in exclude or key in seen or not key[0] or not key[1]:
            continue
        sig = r.get("sig_text", "") or ""
        body = r.get("body_text", "") or ""
        own_type = container_type_name(r.get("container", "") or "")
        for tn in type_names:
            if not tn:
                continue
            if (returns_type(sig, tn, own_type=own_type)
                    or constructs_type(body, tn)):
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
    missing input.

    The subject types are narrowed to the ones this crate DECLARES as struct or
    enum (`domain.type_production.ProducibleTypes`). A generic parameter, a
    foreign type and a type alias each answer "who produces this?" with the
    whole crate, and on the live tree the unfiltered subject set was
    ``{E, Option, Result, Box, bool, …}`` — see that module for the measured
    sizes.

    Failing to READ the crate's types is reported as itself, never as an answer
    about the crate. Both are silence for the escalation, and treating them as
    one cost three days: a reader that could not answer `enumerate_types` raised
    `AttributeError` into a blanket `except`, and 99 refusals then said
    ``['Error'] name no type this crate declares`` about a crate that declares
    `Error`. A guard whose read failure is indistinguishable from its verdict
    cannot be caught by reading the log it writes."""
    try:
        recs = deps.structure.enumerate_fns(src_dir) or []
    except Exception:
        recs = []
    types_unreadable = ""
    try:
        producible = ProducibleTypes.from_type_records(
            deps.structure.enumerate_types(src_dir))
    except Exception as e:
        producible = EMPTY_PRODUCIBLE
        types_unreadable = f"{type(e).__name__}: {e}"
    containers = {(r.get("file", ""), r.get("fn_name", "")): r.get("container", "")
                  for r in recs}
    candidates = {container_type_name(containers.get((k[0], k[1]), ""))
                  for k in futile_units}
    candidates.discard("")
    type_names = set(producible.filter(candidates))
    if not type_names:
        if candidates and types_unreadable:
            # Says nothing about the candidates: they were never tested against
            # anything. The escalation is disarmed until this read works.
            deps.log(f"  [attribution/escalate] {test_name}: not escalating — "
                     "COULD NOT READ this crate's type declarations "
                     f"({types_unreadable}); {len(candidates)} candidate "
                     "subject(s) went untested. This is a pipeline defect, not "
                     "a fact about the crate.")
        elif candidates:
            deps.log(f"  [attribution/escalate] {test_name}: not escalating — "
                     f"{sorted(candidates)} name no type this crate declares "
                     "(generic parameter, foreign type or alias)")
        return {}
    producers = producers_for_types(recs, type_names, exclude=futile_units)
    if not producers:
        return {}
    dropped = sorted(candidates - type_names)
    deps.log(f"  [attribution/escalate] {test_name}: accessors of "
             f"{sorted(type_names)} are futile — escalating to "
             f"{len(producers)} producer fn(s): "
             f"{', '.join(f'{f}::{fn}' for f, fn in producers[:5])}"
             + ("..." if len(producers) > 5 else "")
             + (f" (not escalated: {dropped})" if dropped else ""))
    return {key: {test_name} for key in producers}
