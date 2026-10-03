"""Which functions a cluster compiles does some test execute — measured.

Test augmentation used to stop on one number: a crate-wide LINE percentage.
A function no test enters and a function with one missed branch were the same
kind of gap to it, and the stop fired at 95.2 % on anyhow's `nightly` cluster
(task0c, run `330242-0be2c3e1`, 2026-09-03) with `src/wrapper.rs`'s
`provide` never executed by any test. Nothing downstream asked either: the
TDD fill loop dispatches only what a failing test reaches, so a body no test
enters stayed `unimplemented!()` through every gate and first surfaced at the
end-of-run stub gate as exit 1 (run `301146-2d8a5f06`, 2026-09-17).

This module is the criterion's pure half, shared by task0c (over the lcov
`FN`/`FNDA` records) and by the post-strip coverage slice (over the llvm-cov
JSON export):

- :class:`SiteIndex` names a coverage body at the extractor site whose line
  span holds the body's first line (innermost wins);
- :func:`executed_by_key` is the one join rule — a key is unexecuted only
  when every body under it counted 0 (instantiations of a generic and the
  per-binary copies of one fn collapse by it);
- :func:`candidate_records` is the set the criterion is about: `src/` fns the
  cluster compiles, not test code, not a body the original itself leaves
  hollow;
- :class:`FnCoverageRecord` is what augmentation persists per cluster and
  what the fill loop's close and the end gate read: executed, unexecuted
  with the targeted attempts spent so far, unreached (the attempt budget
  spent with the count still 0), unmeasured.

A function leaves the unexecuted set only on a measurement: a count above 0
makes it executed; a budget of targeted attempts whose test blocks compiled
and ran, with the count still 0, makes it unreached. No model classification
removes one.

A body the reading has no record for is UNMEASURED, never unexecuted and
never executed: rustc emits a zero-count record for an uncalled fn and for an
uninstantiated generic (`tests/integration/test_fn_execution_criterion.py`
pins that premise), so an absent record means the measurement could not see
the body — a file the coverage run ignores, a macro-generated item — and an
absence is not evidence either way.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from pipeline_core.domain.attribution import canon_site, record_site
from pipeline_core.domain.cfg import CfgSet, crate_cfg_predicates
from pipeline_core.domain.models import FN_EXECUTION_CRITERION as FN_EXECUTION_CRITERION
from pipeline_core.domain.models import FnRecord, FnSite, ModRecord
from pipeline_core.domain.oracle_immutability import is_oracle_fn

# `(crate-relative file, first line of the body)` -> execution count, max over
# every instantiation and every binary that carries a copy.
BodyCounts = Mapping[tuple[str, int], int]

class SiteIndex:
    """Which extractor site a coverage line belongs to, per file.

    An llvm-cov export names a function by its FIRST region, and for a Rust
    fn that region starts on the SIGNATURE line at the `fn` keyword's column
    (`[8, 5, 8, 61, …]` for `    fn fmt(` at line 8 of the `dual_fmt`
    fixture, computed 2026-09-03), while the extractor's `sig_byte_start` is
    the byte of that keyword. Resolving by byte containment of the line's
    column-0 offset therefore missed every indented fn — every impl method —
    and the first site resolver emptied the owner map for any test that
    entered only methods, which silently disarmed `unreached_by_test` and
    the dispatcher redirect built on it. Lines are the unit both sides
    agree on: a record spans from the line of `sig_byte_start` to the line
    of `body_byte_end`, and a coverage line resolves to the innermost record
    spanning it (a `fn` nested in a body is the inner one).

    The lcov `FN:<line>,<name>` record carries the same first-region line,
    and a closure's record starts inside its enclosing fn, so it resolves to
    that fn — a closure that ran is evidence its enclosing body ran."""

    def __init__(self, records: Iterable[FnRecord],
                 file_texts: Mapping[str, str]) -> None:
        self._spans: dict[str, list[tuple[int, int, FnSite]]] = {}
        for rec in records:
            text = file_texts.get(rec.file)
            if text is None:
                continue
            first = text.count("\n", 0, rec.sig_byte_start) + 1
            last = text.count("\n", 0, rec.body_byte_end) + 1
            self._spans.setdefault(rec.file, []).append(
                (first, last, record_site(rec)))

    def resolve(self, rel: str, line: int) -> tuple[FnSite, int, int] | None:
        """`(site, first_line, last_line)` of the innermost record spanning
        `line` in `rel`, or None when no crate fn does (a generated test, a
        doc-test shim, a file the enumerator did not see)."""
        best: tuple[int, int, FnSite] | None = None
        for first, last, site in self._spans.get(rel, ()):
            if first <= line <= last and (
                    best is None or last - first < best[1] - best[0]):
                best = (first, last, site)
        return None if best is None else (best[2], best[0], best[1])

    def site_at(self, rel: str, line: int) -> FnSite | None:
        hit = self.resolve(rel, line)
        return None if hit is None else hit[0]


def executed_by_key[K: Hashable](bodies: Iterable[tuple[K, int]]) -> dict[K, bool]:
    """The join rule: `key -> True` when any body under the key ran, `False`
    when every body under it counted 0. A key with no body is absent — the
    reading said nothing about it.

    Measured 2026-08-30 on the filled stable tree: collapsing by file+name
    without the every-body check reported `src/error.rs::backtrace` dead at
    line 918 (the ErrorImpl body) while the Error method at line 389 was live.
    The same rule collapses one fn's per-binary copies and a generic's
    instantiations: the lcov of an invented crate (2026-09-17) carries every
    `src/` fn once per binary under a different crate hash, the lib's copy at
    0 and the test binary's at the real count."""
    out: dict[K, bool] = {}
    for key, count in bodies:
        out[key] = out.get(key, False) or count > 0
    return out


