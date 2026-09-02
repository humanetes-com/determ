"""The red-slice session's pure half: which failing tests form one session,
how a reply's bodies route to the slice's units, and what the harness
writes back after a turn.

The crate here is NOT the recorded one — a Display dispatcher in `lib.rs`
and a `render.rs` implementer, the never-steer check's own example; the
functions read only the owner map shape, the reply and the measurement.
"""
from __future__ import annotations

from pipeline_core.domain.models import FnLocator, FnRecord
from pipeline_core.domain.slice_session import (
    SessionSlice,
    TargetStatus,
    TurnReading,
    extract_notes,
    plan_slices,
    render_compile_turn,
    render_test_turn,
    route_change_set,
    session_slices_for_failing,
    slices_with_pair_hops,
)

_FMT = FnLocator("src/lib.rs", "fmt")
_RENDER = FnLocator("src/render.rs", "render")
_NEW = FnLocator("src/render.rs", "new")
_OTHER = FnLocator("src/other.rs", "go")


def test_tests_with_overlapping_slices_share_one_session() -> None:
    plans = plan_slices({
        "t_a": {_FMT, _RENDER},
        "t_b": {_RENDER, _NEW},
        "t_c": {_OTHER},
    })
    assert [p.tests for p in plans] == [("t_a", "t_b"), ("t_c",)]
    assert plans[0].units == (_FMT, _NEW, _RENDER)
    assert plans[0].files == ("src/lib.rs", "src/render.rs")


def test_a_slice_touching_a_stub_is_left_to_the_stub_routes() -> None:
    plans = plan_slices({"t_a": {_FMT, _RENDER}}, stubs={_RENDER})
    assert plans == []


def test_a_slice_over_the_unit_cap_is_dropped_not_truncated() -> None:
    big = {FnLocator("src/x.rs", f"f{i}") for i in range(5)}
    assert plan_slices({"t": big}, max_units=4) == []
    assert len(plan_slices({"t": big}, max_units=5)) == 1


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
        units_by_test={t_cfg: {_FMT, _RENDER}},
        pairs=[(_FMT, _RENDER)],
    )
    plans = plan_slices(slices)
    assert [p.tests for p in plans] == [(t_cfg,)]
    assert set(plans[0].units) == {_FMT, _RENDER}
    # Today's loop over owners.items() on the same map.
    today: dict[str, frozenset[FnLocator]] = {}
    for name, units in {"ui": frozenset()}.items():
        today[name] = frozenset(units) | frozenset()
    assert plan_slices(slices_with_pair_hops(today, [(_FMT, _RENDER)])) == []


def _records() -> dict[FnLocator, FnRecord]:
    return {
        _FMT: FnRecord(file="src/lib.rs", fn_name="fmt",
                       container="impl fmt::Display for Report",
                       sig_text="fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result"),
        _RENDER: FnRecord(file="src/render.rs", fn_name="render",
                          container="impl Renderer", sig_text="pub fn render(&self) -> u32"),
        FnLocator("src/render.rs", "fmt"): FnRecord(
            file="src/render.rs", fn_name="fmt", container="impl fmt::Debug for Renderer",
            sig_text="fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result"),
    }


def test_bodies_route_by_name_and_block_to_the_slice_units() -> None:
    cs = route_change_set([
        {"fn_name": "fmt", "block": "impl fmt::Display for Report", "body": "A", "where_clause": None},
        {"fn_name": "render", "block": "", "body": "B", "where_clause": None},
    ], _records())
    assert [(r.unit, r.body) for r in cs.routed] == [(_FMT, "A"), (_RENDER, "B")]
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
    a, b, c = FnLocator("src/a.rs", "a"), FnLocator("src/b.rs", "b"), FnLocator("src/c.rs", "c")
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
    base = {_FMT: "a", _RENDER: "b"}
    cur = {_FMT: "a", _RENDER: "c", _NEW: "n"}
    assert kept_units(base, cur) == frozenset({_RENDER})
    assert units_by_file([_FMT, _RENDER, _NEW]) == {
        "src/lib.rs": [_FMT], "src/render.rs": [_RENDER, _NEW]}


def test_turn0_renders_prior_regression_and_omits_empty_notes() -> None:
    from pipeline_core.prompts.slice_session import build_slice_session_prompt
    sl = SessionSlice(tests=("t_pair",), units=(_FMT, _RENDER))
    recs = _records()
    text = build_slice_session_prompt(
        module_name="m", cluster_name="t", slice_=sl, records=recs,
        all_records=list(recs.values()),
        file_texts={"src/lib.rs": "fn fmt() {}", "src/render.rs": "fn render() {}"},
        target_evidence={"t_pair": 'left: "a"'},
        test_blocks=(), types_section="",
        prior_regression="the PREVIOUS attempt reddened t_keep")
    assert "PRIOR_REGRESSION" in text
    assert "reddened t_keep" in text
    assert "PREVIOUS_SESSION_NOTES" not in text


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
    from pipeline_core.domain.slice_session import coverage_note_for
    note = coverage_note_for(
        changed=[_FMT, _RENDER], owners={"t_a": {_FMT, _NEW}},
        editable=[_FMT, _RENDER, _NEW], unchanged_since_turn=3)
    assert "changed units they EXECUTE: src/lib.rs::fmt" in note
    assert "do NOT execute: src/render.rs::render" in note
    assert "editable units the targets execute now: src/lib.rs::fmt, src/render.rs::new" in note
    assert "byte-identical to turn 3's" in note


def test_a_formatter_pair_closes_the_slice_over_both_halves() -> None:
    from pipeline_core.domain.slice_session import slices_with_pair_hops
    out = slices_with_pair_hops({"t_a": {_FMT}, "t_b": {_OTHER}}, [(_FMT, _RENDER)])
    assert out["t_a"] == frozenset({_FMT, _RENDER})
    assert out["t_b"] == frozenset({_OTHER})


def test_the_verdict_turn_names_dropped_bodies_and_the_coverage_fact() -> None:
    reading = TurnReading(targets=(TargetStatus("t_a", False, "assertion failed: x"),))
    text = render_test_turn(
        reading, dropped=["context"], dropped_diagnostics="error[E0308]: mismatched types",
        coverage_note="COVERAGE … do NOT execute: src/fmt.rs::debug")
    assert text.startswith("COMPILER: 1 body(ies) did not compile and were DROPPED")
    assert "error[E0308]" in text and "Dropped: context" in text
    assert "do NOT execute: src/fmt.rs::debug" in text
