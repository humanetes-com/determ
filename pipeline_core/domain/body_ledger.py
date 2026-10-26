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
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pipeline_core.domain.assertion_position import (
    KeyedVerdicts,
    TestKey,
    outcomes_of,
)
from pipeline_core.domain.attribution import container_key
from pipeline_core.domain.crash_site import CrashSite
from pipeline_core.domain.impl_routing import norm_src_rel
from pipeline_core.domain.models import FnLocator, FnSite
from pipeline_core.domain.oracle_ledger import bare_test_name

GATE_VERSION = "bl1"
"""Bumped when the layout gate's meaning changes.

An entry whose `gate_ver` is not this one is never re-seeded. Bumping costs a
re-derivation of the affected units and can never cost a bad splice, which is
the right direction for a gate whose failure mode is a tree that does not
build."""

COMPARATOR_VERSION = "pos1"
"""The meaning of an earlier-panic comparison a grade was earned under.

A different field from `gate_ver`, on purpose: `reseed_allowed` refuses any
`gate_ver != GATE_VERSION` before it reads the state, so bumping the gate
would keep the rows this exists to release refused, and would refuse every
proven row written under `bl1` as well.

`pos1` is the typed assertion position (`assertion_position`): a position
exists only inside the failing test's own body, keyed by binary and test.
Rows with no comparator were graded by line order over any file, which on
run `273278-22529e44` (2026-09-29) refuted ten landings on `src/` stub sites
(`src/context.rs` 43|50 -> 23) with no test lost. A REFUTED row whose
evidence is position-only (`regressed`, no `lost`) and whose comparator is
not this one is reseedable (`LedgerEntry.refutation_superseded`); no row is
edited."""


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
    """The attributed tests ran under those same conditions and regressed:
    a test that passed now fails (delta `lost`), or — with no convicting test
    gained — a convicting test red before and after stopped at an earlier
    assertion position of its own body (delta `regressed`).
    Never re-seeded, unless the refutation was position-only and its
    comparator is superseded (`LedgerEntry.refutation_superseded`). This
    is not a conviction and the retro gate must not read it: it says one
    BODY was wrong, not that a unit is hard."""
    FLAT = "flat"
    """A graded try with an empty verdict delta on the tests that convicted
    the dispatch. Two writers, one claim:

    * the landed-change audit, when an escalated REWRITE returned a body
      IDENTICAL to the one already on the tree. A fill that spliced then
      RESTORED the snapshot (peel / compile / U2 / regression revert) is
      not this state: the tree body is the pre-fill one, not the reply.
    * `classify`, when a landing's convicting tests (attributed AND
      failing at the pre-landing sweep) ran and all still fail. Passing
      siblings must not mint `proven` — run `9872-51b20f30` judged
      `src/fmt.rs::debug` body `f2c853ff` `proven` with `verdict_delta: []`
      while the convicting test still failed, and that row then poisoned
      every FLAT consumer.

    Before the audit arm existed the identical-reply row was never written:
    run `82157-476ddc16` iter 1 (2026-08-26) dispatched twelve escalated
    units, nine of them came back unchanged, and the ledger held no trace
    of the try — so the next process's re-buy detector could not fire.

    NOT re-seedable: a body whose convicting tests still fail is the later
    measurement of a try that did not pay, and a re-seed must not vouch for
    it on the strength of an earlier, weaker pass. `reseedable` stays
    PROVEN/UNMEASURED/ADVANCED only."""
    ADVANCED = "advanced"
    """The convicting tests ran and all still fail, and at least one of them
    now stops at a LATER assertion position of its own body than at the
    sweep before the landing (`assertion_position`): every assertion before the
    new panic passes under this body. Not FLAT — the try moved the test,
    and a FLAT row is what retires a unit from dispatch
    (`flat_standing_unchanged`) and what the prompt's try history lists as
    a rewrite that bought nothing. Not PROVEN — the test is still red.

    Run `11358-e5de094e` (2026-09-15) is the instance: fill 4 kept a pair
    write whose aimed test went from failing at its first assertion (actual
    `()`) to failing at its second, and the ledger graded the landing
    `flat` beside a byte-identical verdict vector.

    Re-seedable: the attributed tests ran, none regressed, and one of them
    measurably got further — a stronger claim than UNMEASURED."""


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
    # The BARE names of the tests that convicted the try this row grades —
    # attributed AND failing at the sweep the grade was earned against.
    # A FLAT claim ("tried and failed, empty delta") is scoped to exactly
    # these tests: a later reader deciding whether the try is worth
    # re-buying must compare the tests convicting the unit NOW against
    # this premise, and a NEW convicting test outside it is moved
    # evidence. Empty on every row written before the field existed —
    # `flat_standing_unchanged` reads that as premise-unrecorded and
    # refuses to exclude, so a legacy flat is re-graded once (minting a
    # premised row) instead of being locked out of dispatch forever.
    convicting: tuple[str, ...] = ()
    # The identity of the EVIDENCE the dispatch that earned this grade
    # bought — the file the prompt quotes, the retrospective rows it may
    # inject, the crash excerpt naming the unit
    # (`domain/evidence_premise.evidence_fingerprints`). The three clauses
    # beside it all describe the world AROUND the unit, and on a stalled
    # tree none of them can move, so a FLAT grade scoped to those alone
    # retires a unit that lands a compiling, non-paying body permanently
    # (run `837207-ce5b1647`, 2026-09-15: three such units, `dispatching
    # 0 of 0` against `frontier=3` for the run's last three hours). This
    # field is what the unit was TOLD, so an unchanged question can be
    # refused and a changed one asked. Empty on every row written before
    # the field existed — `flat_standing_unchanged` reads that as
    # premise-unrecorded and refuses, exactly as it does for `convicting`.
    evidence_fp: str = ""
    # The RAW bodies this landing replaced, in occurrence order, read off the
    # extractor's records before the fill. What the sweep puts back when it
    # grades the landing REFUTED on an earlier panic (`classify`, delta
    # `(test, "regressed")`): such a landing was kept by nothing but a name
    # set, and no other mechanism reverts it — the batch ratchet sees an
    # unchanged pass count. Empty for a unit that did not exist before the
    # landing and on every row written before the field.
    prior_bodies: tuple[str, ...] = ()
    # The `COMPARATOR_VERSION` an earlier-panic comparison was made under
    # when this row was graded. "" on every row graded before the typed
    # position existed.
    comparator: str = ""
    # The BARE names of the tests the keep that landed this body turned
    # green in its own comparison (the route row's `newly_green`). None =
    # unrecorded (a row written before the field, or a keep with no reading).
    # The sweep's grade credits a convicting test that turned green only when
    # it is in this set: a sweep on 2026-10-02 judged `4 landed body(ies) —
    # 4 proven` for one keep that moved the test, because every landed unit
    # attributed to a test that turned green between two sweeps was credited.
    kept_green: tuple[str, ...] | None = None
    # The crash a settlement refuted this body on, as the localizer read it
    # (`Settlement.render`). Its reader is the stub's next prompt
    # (`latest_crash_refutation`): before the field the refutation's only
    # reader was the reseed gate, and run `757853-e28f1a7d` (2026-10-16)
    # re-wrote the refuted double drop three times on a prompt that said
    # only that the fn was a stub. "" on every other row.
    crash_evidence: str = ""

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
            "convicting": list(self.convicting),
            "evidence_fp": self.evidence_fp,
            **({"prior_bodies": list(self.prior_bodies)}
               if self.prior_bodies else {}),
            **({"comparator": self.comparator} if self.comparator else {}),
            **({"kept_green": list(self.kept_green)}
               if self.kept_green is not None else {}),
            **({"crash_evidence": self.crash_evidence}
               if self.crash_evidence else {}),
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
                # Missing on pre-field rows: () reads as premise-unrecorded.
                convicting=tuple(
                    str(t) for t in (raw.get("convicting") or ())),
                # Missing on pre-field rows: "" reads as premise-unrecorded
                # and `flat_standing_unchanged` refuses to exclude on it.
                evidence_fp=str(raw.get("evidence_fp") or ""),
                prior_bodies=tuple(
                    str(b) for b in (raw.get("prior_bodies") or ())),
                comparator=str(raw.get("comparator") or ""),
                kept_green=(None if raw.get("kept_green") is None else tuple(
                    str(t) for t in raw.get("kept_green") or ())),
                crash_evidence=str(raw.get("crash_evidence") or ""),
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
        return self.state in (BodyState.PROVEN, BodyState.UNMEASURED,
                              BodyState.ADVANCED)

    @property
    def refutation_superseded(self) -> bool:
        """A REFUTED row whose only evidence is an earlier position (a
        `regressed` delta and no `lost`), graded under a comparator that is
        no longer the current one. The grade was a claim of the old
        comparison, not of the tests' verdicts; it no longer refuses the
        body. A `gained` beside it does not keep it refuted — under the
        current rule a gain outranks the position. A row that lost a test
        stays refuted under any comparator.

        Run `273278-22529e44`: all ten refuted rows are position-only, eight
        of them with `gained` deltas; regraded on the recorded capture pair
        under `pos1` they are eight `proven` and two `flat`."""
        kinds = {k for _t, k in self.verdict_delta}
        return (self.state is BodyState.REFUTED
                and self.comparator != COMPARATOR_VERSION
                and "regressed" in kinds and "lost" not in kinds)


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
    convicting_of: Mapping[tuple[str, str], Iterable[str]] | None = None,
    evidence_of: Mapping[tuple[str, str], str] | None = None,
    prior_of: Mapping[tuple[str, str], Sequence[str]] | None = None,
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
            oracle_fp=oracle_fp,
            # The premise a FLAT claim is scoped to (see `LedgerEntry.
            # convicting`); the caller supplies the tests whose failure
            # bought THIS dispatch, per unit.
            convicting=tuple(sorted(
                (convicting_of or {}).get((file, fn_name), ()))),
            # ...and what the unit was TOLD when it was bought, plus what
            # the attempt wrote (see `LedgerEntry.evidence_fp`). The caller
            # computes it: over the tree as the landing left it for a body
            # that landed, so the landing cannot move its own premise; at
            # dispatch for a row whose body did not move.
            evidence_fp=str((evidence_of or {}).get((file, fn_name), "")),
            # The bodies a landing replaced (see `LedgerEntry.prior_bodies`);
            # the caller reads them off the records taken before the fill.
            prior_bodies=tuple((prior_of or {}).get((file, fn_name), ()))))
    return rows