def is_test_only(preds: Iterable[str]) -> bool:
    """True when the cfg chain compiles the item only under `cfg(test)` — a
    `#[cfg(test)]` helper, a `mod tests` member. `test` is a builtin atom
    `excludes_for_cargo` never decides, so it is asked directly."""
    no_test = CfgSet.of(cfg_false=["test"])
    return any(no_test.evaluate(p) is False for p in preds)


def mod_chain_active(file_name: str, mod_records: Sequence[ModRecord],
                     cs: CfgSet, depth: int = 0) -> bool:
    """Whether a `src/` file (`name` relative to `src/`) is compiled under
    `cs`: every cfg along one of its `mod` declaration chains up to the crate
    root keeps it. Dual-form declarations (`#[cfg(X)] mod m;` beside
    `#[cfg(not(X))] mod m;`) keep the file when ANY chain does. A file with no
    declaration found is kept — may under-exclude, never over-exclude."""
    stem = file_name.rsplit("/", 1)[-1].removesuffix(".rs")
    if stem in ("lib", "main") or depth > 32:
        return True
    base = file_name.rsplit("/", 1)[-1]
    matches = [r for r in mod_records if r.name == stem or r.file == base]
    if not matches:
        return True
    for rec in matches:
        if any(cs.excludes_for_cargo(p) for p in rec.cfg_predicates):
            continue
        if mod_chain_active(rec.declared_in, mod_records, cs, depth + 1):
            return True
    return False


def flipped_cfg_set(cfg_true: Iterable[str], cfg_false: Iterable[str],
                    default_features: Iterable[str] = ()) -> CfgSet:
    """The world with the cluster's own cfg identity inverted: its active
    atoms off, its excluded atoms on. Code this world compiles out depends on
    the cluster's identity — it is compiled only where the cluster's cfg
    holds (`#[cfg(X)]` for the `X` cluster, `#[cfg(not(X))]` for the default
    one)."""
    return CfgSet.of(cfg_true=cfg_false, cfg_false=cfg_true,
                     default_features=default_features)


