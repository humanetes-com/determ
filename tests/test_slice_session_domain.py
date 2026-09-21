"""The red-slice session's pure half: which failing tests form one session,
how a reply's bodies route to the slice's units, and what the harness
writes back after a turn.

The crate here is NOT the recorded one — a Display dispatcher in `lib.rs`
and a `render.rs` implementer, the never-steer check's own example; the
functions read only the owner map shape, the reply and the measurement.
"""
from __future__ import annotations

from pipeline_core.domain.attribution import locator_of
from pipeline_core.domain.fill_schedule import RouteKind, RouteRun
from pipeline_core.domain.models import FnLocator, FnRecord, FnSite
from pipeline_core.domain.slice_session import (
    SessionSlice,
    TargetStatus,
    TurnReading,
    attributed_units_for_session,
    extract_notes,
    locator_only_expansions,
    plan_slices,
    render_compile_turn,
    render_test_turn,
    route_change_set,
    session_slices_for_failing,
    slice_for_run,
    slice_plan_gaps,
    slices_with_pair_hops,
)

_DISP = FnSite("src/lib.rs", "fmt", "impl fmt::Display for Report")
_RENDER = FnSite("src/render.rs", "render", "impl Renderer")
_NEW = FnSite("src/render.rs", "new", "impl Renderer")
_OTHER = FnSite("src/other.rs", "go", "free fn")
_DEBUG_RENDER = FnSite("src/render.rs", "fmt", "impl fmt::Debug for Renderer")


def test_tests_with_overlapping_slices_share_one_session() -> None:
    plans = plan_slices({
        "t_a": {_DISP, _RENDER},
        "t_b": {_RENDER, _NEW},
        "t_c": {_OTHER},
    })
    assert [p.tests for p in plans] == [("t_a", "t_b"), ("t_c",)]
    assert plans[0].units == (_DISP, _NEW, _RENDER)
    assert plans[0].files == ("src/lib.rs", "src/render.rs")


def test_a_slice_touching_a_stub_is_left_to_the_stub_routes() -> None:
    plans = plan_slices({"t_a": {_DISP, _RENDER}},
                        stubs={locator_of(_RENDER)})
    assert plans == []


def test_a_slice_over_the_unit_cap_is_dropped_not_truncated() -> None:
    big = {FnSite("src/x.rs", f"f{i}", f"impl X{i}") for i in range(5)}
    assert plan_slices({"t": big}, max_units=4) == []
    assert len(plan_slices({"t": big}, max_units=5)) == 1
    gaps = slice_plan_gaps({"t": big, "ui": frozenset()}, max_units=4)
    assert (gaps.empty, gaps.hollow, gaps.over_cap, gaps.over_cap_units) == (
        1, 0, 1, 5)
    assert gaps.log_clause() == (
        "1 empty; 1 over SLICE_SESSION_MAX_UNITS=4 (5 unit(s))")


def test_slice_plan_gaps_names_hollow_apart_from_over_cap() -> None:
    gaps = slice_plan_gaps(
        {"t_stub": {_DISP}, "t_ok": {_RENDER}},
        stubs={locator_of(_DISP)}, max_units=32)
    assert (gaps.empty, gaps.hollow, gaps.over_cap) == (0, 1, 0)
    assert gaps.log_clause() == "1 hollow"
    empty_only = slice_plan_gaps({"ui": frozenset()})
    assert empty_only.log_clause() == "1 empty"
    assert "over SLICE_SESSION_MAX_UNITS" not in empty_only.log_clause()


def test_an_empty_owner_map_plans_no_session() -> None:
    """Negative control of iterating coverage keys: ui with 0 units is
    dropped, so a failing set that coverage listed only as that empty
    key yields no session (the live nightly iters 4-9 log line)."""
    assert plan_slices({"ui": frozenset()}) == []


