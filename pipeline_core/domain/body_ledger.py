"""The per-unit proven-body ledger: which body was right, under which layout.

WHAT WAS MISSING. The pipeline's only cross-run store,
`.phase_c_logs/retrospectives.json`, holds 39 records over 19 fields and every
one of them is a NEGATIVE claim — `category`, `guidance`, `error_count`,
`failing_test_count`, `convicted_by`, `fully_exhausted`, `blocked_on`,
`reset_attempts`. There is no field, and was no file, recording which body was
proven correct. So every mechanism that destroys progress ON PURPOSE — the
retrigger's re-stub, the ratchet's restore, the merge, the augmented restart —
forced the loop to re-derive work it had already done, through the model, which
is the one part of the system where every known failure class lives (routing
drops, splice refusals, poisoned convictions, cfg-world mismatch, truncated
generics). Run `259637-36db9aad` (2026-08-07) is the measured cost: the
retrigger reverted 56 of 178 filled bodies, the next sweep read 21 against a
high-water of 81, and nine hours, 148 landed fills and +5 tests later the run
had no exit. It had not LOST those bodies. It had lost the addressing of them.

This module is the addressing. It is pure domain: identity, the layout gate,
the promotion rule and the re-seed decision. Storage is
`adapters/body_ledger_store.py`; the sites that write and read are
`application/reconstruct/phase_c_tdd.py` and `application/reconstruct/
body_reseed.py`.

THE KEY IS `FnLocator`, NOT `RetroKey`, and that is forced by the seams rather
than chosen for simplicity. `landed_change.unit_bodies` keys on
`(file, fn_name)` and its value is the ordered tuple of EVERY matching
occurrence — deliberately, because `src/context.rs::source` is two impls and
the escalation's unit is the pair — and `fn_revert.revert_fns_to_stub` reverts
by the same key. What the re-seed restores is exactly the tuple the revert
removed, so the container precision `RetroKey` carries is precision this store
would have to discard.

THE BODIES ARE STORED RAW. `unit_bodies` hands back `_canon_body(...)`, i.e.
`" ".join(text.split())`. That form is an IDENTITY and must never be spliced:
a body holding a `//` comment, folded onto one line, comments out its own
remainder, and the result is a broken body in a tree whose only symptom is a
compile error the model gets blamed for. The canonical form is what `body_sha`
digests; the raw occurrence text is what goes back on the tree.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pipeline_core.domain.models import FnLocator

GATE_VERSION = "bl1"
"""Bumped when the layout gate's meaning changes.

