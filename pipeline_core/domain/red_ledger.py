"""Why each red test is still red when its cluster ends.

WHY. A cluster that ends red must say, per red test, whether the pipeline
held it or the model did. Without that, a failed run is a count: run
`39786-fd38e72f` (2026-10-09) left `stable` at 137/165 with 28 red tests and
no record of which had been offered to a writer, which bodies a gate had
refused, or which never compiled. The classes:

- ``withdrawn`` (W): the original crate fails it under this cluster's cfg; it
  cannot be made green and is out of the denominator.
- ``refused_gain`` (G): a run whose bodies turned this test green was
  reverted because a sibling went red. A right-or-better body was refused.
- ``compile_loss`` (C): an offer ended in a reply that did not compile, and
  no repair turn saw that body's diagnostics.
- ``never_offered`` (N): no function of it was ever asked or offered, every
  offer found no route that planned a run, the session route cannot be
  built, or no function could be attributed to it at all.
- ``evidence_missing`` (E): a real ask was made and the test's own words
  were not in it.
- ``model_limit`` (M): nothing new can be asked for it (every stub and tier
  spent), and the writer was asked: a landing or a planned run on every
  rung, the test's words in that ask, every compile miss repaired, and
  every gained revert jointly repaired. The test is still red.
- ``open`` : the cluster ended (crash, floor, backstop) while something was
  still owed for it; no verdict on the writer.

G, C, N and E are pipeline defects: one of them at a cluster end is a
reason to fix the pipeline, never an acceptable end state. M is the model's.

Nothing here names a crate, file, test, trait or symbol."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pipeline_core.domain.candidate_tiers import AskLedger, DueReading
from pipeline_core.domain.test_sight import SightReading

__all__ = ["PIPELINE_CLASSES", "RedClass", "RedEntry", "RunFacts", "red_ledger",
           "render_red_ledger"]


class RedClass(StrEnum):
    WITHDRAWN = "withdrawn"
    REFUSED_GAIN = "refused_gain"
    COMPILE_LOSS = "compile_loss"
    UNSEEN = "unseen"
    NEVER_OFFERED = "never_offered"
    EVIDENCE_MISSING = "evidence_missing"
    MODEL_LIMIT = "model_limit"
    OPEN = "open"


#: The classes that are the pipeline's fault, never an acceptable end.
PIPELINE_CLASSES = frozenset({RedClass.REFUSED_GAIN, RedClass.COMPILE_LOSS,
                              RedClass.UNSEEN, RedClass.NEVER_OFFERED,
                              RedClass.EVIDENCE_MISSING})


@dataclass(frozen=True, slots=True)
class RunFacts:
    """One route run of this cluster, as the ledger reads it."""

    label: str
    reverted: frozenset[str]
    newly_red: frozenset[str]
    newly_green: frozenset[str]
    #: A joint answer for this run kept the union, or failed without
    #: refuting a green the candidate also kept. G does not stick.
    joint_repaired: bool = False

    @classmethod
    def of_row(cls, row: Any) -> RunFacts:
        def keys(pairs: Iterable[Any]) -> frozenset[str]:
            return frozenset(f"{p[0]}::{p[1]}" for p in pairs or ())
        return cls(label=str(getattr(row, "label", "")),
                   reverted=keys(getattr(row, "reverted", ())),
                   newly_red=frozenset(getattr(row, "newly_red", ()) or ()),
                   newly_green=frozenset(getattr(row, "newly_green", ()) or ()),
                   joint_repaired=bool(getattr(row, "joint_repaired", False)))


@dataclass(frozen=True, slots=True)
class RedEntry:
    test: str
    klass: RedClass
    why: str
    facts: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"test": self.test, "class": str(self.klass), "why": self.why,
                **dict(self.facts)}


def _bare(name: str) -> str:
    return name.rsplit("::", 1)[-1]


def _units_of(reading: Any) -> list[str]:
    out = [u for tier in reading.tiers for u in sorted(tier)]
    out.extend(sorted(reading.held_by))
    return list(dict.fromkeys(out))


_NOT_A_RUN = frozenset({"not_planned", "route_absent"})


def _landed(ledger: AskLedger, unit: str, test: str) -> bool:
    """A body landed for `(unit, test)` on some fingerprint or rung."""
    key = AskLedger._key(unit, test)
    if ledger.seen.get(key):
        return True
    return any(n < 0 for rungs in ledger.rungs.get(key, {}).values()
               for n in rungs.values())


def _session_landed(ledger: AskLedger, unit: str, test: str) -> bool:
    key = AskLedger._key(unit, test)
    return any((rungs.get("session") or 0) < 0
               for rungs in ledger.rungs.get(key, {}).values()
               if "session" in rungs)


def _real_unit(ledger: AskLedger, unit: str, test: str) -> bool:
    """A landing, or a planned miss. ``not_planned`` and ``route_absent``
    are not runs. A bare miss (no reason stored) is a planned miss."""
    if _landed(ledger, unit, test):
        return True
    counts = ledger.misses.get(AskLedger._key(unit, test), {})
    if not any(counts.values()):
        return False
    reasons = set(ledger.reasons_for(unit, test).values())
    if not reasons:
        return True
    return bool(reasons - _NOT_A_RUN)


def red_ledger(red: Iterable[str], *, reading: DueReading, ledger: AskLedger,
               sight: SightReading,
               runs: Iterable[RunFacts] = (),
               words: Mapping[str, str] | None = None) -> tuple[RedEntry, ...]:
    """One entry per red test (bare names).

    Order: withdrawn, unrepaired refused gain, unseen (the exit sweep held no
    observation of the test executing: a measurement gap, the pipeline's,
    with the reason `test_sight` read), never offered (including a session
    route that cannot be built), unrepaired compile loss, evidence missing,
    open while something is owed, model limit only with proof. `sight` is
    required: an exit that could not judge sight passes one not taken."""
    runs = tuple(runs)
    out: list[RedEntry] = []
    for t in sorted({_bare(x) for x in red}):
        if t in reading.withdrawn:
            out.append(RedEntry(t, RedClass.WITHDRAWN,
                                "the original crate fails it under this cfg"))
            continue
        gained_by = [r for r in runs
                     if r.reverted and r.newly_red and not r.joint_repaired
                     and t in {_bare(x) for x in r.newly_green}]
        if gained_by:
            out.append(RedEntry(
                t, RedClass.REFUSED_GAIN,
                "a reverted run turned it green: " + gained_by[-1].label,
                {"runs": [r.label for r in gained_by],
                 "newly_red": sorted(gained_by[-1].newly_red)}))
            continue
        unseen = sight.by_test.get(t)
        if unseen is not None and sight.is_unseen(t):
            out.append(RedEntry(
                t, RedClass.UNSEEN,
                f"no observation of it executing: {unseen.reason}"
                + (f" ({unseen.detail})" if unseen.detail else ""),
                {"reason": str(unseen.reason), "detail": unseen.detail}))
            continue
        if t in reading.unread or t in reading.unread_spent:
            out.append(RedEntry(t, RedClass.NEVER_OFFERED,
                                "no function could be attributed to it"))
            continue
        r = reading.by_test.get(t)
        if r is None:
            out.append(RedEntry(t, RedClass.NEVER_OFFERED,
                                "no owed reading was taken for it"))
            continue
        fp = reading.fingerprints.get(t, "")
        units = _units_of(r)
        asked = [u for u in units if ledger.asked(u, t, fp)]
        missed = {u: ledger.missed(u, t, fp) for u in units
                  if ledger.missed(u, t, fp)}
        reasons = {}
        for u in units:
            got = ledger.reasons_for(u, t)
            if got:
                reasons[u] = got.get(fp) or next(iter(got.values()))
        supplied = ""
        if words is not None:
            supplied = " ".join(str(words.get(t, "")).split())
        carried = any(
            ledger.words_carried(u, t, fp)
            or bool(ledger.words.get(AskLedger._key(u, t)))
            for u in units)
        facts = {"fingerprint": fp, "asked": asked, "missed": missed,
                 "words": supplied[:600],
                 "miss_reasons": reasons, "held_by": sorted(r.held_by),
                 "owed": sorted(r.owed),
                 "tiers": [sorted(x) for x in r.tiers[:4]]}
        real = any(_real_unit(ledger, u, t) for u in units)
        absent = any(ledger.route_absent(u, t) for u in units)
        session_landed = any(_session_landed(ledger, u, t) for u in units)
        unrepaired_compile = [
            u for u in units
            if ledger.reason_is(u, t, "compile") and not ledger.repair_seen(u, t)]
        if not real:
            why = ("its owed functions were offered and no route planned a "
                   "run for them" if missed or absent
                   else "no owed function was ever asked or offered")
            out.append(RedEntry(t, RedClass.NEVER_OFFERED, why, facts))
        elif absent and not session_landed:
            out.append(RedEntry(
                t, RedClass.NEVER_OFFERED,
                "its session route was never planned or cannot be built",
                facts))
        elif unrepaired_compile:
            out.append(RedEntry(
                t, RedClass.COMPILE_LOSS,
                "an offer failed to compile and no repair saw the diagnostic",
                facts))
        elif supplied and not carried and not r.held_by:
            # A test still held in a stub printed only that stub's own
            # panic: there were no words about a body to carry.
            out.append(RedEntry(
                t, RedClass.EVIDENCE_MISSING,
                "a real ask did not carry the test's words", facts))
        elif not r.exhausted:
            out.append(RedEntry(t, RedClass.OPEN,
                                "the cluster ended while something was owed",
                                facts))
        else:
            out.append(RedEntry(
                t, RedClass.MODEL_LIMIT,
                "every stub and tier spent: the writer was asked "
                "and nothing it wrote turned the test green", facts))
    return tuple(out)


def render_red_ledger(entries: Iterable[RedEntry], *, limit: int = 40) -> list[str]:
    """Log lines: a count per class, then one line per test."""
    entries = tuple(entries)
    counts: dict[str, int] = {}
    for e in entries:
        counts[str(e.klass)] = counts.get(str(e.klass), 0) + 1
    head = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    lines = [f"red ledger: {len(entries)} red test(s): {head}"]
    for e in entries[:limit]:
        lines.append(f"  {e.klass}: {e.test} — {e.why}")
    return lines
