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
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
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
    FormatterPair,
    as_dispatch,
    canon_site,
    container_key,
    lift_sites,
    line_to_fn_record,
    record_site,
)
from pipeline_core.domain.coverage_slice import unreached_by_test
from pipeline_core.domain.failing_observable import observable_source
from pipeline_core.domain.format_traits import format_traits_used
from pipeline_core.domain.impl_routing import norm_src_rel
from pipeline_core.domain.language_driver import language_driver
from pipeline_core.domain.models import (
    Diagnostic,
    FnLocator,
    FnRecord,
    FnSite,
    Frontier,
    SiteFrontier,
)
from pipeline_core.domain.oracle_immutability import is_oracle_path
from pipeline_core.domain.rust_idents import (
    ascii_word_tokens,
    canon_container,
    impl_self_type,
    impl_trait_name,
    is_compile_fail_test_source,
    test_fn_names,
)
from pipeline_core.domain.rust_lex import code_mask
from pipeline_core.domain.test_report import FailureKind as TestFailureKind
from pipeline_core.domain.test_report import (
    failure_evidence_by_test,
    parse_test_output,
)
from pipeline_core.domain.test_results import (
    build_phase_output,
    build_test_name_index,
    failing_test_names,
    identifiers_in,
    parse_failed_tests,
    parse_rustc_compile_errors,
)
from pipeline_core.domain.type_production import (
    ProducibleTypes,
    called_method_names,
    constructs_type,
    returns_type,
    trait_impl_fns_named,
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
    project the resolved record onto their own identity type.

    The cache is per closure. A splice that moves which container owns a
    line requires a NEW locator — ``fill_impl_blocks`` builds one at each
    post-splice ``focus_diagnostics`` call. Reusing one closure across
    splices overrode ``container_line_window`` with a pre-splice hit.
    """
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


def make_untouched_arm_probe(
    structure: Any, src_dir: Path,
) -> Callable[[str, int], bool]:
    """``(panic_file, panic_line) -> bool`` — the line lies in a definition
    that still holds the skeleton's whole-body stub (the extractor's
    `has_unimplemented_body`) while another definition of the same site
    (file, name, container; complementary `#[cfg]` arms share one) holds a
    real body. That definition is a body no write of the site has produced
    yet, so the keep gate's partition counts its stub panic as a still-stubbed
    sibling's (`test_report.UnitScope`). A site with one definition never
    answers True, so a reply that is itself a stub stays the unit's own
    failure. Read on the tree after the splice, like the site locator."""
    core = _make_record_locator(structure, src_dir)
    by_site: dict[tuple[str, str, str], list[dict[str, Any]]] | None = None

    def _sites() -> dict[tuple[str, str, str], list[dict[str, Any]]]:
        nonlocal by_site
        if by_site is None:
            by_site = {}
            for r in structure.enumerate_fns(src_dir) or []:
                key = (r.get("file", ""), r.get("fn_name", ""),
                       canon_container(r.get("container", "") or ""))
                by_site.setdefault(key, []).append(r)
        return by_site

    def probe(panic_file: str, panic_line: int) -> bool:
        hit = core(panic_file, panic_line)
        if hit is None:
            return False
        rel, rec = hit
        if not rec.get("has_unimplemented_body"):
            return False
        arms = _sites().get(
            (rel, rec["fn_name"],
             canon_container(rec.get("container", "") or "")), [])
        return any(not a.get("has_unimplemented_body") for a in arms)

    return probe


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
) -> tuple[Frontier, SiteFrontier]:
    """Parse cargo test output → (dispatch map, site-precise convicting).

    Dispatch keys are (file_rel, fn_name). Site keys keep the container the
    panic landed in, so a RUNTIME hit at ``construct_from_boxed`` does not
    convict a ``from`` site. Built on the shared structured parser
    (`parse_test_output`): only STUB_PANIC failures are included.

    Returns ``({}, {})`` when no stub panics resolve (build failed before any
    test ran, or every failure is an assertion). Callers that want dispatch
    only use :func:`as_dispatch`.
    """
    frontier: Frontier = {}
    sites: SiteFrontier = {}
    if not test_output:
        return frontier, sites
    locate = make_fn_site_locator(deps.structure, src_dir)
    for f in parse_test_output(test_output).failures:
        if f.kind is not TestFailureKind.STUB_PANIC:
            continue
        if not (f.panic_file and f.panic_line is not None):
            continue
        site = locate(f.panic_file, f.panic_line)
        if site is None:
            continue
        site = canon_site(site)
        loc = FnLocator(site.file, site.fn_name)
        frontier.setdefault(loc, set()).add(f.test_name)
        sites.setdefault(site, set()).add(f.test_name)
    return frontier, sites

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
    fr, _sites, srcs = behavior_frontier_from_texts(
        deps, test_output, texts, project_dir)
    return fr, srcs


def _code_only(text: str) -> str:
    """`text` with every string/char-literal and comment span blanked to
    spaces, so an identifier scan sees only what the code NAMES. An
    expected-output constant, an env-var name (`RUST_BACKTRACE`) or a doc
    comment mentions tokens without calling anything: on run
    `114436-9cb17382` the failing Debug tests owned a fn named `backtrace`
    solely through `std::env::set_var("RUST_BACKTRACE", …)` and the asserted
    substring `Stack backtrace`, and that ownership fed a futility escalation
    toward that accessor's producers."""
    mask = code_mask(text)
    return "".join(ch if not m else " "
                   for ch, m in zip(text, mask, strict=False))


def _impl_trait_and_self(container: str) -> tuple[str, str] | None:
    """``('Debug', 'Error')`` for a trait-impl header, read by the domain's own
    parsers (`impl_trait_name`, `impl_self_type`) — the same ones the producer
    ranking and the retrospectives store read, so an HRTB binder, a
    `for dyn …` self type or a where-clause parses here exactly as it parses
    there rather than through a second ``split(" for ")`` that agreed with
    them only on the shapes the live extractor happens to emit. ``None`` for
    an inherent impl or a non-impl."""
    trait = impl_trait_name(container)
    self_ty = impl_self_type(container)
    if not trait or not self_ty:
        return None
    return trait, self_ty


