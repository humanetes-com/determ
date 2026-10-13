"""Which functions a red test is owed next, and when nothing new can be asked.

WHY. A red test's executed slice is 16-23 functions wide, and the functions
that decide its verdict are a few of them. Every predicate stated over the
whole slice is satisfied by writing the wrong function: on run
`14141-9a904822` (2026-10-08, cluster `stable`) every red test had SOME
owner written by an opened run in every fill, while the one wrong body behind
16 of the 21 leftovers (`src/ensure.rs::render`) went unwritten from fill 3 to
fill 8. And a rule "due until the tests turn green" keeps the shared, already
correct functions (`construct`, `drop`, `vtable`) due for as long as any test
that runs through them is red.

WHAT. Two readings, both over data the bake already stores:

- **Tiers.** A function's *reach* is the set of book features whose examples
  exercise it (`BehaviourSpec.exercises`, joined through the features that
  list the example). On one red test's slice, the narrowest reach is the
  first tier, functions no harvested example exercises come right after it
  (nothing says how specific they are), then the wider reaches in order. A
  tier is owed only when every function of the tiers before it has been
  asked under its current evidence.
- **The ask fingerprint.** What the test says about itself: the sweep's
  failure text for it, with libtest's per-run noise (thread ids, addresses)
  masked. A function is asked once per (test, fingerprint); the seen set
  keeps every one, so an oscillating reading does not re-owe a state
  already asked. Neither the function's own body nor the other bodies in
  its file are part of it: a landing that changes no red test's evidence
  re-owes nothing, which is what lets :func:`due_reading` say "nothing new
  can be asked". With the own body in it, every new wrong body re-owed its
  function and a model that kept changing its answer kept the cluster open
  forever; one ask still buys the route's whole attempt budget.

A test held at a still-stubbed function owes that stub first, and nothing
on its slice: no function past the stub can be observed by it. When the stub
has been asked under the test's evidence and the test is still held there,
the test is spent, not open forever.

**What an ask is.** An owed function is asked when a body for it LANDED in
the fill that owed it: the answer reached the tree and the next sweep reads
it. A dispatch that landed nothing (no route planned it, the reply was
refused, did not compile, or was lost) is a MISS. A function is spent for a
(test, fingerprint) once it was asked, or after `misses` misses: one bad
sample does not spend it, and a function no route can write does not keep
the cluster open forever.

**A fingerprint moves only with the tree.** The ledger remembers, per test,
the tree each fingerprint was read on. A reading on the same tree that
reads differently is per-run noise the mask did not catch (a hash order, a
temp path), and keeps the earlier fingerprint: the same tree cannot have
said anything new.

**With no reach** (a cluster whose suite loads empty, or functions no
example runs), the reach-0 group is split by *spread*: how many red tests'
slices hold the function. A function on one red test's slice is narrower
evidence than one on every slice. Without that split an empty suite owed
every function on every slice at once, the wide set the tiers exist to
avoid (35 of 35 on the `14141-9a904822` fill-8 map).

`culprits` (the functions only one feature's examples exercise) is the
rule-aiming set: a feature's kept rules reach those functions' prompts
(`behaviour_book.rules_for_units`). It is never the due set — a feature whose
every function is shared still has its red tests' tiers asked.

Nothing here names a crate, file, test, trait or symbol.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pipeline_core.domain.models import FnLocator

__all__ = [
    "DEFAULT_MISSES",
    "LADDER",
    "JointCandidate",
    "JointVerdict",
    "joint_rollback",
    "joint_accept",
    "joint_candidates",
    "joint_note",
    "miss_reasons",
    "UNREAD",
    "AskLedger",
    "DueReading",
    "TestTiers",
    "ask_fingerprint",
    "culprits",
    "due_reading",
    "evidence_text",
    "feature_reach",
    "merge_slices",
    "render_failed",
    "slices_by_key",
    "tiers_event",
    "tiers_of",
    "unit_key",
]

_WIDTH = 12

#: Fills an owed function may be offered and land nothing, under one
#: (test, fingerprint), before it counts as asked. Each fill already spends
#: the route's own attempt budget on it, so this many misses are that many
#: full route budgets on unchanged evidence. The same three the schedule
#: gives a hold before it lapses (`fill_schedule.DEFAULT_UNPAID_GRACE`).
DEFAULT_MISSES = 3

#: The rungs an owed function climbs, narrow to wide, before its test can
#: be spent: a unit route (per-fn, impl, file: the function and its
#: neighbours), then a slice session (the test's whole executed slice,
#: multi-turn, the test re-run between turns). A rung is spent when a body
#: landed through it, or after `misses` offers it landed nothing.
LADDER: tuple[str, ...] = ("unit", "session")

#: The ledger key an unread red test (no function attributed at all) is
#: counted under: its misses are the sweeps it stayed unread.
UNREAD = "<no function attributed>"


def unit_key(loc: FnLocator | tuple[str, str] | str) -> str:
    """`file::fn` — the spelling `BehaviourSpec.exercises` uses."""
    if isinstance(loc, str):
        return loc
    file, fn = loc[0], loc[1]
    return f"{file}::{fn}"


def _locator(key: str) -> FnLocator:
    file, _, fn = key.rpartition("::")
    return FnLocator(file, fn)


def feature_reach(specs: Iterable[Any]) -> dict[str, frozenset[str]]:
    """Per function (`file::fn`), the features whose examples exercise it.

    `specs` are `BehaviourSpec`s with `features` attached by the suite (the
    book features listing the example). A spec with no feature contributes
    its own id as its feature, so an example the book did not group still
    separates the functions it alone runs."""
    out: dict[str, set[str]] = {}
    for s in specs:
        feats = tuple(getattr(s, "features", ()) or ())
        if not feats:
            sid = str(getattr(s, "behaviour_id", "") or "")
            if not sid:
                continue
            feats = (f"example:{sid}",)
        for u in getattr(s, "exercises", ()) or ():
            out.setdefault(str(u), set()).update(feats)
    return {u: frozenset(fs) for u, fs in out.items()}


def culprits(feature: str, reach: Mapping[str, frozenset[str]]) -> frozenset[str]:
    """The functions only `feature`'s examples exercise."""
    return frozenset(u for u, fs in reach.items() if fs == {feature})