def compiled_under(rec: FnRecord, cs: CfgSet,
                   mod_records: Sequence[ModRecord] | None,
                   file_texts: Mapping[str, str]) -> bool:
    """Whether the world `cs` compiles this `src/` fn record: its file's
    crate-level `#![cfg]`, one of its `mod` declaration chains, and its own
    cfg chain all keep it. The one rule for "a cluster compiles this body":
    the augmentation criterion's candidates and the end gate's dead-stub
    reading both ask it. A record whose file text is unknown is not compiled
    (nothing can be said about it)."""
    text = file_texts.get(rec.file)
    if text is None or not rec.file.startswith("src/"):
        return False
    if any(cs.evaluate(p.strip()) is False for p in crate_cfg_predicates(text)):
        return False
    if mod_records is not None and not mod_chain_active(
            rec.file[len("src/"):], mod_records, cs):
        return False
    return not any(cs.excludes_for_cargo(p) for p in rec.cfg_predicates)


@dataclass(frozen=True, slots=True)
class CandidateFn:
    """One function the criterion is about: the record plus whether the
    cluster's cfg identity is what compiles it."""

    record: FnRecord
    cluster_gated: bool

    @property
    def site(self) -> FnSite:
        return record_site(self.record)


def candidate_records(
    records: Iterable[FnRecord],
    *,
    cs: CfgSet,
    flipped: CfgSet,
    mod_records: Sequence[ModRecord] | None,
    file_texts: Mapping[str, str],
    hollow: Callable[[FnRecord], bool],
) -> list[CandidateFn]:
    """The `src/` fns the cluster compiles that some test ought to execute.

    Kept: a record under `src/` whose file the cluster compiles (mod chain
    and a file-level `#![cfg]`) and whose own cfg chain `excludes_for_cargo`
    does not rule out — the same evaluation the expected-test counter applies.
    Dropped: oracle content (a test fn, a `gen_tests` sibling, a test
    directory), a body compiled only under `cfg(test)`, and a body the tree
    itself leaves hollow (`hollow`), which pre-strip is exactly the set the
    reference records as the original's own stubs.

    `cluster_gated` is True when the flipped world (:func:`flipped_cfg_set`)
    does not compile the record: its tests need the cluster's gated symbols
    and a generated test for it is gated to the cluster."""
    out: list[CandidateFn] = []
    for rec in records:
        if not rec.fn_name or not rec.file.startswith("src/"):
            continue
        if is_oracle_fn(rec) or is_test_only(rec.cfg_predicates):
            continue
        if not compiled_under(rec, cs, mod_records, file_texts):
            continue
        if hollow(rec):
            continue
        gated = not compiled_under(rec, flipped, mod_records, file_texts)
        out.append(CandidateFn(rec, gated))
    return out


@dataclass(frozen=True, slots=True)
class FnTarget:
    """An unexecuted function as a generation target: where its lines are
    and whether the cluster's cfg identity compiles it."""

    site: FnSite
    first_line: int
    last_line: int
    cluster_gated: bool

    @property
    def label(self) -> str:
        return site_label(self.site)


def site_label(site: FnSite) -> str:
    base = f"{site.file}::{site.fn_name}"
    return f"{base} [{site.container}]" if site.container else base


@dataclass(frozen=True, slots=True)
class FunctionExecution:
    """One reading of the criterion: the candidate sites split by what the
    measurement says about them."""

    executed: frozenset[FnSite] = frozenset()
    unexecuted: tuple[FnTarget, ...] = ()
    unmeasured: tuple[FnSite, ...] = ()

    @property
    def candidates(self) -> int:
        return len(self.executed) + len(self.unexecuted) + len(self.unmeasured)