def _qualified_calls(body_text: str) -> frozenset[tuple[str, str]]:
    """Every `Qualifier::method` token pair in a body.

    `unsafe { ErrorImpl::display(r, f) }` yields `('ErrorImpl', 'display')`;
    `Self::debug(r, f)` yields `('Self', 'debug')`, which the caller
    resolves to the dispatcher's own self-type. Std-trait dispatch
    (`Display::fmt(x, f)`) yields `('Display', 'fmt')` and is dropped
    downstream because the qualifier is matched against a fn record's
    SELF-TYPE (`impl_self_type` of its container), never against any token of
    the container: `Display`/`Debug` are std traits, the self-type of no crate
    impl, so they match no record. A path `core::fmt::Display::fmt` yields
    one pair per `::` (`Display`/`fmt`, `fmt`/`Display`, `core`/`fmt`); the
    same self-type match drops every one that is not a crate type."""
    out: set[tuple[str, str]] = set()
    pos = 0
    n = len(body_text)
    while True:
        pos = body_text.find("::", pos)
        if pos == -1:
            break
        k = pos + 2
        while k < n and (body_text[k].isalnum() or body_text[k] == "_"):
            k += 1
        method = body_text[pos + 2:k]
        j = pos - 1
        while j >= 0 and body_text[j] in " \t":
            j -= 1
        end = j + 1
        while j >= 0 and (body_text[j].isalnum() or body_text[j] == "_"):
            j -= 1
        qual = body_text[j + 1:end]
        # method must be a whole token (call/path), not a prefix
        if qual and method and not (k < n and (body_text[k].isalnum()
                                               or body_text[k] == "_")):
            out.add((qual, method))
        pos = k if k > pos + 2 else pos + 2
    return frozenset(out)


def _format_impl_calls(
    body_text: str,
    methods: tuple[str, ...] | None = None,
) -> frozenset[tuple[str, str]]:
    """`_qualified_calls` restricted to `methods`.

    When `methods` is omitted, the bound language driver supplies the
    follow set: catalog is `fmt`/`debug` (the pre-complement hop — a
    Qual::display body is empty under it, run `3985224-5484d810`);
    generic is `fmt` only. Production passes
    `_follow_methods_for_trait(trait)` and does not use the default.
    """
    want = frozenset(methods if methods is not None
                     else language_driver().format_follow_default())
    return frozenset(
        (q, m) for q, m in _qualified_calls(body_text) if m in want)


def _called_qualifier_complements(
    trait: str, body_text: str,
) -> frozenset[tuple[str, str]]:
    """The crate method this dispatcher's trait still needs, of a qualifier
    the body already names.

    A body that calls `Qual::anything` has named `Qual`. The crate method
    the hop already searches for a format-trait dispatcher is
    `trait.lower()` (`Debug` → `debug`, `Display` → `display`) — the same
    mapping `format_traits_used` already decided the test needs, not a
    per-keyword branch. If that call is absent, the default follow sees
    nothing and the session never gains the implementer: run
    `3985224-5484d810` (a Debug dispatcher whose body was a Qual::display
    call; default parse empty). A Display dispatcher that currently calls
    `Qual::debug` complements `display` the same way. A dispatcher that
    already calls the wanted method, or that names no qualifier, yields
    nothing."""
    want = trait.lower() if trait else ""
    if not want:
        return frozenset()
    called = _qualified_calls(body_text)
    quals = {q for q, _m in called}
    have = {q for q, m in called if m == want}
    return frozenset((q, want) for q in quals if q not in have)


def _follow_methods_for_trait(trait: str) -> tuple[str, ...]:
    """Attribution follow: the std format method `fmt`, plus the crate
    method named for this trait (`debug`, `display`). A Display dispatcher
    that currently calls `Qual::display` is no longer self-contained."""
    if trait:
        return ("fmt", trait.lower())
    return ("fmt",)


def _same_src_file(a: str, b: str) -> bool:
    return a == b or norm_src_rel(a) == norm_src_rel(b)


def _is_inherent_impl(container: str) -> bool:
    """True for `impl Type` / `impl Type<T>` (no trait). False for
    `impl Trait for Type` and for a free-fn empty container.

    The hop follows a crate qualifier to this shape (`impl Inner { fn debug }`).
    A dispatcher that names no qualifier still needs the same method, and
    the trait-impl `fmt` sites are dispatchers themselves — they are not
    this helper."""
    c = (container or "").strip()
    if not c.startswith("impl"):
        return False
    return _impl_trait_and_self(c) is None


def _inherent_methods_named(
    method: str,
    recs: Iterable[Mapping[str, Any]],
    *,
    exclude_container: str = "",
) -> list[tuple[FnLocator, dict[str, Any]]]:
    """Inherent-impl records whose fn is `method` (`trait.lower()`).

    Run `511277-e4cf5ade` (2026-09-10): the Debug dispatcher collected a
    chain and called `Display::fmt`, so `_called_qualifier_complements`
    offered only std-trait tokens and the hop recorded no pair; the
    inherent `debug` on the other file stayed dead and a FILE pull of
    that file admitted nothing. Method name is the same mapping the hop
    already uses; inherent-impl is the filter that keeps `impl Trait for
    Type { fn fmt }` out. No trait name is special-cased."""
    if not method:
        return []
    want_excl = container_key(exclude_container) if exclude_container else ""
    out: list[tuple[FnLocator, dict[str, Any]]] = []
    seen: set[FnLocator] = set()
    for rec in recs:
        if str(rec.get("fn_name") or "") != method:
            continue
        icont = str(rec.get("container") or "")
        if want_excl and container_key(icont) == want_excl:
            continue
        if not _is_inherent_impl(icont):
            continue
        key = FnLocator(str(rec["file"]), method)
        if key in seen:
            continue
        seen.add(key)
        out.append((key, dict(rec)))
    return out


