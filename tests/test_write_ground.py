"""A write is judged on the tests it was bought on; only the convicting ones
may revert it.

Run `3366333-dfeac80c` (2026-10-29), fill 1: the per-fn run's claim stood on
an escalated test (the dispatch projection), while its keep gate was handed
the convicting projection, empty for those five bodies; each was kept "no
test is attributed to this body", graded unreachable. The pair after it was
gated on its convicting test, already green, and graded mis-targeted, while
the escalated test it was bought for ran in neither gate. Over the
`phase_c.verdict` rows carrying that reason since 2026-10-28: 22 keeps.

Claims (invented shape):
- the frontier builds the ground: judged is the dispatch set less tests only
  a homonym's site convicts, convicting is the body's own; convicting is
  always inside judged;
- the gate runs every judged test; a judged test that is not convicting and
  fails inside the write is a measured ACCEPT that names it, never a REJECT;
  a convicting failure still rejects;
- the per-fn grade of that keep is WRONG_BODY, not UNREACHABLE;
- nothing in the pipeline builds a ground except the frontier.
Negative control: the pre-fix call, the convicting set as both halves, is the
NO_TESTS keep graded unreachable.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from pipeline_core.application.reconstruct.cargo_runner import u2_test_gate
from pipeline_core.domain.attribution import FailureFrontier, WriteGround
from pipeline_core.domain.fill_class import (
    FillOutcomeClass,
    class_for_kept,
    keep_reading,
)
from pipeline_core.domain.fill_class import (
    tests_could_not_run as could_not_run,
)
from pipeline_core.domain.models import FnLocator, FnSite
from pipeline_core.domain.test_report import U2Decision

_U = FnLocator("src/a.rs", "unit")


def _frontier() -> FailureFrontier:
    # t_direct convicts the unit; t_esc reached it only by escalation.
    return FailureFrontier.build({_U: {"t_direct", "t_esc"}},
                                 {_U: {"t_direct"}})


def test_the_frontier_builds_the_ground_of_a_body_and_of_a_transaction(
        ) -> None:
    g = _frontier().ground_for_body("src/a.rs", "unit", "")
    assert g == WriteGround(frozenset({"t_direct", "t_esc"}),
                            frozenset({"t_direct"}))
    txn = _frontier().ground_for_units([_U])
    assert txn == g
    # An escalated test is judged and does not convict. The signature has
    # no argument that can promote the dispatch set into the revert set.
    assert "t_esc" in txn.judged and "t_esc" not in txn.convicting
    with pytest.raises(TypeError):
        _frontier().ground_for_units([_U], dispatch_convicts={_U})  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="not in judged"):
        WriteGround(frozenset(), frozenset({"t"}))


def test_a_homonyms_measured_test_is_not_judged_on_the_other_body() -> None:
    alpha = FnSite("src/a.rs", "unit", "impl S for Alpha")
    f = FailureFrontier.build(
        {_U: {"t_alpha", "t_esc"}}, {alpha: {"t_alpha"}})
    from dataclasses import replace
    f = replace(f, body_exact=frozenset({alpha}))
    beta = f.ground_for_body("src/a.rs", "unit", "impl S for Beta")
    assert beta.judged == frozenset({"t_esc"}) and not beta.convicting
    a = f.ground_for_body("src/a.rs", "unit", "impl S for Alpha")
    assert a.judged == frozenset({"t_alpha", "t_esc"})
    assert a.convicting == frozenset({"t_alpha"})


class _Structure:
    def enumerate_fns(self, _src: Path) -> list:
        return []


def _run(rc: int, out: str):
    seen: dict[str, str] = {}

    def _fn(cmd: str, _cwd: Path, _env: dict[str, str], *,
            timeout: int = 180) -> tuple[int, str]:
        seen["cmd"] = cmd
        return rc, out
    _fn.seen = seen  # type: ignore[attr-defined]
    return _fn


def _gate(run, ground: WriteGround):
    return u2_test_gate(
        run, ground, frozenset({FnSite("src/a.rs", "unit", "")}),
        "cargo test", Path("."), {}, structure=_Structure(), enabled=True,
        timeout=120, err_cap=2000, guard=lambda *_: None)


_ESC_RED = ("test t_direct ... ok\ntest t_esc ... FAILED\n\n"
            "---- t_esc stdout ----\n"
            "thread 't_esc' panicked at tests/t.rs:3:5:\nassertion failed\n")


def test_the_gate_runs_the_escalated_test_and_reads_it_without_reverting(
        ) -> None:
    run = _run(101, _ESC_RED)
    v = _gate(run, _frontier().ground_for_body("src/a.rs", "unit", ""))
    assert "t_esc" in run.seen["cmd"]
    assert v.decision is U2Decision.ACCEPT and v.measured
    assert v.judged_red == frozenset({"t_esc"})
    assert v.reported == frozenset({"t_direct", "t_esc"})


def test_a_convicting_failure_still_reverts() -> None:
    out = ("test t_esc ... ok\ntest t_direct ... FAILED\n\n"
           "---- t_direct stdout ----\n"
           "thread 't_direct' panicked at tests/t.rs:3:5:\nassertion failed\n")
    v = _gate(_run(101, out), _frontier().ground_for_body("src/a.rs", "unit", ""))
    assert v.decision is U2Decision.REJECT
    assert v.self_failures == frozenset({"t_direct"})


def _grade(v, judged: frozenset[str]) -> FillOutcomeClass:
    return class_for_kept(
        reading=keep_reading(net_delta=0, newly_green=(), attributed=judged),
        passed=v.self_passed, unreachable=could_not_run(),
        attributed=judged, measured=v.measured)


def test_a_keep_bought_on_an_escalated_test_is_graded_on_it() -> None:
    """The per-fn shape of the live fill: no convicting test at the body."""
    f = FailureFrontier.build({_U: {"t_esc"}}, {})
    ground = f.ground_for_body("src/a.rs", "unit", "")
    out = ("test t_esc ... FAILED\n\n---- t_esc stdout ----\n"
           "thread 't_esc' panicked at tests/t.rs:3:5:\nassertion failed\n")
    v = _gate(_run(101, out), ground)
    assert _grade(v, ground.judged) is FillOutcomeClass.WRONG_BODY
    # Negative control: the pre-fix gate was handed the convicting set only.
    pre = WriteGround(ground.convicting, ground.convicting)
    v0 = _gate(_run(101, out), pre)
    assert v0.decision is U2Decision.NO_TESTS
    assert _grade(v0, pre.judged) is FillOutcomeClass.UNREACHABLE_VERDICT


def test_only_the_frontier_builds_a_ground() -> None:
    root = Path(__file__).resolve().parents[1] / "pipeline_core"
    pat = re.compile(r"\bWriteGround\(")
    hits = [f"{p.relative_to(root)}:{i}"
            for p in root.rglob("*.py")
            if p.name != "attribution.py"
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if pat.search(line)]
    assert hits == []