def tiers_of(slice_units: Iterable[str],
             reach: Mapping[str, frozenset[str]],
             spread: Mapping[str, int] | None = None,
             ) -> tuple[frozenset[str], ...]:
    """`slice_units` in the order they are owed: the narrowest non-zero reach
    first, then the functions no example exercises, then each wider reach.
    With `spread` (function -> how many red tests' slices hold it), the
    unexercised group is split, narrowest spread first. Never an empty tier;
    empty only for an empty slice."""
    by_count: dict[int, set[str]] = {}
    for u in slice_units:
        by_count.setdefault(len(reach.get(u, ())), set()).add(u)
    counted = sorted(c for c in by_count if c > 0)
    unexercised: list[frozenset[str]] = []
    if 0 in by_count:
        if spread:
            by_spread: dict[int, set[str]] = {}
            for u in by_count[0]:
                by_spread.setdefault(spread.get(u, 0), set()).add(u)
            unexercised = [frozenset(by_spread[k]) for k in sorted(by_spread)]
        else:
            unexercised = [frozenset(by_count[0])]
    return tuple([frozenset(by_count[c]) for c in counted[:1]]
                 + unexercised
                 + [frozenset(by_count[c]) for c in counted[1:]])


# Per-run noise in a libtest failure: the OS thread id after the test name
# (`thread 'test_as' (919225) panicked`) and raw addresses. Neither is
# evidence about a body, and either would make every sweep a new question.
_NOISE = (
    (re.compile(r"(thread '[^']*') \(\d+\)"), r"\1"),
    (re.compile(r"0x[0-9a-fA-F]{4,}"), "0x_"),
)


def evidence_text(text: str) -> str:
    """`text` with libtest's per-run noise masked and whitespace folded."""
    out = str(text)
    for pat, rep in _NOISE:
        out = pat.sub(rep, out)
    return " ".join(out.split())


def ask_fingerprint(test_evidence: str) -> str:
    """The identity of one ask: what one red test says about itself."""
    h = hashlib.sha256()
    h.update(b"test\0")
    h.update(evidence_text(test_evidence).encode("utf-8"))
    return h.hexdigest()[:_WIDTH]


