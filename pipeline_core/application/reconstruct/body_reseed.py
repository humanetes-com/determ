"""The re-seed, transactional: write stored bodies, build, keep only what built.

WHY IT IS TRANSACTIONAL AND NOT A PLAIN WRITE. Every site that puts bodies into
a tree today writes and walks away, and the loop cannot recover from a tree that
does not compile: the unit-scoped fix route may only rewrite a body, never
remove a definition (the `E0592` duplicate-`debug` incident of 2026-08-11,
where every fix attempt re-checked to the identical error for two full passes),
and a tree that does not build scores 0 on every sweep with NO per-unit signal
to aim at — `nightly 0/124 for 8.7 h` in the score history is exactly that,
from a seed defect. A re-seed is the one write in this pipeline that is made of
code nobody just wrote, so it is also the one that must prove itself.

The transaction: splice everything the gate allows, `cargo check`, and if that
fails, bisect — by FILE first, then by unit inside the failing file — reverting
the rejects to `unimplemented!()` and recording the rejection so the same body
is not offered again under the same layout. The bisect is bounded by
`files + units_in_failing_files` builds, and the count is logged, because a
re-seed that spends more wall clock than the fills it saves is a defect this
module has to be able to show.

WHAT IT NEVER DOES. It does not choose which units matter: the caller passes the
stubbed set. It does not remove a unit from the frontier, mark one exhausted, or
suppress a prompt. A re-seeded body leaves the unit filled and everything
downstream treats it exactly as it treats any other filled unit.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pipeline_core.domain.body_ledger import (
    BodyState,
    LayoutDep,
    LedgerEntry,
    LedgerKey,
    authored_layout_fp,
    plan_reseed,
    sig_sha_of,
)
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.scaffold_rewrite import find_definition_span

BuildCheck = Callable[[], tuple[bool, str]]
SpliceFn = Callable[[Sequence[tuple[FnLocator, Sequence[str]]], Path],
                    tuple[list[FnLocator], list[tuple[FnLocator, str]]]]
RevertFn = Callable[[list[FnLocator], Path], int]
SignaturesFn = Callable[[FnLocator, Path], tuple[str, ...]]
BodiesFn = Callable[[FnLocator, Path], tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class TreeWriter:
    """The three tree operations a re-seed needs, injected rather than imported.

    `adapters/fn_reseed.py` and `adapters/fn_revert.py` implement all three, and
    the layer contract (adapters > application) is why they arrive as callables:
    this module decides WHAT goes back on the tree and the adapters know how to
    write it. The composition root wires them together — the same shape the
    retrigger's own revert seam already uses."""

    splice: SpliceFn
    revert: RevertFn
    signatures: SignaturesFn
    bodies: BodiesFn


@dataclass(slots=True)
class ReseedOutcome:
    """What a re-seed site did, in the shape its one log line needs."""

    site: str
    seeded: list[LedgerKey] = field(default_factory=list)
    rejected: list[LedgerKey] = field(default_factory=list)
    skipped: list[tuple[LedgerKey, str]] = field(default_factory=list)
    builds: int = 0
    proven: int = 0
    unmeasured: int = 0
    checked: bool = False

    def render(self) -> str:
        parts = [f"re-seeded {len(self.seeded)} unit(s) "
                 f"({self.proven} proven, {self.unmeasured} unmeasured)"]
        if self.skipped:
            # Count by REASON, not one blanket label: "layout moved" and
            # "state landed" send a reader to different places, and lumping
            # them was how a re-seed that skipped everything for the second
            # reason read as a re-author that had not happened.
            by_reason = Counter(why for _k, why in self.skipped)
            parts.append("skipped " + ", ".join(
                f"{n} ({why})" for why, n in sorted(by_reason.items())))
        if self.rejected:
            parts.append(f"{len(self.rejected)} rejected (did not build)")
        if not self.checked:
            parts.append("NOT build-checked (no checker wired)")
        elif self.builds:
            parts.append(f"{self.builds} cargo check(s)")
        return "; ".join(parts)

    @property
    def data(self) -> dict[str, object]:
        return {
            "site": self.site,
            "seeded": sorted(f"{k.file}::{k.fn_name}" for k in self.seeded),
            "rejected": sorted(f"{k.file}::{k.fn_name}" for k in self.rejected),
            "skipped": sorted(f"{k.file}::{k.fn_name}: {why}"
                              for k, why in self.skipped),
            "builds": self.builds, "checked": self.checked,
        }


