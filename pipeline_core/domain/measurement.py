"""Measurement provenance for a tdd iteration (pure domain).

Pass counts were recorded with NO tree identity and NO test names, so a
session-to-session claim like "pass climbed 14→22" was unverifiable and the
oracle silently grew 61→68 between runs unflagged. A :class:`Measurement`
stamps every `phase_c.tdd.iter` event with the content hash of the fill tree it
measured, the oracle size (denominator), and the actual passing/failing test
names — the scientific control that makes cross-run comparison valid.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Measurement:
    """One tdd iteration's measured state, keyed to the exact tree it ran on."""

    cluster: str
    iteration: int
    tree_sha: str            # content hash over the cluster's fill-tree *.rs
    oracle_size: int         # tests that reported a verdict (the denominator)
    pass_count: int
    fail_count: int
    passed: frozenset[str]
    failed: frozenset[str]
    # Binary-qualified verdicts: (binary_label, test_name). A bare NAME is not
    # a verdict on this crate — one name runs in several binaries — so the
    # bare-name sets above cannot reconcile with the trailer counts in either
    # direction. See `reconcile`.
    passed_pairs: frozenset[tuple[str, str]] = frozenset()
    failed_pairs: frozenset[tuple[str, str]] = frozenset()
    # Binaries that opened a section and never printed a `test result:`
    # trailer. Their pre-crash `ok` lines ARE passes; the counter just never
    # saw them.
    incomplete: frozenset[str] = frozenset()
    # The denominator from the STATIC inventory, not `pass_count + fail_count`.
    # The derived form is a tautology (`fail_count` is itself computed as
    # `oracle - pass`), so no invariant stated over it can ever fail.
    oracle_static: int = 0

    def reconcile(self) -> VerdictReconciliation:
        """Explain any gap between the trailer COUNTS and the name SETS.

        Both were always emitted and they never agreed: live on
        `anyhow/nightly`, `pass_count=93` against `len(passed)=94` and
        `fail_count=30` against `len(failed)=26`, identically at every sweep
        since 07-30 20:09. The pinned handover instructs every session to
        rebuild score rows from these fields and to "never reason from the
        counter alone — diff `passed[]`", so both instructions were wrong by 1
        and by 3 and every delta quoted as a fact was ±3.

        It is ONE defect wearing two faces: names are not verdicts, and a
        crashed binary breaks the name↔execution mapping both ways at once.
        `tests/test_downcast.rs` aborts, so (a) names it printed `ok` for
        before dying are scraped into `passed[]` but its trailer never prints,
        and (b) the bare names it SHARES with a surviving binary are `observed`
        via that binary, so their lost executions can never enter `failed[]`.
        """
        return VerdictReconciliation(
            counter_pass=self.pass_count,
            qualified_pass=len(self.passed_pairs),
            counter_fail=self.fail_count,
            qualified_fail=len(self.failed_pairs),
            incomplete=self.incomplete,
            has_qualified=bool(self.passed_pairs or self.failed_pairs))

    def to_event(self, *, kind: str, rc: int, frontier_size: int,
                 name_cap: int = 200) -> dict[str, object]:
        """The `phase_c.tdd.iter` payload — historical keys plus provenance
        (tree_sha, oracle_size, capped name lists) plus the binary-qualified
        verdicts that make the two reconcilable."""
        rec = self.reconcile()
        return {
            "cluster": self.cluster, "iter": self.iteration, "rc": rc,
            "kind": kind, "frontier_size": frontier_size,
            "pass_count": self.pass_count, "fail_count": self.fail_count,
            "tree_sha": self.tree_sha, "oracle_size": self.oracle_size,
            "oracle_static": self.oracle_static,
            "passed": sorted(self.passed)[:name_cap],
            "failed": sorted(self.failed)[:name_cap],
            "passed_pairs": [list(p) for p in sorted(self.passed_pairs)][:name_cap],
            "failed_pairs": [list(p) for p in sorted(self.failed_pairs)][:name_cap],
            "incomplete": sorted(self.incomplete),
            "reconciled": rec.reconciled,
            "reconcile_note": rec.note,
        }


