"""The generation pass's half of the function-execution criterion.

Driven through `_run_phase_c` on the microcrate fixture with the fakes of
`test_task0c_contract.py` (cargo compiles and passes, replies scripted). What
the entry claims, asserted here:

- a file carrying a function no test executes goes to the front of the queue,
  ahead of the easy-first order (negative control: without the target the
  easy-first file comes first);
- the prompt lists each such function's body as NEVER EXECUTED, names its cfg
  world, and the instruction asks for a call to each;
- the cluster rules are rendered from the prompt's own targets — a cluster's
  gated function is named as gated, a shared one as shared, and no prompt says
  "stable lines are already covered", the static claim the baked rule used to
  carry beside a target list that contradicted it;
- a block that compiled and ran charges one attempt to each target its lines
  covered; a pass with no block charges nothing.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from pipeline_core.application.tasks.task0c import (
    Task0cFileHistory,
    _PhaseCCtx,
    _PhaseCOutcome,
    _run_phase_c,
)
from pipeline_core.domain.cluster_plan import nightly_rules_for, plan_clusters, target_scope_rule
from pipeline_core.domain.coverage import PerFileCoverage
from pipeline_core.domain.fn_execution import FnTarget
from pipeline_core.domain.models import FnSite
from tests.test_task0c_contract import _MICROCRATE, EXACT, _Harness

_CLUSTERS = plan_clusters({"extra_cap": 1}, {}, cargo_features=[], default_features=[],
                          feature_gate_cfgs=[])
_STOCK, _EXTRA = _CLUSTERS[0], _CLUSTERS[1]
# microcrate/src/lib.rs: `add` spans 27-29; `capacity_hint` under
# `#[cfg(any(extra_cap, feature = "extra_cap"))]` spans 55-57.
_ADD = FnTarget(FnSite("src/lib.rs", "add", ""), 27, 29, False)
_HINT = FnTarget(FnSite("src/lib.rs", "capacity_hint", ""), 55, 57, True)


def _files(h: _Harness, lines: dict[str, list[int]]) -> list[PerFileCoverage]:
    return [PerFileCoverage(abs_path=str(h.ws / rel), path=list((h.ws / rel).parts),
                            traces=[{"line": ln, "stats": {"Line": 0}} for ln in lns],
                            covered=3, coverable=10)
            for rel, lns in lines.items()]


def _run(h: _Harness, cluster: Any, lines: dict[str, list[int]],
         targets: tuple[FnTarget, ...]) -> _PhaseCOutcome:
    deps = h.deps
    object.__setattr__(deps, "nightly_rules_for_cluster", lambda: nightly_rules_for(cluster))
    return _run_phase_c(
        project_dir=h.ws, module_name="microcrate", module_dir=h.ws,
        project_context="", coverage_strategy="", attempt=4, coverage=95.0,
        covered_lines=3, files_data=_files(h, lines), hist=Task0cFileHistory(),
        ctx=_PhaseCCtx(deps=deps, current_cluster=cluster, cluster_label=cluster.name,
                       split_n=1, allow_line="", prelude_block="", prelude_names="",
                       file_stall_limit=5, retro_deep_at_stall=1, stall_context=False),
        fn_targets=targets)


def test_the_prompt_lists_never_executed_bodies_and_rules_from_the_targets(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(tmp_path, monkeypatch, src_name="lib.rs", replies=[EXACT], target=0.0, specs=())
    outcome = _run(h, _EXTRA, {"src/lib.rs": [48]}, (_HINT, _ADD))
    prompt = h.prompts[0]
    assert "Functions NO test executes" in prompt
    assert ("// NEVER EXECUTED by any test: src/lib.rs::capacity_hint — compiled only "
            "under this cluster's cfg") in prompt
    assert "  56 |     64" in prompt
    assert "// NEVER EXECUTED by any test: src/lib.rs::add — shared code" in prompt
    assert "calling every function listed as NEVER EXECUTED (`capacity_hint`, `add`)" in prompt
    assert ("Compiled ONLY under this cluster's cfg (`extra_cap` active): function(s) "
            "`src/lib.rs::capacity_hint`") in prompt
    assert ("reached by no test run under this cluster yet: function(s) `src/lib.rs::add` "
            "and 1 uncovered line(s)") in prompt
    assert "already covered" not in nightly_rules_for(_EXTRA)
    assert "stable lines are already covered" not in prompt
    # The admitted block ran: both targets it was written for are charged.
    assert outcome.blocks_admitted == 1
    assert outcome.fn_attempted == {_HINT.site, _ADD.site}


def test_a_pass_with_no_block_charges_no_attempt(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(tmp_path, monkeypatch, src_name="lib.rs", replies=[], target=0.0, specs=())
    outcome = _run(h, _EXTRA, {"src/lib.rs": [48]}, (_HINT,))
    assert len(h.prompts) == 2                      # full prompt, then the medium retry
    assert all("NEVER EXECUTED by any test: src/lib.rs::capacity_hint" in p for p in h.prompts)
    assert outcome.fn_attempted == frozenset()


def test_a_file_carrying_an_unexecuted_function_goes_first(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lines = {"src/lib.rs": [27, 28, 46, 47, 48, 50], "src/counter.rs": [14]}
    h = _Harness(tmp_path, monkeypatch, src_name="lib.rs", replies=[], target=0.0, specs=())
    shutil.copy(_MICROCRATE / "counter.rs", h.ws / "src" / "counter.rs")
    _run(h, _STOCK, lines, (_ADD,))
    assert "Target file: src/lib.rs" in h.prompts[0]
    assert any("1 unexecuted function(s) targeted first" in ln for ln in h.logs)

    # Negative control: the same queue without the target is easy-first, and
    # the one-line file opens the pass.
    tmp2 = tmp_path / "control"
    tmp2.mkdir()
    c = _Harness(tmp2, monkeypatch, src_name="lib.rs", replies=[], target=0.0, specs=())
    shutil.copy(_MICROCRATE / "counter.rs", c.ws / "src" / "counter.rs")
    _run(c, _STOCK, lines, ())
    assert "Target file: src/counter.rs" in c.prompts[0]


def test_a_target_file_with_no_uncovered_line_is_still_queued(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(tmp_path, monkeypatch, src_name="lib.rs", replies=[], target=0.0, specs=())
    outcome = _run(h, _STOCK, {}, (_ADD,))
    assert h.prompts and "NEVER EXECUTED by any test: src/lib.rs::add" in h.prompts[0]
    assert outcome.kind == "continue"


def test_the_scope_rule_is_rendered_from_targets_only() -> None:
    assert target_scope_rule(None, shared_fns=["a"]) == ""
    one_world = plan_clusters({}, {}, cargo_features=[], default_features=[],
                              feature_gate_cfgs=[])[0]
    assert target_scope_rule(one_world, shared_fns=["src/a.rs::f"]) == ""
    assert target_scope_rule(_EXTRA) == ""
    shared_only = target_scope_rule(_EXTRA, shared_fns=["src/a.rs::f"], shared_lines=2)
    assert "Shared code every cluster compiles" in shared_only
    assert "Compiled ONLY" not in shared_only and "already covered" not in shared_only
    stock = target_scope_rule(_STOCK, gated_lines=3)
    assert "(`extra_cap` NOT active): 3 uncovered line(s)" in stock
    # The baked rule keeps the cfg facts, and no target-scope claim.
    assert "You MAY use any symbols gated behind `#[cfg(extra_cap)]`" in _EXTRA.prompt_nightly_rule
    assert "Only write tests for lines inside" not in _EXTRA.prompt_nightly_rule
