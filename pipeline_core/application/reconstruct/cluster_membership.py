"""Cluster-aware static membership analysis.

Given a build cluster (its `cfg_true`/`cfg_false` sets), decide which source
files and functions are *active* — i.e. would compile in that cluster — so the
per-fn fill loop only queues stubs that actually matter for the cluster under
reconstruction. Pure static analysis (no LLM).

Source facts come from the syn extractor through injected callables
(`mod_records`, `enumerate_fns`); cfg evaluation is delegated to
:mod:`pipeline_core.domain.cfg`. The only source-text path is the extractor-
absent regex *fallback* (`file_cfg_predicate_regex_fallback`), which locates a
`mod <stem>;` declaration via a structured line scan (no regex) and reads its
gating cfg attrs through `domain.cfg.cfg_inline_predicates`.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.stub_scan import (
    scan_stub_inventory,
)
from pipeline_core.domain import cfg as _cfg
from pipeline_core.domain.attribution import FailureFrontier
from pipeline_core.domain.cfg_dead_sites import CfgDeadSites
from pipeline_core.domain.cfg_worlds import world_mark, worlds_of
from pipeline_core.domain.cluster_membership_report import ClusterMembershipReport
from pipeline_core.domain.crash_site import CrashSites
from pipeline_core.domain.dispatch import impl_route_covers
from pipeline_core.domain.models import (
    FieldRecord,
    FnLocator,
    ImplBlock,
    MethodRecord,
    ModRecord,
    TypeRecord,
)
from pipeline_core.domain.oracle_immutability import is_oracle_path
from pipeline_core.domain.refill_stamp import (
    convicting_tests_for_member,
    homonym_containers,
    refill_admission,
    stamp_reads_dispatch,
)
from pipeline_core.domain.rust_lex import code_mask
from pipeline_core.domain.rust_scan import (
    contains_decl_keyword,
    item_start,
    named_fn_decl_start,
)
from pipeline_core.domain.stub_inventory import StubInventory, StubSite
from pipeline_core.domain.test_count import file_top_cfg_excluded


def filter_tests_for_cluster(
    test_files: list[tuple[str, str]], cluster: Any,
) -> list[tuple[str, str]]:
    """Drop test files whose top-level `#![cfg(...)]` is decidably false under
    `cluster`. Files with no top-level cfg or an undecidable predicate are kept
    (conservative). `cluster is None` → unchanged."""
    if cluster is None:
        return test_files
    cfg_true = {_cfg.normalize_pred(p) for p in cluster.cfg_true}
    cfg_false = {_cfg.normalize_pred(p) for p in cluster.cfg_false}
    return [(rel, content) for rel, content in test_files
            if not file_top_cfg_excluded(content, cfg_true, cfg_false)]


def _leading_attr_text(definition: str) -> str:
    """The attribute block above a rendered item's head — doc comments and
    blank lines skipped, everything from the first code line on ignored.

    Only the leading block may speak for the ITEM: a `#[cfg(...)]` on an
    inner field or variant gates that member, and reading it as the type's
    own gate would delete a live type over a dead field."""
    out: list[str] = []
    for raw in definition.splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if not line.startswith("#["):
            break
        out.append(line)
    return "\n".join(out)


def cfg_dead_type_records(
    records: list[TypeRecord], cluster: Any,
) -> frozenset[str]:
    """Names of the type records whose OWN definition is gated by a cfg this
    cluster decides False — the arm the build does not compile."""
    if cluster is None:
        return frozenset()
    cfg_set = _cfg.cluster_cfg_set(cluster)
    dead: set[str] = set()
    for r in records:
        preds = _cfg.cfg_inline_predicates(_leading_attr_text(r.definition))
        if preds and any(cfg_set.evaluate(p) is False for p in preds):
            dead.add(r.name)
    return frozenset(dead)


def cluster_filter_type_members(
    records: list[TypeRecord], cluster: Any,
    all_clusters: Sequence[Any] = (),
) -> list[TypeRecord]:
    """Drop what `cluster` does not compile so TYPES_IN_SCOPE advertises only
    what exists here: a METHOD whose cfg evaluates definitively
    False, a FIELD whose effective cfg does, a TRAIT IMPL whose impl-block cfg
    does, and a whole TYPE RECORD whose own leading cfg does. Undecidable predicates are kept (conservative).
    `cluster is None` → unchanged.

    With the run's whole world plan (`all_clusters`, two or more), fields and
    methods are projected for the CRATE, not for one world: a member some
    world compiles is kept and carries the worlds that compile it (`worlds`)
    when that is not all of them; only a member no world compiles is dropped.
    The body a fill writes is built in every world (`cargo_runner
    .WorldChecks`), so it has to see both kinds of gated member: one that
    exists here and not elsewhere, which it may only touch under the same
    gate, and one that exists elsewhere and not here, which a struct literal
    there still has to initialise. Run `292191-d60a5308` (2026-10-02): the
    `stable` fills were shown `ErrorVTable.object_backtrace` with no gate and
    never shown that `nightly` lacks it; six constructors initialised it
    unguarded and did not build in `nightly`. Hiding the other world's
    members (the 2026-09-07 projection below) made each tree right for its
    own world and wrong for the crate. Trait impls, slot candidates and whole
    type records stay projected to `cluster`: those lists render as complete
    for the world the fill is judged in.

    Fields are the member kind this filter did not reach until 2026-09-07.
    The extractor carried cfg on methods only, so the type section and the
    `construct:` hint rendered every field of `ErrorVTable` under `nightly`,
    including `#[cfg(all(not(error_generic_member_access), feature = "std"))]
    object_backtrace`, and the `impl Error` fill wrote exactly the hinted
    seven-field literal in four of five attempts — `E0560: struct ErrorVTable
    has no field named object_backtrace` (runs `2065433-a1c7219a` and
    `2131920-86818224`). The nightly cfg-set decides that gate False; the
    stable one keeps the field (its `msg` vtable carries `object_backtrace`),
    so the answer is per-cluster evaluation of the field's effective cfg —
    struct gate plus field gate, as `MethodRecord.cfg_predicates` already is
    for impl blocks — never dropping the field. Every renderer that shows
    fields (`format_type_for_prompt`, `_construct_hint`, the private-field
    lines) reads the records this function returns, which is why the
    projection lives here and not in each renderer.

    The record-level drop is the half added after run `617614-0abaf2d0`
    (2026-09-01). `src/backtrace.rs` is the standard no-std shim — `#[cfg(
    feature = "std")] pub(crate) use std::backtrace::Backtrace;` beside
    `#[cfg(not(feature = "std"))] pub(crate) enum Backtrace {}` — and only the
    second is an ITEM, so the extractor emitted the DEAD arm as the sole
    record for that name: `enum Backtrace`, no fields, no variants, no
    methods. TYPES_IN_SCOPE rendered it verbatim while RUST_GOTCHAS told the
    model to use only what that table shows and never to call an associated
    fn a type does not define, and the pair fill's reasoning trace cites
    exactly that ("But types in scope just enum Backtrace. Might be opaque.")
    when it proposes `status()` on what is really a `std::backtrace::
    Backtrace` and then drops it. A type absent from the table asserts
    nothing; a dead arm rendered as the definition asserts something false.

    What it deliberately does not do: name what the live arm resolves to.
    The extractor already knows (`--enumerate-uses` returns `src/backtrace.rs`
    alias `Backtrace` -> `std::backtrace::Backtrace`), but no route carries
    use records into the type section, and inventing that channel here would
    be a second mechanism inside a one-line screen. The file's own `use` is
    already quoted in the prompt's source block."""
    if cluster is None:
        return records
    cfg_set = _cfg.cluster_cfg_set(cluster)
    worlds = list(all_clusters) if len(all_clusters) > 1 else []
    every = [str(c.name) for c in worlds]

    def _here(preds: tuple[str, ...]) -> bool:
        return not any(cfg_set.evaluate(p) is False for p in preds)

    def _member[M: (FieldRecord, MethodRecord)](member: M) -> M | None:
        """The member as this projection shows it, or None when dropped."""
        if not worlds:
            return member if _here(member.cfg_predicates) else None
        compiled = worlds_of(member.cfg_predicates, worlds)
        if not compiled:
            return None
        mark = world_mark(compiled, every)
        return member if mark == member.worlds else dataclasses.replace(
            member, worlds=mark)

    def _cfg_kept(impls: tuple[Any, ...] | None) -> tuple[Any, ...] | None:
        if impls is None:
            return None
        return tuple(
            t for t in impls
            if not any(cfg_set.evaluate(p) is False for p in t.cfg_predicates))

    out: list[TypeRecord] = []
    for r in records:
        # Each record answers for its OWN leading cfg, never for its name:
        # the shim's live twin is a `use`, and the structure reader now
        # appends a record for what that `use` binds (`external struct
        # Backtrace`, gated by the shim's complement). Dropping by name
        # removed both arms at once and left the table with no `Backtrace`
        # at all — the pre-existing absence, measured 2026-09-02.
        preds = _cfg.cfg_inline_predicates(_leading_attr_text(r.definition))
        if preds and any(cfg_set.evaluate(p) is False for p in preds):
            continue
        kept = tuple(m for m in map(_member, r.methods) if m is not None)
        # A slot candidate the cluster does not compile cannot be installed
        # here; the fits list renders as complete, so it leaves it.
        kept_fields = tuple(
            f if f.fits is None else dataclasses.replace(f, fits=tuple(
                c for c in f.fits
                if not any(cfg_set.evaluate(p) is False
                           for p in c.cfg_predicates)))
            for f in map(_member, r.fields) if f is not None
        )
        # A trait impl the cluster does not compile is not an impl here: the
        # list renders as complete, so an impl the cfg world drops must leave
        # it, exactly as a dropped field leaves the `construct:` hint. The
        # crate's type-parameter impls are the same kind of list.
        kept_impls = _cfg_kept(r.trait_impls)
        kept_blankets = _cfg_kept(r.blanket_impls)
        if (kept != r.methods or kept_fields != r.fields
                or kept_impls != r.trait_impls
                or kept_blankets != r.blanket_impls):
            out.append(dataclasses.replace(
                r, methods=kept, fields=kept_fields,
                trait_impls=kept_impls, blanket_impls=kept_blankets))
        else:
            out.append(r)
    return out