def test_a_failing_test_absent_from_owners_still_gets_a_session() -> None:
    """The failing set is the input, not the coverage-key subset. A
    cfg-gated test coverage omitted still forms a session when
    attribution already owns the format pair; an empty-owner leftover
    does not."""
    t_cfg = "t_cfg"
    slices = session_slices_for_failing(
        {t_cfg, "ui"},
        owners={"ui": frozenset()},
        units_by_test={t_cfg: {_DISP, _RENDER}},
        pairs=[(_DISP, _RENDER)],
    )
    plans = plan_slices(slices)
    assert [p.tests for p in plans] == [(t_cfg,)]
    assert set(plans[0].units) == {_DISP, _RENDER}
    # Today's loop over owners.items() on the same map.
    today: dict[str, frozenset[FnSite]] = {}
    for name, units in {"ui": frozenset()}.items():
        today[name] = frozenset(units) | frozenset()
    assert plan_slices(slices_with_pair_hops(today, [(_DISP, _RENDER)])) == []


def test_a_paired_test_does_not_pack_coverage_into_a_session() -> None:
    """A leftover that already has a hop pair does not union coverage
    owners. The slice is the two hop members, not the spine. Control:
    the same maps without paired_tests still include the spine."""
    owners = {"t_fmt": {_DISP, _RENDER, _OTHER}}
    units = {"t_fmt": {_DISP, _RENDER}}
    pairs = [(_DISP, _RENDER)]
    skipped = session_slices_for_failing(
        {"t_fmt"}, owners=owners, units_by_test=units, pairs=pairs,
        paired_tests={"t_fmt"})
    assert skipped["t_fmt"] == frozenset({_DISP, _RENDER})
    control = session_slices_for_failing(
        {"t_fmt"}, owners=owners, units_by_test=units, pairs=pairs)
    assert _OTHER in control["t_fmt"]
    assert {_DISP, _RENDER} <= control["t_fmt"]


def _records() -> dict[FnSite, FnRecord]:
    return {
        _DISP: FnRecord(file="src/lib.rs", fn_name="fmt",
                       container="impl fmt::Display for Report",
                       sig_text="fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result"),
        _RENDER: FnRecord(file="src/render.rs", fn_name="render",
                          container="impl Renderer", sig_text="pub fn render(&self) -> u32"),
        _DEBUG_RENDER: FnRecord(
            file="src/render.rs", fn_name="fmt", container="impl fmt::Debug for Renderer",
            sig_text="fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result"),
    }


def test_bodies_route_by_name_and_block_to_the_slice_units() -> None:
    cs = route_change_set([
        {"fn_name": "fmt", "block": "impl fmt::Display for Report", "body": "A", "where_clause": None},
        {"fn_name": "render", "block": "", "body": "B", "where_clause": None},
    ], _records())
    assert [(r.unit, r.body) for r in cs.routed] == [(_DISP, "A"), (_RENDER, "B")]
    assert cs.rejected == ()


def test_an_ambiguous_name_without_a_block_is_rejected_with_candidates() -> None:
    cs = route_change_set(
        [{"fn_name": "fmt", "block": "", "body": "A", "where_clause": None}], _records())
    assert cs.routed == ()
    assert "ambiguous" in cs.rejected[0] and "impl fmt::Debug for Renderer" in cs.rejected[0]


def test_a_name_outside_the_editable_set_never_routes() -> None:
    cs = route_change_set(
        [{"fn_name": "secret", "block": "impl X", "body": "A", "where_clause": None}], _records())
    assert cs.routed == () and "not in this session's editable set" in cs.rejected[0]


def test_notes_are_extracted_and_absent_notes_are_empty() -> None:
    assert extract_notes("x <NOTES> keep the gate </NOTES> y") == "keep the gate"
    assert extract_notes("no notes here") == ""


def test_the_verdict_turn_shows_printed_and_observed_values() -> None:
    reading = TurnReading(
        targets=(TargetStatus("t_a", False, "assertion failed: s.contains(\"x\")",
                              'observed at line 9: s = "plain"'),
                 TargetStatus("t_b", True)),
        newly_red=("t_keep",),
        newly_red_said={"t_keep": 'left: "p<disabled>"\nright: "p"'},
        newly_red_observed={"t_keep": 'observed at line 4: out = "p<disabled>"'},
    )
    text = render_test_turn(reading)
    assert "FAIL t_a" in text and "PASS t_b" in text
    assert 'observed at line 9: s = "plain"' in text
    assert "REGRESSION: 1 previously-passing test(s)" in text
    assert 'left: "p<disabled>"' in text
    assert 'observed at line 4: out = "p<disabled>"' in text
    assert not reading.clean and reading.regressed


