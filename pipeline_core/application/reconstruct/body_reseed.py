"""The re-seed, transactional: write stored bodies, verify, keep only what holds.

WHY IT IS TRANSACTIONAL AND NOT A PLAIN WRITE. Every site that puts bodies into
a tree today writes and walks away, and the loop cannot recover from a tree that
does not compile: the unit-scoped fix route may only rewrite a body, never
remove a definition (the `E0592` duplicate-`debug` incident of 2026-08-11,
where every fix attempt re-checked to the identical error for two full passes),
and a tree that does not build scores 0 on every sweep with NO per-unit signal
to aim at — `nightly 0/124 for 8.7 h` in the score history is exactly that,
from a seed defect. A re-seed is the one write in this pipeline that is made of
code nobody just wrote, so it is also the one that must prove itself.

The transaction: splice everything the gate allows into one :class:`TreeTxn`,
verify (parse of every touched file, then `cargo check`), and if that refuses,
roll the BYTES back and bisect — by FILE first, then by unit inside the
failing file — each step its own transaction with its own pre-image, so a
rejected file is restored before the next file is tried and one bad body
never costs its neighbours or the file after it. The bisect is bounded by
`files + units_in_failing_files` builds, and the count is logged, because a
re-seed that spends more wall clock than the fills it saves is a defect this
module has to be able to show. It ends with a post-condition on the whole
tree: a re-seed that cannot leave the tree at least as buildable as it found
it restores every pre-image and says so on its own line.

WHY THE ROLLBACK IS A BYTE RESTORE. Until 2026-09-04 rejection reverted the
unit to `unimplemented!()` by re-locating its span BY NAME on the text as it
stood. Run `2973-66e50ad9` handed the splicer the same unit twice (the stub
scan was site-cardinality), the double splice corrupted `src/context.rs`, the
name-keyed revert ran over the wreck and left a residual `})`, every later
file was rejected for that file's reason, and the outcome line reported
`7 rejected (did not build)` about seven bodies that build. A pre-image does
not need the text to be sane.

WHAT IT NEVER DOES. It does not choose which units matter: the caller passes the
stubbed set. It does not remove a unit from the frontier, mark one exhausted, or
suppress a prompt. A re-seeded body leaves the unit filled and everything
downstream treats it exactly as it treats any other filled unit.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pipeline_core.application.reconstruct.tree_txn import (
    BuildCheck,
    ParseValidator,
    TreeTxn,
    unparseable_files,
)
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

SpliceFn = Callable[[Mapping[FnLocator, Sequence[str]], TreeTxn],
                    tuple[list[FnLocator], list[tuple[FnLocator, str]]]]
SignaturesFn = Callable[[FnLocator, Path], tuple[str, ...]]
BodiesFn = Callable[[FnLocator, Path], tuple[str, ...]]

# The post-condition's three readings of the tree the site hands back.
POST_BUILDS = "builds"
POST_PARSES = "parses"       # parse-clean, no build checker wired
POST_RESTORED = "restored"   # verification refused; every pre-image put back
POST_UNCHECKED = "unchecked"


@dataclass(frozen=True, slots=True)
class TreeWriter:
    """The tree operations a re-seed needs, injected rather than imported.

    `adapters/fn_reseed.py` implements all three, and the layer contract
    (adapters > application) is why they arrive as callables: this module
    decides WHAT goes back on the tree and the adapter knows how to write it.
    There is no `revert`: undoing a write is the transaction's byte restore."""

    splice: SpliceFn
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
    taken: int = 0
    post_state: str = POST_UNCHECKED

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
        if self.taken == 0:
            # Nothing passed the gate, so there was nothing to check. Saying
            # "no checker wired" here sent a reader to the composition root
            # (run `330242-0be2c3e1` iter 3, 2026-09-03).
            parts.append("nothing to build-check")
        elif not self.checked:
            parts.append("NOT build-checked (no checker wired)")
        elif self.builds:
            parts.append(f"{self.builds} cargo check(s)")
        if self.taken and self.post_state != POST_UNCHECKED:
            parts.append(f"tree after: {self.post_state}")
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
            "taken": self.taken, "post_state": self.post_state,
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


def definition_checker(skeleton_root: Path) -> Callable[[LayoutDep], bool]:
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


def _units_of(entries: Iterable[LedgerEntry]) -> dict[FnLocator, Sequence[str]]:
    return {FnLocator(e.key.file, e.key.fn_name): e.bodies for e in entries}