def admit_format_siblings_for_file(
    file_rel: str,
    editable: Mapping[FnSite, FnRecord],
    tree: Sequence[FnRecord],
) -> tuple[FnRecord, ...]:
    """Units in `file_rel` a format dispatcher in `editable` still needs,
    that the session cannot yet write.

    Two ways a dispatcher names that unit: a crate qualifier the body
    already wrote (`Qual::display` while the trait wants `debug`), or —
    when the body named no crate qualifier — the inherent method
    `trait.lower()` on the tree. The pair hop records both at plan time
    when the dispatcher is in the slice. A FILE request is the backstop
    for a session that opened without them (run `164454-8ec9c4d4`: the
    first turn asked for `src/fmt.rs` and the write set did not grow;
    run `511277-e4cf5ade` asked again and complements were std-trait
    tokens, so admission stayed empty). Same mapping as the hop: no
    trait name is special-cased. Empty when the file holds no such
    method, or every one is already editable."""
    already = set(editable)
    out: list[FnRecord] = []
    seen: set[FnSite] = set()

    def _take(r: FnRecord) -> None:
        site = record_site(r)
        if site in already or site in seen:
            return
        seen.add(site)
        out.append(r)

    for rec in editable.values():
        parsed = _impl_trait_and_self(rec.container)
        if parsed is None:
            continue
        trait, self_ty = parsed
        for qual, method in _called_qualifier_complements(trait, rec.body_text):
            if qual == "Self":
                qual = self_ty
            for r in tree:
                if not _same_src_file(r.file, file_rel):
                    continue
                if r.fn_name != method:
                    continue
                if qual != (impl_self_type(r.container) or ""):
                    continue
                _take(r)
        want = trait.lower() if trait else ""
        if want:
            for r in tree:
                if not _same_src_file(r.file, file_rel):
                    continue
                if r.fn_name != want:
                    continue
                if not _is_inherent_impl(r.container or ""):
                    continue
                _take(r)
    return tuple(out)


