"""Held-out captures: the part of the pipeline's own captured behaviours the
fill never sees, and the agreement reading taken on them.

The loop optimises the oracle's verdict. Whether the reconstructed crate
behaves like the original is a different quantity, and until this module no
line of any log stated it. The one recorded comparison (workspace commit
`14acd2b0`, 2026-08-25, stable 98 pass / 7 fail, against anyhow 1.0.102)
found every pipeline-authored test green on a tree wrong in three public
behaviours: only the crate's own tests disagreed.

A capture is a program run against the ORIGINAL pre-strip with its stdout
recorded as the Then. A deterministic share of them is set aside when taken:
never emitted as a test, never in the suite a prompt reads, never in the
book. When a cluster's fill closes, and at the final test phase, each
held-out program is run against the reconstruction and compared with what
the original printed. The pair (oracle pass count, held-out agreement) is the
reading: a tree green on its oracle and short on agreement is wrong where no
test of its own looks.

It is a measurement and stays one. The split is keyed on the PROGRAM TEXT,
not on the capture's id: an id is `probe::<cluster>::<unit>::<n>` with `n`
the order of a model reply, so the same program would fall on either side of
the split from one pass to the next and two readings would be two samples.
Only pipeline-authored captures are split; the crate's own tests are the
oracle the project is defined against and are never held out.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum, auto

from pipeline_core.domain.behaviour import BehaviourSpec, is_probe_id


def capture_key(spec: BehaviourSpec) -> str:
    """What a capture RUNS, as one string: its setup, its expression, the
    kind it is observed under and the env it is pinned to. Two captures of
    one program share it whatever their ids."""
    kind = spec.then.kind.name if spec.then is not None else ""
    env = ";".join(f"{k}={v}" for k, v in spec.required_env)
    return "\x00".join((" ".join(spec.setup.split()), " ".join(spec.when.split()), kind, env))


def is_held_out(spec: BehaviourSpec, share_pct: int) -> bool:
    """True for a pipeline-authored capture whose program text falls in the
    held-out `share_pct` percent. A harvested example never is."""
    if share_pct <= 0 or not is_probe_id(spec.behaviour_id):
        return False
    digest = hashlib.sha256(capture_key(spec).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % 100 < min(share_pct, 100)


def split_held_out(
    specs: Iterable[BehaviourSpec], share_pct: int,
) -> tuple[tuple[BehaviourSpec, ...], tuple[BehaviourSpec, ...]]:
    """`(kept, held out)`, each in input order."""
    kept: list[BehaviourSpec] = []
    held: list[BehaviourSpec] = []
    for s in specs:
        (held if is_held_out(s, share_pct) else kept).append(s)
    return tuple(kept), tuple(held)


class Agreement(Enum):
    """One held-out program against the reconstruction."""

    AGREES = auto()      # it printed what the original printed
    DIFFERS = auto()     # it ran and printed something else
    NOT_RUN = auto()     # it did not build, panicked or timed out


@dataclass(frozen=True, slots=True)
class AgreementRow:
    """One held-out capture's outcome, for the per-capture file a person
    reads. Never an input to dispatch, attribution or a prompt."""

    behaviour_id: str
    outcome: Agreement
    expected: str = ""
    observed: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"behaviour_id": self.behaviour_id, "outcome": self.outcome.name,
                "expected": self.expected, "observed": self.observed}


@dataclass(frozen=True, slots=True)
class AgreementReading:
    """The agreement reading of one tree: counts only. `oracle_pass` /
    `oracle_total` and `stubs` are the tree's own numbers at the same
    moment, printed beside it when the caller has them (None: unknown)."""

    cluster: str
    where: str
    agree: int = 0
    differ: int = 0
    not_run: int = 0
    oracle_pass: int | None = None
    oracle_total: int | None = None
    stubs: int | None = None

    @property
    def held(self) -> int:
        return self.agree + self.differ + self.not_run

    def line(self) -> str:
        beside = []
        if self.oracle_pass is not None and self.oracle_total is not None:
            beside.append(f"oracle {self.oracle_pass}/{self.oracle_total}")
        if self.stubs is not None:
            beside.append(f"{self.stubs} stub(s) left")
        tail = f" ({', '.join(beside)})" if beside else ""
        return (f"behaviour-agreement: {self.agree} of {self.held} held-out capture(s) agree, "
                f"{self.differ} differ, {self.not_run} did not run{tail}")

    def payload(self) -> dict[str, object]:
        return {"cluster": self.cluster, "where": self.where, "held": self.held,
                "agree": self.agree, "differ": self.differ, "not_run": self.not_run,
                "oracle_pass": self.oracle_pass, "oracle_total": self.oracle_total,
                "stubs": self.stubs}


def reading_of(cluster: str, where: str, rows: Iterable[AgreementRow], *,
               oracle_pass: int | None = None, oracle_total: int | None = None,
               stubs: int | None = None) -> AgreementReading:
    tally = dict.fromkeys(Agreement, 0)
    for r in rows:
        tally[r.outcome] += 1
    return AgreementReading(cluster, where, tally[Agreement.AGREES], tally[Agreement.DIFFERS],
                            tally[Agreement.NOT_RUN], oracle_pass, oracle_total, stubs)
