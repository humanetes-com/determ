"""The shared repair loop for a test candidate that did not build, and the
task 0d probe that now uses it.

The census of 2026-10-20 (`scripts/test_writer_census.py`): task 0c's fix ask
recovered 266 of the 431 blocks that needed it; task 0d's probes had no
repair and dropped 19 of 72 (run `202215-c5c6347b`) and 25 of 48 (run
`486447-5207101c`) for not building.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pipeline_core.application.tasks import task0d
from pipeline_core.application.test_repair import repair_until_built
from pipeline_core.domain.behaviour_probe import CapturedProbe, ProbeResult
from pipeline_core.domain.models import BuildCluster

_E0599 = ("error[E0599]: no method named `chain_len` found for struct "
          "`anyhow::Error` in the current scope\n --> examples/p.rs:5:9\n")


def test_the_loop_asks_with_the_diagnostics_until_the_candidate_builds() -> None:
    seen: list[tuple[str, str]] = []
    runs = {"bad": (False, _E0599), "good": (True, "")}
    cand, res, asks = repair_until_built(
        "bad", run=lambda c: runs[c],
        diagnostics=lambda r: None if r[0] else r[1],
        ask=lambda c, d: (seen.append((c, d)), "good")[1], attempts=2)
    assert (cand, res, asks) == ("good", (True, ""), 1)
    assert seen == [("bad", _E0599)]
    # Bounded, and an ask that returns nothing ends it.
    _c, _r, n = repair_until_built(
        "bad", run=lambda c: runs["bad"], diagnostics=lambda r: r[1],
        ask=lambda c, d: "bad", attempts=2)
    assert n == 2
    _c, _r, n = repair_until_built(
        "bad", run=lambda c: runs["bad"], diagnostics=lambda r: r[1],
        ask=lambda c, d: None, attempts=2)
    assert n == 1


def _cluster() -> BuildCluster:
    return BuildCluster(name="stable", env_extra={}, tarpaulin_exclude_files=[],
                        excluded_cfg=[], active_cfg=[], prompt_nightly_rule="")


def _capture(attempts: int) -> tuple[object, list[str], list[str]]:
    asked: list[str] = []
    logs: list[str] = []

    def run_probe(_pd, _c, program, _tag, **_kw):  # type: ignore[no-untyped-def]
        if "chain_len" in program:
            return ProbeResult(False, "", _E0599)
        return ProbeResult(True, "3\n", "")

    reply = ('<BEHAVIOUR name="probe">\n<GIVEN>\nlet e = anyhow!("x");\n</GIVEN>\n'
             "<WHEN>e.chain().count()</WHEN>\n<OBSERVE>display</OBSERVE>\n</BEHAVIOUR>\n")
    deps = SimpleNamespace(
        run_probe=run_probe, ask=lambda p: (asked.append(p), reply)[1],
        log=logs.append, PROBE_REPAIR_ATTEMPTS=attempts)
    probe = CapturedProbe(name="chain_count", given='let e = anyhow!("x");',
                          when="e.chain_len()", observe="display")
    misses: list[dict[str, str]] = []
    spec, _runs = task0d._capture_twice(
        deps, Path("/w"), "anyhow", _cluster(), probe, unit="chain_count", n=0,  # type: ignore[arg-type]
        tag="t", default_cluster="stable", misses=misses)
    return spec, asked, logs


def test_a_probe_that_did_not_build_is_asked_back_and_captured() -> None:
    """NEGATIVE CONTROL: with no repair attempts the probe is dropped, as
    every non-building probe was before."""
    spec, asked, logs = _capture(1)
    assert spec is not None
    assert len(asked) == 1 and "error[E0599]" in asked[0] and "e.chain_len()" in asked[0]
    assert any("repaired after 1 ask(s)" in m for m in logs)
    dropped, asked0, logs0 = _capture(0)
    assert dropped is None and asked0 == []
    assert any("dropped (failed" in m for m in logs0)