@dataclass
class AskLedger:
    """Every (function, test) fingerprint a fill has asked, under one oracle.

    Keyed by oracle: a moved oracle is a different question set, so the
    ledger starts empty for it."""

    oracle_fp: str = ""
    seen: dict[str, set[str]] = field(default_factory=dict)
    #: (unit, test) -> fingerprint -> fills it was offered and landed nothing.
    misses: dict[str, dict[str, int]] = field(default_factory=dict)
    #: test -> (tree, fingerprint) of its last reading (:meth:`settle`).
    last: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: (unit, test) -> fingerprint -> why the last offer landed nothing:
    #: ``compile`` (the reply did not compile), ``not_planned`` (no run
    #: targeted it), ``no_landing`` (a run targeted it and landed nothing).
    reasons: dict[str, dict[str, str]] = field(default_factory=dict)
    #: (unit, test) -> fingerprint -> rung -> "asked" or the miss count.
    rungs: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)
    #: (unit, test) -> fingerprints whose ask carried the test's own words.
    words: dict[str, set[str]] = field(default_factory=dict)
    #: (unit, test) -> fingerprint -> digest of the diagnostic a repair saw.
    repairs: dict[str, dict[str, str]] = field(default_factory=dict)
    #: (unit, test) -> fingerprints whose rung cannot be built (no slice).
    absent: dict[str, set[str]] = field(default_factory=dict)
    #: Route-run labels a joint answer repaired (accept, or not a refutation).
    joint_repaired: set[str] = field(default_factory=set)
    #: (unit, test) -> fingerprints a compile miss was left pending on once.
    pending: dict[str, set[str]] = field(default_factory=dict)
    #: unit -> its own ask fingerprints (`evidence_premise.unit_ask_fingerprint`:
    #: own body + its red tests' words) a route OPENED it under. Read by the
    #: rank for served units nothing owes (`dispatch_rank.asked_unchanged`).
    unit_asks: dict[str, set[str]] = field(default_factory=dict)

    def record_unit(self, unit: str, fp: str) -> bool:
        """Record that a route opened `unit` under its own fingerprint
        `fp`; True when new. `fp` empty records nothing: an unobserved
        premise is never an ask."""
        if not fp:
            return False
        bucket = self.unit_asks.setdefault(unit, set())
        if fp in bucket:
            return False
        bucket.add(fp)
        return True

    def unit_asked(self, unit: str, fp: str) -> bool:
        """`unit` was opened under `fp` before. `fp` empty is never asked."""
        return bool(fp) and fp in self.unit_asks.get(unit, ())

    @staticmethod
    def _key(unit: str, test: str) -> str:
        return f"{unit}\t{test}"

    def asked(self, unit: str, test: str, fp: str) -> bool:
        return fp in self.seen.get(self._key(unit, test), ())

    def missed(self, unit: str, test: str, fp: str) -> int:
        return self.misses.get(self._key(unit, test), {}).get(fp, 0)

    def spent(self, unit: str, test: str, fp: str,
              misses: int = DEFAULT_MISSES) -> bool:
        """Asked under `fp`, or offered `misses` times with nothing landed."""
        return (self.asked(unit, test, fp)
                or (misses > 0 and self.missed(unit, test, fp) >= misses))

    def record(self, unit: str, test: str, fp: str, rung: str = "") -> bool:
        """Record one ask (a body landed, through `rung` when given); True
        when it was new on any rung."""
        if rung:
            self.rungs.setdefault(self._key(unit, test), {}).setdefault(
                fp, {})[rung] = -1
        bucket = self.seen.setdefault(self._key(unit, test), set())
        if fp in bucket:
            return False
        bucket.add(fp)
        return True

    def rung_spent(self, unit: str, test: str, fp: str, rung: str,
                   misses: int = DEFAULT_MISSES) -> bool:
        """`rung` asked under `fp` (a body landed through it), or offered on
        it `misses` times with nothing landed."""
        n = self.rungs.get(self._key(unit, test), {}).get(fp, {}).get(rung)
        return n is not None and (n < 0 or (misses > 0 and n >= misses))

    def next_rung(self, unit: str, test: str, fp: str,
                  ladder: Sequence[str], misses: int = DEFAULT_MISSES) -> str:
        """The first rung of `ladder` not spent under `fp`, or "" when every
        rung is. A one-rung ladder reads the rung-blind record (an ask on
        any route spends it), as before the ladder."""
        if len(ladder) <= 1:
            return "" if self.spent(unit, test, fp, misses) else (
                ladder[0] if ladder else "unit")
        for r in ladder:
            if not self.rung_spent(unit, test, fp, r, misses):
                return r
        return ""

    def miss(self, unit: str, test: str, fp: str, reason: str = "",
             rung: str = "", misses: int = DEFAULT_MISSES) -> int:
        """Record one offer that landed nothing, and why (on `rung` when
        given); the count after it (on that rung when given).

        Every offer counts: an offer no route planned is still an offer, and
        a rung that only ever finds no planned run must end, or the cluster
        never reaches "nothing new can be asked" (a session the planner never
        opens for a test kept its functions owed forever). A session-rung
        offer no route planned does not erase a real reason already stored
        for this fingerprint, and once it has spent the rung that way the
        rung is recorded absent: the test reads never-offered on that rung,
        not the model's."""
        key = self._key(unit, test)
        unplanned_side = reason == "not_planned" and rung not in ("", "unit")
        on_rung = 0
        if rung:
            per_r = self.rungs.setdefault(key, {}).setdefault(fp, {})
            if per_r.get(rung, 0) >= 0:
                per_r[rung] = per_r.get(rung, 0) + 1
            on_rung = max(per_r[rung], 0)
        per = self.misses.setdefault(key, {})
        per[fp] = per.get(fp, 0) + 1
        if reason:
            slot = self.reasons.setdefault(key, {})
            if not unplanned_side or not slot.get(fp):
                slot[fp] = reason
        if unplanned_side and misses > 0 and on_rung >= misses:
            self.absent.setdefault(key, set()).add(fp)
        return on_rung if rung else per[fp]

    def note_words(self, unit: str, test: str, fp: str) -> None:
        """The ask for `(unit, test)` under `fp` carried the test's words."""
        if fp:
            self.words.setdefault(self._key(unit, test), set()).add(fp)

    def words_carried(self, unit: str, test: str, fp: str) -> bool:
        got = self.words.get(self._key(unit, test), set())
        if fp in got:
            return True
        bare = fp.split("+", 1)[0]
        return bare in got or any(x.split("+", 1)[0] == bare for x in got)

    def note_repair(self, unit: str, test: str, fp: str, digest: str = "") -> None:
        """A repair turn saw this compile miss's diagnostic (`digest`)."""
        self.repairs.setdefault(self._key(unit, test), {})[fp] = digest

    def repair_seen(self, unit: str, test: str, fp: str = "") -> bool:
        """A repair was recorded for `fp`, or for the fingerprint a folded
        ask was stored on (the text before `+`)."""
        got = self.repairs.get(self._key(unit, test), {})
        if not fp:
            return bool(got)
        if fp in got:
            return True
        bare = fp.split("+", 1)[0]
        return bare in got or any(k.split("+", 1)[0] == bare for k in got)

    def note_compile_pending(self, unit: str, test: str, fp: str) -> bool:
        """The reply did not compile and no repair turn saw its diagnostic.
        The reason is stored, so the next prompt for the unit carries the
        compiler's words (`unit_notes`), and the offer is not charged.
        Pending at most once per (unit, test, fingerprint): True the first
        time; False after, when the caller charges the miss, because the
        next offer carried the diagnostic or none was ever stored to carry.
        Without that bound a route whose every reply fails to compile in one
        call kept its function owed forever."""
        key = self._key(unit, test)
        self.reasons.setdefault(key, {})[fp] = "compile"
        seen = self.pending.setdefault(key, set())
        if fp in seen:
            return False
        seen.add(fp)
        return True

    def note_absent(self, unit: str, test: str, fp: str, rung: str,
                    misses: int = DEFAULT_MISSES) -> None:
        """`rung` cannot be built (no session slice). Spend that rung and
        record ``route_absent`` so the test can end never-offered."""
        key = self._key(unit, test)
        per_r = self.rungs.setdefault(key, {}).setdefault(fp, {})
        if per_r.get(rung, 0) >= 0:
            per_r[rung] = max(per_r.get(rung, 0), misses if misses > 0 else 1)
        slot = self.reasons.setdefault(key, {})
        if slot.get(fp) in (None, "", "not_planned", "route_absent"):
            slot[fp] = "route_absent"
        self.absent.setdefault(key, set()).add(fp)

    def route_absent(self, unit: str, test: str) -> bool:
        return bool(self.absent.get(self._key(unit, test)))

    def reason(self, unit: str, test: str, fp: str) -> str:
        return self.reasons.get(self._key(unit, test), {}).get(fp, "")

    def reason_on(self, unit: str, test: str, fp: str) -> str:
        """The reason stored for `fp`, or for the fingerprint a folded ask
        (`fp+…`) was read from: not one from an earlier evidence state."""
        got = self.reasons.get(self._key(unit, test), {})
        if fp in got:
            return got[fp]
        bare = fp.split("+", 1)[0]
        for k, v in got.items():
            if k.split("+", 1)[0] == bare:
                return v
        return ""

    def reason_is(self, unit: str, test: str, reason: str) -> bool:
        """`reason` was stored for `(unit, test)` on any fingerprint, not
        only the one a later fold produced."""
        return reason in self.reasons.get(self._key(unit, test), {}).values()

    def reasons_for(self, unit: str, test: str) -> dict[str, str]:
        return dict(self.reasons.get(self._key(unit, test), {}))

    def settle(self, test: str, tree: str, fp: str) -> str:
        """The fingerprint `test` is read under on `tree`. A different
        reading on the tree its last reading was taken on is noise: the
        earlier fingerprint stands. `tree` empty: no tree reading, `fp`."""
        if not tree:
            return fp
        prev = self.last.get(test)
        if prev is not None and prev[0] == tree:
            return prev[1]
        self.last[test] = (tree, fp)
        return fp

    def for_oracle(self, oracle_fp: str) -> AskLedger:
        if oracle_fp == self.oracle_fp:
            return self
        return AskLedger(oracle_fp=oracle_fp)

    def to_dict(self) -> dict[str, Any]:
        return {"oracle_fp": self.oracle_fp,
                "seen": {k: sorted(v) for k, v in sorted(self.seen.items())},
                "misses": {k: dict(sorted(v.items()))
                           for k, v in sorted(self.misses.items())},
                "last": {t: list(v) for t, v in sorted(self.last.items())},
                "reasons": {k: dict(sorted(v.items()))
                            for k, v in sorted(self.reasons.items())},
                "rungs": {k: {f: dict(sorted(r.items())) for f, r in sorted(v.items())}
                          for k, v in sorted(self.rungs.items())},
                "words": {k: sorted(v) for k, v in sorted(self.words.items())},
                "repairs": {k: dict(sorted(v.items()))
                            for k, v in sorted(self.repairs.items())},
                "absent": {k: sorted(v) for k, v in sorted(self.absent.items())},
                "joint_repaired": sorted(self.joint_repaired),
                "pending": {k: sorted(v) for k, v in sorted(self.pending.items())},
                "unit_asks": {k: sorted(v)
                              for k, v in sorted(self.unit_asks.items())}}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> AskLedger:
        seen = raw.get("seen") or {}
        misses = raw.get("misses") or {}
        last = raw.get("last") or {}
        reasons = raw.get("reasons") or {}
        rungs = raw.get("rungs") or {}
        words = raw.get("words") or {}
        repairs = raw.get("repairs") or {}
        absent = raw.get("absent") or {}
        repaired = raw.get("joint_repaired") or ()
        pending = raw.get("pending") or {}
        unit_asks = raw.get("unit_asks") or {}
        return cls(oracle_fp=str(raw.get("oracle_fp", "")),
                   seen={str(k): {str(x) for x in v} for k, v in seen.items()},
                   misses={str(k): {str(f): int(n) for f, n in v.items()}
                           for k, v in misses.items()},
                   last={str(t): (str(v[0]), str(v[1]))
                         for t, v in last.items() if len(v) == 2},
                   reasons={str(k): {str(f): str(r) for f, r in v.items()}
                            for k, v in reasons.items()},
                   rungs={str(k): {str(f): {str(r): int(n) for r, n in rr.items()}
                                   for f, rr in v.items()}
                          for k, v in rungs.items()},
                   words={str(k): {str(x) for x in v} for k, v in words.items()},
                   repairs={str(k): {str(f): str(d) for f, d in v.items()}
                            for k, v in repairs.items()},
                   absent={str(k): {str(x) for x in v} for k, v in absent.items()},
                   joint_repaired={str(x) for x in repaired},
                   pending={str(k): {str(x) for x in v} for k, v in pending.items()},
                   unit_asks={str(k): {str(x) for x in v}
                              for k, v in unit_asks.items()})