def raw_bodies_of(
    records: Iterable[Mapping[str, Any]], units: Iterable[tuple[str, str]],
) -> dict[tuple[str, str], tuple[str, ...]]:
    """`(file, fn_name) -> RAW body texts` in record order for `units`, from
    the extractor's per-fn records — the shape `rows_for_units` stores and
    `fn_reseed.splice_bodies` writes back. A unit with no record is absent."""
    wanted = set(units)
    out: dict[tuple[str, str], tuple[str, ...]] = {}
    for r in records:
        key = (str(r.get("file") or ""), str(r.get("fn_name") or ""))
        if key in wanted:
            out[key] = (*out.get(key, ()), str(r.get("body_text") or ""))
    return out


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
    if entry.state is BodyState.REFUTED and not entry.refutation_superseded:
        return GateDecision(False, "refuted")
    if not (entry.reseedable or entry.refutation_superseded):
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


def candidate_seed_allowed(
    entry: LedgerEntry | None, *, row_oracle_fp: str, oracle_fp: str,
    file_fp: str, sig_sha: str, located: bool,
    definition_present: Any = None,
) -> GateDecision:
    """May a joint candidate's stored body be put on this tree as the seed
    of a joint session?

    The comparisons of :func:`reseed_allowed` that are about the TREE — the
    oracle the body was graded under, its signature, the layout — and not
    the one about the BODY's state. A joint candidate is by definition a
    body a gate reverted, so its ledger entry, when it has one, is REFUTED
    or FLAT, and the reseed gate refuses exactly every such body: on run
    `4012759-5c5177c6` that refused the only recorded answer that turned
    `test_debug` green. The seed is judged by the session that splices it,
    so the state is not a premise here.

    `entry` is the ledger row whose `body_sha` is the candidate's body, graded
    under the current oracle, when one exists: its `sig_sha` and `layout_fp`
    supply the comparisons, and a moved fingerprint falls back to
    `definition_present` over the entry's dependencies as the reseed gate's
    does. `row_oracle_fp` is the oracle the candidate's route row was
    measured under, never the caller's current one passed twice. With
    none, the body is seedable while its declaration still locates a body
    span on this tree (`located`) and the route row's oracle matches; a
    splice refusal at seed time is then the layout failure, logged by the
    session with the splice reason."""
    if not row_oracle_fp or row_oracle_fp != oracle_fp:
        return GateDecision(False, "oracle moved")
    if not located:
        return GateDecision(False, "no body span on this tree")
    if entry is None or not entry.layout_fp:
        # No layout evidence for this body: the located declaration is the
        # whole check, as for a body with no ledger row.
        if entry is not None and entry.sig_sha and sig_sha and entry.sig_sha != sig_sha:
            return GateDecision(False, "signature moved")
        return GateDecision(True)
    if entry.sig_sha and sig_sha and entry.sig_sha != sig_sha:
        return GateDecision(False, "signature moved")
    # The layout comparison is the reseed gate's: the fast path needs both
    # fingerprints and equality; a fingerprint the caller could not compute
    # (a key miss) is not a match, it falls to the dependency check.
    if file_fp and entry.layout_fp == file_fp:
        return GateDecision(True)
    if definition_present is None or not entry.layout_deps:
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
    #: (binary, test) -> assertion position of a failing test
    #: (`assertion_position.assertion_positions`), computed per binary
    #: before any collapse onto the bare name. Absent for a failure with no
    #: position; never defaulted, since a sentinel line would read as
    #: movement.
    positions: Mapping[TestKey, int] = field(default_factory=dict)
    #: bare test name -> the still-stubbed functions its first failure is
    #: inside (`test_outcome.stub_hold_of`). A test held at a function other
    #: than the graded one stopped before the graded body could matter to
    #: it, so it does not judge that body (`classify`). Empty = no reading,
    #: and every test that ran judges as before.
    held: Mapping[str, frozenset[FnLocator]] = field(default_factory=dict)
    #: Tests whose own isolated process died in this sweep
    #: (`TestStateReading.aborted_names`), and the ones among them that
    #: printed no panic (`aborted_silent_names`): what `outcomes_of` needs to
    #: tell a body that made a test's process die from one that left it red.
    aborted: frozenset[str] = frozenset()
    aborted_silent: frozenset[str] = frozenset()
    #: The same sweep per (binary, test) (`TestStateReading.keyed`), what
    #: every outcome the ledger grades is read from. The name sets above
    #: union the binaries: on run `14495-a26ad4ea` (2026-10-15)
    #: `tests/test_repr.rs::test_drop` passed while
    #: `tests/test_downcast.rs::test_drop` failed, so `test_drop` sat in
    #: `passed` and in `failed` at once, and a name-set grade reads a
    #: landing that left that test red as "lost" or as "proven". None only
    #: on a view given by names (a one-binary fixture, never the loop's:
    #: `of_sweep` requires it): :attr:`keyed` then reads every name as a
    #: test of one binary, the one its positions name when they name one.
    verdicts: KeyedVerdicts | None = None
    #: The crash sites the sweep localized for its capture, when a test's
    #: own process died without a panic: what `classify` hands
    #: `outcomes_of`, so an already-red convicting test that now dies in a
    #: crate body is judged by the abort partition (`abort_verdict`), not
    #: refuted on sight. Empty: every red abort reads unlocated, as before.
    crash_sites: tuple[CrashSite, ...] = ()

    @property
    def ran(self) -> frozenset[str]:
        return self.passed | self.failed

    @property
    def keyed(self) -> KeyedVerdicts:
        if self.verdicts is not None:
            return self.verdicts
        labels = {k.binary for k in self.positions}
        return KeyedVerdicts.of_names(
            passed=self.passed, failed=self.failed, aborted=self.aborted,
            aborted_silent=self.aborted_silent, positions=self.positions,
            binary=labels.pop() if len(labels) == 1 else "")

    def observers_of(self, unit: FnLocator, tests: Iterable[str]) -> frozenset[str]:
        """`tests` minus those held at a still-stubbed function other than
        `unit`."""
        return frozenset(
            t for t in tests
            if not (h := self.held.get(t)) or unit in h)

    @classmethod
    def of_sweep(
        cls, *, passed: Iterable[str], failed: Iterable[str],
        vanished: Iterable[str], intact: bool, epoch: int,
        positions: Mapping[TestKey, int],
        aborted: Iterable[str], aborted_silent: Iterable[str],
        verdicts: KeyedVerdicts,
        held: Mapping[str, frozenset[FnLocator]] | None = None,
        crash_sites: Sequence[CrashSite] = (),
    ) -> SweepView:
        """The ledger's view of a sweep whose failed set the score has already
        padded with crash-vanished tests.

        The sweep counts a test that reported no verdict because its binary
        aborted as FAILING — right for a score, which must not rise when a
        binary dies. It is wrong for the ledger: `ran` is the guard that makes
        a test in a crashed binary judge `unmeasured`, and a vanished name in
        `failed` walks straight through it and mints `refuted` on a verdict
        that was never produced. Run `119837-09e3ac57`, cluster `nightly`,
        2026-09-15, iter 2: 102 tests vanished when ten binaries aborted on
        panics inside still-stubbed fns; the batch ratchet declined to charge
        exactly that drop, and the same sweep graded 8 of 12 landed bodies
        `refuted` on `lost` tests that had not run — `ptr.rs::new`,
        `ptr.rs::boxed` and `ptr.rs::read` among them. A refuted row is never
        re-seeded. So vanished names are removed from `failed` here; they are
        neither passed nor failed to the ledger. `verdicts` is the capture's
        own per-binary reading; a vanished test reported no verdict, so it
        holds no key for it."""
        gone = frozenset(vanished)
        return cls(passed=frozenset(passed),
                   failed=frozenset(t for t in failed if t not in gone),
                   intact=intact, epoch=epoch, positions=positions,
                   held=dict(held or {}),
                   aborted=frozenset(aborted),
                   aborted_silent=frozenset(aborted_silent),
                   verdicts=verdicts, crash_sites=tuple(crash_sites))