An entry whose `gate_ver` is not this one is never re-seeded. Bumping costs a
re-derivation of the affected units and can never cost a bad splice, which is
the right direction for a gate whose failure mode is a tree that does not
build."""


class BodyState(StrEnum):
    """What the record says about a body, and what it licenses.

    The four exist because three did not survive contact with the loop. "The
    attributed tests are at or above the prior reading" promotes the
    unmeasurable — a body whose tests are blocked by a still-stubbed sibling
    moves nothing, so `0 >= 0` would promote every such body and the word
    `proven` would carry no evidence at all (in run `259637-36db9aad`'s last
    iteration, 17 of 17 frontier units were in exactly that state). And it
    refutes across destructions: a body whose tests drop because a SIBLING was
    re-stubbed would be marked refuted, which is the one state that suppresses
    re-seeding forever."""

    LANDED = "landed"
    """A semantic body change took. No sweep has spoken about it yet."""
    UNMEASURED = "unmeasured"
    """The attributed tests did not RUN, or the reading cannot be compared:
    a crashed binary, a cfg-excluded test, a test blocked behind a stubbed
    sibling, a comparison spanning a destruction epoch, or a body that is no
    longer the one on the tree. Re-seedable — an unmeasured body is worth
    putting back and is NOT evidence about anything."""
    PROVEN = "proven"
    """The attributed tests ran, inside one destruction epoch, with this body
    on the tree, none of them regressed, and at least one of them PASSES.
    That last clause is what stops the word drifting back to "did not break
    anything": a unit whose tests are all still red has not been shown to be
    right."""
    REFUTED = "refuted"
    """The attributed tests ran under those same conditions and regressed.
    Never re-seeded. This is not a conviction and the retro gate must not read
    it: it says one BODY was wrong, not that a unit is hard."""
    FLAT = "flat"
    """The unit was dispatched under an escalated REWRITE grade, its
    attributed convicting tests had run, and the model returned a body
    IDENTICAL to the one already on the tree — a graded try with an empty
    verdict delta. A fill that spliced then RESTORED the snapshot (peel of
    compiler-blamed bodies, compile / U2 / regression revert) is not this
    state: the tree body is the pre-fill one, not the reply.

    Before this state existed the row was never written: the landed-change
    audit appended rows from the landed set only, and an escalated unit that
    returned unchanged was logged under `ESCALATED, unchanged` and dropped.
    Run `82157-476ddc16` iter 1 (2026-08-26) dispatched twelve escalated
    units, nine of them (`src/error.rs::construct`, `construct_from_adhoc`,
    `construct_from_boxed`, `construct_from_context`, `construct_from_display`,
    `construct_from_std`, `context`, `msg`, `new`) came back unchanged, and
    the ledger held no trace of the try — so the next process's re-buy
    detector, which joins on prior graded rows, could not fire on them.

    NOT re-seedable: an unchanged body under a rewrite instruction is the
    later measurement of a body whose convicting tests still fail, and a
    re-seed must not vouch for it on the strength of an earlier, weaker pass.
    `reseedable` stays PROVEN/UNMEASURED only."""


@dataclass(frozen=True, slots=True)
class LedgerKey:
    """One unit in one cluster of one project. `file` is repo-relative."""

    project: str
    cluster: str
    file: str
    fn_name: str

    def as_str(self) -> str:
        return f"{self.project}/{self.cluster}/{self.file}::{self.fn_name}"


@dataclass(frozen=True, slots=True)
class LayoutDep:
    """One authored item a body depends on, as it stood when the body landed.

    The definition TEXT is kept, not merely its digest, for two reasons: the
    gate's fallback asks "is this exact definition still in the tree", which is
    a search rather than a comparison, and a mismatch is rendered into the
    unit's prompt as "a body proven under a different layout of this file",
    which needs the old text to diff against the new one."""

    name: str
    kind: str
    definition: str
    file: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "kind": self.kind,
                "definition": self.definition, "file": self.file}

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> LayoutDep:
        return LayoutDep(name=str(raw.get("name") or ""),
                         kind=str(raw.get("kind") or ""),
                         definition=str(raw.get("definition") or ""),
                         file=str(raw.get("file") or ""))


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """A claim about one unit's body under one authored layout AND one
    oracle.

    `oracle_fp` is the fingerprint of the test set that graded the row
    (`oracle_fingerprint`): the cluster tree's `*.gen_tests_*.rs` siblings,
    the crate's `tests/**/*.rs`, and the baked behaviour suite. It exists
    because layout identity alone let a body proven under one oracle come
    back on a tree graded by another: measured 2026-08-26 on the anyhow
    ledger before the planned cold run (which re-runs t0c and therefore
    regenerates the gen_tests), 568 stable rows collapsed to 85 latest units,
    62 PROVEN + 23 UNMEASURED, and against the live `.skeleton/stable` the
    gate would TAKE 73 of them — all 10 files' layout fingerprints matched —
    every one graded by tests the cold run was about to replace. An empty
    value means nobody recorded the oracle; the gate treats that as a moved
    oracle (see `reseed_allowed`)."""

    key: LedgerKey
    body_sha: str
    bodies: tuple[str, ...]           # RAW occurrence texts, in splice order
    sig_sha: str
    layout_fp: str
    layout_deps: tuple[LayoutDep, ...] = ()
    state: BodyState = BodyState.LANDED
    verdict_delta: tuple[tuple[str, str], ...] = ()   # (test, "gained"|"lost")
    run_id: str = ""
    iteration: int = 0
    epoch: int = 0
    gate_ver: str = GATE_VERSION
    source: str = "live"              # "live" | a backfill source label
    rejected_builds: int = 0          # times a re-seed of this body did not build
    oracle_fp: str = ""               # fingerprint of the oracle that graded it

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.key.project, "cluster": self.key.cluster,
            "file": self.key.file, "fn": self.key.fn_name,
            "body_sha": self.body_sha, "bodies": list(self.bodies),
            "sig_sha": self.sig_sha, "layout_fp": self.layout_fp,
            "layout_deps": [d.to_dict() for d in self.layout_deps],
            "state": str(self.state), "verdict_delta":
                [list(p) for p in self.verdict_delta],
            "run_id": self.run_id, "iteration": self.iteration,
            "epoch": self.epoch, "gate_ver": self.gate_ver,
            "source": self.source, "rejected_builds": self.rejected_builds,
            "oracle_fp": self.oracle_fp,
        }

    @staticmethod
    def from_dict(raw: Any) -> LedgerEntry | None:
        """Parse one index row, or None when it is absent or malformed.

        Total by design, exactly as `TddFloor.from_dict` is: this runs at
        cluster entry over an append-only file that a killed process may have
        left with a half-written last line, and a corrupt row must cost one
        entry, never the run."""
        if not isinstance(raw, dict):
            return None
        try:
            state = BodyState(str(raw.get("state") or "landed"))
        except ValueError:
            return None
        try:
            return LedgerEntry(
                key=LedgerKey(project=str(raw["project"]),
                              cluster=str(raw["cluster"]),
                              file=str(raw["file"]), fn_name=str(raw["fn"])),
                body_sha=str(raw["body_sha"]),
                bodies=tuple(str(b) for b in (raw.get("bodies") or ())),
                sig_sha=str(raw.get("sig_sha") or ""),
                layout_fp=str(raw.get("layout_fp") or ""),
                layout_deps=tuple(
                    LayoutDep.from_dict(d)
                    for d in (raw.get("layout_deps") or ())
                    if isinstance(d, dict)),
                state=state,
                verdict_delta=tuple(
                    (str(p[0]), str(p[1]))
                    for p in (raw.get("verdict_delta") or ())
                    if isinstance(p, (list, tuple)) and len(p) == 2),
                run_id=str(raw.get("run_id") or ""),
                iteration=int(raw.get("iteration") or 0),
                epoch=int(raw.get("epoch") or 0),
                gate_ver=str(raw.get("gate_ver") or ""),
                source=str(raw.get("source") or "live"),
                rejected_builds=int(raw.get("rejected_builds") or 0),
                # Missing on every row written before the field existed:
                # parses to "" and the gate refuses it once (`reseed_allowed`).
                oracle_fp=str(raw.get("oracle_fp") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return None

    @property
    def reseedable(self) -> bool:
        """States a re-seed may draw from. `LANDED` is excluded on purpose:
        it means no sweep has judged the body on THIS code, which is exactly
        what a backfilled entry is, and putting an unjudged body on the tree
        is how a seed defect becomes a 0-scoring sweep with no per-unit signal
        (`nightly 0/124 for 8.7 h`)."""
        return self.state in (BodyState.PROVEN, BodyState.UNMEASURED)


# --- identity ----------------------------------------------------------------

def canon_body(text: str) -> str:
    """A body's semantic identity: whitespace runs folded.

    The same fold `landed_change._canon_body` uses, restated here rather than
    imported so the two stores cannot drift apart silently — if this ever needs
    to differ, the difference has to be written down."""
    return " ".join(text.split())


def body_sha(bodies: Sequence[str]) -> str:
    h = hashlib.sha256()
    for b in bodies:
        h.update(canon_body(b).encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def sig_sha_of(sigs: Sequence[str]) -> str:
    """Identity of a unit's SIGNATURE set, folded exactly like its bodies.

    A body answers to its own signature before it answers to any layout item,
    and the signature is the one thing a re-seed can check without reading the
    layout at all — so it is a separate condition in the gate rather than a
    component of the layout fingerprint."""
    return body_sha(sigs)


def text_sha(text: str) -> str:
    return hashlib.sha256(canon_body(text).encode("utf-8")).hexdigest()[:16]


def strip_comments(text: str) -> str:
    """Rust source with `//` line comments and `/* */` blocks removed.

    Deliberately naive about strings containing comment markers: this feeds a
    fingerprint, and the only cost of over-stripping is a fingerprint that
    moves when a string literal changes — a refused re-seed, which is the safe
    direction. Nesting is honoured because Rust block comments nest."""
    out: list[str] = []
    i, n, depth = 0, len(text), 0
    while i < n:
        two = text[i:i + 2]
        if depth:
            if two == "/*":
                depth += 1
                i += 2
                continue
            if two == "*/":
                depth -= 1
                i += 2
                continue
            i += 1
            continue
        if two == "/*":
            depth = 1
            i += 2
            continue
        if two == "//":
            j = text.find("\n", i)
            if j == -1:
                break
            out.append("\n")
            i = j + 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def authored_layout_fp(file_text: str) -> str:
    """The fingerprint of ONE cluster skeleton file: comments stripped,
    whitespace folded, digested.

    WHY NOT `retro_gate.layout_fingerprint`. That one digests
    `rust_scan.struct_names` — `struct NAME` items and their field sets, and
    nothing else. t1.5 authors five kinds (`AuthoredItem.kind` is
    `struct | enum | type | trait | fn`), so an enum variant set, a trait's
    method set, a type alias and a free fn signature can all move with it
    unchanged, and a body re-seeded across such a move does not compile. Worse,
    `backtrace.rs` and `macros.rs` declare no struct at all and therefore digest
    to `d41d8cd9` — md5 of the empty string — so for those files that gate
    carries no information and every body would re-seed unconditionally. The
    two stores also want opposite failure modes: for a conviction a fingerprint
    that is too strict silently retires evidence, for a body one that is too
    loose splices code that does not build. They must not share a digest.

    WHY NOT `.skeleton/_layout_registry.json` either, which is the obvious
    source and looks perfect (name -> {kind, definition} over all five kinds).
    `adapters/layout_registry_store.py` holds ONE definition per uid
    crate-wide — "whichever cfg-cluster authored it first" — and `upsert`
    overwrites it on a re-author while "clusters that already adopted the
    superseded one keep it: their trees are already spliced, and this store is
    the only place the discrepancy is visible". A gate keyed on it would report
    a moved layout for a cluster whose tree did not move.

    WHY THE WHOLE FILE TEXT IS ENOUGH. The argument is that this reads the
    SKELETON, not the filled tree: `.skeleton/<cluster>/src` is the authored
    layout with `unimplemented!()` bodies (12 files, 183 fn declarations, 144
    of them stubs, measured 2026-08-21), and fills land in
    `.skeleton-filled/<cluster>/src`. So this digest cannot move because a body
    was filled; it moves when the layout the bodies answer to moves, which is
    exactly the event that must refuse a re-seed. The 39 non-stub bodies in the
    skeleton are the frozen public/inline surface the strip keeps, and they too
    move only when t1.5 re-authors."""
    return hashlib.sha256(
        canon_body(strip_comments(file_text)).encode("utf-8")).hexdigest()[:12]


def oracle_fingerprint(files: Iterable[tuple[str, str]]) -> str:
    """The identity of the test set a row was graded under: sha256 over the
    sorted `(rel_path, whitespace-folded text)` pairs, 12 hex.

    Whitespace is folded and nothing else: a reformatted test file is the
    same oracle, a test with one assertion changed is not. Comments are kept
    on purpose — a `// !! UNCOVERED` marker or a doc comment moving is not a
    new oracle, but this digest is allowed to be stricter than the truth,
    because its failure direction is a refused re-seed (one re-derivation)
    and never a body vouched for by tests that no longer exist. The pairs
    are sorted so the caller's enumeration order cannot move it, and the
    path is part of the digest because two files with swapped contents are a
    different oracle (a gen_tests sibling is attached to ITS parent module).

    An empty file set has a fingerprint (the digest of nothing) and it is
    deliberately not `""`: the empty string means "nobody recorded the
    oracle", which the gate refuses, whereas a cluster with no oracle files
    is a recorded fact the gate can compare."""
    h = hashlib.sha256()
    for rel, text in sorted(files):
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(canon_body(text).encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:12]


# --- the layout gate ---------------------------------------------------------

def body_layout_deps(
    body_text: str, signature: str,
    items: Mapping[str, tuple[str, str]],
    *, file_of: Mapping[str, str] | None = None,
) -> tuple[LayoutDep, ...]:
    """The authored items this body depends on: every item name that appears as
    a word in the body or its signature.

    `items` is `name -> (kind, definition)` — the crate's authored item set,
    which IS what the registry is good for (a name set does not care that the
    registry is crate-wide). Word matching is deliberately generous: an item
    named in a comment costs a dependency that will simply be checked and found
    unchanged, whereas a missed dependency costs a body spliced against a
    definition that moved.

    A body with no dependencies is not a hole: it is gated by its file's
    fingerprint and by its own signature, which is the honest statement of what
    could break it."""
    haystack = f"{signature}\n{body_text}"
    words = set(_words(haystack))
    out = [LayoutDep(name=name, kind=items[name][0], definition=items[name][1],
                     file=(file_of or {}).get(name, ""))
           for name in sorted(words & set(items))]
    return tuple(out)


def _words(text: str) -> Iterable[str]:
    cur: list[str] = []
    for ch in text:
        if ch.isalnum() or ch == "_":
            cur.append(ch)
        elif cur:
            yield "".join(cur)
            cur = []
    if cur:
        yield "".join(cur)


# --- row construction --------------------------------------------------------

def rows_for_units(
    units: Iterable[tuple[str, str]],
    records: Iterable[Mapping[str, Any]],
    *, project: str, cluster: str, state: BodyState,
    items: Mapping[str, tuple[str, str]], file_of: Mapping[str, str],
    file_fp: Mapping[str, str], run_id: str, iteration: int, epoch: int,
    oracle_fp: str,
) -> list[LedgerEntry]:
    """One row per `(file, fn_name)` in `units`, built from the extractor's
    per-fn `records` as they stand NOW — the one construction both audit arms
    share, so a `flat` row and a `landed` row can never describe the same
    tree two different ways.

    `records` are the extractor's dicts (`file`, `fn_name`, `body_text`,
    `sig_text`); a unit is the ORDERED tuple of every matching record, which
    is what `landed_change.unit_bodies` keys on and what the revert seam
    reverts (`src/context.rs::source` is two impls). RAW `body_text` goes into
    the row, never the folded identity — folded, a body holding a `//` comment
    comments out its own remainder the moment it is spliced back. A unit with
    no record (the extractor could not see it) yields no row: a row with an
    empty body tuple would be a re-seed of nothing.

    The FLAT arm is why this is a function and not two copies. Before it,
    the audit built landed rows inline and dropped the escalated-unchanged
    set after logging it — the nine `src/error.rs` constructors of run
    `82157-476ddc16` iter 1 left no row, so nothing downstream could tell
    "graded, empty delta" from "never tried". With one constructor the FLAT
    row carries the CURRENT body (the one the rewrite returned unchanged),
    stamped with the same layout, oracle and process identity the landed rows
    carry, and `rebuy_hits` fires on it in the next process without any rule
    of its own."""
    by_unit: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for r in records:
        by_unit.setdefault(
            (str(r.get("file") or ""), str(r.get("fn_name") or "")), []
        ).append(r)
    rows: list[LedgerEntry] = []
    for file, fn_name in sorted(set(units)):
        recs = by_unit.get((file, fn_name), [])
        bodies = tuple(str(r.get("body_text") or "") for r in recs)
        sigs = tuple(str(r.get("sig_text") or "") for r in recs)
        if not bodies:
            continue
        rows.append(LedgerEntry(
            key=LedgerKey(project=project, cluster=cluster, file=file,
                          fn_name=fn_name),
            body_sha=body_sha(bodies), bodies=bodies, sig_sha=sig_sha_of(sigs),
            layout_fp=file_fp.get(file, ""),
            layout_deps=body_layout_deps("\n".join(bodies), "\n".join(sigs),
                                         items, file_of=file_of),
            state=state, run_id=run_id, iteration=iteration, epoch=epoch,
            oracle_fp=oracle_fp))
    return rows


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Whether a stored body may go back on the tree, and why not."""

    allowed: bool
    reason: str = ""
    moved: tuple[str, ...] = ()       # dep names whose definition moved

    def __bool__(self) -> bool:
        return self.allowed


