"""Walk: run `4012759-5c5177c6` (2026-10-21/22, cluster `stable`), the joint
ask that sat on the shared tree for a whole fill.

Fill 9 opened with the iteration-8 candidate (`session[4 test(s), 6 unit(s)]
#0`: kept `from_boxed`, reverted `debug`, turned `test_debug` green and three
display/debug tests red) spliced before the fill. Every baseline of the fill
read 337 against the 343 sweep, a pair body breaking the same tests was kept
blind, and the next sweep's batch ratchet reverted four files.

What the walk asserts is reachability, on the recorded sweeps:
- every fill is handed the candidate as one joint session slice whose seed
  is the reverted body alone (`from_boxed` was kept and is already on the
  tree), so the candidate reaches a route through a judged turn;
- no route's premise in any fill is read on a tree other than the one the
  sweep measured, and no `UNJUDGED_WRITE` is logged.

Negative controls in the same module: today's pre-fill splice injected
(a write outside every hold before the first route) is restored by the
fill's ground before the premise reads it, with the health line; and with
the ground disabled the premise reads the candidate's bytes, the fill-9
defect. What the walk cannot assert is the score: the batch revert of
iteration 10 is a consequence of the recorded 339 reading, which a fixed
observation sequence serves whatever the code does.

Test identity follows the harness: the gained test is `t360`, a name that
fails on every recorded sweep; the lost ones are `t1`..`t3`.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from pipeline_core.application.reconstruct import phase_c_tdd
from pipeline_core.application.reconstruct.splice import splice_lock_for
from pipeline_core.domain.fill_schedule import RouteKind, RouteLedgerRow, RunOutcome
from pipeline_core.domain.models import ClusterFillResult
from tests.walks.harness import load_walk, replay

pytestmark = pytest.mark.walk

_RUN = "4012759-5c5177c6"
_LABEL = "session[4 test(s), 6 unit(s)] #0"


def _iteration_8_row() -> RouteLedgerRow:
    return RouteLedgerRow(
        kind=RouteKind.SESSION, label=_LABEL,
        targets=(("src/error.rs", "from_boxed"), ("src/fmt.rs", "debug")),
        bodies_sha="s8", oracle_fp="o", outcome=RunOutcome.REVERTED,
        run_id=_RUN, iteration=8, cluster="stable", net_pass_delta=-4,
        kept=(("src/error.rs", "from_boxed"),),
        reverted=(("src/fmt.rs", "debug"),),
        newly_green=("t360",), newly_red=("t1", "t2", "t3"),
        target_tests=("t1", "t2", "t3", "t360"),
        candidate_bodies={"src/error.rs::from_boxed": "FROM_BOXED",
                          "src/fmt.rs::debug": "DEBUG"})


def _records(_src: Any, **_k: Any) -> list[dict[str, Any]]:
    return [{"file": "src/a.rs", "fn_name": "f", "body_text": "orig"},
            {"file": "src/fmt.rs", "fn_name": "debug", "body_text": "x"},
            {"file": "src/error.rs", "fn_name": "from_boxed", "body_text": "y"}]


def _engine(seen: list[dict[str, Any]], *, inject: bool):  # type: ignore[no-untyped-def]
    """A fill that opens one route: it takes the tree's lock and reads its
    premise, as every route does. `inject` writes the candidate first,
    outside every hold — the pre-fill splice this walk is about."""
    def fill(**kw: Any) -> ClusterFillResult:
        root = Path(kw["target_src_dir"])
        if inject:
            (root / "src" / "a.rs").write_text("CANDIDATE", encoding="utf-8")
        with splice_lock_for(root):
            seen.append({
                "iter": kw.get("fill_iteration"),
                "joint": list(kw.get("joint_slices") or ()),
                "premise": (root / "src" / "a.rs").read_text(encoding="utf-8")})
        return ClusterFillResult(attempts=1, fns_filled=0)
    return fill


def _replay(monkeypatch, tmp_path: Path, *, inject: bool) -> tuple[list[str], list[dict]]:  # type: ignore[no-untyped-def]
    # The owed reading, where a joint candidate is read off the route
    # ledger, runs only when the behaviour specs are wired (production wires
    # them); an empty spec set changes nothing else the walk reads.
    import tests.test_tdd_ratchet as ratchet
    base = ratchet._driver_deps

    def _with_specs(*a: Any, **k: Any) -> Any:
        return replace(base(*a, **k), behaviour_specs_for=lambda pd, cl: [])

    monkeypatch.setattr(ratchet, "_driver_deps", _with_specs)
    seen: list[dict[str, Any]] = []
    events: list[tuple[str, dict]] = []
    logs = replay(load_walk(_RUN), monkeypatch, tmp_path, events=events,
                  fill=_engine(seen, inject=inject), enumerate_fns=_records,
                  route_rows=[_iteration_8_row()], oracle_fp="o",
                  max_stalls="0", no_frontier_stop="0", backstop="12")
    return logs, seen


def test_every_fill_is_offered_the_candidate_as_one_seeded_session(monkeypatch, tmp_path: Path) -> None:
    logs, seen = _replay(monkeypatch, tmp_path, inject=False)
    assert seen, "the replay reached no fill"
    for fill in seen:
        (joint,) = fill["joint"]
        assert joint.label() == f"joint[{_LABEL}]"
        assert joint.seed == (("src/fmt.rs::debug", "DEBUG"),)
        assert set(joint.tests) == {"t1", "t2", "t3", "t360"}
        assert joint.joint_gained == ("t360",)
    assert all(f["premise"] == "orig" for f in seen)
    assert not [ln for ln in logs if "UNJUDGED_WRITE" in ln]


def test_control_a_pre_fill_splice_is_restored_before_the_premise(monkeypatch, tmp_path: Path) -> None:
    logs, seen = _replay(monkeypatch, tmp_path, inject=True)
    assert seen and all(f["premise"] == "orig" for f in seen)
    assert [ln for ln in logs if "HEALTH ‼ UNJUDGED_WRITE" in ln]


def test_control_without_the_ground_the_premise_reads_the_candidate(monkeypatch, tmp_path: Path) -> None:
    @contextmanager
    def _no_ground(*_a: Any, **_k: Any) -> Iterator[None]:
        yield None

    monkeypatch.setattr(phase_c_tdd, "fill_ground", _no_ground)
    _logs, seen = _replay(monkeypatch, tmp_path, inject=True)
    assert seen and all(f["premise"] == "CANDIDATE" for f in seen)