@dataclass(frozen=True, slots=True)
class TestTiers:
    """One red test's reading: its tiers, the tier it owes now (-1 when it
    owes none, or when what it owes is the stub holding it) and the
    fingerprint each owed function would be asked under."""

    test: str
    tiers: tuple[frozenset[str], ...]
    owed_tier: int
    owed: Mapping[str, str]
    held_by: frozenset[str] = frozenset()
    #: owed function -> the ladder rung it is owed on.
    rungs: Mapping[str, str] = field(default_factory=dict)

    @property
    def exhausted(self) -> bool:
        """Nothing left to ask for this test under its evidence: every tier
        spent, or every stub holding it spent."""
        return not self.owed and bool(self.tiers or self.held_by)


@dataclass(frozen=True, slots=True)
class DueReading:
    """Every red test's tiers this sweep, and the union of what is owed."""

    by_test: Mapping[str, TestTiers]
    #: Red tests with no function attributed at all, and not yet read so for
    #: `misses` sweeps under one fingerprint: they keep the cluster open.
    unread: frozenset[str] = frozenset()
    #: Red tests read unread for `misses` sweeps: named, not open.
    unread_spent: frozenset[str] = frozenset()
    #: Red tests the original crate also fails under this cluster's cfg: out
    #: of the denominator, never owed, never holding the cluster open.
    withdrawn: frozenset[str] = frozenset()
    #: test -> the fingerprint it was read under (unread tests included).
    fingerprints: Mapping[str, str] = field(default_factory=dict)

    @property
    def units(self) -> frozenset[FnLocator]:
        out: set[FnLocator] = set()
        for r in self.by_test.values():
            out |= {_locator(u) for u in r.owed}
        return frozenset(out)

    def rung_of(self, unit: FnLocator) -> str:
        """The narrowest rung any red test owes `unit` on ("unit" when no
        test names one)."""
        k = unit_key(unit)
        found = [r.rungs.get(k, "") for r in self.by_test.values() if k in r.owed]
        ranked = sorted((LADDER.index(x) if x in LADDER else 0) for x in found)
        return LADDER[ranked[0]] if ranked else LADDER[0]

    def tests_for(self, unit: FnLocator) -> frozenset[str]:
        k = unit_key(unit)
        return frozenset(t for t, r in self.by_test.items() if k in r.owed)

    @property
    def exhausted(self) -> bool:
        """Nothing new can be asked: every red test that is not withdrawn
        has spent every stub holding it and every tier of its slice under
        its current fingerprint, or was read with no function at all for
        `misses` sweeps. False when no red test is left to judge (green, or
        only withdrawn tests: the reference floor closes that)."""
        if self.unread:
            return False
        if not (self.by_test or self.unread_spent):
            return False
        return all(r.exhausted for r in self.by_test.values())

    def answered(self, offered: Collection[FnLocator],
                 landed: Collection[FnLocator],
                 ) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
        """(asks, misses) of one fill: (unit, test, fingerprint) for every
        owed function the fill offered — an ask when its body landed, a miss
        when it did not. An owed function the batch cut is neither."""
        put = {unit_key(u) for u in offered}
        got = {unit_key(u) for u in landed}
        asks: list[tuple[str, str, str]] = []
        misses: list[tuple[str, str, str]] = []
        for t, r in sorted(self.by_test.items()):
            for u, fp in sorted(r.owed.items()):
                if u in got:
                    asks.append((u, t, fp))
                elif u in put:
                    misses.append((u, t, fp))
        return asks, misses

    def asks(self, opened: Collection[FnLocator]) -> list[tuple[str, str, str]]:
        """(unit, test, fingerprint) for every owed function in `opened`."""
        return self.answered(opened, opened)[0]