def function_execution(
    candidates: Sequence[CandidateFn],
    index: SiteIndex,
    counts: BodyCounts,
    file_texts: Mapping[str, str],
) -> FunctionExecution:
    """Join the reading to the candidates.

    `index` is built over EVERY record of the tree (test fns and inactive
    twins included), so a count on a nested fn or a test helper resolves to
    that record and never to the body around it. A candidate site no counted
    body resolves to is unmeasured."""
    by_site = executed_by_key(
        (hit[0], count)
        for (rel, line), count in counts.items()
        for hit in [index.resolve(rel, line)] if hit is not None)
    executed: set[FnSite] = set()
    unexecuted: dict[FnSite, FnTarget] = {}
    unmeasured: dict[FnSite, None] = {}
    for cand in candidates:
        site = cand.site
        state = by_site.get(site)
        if state is None:
            unmeasured.setdefault(site, None)
            continue
        if state:
            executed.add(site)
            continue
        text = file_texts.get(cand.record.file, "")
        first = text.count("\n", 0, cand.record.sig_byte_start) + 1
        last = text.count("\n", 0, cand.record.body_byte_end) + 1
        prior = unexecuted.get(site)
        if prior is None:
            unexecuted[site] = FnTarget(site, first, last, cand.cluster_gated)
        else:
            unexecuted[site] = FnTarget(
                site, min(prior.first_line, first), max(prior.last_line, last),
                prior.cluster_gated and cand.cluster_gated)
    return FunctionExecution(
        executed=frozenset(executed),
        unexecuted=tuple(unexecuted.values()),
        unmeasured=tuple(s for s in unmeasured if s not in executed
                         and s not in unexecuted))


def _site_json(site: FnSite) -> dict[str, str]:
    return {"file": site.file, "fn": site.fn_name, "container": site.container}


def _site_of(raw: Mapping[str, Any]) -> FnSite:
    return canon_site(FnSite(str(raw.get("file", "")), str(raw.get("fn", "")),
                             str(raw.get("container", "") or "")))


@dataclass(frozen=True, slots=True)
class PendingFn:
    """An unexecuted function and the targeted attempts already spent on it
    (outer augmentation attempt numbers — the `attempt` field of
    `.task0c_attempts.jsonl`). An attempt is a pass that sent the function in
    a prompt and got a test block back, whatever the block then did: a block
    that does not compile is an attempt that did not reach the function. A
    pass with no block at all (an empty or refused reply) is not one."""

    target: FnTarget
    attempts: tuple[int, ...] = ()
    # What each charged attempt's block did, parallel to `attempts` (`ran`,
    # `compile_failed`, `reverted`, `no_tests`): the budget counts aimed
    # passes, and this is why each one did not reach the function.
    outcomes: tuple[str, ...] = ()
    # The retrospective's classification of this function, when it named
    # exactly this site; evidence the next pass is asked to test, never a
    # reason to stop on its own.
    verdict: str = ""


@dataclass(frozen=True, slots=True)
class UnreachedFn:
    """The attempt budget was spent on this function and the measurement
    after the last attempt still counted 0."""

    site: FnSite
    attempts: tuple[int, ...] = ()
    cluster_gated: bool = False
    outcomes: tuple[str, ...] = ()
    verdict: str = ""


# How a stub reads against augmentation's record, for the gates downstream.
STUB_EXECUTED = "executed"
STUB_UNREACHED = "unreached"


