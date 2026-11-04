"""A slice the packer emits is a slice whose session can send its turn 0.

Run `3203804-8c6a0066` (2026-11-03, cluster `stable`, fills 3-5): 19 turn 0
builds, 13 with nothing sent because the turn 0 was over 132,000 chars at
the narrowest rendering. The packer had admitted each on an estimate of the
PLANNED units (121,121 to 130,369 charged); the session opened on its write
set (6 -> 14, 12 -> 17, 14 -> 22 units) and carried sections the estimate
did not price. Fill 5 opened four sessions under a cap of 4, three of them
could not send a turn 0, and three sessions of 4, 3 and 2 units were
deferred `kind_cap`.

Claims (invented shapes, no crate names):
- the packer decides on the length of the turn 0 the session builder
  builds, write set and every section included: a slice whose growth
  pushes it over is split by tests, and a single test whose own built turn
  0 is over at every step is reported, never planned;
- the rendering ladder narrows past the file threshold: target tests only,
  then a lower TYPES_IN_SCOPE cap, then one optional section at a time
  (shared background, then the previous session's record, then this
  attempt's evidence), and last the probe's observed lines. A packing no
  file threshold fits opens at the step that fits;
- the narrowing steps keep what a target needs: the test-file cut keeps
  the target test and every helper, and the types section prints a block
  once and cuts its ranking from the end;
- a session that cannot open its prompt is its own ledger outcome and
  gives its kind-cap slot to the next session in the same fill.
Each carries the negative control: the estimate as the verdict, the ladder
without its non-file steps, the release disabled.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from pipeline_core.adapters.route_ledger_store import RouteLedgerStore
from pipeline_core.application.reconstruct import fill_reading
from pipeline_core.application.reconstruct import fill_scheduler as fs
from pipeline_core.application.reconstruct.fill_routes import RouteRunReport
from pipeline_core.domain.fill_schedule import (
    RouteKind,
    RouteRun,
    RunClock,
    RunOutcome,
    ScheduledRun,
    release_capped,
)
from pipeline_core.domain.models import FnLocator, FnSite
from pipeline_core.domain.slice_session import (
    Rendering,
    SessionSlice,
    Turn0Fit,
    bound_observed,
    fit_turn0,
    plan_slices,
    quoted_test_for_targets,
    rendering_ladder,
    slice_plan_gaps,
)

_HUB = FnSite("src/hub.rs", "build", "impl Hub")
_A = FnSite("src/a.rs", "f", "impl A")
_B = FnSite("src/b.rs", "g", "impl B")
_C = FnSite("src/c.rs", "h", "impl C")
#: What the write set adds when a unit is editable: the table slots its
#: builder fills (the installable complement), invented.
_GROWS = {_A: {FnSite("src/a.rs", "slot_a1", ""), FnSite("src/a.rs", "slot_a2", "")},
          _B: {FnSite("src/b.rs", "slot_b1", ""), FnSite("src/b.rs", "slot_b2", "")}}
_PER_UNIT = 10_000
_FIXED = 20_000
_CAP = 80_000


def _write_set(units: Sequence[FnSite]) -> set[FnSite]:
    out = set(units)
    for u in units:
        out |= _GROWS.get(u, set())
    return out


def _built(sl: SessionSlice) -> int:
    """The builder's length: the session opens on the write set."""
    return _FIXED + _PER_UNIT * len(_write_set(sl.units))


def _estimate(sl: SessionSlice) -> int:
    """The pre-fix charge: the planned units only."""
    return _FIXED + _PER_UNIT * len(sl.units)


def _fit(length: Callable[[SessionSlice], int]) -> Turn0Fit:
    return Turn0Fit(length=length, ladder=(Rendering(36_000), Rendering(18_000)),
                    capacity=_CAP)


_SLICES = {"t_a": {_HUB, _A}, "t_b": {_HUB, _B}, "t_c": {_HUB, _C}}