def due_reading(
    red_tests: Iterable[str],
    *,
    slices: Mapping[str, Collection[str]],
    reach: Mapping[str, frozenset[str]],
    evidence: Mapping[str, str],
    held: Mapping[str, Collection[str]],
    ledger: AskLedger,
    tree: str = "",
    misses: int = DEFAULT_MISSES,
    withdrawn: Collection[str] = (),
    joint: Iterable[JointCandidate] = (),
    ladder: Sequence[str] = ("unit",),
    lessons: Mapping[tuple[str, str], str] | None = None,
) -> DueReading:
    """What each red test owes this sweep.

    `slices` is test -> the functions it executes (`file::fn`); `evidence`
    test -> the sweep's failure text; `held` test -> the still-stubbed
    functions it dies in; `tree` the sweep's tree digest (fingerprints move
    only with it, :meth:`AskLedger.settle`); `withdrawn` the red tests the
    original crate fails too. A held test owes its unspent stubs and nothing
    else. Otherwise the first tier holding a function not spent under the
    test's fingerprint is owed, all of that tier's unspent functions
    together. With a `ladder` of more than one rung, a tier function stays
    owed until it is spent on every rung, and `TestTiers.rungs` names the
    rung it is owed on. A held stub climbs that same ladder. A joint
    candidate and a lesson are owed on the first rung only, once. Reads
    the ledger; `settle` is the one write (the tree each fingerprint was
    read on)."""
    out_ = frozenset(withdrawn)
    red = sorted(set(red_tests) - out_)
    joint_list = tuple(joint)
    spread: dict[str, int] = {}
    for t in red:
        for u in set(slices.get(t, ())):
            spread[u] = spread.get(u, 0) + 1
    by_test: dict[str, TestTiers] = {}
    unread: set[str] = set()
    unread_spent: set[str] = set()
    fps: dict[str, str] = {}
    for t in red:
        fp = ledger.settle(t, tree, ask_fingerprint(evidence.get(t, "")))
        fps[t] = fp
        sl = tuple(slices.get(t, ()))
        stubs = frozenset(held.get(t, ()))
        if not sl and not stubs:
            (unread_spent if ledger.spent(UNREAD, t, fp, misses)
             else unread).add(t)
            continue
        tiers = tiers_of(sl, reach, spread)
        if stubs:
            # A held stub climbs the same ladder as a tier function. One
            # unit-route landing does not exhaust it while a later rung
            # remains.
            nxt = {u: ledger.next_rung(u, t, fp, ladder, misses)
                   for u in sorted(stubs)}
            owed_h = {u: fp for u, r in nxt.items() if r}
            by_test[t] = TestTiers(
                t, tiers, -1, owed_h, held_by=stubs,
                rungs={u: nxt[u] for u in owed_h})
            continue
        # A reverted run that turned this test green owes its units again,
        # together and before any tier, once per candidate.
        joint_owed = {u: _fold(fp, c.digest) for c in joint_list
                      if t in c.gained for u in sorted(c.units)
                      if not ledger.spent(u, t, _fold(fp, c.digest), misses)}
        if joint_owed:
            by_test[t] = TestTiers(t, tiers, -1, joint_owed)
            continue
        # A lesson task 4 admitted for (function, this test) owes that
        # function again, first, once per lesson set (`domain/test_lessons`).
        lesson_owed = {u: _fold(fp, d) for (u, tt), d in sorted((lessons or {}).items())
                       if tt == t and not ledger.spent(u, t, _fold(fp, d), misses)}
        if lesson_owed:
            by_test[t] = TestTiers(t, tiers, -1, lesson_owed)
            continue
        owed_tier, owed, rung_of = -1, {}, {}
        for i, tier in enumerate(tiers):
            nxt = {u: ledger.next_rung(u, t, fp, ladder, misses)
                   for u in sorted(tier)}
            fresh = {u: fp for u, r in nxt.items() if r}
            if fresh:
                owed_tier, owed = i, fresh
                rung_of = {u: nxt[u] for u in fresh}
                break
        by_test[t] = TestTiers(t, tiers, owed_tier, owed, rungs=rung_of)
    return DueReading(by_test, frozenset(unread), frozenset(unread_spent),
                      frozenset(out_ & set(red_tests)), fps)


