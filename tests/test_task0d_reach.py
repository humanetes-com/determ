"""t0d's reach round: a probe along the MIR's public route to each function no
example enters, judged by the probe's own site-level coverage.

Driven through `run_task0d` with the fakes of `test_task0d.py`. What the plan
claims, asserted here:

- a routed function no spec enters gets ONE ask, in public terms only: the
  route's public call, the result side to unwrap, the render the dump says
  reaches it (`{:#?}` for an alternate-gated Debug dispatch); control: a
  function some spec enters gets no ask;
- a function without a route (a proved-unreachable one has none) gets no ask;
- the outcome is read from the probe's coverage, never from the reply: a probe
  whose hits do not name the target is `probe_missed` even when its reply says
  otherwise; one whose hits do is `reached` and becomes an emitted capture;
- t0c's NEVER EXECUTED block quotes a measured miss beside the route; a
  reached target carries no such line.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.tasks import task0c as task0c_mod
from pipeline_core.application.tasks.task0d import _reach_unit, run_task0d
from pipeline_core.domain.behaviour_probe import probe_behaviour_id
from pipeline_core.domain.fn_execution import FnTarget
from pipeline_core.domain.fn_routes import FnRoutes, GatedDispatch, PublicRoute, WorldFacts
from pipeline_core.domain.models import FnSite
from pipeline_core.domain.reach_probe import PROBE_FAILED, PROBE_MISSED, REACHED, ReachOutcome
from tests.test_task0d import _REPLY, _cluster, _World

_TARGET = "src/wrapper.rs::fmt [impl Debug for DisplayError<M>]"
_OTHER = "src/wrapper.rs::fmt [impl Debug for MessageError<M>]"
_FACTS = WorldFacts("stable", routes={
    _TARGET: FnRoutes(
        (PublicRoute("`<Option<T> as Context<T, Infallible>>::context`",
                     ("`Error::construct_from_display`",), "builds `DisplayError`", "Err"),),
        (GatedDispatch("`ErrorImpl::debug`", "<dyn Error + Send + Sync as Debug>::fmt", "alternate"),)),
    _OTHER: FnRoutes((PublicRoute("`Error::msg`", ("`Error::construct_from_adhoc`",),
                                  "builds `MessageError`"),)),
})
_REACH_REPLY = (
    '<BEHAVIOUR name="reach_1">\n<GIVEN>\n    let err = None::<u8>.context("boom").unwrap_err();\n</GIVEN>\n'
    "<WHEN>err</WHEN>\n<OBSERVE>debug</OBSERVE>\n</BEHAVIOUR>\n"
    '<BEHAVIOUR name="reach_2">\n<GIVEN>\n    let err = Error::msg("m");\n</GIVEN>\n'
    "<WHEN>err</WHEN>\n<OBSERVE>debug</OBSERVE>\n</BEHAVIOUR>")
# A spec the harvest owns, whose hits are the OTHER target: it is entered.
_HARVESTED = "testassert::tests/test_fmt.rs::test_altdebug::0"


def _run(tmp_path: Path, *, sites: dict[str, frozenset[str]],
         facts: WorldFacts | None = _FACTS) -> tuple[_World, list[ReachOutcome]]:
    w = _World(reply=_REPLY, exercises={_HARVESTED: frozenset({"src/fmt.rs::fmt"})})
    w.sites = dict(sites)
    saved: list[ReachOutcome] = []

    def ask(prompt: str) -> str:
        w.prompts.append(prompt)
        return _REACH_REPLY if "reach_1" in prompt else _REPLY

    deps = w.deps(ask=ask, load_construction_facts=lambda _p, c: facts if c == "stable" else None,
                  save_reach=lambda _p, _c, out: saved.extend(out))
    run_task0d(deps, tmp_path, "anyhow", [_cluster()], tmp_path / "src")
    return w, saved


def _reach_id(label: str) -> str:
    return probe_behaviour_id("stable", _reach_unit(label), 0)


def test_an_unentered_routed_function_gets_one_public_ask(tmp_path: Path) -> None:
    w, _ = _run(tmp_path, sites={_HARVESTED: frozenset({_OTHER})})
    asks = [p for p in w.prompts if "reach_1" in p]
    assert len(asks) == 1
    ask = asks[0]
    assert "`<Option<T> as Context<T, Infallible>>::context`" in ask
    assert "take it with `.unwrap_err()`" in ask
    assert "render the value with `{:#?}` (OBSERVE `alt_debug`)" in ask
    # Public terms only: neither the target nor the crate fn on the way is named.
    assert "DisplayError" not in ask and "construct_from_display" not in ask
    # Control: the function the harvested spec enters is not asked about.
    assert "Error::msg" not in ask and "reach_2" not in ask


def test_a_function_without_a_route_gets_no_ask(tmp_path: Path) -> None:
    w, saved = _run(tmp_path, sites={}, facts=WorldFacts("stable"))
    assert not any("reach_1" in p for p in w.prompts) and saved == []


def test_the_outcome_is_the_probes_own_coverage_not_its_reply(tmp_path: Path) -> None:
    # The probe ran and entered only Error's Display: missed.
    _, saved = _run(tmp_path, sites={_HARVESTED: frozenset({_OTHER}),
                                     _reach_id(_TARGET): frozenset({"src/fmt.rs::display"})})
    (out,) = saved
    assert out.label == _TARGET and out.outcome == PROBE_MISSED
    assert "context(\"boom\").unwrap_err()" in out.given and out.observe == "alt_debug"


def test_a_probe_that_enters_its_target_is_reached_and_emitted(tmp_path: Path) -> None:
    w, saved = _run(tmp_path, sites={_HARVESTED: frozenset({_OTHER}),
                                     _reach_id(_TARGET): frozenset({_TARGET})})
    (out,) = saved
    assert out.outcome == REACHED and out.behaviour_id == _reach_id(_TARGET)
    suite = w.saved["stable"]
    spec = next(s for s in suite.specs if s.behaviour_id == _reach_id(_TARGET))
    # The reply said `debug`; the line gave `alt_debug`, and that is what ran.
    assert spec.then is not None and spec.then.kind.name == "ALT_DEBUG_STRING"


def test_a_probe_that_does_not_capture_is_recorded_with_its_reason(tmp_path: Path) -> None:
    w = _World(reply=_REPLY, probe_ok=False)
    saved: list[ReachOutcome] = []
    deps = w.deps(ask=lambda p: _REACH_REPLY if "reach_1" in p else _REPLY,
                  load_construction_facts=lambda _p, _c: _FACTS,
                  save_reach=lambda _p, _c, out: saved.extend(out))
    run_task0d(deps, tmp_path, "anyhow", [_cluster()], tmp_path / "src")
    assert {o.outcome for o in saved} == {PROBE_FAILED}
    assert all("failed" in o.reason for o in saved)


def test_t0c_quotes_a_measured_miss_beside_the_route_and_nothing_for_a_reached_one() -> None:
    src = "impl<M> Debug for DisplayError<M> {\n    fn fmt(&self) {}\n}\n"
    tgt = FnTarget(FnSite("src/wrapper.rs", "fmt", "impl Debug for DisplayError<M>"), 2, 2, False)
    missed = ReachOutcome(_TARGET, PROBE_MISSED, given='let err = anyhow!("x");', when="err",
                          observe="alt_debug")
    out = task0c_mod._render_unexecuted_fns(src, (tgt,), _FACTS, {tgt.label: missed})
    assert "Reached from the public API" in out
    assert ("// Measured on the original crate: a program that built `let err = anyhow!(\"x\");` "
            "and rendered `err` with `{:#?}` ran and did NOT enter this function") in out
    assert "does not reach" not in out
    failed = ReachOutcome(
        _TARGET, PROBE_FAILED, given='None::<u8>.context("boom").unwrap_err()', when="err",
        observe="alt_debug", reason="cargo failed")
    failed_out = task0c_mod._render_unexecuted_fns(src, (tgt,), _FACTS, {tgt.label: failed})
    assert 'None::<u8>.context("boom").unwrap_err()' in failed_out
    assert "cargo failed" in failed_out
    assert "does not reach" not in failed_out
    reached = ReachOutcome(_TARGET, REACHED)
    assert "Measured on the original" not in task0c_mod._render_unexecuted_fns(
        src, (tgt,), _FACTS, {tgt.label: reached})


def test_the_probe_cap_bounds_the_reach_round(tmp_path: Path) -> None:
    """Review remainder: a positive `TASK0D_PROBE_MAX` did not limit how many
    routed functions the round asks. With both targets unentered and a cap
    of 1, one is asked and the log says the rest were not. Control: the
    default cap asks both."""
    w = _World(reply=_REPLY, exercises={_HARVESTED: frozenset({"src/fmt.rs::fmt"})})
    w.sites = {_HARVESTED: frozenset()}
    logs: list[str] = []

    def ask(prompt: str) -> str:
        w.prompts.append(prompt)
        return _REACH_REPLY if "reach_1" in prompt else _REPLY

    deps = w.deps(ask=ask, load_construction_facts=lambda _p, c: _FACTS if c == "stable" else None,
                  PROBE_MAX_PER_CLUSTER=1, PROBE_BLOCKS_PER_REPLY=4)
    from dataclasses import replace
    deps = replace(deps, log=logs.append)
    run_task0d(deps, tmp_path, "anyhow", [_cluster()], tmp_path / "src")
    asks = [p for p in w.prompts if "reach_1" in p]
    assert len(asks) == 1 and "reach_2" not in asks[0]
    assert any("reach probes capped at 1 of 2" in ln for ln in logs)