def test_a_clean_reading_is_reported_as_kept() -> None:
    reading = TurnReading(targets=(TargetStatus("t_a", True),))
    assert reading.clean
    assert "KEPT" in render_test_turn(reading)


def test_the_compile_turn_says_the_tree_was_not_changed() -> None:
    text = render_compile_turn("error[E0616]: field `x` is private", ["`y`: splice refused"])
    assert "did NOT compile" in text and "E0616" in text and "`y`: splice refused" in text


# --- the branches the first fixture did not reach -----------------------------

def test_a_test_bridging_two_groups_joins_them() -> None:
    a = FnSite("src/a.rs", "a", "impl A")
    b = FnSite("src/b.rs", "b", "impl B")
    c = FnSite("src/c.rs", "c", "impl C")
    plans = plan_slices({"t_1": {a}, "t_2": {b}, "t_3": {a, b, c}})
    assert len(plans) == 1
    assert plans[0].tests == ("t_1", "t_2", "t_3") and plans[0].units == (a, b, c)


def test_a_unique_name_routes_even_when_the_block_is_off() -> None:
    cs = route_change_set(
        [{"fn_name": "render", "block": "impl Something Else", "body": "B", "where_clause": None}],
        _records())
    assert [r.unit for r in cs.routed] == [_RENDER]


def test_a_body_given_twice_keeps_the_first() -> None:
    cs = route_change_set([
        {"fn_name": "render", "block": "", "body": "B1", "where_clause": None},
        {"fn_name": "render", "block": "", "body": "B2", "where_clause": "where T: Copy"},
    ], _records())
    assert [(r.unit, r.body) for r in cs.routed] == [(_RENDER, "B1")]
    assert "given twice" in cs.rejected[0]


def test_rejected_only_and_crash_turns_render() -> None:
    from pipeline_core.domain.slice_session import (
        kept_units,
        render_rejected_only_turn,
        units_by_file,
    )
    assert "no body in that reply could be applied" in render_rejected_only_turn(["`x`: bad"])
    reading = TurnReading(
        targets=(TargetStatus("t_a", True),), crashed=("tests/bin_a",))
    text = render_test_turn(reading, ["`y`: splice refused"])
    assert "CRASH: test binary ['tests/bin_a']" in text
    assert "Bodies not applied: `y`: splice refused" in text
    assert reading.regressed and not reading.clean
    base = {_DISP: "a", _RENDER: "b"}
    cur = {_DISP: "a", _RENDER: "c", _NEW: "n"}
    assert kept_units(base, cur) == frozenset({_RENDER})
    assert units_by_file([_DISP, _RENDER, _NEW]) == {
        "src/lib.rs": [_DISP], "src/render.rs": [_RENDER, _NEW]}


def _turn0(**kwargs) -> str:
    from pipeline_core.prompts.slice_session import build_slice_session_prompt
    sl = SessionSlice(tests=("t_pair",), units=(_DISP, _RENDER))
    recs = _records()
    args = {
        "module_name": "m", "cluster_name": "t", "slice_": sl, "records": recs,
        "all_records": list(recs.values()),
        "file_texts": {"src/lib.rs": "fn fmt() {}", "src/render.rs": "fn render() {}"},
        "target_evidence": {"t_pair": 'left: "a"'},
        "test_blocks": (), "types_section": "",
        **kwargs,
    }
    return build_slice_session_prompt(**args)


def test_turn0_renders_prior_regression_and_omits_empty_notes() -> None:
    text = _turn0(prior_regression="the PREVIOUS attempt reddened t_keep")
    assert "PRIOR_REGRESSION" in text
    assert "reddened t_keep" in text
    assert "PREVIOUS_SESSION_NOTES" not in text
    # No panic / regression-head mark: kind_from_text is UNKNOWN, so the
    # pair's faulting-call prefix must not appear.
    assert "compiled and then" not in text
    assert "do not emit" not in text
    empty = _turn0(prior_regression="")
    assert "PRIOR_REGRESSION" not in empty
    assert "compiled and then" not in empty