def render_failed(reading: DueReading, *, limit: int = 30) -> list[str]:
    """The `cluster_failed` lines: each red test, what holds it or its
    tiers, then the tests nothing could be attributed to and the tests
    withdrawn because the original fails them."""
    lines: list[str] = []
    for t, r in sorted(reading.by_test.items())[:limit]:
        if r.held_by:
            lines.append(f"{t}: held in still-stubbed "
                         + ", ".join(sorted(r.held_by))
                         + " (asked, or offered with nothing landed)")
            continue
        lines.append(
            f"{t}: " + " | ".join(
                "tier " + str(i + 1) + " [" + ", ".join(sorted(tier)[:6])
                + (f" +{len(tier) - 6}" if len(tier) > 6 else "") + "]"
                for i, tier in enumerate(r.tiers[:4])))
    for t in sorted(reading.unread_spent)[:limit]:
        lines.append(f"{t}: no function attributed (no slice, no frontier "
                     "entry, no crate failure site): never offered to a writer")
    for t in sorted(reading.withdrawn)[:limit]:
        lines.append(f"{t}: withdrawn: the original crate fails it under "
                     "this cluster's cfg")
    return lines


def tiers_event(reading: DueReading) -> dict[str, Any]:
    """The event shape: per test, tiers, the owed tier and its fingerprint."""
    return {t: {"tiers": [sorted(x) for x in r.tiers],
                "owed_tier": r.owed_tier,
                "owed": dict(sorted(r.owed.items())),
                "held_by": sorted(r.held_by),
                "rungs": dict(sorted(r.rungs.items())),
                "fingerprint": reading.fingerprints.get(t, "")}
            for t, r in sorted(reading.by_test.items())}