def cluster_cfg_attr(cluster: Any, all_clusters: list[Any]) -> str:
    """The cfg predicate gating code to `cluster`. Each active cfg `N`
    → `any(N, feature = "N")`; multiple combine via `all(...)`. The default
    (stable) cluster is gated by the NEGATION of every other cluster's flags
    (its gen_tests were only validated under the default build). No other
    clusters → no gate. `all_clusters` replaces the run-global cluster list."""
    if cluster is None:
        return ""
    if getattr(cluster, "is_default", True):
        other_flags: set[str] = set()
        for c in all_clusters:
            if c is cluster or getattr(c, "is_default", True):
                continue
            other_flags.update(getattr(c, "active_cfg", []) or [])
        if not other_flags:
            return ""
        terms = [t for f in sorted(other_flags)
                 for t in (f, f'feature = "{f}"')]
        return "not(any(" + ", ".join(terms) + "))"
    flags = list(getattr(cluster, "active_cfg", []) or [])
    if not flags:
        return ""
    terms = [f'any({f}, feature = "{f}")' for f in flags]
    return terms[0] if len(terms) == 1 else "all(" + ", ".join(terms) + ")"


def propagate_or_quarantine_sibling(
    gt: Path, origin_manifest: dict[str, Any], parent_stem: str, pass_n: int,
    log_fn: Callable[[str], None] | None = None, tag: str = "",
) -> bool:
    """The origin manifest is authoritative for which gen_tests siblings
    are legit. True → propagate (declared, or empty/absent manifest = legacy
    run). A sibling absent from a NON-empty manifest was never coverage-validated
    pre-strip → quarantine to `.broken` and return False (the cluster-leak class)."""
    key = f"{parent_stem}.gen_tests_{pass_n}"
    if origin_manifest and key not in origin_manifest:
        if not gt.name.endswith(".broken"):
            broken = gt.with_name(gt.name + ".broken")
            try:
                gt.rename(broken)
                if log_fn:
                    log_fn(f"  {tag} [skeleton-C] ⚠️  orphan gen_tests sibling "
                           f"'{gt.name}' absent from origin manifest (undeclared "
                           f"pre-strip) — quarantined → {broken.name}, not propagated.")
            except OSError as e:
                if log_fn:
                    log_fn(f"  {tag} [skeleton-C] orphan quarantine failed {gt.name}: {e}")
        return False
    return True