def _attribute_format_only_test(
    tname: str, tsrc: str, file_text: str,
    by_name: dict[str, list[dict[str, Any]]], frontier: Frontier,
    sites: SiteFrontier,
    log: Callable[[str], None],
    owned: bool = False,
    pairs_out: set[FormatterPair] | None = None,
    observed: str | None = None,
) -> None:
    """Give a frontier owner to a failing test whose only crate contact is a
    formatting macro (`format!("{:#?}", v)`), which names no crate fn — so
    `identifiers_in(tsrc)` meets nothing, the test owns no unit, is never
    dispatched, and no iteration can move it. Measured on `test_altdebug`,
    every sweep of the 98-plateau (runs `4911-9ab8852e` / `95878-aaf5bf1d`,
    `fail=7 frontier=6`): 0 of 68 393 fill prompts in the project's history
    ever carried it as an attributed test.

    The candidate is the crate's `fmt` method for the trait the test's
    placeholder needs (`{:#?}` -> `Debug`) whose SELF-TYPE is named in the test
    FILE — a `{:#?}`-on-`Error` test attributes toward `Debug for Error`
    (`src/error.rs`) and never `Debug for MessageError`, which it never names.

    But the matched `fmt` body is often NOT the defect — it is the dispatcher.
    Attribution follows `Qual::fmt` and `Qual::<trait.lower()>` (`debug` for
    a Debug dispatcher, `display` for a Display dispatcher). When the body
    currently calls a different method of the same qualifier, that follow
    is empty and the session never gained the implementer (run
    `3985224-5484d810`: `impl Debug for Error` was `ErrorImpl::display(...)`;
    `_format_impl_calls` of `fmt`/`debug` returned empty).
    `_called_qualifier_complements` is that misshape, for any format trait:
    same qualifier, method `trait.lower()`. A dispatcher that names no
    crate qualifier at all (chain collect + std `Display::fmt`, run
    `511277-e4cf5ade` 2026-09-10) is the same misshape one step earlier:
    complements are std-trait tokens, no record's self-type matches, the
    hop recorded no pair, and the inherent method sat dead. Escalating
    the `fmt` accessor cannot reach that method — the futility escalation
    adds fns that construct/return the accessor's container type, and
    the inherent helper returns `fmt::Result` — and `FnLocator` erases
    the container, so `file::fmt` dispatch is coarser than the trait impl
    anyway. So follow ONE hop: if the matched `fmt` body calls a crate
    formatting method of the trait, or names a qualifier whose
    trait-named method exists on the tree and is not yet called, or the
    tree holds an inherent method named `trait.lower()` the body does
    not call, attribute THAT implementer — the body the verdict actually
    pins. Only when the matched `fmt` calls no sibling formatter and the
    tree has no such inherent method (it IS the implementer) is the
    `fmt` itself attributed.

    `owned=False` is the original constituency: a test the primary attribution
    left unowned, for which this adds the only owner it will ever have.
    `owned=True` is a test that DOES name crate fns and still renders through a
    format trait — its identifier owners (a fixture constructor, `.context()`)
    cannot change what `{:?}` emits, so the implementer is attributed BESIDE
    them; without that, the futility escalation asks who produces the
    accessors' container and buys the constructor family instead of the
    formatter (run `114436-9cb17382`, the 135/137 plateau). Additive in both
    modes: an owner is only ever added, never removed or replaced.

    THE HOP'S THEORY IS MEASURABLE, AND THE MEASUREMENT WINS. "The
    implementer is the body the verdict actually pins" assumes the
    dispatcher delegates on the failing test's arm; a dispatcher that
    delegates only under `f.alternate()` refutes that for a `{:?}` test, and
    the sweep's own owner map says so — run `358833-aa15d93c` (2026-09-01)
    printed the attribution to `src/fmt.rs::debug` two lines above `red-dead
    … executed by no FAILING test: src/fmt.rs::debug`, every sweep, while
    the refuted owner kept eating file-first attempts and the per-impl fix
    loop. So each followed implementer is screened against the BOUND
    coverage reading (`unreached_by_test` — the previous sweep's, since
    attribution runs before this sweep's bind): positively proven unentered
    drops it, and when every implementer drops, the matched dispatcher —
    the body coverage shows the test DOES pin — is attributed instead. The
    dispatcher is screened by the same rule: when the reading proves the
    test enters it neither, the hop attributes nothing for that test — the
    renderer runs only while the failure is reported. An unmeasured test,
    an unbound reading or a crashed binary changes nothing (the
    conservative rule refuses only on positive evidence).

    `pairs_out` collects the dispatcher/implementer pairs the hop resolved
    (`FormatterPair`, container-qualified), in BOTH directions — the pair is
    what the cross-file pair-fill route dispatches as one transaction. A
    pair is recorded for a test unless the reading proves the test enters
    neither half: a pair no failing test runs is a transaction with no
    verdict to move, and the measured-owners withholding and the
    schedule's pair wait would otherwise hold the fill behind it."""
    # The traits of what the test FAILS on (`domain.failing_observable`),
    # not of every format token in its source: a test that renders a value
    # on a passing line and then fails on a `None` downcast is not a format
    # test (run `3076-051d385a`, `tests/test_boxed.rs:26:14`). `observed`
    # None is ignorance and reads the whole source, as before.
    traits = format_traits_used(observed if observed is not None else tsrc)
    # What the traits were read from, on every line the hop prints: 341's
    # offline reading of eleven `format_err_else_branch_*` hop lines could
    # not tell whether the hop had read the failing line or the whole test.
    _obs_one = " ".join((observed or "").split())
    _read_from = (f"read from the failing observable `{_obs_one[:120]}`"
                  if observed is not None else
                  "read from the whole test source (no failing observable)")
    fmt_recs = by_name.get("fmt")
    if not traits or not fmt_recs:
        return
    # Scope self-type matching to identifiers in the file's CODE, not its
    # string/char literals: a `{:#?}` test's EXPECTED-output constant quotes
    # `Error { ... }`, `Custom { ... }` and other type names, and matching
    # those would attribute the test to every Debug impl whose type is merely
    # printed in an expected string (4 spurious `fmt` dispatchers on the live
    # `test_fmt.rs`). `code_mask` masks string/char/comment spans, so the only
    # survivors are types the test's real code names (`io::Error` etc.).
    file_idents = identifiers_in(_code_only(file_text))
    added: set[FnLocator] = set()
    # Implementers the bound coverage reading positively proves this test
    # never enters — the redirect the final log line names.
    redirected: list[FnLocator] = []
    # Dispatchers the same reading proves this test never enters before its
    # failure: attributed to nobody, reported on their own line.
    screened: list[FnLocator] = []

    def _add(key: FnLocator, rec: dict[str, Any] | None = None) -> None:
        if tname not in frontier.get(key, set()):
            added.add(key)
        frontier.setdefault(key, set()).add(tname)
        cont = str(rec.get("container", "") or "") if rec is not None else ""
        sites.setdefault(canon_site(FnSite(key.file, key.fn_name, cont)),
                         set()).add(tname)

    for rec in fmt_recs:
        parsed = _impl_trait_and_self(str(rec.get("container", "")))
        if parsed is None:
            continue
        trait, self_ty = parsed
        if not (trait in traits and self_ty in file_idents):
            continue
        # The dispatcher faces the same measured screen as every implementer
        # it delegates to: `unreached_by_test` reads the bound, panic-cut
        # owner map, so a test whose only contact with this renderer is its
        # failure being printed (`unwrap` on an `Err` formats Debug before
        # the panic) does not enter it. Run `92187-f71f12bf` (2026-09-22)
        # attributed five failing tests to a dispatcher the same sweep
        # listed as executed by no failing test, and the pair it formed
        # held 13 other runs of the fill behind it.
        disp = FnLocator(rec["file"], "fmt")
        disp_unentered = unreached_by_test(disp, tname)
        # Follow the dispatcher's call to its real formatting implementer.
        # A dispatcher that currently calls a different method of the same
        # qualifier is the implementer-not-yet-called case: fold in
        # Qual::<trait.lower()> so the pair hop can add it (empty follow
        # otherwise). No trait name is special-cased.
        implementers: list[tuple[FnLocator, dict[str, Any]]] = []
        body_text = str(rec.get("body_text", "") or "")
        calls = (_format_impl_calls(body_text, _follow_methods_for_trait(trait))
                 | _called_qualifier_complements(trait, body_text))
        for qual, method in calls:
            if qual == "Self":
                # `Self::debug(r, f)` inside `impl Debug for Error` names the
                # dispatcher's own self-type: resolve it before matching.
                qual = self_ty
            for irec in by_name.get(method, []):
                icont = str(irec.get("container", ""))
                # Match the call's QUALIFIER against the record's SELF-TYPE, not
                # any container token: `ErrorImpl::debug` -> the `debug` on
                # `impl ErrorImpl`; a trait-dispatch `Display::fmt` names the
                # trait, which is no fn's self-type, so it is correctly ignored
                # (else it would match every `impl Display for X`).
                if (icont != str(rec.get("container", ""))
                        and qual == (impl_self_type(icont) or "")):
                    implementers.append((FnLocator(irec["file"], method), irec))
        # Body named no crate qualifier: still pair with the inherent
        # method `trait.lower()` the Qual follow would have taken.
        if not implementers:
            implementers.extend(_inherent_methods_named(
                trait.lower() if trait else "",
                by_name.get(trait.lower() if trait else "", ()),
                exclude_container=str(rec.get("container", "") or "")))
        if implementers:
            # Canonical from construction: the session's owner and site
            # frontiers are `canon_site`d, and a raw extractor header
            # (`impl Debug for ContextError < C , E >`) never equals its
            # canonical form, so a pair built raw matched no slice member
            # for any generic container (40 of the live nightly tree's
            # containers differ under `container_key`, 2026-09-03).
            _disp_site = record_site(rec)
            seen: set[FnLocator] = set()
            kept_impls: list[tuple[FnLocator, dict[str, Any]]] = []
            unentered: list[FnLocator] = []
            for k, irec in implementers:
                if k in seen:
                    continue
                seen.add(k)
                impl_unentered = unreached_by_test(k, tname)
                # A pair is a joint write for a test that runs it: when the
                # bound reading proves the test enters NEITHER half before
                # its failure, the pair has no customer here.
                if pairs_out is not None and not (
                        impl_unentered and disp_unentered):
                    pairs_out.add(FormatterPair(_disp_site, record_site(irec)))
                if impl_unentered:
                    unentered.append(k)
                else:
                    kept_impls.append((k, irec))
            for k, irec in kept_impls:
                _add(k, irec)
            if unentered and not kept_impls:
                # Every followed implementer is measured-unentered by this
                # test: the delegation the theory assumed does not run on
                # the failing arm. The dispatcher is the measured owner —
                # when the same reading shows the test enters it.
                if disp_unentered:
                    screened.append(disp)
                else:
                    _add(disp, rec)
                redirected.extend(unentered)
        elif disp_unentered:
            screened.append(disp)
        else:
            # The matched fmt is self-contained — it IS the implementer.
            _add(disp, rec)
    if screened and not added:
        log(f"  [attribution/behavior] {tname}: renders through "
            f"{'/'.join(sorted(traits))} in its source, but the sweep's "
            "coverage reading proves it enters neither the dispatcher(s) "
            + ", ".join(sorted({f"{k.file}::{k.fn_name}" for k in screened}))
            + " nor their implementer(s) before its failure — no format "
            "owner and no pair: what renders it is the failure report "
            f"(traits {_read_from}).")
    if added:
        _why = ("no crate fn named in its source (formatting-macro test)"
                if not owned else
                "renders through a format trait its identifier owners cannot "
                "satisfy")
        log(f"  [attribution/behavior] {tname}: {_why} — attributed to "
            + ", ".join(f"{k.file}::{k.fn_name}"
                        for k in sorted(added, key=lambda k: (k.file, k.fn_name)))
            + f" via {'/'.join(sorted(traits))} ({_read_from}) on a self-type "
            "the test file names, following the dispatcher to its formatting "
            "implementer"
            + ("" if not redirected else
               " — REDIRECTED to the dispatcher: the sweep's coverage "
               "reading proves this test never enters "
               + ", ".join(f"{k.file}::{k.fn_name}"
                           for k in sorted(redirected)))
            + ("; without this the test owns no unit and no sweep can move it."
               if not owned else
               "; without this only the escalation could widen past the "
               "identifier owners, and it widens to their producers."))