def reseed_allowed(
    entry: LedgerEntry, *, file_fp: str, sig_sha: str, oracle_fp: str,
    definition_present: Any = None,
) -> GateDecision:
    """May `entry`'s body be spliced into the tree as it stands now?

    Four conditions, cheapest first:

    1. the gate version is current — an entry earned under an older meaning of
       "the layout matched" is not re-seeded, it is re-derived;
    2. the ORACLE that graded the row is the oracle this tree is scored by
       (`oracle_fp`, the caller's fingerprint of the current test set). A
       body is `proven` by tests; under different tests the word says nothing.
       An EMPTY stored fingerprint refuses too: the row was graded under an
       oracle nobody recorded, which is the same rule as `gate_ver !=
       GATE_VERSION` — evidence whose premise cannot be checked is re-derived,
       not trusted. This refuses every row written before the field existed
       exactly once: the next process re-lands or re-judges the body under a
       recorded oracle (the promotion pass and the audit stamp it), and from
       then on the row is comparable. That one-time cost is correct because
       the alternative was measured: 73 of 85 latest stable units would have
       re-seeded onto the cold run's tree, all graded by the gen_tests that
       run was regenerating (2026-08-26, `scripts/ledger_reseed_exposure.py`);
    3. the unit's SIGNATURE is unchanged, because a body answers to its own
       signature before it answers to anything else;
    4. the layout matches — either the file fingerprint is identical (the fast
       path, and the common one: fingerprinting all 12 files of the 08-14
       archive, the 08-16 archive and the live skeleton gave 12/12 identical,
       so a week and an augmented restart moved no layout at all), or every
       authored item this body actually references is still defined exactly as
       it was.

    `definition_present(dep) -> bool` is how condition 4's fallback reaches the
    tree; passing None makes the fallback unavailable and a moved fingerprint
    simply refuses. Refusing is never expensive: it costs a re-derivation of one
    unit and it is what keeps the 2026-08-08 `ErrorImpl` lesson — the tree that
    lost `pub(crate) backtrace` under bodies that referenced it — from becoming
    a splice."""
    if entry.gate_ver != GATE_VERSION:
        return GateDecision(False, f"gate {entry.gate_ver} != {GATE_VERSION}")
    if entry.state is BodyState.REFUTED:
        return GateDecision(False, "refuted")
    if not entry.reseedable:
        return GateDecision(False, f"state {entry.state}")
    if not entry.oracle_fp or entry.oracle_fp != oracle_fp:
        return GateDecision(False, "oracle moved")
    if entry.sig_sha and sig_sha and entry.sig_sha != sig_sha:
        return GateDecision(False, "signature moved")
    if entry.layout_fp and file_fp and entry.layout_fp == file_fp:
        return GateDecision(True)
    if definition_present is None or not entry.layout_deps:
        # No dependency list means the fallback has nothing to check, and an
        # empty check is not a pass: a body that names no authored item is
        # gated by its file alone (the docstring's own rule), so a moved file
        # refuses it. This is the direction that costs a re-derivation instead
        # of a tree that does not build.
        return GateDecision(False, "layout moved")
    moved = tuple(d.name for d in entry.layout_deps if not definition_present(d))
    if moved:
        return GateDecision(False, "layout moved", moved)
    return GateDecision(True)