def collect_impl_blocks_for_cluster(
    target_src_dir: Path, cluster: Any, *,
    collect_impl_blocks: Callable[[Path], list[ImplBlock] | None],
    fn_belongs: Callable[..., bool],
) -> list[ImplBlock] | None:
    """Cluster-aware `collect_impl_blocks`: a block is kept iff ≥1 of its
    members (stub OR already-filled) is active in `cluster`; inactive stubs are
    dropped from `stub_members`. A block with zero cluster-active STUBS but a
    cluster-active already-filled member is still kept (with `stub_members`
    narrowed to `()`) — dropping it here would make an all-filled-but-wrong
    block unreachable for behavioral re-fill targeting downstream (see
    `collect_impl_refill_targets`). None (extractor unavailable) passes through.

    Blocks in an oracle FILE are dropped outright, before any cluster question
    is asked. This route enumerates impls from the tree rather than from a
    frontier, so it is not covered by the frontier's `filter_test_targets`: on
    2026-08-01 it filled `tests/drop/mod.rs::Flag`, `::DetectDrop`,
    `::DisplayforDetectDrop`, `::DropforDetectDrop`, `tests/test_ensure.rs::
    DebugforEnum<T>` and `tests/test_source.rs::DisplayforTestError` — ten
    fixture bodies in the shared, symlinked test oracle — and two of them did
    not compile, which took the cluster from 93/103 to a red build it could not
    repair. An oracle fixture is never a fill target however the cluster gates
    resolve."""
    blocks = collect_impl_blocks(target_src_dir)
    if blocks is None or cluster is None:
        return blocks
    out: list[ImplBlock] = []
    for b in blocks:
        if is_oracle_path(b.file):
            continue
        kept = tuple(
            m for m in b.stub_members
            if fn_belongs(target_src_dir, b.file, m.fn_name, cluster,
                          fn_cfg_predicates=list(m.cfg_predicates))
        )
        any_active = bool(kept) or any(
            fn_belongs(target_src_dir, b.file, m.fn_name, cluster,
                       fn_cfg_predicates=list(m.cfg_predicates))
            for m in b.members
        )
        if any_active:
            out.append(dataclasses.replace(b, stub_members=kept))
    return out