def reseed(
    *, site: str, entries: Iterable[LedgerEntry], wanted: Iterable[FnLocator],
    project: str, cluster: str, src_dir: Path, skeleton_root: Path,
    writer: TreeWriter, oracle_fp: str,
    check_builds: BuildCheck | None = None,
    validate_parse: ParseValidator | None = None,
    log: Callable[[str], None] | None = None,
) -> ReseedOutcome:
    """Put back every body the ledger can vouch for, and keep only what holds.

    `wanted` is the set of units the caller wants filled, and every one of them
    must currently be a STUB: the transaction's fallback is the pre-image, so
    a unit that arrived here already filled would simply be left as it was,
    but the ledger would have been asked about a body the tree already holds.
    At a retrigger seam that set is the units it just reverted; at cluster
    entry it is the whole stub set. Repeats in `wanted` are collapsed here —
    a unit is one key however many sites carry its name. Units the ledger has
    nothing for are silently absent: an empty ledger makes this a no-op, which
    is what makes the write path shippable before any read path exists.

    `oracle_fp` is the fingerprint of the test set scoring `src_dir`, computed
    by the CALLER (`adapters/oracle_fingerprint.cluster_oracle_fp`, through
    the driver's dep) — this module reads the layout to fingerprint it and
    the tree to read signatures, and it deliberately does not also decide what
    the oracle is. A row graded under another oracle is skipped as `oracle
    moved`, which the outcome line counts by reason like every other refusal.

    `validate_parse` is the structure reader's parse check; with it every
    touched file is parse-verified even when no build checker is wired, so a
    mechanical write can never persist an unparseable file. `check_builds`
    adds `cargo check` and enables the bisect.
    """
    outcome = ReseedOutcome(site=site)
    wanted_keys = [LedgerKey(project=project, cluster=cluster, file=k.file,
                             fn_name=k.fn_name)
                   for k in dict.fromkeys(FnLocator(w.file, w.fn_name)
                                          for w in wanted)]
    if not wanted_keys:
        return outcome
    file_fp = skeleton_fingerprints(skeleton_root)
    sig_sha = {
        key: sig_sha_of(writer.signatures(
            FnLocator(key.file, key.fn_name), src_dir))
        for key in wanted_keys}
    plan = plan_reseed(entries, wanted=wanted_keys, file_fp=file_fp,
                       sig_sha=sig_sha, oracle_fp=oracle_fp,
                       definition_present=definition_checker(skeleton_root))
    outcome.skipped = list(plan.skipped)
    if not plan.take:
        return outcome
    outcome.taken = len(plan.take)

    root = TreeTxn(src_dir, validate_parse=validate_parse,
                   build_check=check_builds)
    spliced, refused = writer.splice(_units_of(plan.take), root)
    for key, why in refused:
        outcome.skipped.append(
            (LedgerKey(project, cluster, key.file, key.fn_name), why))
    kept = [e for e in plan.take
            if FnLocator(e.key.file, e.key.fn_name) in set(spliced)]
    if not kept:
        root.commit()
        return outcome

    if check_builds is None:
        # No build checker wired (a unit test, or a site whose caller has not
        # been given one yet). The parse invariant still holds — a touched
        # file that no longer parses puts every pre-image back — and the line
        # says the build was not verified rather than implying it was.
        verdict = root.close()
        if verdict.ok:
            outcome.seeded = [e.key for e in kept]
            outcome.post_state = (POST_PARSES if validate_parse is not None
                                  else POST_UNCHECKED)
        else:
            outcome.rejected = [e.key for e in kept]
            outcome.post_state = POST_RESTORED
            if log is not None:
                log(f"    [phase-c/ledger] re-seed refused without a build "
                    f"check — {verdict.reason}; every pre-image restored")
        _tally(outcome, kept if verdict.ok else [])
        return outcome

    outcome.checked = True
    kept = _bisect_to_what_builds(kept, root, src_dir, writer, check_builds,
                                  validate_parse, outcome, log)
    outcome.seeded = [e.key for e in kept]
    _tally(outcome, kept)
    return outcome