# --- promotion ---------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SweepView:
    """The half of a sweep the ledger is allowed to read.

    Never a pass count and never a high-water: the ledger's admission test is
    the per-test verdict vector, so the standing rule about baselines taken
    across a deliberate destruction cannot reach it through a score."""

    passed: frozenset[str] = frozenset()
    failed: frozenset[str] = frozenset()
    intact: bool = True
    epoch: int = 0

    @property
    def ran(self) -> frozenset[str]:
        return self.passed | self.failed


def classify(
    entry: LedgerEntry, *, attributed: Iterable[str],
    before: SweepView, after: SweepView, body_now_sha: str,
) -> tuple[BodyState, tuple[tuple[str, str], ...]]:
    """What the sweep after a landing says about the body that landed.

    The three guards that keep `refuted` honest, and each of them is a defect
    that would otherwise be written into the store permanently:

    * the attributed tests must have RUN in this sweep. Blocked behind a
      stubbed sibling, excluded by cfg, or in a binary that crashed, they said
      nothing — `UNMEASURED`, which is re-seedable and is the majority class.
    * the comparison must sit inside ONE destruction epoch. The retrigger's
      re-stub bumps it; a baseline taken before a deliberate destruction is
      unsatisfiable exactly when the destruction was intentional.
    * the body being judged must still be the body on the tree. This clause
      carries the case the epoch cannot: the batch ratchet's restore is
      deliberately NOT an epoch bump ("the restore returns the tree TO the
      snapshot that set the high-water"), so a restore that reverted a sibling
      leaves the epoch unchanged while changing the tree under the comparison.

    A sweep that could not be reconciled (`intact` False) judges nothing
    either: that is the crash-noise reading the batch ratchet already refuses
    to convict on."""
    names = frozenset(attributed)
    if not names or not (names & after.ran):
        return BodyState.UNMEASURED, ()
    if body_now_sha and body_now_sha != entry.body_sha:
        return BodyState.UNMEASURED, ()
    if before.epoch != after.epoch or not after.intact or not before.intact:
        return BodyState.UNMEASURED, ()
    judged = names & after.ran & before.ran
    if not judged:
        return BodyState.UNMEASURED, ()
    lost = tuple(sorted(t for t in judged
                        if t in before.passed and t in after.failed))
    gained = tuple(sorted(t for t in judged
                          if t in before.failed and t in after.passed))
    delta = tuple([(t, "lost") for t in lost] + [(t, "gained") for t in gained])
    if lost:
        return BodyState.REFUTED, delta
    if not (names & after.passed):
        # The tests RAN and every one of them is still red. Nothing regressed,
        # so this is not a refutation — but nothing passed either, so calling
        # it `proven` would put the word back where it started: a body whose
        # attributed tests are all failing has not been shown to be right, it
        # has been shown not to have broken anything. `unmeasured` is
        # re-seedable and claims nothing, which is exactly the state that
        # describes it.
        return BodyState.UNMEASURED, delta
    return BodyState.PROVEN, delta