def _skeleton_files(skeleton_root: Path) -> list[tuple[str, Path]]:
    """`(key, path)` for every layout file, keyed AS THE EXTRACTOR NAMES IT.

    The key is relative to the crate root, not to `src/`, because that is what
    every other identity in this pipeline uses: the syn extractor records
    `file` as `src/error.rs` and cargo's panic messages print the same path
    (`phase_c_tdd` says so where it aliases `src_dir = target_src_dir`). A map
    keyed `error.rs` would miss every lookup and the gate would report "layout
    moved" for a layout that had not moved — silently, and for every unit."""
    src = Path(skeleton_root) / "src"
    try:
        return [(str(p.relative_to(skeleton_root)), p)
                for p in sorted(src.rglob("*.rs"))]
    except OSError:
        return []


def skeleton_fingerprints(skeleton_root: Path) -> dict[str, str]:
    """`file -> fingerprint` over the cluster's authored layout.

    Reads `.skeleton/<cluster>`, never the filled tree: the skeleton is where
    the layout lives and where a t1.5 re-author writes, so this digest cannot
    move because a body was filled."""
    out: dict[str, str] = {}
    for key, p in _skeleton_files(skeleton_root):
        try:
            out[key] = authored_layout_fp(
                p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return out


def _definition_checker(skeleton_root: Path) -> Callable[[LayoutDep], bool]:
    """Is this dependency's definition still in the tree, verbatim?

    `find_definition_span` matches on the non-space projection, so indentation
    and line breaks do not count as a move — which is the same tolerance the
    scaffold rewriter uses to locate a definition it is about to replace."""
    cache: dict[str, str] = {}
    files = _skeleton_files(skeleton_root)

    def _text(rel: str) -> str:
        if rel not in cache:
            try:
                cache[rel] = (Path(skeleton_root) / rel).read_text(
                    encoding="utf-8", errors="replace")
            except OSError:
                cache[rel] = ""
        return cache[rel]

    def _present(dep: LayoutDep) -> bool:
        if not dep.definition:
            return True
        if dep.file and find_definition_span(_text(dep.file),
                                             dep.definition) is not None:
            return True
        # The item may have MOVED FILE without moving, which is not a layout
        # change for a body that only names it — so the fallback searches the
        # whole layout before it refuses.
        return any(find_definition_span(_text(key), dep.definition) is not None
                   for key, _p in files)

    return _present


def reseed(
    *, site: str, entries: Iterable[LedgerEntry], wanted: Iterable[FnLocator],
    project: str, cluster: str, src_dir: Path, skeleton_root: Path,
    writer: TreeWriter, oracle_fp: str,
    check_builds: BuildCheck | None = None,
    log: Callable[[str], None] | None = None,
) -> ReseedOutcome:
    """Put back every body the ledger can vouch for, and keep only what builds.

    `wanted` is the set of units the caller wants filled, and every one of them
    must currently be a STUB: the bisect's fallback is `unimplemented!()`, so a
    unit that arrived here already filled could be stubbed by a failed build it
    had no part in. At a retrigger seam that set is the units it just reverted;
    at cluster entry it is the whole stub set. Units the ledger has nothing for
    are silently absent: an empty ledger makes this a no-op, which is what makes
    the write path shippable before any read path exists.

    `oracle_fp` is the fingerprint of the test set scoring `src_dir`, computed
    by the CALLER (`adapters/oracle_fingerprint.cluster_oracle_fp`, through
    the driver's dep) — this module reads the layout to fingerprint it and
    the tree to read signatures, and it deliberately does not also decide what
    the oracle is. A row graded under another oracle is skipped as `oracle
    moved`, which the outcome line counts by reason like every other refusal.
    """
    outcome = ReseedOutcome(site=site)
    wanted_keys = [LedgerKey(project=project, cluster=cluster, file=k.file,
                             fn_name=k.fn_name) for k in wanted]
    if not wanted_keys:
        return outcome
    file_fp = skeleton_fingerprints(skeleton_root)
    sig_sha = {
        key: sig_sha_of(writer.signatures(
            FnLocator(key.file, key.fn_name), src_dir))
        for key in wanted_keys}
    plan = plan_reseed(entries, wanted=wanted_keys, file_fp=file_fp,
                       sig_sha=sig_sha, oracle_fp=oracle_fp,
                       definition_present=_definition_checker(skeleton_root))
    outcome.skipped = list(plan.skipped)
    if not plan.take:
        return outcome

    units = [(FnLocator(e.key.file, e.key.fn_name), e.bodies) for e in plan.take]
    spliced, refused = writer.splice(units, src_dir)
    for key, why in refused:
        outcome.skipped.append(
            (LedgerKey(project, cluster, key.file, key.fn_name), why))
    kept = [e for e in plan.take
            if FnLocator(e.key.file, e.key.fn_name) in set(spliced)]
    if not kept:
        return outcome

    if check_builds is None:
        # No checker wired (a unit test, or a site whose caller has not been
        # given one yet). Say so in the line rather than implying a tree that
        # was verified: an unchecked re-seed is exactly the risk this module
        # exists to remove.
        outcome.seeded = [e.key for e in kept]
        _tally(outcome, kept)
        return outcome

    outcome.checked = True
    kept = _bisect_to_what_builds(kept, src_dir, writer, check_builds, outcome,
                                  log)
    outcome.seeded = [e.key for e in kept]
    _tally(outcome, kept)
    return outcome


def _tally(outcome: ReseedOutcome, kept: Sequence[LedgerEntry]) -> None:
    outcome.proven = sum(1 for e in kept if e.state is BodyState.PROVEN)
    outcome.unmeasured = len(kept) - outcome.proven


def _revert(keys: Iterable[FnLocator], src_dir: Path,
            writer: TreeWriter) -> None:
    keys = list(keys)
    if keys:
        writer.revert(keys, src_dir)


def _bisect_to_what_builds(
    kept: list[LedgerEntry], src_dir: Path, writer: TreeWriter,
    check_builds: BuildCheck, outcome: ReseedOutcome,
    log: Callable[[str], None] | None,
) -> list[LedgerEntry]:
    """The transaction's second half: find the largest subset that compiles.

    File first because a body that does not build usually does not build for a
    reason its whole file shares (a moved layout item the fingerprint's fallback
    let through, a cfg world the body assumed); unit second because one bad body
    must not cost its eleven neighbours. Everything rejected goes back to
    `unimplemented!()`, which is the state the caller would have had anyway."""
    outcome.builds += 1
    ok, _err = check_builds()
    if ok:
        return kept

    by_file: dict[str, list[LedgerEntry]] = {}
    for e in kept:
        by_file.setdefault(e.key.file, []).append(e)
    # Start from the stub tree: revert everything, then re-admit file by file.
    _revert([FnLocator(e.key.file, e.key.fn_name) for e in kept], src_dir,
            writer)
    admitted: list[LedgerEntry] = []
    for rel_file, group in sorted(by_file.items()):
        writer.splice([(FnLocator(e.key.file, e.key.fn_name), e.bodies)
                       for e in group], src_dir)
        outcome.builds += 1
        ok, _err = check_builds()
        if ok:
            admitted.extend(group)
            continue
        if log is not None:
            log(f"    [phase-c/ledger] {rel_file}: the file's "
                f"{len(group)} re-seeded body(ies) do not build together — "
                "bisecting to the unit")
        _revert([FnLocator(e.key.file, e.key.fn_name) for e in group], src_dir,
                writer)
        for e in group:
            writer.splice([(FnLocator(e.key.file, e.key.fn_name), e.bodies)],
                          src_dir)
            outcome.builds += 1
            ok, _err = check_builds()
            if ok:
                admitted.append(e)
            else:
                _revert([FnLocator(e.key.file, e.key.fn_name)], src_dir,
                        writer)
                outcome.rejected.append(e.key)
    return admitted