def collect_impl_refill_targets(
    blocks: list[ImplBlock],
    tdd_frontier_keys: set[FnLocator] | None,
    tdd_frontier_tests: FailureFrontier | None,
    *,
    max_stubs: int = 12,
    crash_blamed: frozenset[FnLocator] | None = None,
    crash_sites: CrashSites | None = None,
    file_primary: bool = False,
) -> list[ImplBlock]:
    """Block-level analog of
    `PhaseCFillEngine._collect_refill_targets`: for each block, find members
    that are already FILLED (not in `stub_members`) but have a failing
    attributed test, and stamp them onto `refill_members`.

    For a block the per-impl route will attempt, the stamp reads
    ``convicting_for`` plus the container-homonym filter
    (``domain.refill_stamp``): dispatch over-includes, and a compiling
    ``from`` that inherited Debug asserts is a GPU re-buy (run
    ``463713-b7c666c8``). For a block that route will never attempt
    (free-fn / oversized), OR under file-primary where the file pass writes
    the block and the impl route then drops it, the stamp reads
    ``tests_for`` — otherwise a dispatch-only constructor in the batch is
    stranded in a still-coverable-looking Error impl (run
    ``125003-269e2338``: 9 of 12 ``no route attempted``). Empty unless the
    TDD outer loop supplied both the frontier keys and the failing-test map
    (mirrors the per-fn version's same guard).

    A crashed binary reports no verdict, so ``tests_for`` /
    ``convicting_for`` are empty for the blamed body. Without a stamp the
    file-first write list is the file's other refill members plus the
    installable complement, and ``_plan_line``'s CRASHED clause (the MUST)
    never sees the conversion. Live: run ``1869206-7fd52754``, blamed
    ``into_boxed_dyn_error``, write list was ``object_boxed`` /
    ``object_drop`` / … graded REWRITE WITH THE PRODUCER. A locator in
    ``crash_blamed`` is a refill even when no test convicts it — in the
    container a culprit frame resolved to (``crash_sites``), so a blame on
    one `fmt` does not admit its homonym in another impl.
    """
    if not (tdd_frontier_keys and tdd_frontier_tests):
        return blocks
    crashed = crash_blamed or frozenset()
    at_site = crash_sites or CrashSites()
    homs = homonym_containers(blocks)
    out: list[ImplBlock] = []
    for b in blocks:
        stub_keys = {FnLocator(b.file, m.fn_name) for m in b.stub_members}
        dispatched = tuple(
            m for m in b.members
            if (loc := FnLocator(b.file, m.fn_name)) in tdd_frontier_keys
            and loc not in stub_keys
            and refill_admission(tdd_frontier_tests.tests_for(loc),
                                 crash_blamed=at_site.blames(
                                     crashed, b.file, m.fn_name, b.container))
            is not None
        )
        probe = dataclasses.replace(b, refill_members=dispatched)
        covers = impl_route_covers(probe, max_stubs=max_stubs)
        if stamp_reads_dispatch(covers=covers, file_primary=file_primary):
            refill = dispatched
        else:
            refill = tuple(
                m for m in dispatched
                if refill_admission(
                    convicting_tests_for_member(
                        tdd_frontier_tests, b.file, m.fn_name, b.container,
                        homs.get((b.file, m.fn_name), (b.container,))),
                    crash_blamed=at_site.blames(
                        crashed, b.file, m.fn_name, b.container))
                is not None
            )
        out.append(dataclasses.replace(b, refill_members=refill) if refill else b)
    return out