def test_turn0_faulting_prior_regression_uses_the_pair_prefix() -> None:
    from pipeline_core.domain.last_error import LastErrorKind, last_error_prefix
    from pipeline_core.domain.test_results import ATTEMPT_QUOTE_MARK
    quote = (
        "t_sib said: panicked at src/lib.rs:4:1\n"
        f"  {ATTEMPT_QUOTE_MARK}: `self.0[n + 3]` inside `fn widen`")
    text = _turn0(prior_regression=quote, kind=LastErrorKind.REDDENED)
    prefix = last_error_prefix(quote, LastErrorKind.REDDENED)
    assert prefix.strip() in text
    assert ATTEMPT_QUOTE_MARK in text
    assert "self.0[n + 3]" in text
    compile_kind = _turn0(prior_regression=quote, kind=LastErrorKind.COMPILE)
    assert prefix.strip() not in compile_kind
    assert ATTEMPT_QUOTE_MARK in compile_kind


def test_render_test_turn_prefixes_a_faulting_regression() -> None:
    from pipeline_core.domain.last_error import LastErrorKind, last_error_prefix
    from pipeline_core.domain.test_results import ATTEMPT_QUOTE_MARK
    said = (
        "panicked at src/lib.rs:4:1\n"
        f"{ATTEMPT_QUOTE_MARK}: `self.0[n + 3]` inside `fn widen`")
    reading = TurnReading(
        targets=(TargetStatus("t_a", False, said="still red"),),
        newly_red=("t_sib",),
        newly_red_said={"t_sib": said})
    text = render_test_turn(reading)
    prefix = last_error_prefix(said, LastErrorKind.REDDENED).rstrip()
    assert prefix in text
    assert ATTEMPT_QUOTE_MARK in text
    clean = render_test_turn(TurnReading(
        targets=(TargetStatus("t_a", True),)))
    assert "compiled and then" not in clean
    assert "do not emit" not in clean


# --- what the first live session taught (run 164454-8ec9c4d4, 2026-09-02) ---

def test_a_body_carrying_another_fns_definition_is_rejected_before_cargo() -> None:
    from pipeline_core.domain.slice_session import declares_other_fn
    body = ("pub(crate) unsafe fn display(this: Ref<Self>, f: &mut fmt::Formatter) -> fmt::Result {\n"
            "    let err = unsafe { Self::error(this) };\n    core::fmt::Display::fmt(err, f)\n}\n"
            "pub(crate) unsafe fn debug(this: Ref<Self>, f: &mut fmt::Formatter) -> fmt::Result {\n"
            "    Ok(())\n}\n")
    assert declares_other_fn(body, "debug") == "display"
    assert declares_other_fn("pub fn other() { 1 }\nOk(())", "render") == "other"
    assert declares_other_fn("let f = 1;\nfn_call(x)\n// fn comment(a)", "render") is None
    cs = route_change_set(
        [{"fn_name": "render", "block": "impl Renderer", "body": body.replace("debug", "render"),
          "where_clause": None}], _records())
    assert cs.routed == ()
    assert "carries a fn DEFINITION (`display`)" in cs.rejected[0]


def test_coverage_note_names_reached_and_unreached_changed_units() -> None:
    from pipeline_core.domain.attribution import site_label
    from pipeline_core.domain.slice_session import coverage_note_for
    note = coverage_note_for(
        changed=[_DISP, _RENDER], owners={"t_a": {_DISP, _NEW}},
        editable=[_DISP, _RENDER, _NEW], unchanged_since_turn=3)
    # Each unit is named as a SITE: two `fmt` impls in one file must not
    # read as one unit named twice.
    assert f"changed units they EXECUTE: {site_label(_DISP)}" in note
    assert f"do NOT execute: {site_label(_RENDER)}" in note
    assert (f"editable units the targets execute now: {site_label(_DISP)}, "
            f"{site_label(_NEW)}") in note
    assert "byte-identical to turn 3's" in note


def test_a_formatter_pair_closes_the_slice_over_both_halves() -> None:
    from pipeline_core.domain.slice_session import slices_with_pair_hops
    out = slices_with_pair_hops({"t_a": {_DISP}, "t_b": {_OTHER}}, [(_DISP, _RENDER)])
    assert out["t_a"] == frozenset({_DISP, _RENDER})
    assert out["t_b"] == frozenset({_OTHER})