@dataclass(frozen=True, slots=True)
class VerdictReconciliation:
    """Whether a sweep's counters and its name sets tell the same story."""

    counter_pass: int
    qualified_pass: int
    counter_fail: int
    qualified_fail: int
    incomplete: frozenset[str]
    # Whether this sweep recorded binary-qualified verdicts at all. Events
    # written before they existed carry none, and "no pairs" is UN-ASSESSABLE,
    # not a mismatch — reporting those as unreconciled would flood a historical
    # replay with alarms about a check that could not run.
    has_qualified: bool = True

    @property
    def pass_gap(self) -> int:
        return self.qualified_pass - self.counter_pass

    @property
    def reconciled(self) -> bool:
        """True when the counters and the qualified verdicts agree.

        With every binary COMPLETE they must agree exactly; a gap then is a
        real parser defect and should be read as one. With a crashed binary a
        gap is EXPECTED and its size is bounded by what that binary managed to
        report before dying — so the honest verdict is "explained", not
        "equal", and the explanation is named rather than absorbed."""
        if not self.has_qualified:
            return True
        if self.incomplete:
            return self.pass_gap >= 0
        return self.pass_gap == 0

    @property
    def note(self) -> str:
        if not self.has_qualified:
            return ""
        if not self.incomplete:
            return ("" if self.pass_gap == 0 else
                    f"UNEXPLAINED: {self.qualified_pass} qualified pass(es) vs "
                    f"pass_count={self.counter_pass}, every binary completed")
        if self.pass_gap < 0:
            return (f"UNEXPLAINED: pass_count={self.counter_pass} exceeds the "
                    f"{self.qualified_pass} qualified pass(es) observed")
        return (f"{self.pass_gap} pass(es) reported by "
                f"{len(self.incomplete)} trailerless binary/binaries "
                f"({', '.join(sorted(self.incomplete))}) — counted by name, "
                "never by trailer")


def qualified_verdicts(
    binaries: Iterable[Any],
) -> tuple[frozenset[tuple[str, str]], frozenset[tuple[str, str]],
           frozenset[str]]:
    """`(passed_pairs, failed_pairs, incomplete_labels)` from per-binary
    results (`test_results.parse_binary_results`).

    Typed loosely on purpose: `domain.measurement` must not import
    `domain.test_results` (that module imports this one), and the only shape
    needed here is `label` / `passed` / `failed` / `completed`."""
    passed: set[tuple[str, str]] = set()
    failed: set[tuple[str, str]] = set()
    incomplete: set[str] = set()
    for b in binaries:
        # The binary's key: its label, world-qualified when the capture
        # joined worlds — the same binary in two worlds is two verdicts.
        label = str(getattr(b, "key", None) or getattr(b, "label", "?"))
        for name in getattr(b, "passed", ()):
            passed.add((label, str(name)))
        for name in getattr(b, "failed", ()):
            failed.add((label, str(name)))
        if not getattr(b, "completed", True):
            incomplete.add(label)
    return frozenset(passed), frozenset(failed), frozenset(incomplete)


def cargo_target_of(binary_label: str) -> str | None:
    """The cargo target name of an integration-test binary as the sweep
    labels it: `tests/foo.rs` and `tests/foo/main.rs` -> `foo`. None for
    any other label (the lib's unit tests, doc tests): those are one binary
    per crate and need no disambiguation by name.

    A world-qualified key (`nightly::tests/foo.rs`, `test_results.
    binary_key`, from a capture that joined build worlds) names the same
    target: the world is not part of the cargo target, and reading the key
    whole dropped every failure of another world from the coverage map
    (found in review, 2026-10-20). Split inline: this module may not import
    `test_results`, which imports it."""
    _world, _sep, label = binary_label.partition("::")
    parts = (label if _sep else binary_label).replace("\\", "/").split("/")
    if len(parts) == 2 and parts[0] == "tests" and parts[1].endswith(".rs"):
        return parts[1][:-3] or None
    if len(parts) == 3 and parts[0] == "tests" and parts[2] == "main.rs":
        return parts[1] or None
    return None