def screen_unentered(
    slices: Mapping[str, frozenset[str]],
    unentered: Callable[[str, str], bool],
) -> tuple[dict[str, frozenset[str]], dict[str, frozenset[str]]]:
    """Each red test's slice without the functions the sweep's coverage
    reading PROVES it never entered, and what was removed per test.

    A slice is coverage owners PLUS the units attribution named and the hop
    pair's members, and the tiers rank that union by reach. So a unit named
    by an identifier and proven unentered could be tier 1 because few
    examples exercise it. Run `3076-051d385a` iter 3 (2026-10-13): tier 1 of a
    red test was `[src/chain.rs::len, src/error.rs::as_ref]` on the sweep
    whose own service order listed `len` as executed by no failing test; the
    fill then planned a file run on `len` alone and a per-fn run on it.

    `unentered(unit, test)` answers on positive evidence only
    (`coverage_slice.unreached_by_test`: False for an unmeasured or crashed
    test). A slice the screen would empty is kept whole: the screen narrows a
    test's question, it never takes the test's question away. Held stubs are
    not slice members (`due_reading` takes them from `held`), so a stub a
    test dies in is never screened."""
    out: dict[str, frozenset[str]] = {}
    removed: dict[str, frozenset[str]] = {}
    for t, sl in slices.items():
        gone = frozenset(u for u in sl if unentered(u, t))
        if gone and gone != sl:
            out[t] = frozenset(sl) - gone
            removed[t] = gone
        else:
            out[t] = frozenset(sl)
    return out, removed


def slices_by_key(slices: Mapping[str, Iterable[Any]]) -> dict[str, frozenset[str]]:
    """Slice map with `FnSite`/`FnLocator` members as `file::fn` keys."""
    return {t: frozenset(unit_key((s[0], s[1])) for s in sl)
            for t, sl in slices.items()}


def merge_slices(primary: Mapping[str, frozenset[str]],
                 fallback: Mapping[str, Iterable[str]],
                 tests: Sequence[str],
                 site: Mapping[str, Iterable[str]] | None = None,
                 ) -> dict[str, frozenset[str]]:
    """`primary` per test, else `fallback` (the frontier's attribution),
    else `site` (the crate function its first failure is inside) — so a red
    test the session slicer skipped and attribution left out still names the
    function it fails through."""
    out: dict[str, frozenset[str]] = {}
    for t in tests:
        sl = (primary.get(t) or frozenset(fallback.get(t, ()))
              or frozenset((site or {}).get(t, ())))
        if sl:
            out[t] = sl
    return out


def miss_reasons(units: Collection[str], rows: Iterable[Any]) -> dict[str, str]:
    """Why each owed function (`file::fn`) a fill offered landed nothing,
    read off that fill's route-ledger rows: ``not_planned`` when no run
    targeted it, ``compile`` when a run that targeted it and did not keep
    it counted compile failures, else ``no_landing``."""
    rows = tuple(rows)

    def keys(pairs: Iterable[Any]) -> set[str]:
        return {f"{p[0]}::{p[1]}" for p in pairs or ()}

    out: dict[str, str] = {}
    for u in units:
        targeting = [r for r in rows if u in keys(getattr(r, "targets", ()))]
        if not targeting:
            out[u] = "not_planned"
        elif any(int((getattr(r, "classes", None) or {}).get("compile", 0)) > 0
                 and u not in keys(getattr(r, "kept", ()))
                 for r in targeting):
            out[u] = "compile"
        else:
            out[u] = "no_landing"
    return out


def _fold(fp: str, extra: str) -> str:
    """`fp` with one more channel folded in (a joint candidate, a lesson):
    the same test evidence asked under something new is a new question."""
    return f"{fp}+{extra}" if extra else fp


