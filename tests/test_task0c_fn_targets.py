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
  covered; a pass with no block charges nothing;
- every claim a prompt makes about what it targets — the status sentence's
  counts, the cluster scope rule's function lists, the call instruction — is
  rendered from the targets that prompt itself shows, split-and-merge subsets
  included (negative control: the file-level claims reused, as run
  3044-3c9670bf sent them on 2026-09-17).
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pipeline_core.application.tasks import task0c as task0c_mod
from pipeline_core.application.tasks.task0c import (
    Task0cFileHistory,
    _PhaseCCtx,
    _PhaseCOutcome,
    _run_phase_c,
)
from pipeline_core.domain.cluster_plan import nightly_rules_for, plan_clusters, target_scope_rule
from pipeline_core.domain.coverage import PerFileCoverage
from pipeline_core.domain.fn_execution import FnTarget
from pipeline_core.domain.fn_routes import FnRoutes, GatedDispatch, PublicRoute, TypeFact, WorldFacts
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
         targets: tuple[FnTarget, ...], *, split_n: int = 1,
         hist: Task0cFileHistory | None = None,
         facts: WorldFacts | None = None) -> _PhaseCOutcome:
    deps = h.deps
    object.__setattr__(deps, "nightly_rules_for_cluster", lambda: nightly_rules_for(cluster))
    return _run_phase_c(
        project_dir=h.ws, module_name="microcrate", module_dir=h.ws,
        project_context="", coverage_strategy="", attempt=4, coverage=95.0,
        covered_lines=3, files_data=_files(h, lines),
        hist=hist if hist is not None else Task0cFileHistory(),
        ctx=_PhaseCCtx(deps=deps, current_cluster=cluster, cluster_label=cluster.name,
                       split_n=split_n, allow_line="", prelude_block="", prelude_names="",
                       file_stall_limit=5, retro_deep_at_stall=1, stall_context=False,
                       facts=facts),
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
    # The whole-file prompt renders the whole file's targets, so it keeps the
    # claim a subset may not make.
    assert ("The 1 line(s) in fn_bodies below and the 2 function(s) listed as never "
            "executed are the ONLY remaining targets in this file — all other lines "
            "are already covered.") in prompt
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


def _claimed_and_shown(prompt: str) -> tuple[int, int]:
    """(functions the status sentence claims are listed, bodies it lists)."""
    m = re.search(r"[Tt]he (\d+) function\(s\) listed as never executed", prompt)
    return (int(m.group(1)) if m else 0,
            prompt.count("// NEVER EXECUTED by any test:"))


def _split_run(h: _Harness) -> tuple[str, str]:
    """One file, two targets, three uncovered lines, partitioned so each
    subset request carries exactly one never-executed function: `add`'s body
    line alone, then `capacity_hint`'s body line beside a plain uncovered
    line. Returns the two subset prompts in partition order."""
    object.__setattr__(h.deps, "partition_uncovered_lines",
                       lambda _text, _lines, _n: [{27}, {48, 55}])
    _run(h, _EXTRA, {"src/lib.rs": [27, 48, 55]}, (_ADD, _HINT), split_n=2)
    assert len(h.parallel_prompts) == 2
    return h.parallel_prompts[0], h.parallel_prompts[1]


def test_a_subset_prompt_claims_only_the_targets_it_renders(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    add_p, hint_p = _split_run(
        _Harness(tmp_path, monkeypatch, src_name="lib.rs", replies=[], target=0.0,
                 specs=(), split_n=2))
    assert _claimed_and_shown(add_p) == (1, 1)
    assert _claimed_and_shown(hint_p) == (1, 1)

    # The subset holding only `add`'s body: one function, no line target, and a
    # scope rule that knows nothing of the gated function it does not show.
    assert ("The 1 function(s) listed as never executed are the ONLY targets of "
            "this request") in add_p
    assert "// NEVER EXECUTED by any test: src/lib.rs::add" in add_p
    assert "calling every function listed as NEVER EXECUTED (`add`)" in add_p
    assert "`src/lib.rs::capacity_hint`" not in add_p
    assert "Compiled ONLY under this cluster's cfg" not in add_p

    # The other subset: its own line target, its own gated function.
    assert ("The 1 line(s) in fn_bodies below and the 1 function(s) listed as "
            "never executed are the ONLY targets of this request") in hint_p
    assert ("Compiled ONLY under this cluster's cfg (`extra_cap` active): "
            "function(s) `src/lib.rs::capacity_hint`") in hint_p
    assert "`src/lib.rs::add`" not in hint_p

    # Neither may tell a subset the file's other uncovered lines are covered:
    # they are the concurrent request's targets.
    assert all("all other lines are already covered" not in _p
               for _p in (add_p, hint_p))


def test_negative_control_the_file_level_claims_reused_contradict_the_subset(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-fix behaviour injected: both claims built once from the file's
    totals — two functions, one line — and handed to every subset."""
    _status, _rule = task0c_mod._coverage_status_line, task0c_mod.target_scope_rule
    monkeypatch.setattr(task0c_mod, "_coverage_status_line",
                        lambda cov, **_kw: _status(cov, line_targets=1,
                                                   fn_targets=(_HINT, _ADD),
                                                   whole_file=True))
    monkeypatch.setattr(task0c_mod, "target_scope_rule",
                        lambda cluster, **_kw: _rule(
                            cluster, gated_fns=[_HINT.label], shared_fns=[_ADD.label],
                            gated_lines=0, shared_lines=1))
    add_p, _hint_p = _split_run(
        _Harness(tmp_path, monkeypatch, src_name="lib.rs", replies=[], target=0.0,
                 specs=(), split_n=2))
    # Claims two never-executed functions over a prompt that renders one, tells
    # the subset the rest of the file is covered, and its scope rule names a
    # cfg-gated function whose body is in the other request.
    assert _claimed_and_shown(add_p) == (2, 1)
    assert "all other lines are already covered" in add_p
    assert "`src/lib.rs::capacity_hint`" in add_p


def test_a_block_that_does_not_compile_is_still_an_attempt_at_its_function(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The nightly `Mut::clone` defect: the budget was charged only when the
    block compiled, so a function whose blocks kept failing to compile never
    spent its attempts. The block was written for the function; it is an
    attempt, and the reason it did not reach the function is recorded."""
    h = _Harness(tmp_path, monkeypatch, src_name="lib.rs", replies=[EXACT], target=0.0, specs=())
    object.__setattr__(h.deps, "subprocess", SimpleNamespace(
        run=lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="error[E0277]")))
    outcome = _run(h, _EXTRA, {"src/lib.rs": [48]}, (_HINT,))
    assert outcome.blocks_admitted == 0
    assert outcome.fn_attempted == {_HINT.site}
    assert dict(outcome.fn_outcomes) == {_HINT.site: "compile_failed"}


_NO_GAIN = "FUNCTION {name}: UNREACHABLE — dead code, add an ignore pragma"


def _stalled_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str,
                  targets: tuple[FnTarget, ...]) -> tuple[_Harness, list[_PhaseCOutcome], str]:
    """Two passes whose blocks add no line (quick coverage stays at the seed),
    so the file stalls on the second and the retrospective runs; it classifies
    `name` UNREACHABLE. Returns the harness, the outcomes and the source text
    before the passes."""
    from pipeline_core.domain.refine import parse_retrospective_categories
    h = _Harness(tmp_path, monkeypatch, src_name="lib.rs",
                 replies=[EXACT, EXACT, _NO_GAIN.format(name=name)], target=0.0, specs=())
    object.__setattr__(h.deps, "quick_file_coverage", lambda pd, m, rel: (3, 10))
    object.__setattr__(h.deps, "parse_retrospective_categories", parse_retrospective_categories)
    before = (h.ws / "src" / "lib.rs").read_text(encoding="utf-8")
    hist = Task0cFileHistory()
    outs = [_run(h, _EXTRA, {"src/lib.rs": [48]}, targets, hist=hist) for _ in range(2)]
    return h, outs, before


def test_a_retrospective_verdict_writes_nothing_to_the_source(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """anyhow's `format_err`: the retrospective said UNREACHABLE for an
    executed function's `else` arm and a pragma hid line 690, which
    `anyhow!("x {var}")` executes. A verdict is evidence on the record and text
    for the next prompt; it never edits the source."""
    h, outs, before = _stalled_pass(tmp_path, monkeypatch, "capacity_hint", (_HINT,))
    assert any(ln.startswith("  [microcrate] Retrospective:") for ln in h.logs)
    assert (h.ws / "src" / "lib.rs").read_text(encoding="utf-8") == before
    assert not any("Targeted pragma" in ln or "[targeted-pragma]" in ln for ln in h.logs)
    # The verdict is on the record for the pending function.
    assert (_HINT.site, "UNREACHABLE") in outs[-1].fn_verdicts


def test_negative_control_a_verdict_on_a_function_not_pending_writes_nothing_either(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h, outs, before = _stalled_pass(tmp_path / "ctl", monkeypatch, "target", ())
    assert (h.ws / "src" / "lib.rs").read_text(encoding="utf-8") == before
    assert outs[-1].fn_verdicts == ()


def test_a_trait_impl_target_is_told_the_route_that_reaches_it() -> None:
    src = "impl<E> Debug for ErrorImpl<E> {\n    fn fmt(&self) {}\n}\n"
    dbg = FnTarget(FnSite("src/error.rs", "fmt", "impl < E > Debug for ErrorImpl < E >"), 2, 2, False)
    inherent = FnTarget(FnSite("src/error.rs", "new", "impl Error"), 2, 2, False)
    out = task0c_mod._render_unexecuted_fns(src, (dbg,))
    assert "Reached through the trait, not by a direct call: `{:?}`, `{:#?}`" in out
    assert "Reached through the trait" not in task0c_mod._render_unexecuted_fns(src, (inherent,))


# --- the world's routes and public index reach the prompt ----------------------

_ROUTED = WorldFacts(
    "extra_cap",
    routes={"src/lib.rs::capacity_hint": FnRoutes(
        (PublicRoute("`<Option<T> as Context<T>>::context`", ("`Error::from_display`",),
                     "builds `DisplayError`"),),
        (GatedDispatch("`<Error as fmt::Debug>::fmt`", "<dyn Error as Debug>::fmt", "alternate"),))},
    unnameable=(TypeFact("MathError", "src/lib.rs",
                         (PublicRoute("`checked_div`", (), "builds `MathError`"),)),),
    item_names={"src/lib.rs": frozenset({"add", "checked_div", "capacity_hint"})})


def test_a_routed_target_is_told_its_public_route_instead_of_the_generic_text(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(tmp_path, monkeypatch, src_name="lib.rs", replies=[EXACT], target=0.0, specs=())
    _run(h, _EXTRA, {"src/lib.rs": [48]}, (_HINT, _ADD), facts=_ROUTED)
    prompt = h.prompts[0]
    block = prompt.split("// NEVER EXECUTED by any test: src/lib.rs::capacity_hint", 1)[1]
    block = block.split("// NEVER EXECUTED by any test:", 1)[0]
    assert ("// Reached from the public API (callers in this crate's MIR): "
            "`<Option<T> as Context<T>>::context` → `Error::from_display` (builds `DisplayError`)") in block
    assert "only when `f.alternate()` is true (`{:#?}`)" in block
    # Control: the unrouted target in the same prompt carries no route line.
    other = prompt.split("// NEVER EXECUTED by any test: src/lib.rs::add", 1)[1]
    assert "Reached from the public API" not in other.split("```", 1)[0]


_API = "pub fn add(a: i32, b: i32) -> i32\npub enum MathError { DivByZero }\npub fn checked_div(a: i32, b: i32)"


def test_the_api_section_lists_only_what_the_public_index_names(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(tmp_path, monkeypatch, src_name="lib.rs", replies=[EXACT], target=0.0, specs=())
    object.__setattr__(h.deps, "extract_public_symbols", lambda _t: _API)
    _run(h, _EXTRA, {"src/lib.rs": [48]}, (_HINT,), facts=WorldFacts(
        "extra_cap", unnameable=_ROUTED.unnameable,
        item_names={"src/lib.rs": frozenset({"add", "capacity_hint", "checked_div"})}))
    api = h.prompts[0].split("Public API of `src/lib.rs`", 1)[1].split("```", 2)[1]
    assert "pub fn add" in api
    assert "pub enum MathError" not in api
    assert ("// Internal, NOT nameable from tests/ (pub in a private module; do not `use` it): "
            "`MathError`; built by `checked_div` (builds `MathError`)") in api


def test_negative_control_without_a_reading_the_api_section_is_unchanged(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(tmp_path, monkeypatch, src_name="lib.rs", replies=[EXACT], target=0.0, specs=())
    object.__setattr__(h.deps, "extract_public_symbols", lambda _t: _API)
    _run(h, _EXTRA, {"src/lib.rs": [48]}, (_HINT,))
    api = h.prompts[0].split("Public API of `src/lib.rs`", 1)[1].split("```", 2)[1]
    assert "pub enum MathError" in api and "NOT nameable" not in api
    assert "Reached from the public API" not in h.prompts[0]


# --- a committed block aimed at a still-unexecuted function -----------------------

def test_the_next_prompt_states_that_a_committed_block_left_its_target_at_zero(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(tmp_path, monkeypatch, src_name="lib.rs", replies=[EXACT, EXACT], target=0.0, specs=())
    hist = Task0cFileHistory()
    first = _run(h, _EXTRA, {"src/lib.rs": [48]}, (_HINT,), hist=hist)
    assert first.fn_outcomes == ((_HINT.site, "committed"),)
    assert "still has 0 executions" not in h.prompts[0]
    # The next measurement still counts it unexecuted: it is a target again.
    _run(h, _EXTRA, {"src/lib.rs": [48]}, (_HINT,), hist=hist)
    prompt = h.prompts[-1]
    assert "(a fact, not a hypothesis)" in prompt
    assert re.search(r"- `tests/lib_gen_\d+\.rs` ran; `src/lib\.rs::capacity_hint` still has 0 executions\.",
                     prompt), prompt[:3000]
    # No cause is claimed for it.
    assert "does not build" not in prompt


def test_negative_control_an_executed_function_gets_no_zero_line(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(tmp_path, monkeypatch, src_name="lib.rs", replies=[EXACT, EXACT], target=0.0, specs=())
    hist = Task0cFileHistory()
    _run(h, _EXTRA, {"src/lib.rs": [48]}, (_HINT,), hist=hist)
    # Executed now: not among the next pass's targets.
    _run(h, _EXTRA, {"src/lib.rs": [48]}, (_ADD,), hist=hist)
    assert "still has 0 executions" not in h.prompts[-1]