def behavior_frontier_from_texts(
    deps: AttributionDeps,
    test_output: str, texts: dict[str, str], fns_root: Path,
    unrepairable_tests: frozenset[str] = frozenset(),
    pairs_out: set[FormatterPair] | None = None,
    observables_out: dict[str, str] | None = None,
    stub_located: Collection[str] = (),
) -> tuple[Frontier, SiteFrontier, dict[str, tuple[str, str]]]:
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
    name fns.

    `observables_out` collects, per failing test whose failure location
    falls inside its own fn, the text that failure is about
    (`failing_observable.observable_source`). Every question of the form
    "does this failure render through a format trait" reads it; a test
    absent from it is read on its whole source.

    A test already located as a stub panic (`extract_tdd_frontier`, plus
    `stub_located` when a caller already resolved the names) keeps the
    identifier owners below and is not given a format owner. The format
    hop reads the whole source when the failure has no observable, which
    is that panic, and the schedule then prefers the formatter and
    withholds the panic site (run `1680589-7a6824f8`, 2026-10-19)."""
    failed = failing_test_names(test_output)
    if unrepairable_tests:
        failed = [t for t in failed
                  if t.split("::")[-1] not in unrepairable_tests]
    if not failed or not texts:
        return {}, {}, {}
    # Derived here, not by each caller: a new caller of this function
    # cannot forget the skip. `stub_located` adds names a caller already
    # resolved against another tree.
    _located = set(stub_located)
    _rt, _rt_sites = extract_tdd_frontier(deps, test_output, fns_root)
    for _names in _rt.values():
        _located.update(_names)
    for _names in _rt_sites.values():
        _located.update(_names)
    _located_bare = {n.rsplit("::", 1)[-1] for n in _located}
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
    sites: SiteFrontier = {}
    test_srcs: dict[str, tuple[str, str]] = {}
    _words = failure_evidence_by_test(test_output, per_test_cap=4000)
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
        # Identifier scan over the test's CODE only. A token inside a string
        # or char literal is not a call: `std::env::set_var("RUST_BACKTRACE",
        # …)` and an asserted substring `Stack backtrace` made a Debug test
        # own a fn named `backtrace` (run `114436-9cb17382`), and that false
        # edge fed the futility escalation the producer set of an accessor
        # the test never calls. The prompt still quotes `tsrc` unmasked.
        idents = identifiers_in(_code_only(tsrc))
        test_srcs[tname] = (trel, tsrc)
        _observed = observable_source(
            texts[trel], bare, _words.get(tname) or next(
                (w for n, w in _words.items()
                 if n.rsplit("::", 1)[-1] == bare), ""), trel)
        if _observed is not None and observables_out is not None:
            observables_out[tname] = _observed
        _owned = False
        for fn_name in idents & set(by_name):
            recs = by_name[fn_name]
            for rec in recs:
                if len(recs) > 1 and not (
                        identifiers_in(rec.get("container", "")) & idents):
                    continue
                key = FnLocator(rec["file"], fn_name)
                frontier.setdefault(key, set()).add(tname)
                sites.setdefault(
                    canon_site(FnSite(rec["file"], fn_name,
                                      rec.get("container", "") or "")),
                    set()).add(tname)
                _owned = True
        if tname in _located or bare in _located_bare:
            # The runtime path already owns this test at the panic site.
            # Identifier owners above stay. The format hop does not run:
            # with no failing observable it would read the whole source
            # and hand the test to a formatter.
            continue
        if not _owned or format_traits_used(
                _observed if _observed is not None else tsrc):
            # A formatting-macro test names no crate fn and so owns no unit;
            # attribute it through the format trait it uses. Additive only —
            # and no longer gated on being unowned: a test whose real calls
            # are a fixture constructor plus `format!("{:?}", …)` owns the
            # constructor, but no constructor rewrite changes what the format
            # trait emits, so the implementer is attributed beside it (the
            # hop no-ops for a test that uses no format trait).
            _attribute_format_only_test(
                tname, tsrc, texts[trel], by_name, frontier, sites, deps.log,
                owned=_owned, pairs_out=pairs_out, observed=_observed)
    return frontier, sites, test_srcs

def extract_tdd_compile_frontier(deps: AttributionDeps,
    build_output: str, src_dir: Path,
) -> tuple[Frontier, SiteFrontier]:
    """When cargo test fails because cargo BUILD failed (compile errors,
    not panics), parse error[Exxxx] messages → fn records. Same shape as
    `_extract_tdd_frontier` but with test names replaced by "<compile>"
    so the caller can distinguish. Sites keep the container the diagnostic
    resolved to.

    Only the BUILD phase of the capture is read (`build_phase_output`): a
    diagnostic printed after a test binary announced its tests is something
    that test said, not this build failing. The sweep's red-build verdict
    already reads that cut; this reader did not, and every caller hands it the
    whole `cargo test` stream. Run `603120-24aabfc5` (2026-09-16, cluster
    `nightly`) swept a tree that compiled with 150 of 152 tests passing, yet a
    trybuild test inside the suite rebuilds the crate without the cluster's
    `--cfg`; reproduced on the tree that run left, its six `error[E0063]`
    lines after `running 1 test` resolve to six constructors under
    `<compile>`. The escalation fired
    on that sentinel as if it were a failing test, kept 21 units on the
    frontier, and the file route spent 30 minutes rewriting bodies whose tests
    all pass. A genuine red build runs no binary, so its capture is its build
    phase in full and nothing is lost here.
    """
    frontier: Frontier = {}
    sites: SiteFrontier = {}
    build_output = build_phase_output(build_output or "")
    if not build_output:
        return frontier, sites
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
        loc = FnLocator(rel, rec["fn_name"])
        site = canon_site(FnSite(rel, rec["fn_name"], rec.get("container", "") or ""))
        frontier.setdefault(loc, set()).add("<compile>")
        sites.setdefault(site, set()).add("<compile>")
    return frontier, sites

def _resolved_diag_records(
    diagnostics: list[Diagnostic], src_dir: Path,
    enumerate_fns: Any,
) -> list[tuple[str, dict[str, Any]]]:
    """(file_rel, fn record) for every span of `diagnostics` that resolves to
    a fn — the ONE span walk both consumers below share, so the two cannot
    drift. Per diagnostic: the primary span, falling back to the recorded
    macro invocation chain only when the primary fails to attribute (an error
    raised inside a macro expansion renders its primary span at the macro
    DEFINITION — `backtrace_if_absent!`'s two E0605s pointed at
    src/backtrace.rs:21 while the fillable culprits sat parsed in
    `macro_call_sites`, run `2514529-5e77dafb`, 2026-08-10); then every
    labelled secondary span that resolves to a DIFFERENT fn (a cross-file
    error names both of its ends and only one is primary — `E0592`,
    2026-08-11). A diagnostic whose primary attributes to nothing contributes
    nothing, secondaries included — byte-compatible with the frontier this
    walk was extracted from.
    """
    fn_records_by_file: dict[str, list[dict[str, Any]]] = {}
    file_text_cache: dict[str, str] = {}

    def _rec(rel: str, line_no: int) -> dict[str, Any] | None:
        if not rel or line_no < 1:
            return None
        if rel not in fn_records_by_file:
            _all_recs = enumerate_fns(src_dir) or []
            fn_records_by_file[rel] = [r for r in _all_recs
                                       if r.get("file") == rel]
        recs = fn_records_by_file[rel]
        if not recs:
            return None
        if rel not in file_text_cache:
            try:
                file_text_cache[rel] = (src_dir / rel).read_text(encoding="utf-8")
            except Exception:
                return None
        return line_to_fn_record(file_text_cache[rel], line_no, recs)

    out: list[tuple[str, dict[str, Any]]] = []
    for d in diagnostics or []:
        p_rel = d.file or ""
        rec = _rec(p_rel, int(d.line or 0))
        if rec is None:
            for m_file, m_line in d.macro_call_sites:
                rec = _rec(m_file or "", int(m_line or 0))
                if rec is not None:
                    p_rel = m_file or ""
                    break
        if rec is None:
            continue
        out.append((p_rel, rec))
        p_loc = (p_rel, str(rec.get("fn_name", "")))
        for s_file, s_line in d.secondary_spans:
            s_rel = s_file or ""
            s_rec = _rec(s_rel, int(s_line or 0))
            if (s_rec is not None
                    and (s_rel, str(s_rec.get("fn_name", ""))) != p_loc):
                out.append((s_rel, s_rec))
    return out


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
    for rel, rec in _resolved_diag_records(
            diagnostics, src_dir, deps.structure.enumerate_fns):
        frontier.setdefault(
            FnLocator(rel, rec["fn_name"]), set()).add("<compile>")
    return frontier


def implicated_fn_keys(deps: AttributionDeps,
    diagnostics: list[Diagnostic], src_dir: Path,
) -> frozenset[tuple[str, str, str]]:
    """(file, fn_name, one-line container) for every fn the diagnostics'
    spans resolve to — the PER-BODY implication identity the fn-granular
    peel keys on.

    `frontier_from_diags` projects the same resolution to
    `FnLocator(file, fn_name)`, which erases the container — and two impls
    legally share a method name. Keyed by name, the peel that met run
    `57025-6ba652f2`'s context.rs reply (six bodies over three names, two
    `ext_context` impls among them) dropped BOTH impls for every implicated
    name, slipped its all-implicated guard on the name-vs-body cardinality
    (3 != 6), re-applied the empty subset onto the pristine snapshot and
    returned the total loss as a peel win (`phase_c.file.fill.peeled`
    17:40:17 UTC: reverted six, kept 0, rounds 1) — deleting the first
    correct-shaped generic `ext_context` the project ever produced alongside
    its doomed concrete-self sibling. The span resolution has the container
    all along (`line_to_fn_record` returns the whole record); this keeps it.
    """
    return frozenset(
        (rel, str(rec.get("fn_name", "")),
         container_key(str(rec.get("container", "") or "")))
        for rel, rec in _resolved_diag_records(
            diagnostics, src_dir, deps.structure.enumerate_fns))

def compile_error_frontier(
    deps: AttributionDeps,
    output: str, project_dir: Path, check_cmd: str,
    env: dict[str, str], timeout: int,
) -> Frontier:
    """Structured-first compile-error frontier: re-run the derived
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
    return as_dispatch(extract_tdd_compile_frontier(deps, output, project_dir))

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
         intersection with fn records; runs BEFORE the LLM
         classifier; skipped when the caller has no test texts)
      4. LLM classifier — `_classify_phase_c_test_failures`
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
    convicting: SiteFrontier = {}
    merged_sites: SiteFrontier = {}
    # A test RUNTIME/COMPILE already located AT a site. BEHAVIOR may also
    # name that test on a different site (the four `from` impls share
    # `Error` with Debug asserts). Dispatch still unions both; conviction
    # does not — the locate() site owns the revert. Run `463713-b7c666c8`
    # (2026-08-28/29) otherwise convicted `From<Error> for Box<dyn …>` of
    # `test_boxed_str`, which panicked inside still-stubbed
    # `construct_from_boxed`.
    observed_at: dict[str, FnSite] = {}
    _src_map: dict[str, str] = {}

    def _unpack_pair(raw: Any) -> tuple[Frontier, SiteFrontier]:
        """Live extractors return ``(dispatch, sites)``. Tests monkeypatch
        a locator-only dict or a 2-tuple; lift those to empty-container
        sites so the RUNTIME-wins-the-test invariant still holds on locators."""
        if isinstance(raw, tuple) and len(raw) == 2:
            a, b = raw
            if (isinstance(b, dict) and b
                    and (isinstance(next(iter(b)), FnSite)
                         or (isinstance(next(iter(b)), tuple)
                             and len(next(iter(b))) == 3))):
                return a, b
            return a, lift_sites(a) if isinstance(a, dict) else {}
        if isinstance(raw, dict):
            return raw, lift_sites(raw)
        return {}, {}

    def _strip_sites(sm: SiteFrontier) -> SiteFrontier:
        if not _test_keys:
            return sm
        return {s: v for s, v in sm.items()
                if FnLocator(s.file, s.fn_name) not in _test_keys}

    def _merge(add: Frontier, kind: FailureKind,
               sites: SiteFrontier | None = None) -> None:
        if not add:
            return
        contributors.append(kind)
        for k, v in add.items():
            merged.setdefault(k, set()).update(v)
        site_map = sites if sites is not None else lift_sites(add)
        for site, v in site_map.items():
            merged_sites.setdefault(canon_site(site), set()).update(v)
        if not kind.convicts:
            return
        for site, v in site_map.items():
            site = canon_site(site)
            if kind is FailureKind.BEHAVIOR:
                kept = {t for t in v if observed_at.get(t, site) == site}
                if kept:
                    convicting.setdefault(site, set()).update(kept)
                continue
            convicting.setdefault(site, set()).update(v)
            for t in v:
                observed_at.setdefault(t, site)

    _rt_d, _rt_s = _unpack_pair(extract_tdd_frontier(deps, test_output, src_dir))
    _merge(_strip(_rt_d), FailureKind.RUNTIME, _strip_sites(_rt_s))
    _cc_d, _cc_s = _unpack_pair(
        extract_tdd_compile_frontier(deps, test_output, src_dir))
    _merge(_strip(_cc_d), FailureKind.COMPILE, _strip_sites(_cc_s))
    _hop_pairs: set[FormatterPair] = set()
    _observables: dict[str, str] = {}
    if test_texts:
        _bf_raw: Any = behavior_frontier_from_texts(
            deps, test_output, test_texts, src_dir, unrepairable_tests,
            pairs_out=_hop_pairs, observables_out=_observables)
        if isinstance(_bf_raw, tuple) and len(_bf_raw) == 3:
            _bf, _bf_sites, _srcs = _bf_raw
        elif isinstance(_bf_raw, tuple) and len(_bf_raw) == 2:
            _bf, _srcs = _bf_raw
            _bf_sites = lift_sites(_bf)
        else:
            _bf, _bf_sites, _srcs = {}, {}, {}
        _src_map = {name: src for name, (_rel, src) in _srcs.items()}
        _merge(_strip(_bf), FailureKind.BEHAVIOR, _strip_sites(_bf_sites))
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
        return AttributionResult(merged, kind, convicting, _src_map,
                                 frozenset(_hop_pairs), merged_sites,
                                 observables=_observables)
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
                return AttributionResult(synth, FailureKind.CLASSIFIER, {},
                                         site_frontier=lift_sites(synth))
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
                    recent_synth, FailureKind.RECENTLY_FILLED, {},
                    site_frontier=lift_sites(recent_synth))
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
                    reverse_synth, FailureKind.REVERSE_MAP, {},
                    site_frontier=lift_sites(reverse_synth))
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