def scan_unimplemented_records_for_cluster(
    target_src_dir: Path, cluster: Any, *,
    scan_records: Callable[[Path], list[dict[str, Any]] | None],
    fn_belongs: Callable[..., bool],
    log: Callable[[str], None],
) -> list[dict[str, Any]] | None:
    """Cluster-aware `scan_unimplemented_records`: keep only records whose
    file/fn cfg evaluates true for `cluster`. None (extractor absent) passes
    through so callers can fall back to the tuple-form scan."""
    recs = scan_records(target_src_dir)
    if recs is None or cluster is None:
        return recs
    out: list[dict[str, Any]] = []
    skipped: list[str] = []
    for r in recs:
        if fn_belongs(target_src_dir, r["file"], r["fn_name"], cluster,
                      fn_cfg_predicates=list(r.get("cfg_predicates") or [])):
            out.append(r)
        else:
            skipped.append(f"{r['file']}::{r['fn_name']}")
    if skipped:
        log(ClusterMembershipReport(
            cluster.name, len(recs), tuple(skipped)).log_line())
    return out


def _line_declares_mod(line: str, stem: str) -> bool:
    """True when `line` contains a `mod <stem>;` declaration (token-exact:
    `mod` is a whole word, `stem` follows after whitespace as a whole word,
    then `;` after optional whitespace). Structured replacement for the old
    `\\bmod\\s+stem\\s*;` regex — same match set, no regex on source."""

    def _word_char(c: str) -> bool:
        return c.isalnum() or c == "_"

    n = len(line)
    i = 0
    while True:
        j = line.find("mod", i)
        if j < 0:
            return False
        i = j + 3
        if j > 0 and _word_char(line[j - 1]):
            continue
        if i < n and _word_char(line[i]):
            continue
        k = i
        while k < n and line[k] in " \t":
            k += 1
        if k == i:  # `\s+` requires at least one space after `mod`
            continue
        if not line.startswith(stem, k):
            continue
        e = k + len(stem)
        if e < n and _word_char(line[e]):
            continue
        p = e
        while p < n and line[p] in " \t":
            p += 1
        if p < n and line[p] == ";":
            return True
    # unreachable


def _find_mod_decl(text: str, stem: str) -> tuple[str, int] | None:
    """Return (decl_line, line_start_offset) of the first `mod <stem>;`
    declaration in `text`, or None. Offset is the index of the line's first
    char (matching the old regex's post-newline match start)."""
    pos = 0
    for line in text.split("\n"):
        if _line_declares_mod(line, stem):
            return line, pos
        pos += len(line) + 1
    return None