def test_every_emitted_slice_opens_when_the_packer_reads_the_built_turn0() -> None:
    plans = plan_slices(_SLICES, fit=_fit(_built))
    assert plans, "the slices fit apart"
    assert all(_built(p) <= _CAP for p in plans)
    # Split by tests: no test is cut out of its own slice.
    for p in plans:
        for t in p.tests:
            assert _SLICES[t] <= set(p.units)


def test_negative_control_the_estimate_admits_a_slice_that_cannot_open() -> None:
    plans = plan_slices(_SLICES, fit=_fit(_estimate))
    assert any(_built(p) > _CAP for p in plans)


def test_a_single_test_over_at_every_step_is_reported_not_planned() -> None:
    # Planned: 2 units, estimate 40,000. Built: the write set grows it to
    # 9 units, 110,000, at every rendering.
    grown = {FnSite("src/a.rs", f"slot_{n}", "") for n in range(7)}

    def built(sl: SessionSlice) -> int:
        return _FIXED + _PER_UNIT * (len(sl.units) + len(grown))

    slices = {"t_wide": {_HUB, _A}}
    assert plan_slices(slices, fit=_fit(built)) == []
    gaps = slice_plan_gaps(slices, fit=_fit(built))
    assert gaps.over_chars_single == 1 and gaps.over_cap == 0
    assert "SLICE_SESSION_TURN0_CHARS" in gaps.log_clause()
    # Negative control: the estimate plans it.
    assert [p.tests for p in plan_slices(slices, fit=_fit(_estimate))] == [("t_wide",)]


def test_sections_the_estimate_does_not_price_refuse_the_slice() -> None:
    # Same units planned and opened; the builder carries 50,000 of sections
    # (notes, probe lines, blueprint) the estimate never charged.
    def built(sl: SessionSlice) -> int:
        return _estimate(sl) + 50_000

    slices = {"t_a": {_HUB, _A}}
    assert plan_slices(slices, fit=_fit(built)) == []
    assert [p.tests for p in plan_slices(slices, fit=_fit(_estimate))] == [("t_a",)]


# --- the ladder --------------------------------------------------------------

def test_the_ladder_narrows_files_then_tests_then_types_then_context() -> None:
    from dataclasses import replace

    from pipeline_core.domain.slice_session import CONTEXT_DROPS
    ladder = rendering_ladder((36_000, 18_000, 9_000), 96_000)
    head = (
        Rendering(36_000), Rendering(18_000), Rendering(9_000),
        Rendering(9_000, tests_cut=True),
        Rendering(9_000, True, 48_000), Rendering(9_000, True, 24_000),
        Rendering(9_000, True, 12_000))
    tail = tuple(replace(head[-1], context_drops=n)
                 for n in range(1, len(CONTEXT_DROPS) + 2))
    assert ladder == head + tail
    assert ladder[-1].omits_observed and not ladder[-2].omits_observed


def _sections(sl: SessionSlice) -> int:
    """A turn 0 whose files are small and whose types and test quotes are
    not: files 30,000 at the widest, 10,000 narrowest; quoted tests 30,000,
    5,000 cut; types 40,000, or the cap."""
    files = 30_000 if sl.file_chars >= 36_000 else 10_000
    tests = 5_000 if sl.tests_cut else 30_000
    types = min(40_000, sl.types_cap) if sl.types_cap else 40_000
    return 10_000 + files + tests + types


def test_a_packing_no_file_threshold_fits_opens_at_a_later_step() -> None:
    ladder = rendering_ladder((36_000, 18_000), 96_000)
    fit = Turn0Fit(length=_sections, ladder=ladder, capacity=55_000)
    (p,) = plan_slices({"t_a": {_A}}, fit=fit)
    assert p.rendering == Rendering(18_000, True, 24_000)
    assert _sections(p) <= 55_000
    # Negative control: the file-threshold ladder alone refuses it.
    files_only = Turn0Fit(length=_sections, ladder=ladder[:2], capacity=55_000)
    assert plan_slices({"t_a": {_A}}, fit=files_only) == []