@dataclass(frozen=True, slots=True)
class ProducerScan:
    """Who produces the types the futile accessors' containers name.

    `producers` is empty both when none of the fns produces a declared
    nominal type and when there is no such type. `candidates` is every
    container type the accessors named, before the nominal-type filter, so
    a caller can tell "nothing matched" from "the scan was not run".
    """

    producers: tuple[FnLocator, ...]
    candidates: tuple[str, ...]
    type_names: tuple[str, ...]


def producer_scan(
    fn_records: list[dict[str, Any]],
    type_records: list[Any] | None,
    futile_units: set[FnLocator],
) -> ProducerScan:
    """The producer subset a futility fire would grade, from records already
    read. Oracle fns are excluded. The futile accessors themselves are
    excluded: the scan answers who builds what they read, not who they are.

    `type_records` of None is the extractor unavailable, the same answer as
    an empty declaration list: no nominal type, so no producer. A caller
    that could not read the declarations must not treat that empty result
    as a measurement."""
    recs = [r for r in fn_records if not is_test_fn_record(r)]
    producible = ProducibleTypes.from_type_records(type_records)
    containers = {(r.get("file", ""), r.get("fn_name", "")): r.get("container", "")
                  for r in recs}
    candidates = {container_type_name(containers.get((k[0], k[1]), ""))
                  for k in futile_units}
    candidates.discard("")
    type_names = producible.filter(candidates)
    if not type_names:
        return ProducerScan((), tuple(sorted(candidates)), ())
    producers = producers_for_types(
        recs, set(type_names), exclude=set(futile_units))
    return ProducerScan(
        tuple(producers), tuple(sorted(candidates)), tuple(sorted(type_names)))