def test_the_verdict_turn_names_dropped_bodies_and_the_coverage_fact() -> None:
    reading = TurnReading(targets=(TargetStatus("t_a", False, "assertion failed: x"),))
    text = render_test_turn(
        reading, dropped=["context"], dropped_diagnostics="error[E0308]: mismatched types",
        coverage_note="COVERAGE … do NOT execute: src/fmt.rs::debug")
    assert text.startswith("COMPILER: 1 body(ies) did not compile and were DROPPED")
    assert "error[E0308]" in text and "Dropped: context" in text
    assert "do NOT execute: src/fmt.rs::debug" in text


def test_attributed_units_eligible_without_frontier_is_not_a_locator_only_unit() -> None:
    """Eligible locators that never entered the site frontier do not become
    units. The 238.5 loop invented FnSite(file, fn, "") for those and the
    expander then named every container of the name."""
    loc = FnLocator("src/w.rs", "fmt")
    assert attributed_units_for_session({loc: {"t_pay"}}, {}) == {}
    dbg = FnSite("src/w.rs", "fmt", "impl Debug for W")
    # Frontier site whose locator is not dispatchable this iter is omitted.
    assert attributed_units_for_session({}, {dbg: {"t_pay"}}) == {}
    # Frontier site for T whose locator is eligible is included.
    assert attributed_units_for_session(
        {loc: {"crate::t_pay"}}, {dbg: {"t_pay"}}) == {"t_pay": {dbg}}
    # Empty-container frontier site still enters (classifier / reverse-map).
    bare = FnSite("src/w.rs", "fmt", "")
    assert attributed_units_for_session(
        {loc: {"t_pay"}}, {bare: {"t_pay"}}) == {"t_pay": {bare}}


def test_locator_only_expansions_names_the_fan_out() -> None:
    disp = FnSite("src/w.rs", "fmt", "impl Display for W")
    dbg = FnSite("src/w.rs", "fmt", "impl Debug for W")
    assert locator_only_expansions(
        {"t": {FnSite("src/w.rs", "fmt", "")}}, [disp, dbg]) == (
        ("w.rs", "fmt", 2),)
    assert locator_only_expansions({"t": {dbg}}, [disp, dbg]) == ()


def test_two_sessions_with_the_same_count_label_each_bind_their_own_slice() -> None:
    """Plan sentence: two SessionSlice values with the same counts and
    disjoint test sets bind by target_tests, not by label."""
    a = SessionSlice(tests=("t_a", "t_b"), units=(_DISP, _RENDER))
    b = SessionSlice(tests=("t_c", "t_d"), units=(_NEW, _OTHER))
    assert a.label() == b.label()
    plans = [a, b]
    # Negative control: dict-by-label last-wins; both runs would bind b.
    by_label = {sl.label(): sl for sl in plans}
    assert by_label[a.label()] is b
    run_a = RouteRun(
        kind=RouteKind.SESSION, label=a.label(),
        targets=frozenset(locator_of(u) for u in a.units),
        target_tests=frozenset(a.tests), seq=0)
    run_b = RouteRun(
        kind=RouteKind.SESSION, label=b.label(),
        targets=frozenset(locator_of(u) for u in b.units),
        target_tests=frozenset(b.tests), seq=1)
    assert slice_for_run(plans, run_a) is a
    assert slice_for_run(plans, run_b) is b
    miss = RouteRun(
        kind=RouteKind.SESSION, label=a.label(),
        targets=frozenset(), target_tests=frozenset({"t_z"}), seq=9)
    assert slice_for_run(plans, miss) is None


# ---- packing an over-union group under the cap -----------------------------

def _hub_and_unique(n: int) -> dict[str, set[FnSite]]:
    """`n` tests that share one hub site and each execute one site of their
    own — the shape of a coverage-complete leftover set whose tests all fit
    the cap while their union does not."""
    hub = FnSite("src/core.rs", "construct", "impl Core")
    return {f"t_{i:02d}": {hub, FnSite(f"src/m{i}.rs", "go", f"impl M{i}")}
            for i in range(n)}