@dataclass(frozen=True, slots=True)
class FnCoverageRecord:
    """What augmentation measured about function execution in one cluster.

    Persisted as `.cluster_augmented/<cluster>__<module>.fns.json` — names,
    counts and attempt numbers, no source. `measured` is False when no reading
    could be taken (the backend reports no function records, the extractor is
    unavailable): the record then accounts for nothing and says so."""

    criterion: str = FN_EXECUTION_CRITERION
    measured: bool = False
    candidates: int = 0
    executed: tuple[FnSite, ...] = ()
    pending: tuple[PendingFn, ...] = ()
    unreached: tuple[UnreachedFn, ...] = ()
    unmeasured: tuple[FnSite, ...] = ()
    reason: str = ""

    @property
    def accounted(self) -> bool:
        """No function is left that is unexecuted and still within its
        attempt budget. An unmeasured record accounts for nothing and so
        cannot hold the stop open — the caller logs why."""
        return not self.pending

    @property
    def gated_pending(self) -> int:
        return sum(1 for p in self.pending if p.target.cluster_gated)

    def summary(self) -> str:
        if not self.measured:
            return f"functions: not measured ({self.reason or 'no reading'})"
        return (f"functions: executed {len(self.executed)}/{self.candidates}, "
                f"unexecuted {len(self.pending)} (cluster-gated "
                f"{self.gated_pending}), unreached {len(self.unreached)}"
                + (f", unmeasured {len(self.unmeasured)}"
                   if self.unmeasured else ""))

    def targets(self) -> tuple[FnTarget, ...]:
        """Generation targets: every pending function, cluster-gated first."""
        return tuple(sorted((p.target for p in self.pending),
                            key=lambda t: (not t.cluster_gated, t.site.file,
                                           t.first_line)))

    def classify_stub(self, site: FnSite) -> str | None:
        """How a stub at `site` reads against this record: `executed`,
        `unreached`, or None when the record does not account for it (pending,
        unmeasured, or not in the record).

        The EXACT site only (`canon_site`, so token spacing in a container does
        not split one impl in two). A locator fallback was tried and refuted on
        run `301146-2d8a5f06`'s merged tree (2026-09-17): the probe-only
        `src/nightly.rs::provide [impl Error for MyError]`, compiled by no
        cluster, read "executed" because the free `provide` in the same file
        ran. Every real stub site of that run matched its record exactly; a site
        a layout re-author renamed reads as not accounted, which can keep a run
        failing and can never excuse a stub."""
        want = canon_site(site)
        statuses = self._statuses(lambda s: s == want)
        if STUB_EXECUTED in statuses:
            return STUB_EXECUTED
        if statuses and all(st == STUB_UNREACHED for st in statuses):
            return STUB_UNREACHED
        return None

    def speaks_about(self, site: FnSite) -> bool:
        """True when the record lists this exact site, in any state."""
        want = canon_site(site)
        return bool(self._statuses(lambda s: s == want))

    def unreached_entry(self, site: FnSite) -> UnreachedFn | None:
        want = canon_site(site)
        for u in self.unreached:
            if u.site == want:
                return u
        return None

    def _statuses(self, match: Callable[[FnSite], bool]) -> list[str]:
        out: list[str] = []
        out += [STUB_EXECUTED for s in self.executed if match(s)]
        out += ["pending" for p in self.pending if match(p.target.site)]
        out += [STUB_UNREACHED for u in self.unreached if match(u.site)]
        out += ["unmeasured" for s in self.unmeasured if match(s)]
        return out

    def to_json(self) -> str:
        return json.dumps({
            "criterion": self.criterion,
            "measured": self.measured,
            "reason": self.reason,
            "candidates": self.candidates,
            "executed": [_site_json(s) for s in self.executed],
            "unexecuted": [
                {**_site_json(p.target.site), "first_line": p.target.first_line,
                 "last_line": p.target.last_line,
                 "cluster_gated": p.target.cluster_gated,
                 "attempts": list(p.attempts), "outcomes": list(p.outcomes),
                 "verdict": p.verdict}
                for p in self.pending],
            "unreached": [
                {**_site_json(u.site), "cluster_gated": u.cluster_gated,
                 "attempts": list(u.attempts), "outcomes": list(u.outcomes),
                 "verdict": u.verdict}
                for u in self.unreached],
            "unmeasured": [_site_json(s) for s in self.unmeasured],
        }, indent=2) + "\n"

    @classmethod
    def from_json(cls, text: str) -> FnCoverageRecord | None:
        """None when the text is not a record of THIS criterion — an older or
        unreadable record accounts for nothing."""
        try:
            raw = json.loads(text)
        except ValueError:
            return None
        if not isinstance(raw, dict) or raw.get("criterion") != FN_EXECUTION_CRITERION:
            return None
        return cls(
            criterion=FN_EXECUTION_CRITERION,
            measured=bool(raw.get("measured", False)),
            reason=str(raw.get("reason", "")),
            candidates=int(raw.get("candidates", 0) or 0),
            executed=tuple(_site_of(s) for s in raw.get("executed", [])),
            pending=tuple(
                PendingFn(FnTarget(_site_of(p), int(p.get("first_line", 0)),
                                   int(p.get("last_line", 0)),
                                   bool(p.get("cluster_gated", False))),
                          tuple(int(a) for a in p.get("attempts", [])),
                          tuple(str(o) for o in p.get("outcomes", [])),
                          str(p.get("verdict", "")))
                for p in raw.get("unexecuted", [])),
            unreached=tuple(
                UnreachedFn(_site_of(u), tuple(int(a) for a in u.get("attempts", [])),
                            bool(u.get("cluster_gated", False)),
                            tuple(str(o) for o in u.get("outcomes", [])),
                            str(u.get("verdict", "")))
                for u in raw.get("unreached", [])),
            unmeasured=tuple(_site_of(s) for s in raw.get("unmeasured", [])),
        )