def judgment_attributed(
    attributed: Iterable[str], entry: LedgerEntry,
) -> frozenset[str]:
    """The tests a landed row is judged against: the reverse map's
    attribution joined with the row's own dispatch premise.

    The premise (`LedgerEntry.convicting`) is the tests whose failure bought
    the dispatch — attributed-or-escalated and failing at the pre-fill
    sweep — stamped on the LANDED row. The reverse map knows attribution
    proper only. A unit on the frontier through a futility escalation or
    the owner-map admission is absent from it, so a judgment read off the
    reverse map alone graded every such landing UNMEASURED and re-stamped
    its premise EMPTY, while the tests that convicted the dispatch had run
    and stayed red. Run `2965-453dd7a4` (2026-09-05, cluster stable), iter
    2: `src/error.rs::construct_from_display`, `::object_ref`,
    `src/lib.rs::must_use` and `src/fmt.rs::display` landed at iter 1 with
    7-19 convicting tests each, the iter-2 sweep ran all 28 binaries with a
    byte-identical verdict vector, and the four were judged `unmeasured`
    with `convicting=()`. Three of them were re-admitted by the owner map
    on every later iteration; a FLAT row would have excluded them
    (`flat_standing_unchanged`), and the escalated re-buy the dispatch
    ranking then spent its leftover cap slots on is the class that erasure
    kept alive.

    Joining the premise can only add outcomes on tests that were failing
    before the landing (the premise is a subset of the pre-fill failing set
    by construction), so it can mint FLAT or PROVEN where the reverse map
    minted UNMEASURED — never REFUTED. A row without a premise (INFERRED
    with nothing convicting it, or written before the field) judges exactly
    as before.
    """
    return frozenset(attributed) | frozenset(entry.convicting)


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
    to convict on.

    Proof is scoped to the tests that convicted the dispatch (attributed AND
    failing at `before`). Passing siblings cannot mint `proven`. Convicting
    tests that ran and all still fail are `flat`: a graded try with an empty
    delta, not `unmeasured` (the tests ran) and not `proven` — unless one of
    them now stops at a later assertion position of its own body, which is
    `advanced` (delta entries `(test, "advanced")`). One that stops at an
    EARLIER position is `refuted` (delta entries `(test, "regressed")`) —
    but only when no convicting test passes: a verdict outranks a position
    (`TestOutcomes.refuses`, the rule every keep reads). Run
    `273278-22529e44` (2026-09-29) refuted eight landings that each turned
    convicting tests green, on line order over `src/` stub sites."""
    # A convicting test whose first failure, after the landing, is inside a
    # DIFFERENT still-stubbed function never reached this body: it stopped
    # first. It judges nothing about the unit. Run `14141-9a904822`
    # (2026-10-08): `src/ensure.rs::render` was graded FLAT at iter 3 while
    # 12 of its 15 convicting tests died inside the stubbed Display
    # `src/error.rs::fmt` that prints the message it builds. Across ten cold
    # runs 62 FLAT rows were graded with every convicting test held that way.
    # Only a test that was ALREADY failing is dropped that way: a test green
    # before the landing and dying in another stub after it reached that stub
    # through the new body, so it is the landing's loss and refutes it.
    names = frozenset(attributed)
    held_elsewhere = names - after.observers_of(
        FnLocator(entry.key.file, entry.key.fn_name), names)
    names = names - (held_elsewhere - before.passed)
    if not names or not (names & after.ran):
        return BodyState.UNMEASURED, ()
    if body_now_sha and body_now_sha != entry.body_sha:
        return BodyState.UNMEASURED, ()
    if before.epoch != after.epoch or not after.intact or not before.intact:
        return BodyState.UNMEASURED, ()
    judged = names & after.ran & before.ran
    if not judged:
        return BodyState.UNMEASURED, ()
    # Per (binary, test), never by name set: the name sets union the
    # binaries (`SweepView.verdicts`).
    judged_out = outcomes_of(judged, before=before.keyed, after=after.keyed)
    lost = tuple(sorted(judged_out.lost))
    gained = tuple(sorted(judged_out.gained))
    delta = tuple([(t, "lost") for t in lost] + [(t, "gained") for t in gained])
    if lost:
        return BodyState.REFUTED, delta
    # Scope proof to the tests that convicted the dispatch (attributed AND
    # failing at the pre-landing sweep). A passing sibling is not proof:
    # run `9872-51b20f30` minted empty-delta `proven` for `src/fmt.rs::debug`
    # from tests that were already green, while the convicting test stayed
    # red. When those convicting tests ran and all still fail, the landing
    # is a graded try with an empty delta — `flat`, not `proven` and not
    # `unmeasured` (`unmeasured` claims the tests did not run).
    convicting = names & before.failed
    if convicting:
        if not (convicting & after.ran):
            return BodyState.UNMEASURED, delta
        outcomes = outcomes_of(convicting, before=before.keyed,
                               after=after.keyed,
                               crash_sites=after.crash_sites)
        # The abort partition (`abort_verdict`), with the gain the landing's
        # own keep was judged on beside the convicting tests that turned
        # green here: a convicting test that was red and now dies in a crate
        # body is not a refutation of a landing that paid. The session that
        # kept net +20 beside such an abort (run `42057-641e589c`, fill 5)
        # would otherwise be graded REFUTED here and never re-seeded.
        _gain = bool(outcomes.gained) or bool(entry.kept_green)
        if outcomes.abort.refuses(strict_gain=_gain, lost=bool(lost)):
            return BodyState.REFUTED, delta + tuple(
                (t, "aborted") for t in sorted(outcomes.aborted))
        delta = delta + tuple(
            (t, "aborted") for t in sorted(outcomes.aborted))
        # The decided rule: an earlier position refuses only when no
        # convicting test gained (`lost` already returned above). Run
        # `24951-a72c4642` (2026-09-29) is the all-red case this grade
        # exists for, graded `flat` before it.
        if outcomes.refuses_with(strict_gain=_gain):
            return BodyState.REFUTED, delta + tuple(
                (t, "regressed") for t in outcomes.earlier_tests)
        # A convicting test still red that stops later is recorded whatever
        # else the landing did: the next sweep's attribution reads it
        # (`premise_advance`), and a landing that turned some tests green
        # and moved the rest past itself used to be graded `proven` with the
        # moves dropped (run `1680589-7a6824f8`, fill 11, `src/fmt.rs`).
        advanced = tuple((t, "advanced") for t in outcomes.later_tests)
        if not outcomes.gained:
            if outcomes.later:
                return BodyState.ADVANCED, delta + advanced
            return BodyState.FLAT, delta
        if (entry.kept_green is not None
                and not (outcomes.gained & frozenset(entry.kept_green))):
            # The convicting tests that turned green were not turned by THIS
            # landing's keep (its own comparison recorded none of them): a
            # sibling keep paid them. A graded try that bought nothing.
            return BodyState.FLAT, delta
        return BodyState.PROVEN, delta + advanced
    if not (names & after.passed):
        return BodyState.UNMEASURED, delta
    # No attributed test was red when the body landed, and they pass now:
    # the tests ran, nothing was at stake, and the delta is empty. That is a
    # graded try that bought nothing — `flat` on an empty convicting set —
    # not `proven`: the schedule ranks a proven landing as paying, so a
    # rewrite of a body whose tests were already green was bought again at
    # the never-served weight (run `3040-51eab48c`, 2026-10-02, the keeps
    # after the fill's last red test turned green). The flat claim stands
    # only while nothing convicts the unit, so a test that later fails in it
    # reopens it.
    return BodyState.FLAT, delta


@dataclass(frozen=True, slots=True)
class ReportKeep:
    """One route report of a wave: the units it kept and the tests its row's
    own comparison turned green (bare names; None when that row recorded no
    comparison)."""

    kept: frozenset[FnLocator]
    newly_green: frozenset[str] | None


class WaveKept(frozenset[FnLocator]):
    """The units a fill wave's routes kept — a frozenset of them, so every
    reader of the plain set is unchanged — carrying each report's keep
    (`ReportKeep`). Which kept unit a green test is credited to is decided
    by :func:`credited_green` with the attribution the driver holds; the
    unit ledger stamps the result on the landed row
    (`LedgerEntry.kept_green`)."""

    reports: tuple[ReportKeep, ...]

    def __new__(cls, units: Iterable[FnLocator],
                reports: Iterable[ReportKeep] = ()) -> WaveKept:
        obj = super().__new__(cls, units)
        obj.reports = tuple(reports)
        return obj


def tests_creditable_to_locator(
    tests: Collection[str],
    convicting: Mapping[FnSite, Collection[str]],
    file: str,
    fn_name: str,
) -> frozenset[str]:
    """The tests of `tests` this locator's ledger row may be credited with.

    The row is one unit for every body of `(file, fn_name)` — the ordered
    tuple `rows_for_units` stores. When `convicting` names two or more
    containers of that name, a test that convicts only some of them was
    not paid by the unit as a whole, so it is left out of the row's
    credit. A test the map does not split, and a name with fewer than
    two containers, stay on the locator."""
    bare = {bare_test_name(t) for t in tests}
    want = norm_src_rel(file)
    holders: dict[str, set[str]] = {}
    for site, site_tests in convicting.items():
        # A locator key has no container. It does not name a body, so it
        # does not split the row — the same reading as an empty container.
        container = getattr(site, "container", "")
        if (getattr(site, "fn_name", None) != fn_name
                or norm_src_rel(getattr(site, "file", "")) != want
                or not container):
            continue
        holders.setdefault(container_key(container), set()).update(
            bare_test_name(t) for t in site_tests)
    if len(holders) < 2:
        return frozenset(bare)
    confined: set[str] = set()
    for test in {t for ts in holders.values() for t in ts}:
        who = [c for c, ts in holders.items() if test in ts]
        if len(who) < len(holders):
            confined.add(test)
    return frozenset(t for t in bare if t not in confined)


def credited_green(
    reports: Iterable[ReportKeep],
    attributed: Mapping[FnLocator, Collection[str]],
) -> dict[FnLocator, frozenset[str] | None]:
    """Per kept unit, the tests its own keep can be shown to have paid.

    A file or pair row runs one cargo invocation for the whole reply, so it
    cannot see which of two bodies attributed to the same test turned it
    green. A test in a row's `newly_green` is therefore credited to a kept
    unit only when that unit is the ONLY unit the same report kept that is
    attributed to the test; a test two kept units share is credited to
    neither. A report whose row recorded no comparison does not erase a set
    another report recorded for the same unit: a unit's green is None only
    when every report that kept it is unrecorded."""
    attr = {u: frozenset(bare_test_name(t) for t in ts)
            for u, ts in attributed.items()}
    out: dict[FnLocator, frozenset[str] | None] = {}
    for rep in reports:
        if rep.newly_green is None:
            for u in rep.kept:
                out.setdefault(u, None)
            continue
        paid: dict[FnLocator, set[str]] = {u: set() for u in rep.kept}
        for t in {bare_test_name(n) for n in rep.newly_green}:
            owners = [u for u in rep.kept if t in attr.get(u, frozenset())]
            if len(owners) == 1:
                paid[owners[0]].add(t)
        for u, got in paid.items():
            out[u] = (out.get(u) or frozenset()) | got
    return out


def kept_green_of(kept: frozenset[FnLocator], unit: FnLocator,
                  attributed: Mapping[FnLocator, Collection[str]],
                  ) -> tuple[str, ...] | None:
    """The tests the wave's keep can credit to `unit`
    (:func:`credited_green`), or None when the caller handed a plain set or
    no report that kept `unit` recorded a comparison."""
    reports = getattr(kept, "reports", None)
    if not reports:
        return None
    got = credited_green(reports, attributed).get(unit)
    return None if got is None else tuple(sorted(got))


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
            # Same body, later word about it — including a refutation. Except
            # that a word which says nothing cannot take a refutation back:
            # a crash settlement refutes the body it puts back to a stub, and
            # the next sweep, finding that body no longer on the tree, writes
            # `unmeasured` about it (`classify`); under "later word wins" the
            # re-seed would then splice the body the crash convicted back
            # onto the stub (run `1057870-b427a3a2`'s dangling constructor).
            if (cur.state is BodyState.REFUTED
                    and not cur.refutation_superseded
                    and e.state in (BodyState.UNMEASURED, BodyState.LANDED)):
                continue
            best[e.key] = e
            continue
        if cur.state is BodyState.PROVEN and e.state is not BodyState.PROVEN:
            continue
        best[e.key] = e
    return best


def standing_on_tree(
    entries: Iterable[LedgerEntry],
    body_now: Mapping[LedgerKey, str],
) -> dict[LedgerKey, LedgerEntry]:
    """Latest row about the body currently on the tree.

    `latest_by_unit` answers the re-seed question: a `proven` row outranks
    a later row about a DIFFERENT body, so the store can still put the
    proven bytes back after a destruction. Dispatch asks a different
    question — what is the standing claim about the bytes that are on
    disk. Computed on run `9872-51b20f30` against `.skeleton-filled/stable`:
    `latest_by_unit` FLAT was 10 of the 28-unit cohort, while the latest
    row whose `body_sha` matched the tree was FLAT for 18 (the eight
    extras were masked by an off-tree `proven` — `src/context.rs::
    with_context` tree `997c5614` flat, standing `proven` on `cb41090c`).
    A proven row about an off-tree body is re-seed inventory, not a live
    grade. Units with no `body_now` entry, or no row matching it, are
    omitted (no standing claim).
    """
    want = dict(body_now)
    best: dict[LedgerKey, LedgerEntry] = {}
    for e in entries:
        sha = want.get(e.key)
        if sha is None or e.body_sha != sha:
            continue
        best[e.key] = e
    return best


def flat_standing_unchanged(
    standing: Mapping[LedgerKey, LedgerEntry],
    *,
    oracle_fp_now: str,
    convicting_now: Mapping[LedgerKey, frozenset[str]],
    evidence_now: Mapping[LedgerKey, str],
) -> set[LedgerKey]:
    """Units whose standing claim is FLAT and whose evidence has not moved.

    A FLAT row says "this body, tried under these convicting tests and this
    oracle, moved nothing" — a claim about a MOMENT. Excluding the unit from
    dispatch on the claim alone makes the exclusion permanent: dispatch is
    the only producer of a new body, and a new body is the only thing that
    writes a new standing row, so a gate keyed on the bare grade is a lock,
    not a spend guard (the pass-81 readmission deadlock shape). The claim
    licenses exclusion only while its premise still holds:

    * the tree body is the graded one — `standing` (from `standing_on_tree`)
      already guarantees it;
    * the oracle is the one that graded the row — equality on `oracle_fp`
      (both-empty matches: the same unwired oracle, the harness case; a
      MIXED pair is a moved or unrecorded oracle and refuses);
    * no NEW test convicts the unit — the tests convicting it now are a
      subset of the row's `convicting` premise. A shrunken set still
      excludes (the remaining convictors were already in the premise); an
      empty current set excludes trivially (nothing left to buy);
    * the unit would be asked the SAME question — equality on
      `evidence_fp` (`domain/evidence_premise`), the identity of the file
      the prompt quotes, the retrospective rows it may inject and the crash
      excerpt naming it.

    THE FOURTH CLAUSE IS WHY THE FIRST THREE ARE NOT ENOUGH. Every one of
    them is satisfiable only by the world OUTSIDE the unit, and on a stalled
    tree that world does not move: clause 1 holds *because* the flat body was
    KEPT (it compiles and regresses nothing, which is exactly why it was
    kept), clause 2 holds because the oracle has one identity per tree, and
    clause 3 holds because the convicting set is the same failing tests every
    sweep. So the premise stays perpetually FRESH and the unit is retired by
    its own success — run `837207-ce5b1647` (2026-09-15) excluded
    `src/fmt.rs::debug` from iteration 2, `src/error.rs::fmt` from iteration
    3, and read `dispatching 0 of 0 (0/0 this pass)` against `frontier=3
    fn(s)` for the last three hours of a 6h32m run. The fourth clause is
    keyed on the one thing that DOES move: what the next prompt would carry.

    A row with an EMPTY premise — no `convicting`, or no `evidence_fp`,
    both of which mean the field did not exist when the row was written —
    refuses: the trade is one re-grade of each legacy flat (run
    `9872-51b20f30`'s 17 on-tree flats re-enter dispatch once, come back
    graded with a premised row, and are excluded honestly from then on)
    against a lock class that would otherwise survive on every legacy row
    forever. An absent or empty CURRENT fingerprint refuses for the same
    reason from the other side: a caller that could read none of the
    evidence channels has not observed that the question is unchanged, and
    a gate keyed on nothing observed is the lock again.
    """
    out: set[LedgerKey] = set()
    for k, e in standing.items():
        if e.state is not BodyState.FLAT:
            continue
        if e.oracle_fp != oracle_fp_now:
            continue
        if not e.convicting:
            continue
        if not e.evidence_fp or e.evidence_fp != evidence_now.get(k, ""):
            continue
        if convicting_now.get(k, frozenset()) <= frozenset(e.convicting):
            out.add(k)
    return out


def rebuy_hits(
    ranked: Iterable[FnLocator],
    prior: Mapping[LedgerKey, LedgerEntry],
    *, project: str, cluster: str, run_id: str, oracle_fp: str,
) -> tuple[tuple[FnLocator, LedgerEntry], ...]:
    """Units of the RANKED FRONTIER that a PRIOR process in the same
    cluster already graded with an empty `verdict_delta`, under the SAME
    oracle.

    The join `pattern_census.py` computed after the fact, moved to ranking
    time. The caller hands the whole ranked frontier BEFORE the dispatch
    cut — run `9933-efe68f37` computed this on the cut batch, so the
    reading could only name units already inside the cap and said nothing
    about what the ranking was about to re-buy. Detector only — a re-buy
    is sometimes the correct move (new context); this makes it visible. A
    row with no `run_id` cannot express
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
    for loc in ranked:
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