def test_an_over_union_leftover_set_whose_every_test_fits_is_packed_not_refused() -> None:
    """The plan's sentence. Eight tests of two sites each, union nine, cap
    four: every test fits, the union does not. Control: the pre-packing
    rule (one merged group over the cap → nothing) is what `over_cap`
    used to count; after, ≥2 sessions, each under the cap, each test in
    exactly one, each test's slice a subset of its session, the hub in
    every session that holds a test which executes it."""
    slices = _hub_and_unique(8)
    hub = FnSite("src/core.rs", "construct", "impl Core")
    plans = plan_slices(slices, max_units=4)
    assert len(plans) >= 2
    placed = [t for p in plans for t in p.tests]
    assert sorted(placed) == sorted(slices)
    for p in plans:
        assert len(p.units) <= 4
        assert hub in p.units
        for t in p.tests:
            assert slices[t] <= set(p.units)
    gaps = slice_plan_gaps(slices, max_units=4)
    assert gaps.over_cap == 0
    assert gaps.over_union == 1
    assert gaps.fitted == len(plans)
    assert gaps.fitted_from_units == 9
    assert "fitted under the cap" in gaps.fitted_clause()
    assert gaps.log_clause() == "no session"
    # NEGATIVE CONTROL — the merged union alone is what the old rule dropped.
    from pipeline_core.domain.slice_session import _merge_pending, _pending_after_filters
    pending, _, _ = _pending_after_filters(slices, frozenset())
    groups = [(n, m) for n, m in _merge_pending(pending) if n]
    assert len(groups) == 1 and len(groups[0][1]) == 9 > 4


def test_a_single_test_over_the_cap_is_still_dropped_not_truncated() -> None:
    """One test executes five sites at cap four; a neighbour shares one of
    them and fits. The oversized test is in no session; the neighbour
    still plans on its own slice; the gap names one over-cap TEST."""
    big = {FnSite("src/x.rs", f"f{i}", f"impl X{i}") for i in range(5)}
    shared = next(iter(sorted(big, key=lambda u: u.fn_name)))
    small = {shared, FnSite("src/y.rs", "g", "impl Y")}
    plans = plan_slices({"t_big": big, "t_small": small}, max_units=4)
    assert [p.tests for p in plans] == [("t_small",)]
    assert set(plans[0].units) == small
    gaps = slice_plan_gaps({"t_big": big, "t_small": small}, max_units=4)
    assert (gaps.over_cap, gaps.over_cap_units) == (1, 5)
    assert gaps.fitted == 1 and gaps.over_union == 1
    assert gaps.log_clause() == "1 over SLICE_SESSION_MAX_UNITS=4 (5 unit(s))"


def test_packing_is_deterministic_and_keeps_overlapping_tests_together() -> None:
    """A test joins the session already holding the most of its path."""
    a = {FnSite("src/a.rs", f"a{i}", "impl A") for i in range(3)}
    b = {FnSite("src/b.rs", f"b{i}", "impl B") for i in range(3)}
    hub = FnSite("src/core.rs", "construct", "impl Core")
    slices = {
        "t_a1": a | {hub}, "t_b1": b | {hub},
        "t_a2": set(sorted(a, key=lambda u: u.fn_name)[:2]) | {hub},
        "t_b2": set(sorted(b, key=lambda u: u.fn_name)[:2]) | {hub},
    }
    first = plan_slices(slices, max_units=5)
    assert first == plan_slices(dict(reversed(list(slices.items()))), max_units=5)
    by_test = {t: p for p in first for t in p.tests}
    assert by_test["t_a1"] is by_test["t_a2"]
    assert by_test["t_b1"] is by_test["t_b2"]
    assert by_test["t_a1"] is not by_test["t_b1"]


def test_slice_plan_payload_records_the_union_and_the_sessions() -> None:
    from pipeline_core.domain.slice_session import slice_plan_payload
    slices = _hub_and_unique(3)
    plans = plan_slices(slices, max_units=4)
    gaps = slice_plan_gaps(slices, max_units=4)
    payload = slice_plan_payload(slices, plans, gaps)
    assert payload["tests"] == 3 and payload["units_union"] == 4
    assert payload["max_units"] == 4 and payload["over_union"] == 0
    assert [s["tests"] for s in payload["sessions"]] == [list(p.tests) for p in plans]
    assert set(payload["slices"]) == set(slices)


