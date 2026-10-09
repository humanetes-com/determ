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
- ``compile_loss`` (C): every offer of its owed functions under its current
  evidence ended in a reply that did not compile, and none landed.
- ``never_offered`` (N): no function of it was ever asked or offered, every
  offer found no route that planned a run, or no function could be
  attributed to it at all.
- ``model_limit`` (M): nothing new can be asked for it (every stub and tier
  spent), and the writer was asked: bodies landed or answers were refused,
  and the test is still red.
- ``open`` : the cluster ended (crash, floor, backstop) while something was
  still owed for it; no verdict on the writer.

G, C and N are pipeline defects: one of them at a cluster end is a reason
to fix the pipeline, never an acceptable end state. M is the model's. The
prompt-evidence class (a prompt that lacked the test's words) is read on
the prompts themselves (`scripts/owed_prompt_words.py`), not here.

Nothing here names a crate, file, test, trait or symbol."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pipeline_core.domain.candidate_tiers import AskLedger, DueReading

__all__ = ["PIPELINE_CLASSES", "RedClass", "RedEntry", "RunFacts", "red_ledger",
           "render_red_ledger"]


class RedClass(StrEnum):
    WITHDRAWN = "withdrawn"
    REFUSED_GAIN = "refused_gain"
    COMPILE_LOSS = "compile_loss"
    NEVER_OFFERED = "never_offered"
    MODEL_LIMIT = "model_limit"
    OPEN = "open"


#: The classes that are the pipeline's fault, never an acceptable end.
PIPELINE_CLASSES = frozenset({RedClass.REFUSED_GAIN, RedClass.COMPILE_LOSS,
                              RedClass.NEVER_OFFERED})


@dataclass(frozen=True, slots=True)
class RunFacts:
    """One route run of this cluster, as the ledger reads it."""

    label: str
    reverted: frozenset[str]
    newly_red: frozenset[str]
    newly_green: frozenset[str]

    @classmethod
    def of_row(cls, row: Any) -> RunFacts:
        def keys(pairs: Iterable[Any]) -> frozenset[str]:
            return frozenset(f"{p[0]}::{p[1]}" for p in pairs or ())
        return cls(label=str(getattr(row, "label", "")),
                   reverted=keys(getattr(row, "reverted", ())),
                   newly_red=frozenset(getattr(row, "newly_red", ()) or ()),
                   newly_green=frozenset(getattr(row, "newly_green", ()) or ()))


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


def red_ledger(red: Iterable[str], *, reading: DueReading, ledger: AskLedger,
               runs: Iterable[RunFacts] = (),
               words: Mapping[str, str] | None = None) -> tuple[RedEntry, ...]:
    """One entry per red test (bare names), the class decided in this order:
    withdrawn, refused gain, compile loss, never offered, model limit, open."""
    runs = tuple(runs)
    out: list[RedEntry] = []
    for t in sorted({_bare(x) for x in red}):
        if t in reading.withdrawn:
            out.append(RedEntry(t, RedClass.WITHDRAWN,
                                "the original crate fails it under this cfg"))
            continue
        gained_by = [r for r in runs
                     if r.reverted and r.newly_red
                     and t in {_bare(x) for x in r.newly_green}]
        if gained_by:
            out.append(RedEntry(
                t, RedClass.REFUSED_GAIN,
                "a reverted run turned it green: " + gained_by[-1].label,
                {"runs": [r.label for r in gained_by],
                 "newly_red": sorted(gained_by[-1].newly_red)}))
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
        ever_asked = [u for u in units
                      if ledger.seen.get(AskLedger._key(u, t))]
        missed = {u: ledger.missed(u, t, fp) for u in units
                  if ledger.missed(u, t, fp)}
        reasons = {u: ledger.reason(u, t, fp) for u in missed}
        facts = {"fingerprint": fp, "asked": asked, "missed": missed,
                 "words": " ".join(str((words or {}).get(t, "")).split())[:600],
                 "miss_reasons": reasons, "held_by": sorted(r.held_by),
                 "owed": sorted(r.owed),
                 "tiers": [sorted(x) for x in r.tiers[:4]]}
        why_set = set(reasons.values())
        if not ever_asked and not missed:
            out.append(RedEntry(t, RedClass.NEVER_OFFERED,
                                "no owed function was ever asked or offered",
                                facts))
        elif not ever_asked and why_set == {"not_planned"}:
            out.append(RedEntry(t, RedClass.NEVER_OFFERED,
                                "its owed functions were offered and no route "
                                "planned a run for them", facts))
        elif not asked and why_set == {"compile"}:
            out.append(RedEntry(t, RedClass.COMPILE_LOSS,
                                "every offer under its evidence failed to compile",
                                facts))
        elif r.exhausted:
            out.append(RedEntry(t, RedClass.MODEL_LIMIT,
                                "every stub and tier spent: the writer was asked "
                                "and nothing it wrote turned the test green",
                                facts))
        else:
            out.append(RedEntry(t, RedClass.OPEN,
                                "the cluster ended while something was owed",
                                facts))
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