def failing_binaries_by_name(
    failed_pairs: Iterable[tuple[str, str]],
) -> dict[str, frozenset[str]]:
    """Failing test name -> the cargo targets of the binaries that failed
    it. A name with two targets is two failing tests: run
    `3893442-cb547388` (2026-09-19) failed `test_downcast_ref` in both
    `tests/test_context.rs` and `tests/test_downcast.rs`, and every reader
    keyed by the name alone measured one of them."""
    out: dict[str, set[str]] = {}
    for label, name in failed_pairs:
        target = cargo_target_of(str(label))
        if target is not None:
            out.setdefault(str(name), set()).add(target)
    return {k: frozenset(v) for k, v in out.items()}


def failing_binaries_by_world(
    failed_pairs: Iterable[tuple[str, str]],
    home: str,
) -> dict[str, dict[str, frozenset[str]]]:
    """World -> failing test name -> the cargo targets of the binaries that
    failed it in that world. A label with no world prefix is the home
    world's: a capture of one world carries none.

    `failing_binaries_by_name` drops the world on purpose, for readers that
    run in one env. A reader that runs each world under its own env needs
    it kept: run `1947461-b4eb65c3` (2026-10-24) failed
    `doc_error_example_6` only in `nightly::tests/doc_error_gen_6.rs`, a
    file the home world (`stable`) does not build, and the owner probe asked
    the home world for it and got no owner for 14 iterations."""
    out: dict[str, dict[str, set[str]]] = {}
    for label, name in failed_pairs:
        world, sep, rest = str(label).partition("::")
        world = world if sep else home
        target = cargo_target_of(rest if sep else str(label))
        names = out.setdefault(world, {})
        if target is not None:
            names.setdefault(str(name), set()).add(target)
        else:
            names.setdefault(str(name), set())
    return {w: {n: frozenset(ts) for n, ts in names.items()}
            for w, names in out.items()}


@dataclass(frozen=True, slots=True)
class PassDelta:
    """Whether two measurements' pass counts may be compared at all, and why."""

    comparable: bool
    verdict: str


def compare_measurements(prev: Measurement, cur: Measurement,
                         name_cap: int = 200) -> PassDelta:
    """Rule on a `prev.pass_count → cur.pass_count` claim.

    Stamping provenance is only half the control: a pass count rising while the
    DENOMINATOR moves is a measurement artefact, and reading it as progress is
    the failure this answers (14/oracle=69 → 35/oracle=111 on a tree whose
    content hash never changed — 21 of the 21 newly-passing names were tests
    that did not exist in the earlier suite).

    A delta is comparable only when both readings scored the SAME set of test
    names. `to_event` caps each name list at `name_cap`, so a reading that hit
    the cap degrades to the size check rather than pretending membership is
    known. Set size below `oracle_size` is NOT truncation — one test name runs in
    several binaries and counts once per verdict.
    """
    prev_names, cur_names = prev.passed | prev.failed, cur.passed | cur.failed
    truncated = any(len(names) >= name_cap for names in
                    (prev.passed, prev.failed, cur.passed, cur.failed))
    delta = cur.pass_count - prev.pass_count
    same_tree = prev.tree_sha == cur.tree_sha

    if truncated:
        if prev.oracle_size != cur.oracle_size:
            return PassDelta(False, (
                f"oracle size {prev.oracle_size}→{cur.oracle_size} and test names "
                f"are truncated — pass {prev.pass_count}→{cur.pass_count} is not "
                f"comparable"))
        return PassDelta(True, (
            f"oracle size held at {cur.oracle_size} (names truncated, membership "
            f"unverified) — pass delta {delta:+d}"))

    added, removed = cur_names - prev_names, prev_names - cur_names
    if added or removed:
        tree_note = (f"the fill tree never changed (sha {cur.tree_sha}), so the "
                     f"whole delta is measurement" if same_tree else
                     "the tree also changed, so progress cannot be separated "
                     "from the suite change")
        return PassDelta(False, (
            f"oracle membership moved (+{len(added)}/-{len(removed)} tests, size "
            f"{prev.oracle_size}→{cur.oracle_size}): pass {prev.pass_count}→"
            f"{cur.pass_count} is NOT comparable — {tree_note}"))

    if same_tree and delta != 0:
        return PassDelta(False, (
            f"same tree (sha {cur.tree_sha}) and same {cur.oracle_size}-test oracle "
            f"but pass moved {delta:+d} — the suite is flaky, not progressing"))

    return PassDelta(True, (
        f"same {cur.oracle_size}-test oracle, tree {prev.tree_sha}→{cur.tree_sha}: "
        f"pass delta {delta:+d} is real"))