@dataclass(frozen=True, slots=True)
class ProducerEscalation:
    """Everything a futility fire derives from the tree for one test: the
    producer scan and the traversal steps the futile accessors call.

    The fire and a resumed floor both read this, so a product carried over
    from an older process holds the additions and the grade a fire in this
    process would write. Run `3076-051d385a` (2026-10-13) resumed a floor
    whose writer had stored owner-map admits as additions and producers
    alike: re-deriving only the grade left `src/ptr.rs::cast` and nine
    other non-producers ranked as products, exempt from the red-dead
    filter and re-merged every sweep, while 17 producers the scan names on
    today's tree were graded without ever being added."""

    scan: ProducerScan
    traversed: tuple[FnLocator, ...]
    #: Traversal steps found beyond `_MAX_TRAVERSAL_STEPS`, not escalated.
    traversed_over: int

    @property
    def producers(self) -> tuple[FnLocator, ...]:
        return self.scan.producers

    def additions(self, test_name: str) -> Frontier:
        """The frontier additions the fire returns: empty when the scan
        names no declared type (the fire does not escalate), else every
        producer and traversal step, attributed to `test_name`."""
        if not self.scan.type_names:
            return {}
        return {k: {test_name} for k in (*self.scan.producers, *self.traversed)}


def producer_escalation(
    fn_records: list[dict[str, Any]],
    type_records: list[Any] | None,
    futile_units: set[FnLocator],
) -> ProducerEscalation:
    """The fire's derivation from records already read: `producer_scan`,
    then the traversal steps of the futile accessors, excluding the
    producers and oracle records, capped at `_MAX_TRAVERSAL_STEPS`."""
    scan = producer_scan(fn_records, type_records, futile_units)
    if not scan.type_names:
        return ProducerEscalation(scan, (), 0)
    recs = [r for r in fn_records if not is_test_fn_record(r)]
    steps = _traversal_steps(recs, futile_units, exclude=set(scan.producers))
    kept = steps[:_MAX_TRAVERSAL_STEPS]
    return ProducerEscalation(scan, tuple(kept), len(steps) - len(kept))


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
    producers_out: set[FnLocator] | None = None,
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
    # ORACLE RECORDS OUT, before either scan. `_traversal_steps` excludes them
    # by its own rule ("a test file's own `impl StdError for MyError` is a
    # fixture the reconstruction may not rewrite") and the name-match frontier
    # excludes them too, with the same predicate, forty lines up. The producer
    # scan between them did neither, and a `#[test]` fn that calls
    # `Error::msg(..)` on its way to an assertion CONSTRUCTS the subject type,
    # so it matches `constructs_type` exactly as a real constructor does.
    # Measured on the live tree: `tests/test_chain.rs::error`,
    # `tests/test_context.rs::make_chain` and `tests/test_ffi.rs::anyhow3` were
    # the FIRST THREE names of all fourteen escalation fires across runs
    # `1246060-2339497b` and `3410521-4ccf6d68` — 3 of every 24-25 escalated
    # units. Nothing was ever written to them (the fill route refuses an
    # immutable target, 95 `skipping test-oracle target` lines say so), but
    # they sat in the frontier and therefore in the pass denominator, and an
    # immutable skip is one of the three classes that inflate a traversal's
    # tried-set against the flat-sweep refutation.
    types_unreadable = ""
    try:
        type_records = deps.structure.enumerate_types(src_dir)
    except Exception as e:
        type_records = None
        types_unreadable = f"{type(e).__name__}: {e}"
    # None is the extractor unavailable, which the scan treats as no nominal
    # type. An exception is the same empty scan, and the log below names the
    # exception rather than the crate.
    esc = producer_escalation(
        recs, [] if types_unreadable else type_records, futile_units)
    scan = esc.scan
    candidates = set(scan.candidates)
    type_names = set(scan.type_names)
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
    producers = list(scan.producers)
    traversed = list(esc.traversed)
    if not producers and not traversed:
        return {}
    dropped = sorted(candidates - type_names)
    deps.log(f"  [attribution/escalate] {test_name}: accessors of "
             f"{sorted(type_names)} are futile — escalating to "
             f"{len(producers)} producer fn(s): "
             f"{', '.join(f'{f}::{fn}' for f, fn in producers[:5])}"
             + ("..." if len(producers) > 5 else "")
             + (f" (not escalated: {dropped})" if dropped else ""))
    if traversed:
        deps.log(f"  [attribution/escalate] {test_name}: + "
                 f"{len(traversed)} traversal step(s) the futile accessors "
                 "dispatch through (trait-impl methods they call, which "
                 "produce none of these types and no producer scan can "
                 f"name): {', '.join(f'{f}::{fn}' for f, fn in traversed)}"
                 + (f" (+{esc.traversed_over} more over the "
                    f"cap of {_MAX_TRAVERSAL_STEPS}, NOT escalated)"
                    if esc.traversed_over else ""))
    # `producers_out`, when given, receives the PRODUCER subset alone.
    # The return value merges producers with traversal steps because the
    # frontier is one dict, and a caller that grades the two the same hands the
    # strongest instruction it has to a traversal step — on the live tree the
    # merged `added` set carried `src/error.rs::downcast` and `deref`, the very
    # accessors this fire had just declared futile, beside the five
    # `construct_from_*` it exists to name. The log has always printed them
    # apart; only the return value did not.
    if producers_out is not None:
        producers_out.update(producers)
    return esc.additions(test_name)