def rebuy_by_grade(
    hits: Sequence[tuple[FnLocator, LedgerEntry]],
) -> dict[str, tuple[FnLocator, ...]]:
    """Split re-buy hits by the standing row's actual grade.

    `unmeasured` means the tests did not run. A FLAT / not-FLAT cut puts any
    other empty-delta grade in that bucket — the dispatch-time detector's
    own test uses a PROVEN prior row, and printing that as "unmeasured —
    tests did not run" is the wrong sentence. Computed on run
    `2965-453dd7a4` (2026-09-05) iter 1's 16 hits against the prior
    ledger: 4 FLAT, 12 UNMEASURED, 0 other, so the live line happened to
    be right; the cut still has to name the grade.
    """
    by: dict[str, list[FnLocator]] = {}
    for loc, entry in hits:
        by.setdefault(str(entry.state), []).append(loc)
    return {state: tuple(locs) for state, locs in by.items()}


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


#: The verdict-delta kind a crash settlement stamps on the bodies it refutes.
CRASH_ORIGIN = "crash_origin"


def latest_crash_refutation(
    entries: Iterable[LedgerEntry], file: str, fn_name: str, *,
    oracle_fp: str,
) -> LedgerEntry | None:
    """The last row a crash settlement refuted this unit's body with, under
    this oracle, when it carries the crash it was refuted on. A row graded
    under another oracle describes other tests and is not told."""
    hit: LedgerEntry | None = None
    for e in entries:
        if (e.key.file == file and e.key.fn_name == fn_name
                and e.state is BodyState.REFUTED and e.crash_evidence
                and e.oracle_fp == oracle_fp
                and any(k == CRASH_ORIGIN for _t, k in e.verdict_delta)):
            hit = e
    return hit