@dataclass(frozen=True, slots=True)
class JointCandidate:
    """A reverted route run that turned red tests green and a sibling red:
    a right-or-better answer a gate refused. Its units are owed again,
    together, to the tests it turned green. `bodies` is the text of those
    units as it stood before the revert, kept off the tree."""

    label: str
    units: frozenset[str]
    gained: frozenset[str]
    lost: frozenset[str]
    bodies: Mapping[str, str] = field(default_factory=dict)
    repaired: bool = False

    @property
    def digest(self) -> str:
        parts = [self.label, *sorted(self.units), *sorted(self.gained),
                 *sorted(self.lost)]
        parts.extend(f"{k}={self.bodies[k]}" for k in sorted(self.bodies))
        h = hashlib.sha256("\0".join(parts).encode("utf-8"))
        return "j" + h.hexdigest()[:_WIDTH]


@dataclass(frozen=True, slots=True)
class JointVerdict:
    """Whether a joint answer may stay on the tree.

    Accept only when every gained test is green after the fill and every
    newly red test is green again. Refute only when a test the candidate
    itself gained is red after: the joint answer lost the candidate's own
    gain. A test outside the candidate's gained and lost sets says nothing
    about it: the rest of the fill wrote other bodies, judged by their own
    gates. Empty `gained` owes nothing."""

    accept: bool
    refute: bool
    why: str


def joint_accept(*, gained: Collection[str], lost: Collection[str],
                 after_green: Collection[str]) -> JointVerdict:
    """The union gate over the candidate's own tests. Bare test names."""
    def bare(names: Collection[str]) -> frozenset[str]:
        return frozenset(n.rsplit("::", 1)[-1] for n in names)

    got = bare(gained)
    if not got:
        return JointVerdict(False, False, "empty gained owes nothing")
    after = bare(after_green)
    gain_lost = got - after
    if gain_lost:
        return JointVerdict(
            False, True,
            "lost a test the candidate had gained: "
            + ", ".join(sorted(gain_lost)))
    still_red = bare(lost) - after
    if not still_red:
        return JointVerdict(
            True, False, "gains stayed green and the newly red tests came back")
    return JointVerdict(
        False, False, "gains held, still red: " + ", ".join(sorted(still_red)))


def joint_rollback(cand: Mapping[Any, Sequence[str]],
                   prior: Mapping[Any, Sequence[str]],
                   now: Callable[[Any], Sequence[str]]) -> dict[Any, Sequence[str]]:
    """The functions a rejected joint answer puts back, and to what: each
    candidate function still holding the candidate body gets its prior
    body. One the fill rewrote keeps the fill's body, and nothing outside
    the candidate is touched: a file-level restore erased every other body
    the fill landed in those files while the route rows still recorded
    them kept."""
    return {loc: prior[loc] for loc, body in cand.items()
            if prior.get(loc) and tuple(now(loc)) == tuple(body)}


def _bodies_of(row: Any) -> dict[str, str]:
    raw = getattr(row, "candidate_bodies", None)
    if raw is None and isinstance(row, Mapping):
        raw = row.get("candidate_bodies")
    out: dict[str, str] = {}
    if isinstance(raw, Mapping):
        for k, text in raw.items():
            if text:
                out[str(k)] = str(text)
        return out
    for item in raw or ():
        if isinstance(item, (list, tuple)) and len(item) == 3 and item[2]:
            out[f"{item[0]}::{item[1]}"] = str(item[2])
        elif isinstance(item, (list, tuple)) and len(item) == 2 and item[1]:
            out[str(item[0])] = str(item[1])
    return out


def joint_candidates(rows: Iterable[Any], red_tests: Collection[str],
                     ) -> tuple[JointCandidate, ...]:
    """The route-ledger rows (this run, this oracle) whose run reverted
    units, turned at least one still-red test green, and reddened another
    test. Run `450922-661c5e9f` reverted the `src/ensure.rs` file run and the
    Debug/Display pair this way; the bodies they had written are kept on
    the row (`candidate_bodies`) so the next ask can start from them."""
    red = {t.rsplit("::", 1)[-1] for t in red_tests}
    out: list[JointCandidate] = []
    for r in rows:
        reverted = frozenset(f"{p[0]}::{p[1]}"
                             for p in getattr(r, "reverted", ()) or ())
        lost = frozenset(getattr(r, "newly_red", ()) or ())
        gained = frozenset(
            g.rsplit("::", 1)[-1]
            for g in (getattr(r, "newly_green", ()) or ())) & red
        if reverted and lost and gained:
            out.append(JointCandidate(
                str(getattr(r, "label", "")), reverted, gained, lost,
                _bodies_of(r),
                bool(getattr(r, "joint_repaired", False))))
    return tuple(out)


def joint_note(candidates: Iterable[JointCandidate], test: str) -> str:
    """The line a gained test's words carry into every prompt that prints
    them, or "" when no candidate gained it."""
    notes = [
        (f"(An earlier answer for {', '.join(sorted(c.units)[:6])} turned "
         f"this test green and was reverted because it broke "
         f"{', '.join(sorted(c.lost)[:6])}: write these functions so this "
         "test passes AND those stay green.)")
        for c in candidates if test in c.gained]
    return "\n".join(notes)