def test_the_session_rebuilds_at_later_steps_on_the_tree_it_opens_on() -> None:
    ladder = rendering_ladder((36_000, 18_000), 96_000)
    seen: list[Rendering] = []

    def build(r: Rendering) -> str:
        seen.append(r)
        return "x" * _sections(SessionSlice(tests=("t",), units=(_A,)).at(r))

    text, used = fit_turn0(build, Rendering(18_000, tests_cut=True), ladder, 55_000)
    assert used == Rendering(18_000, True, 24_000) and len(text) <= 55_000
    assert seen[0] == Rendering(18_000, tests_cut=True), "starts where it was planned"
    assert fit_turn0(build, ladder[0], ladder, 1_000) is None


def test_a_shared_section_the_file_steps_cannot_cut_opens_when_it_is_dropped() -> None:
    """A turn 0 that is over at every file, test-cut and types step because
    one shared section dominates fits at the step that drops that section.
    The same ladder stopped before any context drop refuses it."""
    from pipeline_core.domain.slice_session import omitted_names
    ladder = rendering_ladder((36_000, 18_000), 96_000)
    spec = "REPOSITORY_SPECIFICATIONS"
    blob = 40_000

    def length(sl: SessionSlice) -> int:
        base = _sections(sl)
        if spec not in omitted_names(sl.context_drops):
            base += blob
        return base

    fit = Turn0Fit(length=length, ladder=ladder, capacity=50_000)
    (p,) = plan_slices({"t_a": {_A}}, fit=fit)
    assert spec in omitted_names(p.context_drops)
    assert length(p) <= 50_000
    # The step just before that drop is still over: the section is what
    # the later step removes, not a file threshold.
    before = next(r for r in ladder if r.context_drops == p.context_drops - 1)
    assert length(SessionSlice(tests=("t_a",), units=(_A,)).at(before)) > 50_000
    no_context = Turn0Fit(
        length=length,
        ladder=tuple(r for r in ladder if r.context_drops == 0),
        capacity=50_000)
    assert plan_slices({"t_a": {_A}}, fit=no_context) == []