def restore_prior(
    *, rows: Sequence[LedgerEntry], src_dir: Path, writer: TreeWriter,
    check_builds: BuildCheck | None = None,
    validate_parse: ParseValidator | None = None,
) -> tuple[list[LedgerEntry], str]:
    """Put back the bodies each row's landing replaced (`prior_bodies`), as
    ONE transaction: all of them hold together or none is written.

    The caller is the sweep, for landings it graded REFUTED because a
    convicting test now panics at an earlier line — a regression the fill's
    keep did not catch and the batch ratchet cannot see (the pass count did
    not move). One transaction because those landings came from one fill and
    were judged by one reading; restoring half of them would leave a tree
    no reading describes. Returns `(restored rows, reason)`; `reason` is ""
    on success and says why nothing was written otherwise. A row without
    `prior_bodies` (a unit that did not exist before its landing, or a row
    written before the field) is not restorable and is left out."""
    take = [r for r in rows if r.prior_bodies]
    if not take:
        return [], "no row carries the bodies its landing replaced"
    txn = TreeTxn(src_dir, validate_parse=validate_parse,
                  build_check=check_builds)
    spliced, refused = writer.splice(
        {FnLocator(r.key.file, r.key.fn_name): r.prior_bodies for r in take},
        txn)
    if refused:
        txn.rollback()
        return [], "; ".join(f"{k.file}::{k.fn_name}: {why}"
                             for k, why in refused)
    verdict = txn.close()
    if not verdict.ok:
        return [], verdict.reason
    done = set(spliced)
    return [r for r in take if FnLocator(r.key.file, r.key.fn_name) in done], ""


def _tally(outcome: ReseedOutcome, kept: Sequence[LedgerEntry]) -> None:
    outcome.proven = sum(1 for e in kept if e.state is BodyState.PROVEN)
    outcome.unmeasured = len(kept) - outcome.proven


def _bisect_to_what_builds(
    kept: list[LedgerEntry], root: TreeTxn, src_dir: Path, writer: TreeWriter,
    check_builds: BuildCheck, validate_parse: ParseValidator | None,
    outcome: ReseedOutcome, log: Callable[[str], None] | None,
) -> list[LedgerEntry]:
    """The transaction's second half: find the largest subset that holds.

    `root` already holds every spliced body and the pre-image of every file it
    touched. File first because a body that does not build usually does not
    build for a reason its whole file shares (a moved layout item the
    fingerprint's fallback let through, a cfg world the body assumed); unit
    second because one bad body must not cost its eleven neighbours. Each
    step is its own transaction over the tree as the previous steps left it,
    and a refused step is undone by BYTE restore before the next is tried —
    so no step is ever judged on another step's wreckage. Everything
    rejected ends up exactly as it was, which is the state the caller would
    have had anyway."""
    outcome.builds += 1
    verdict = root.verify()
    if verdict.ok:
        root.commit()
        outcome.post_state = POST_BUILDS
        return kept
    # Back to the untouched tree — bytes, not a re-derivation — and re-admit
    # file by file from there.
    root.rollback()

    by_file: dict[str, list[LedgerEntry]] = {}
    for e in kept:
        by_file.setdefault(e.key.file, []).append(e)
    admitted: list[LedgerEntry] = []
    for rel_file, group in sorted(by_file.items()):
        file_txn = TreeTxn(src_dir, validate_parse=validate_parse,
                           build_check=check_builds)
        writer.splice(_units_of(group), file_txn)
        outcome.builds += 1
        if file_txn.close().ok:
            admitted.extend(group)
            continue
        if log is not None:
            log(f"    [phase-c/ledger] {rel_file}: the file's "
                f"{len(group)} re-seeded body(ies) do not build together — "
                "bisecting to the unit")
        for e in group:
            unit_txn = TreeTxn(src_dir, validate_parse=validate_parse,
                               build_check=check_builds)
            writer.splice(_units_of([e]), unit_txn)
            outcome.builds += 1
            if unit_txn.close().ok:
                admitted.append(e)
            else:
                outcome.rejected.append(e.key)

    # The post-condition: the tree handed back must parse everywhere and
    # build. Every step above verified itself, so a failure here is a defect
    # in this module's own accounting — and it is still cheaper to restore
    # every pre-image and say so than to hand the loop a red tree it cannot
    # attribute to any unit.
    broken = (unparseable_files(Path(src_dir), validate_parse)
              if validate_parse is not None else ())
    outcome.builds += 1
    ok, err = check_builds()
    if broken or not ok:
        root.rollback()
        if log is not None:
            why = ("unparseable: " + ", ".join(broken) if broken
                   else "does not build: " + (err.strip().splitlines() or ["?"])[0])
            log(f"    [phase-c/ledger] re-seed left the tree unbuildable "
                f"after its own bisect ({why}) — restoring every pre-image; "
                "nothing re-seeded")
        outcome.rejected = [e.key for e in kept]
        outcome.post_state = POST_RESTORED
        return []
    root.commit()
    outcome.post_state = POST_BUILDS
    return admitted