def unmeasured_record(reason: str) -> FnCoverageRecord:
    return FnCoverageRecord(measured=False, reason=reason)


def advance_record(prior: FnCoverageRecord | None, reading: FunctionExecution,
                   *, attempt_budget: int,
                   inherited: Mapping[FnSite, int] | None = None) -> FnCoverageRecord:
    """Fold a new reading into the record.

    A count above 0 makes a function executed, whatever it was before — an
    unreached function a later block happened to reach is executed. A
    function still at 0 keeps the attempts spent on it, and becomes unreached
    once `attempt_budget` targeted attempts are spent: the reading being
    folded is the measurement after the last of them.

    `inherited` is the attempts other clusters already spent on a site. It
    applies only to a function this cluster does not own (`cluster_gated`
    False — the same body is compiled in the other world) and only after this
    cluster has spent one attempt of its own: a test gated to this world can
    reach what the other world's tests could not, so the other world's
    verdict shortens the budget and never replaces the attempt."""
    spent: dict[FnSite, PendingFn | UnreachedFn] = {}
    if prior is not None:
        for p in prior.pending:
            spent[p.target.site] = p
        for u in prior.unreached:
            spent[u.site] = u
    pending: list[PendingFn] = []
    unreached: list[UnreachedFn] = []
    budget = max(1, attempt_budget)
    for target in reading.unexecuted:
        before = spent.get(target.site)
        attempts = before.attempts if before is not None else ()
        outcomes = before.outcomes if before is not None else ()
        verdict = before.verdict if before is not None else ""
        own = len(attempts)
        carried = 0 if target.cluster_gated else (inherited or {}).get(target.site, 0)
        if own >= budget or (own >= 1 and own + carried >= budget):
            unreached.append(UnreachedFn(target.site, attempts, target.cluster_gated,
                                         outcomes, verdict))
        else:
            pending.append(PendingFn(target, attempts, outcomes, verdict))
    return FnCoverageRecord(
        measured=True,
        candidates=reading.candidates,
        executed=tuple(sorted(reading.executed)),
        pending=tuple(pending),
        unreached=tuple(unreached),
        unmeasured=reading.unmeasured,
    )


def aimed_fn_outcomes(targets: Iterable[FnTarget], aimed_lines: Iterable[int],
                      ran_lines: Iterable[int], otherwise: str) -> dict[FnSite, str]:
    """Per target a prompt carried, what became of the block written for it:
    `ran` when a committed block's lines cover the function, else `otherwise`
    (why the block did not reach it). A target no aimed line falls in was not
    in a prompt that got a block back and is absent."""
    aimed = sorted(set(aimed_lines))
    ran = sorted(set(ran_lines))
    out: dict[FnSite, str] = {}
    for t in targets:
        if any(t.first_line <= ln <= t.last_line for ln in aimed):
            out[t.site] = ("ran" if any(t.first_line <= ln <= t.last_line for ln in ran)
                           else otherwise)
    return out


def unambiguous_verdicts(targets: Iterable[FnTarget],
                         categories: Mapping[str, Iterable[str]]) -> dict[FnSite, str]:
    """The retrospective names a function by bare name. It attributes to a
    site only when that name is exactly one target's in the file and exactly
    one category's: three `clone` impls and one verdict is not evidence about
    any of them."""
    ts = list(targets)
    out: dict[FnSite, str] = {}
    for t in ts:
        name = t.site.fn_name
        if sum(1 for o in ts if o.site.fn_name == name) != 1:
            continue
        said = [cat for cat, names in categories.items() if name in set(names)]
        if len(said) == 1:
            out[t.site] = said[0]
    return out