def test_failing_tests_are_tagged_and_every_other_section_is_a_ladder_drop(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The five largest sections are how the next step is chosen. The
    panic quote and the probe reserve have to be a section, and every
    other tag the builder emits has to be a step it can drop. A tag in
    neither set has no step."""
    import inspect
    import re

    import pipeline_core.prompts.slice_session as sp
    from pipeline_core.domain.models import FnRecord
    from pipeline_core.domain.slice_session import (
        CONTEXT_DROPS,
        KEPT_TAGS,
        evidence_for_step,
        largest_sections,
        omitted_names,
    )
    rec = FnRecord(file="src/a.rs", fn_name="f", container="impl A",
                   sig_text="fn f(&self)", body_text="unimplemented!()")
    sl = SessionSlice(tests=("t_a",), units=(_A,))
    monkeypatch.setattr(sp, "repo_spec_section", lambda *_a, **_k: "SPEC-BODY")
    monkeypatch.setattr(sp, "promoted_error_rules", lambda *_a, **_k: "RECUR-BODY")

    class _Gotchas:
        def gotchas_summary(self) -> str:
            return "GOTCHA-BODY"

    monkeypatch.setattr(sp, "language_driver", lambda: _Gotchas())
    quote = "assertion failed: left 1 right 2"
    observed = "observed: left=1\n" + ("v" * 20_000)
    common = {
        "module_name": "m", "cluster_name": "c", "slice_": sl,
        "records": {_A: rec}, "all_records": [rec],
        "file_texts": {"src/a.rs": "fn f(&self) { unimplemented!() }"},
        "target_evidence": {"t_a": evidence_for_step(
            quote, observed, omit_observed=False)},
        "test_blocks": [("tests/t.rs", "fn t_a() {}")],
        "types_section": "struct Named;",
        "bound_methods": "BOUND-BODY", "invariants": "INVAR-BODY",
        "macros": "MACRO-BODY",
        "notes": "NOTES-BODY", "prior_regression": "REGR-BODY",
        "blueprint": "BLUE-BODY",
        "crash_evidence": "CRASH-BODY", "behaviour_must": "BEHAVE-BODY",
        "unit_notes": "UNITS-BODY",
    }
    full = sp.build_slice_session_prompt(**common)
    named = set(re.findall(
        r'(?:_opt|tag_block)\(\s*"([A-Z][A-Z0-9_]*)"',
        inspect.getsource(sp.build_slice_session_prompt)))
    assert named <= (KEPT_TAGS | set(CONTEXT_DROPS))
    assert {"FAILING_TESTS", "TYPES_IN_SCOPE"} <= named
    closed = {m.group(1) for m in re.finditer(
        r"<([A-Z][A-Z0-9_]*)(?:\s[^>\n]*)?>", full)
        if f"</{m.group(1)}>" in full}
    # FN and NOTES are the reply format in the protocol. A `<T>` inside an
    # impl header has no closer and is not a section.
    assert closed - {"FN", "NOTES"} == KEPT_TAGS | set(CONTEXT_DROPS)
    assert largest_sections(full, n=1)[0][0] == "FAILING_TESTS"
    assert quote in full and "observed: left=1" in full
    # Dropping through the repository specification removes it and every
    # section ahead of it, and leaves this attempt's evidence.
    through_spec = omitted_names(CONTEXT_DROPS.index("REPOSITORY_SPECIFICATIONS") + 1)
    dropped = sp.build_slice_session_prompt(**common, omit=through_spec)
    assert "SPEC-BODY" not in dropped and "MACRO-BODY" not in dropped
    assert "CRASH-BODY" in dropped and "BEHAVE-BODY" in dropped and quote in dropped
    # The last step keeps the panic quote and drops the probe reserve.
    bare = sp.build_slice_session_prompt(
        **{**common, "target_evidence": {"t_a": evidence_for_step(
            quote, observed, omit_observed=True)}},
        omit=CONTEXT_DROPS)
    assert quote in bare and "observed: left=1" not in bare
    assert "<FAILING_TESTS>" in bare and "<CRASH_EVIDENCE>" not in bare


_TEST_FILE = """use crate_x::Thing;

fn helper(n: u8) -> Thing {
    Thing::new(n)
}

macro_rules! check {
    ($e:expr) => { assert!($e) };
}

#[test]
fn t_target() {
    check!(helper(1).ok());
}

/// a sibling
#[test]
fn t_sibling() {
    assert_eq!(helper(2).value(), 2);
}

#[test]
#[should_panic]
fn t_other() {
    helper(3).boom();
}
"""


def test_the_test_cut_keeps_the_target_and_every_helper() -> None:
    cut = quoted_test_for_targets(_TEST_FILE, ["tests::t_target"])
    assert "fn t_target()" in cut and "fn helper(" in cut
    assert "macro_rules! check" in cut and "use crate_x::Thing;" in cut
    assert "t_sibling" not in cut and "t_other" not in cut
    assert "/// a sibling" not in cut
    assert len(cut) < len(_TEST_FILE)
    # A file holding none of the targets, or only targets, is quoted whole.
    assert quoted_test_for_targets(_TEST_FILE, ["t_absent"]) == _TEST_FILE
    whole = ["t_target", "t_sibling", "t_other"]
    assert quoted_test_for_targets(_TEST_FILE, whole) == _TEST_FILE


def test_observed_lines_stay_within_their_bound() -> None:
    lines = "\n".join(f"observed at line {n}: v = {'x' * 90}" for n in range(200))
    out = bound_observed(lines, 2_000)
    assert len(out) <= 2_000 and out.endswith("not shown")
    assert out.splitlines()[0] == lines.splitlines()[0]
    assert bound_observed("short", 2_000) == "short"
    one = bound_observed("y" * 9_000, 500)
    assert len(one) <= 500 and one.endswith("…")


# --- the cap ---------------------------------------------------------------

def _session_runs(n: int) -> list[RouteRun]:
    return [RouteRun(kind=RouteKind.SESSION, label=f"session[{k}]",
                     targets=frozenset({FnLocator(f"src/s{k}.rs", "u")}),
                     target_tests=frozenset({f"t_{k}"}),
                     files=(f"src/s{k}.rs",), bodies_sha=f"b{k}")
            for k in range(n)]


def test_release_capped_gives_back_the_next_capped_run_of_that_kind() -> None:
    a, b, c = _session_runs(3)
    rows = [ScheduledRun(run=a, rank=0, weight=0, reason="kind_cap", level=0,
                         deferred=True, prior_reason="never_served", position=1),
            ScheduledRun(run=b, rank=0, weight=0, reason="pair_wait", level=0,
                         deferred=True, position=2),
            ScheduledRun(run=c, rank=0, weight=0, reason="kind_cap", level=0,
                         deferred=True, prior_reason="served", position=3)]
    first = release_capped(rows, RouteKind.SESSION, ())
    assert first is not None and first.run.key == a.key
    assert not first.deferred and first.reason == "never_served"
    second = release_capped(rows, RouteKind.SESSION, {a.key})
    assert second is not None and second.run.key == c.key
    assert release_capped(rows, RouteKind.SESSION, {a.key, c.key}) is None
    assert release_capped(rows, RouteKind.PAIR, ()) is None


class _Sessions:
    """A session route whose first opened run cannot send its turn 0."""

    kind = RouteKind.SESSION
    parallel = False

    def __init__(self, runs: list[RouteRun], calls: list[str]) -> None:
        self._runs = runs
        self._calls = calls

    def plan(self) -> Sequence[RouteRun]:
        return list(self._runs)

    def run(self, run: RouteRun, clock: RunClock) -> RouteRunReport:
        self._calls.append(run.label)
        if len(self._calls) == 1:
            return RouteRunReport(empty=True, unopenable=True)
        return RouteRunReport(model_calls=1)


def _fill(tmp_path: Path) -> tuple[list[str], fs.FillExecution, list[str]]:
    fill_reading.reset_last_comparison()
    calls: list[str] = []
    log: list[str] = []
    out = fs.execute_fill(
        [_Sessions(_session_runs(4), calls)], hollow={},
        bodies_sha=lambda _t: "b",
        deps=fs.FillExecutionDeps(
            log=log.append, emit=lambda *_a, **_k: None,
            check_stop=lambda _c: None, ledger=RouteLedgerStore(tmp_path),
            project="p", cluster="c", run_id="r", iteration=5, oracle_fp="o",
            max_per_kind={RouteKind.SESSION: 2}))
    return calls, out, log


def test_an_unopenable_session_gives_its_slot_to_the_next(tmp_path: Path) -> None:
    calls, out, log = _fill(tmp_path)
    assert len(calls) == 3, "one unopenable, then the cap's two"
    outcomes = [r.outcome for r in out.rows if r.label in calls]
    assert outcomes.count(RunOutcome.UNOPENABLE) == 1
    deferred = [r for r in out.rows if r.outcome is RunOutcome.DEFERRED]
    assert len(deferred) == 1 and deferred[0].reason == "kind_cap"
    assert any("could not open its prompt" in ln for ln in log)


def test_negative_control_without_the_release_the_slot_is_spent(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fs, "release_capped", lambda *_a: None)
    calls, out, _log = _fill(tmp_path)
    assert len(calls) == 2
    assert sum(1 for r in out.rows if r.outcome is RunOutcome.DEFERRED) == 2


def test_no_dispatch_outcomes_are_one_set() -> None:
    assert RunOutcome.UNOPENABLE.opened_nothing
    assert RunOutcome.EMPTY.opened_nothing
    assert not any(o.opened_nothing for o in RunOutcome
                   if o not in (RunOutcome.EMPTY, RunOutcome.UNOPENABLE))


# --- one TYPES_IN_SCOPE for a session's files -------------------------------

def _type(name: str, file: str) -> object:
    from pipeline_core.domain.models import TypeRecord
    return TypeRecord(file=file, kind="struct", name=name,
                      definition=("/// " + "doc line of the type. " * 8
                                  + f"\npub struct {name} {{ x: u8 }}"),
                      is_pub=True)


def _ctx(types_section: int = 100_000) -> object:
    from types import SimpleNamespace

    from pipeline_core.application.reconstruct.fix_context import (
        FixContextAssembler,
    )
    return FixContextAssembler(
        budgets=SimpleNamespace(types_count=40, types_section=types_section),  # type: ignore[arg-type]
        filter_notes=lambda *_a, **_k: "", guard=lambda *_a, **_k: None)


def test_a_type_two_files_name_is_printed_once() -> None:
    from pipeline_core.application.reconstruct.fix_context import (
        FixContextAssembler,
        TypesPart,
    )
    shared = _type("Shared", "src/types.rs")
    a_only = _type("OnlyA", "src/types.rs")
    b_only = _type("OnlyB", "src/types.rs")
    recs = [shared, a_only, b_only]
    ctx = _ctx()
    parts = [TypesPart("src/a.rs", "fn f(s: Shared, a: OnlyA)", "", ""),
             TypesPart("src/b.rs", "fn g(s: Shared, b: OnlyB)", "", "")]
    merged = FixContextAssembler.types_section_for_files(ctx, parts, recs)  # type: ignore[arg-type]
    assert merged.count("struct Shared") == 1
    assert "struct OnlyA" in merged and "struct OnlyB" in merged
    # Negative control: the per-file sections joined, as the session did.
    joined = "\n\n".join(
        FixContextAssembler.types_section_for_fix(  # type: ignore[arg-type]
            ctx, p.sig_text, p.body_text, "", p.container, "", recs,
            file_rel=p.file_rel) for p in parts)
    assert joined.count("struct Shared") == 2
    assert len(merged) < len(joined)


def test_a_lower_types_cap_cuts_the_merged_ranking_from_the_end() -> None:
    from pipeline_core.application.reconstruct.fix_context import (
        FixContextAssembler,
        TypesPart,
    )
    named = _type("Named", "src/types.rs")
    elsewhere = [_type(f"Body{i}", "src/types.rs") for i in range(6)]
    recs = [*elsewhere, named]
    ctx = _ctx()
    body = " ".join(f"Body{i}::new()" for i in range(6))
    parts = [TypesPart("src/a.rs", "fn f(n: Named)", body, "")]
    full = FixContextAssembler.types_section_for_files(ctx, parts, recs)  # type: ignore[arg-type]
    cut = FixContextAssembler.types_section_for_files(  # type: ignore[arg-type]
        ctx, parts, recs, cap=len(full) // 3)
    assert len(cut) <= len(full) // 3
    assert "struct Named" in cut, "the signature's type ranks first"
    assert "struct Body5" in full and "struct Body5" not in cut


def test_an_unopenable_turn0_is_reported_by_its_largest_sections() -> None:
    from pipeline_core.domain.slice_session import largest_sections
    text = ("<MACROS>\n" + "m" * 300 + "\n</MACROS>\n"
            '<FILE path="src/a.rs" view="part">\n' + "a" * 900 + "\n</FILE>\n"
            '<FILE path="src/b.rs" view="whole">\n' + "b" * 100 + "\n</FILE>\n"
            "<TYPES_IN_SCOPE>\n" + "t" * 600 + "\n</TYPES_IN_SCOPE>\n")
    got = largest_sections(text, n=3)
    assert [n for n, _ in got] == ["FILE src/a.rs", "TYPES_IN_SCOPE", "MACROS"]
    assert got[0][1] >= 900


def test_the_gaps_name_the_tests_no_step_opens() -> None:
    grown = {FnSite("src/a.rs", f"slot_{n}", "") for n in range(7)}

    def built(sl: SessionSlice) -> int:
        extra = len(grown) if "t_wide" in sl.tests else 0
        return _FIXED + _PER_UNIT * (len(sl.units) + extra)

    gaps = slice_plan_gaps({"t_wide": {_HUB, _A}, "t_ok": {_C}}, fit=_fit(built))
    assert gaps.over_chars_tests == ("t_wide",)