def tree_content_sha(src_root: Path) -> str:
    """16-hex content hash over every `*.rs` under `src_root`, path-sorted so it
    is deterministic and identifies the exact tree a measurement ran on. Empty /
    missing tree → "0" * 16 (a stable sentinel, still comparable across runs)."""
    if not src_root.exists():
        return "0" * 16
    h = hashlib.sha256()
    for p in sorted(src_root.rglob("*.rs")):
        try:
            h.update(str(p.relative_to(src_root)).encode())
            h.update(b"\0")
            h.update(p.read_bytes())
            h.update(b"\0")
        except OSError:
            continue
    return h.hexdigest()[:16]


def combine_source_hashes(entries: Iterable[tuple[str, bytes]]) -> str:
    """Fold `(relative_path, contents)` pairs into one stable 12-hex digest.

    Order-independent by construction (sorted here, not by the caller's walk)
    and path-sensitive, so moving a function between two modules changes the
    identity even though the bytes did not."""
    h = hashlib.sha256()
    for rel, data in sorted(entries):
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(hashlib.sha256(data).digest())
    return h.hexdigest()[:12]


def code_identity(root: Path, *, entry: str = "pipeline.py",
                  package: str = "pipeline_core") -> str:
    """Content hash over the composition root + every module it composes —
    `tree_content_sha`'s exact counterpart for the CODE half of a measurement.

    Both identity signals in use were wrong at the same time on 07-31:

    * `git=<sha>` records HEAD, not the tree. The run armed at 10:34 logged
      `git=870ace5` while HEAD was `b60468f`, committed 2.5 h AFTER the process
      started, from files that had been uncommitted-on-disk at start. A dirty
      tree is the normal state here, so HEAD says nothing.
    * `pipeline.py=<md5_8>` — the sanctioned "same code?" check — sat unchanged
      at `bd16a271` across a session that added 279 lines to
      `retrigger_layout.py` and rewrote `impl_fill.py`, `fill_merge.py` and
      `test_results.py`. `pipeline.py` is a composition root by design, so the
      one file the signal covers is the one that no longer holds the logic.

    Every score claim in `handover.md` is "tree X under code Y", and Y was
    unrecoverable after the fact.

    Only `.py` files count: a changed prompt fixture or `.json` state file is
    data, not code, and folding it in would invalidate a resume for a reason
    that cannot change behaviour. Unreadable files are skipped rather than
    raising — an identity that crashes the run it labels is worse than a
    slightly coarse one."""
    entries: list[tuple[str, bytes]] = []
    entry_path = root / entry
    if entry_path.is_file():
        with suppress(OSError):
            entries.append((entry, entry_path.read_bytes()))
    pkg = root / package
    if pkg.is_dir():
        for p in sorted(pkg.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            # `pipeline_core/monitor/` is the standalone observer's library —
            # `monitor.py` is its only importer, and the import-linter
            # contract "The observer library is not composed into the
            # pipeline" (pyproject.toml) is what keeps this exclusion true
            # rather than merely written. Nothing the pipeline process
            # composes lives there, so an edit to it cannot change a loop
            # decision — but folded into this identity it invalidates every
            # resume keyed on `code_id`. Run `3097970-03bcf403` (2026-08-18):
            # an observer-only fix to `monitor/run_data.py` moved the id, the
            # TDD floor resumed `best_pass unset` with its pass cursor and
            # futility evidence discarded, and the crawl re-spent a
            # ~68-minute relaunch cycle re-earning state no pipeline
            # behaviour had invalidated.
            if p.relative_to(pkg).parts[0] == "monitor":
                continue
            try:
                entries.append((str(p.relative_to(root)), p.read_bytes()))
            except OSError:
                continue
    return combine_source_hashes(entries)