# --- selection ---------------------------------------------------------------

@dataclass(slots=True)
class ReseedPlan:
    """What a re-seed site intends to do, before anything touches the tree."""

    take: list[LedgerEntry] = field(default_factory=list)
    skipped: list[tuple[LedgerKey, str]] = field(default_factory=list)

    def render(self) -> str:
        proven = sum(1 for e in self.take if e.state is BodyState.PROVEN)
        unmeasured = len(self.take) - proven
        return (f"{len(self.take)} unit(s) from {len(self.take)} entr(ies) "
                f"({proven} proven, {unmeasured} unmeasured); "
                f"{len(self.skipped)} skipped (layout moved)")


def latest_by_unit(entries: Iterable[LedgerEntry]) -> dict[LedgerKey, LedgerEntry]:
    """The claim that stands for each unit: the LAST row wins, except that a
    `proven` row outranks a later row about a DIFFERENT body that is not
    `proven`.

    A later word about the SAME body always wins, whatever its state. That
    clause is what lets a `flat` row supersede a standing `proven` one: the
    nine `src/error.rs` constructors of run `82157-476ddc16` were `proven` by
    the weak-assertion Display pass, and the escalated rewrite that came back
    identical is the LATER measurement of the same body — its convicting tests
    still fail — so the standing claim is the flat one, not the pass. The
    rule is not special-cased for `flat`; it falls out of "same body, later
    word", exactly as a refutation does.

    The index is append-only precisely so that a body which was proven and then
    refuted keeps both rows — a reader must be able to tell that apart from a
    body nobody ever tried. Collapsing happens here, at read time, where the
    rule is visible."""
    best: dict[LedgerKey, LedgerEntry] = {}
    for e in entries:
        cur = best.get(e.key)
        if cur is None:
            best[e.key] = e
            continue
        if e.body_sha == cur.body_sha:
            # Same body, later word about it — including a refutation.
            best[e.key] = e
            continue
        if cur.state is BodyState.PROVEN and e.state is not BodyState.PROVEN:
            continue
        best[e.key] = e
    return best


