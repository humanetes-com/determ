"""Replay one recorded slice session through the real `run_slice_session`.

A walk replays the TDD loop with the fill stubbed whole; a fill replay
(`tests/fills/harness.py`) replays the schedule with the routes faked. This
harness replays ONE SESSION: the recorded `phase_c.session.turn` series of
run `3014175-d98ed09e` (2026-09-08, distilled by `tools/distill_session.py`)
drives the real harness loop — its splice, its readings, its keep rule, its
peel — with the model, the compiler and the suite scripted from the record.

What it MODELS rather than replays, named here so the list is extended
deliberately:

- Bodies. The events carry no body text; a turn's bodies are the marker
  `turn<N>` and the baseline body is `BASE`, so a unit the model set back
  to its baseline text (excluded from the record's `kept`) counts as
  changed here.
- The suite's reading of a tree state. The record holds one reading per
  JUDGED turn, taken on the tree as the loop held it then. The replay
  reads a tree as the recorded reading of the NEWEST judged turn whose
  marker is on it (the baseline reading when none is): a restored tree is
  byte-identical to the state whose reading was measured, and on the
  recorded peel of session #1 the newest change set (turn 3's `fmt`)
  dominated every intermediate state, which is what the record's `kept 1`
  requires. A state the record cannot read this way (a compile-failed
  turn, an inconclusive one) is restored by the loop before it is judged.
- Readings as test sets. `compare_readings` is real; the readings are
  built so its verdict equals the record — the targets by name, the
  recorded newly-red names, one binary per recorded crash label carrying
  enough baseline passes for the recorded net delta, and synthetic
  passengers for the rest of the count. A record the model cannot satisfy
  raises rather than passes.
- Time, the probe, coverage and the request answerer (a request turn is
  answered with a fixed line).

What a replay may assert: which turn's state the session ended on, what it
kept and reverted, the net delta its route row records (`row_delta`, the
reading `note_kept` records at the session's close), what the transcript
told the model, and how many turns were restored or re-asked. Never a
score of the crate.
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.slice_session import (
    SessionOutcome,
    SessionPorts,
    run_slice_session,
)
from pipeline_core.domain.models import FnRecord, FnSite
from pipeline_core.domain.slice_session import SessionSlice
from pipeline_core.domain.test_results import BinaryResult, TestStateReading

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "sessions"
BASE = "BASE"
_LINE = re.compile(r"^UNIT (?P<file>[^:]+)::(?P<fn>[^:]*)::(?P<cont>.*?) = (?P<body>.*)$")


def load_sessions(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def site_of(d: Mapping[str, Any]) -> FnSite:
    return FnSite(str(d["file"]), str(d["fn"]), str(d.get("container", "")))


@dataclass
class Replay:
    outcome: SessionOutcome
    final_bodies: dict[FnSite, str]
    #: What the session's route row records as its net: the final reading's
    #: delta when it kept anything (`note_kept` at the session's close), 0
    #: otherwise — the same rule `impl_fill` applies.
    row_delta: int
    log: list[str] = field(default_factory=list)
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    #: Every user turn the harness wrote back, in order.
    user_turns: list[str] = field(default_factory=list)
    #: What the run clock was told after each judged turn.
    deltas_noted: list[int] = field(default_factory=list)


class _Tree:
    """One file per slice file, one `UNIT` line per unit."""

    def __init__(self, root: Path, units: list[FnSite]) -> None:
        self.root = root
        by_file: dict[str, list[FnSite]] = {}
        for u in units:
            by_file.setdefault(u.file, []).append(u)
        for f, us in by_file.items():
            p = root / f
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("".join(f"UNIT {u.file}::{u.fn_name}::{u.container} = {BASE}\n"
                                 for u in us), encoding="utf-8")

    def bodies(self) -> dict[FnSite, str]:
        out: dict[FnSite, str] = {}
        for p in self.root.rglob("*.rs"):
            for ln in p.read_text(encoding="utf-8").splitlines():
                m = _LINE.match(ln)
                if m:
                    out[FnSite(m["file"], m["fn"], m["cont"])] = m["body"]
        return out

    def splice(self, path: Path, container: str, fn_name: str, body: str,
               where: Any, **_k: Any) -> tuple[bool, str, int]:
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        hit = False
        for i, ln in enumerate(lines):
            m = _LINE.match(ln)
            if m and m["fn"] == fn_name and m["cont"] == container:
                lines[i] = f"UNIT {m['file']}::{m['fn']}::{m['cont']} = {body}"
                hit = True
        if not hit:
            return False, "no_such_unit", 0
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True, "", 0

    def newest_turn(self) -> int:
        turns = [int(b[4:]) for b in self.bodies().values()
                 if b.startswith("turn") and b[4:].isdigit()]
        return max(turns, default=0)


def _bare(name: str) -> str:
    return name.rsplit("::", 1)[-1]


class _Suite:
    """The recorded readings as `TestStateReading`s (see the module
    docstring for the construction)."""

    def __init__(self, session: Mapping[str, Any]) -> None:
        self.targets = [_bare(t) for t in session["tests"]]
        judged = [t for t in session["turns"] if t["kind"] == "judged"]
        self.by_turn = {int(t["turn"]): t for t in judged}
        self.red_names = sorted({n for t in judged for n in t["newly_red"]})
        labels = sorted({c for t in judged for c in t["crashed"]})
        # Baseline passes per crash label: enough that no recorded turn
        # needs a negative passenger count.
        need = 1
        for t in judged:
            if not t["crashed"]:
                continue
            green = len(self.targets) - len(t["targets_failing"])
            short = green - int(t["pass_delta"]) - len(t["newly_red"])
            need = max(need, math.ceil(short / len(t["crashed"])))
        self.crash_passes = dict.fromkeys(labels, need)
        base = session.get("baseline_pass")
        total = int(base) if base is not None else 50
        self.passengers = max(
            0, total - len(self.red_names) - sum(self.crash_passes.values()))

    def _crash_binaries(self, crashed: list[str]) -> list[BinaryResult]:
        out: list[BinaryResult] = []
        for lab, n in self.crash_passes.items():
            names = frozenset(f"{lab}::c{i}" for i in range(n))
            if lab in crashed:
                out.append(BinaryResult(label=lab, completed=False,
                                        passed=frozenset(), failed=frozenset(),
                                        pass_count=0, fail_count=0))
            else:
                out.append(BinaryResult(label=lab, completed=True, passed=names,
                                        failed=frozenset(), pass_count=n,
                                        fail_count=0))
        return out

    def baseline(self) -> TestStateReading:
        passed = frozenset(self.red_names) | frozenset(
            f"p{i}" for i in range(self.passengers))
        failed = frozenset(self.targets)
        return TestStateReading(binaries=(
            BinaryResult(label="suite", completed=True, passed=passed,
                         failed=failed, pass_count=len(passed),
                         fail_count=len(failed)),
            *self._crash_binaries([])))

    def after(self, turn: int) -> TestStateReading:
        t = self.by_turn[turn]
        failing = {_bare(x) for x in t["targets_failing"]}
        green = [x for x in self.targets if x not in failing]
        red = set(t["newly_red"])
        lost = sum(self.crash_passes[c] for c in t["crashed"])
        extra = int(t["pass_delta"]) + len(red) + lost - len(green)
        n_pass = self.passengers + extra
        if n_pass < 0:
            raise AssertionError(
                f"turn {turn}: the recorded delta {t['pass_delta']} cannot be "
                "modelled without a negative passenger count")
        passed = (frozenset(green) | (frozenset(self.red_names) - red)
                  | frozenset(f"p{i}" for i in range(n_pass)))
        failed = frozenset(failing) | frozenset(red)
        return TestStateReading(binaries=(
            BinaryResult(label="suite", completed=True, passed=passed,
                         failed=failed, pass_count=len(passed),
                         fail_count=len(failed)),
            *self._crash_binaries(list(t["crashed"]))))


def replay(session: Mapping[str, Any], root: Path, *,
           keep_best: bool = True, end_on_keepable: bool = True,
           max_turns: int | None = None) -> Replay:
    units = sorted({site_of(u) for t in session["turns"] for u in t["units"]}
                   | {site_of(u) for u in session["units"]},
                   key=lambda u: (u.file, u.fn_name, u.container))
    tree = _Tree(root, units)
    records = {u: FnRecord(file=u.file, fn_name=u.fn_name, container=u.container,
                           sig_text=f"fn {u.fn_name}()", body_text=BASE)
               for u in units}
    suite = _Suite(session)
    turns = list(session["turns"])
    state = {"i": 0, "kind": "", "baseline_taken": False}
    log: list[str] = []
    events: list[tuple[str, dict[str, Any]]] = []
    user_turns: list[str] = []
    deltas: list[int] = []

    def ask(messages: list[dict[str, Any]]) -> str:
        user_turns.append(str(messages[-1]["content"]))
        if state["i"] >= len(turns):
            state["kind"] = "no_change"
            return ""
        t = turns[state["i"]]
        state["i"] += 1
        state["kind"] = t["kind"]
        if t["kind"] == "request":
            return '<REQUEST kind="file" path="src/x.rs"/>'
        if t["kind"] == "rejected":
            return '<FN name="__not_in_the_slice__">turn0</FN>'
        if t["kind"] in ("no_change", "capped"):
            return ""
        return "\n".join(
            (f'<FN name="{u["fn"]}" block="{u["container"]}">turn{t["turn"]}</FN>'
             if u["container"] else f'<FN name="{u["fn"]}">turn{t["turn"]}</FN>')
            for u in t["units"])

    def check() -> tuple[int, str, list[Any]]:
        if state["kind"] == "compile":
            return 1, "error[E0308]: mismatched types", []
        return 0, "", []

    def measure(raw: list[str]) -> TestStateReading | None:
        raw.append("")
        if not state["baseline_taken"]:
            state["baseline_taken"] = True
            return suite.baseline()
        if state["kind"] == "inconclusive":
            return None
        newest = tree.newest_turn()
        return suite.after(newest) if newest else suite.baseline()

    ports = SessionPorts(
        ask_turns=ask, splice=tree.splice, check=check,
        focus_errors=lambda out, f: out, blamed_type_api=lambda t: t,
        measure_suite=measure, read_source=lambda p: None,
        enumerate_bodies=tree.bodies,
        answer_pulls=lambda r: "answered." if "<REQUEST" in r else None,
        log=log.append, emit=lambda name, **kw: events.append((name, dict(kw.get("data") or {}))),
        check_graceful_stop=lambda s: None,
        note_delta=deltas.append)
    slice_ = SessionSlice(tests=tuple(session["tests"]), units=tuple(units))
    out = run_slice_session(
        ports, target_src_dir=root, cluster_name=str(session.get("cluster", "t")),
        slice_=slice_, records=records, turn0="turn0",
        max_turns=max_turns or int(session.get("max_turns", 8)),
        keep_best=keep_best, end_on_keepable=end_on_keepable)
    row = out.final.pass_delta if (out.kept and out.final is not None) else 0
    return Replay(outcome=out, final_bodies=tree.bodies(), row_delta=row,
                  log=log, events=events, user_turns=user_turns,
                  deltas_noted=deltas)