def inherited_attempts(others: Iterable[FnCoverageRecord]) -> dict[FnSite, int]:
    """The attempts other clusters spent per site, for `advance_record`.
    Only a shared body counts there: an entry another cluster owns
    (`cluster_gated`) is code this world does not compile."""
    out: dict[FnSite, int] = {}
    for rec in others:
        if not rec.measured:
            continue
        for p in rec.pending:
            if not p.target.cluster_gated:
                out[p.target.site] = out.get(p.target.site, 0) + len(p.attempts)
        for u in rec.unreached:
            if not u.cluster_gated:
                out[u.site] = out.get(u.site, 0) + len(u.attempts)
    return out


def note_attempt(record: FnCoverageRecord, sites: Iterable[FnSite],
                 attempt: int, *,
                 outcomes: Mapping[FnSite, str] | None = None) -> FnCoverageRecord:
    """Charge one targeted attempt to each pending function in `sites` — the
    functions a test block was generated for this pass (compiled or not; see
    `PendingFn`).

    Called once per generation pass. `attempt` is that pass's outer attempt
    number as `.task0c_attempts.jsonl` records it; the number restarts at 1
    when the cluster loop re-enters augmentation for another module pass, so
    it is evidence to quote, never a key to de-duplicate on. `outcomes` is
    what the block did, per site (`ran` when absent)."""
    hit = {canon_site(s) for s in sites}
    if not hit:
        return record
    why = {canon_site(k): v for k, v in (outcomes or {}).items()}
    pending = tuple(
        replace(p, attempts=(*p.attempts, attempt),
                outcomes=(*p.outcomes, why.get(p.target.site, "ran")))
        if p.target.site in hit else p
        for p in record.pending)
    return replace(record, pending=pending)


def note_verdicts(record: FnCoverageRecord,
                  verdicts: Mapping[FnSite, str]) -> FnCoverageRecord:
    """Attach the retrospective's classification to the pending functions it
    named. Evidence only: it changes no budget."""
    if not verdicts:
        return record
    said = {canon_site(k): v for k, v in verdicts.items()}
    pending = tuple(
        replace(p, verdict=said[p.target.site]) if p.target.site in said else p
        for p in record.pending)
    return replace(record, pending=pending)


@dataclass(frozen=True, slots=True)
class StubAccount:
    """The cluster-close and end-of-run reading of still-stubbed sites
    against augmentation's records."""

    executed: tuple[FnSite, ...] = ()
    unreached: tuple[tuple[FnSite, UnreachedFn], ...] = ()
    unaccounted: tuple[FnSite, ...] = ()


def classify_stub_sites(sites: Iterable[FnSite],
                        records: Sequence[FnCoverageRecord]) -> StubAccount:
    """Split stub sites by what augmentation measured about them.

    `executed` when any measured record says a test ran the body. `unreached`
    only when EVERY measured record that speaks about the site says
    unreached — a shared function one cluster's tests executed, or one still
    inside another cluster's attempt budget, is not excused. Everything else,
    including a site no record speaks about, is unaccounted."""
    measured = [r for r in records if r.measured]
    executed: list[FnSite] = []
    unreached: list[tuple[FnSite, UnreachedFn]] = []
    unaccounted: list[FnSite] = []
    for site in sites:
        speaking = [r for r in measured if r.speaks_about(site)]
        verdicts = [r.classify_stub(site) for r in speaking]
        if STUB_EXECUTED in verdicts:
            executed.append(site)
            continue
        if speaking and all(v == STUB_UNREACHED for v in verdicts):
            entry = speaking[0].unreached_entry(site)
            if entry is not None:
                unreached.append((site, entry))
                continue
        unaccounted.append(site)
    return StubAccount(executed=tuple(executed), unreached=tuple(unreached),
                       unaccounted=tuple(unaccounted))