def rebuy_hits(
    dispatched: Iterable[FnLocator],
    prior: Mapping[LedgerKey, LedgerEntry],
    *, project: str, cluster: str, run_id: str, oracle_fp: str,
) -> tuple[tuple[FnLocator, LedgerEntry], ...]:
    """Units this process is about to dispatch that a PRIOR process in the
    same cluster already graded with an empty `verdict_delta`, under the
    SAME oracle.

    The join `pattern_census.py` computed after the fact, moved to dispatch
    time. Detector only — a re-buy is sometimes the correct move (new
    context); this makes it visible. A row with no `run_id` cannot express
    "a prior process" (every live row before this field is `""`), so those
    are skipped. A first dispatch (no prior row) and a repeat under a
    non-empty delta (a verdict actually moved) do not fire. `LANDED` is
    not a grade: the fill returned and no sweep has spoken yet (a kill
    after fill-return, before the sweep, leaves this row; logging it as
    "already graded" is a lie). `FLAT` IS a grade — the escalated rewrite
    returned the body unchanged with its convicting tests run — and it is the
    row that makes the `82157-476ddc16` constructors visible to the next
    process, which before it saw no row at all.

    A row graded under another oracle (`entry.oracle_fp != oracle_fp`,
    including the empty fingerprint of a row written before the field) is
    skipped: a dispatch against NEW tests is new context by definition, so
    the prior empty delta says nothing about what this dispatch can buy.
    Measured 2026-08-26: with the oracle ignored the probe fired for
    `src/context.rs::context`, `::ext_context` and `src/kind.rs::new` on
    every fresh run id whatever the layout — rows the cold run's regenerated
    gen_tests would never have graded.
    """
    if not run_id:
        return ()
    hits: list[tuple[FnLocator, LedgerEntry]] = []
    for loc in dispatched:
        entry = prior.get(LedgerKey(project, cluster, loc.file, loc.fn_name))
        if entry is None:
            continue
        if not entry.run_id or entry.run_id == run_id:
            continue
        if entry.state is BodyState.LANDED:
            continue
        if entry.verdict_delta:
            continue
        if entry.oracle_fp != oracle_fp:
            continue
        hits.append((loc, entry))
    return tuple(hits)


def plan_reseed(
    entries: Iterable[LedgerEntry], *, wanted: Iterable[LedgerKey],
    file_fp: Mapping[str, str], sig_sha: Mapping[LedgerKey, str],
    oracle_fp: str, definition_present: Any = None,
) -> ReseedPlan:
    """Which of `wanted` this ledger can put back, and why not the rest.

    `wanted` is the stubbed set at a re-seed site — the ledger never chooses
    which units matter, it only answers about the ones it is asked about. That
    is the line between this store and a dispatch decider: it never removes a
    unit from the frontier, never marks one exhausted and never suppresses a
    prompt. `oracle_fp` is the fingerprint of the test set scoring the tree
    the bodies would go back onto; the caller computes it (this module never
    reads a tree) and the gate refuses every row graded under another."""
    stand = latest_by_unit(entries)
    plan = ReseedPlan()
    for key in wanted:
        entry = stand.get(key)
        if entry is None:
            continue
        decision = reseed_allowed(
            entry, file_fp=file_fp.get(entry.key.file, ""),
            sig_sha=sig_sha.get(key, ""), oracle_fp=oracle_fp,
            definition_present=definition_present)
        if decision:
            plan.take.append(entry)
        else:
            plan.skipped.append((key, decision.reason))
    return plan