def test_a_group_under_the_unit_cap_is_packed_when_its_prompt_is_over_the_char_capacity() -> None:
    """Plan sentence: a session is sized to leave its turns headroom. Two
    tests whose union is 3 units (under the cap) but whose files render to
    more than the capacity are split; the same tests with no char capacity
    (the pre-fix packing) stay one session — the shape of the 05:23 session
    of run 334902-2774708c (18 tests, 9 files, 92% of the call limit after
    one turn)."""
    from pipeline_core.domain.slice_session import SliceCosts
    a = FnSite("src/a.rs", "f", "impl A")
    b = FnSite("src/b.rs", "g", "impl B")
    hub = FnSite("src/hub.rs", "construct", "impl Hub")
    leftover = {"t_a": {a, hub}, "t_b": {b, hub}}
    # One rendering threshold: every file renders the same at every one, so
    # the packing is the only lever and splitting is what has to happen.
    flat = {"src/a.rs": 30_000, "src/b.rs": 30_000, "src/hub.rs": 10_000}
    costs = SliceCosts(
        file_chars={36_000: flat, 18_000: flat},
        test_chars={"t_a": 2_000, "t_b": 2_000}, fixed_chars=5_000,
        max_chars=60_000)
    packed = plan_slices(leftover, costs=costs)
    assert [p.tests for p in packed] == [("t_a",), ("t_b",)]
    assert all(costs.chars(p.tests, p.units) <= 60_000 for p in packed)
    gaps = slice_plan_gaps(leftover, costs=costs)
    assert gaps.over_chars == 1 and gaps.over_union == 0 and gaps.fitted == 2
    assert "char capacity" in gaps.fitted_clause()
    # Control: unit-count packing alone keeps them together (union 3 <= 32).
    single = plan_slices(leftover)
    assert [p.tests for p in single] == [("t_a", "t_b")]
    # A single test over the capacity at EVERY rendering is dropped, as a
    # single test over the unit cap is — never truncated.
    huge = {"src/a.rs": 70_000, "src/hub.rs": 10_000}
    too_big = SliceCosts(file_chars={36_000: huge, 18_000: huge},
                         test_chars={"t_a": 100}, fixed_chars=100, max_chars=60_000)
    assert plan_slices({"t_a": {a, hub}}, costs=too_big) == []
    assert slice_plan_gaps({"t_a": {a, hub}}, costs=too_big).over_chars_single == 1
    assert slice_plan_gaps({"t_a": {a, hub}}, costs=too_big).over_cap == 0
    # The same single test opens when a NARROWER rendering fits it: the
    # planner condenses rather than dropping, and says which rendering.
    shrinks = SliceCosts(
        file_chars={36_000: huge, 18_000: {"src/a.rs": 55_000, "src/hub.rs": 10_000},
                    9_000: {"src/a.rs": 8_000, "src/hub.rs": 4_000}},
        test_chars={"t_a": 100}, fixed_chars=100, max_chars=60_000)
    opened = plan_slices({"t_a": {a, hub}}, costs=shrinks)
    assert [p.tests for p in opened] == [("t_a",)]
    assert opened[0].file_chars == 9_000
    assert slice_plan_gaps({"t_a": {a, hub}}, costs=shrinks).condensed == 1


def test_a_hung_binary_renders_as_a_hang_and_regresses_at_any_gain() -> None:
    """Run `3500465-67c40e78` turn 1: `lib_gen_1` hung inside a test and the
    turn said CRASH about it. A hang is named as a hang (the transcript's
    next turn must say the body does not return), it is not a second CRASH
    line for the same binary, and no net gain forgives it — the binaries
    after it never ran."""
    reading = TurnReading(
        targets=(TargetStatus("t_a", True),),
        crashed=("tests/lib_gen_1.rs", "tests/bin_b.rs"),
        hung=("tests/lib_gen_1.rs::t_hang",), pass_delta=5)
    text = render_test_turn(reading)
    assert "HANG: binary tests/lib_gen_1.rs hung inside test t_hang" in text
    assert "CRASH: test binary ['tests/bin_b.rs']" in text
    assert "lib_gen_1.rs']" not in text.split("CRASH:", 1)[1]
    assert reading.regressed and not reading.clean
    assert not TurnReading(targets=(TargetStatus("t_a", True),),
                           crashed=("tests/bin_b.rs",), pass_delta=5).regressed