def file_cfg_predicate_regex_fallback(
    target_src_dir: Path, file_rel: str,
) -> str | None:
    """Extractor-absent fallback for `file_cfg_predicate`. Scans lib.rs/main.rs
    for the file's `mod <stem>;` declaration (structured, no regex) and collects
    the cfg attrs gating it. Known limitation: cannot see cfg attrs the syn
    extractor would attribute to a sibling; the AST path is preferred whenever
    available.
    """
    stem = Path(file_rel).stem
    if stem in ("lib", "main"):
        return None
    for root in ("src/lib.rs", "src/main.rs"):
        p = target_src_dir / root
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        found = _find_mod_decl(text, stem)
        if found is None:
            continue
        decl_line, decl_start = found
        preds: list[str] = []
        mod_idx = decl_line.find("mod ")
        if mod_idx < 0:
            mod_idx = len(decl_line)
        for pred in _cfg.cfg_inline_predicates(decl_line[:mod_idx]):
            preds.append(pred.strip())
        head = text[:decl_start]
        for ln in reversed(head.splitlines()):
            s = ln.strip()
            if not s:
                break
            if s.startswith("//") or s.startswith("/*"):
                continue
            # Stop on any line that contains a declaration keyword — the
            # cfg attrs on such a line belong to ITS item, not ours.
            if contains_decl_keyword(s):
                break
            ln_preds = _cfg.cfg_inline_predicates(ln)
            if ln_preds:
                preds.extend(pr.strip() for pr in ln_preds)
                continue
            if s.startswith("#["):
                continue
            break
        if not preds:
            return None
        if len(preds) == 1:
            return preds[0]
        return "all(" + ", ".join(preds) + ")"
    return None


def file_cfg_predicate(
    target_src_dir: Path,
    file_rel: str,
    *,
    mod_records: Callable[[Path], list[ModRecord] | None],
) -> str | None:
    """Return the cfg predicate (if any) under which `file_rel` is compiled.

    AST-derived via the syn-based skeleton extractor (`mod_records`). Returns
    None when the file is unconditionally included or no parent mod decl is
    found. Falls back to the structured scan when the extractor is unavailable.
    """
    stem = Path(file_rel).stem
    if stem in ("lib", "main"):
        return None
    recs = mod_records(target_src_dir)
    if recs is None:
        return file_cfg_predicate_regex_fallback(target_src_dir, file_rel)
    file_name = Path(file_rel).name
    for r in recs:
        if r.name == stem or r.file == file_name:
            preds: list[str] = list(r.cfg_predicates)
            if not preds:
                return None
            if len(preds) == 1:
                return preds[0]
            return "all(" + ", ".join(preds) + ")"
    return None


def fn_cfg_predicates(file_text: str, fn_name: str) -> list[str]:
    """Return all `#[cfg(...)]` predicates that gate `fn_name` in `file_text`.

    FALLBACK ONLY: callers holding extractor records must pass the record's
    `cfg_predicates` to `fn_belongs_to_cluster` instead — this walk only ever
    finds the FIRST same-named fn in the file.

    The window is the fn's own item span (`item_start` walks back over exactly
    its attributes and qualifiers), NOT back to the nearest blank line. A blank
    line does not delimit an item: `unsafe fn object_drop<E>(..) { .. }` written
    directly under a cfg-gated struct field would inherit that field's `#[cfg]`,
    be judged inactive in the cluster, and never be filled — while rustc
    compiles it unconditionally.
    """
    m_start = named_fn_decl_start(file_text, fn_name)
    if m_start is None:
        return []
    decl_offset = m_start + (1 if file_text[m_start] == "\n" else 0)
    mask = code_mask(file_text)
    window = file_text[item_start(file_text, mask, decl_offset):decl_offset + 1]
    return [pred.strip() for pred in _cfg.cfg_inline_predicates(window)]


def fn_belongs_to_cluster(
    target_src_dir: Path,
    file_rel: str,
    fn_name: str,
    cluster: Any,
    fn_cfg_preds: Sequence[str] | None = None,
    *,
    mod_records: Callable[[Path], list[ModRecord] | None],
) -> bool:
    """True when the fn (or its enclosing file) is active in `cluster`.

    Evaluates the conjunction of the file's parent-mod cfg and the fn's own cfg
    attrs against the cluster's cfg_true / cfg_false sets.

    `fn_cfg_preds`: the syn extractor's cfg list for THIS fn record —
    callers holding extractor records pass it ([] = ungated, trusted as-is).
    Only when None does the legacy text-walk run.

    Conservative on uncertainty: an undecidable predicate defaults to True
    (include the stub) — better to waste a call than miss a fn we should fill.
    """
    cfg_set = _cfg.cluster_cfg_set(cluster)
    preds: list[str] = []
    file_pred = file_cfg_predicate(target_src_dir, file_rel, mod_records=mod_records)
    if file_pred:
        preds.append(file_pred)
    if fn_cfg_preds is not None:
        preds.extend(fn_cfg_preds)
    else:
        try:
            text = (target_src_dir / file_rel).read_text(encoding="utf-8")
        except Exception:
            text = ""
        if text:
            preds.extend(fn_cfg_predicates(text, fn_name))
    if not preds:
        return True  # no cfg gating → exists in every cluster
    # False → excluded; None (undecidable) → conservatively kept.
    return all(cfg_set.evaluate(p) is not False for p in preds)