# The escalation may add at most this many traversal steps. A futile accessor
# calls a handful of methods and only trait impls of them qualify, so the cap is
# a backstop against a body that calls `clone`/`fmt`/`next` on everything, not a
# routine trim. It is LOGGED when it bites: a silent cap reads as "this is the
# whole set", which is the one thing an escalation must never imply.
_MAX_TRAVERSAL_STEPS = 8


def _traversal_steps(
    recs: list[dict[str, Any]],
    futile_units: set[FnLocator],
    *,
    exclude: set[FnLocator],
) -> list[FnLocator]:
    """Trait-impl methods the futile accessors CALL — the links of a walked
    chain, which produce none of the walked types.

    `producers_for_types` answers "who makes one of these values". A chain
    iterator's defect is not there: `Chain::next` follows `.source()` from link
    to link, and the `source` impl that returns `None` severs the chain while
    producing nothing at all. On `anyhow`/`stable` the four chain tests
    escalated to five constructors on every run of two runs, and
    `context.rs::source` — the fn that actually failed them — appeared in
    neither set.

    Oracle records are excluded: a test file's own `impl StdError for MyError`
    is a fixture the reconstruction may not rewrite."""
    bodies = "\n".join(
        (r.get("body_text", "") or "") for r in recs
        if FnLocator(r.get("file", ""), r.get("fn_name", "")) in futile_units)
    if not bodies.strip():
        return []
    called = called_method_names(bodies)
    out: list[FnLocator] = []
    for file, fn_name in trait_impl_fns_named(recs, called):
        key = FnLocator(file, fn_name)
        if (key in exclude or key in futile_units or key in out
                or is_oracle_path(file)):
            continue
        out.append(key)
    return out