def cluster_dead_sites(
    target_src_dir: Path,
    cluster: Any,
    *,
    enumerate_fns: Callable[[Path], list[dict[str, Any]] | None],
    mod_records: Callable[[Path], list[ModRecord] | None],
) -> CfgDeadSites:
    """The `(file, fn_name)` sites `cluster` never compiles, over EVERY fn
    record of the tree — stub or filled.

    A site is dead iff it has at least one record and `fn_belongs_to_cluster`
    is False for every one of them (the file's parent-mod gate plus the
    record's own `cfg_predicates`). So a name with several records under
    complementary gates (`provide` ×3 in one file, a `cap_limit` per arm)
    stays live while any arm compiles, and an undecidable predicate keeps
    its record live — better a wasted call than a fn nothing can fill.

    The stub-only census this replaces read deadness off the stub list, so
    filling a dead stub made it live to every consumer; a filled body this
    cluster never compiles was then re-dispatched as a re-fill and accepted
    by the splice (run `473471-33301154`, 2026-09-29). Empty when there is
    no cluster or no extractor: unknown is not dead."""
    if cluster is None:
        return CfgDeadSites()
    recs = enumerate_fns(target_src_dir)
    if recs is None:
        return CfgDeadSites()
    return dead_sites_among(recs, target_src_dir, cluster,
                            mod_records=mod_records)


def dead_sites_among(
    recs: Sequence[dict[str, Any]],
    target_src_dir: Path,
    cluster: Any,
    *,
    mod_records: Callable[[Path], list[ModRecord] | None],
) -> CfgDeadSites:
    """`cluster_dead_sites` over records the caller already enumerated from
    `target_src_dir` — the frontier filter reads the oracle keys off the same
    enumeration, so the tree is read once per filter."""
    if cluster is None:
        return CfgDeadSites()
    seen: set[tuple[str, str]] = set()
    live: set[tuple[str, str]] = set()
    for r in recs:
        file_rel, fn_name = str(r.get("file", "")), str(r.get("fn_name", ""))
        key = (file_rel, fn_name)
        seen.add(key)
        if key in live:
            continue
        if fn_belongs_to_cluster(target_src_dir, file_rel, fn_name, cluster,
                                 list(r.get("cfg_predicates") or []),
                                 mod_records=mod_records):
            live.add(key)
    return CfgDeadSites(frozenset(seen - live))


def scan_unimplemented_for_cluster(
    target_src_dir: Path,
    cluster: Any,
    *,
    enumerate_fns: Callable[[Path], list[dict[str, Any]] | None],
    mod_records: Callable[[Path], list[ModRecord] | None],
    log: Callable[[str], None],
) -> StubInventory:
    """Cluster-aware variant of `scan_stub_inventory`.

    Keeps only the stub SITES that belong to `cluster` per static cfg
    evaluation. When `cluster` is None, behaves identically to the
    unfiltered scan. The membership report's denominator is the site count
    (one extractor record per impl site) and its names repeat a locator
    once per skipped site — that is the report's contract, and the unit
    view is what the returned inventory names `units`.
    """
    inventory = scan_stub_inventory(target_src_dir, enumerate_fns=enumerate_fns)
    if cluster is None:
        return inventory
    kept: list[StubSite] = []
    skipped: list[str] = []
    for s in inventory.sites:
        # `cfg_predicates` is the extractor's per-fn attribute list; passing
        # it keeps `fn_belongs_to_cluster` off the text-walk fallback, which
        # attributes a neighbouring item's `#[cfg]` to this fn and silently
        # drops a fn that is in fact ungated.
        if fn_belongs_to_cluster(target_src_dir, s.site.file, s.site.fn_name,
                                 cluster, s.cfg_predicates,
                                 mod_records=mod_records):
            kept.append(s)
        else:
            skipped.append(f"{s.site.file}::{s.site.fn_name}")
    if skipped:
        log(ClusterMembershipReport(
            cluster.name, inventory.site_count, tuple(skipped)).log_line())
    return StubInventory(tuple(kept))
