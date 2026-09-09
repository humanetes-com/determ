"""The session path keys bodies on FnSite — the plan's two-impl fixture.

Plan sentence: on a tree where one type implements Display and Debug, the
coverage reading names both `fmt` sites from a REAL llvm-cov export, the
session's editable set carries both with their own executed mark, and a
reply naming block="impl Debug for …" routes to the Debug record.

The fixture is computed, not described: `tests/fixtures/dual_fmt` is the
crate, `coverage_export.json` is the export `llvm-cov export` produced for
it under `-C instrument-coverage` (2026-09-03; the two `fmt` regions start
at `[8, 5, …]` and `[14, 5, …]`, the SIGNATURE lines), and the records come
from the release extractor. Negative controls are the two resolvers this
one replaced: the regex name lookback (names `fmt`, never the impl) and
byte containment of the line's column-0 offset (names nothing for an
indented fn, which is every impl method).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline_core.adapters.coverage_owner import SiteIndex, _body_counts, _counted_bodies
from pipeline_core.adapters.extractor import CliExtractor
from pipeline_core.application.reconstruct.attribution import (
    _attribute_format_only_test,
    _debug_implementer_not_yet_called,
    _format_impl_calls,
)
from pipeline_core.domain.attribution import (
    FormatterPair,
    canon_site,
    container_key,
    lift_sites,
    record_site,
    records_by_site,
    site_from_payload,
    site_payload,
)
from pipeline_core.domain.cfg import CfgSet
from pipeline_core.domain.coverage_slice import slice_from_counts
from pipeline_core.domain.models import FnLocator, FnRecord, FnSite
from pipeline_core.domain.slice_session import (
    SessionSlice,
    attributed_units_for_session,
    expand_locator_sites,
    plan_slices,
    recorded_locator_sites,
    route_change_set,
    session_slices_for_failing,
    slice_plan_gaps,
    slices_with_pair_hops,
)
from pipeline_core.prompts.slice_session import build_slice_session_prompt

_BIN = (
    Path(__file__).resolve().parents[1]
    / "tools" / "skeleton_extractor" / "target" / "release" / "skeleton-extractor"
)
_FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "dual_fmt"
_EXPORT = json.loads((_FIXTURE_ROOT / "coverage_export.json").read_text(encoding="utf-8"))
_TEXT = (_FIXTURE_ROOT / "src" / "lib.rs").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def fmt_records() -> list[FnRecord]:
    if not _BIN.exists():
        pytest.skip("extractor release binary missing")
    out = CliExtractor(_BIN).enumerate_fns(_FIXTURE_ROOT)
    if not out:
        pytest.skip("enumerate_fns returned nothing on dual_fmt fixture")
    fmts = [r for r in out if r.fn_name == "fmt"]
    if len(fmts) < 2:
        pytest.skip(f"expected two fmt sites, got {len(fmts)}")
    return out


def _records_by_site(recs: list[FnRecord]) -> dict[FnSite, FnRecord]:
    found = records_by_site(recs, compiled=lambda _r: True)
    assert not found.collisions
    return dict(found.by_site)


def _disp_and_debug(recs: list[FnRecord]) -> tuple[FnRecord, FnRecord]:
    fmts = [r for r in recs if r.fn_name == "fmt"]
    disp = next(r for r in fmts if "Display" in (r.container or ""))
    dbg = next(r for r in fmts if "Debug" in (r.container or ""))
    return disp, dbg


# ---- the two retired resolvers, kept here as the negative controls ---------

def _regex_fn_name(text: str, line: int) -> str:
    """The regex lookback the adapter used before sites: the fn NAME only."""
    import re
    decl = re.compile(r"\bfn\s+([A-Za-z_]\w*)")
    src = text.splitlines()
    for i in range(min(line, len(src)) - 1, max(-1, line - 40), -1):
        m = decl.search(src[i])
        if m:
            return m.group(1)
    return "?"


def _byte_containment_site(recs: list[FnRecord], text: str, line: int) -> FnSite | None:
    """The first site resolver: the line's column-0 byte offset against
    `[sig_byte_start, body_byte_end)`. `sig_byte_start` is the `fn` keyword,
    which sits AFTER the indentation, so an impl method never contains its
    own signature line's offset."""
    off = len("".join(text.splitlines(keepends=True)[: line - 1]).encode("utf-8"))
    for rec in recs:
        if rec.sig_byte_start <= off < rec.body_byte_end:
            return record_site(rec)
    return None


def test_export_signature_lines_resolve_to_both_fmt_sites(fmt_records: list[FnRecord]) -> None:
    """The export's function starts are the signature lines (8 and 14), and
    each resolves to ITS impl's site — Display at 8, Debug at 14."""
    disp, dbg = _disp_and_debug(fmt_records)
    bodies = _body_counts(_EXPORT, _FIXTURE_ROOT)
    starts = sorted(start for (_rel, start) in bodies)
    assert starts == [8, 14], bodies
    index = SiteIndex(fmt_records, {"src/lib.rs": _TEXT})
    assert index.site_at("src/lib.rs", 8) == record_site(disp)
    assert index.site_at("src/lib.rs", 14) == record_site(dbg)
    # a line inside each body resolves the same way
    assert index.site_at("src/lib.rs", 15) == record_site(dbg)
    # Negative control 1: the regex lookback names the fn, never the impl.
    assert _regex_fn_name(_TEXT, 8) == _regex_fn_name(_TEXT, 14) == "fmt"
    # Negative control 2: byte containment of the column-0 offset names
    # nothing for either indented signature line.
    assert _byte_containment_site(fmt_records, _TEXT, 8) is None
    assert _byte_containment_site(fmt_records, _TEXT, 14) is None


def test_counted_bodies_from_the_real_export_are_two_live_sites(fmt_records: list[FnRecord]) -> None:
    disp, dbg = _disp_and_debug(fmt_records)
    index = SiteIndex(fmt_records, {"src/lib.rs": _TEXT})
    bodies = _counted_bodies(index, _body_counts(_EXPORT, _FIXTURE_ROOT), {})
    assert {b.locator for b in bodies} == {record_site(disp), record_site(dbg)}
    assert all(b.count == 1 for b in bodies)
    assert slice_from_counts(bodies).dead == frozenset()


def test_locator_keyed_map_hides_the_debug_site(fmt_records: list[FnRecord]) -> None:
    """Pre-fix collapse: one record per (file, fn_name) drops the second fmt."""
    disp, dbg = _disp_and_debug(fmt_records)
    locator_map = {FnLocator(disp.file, disp.fn_name): disp}
    assert record_site(dbg) not in locator_map
    assert len(locator_map) == 1


def test_route_change_set_selects_debug_by_block(fmt_records: list[FnRecord]) -> None:
    disp, dbg = _disp_and_debug(fmt_records)
    sites = _records_by_site(fmt_records)
    slice_sites = (record_site(disp), record_site(dbg))
    cs = route_change_set([{
        "fn_name": "fmt",
        "block": dbg.container or "",
        "body": "write!(f, \"dbg:{:?}\", self.label)",
    }], {u: sites[u] for u in slice_sites})
    assert len(cs.routed) == 1
    assert cs.routed[0].unit == record_site(dbg)
    assert not cs.rejected


def test_turn_zero_marks_both_fmt_sites_separately(fmt_records: list[FnRecord]) -> None:
    disp, dbg = _disp_and_debug(fmt_records)
    sites = _records_by_site(fmt_records)
    slice_ = SessionSlice(
        tests=("t_widget",),
        units=(record_site(disp), record_site(dbg)),
    )
    prompt = build_slice_session_prompt(
        module_name="dual_fmt", cluster_name="t",
        slice_=slice_, records=sites, all_records=fmt_records,
        file_texts={"src/lib.rs": _TEXT},
        target_evidence={"t_widget": 'left: "a"\n right: "b"'},
        test_blocks=(), types_section="",
        executed=frozenset({record_site(disp)}),
    )
    assert "fmt (impl" in prompt
    assert prompt.count("fmt (") >= 2
    assert "[executed by the failing tests]" in prompt
    assert "[NOT executed by the failing tests on the current tree]" in prompt
    assert "Display" in prompt and "Debug" in prompt


# ---- the pair hop builds canonical sites -----------------------------------

_DISP_REC = {
    "file": "src/w.rs", "fn_name": "fmt",
    "container": "impl Debug for Wrapper < T >",
    "sig_text": "fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result",
    "body_text": "\n        Inner::debug(self.inner.by_ref(), f)\n    ",
}
_IMPL_REC = {
    "file": "src/i.rs", "fn_name": "debug",
    "container": "impl Inner < T >",
    "sig_text": "fn debug(this: Ref<Self>, f: &mut fmt::Formatter) -> fmt::Result",
    "body_text": "\n        write!(f, \"{}\", this.value)\n    ",
}
_TSRC = (
    "#[test]\nfn t_wrap() {\n    let w: Wrapper<u8> = make();\n"
    "    let s = format!(\"{:?}\", w);\n    assert!(s.contains(\"x\"));\n}\n"
)


def test_hop_pair_sites_are_canonical_for_a_generic_container() -> None:
    """A dispatcher whose extractor header carries token-spaced generics
    is recorded in its canonical form, so the pair reaches a slice whose
    members came through `canon_site` (coverage owners, the site frontier).
    Negative control: the raw header the hop used to record does not."""
    pairs: set[FormatterPair] = set()
    frontier: dict = {}
    sites: dict = {}
    _attribute_format_only_test(
        "t_wrap", _TSRC, _TSRC,
        {"fmt": [_DISP_REC], "debug": [_IMPL_REC]},
        frontier, sites, log=lambda _m: None, pairs_out=pairs)
    assert len(pairs) == 1, pairs
    pair = next(iter(pairs))
    assert pair.dispatcher == canon_site(pair.dispatcher)
    assert pair.dispatcher.container == container_key("impl Debug for Wrapper < T >")
    assert pair.implementer.container == container_key("impl Inner < T >")
    owners = {"t_wrap": {canon_site(FnSite("src/w.rs", "fmt", "impl Debug for Wrapper < T >"))}}
    hopped = slices_with_pair_hops(owners, [(pair.dispatcher, pair.implementer)])
    assert pair.implementer in hopped["t_wrap"]
    raw = (FnSite("src/w.rs", "fmt", "impl Debug for Wrapper < T >"),
           FnSite("src/i.rs", "debug", "impl Inner < T >"))
    assert raw[0] != pair.dispatcher
    assert slices_with_pair_hops(owners, [raw])["t_wrap"] == frozenset(owners["t_wrap"])


# A Debug dispatcher whose body currently calls Qual::display is the
# implementer-not-yet-called case the pair hop exists for. The default
# call parse only names fmt|debug, so today's hop records no pair.
# Computed 2026-09-09 on run 3985224-5484d810: the filled Debug dispatcher
# body was a Qual::display call and `_format_impl_calls` on it was empty.
_DISP_REC_VIA_DISPLAY = {
    **_DISP_REC,
    "body_text": "\n        Inner::display(self.inner.by_ref(), f)\n    ",
}


def test_debug_dispatcher_calling_display_pairs_the_debug_implementer() -> None:
    """Plan: a Debug dispatcher whose body calls Qual::display (and not
    Qual::debug) still records a pair with Qual::debug, and
    slices_with_pair_hops then puts that debug site in a session that
    only held the dispatcher. Negative control: the same body through
    today's call-follower names no fmt|debug call, so the old hop
    recorded no pair and the session stayed dispatcher-only."""
    assert _format_impl_calls(_DISP_REC_VIA_DISPLAY["body_text"]) == frozenset()
    assert _debug_implementer_not_yet_called(
        "Debug", _DISP_REC_VIA_DISPLAY["body_text"]) == {("Inner", "debug")}
    pairs: set[FormatterPair] = set()
    frontier: dict = {}
    sites: dict = {}
    _attribute_format_only_test(
        "t_wrap", _TSRC, _TSRC,
        {"fmt": [_DISP_REC_VIA_DISPLAY], "debug": [_IMPL_REC]},
        frontier, sites, log=lambda _m: None, pairs_out=pairs)
    assert len(pairs) == 1, pairs
    pair = next(iter(pairs))
    assert pair.implementer.fn_name == "debug"
    assert pair.implementer.file == "src/i.rs"
    owners = {"t_wrap": {canon_site(FnSite(
        "src/w.rs", "fmt", "impl Debug for Wrapper < T >"))}}
    hopped = slices_with_pair_hops(owners, [(pair.dispatcher, pair.implementer)])
    assert pair.implementer in hopped["t_wrap"]
    assert slices_with_pair_hops(owners, [])["t_wrap"] == frozenset(owners["t_wrap"])


def test_display_dispatcher_calling_display_does_not_synthesize_debug() -> None:
    """Negative control: a Display dispatcher that calls Qual::display is
    not the Debug misshape — no debug pair, the fmt itself is the
    implementer."""
    rec = {
        "file": "src/w.rs", "fn_name": "fmt",
        "container": "impl Display for Wrapper < T >",
        "body_text": "\n        Inner::display(self.inner.by_ref(), f)\n    ",
    }
    tsrc = (
        "#[test]\nfn t_wrap() {\n    let w: Wrapper<u8> = make();\n"
        "    let s = format!(\"{}\", w);\n    assert!(s.contains(\"x\"));\n}\n"
    )
    assert _debug_implementer_not_yet_called("Display", rec["body_text"]) == frozenset()
    pairs: set[FormatterPair] = set()
    frontier: dict = {}
    sites: dict = {}
    _attribute_format_only_test(
        "t_wrap", tsrc, tsrc,
        {"fmt": [rec], "debug": [_IMPL_REC],
         "display": [{**_IMPL_REC, "fn_name": "display"}]},
        frontier, sites, log=lambda _m: None, pairs_out=pairs)
    assert pairs == set(), pairs
    owners = {f"{k.file}::{k.fn_name}"
              for k, v in frontier.items() if "t_wrap" in v}
    assert owners == {"src/w.rs::fmt"}, owners


def test_debug_dispatcher_that_already_calls_debug_does_not_need_display() -> None:
    """Negative control: a Debug dispatcher that already calls Qual::debug
    is today's hop; a display call beside it does not change the pair."""
    rec = {
        **_DISP_REC,
        "body_text": (
            "\n        Inner::display(self.inner.by_ref(), f);\n"
            "        Inner::debug(self.inner.by_ref(), f)\n    "
        ),
    }
    assert _debug_implementer_not_yet_called("Debug", rec["body_text"]) == frozenset()
    pairs: set[FormatterPair] = set()
    frontier: dict = {}
    sites: dict = {}
    _attribute_format_only_test(
        "t_wrap", _TSRC, _TSRC,
        {"fmt": [rec], "debug": [_IMPL_REC]},
        frontier, sites, log=lambda _m: None, pairs_out=pairs)
    assert len(pairs) == 1, pairs
    assert next(iter(pairs)).implementer.fn_name == "debug"


# ---- locator-only attributions expand over the tree ------------------------

def test_locator_only_unit_expands_to_every_container_of_the_name() -> None:
    disp = FnSite("src/w.rs", "fmt", "impl Display for W")
    dbg = FnSite("src/w.rs", "fmt", "impl Debug for W")
    free = FnSite("src/w.rs", "run", "")
    tree = [disp, dbg, free]
    out = expand_locator_sites(
        {"t": {FnSite("src/w.rs", "fmt", "")},          # classifier-shaped
         "u": {FnSite("w.rs", "fmt", ""), dbg},          # extractor rooting + a real site
         "v": {FnSite("src/w.rs", "gone", "")}},         # not on the tree
        tree)
    assert out["t"] == frozenset({disp, dbg})
    assert out["u"] == frozenset({disp, dbg})
    assert out["v"] == frozenset({FnSite("src/w.rs", "gone", "")})
    # a locator-keyed map — the pre-fix shape — resolves the name to ONE record
    assert len({FnLocator(s.file, s.fn_name): s for s in (disp, dbg)}) == 1


def test_recorded_fmt_locator_does_not_admit_display_into_the_session() -> None:
    """A recorded owner is a (file, fn) with no container. Restamping it
    as FnSite(file, fn, "") made expand_locator_sites name every same-named
    body — Display re-entered and the merge exceeded the cap (run
    110993-6efa23be). Unique-container resolution keeps a one-site name
    and omits a homonym. Negative control: today's restamp still fans out."""
    disp = FnSite("src/w.rs", "fmt", "impl Display for W")
    dbg = FnSite("src/w.rs", "fmt", "impl Debug for W")
    run = FnSite("src/w.rs", "run", "impl W")
    tree = [disp, dbg, run]
    recorded = {
        "t_crash": {
            FnLocator("src/w.rs", "fmt"),
            FnLocator("src/w.rs", "run"),
        }
    }
    got = recorded_locator_sites(recorded, tree)
    assert got["t_crash"] == frozenset({run})
    assert disp not in got["t_crash"] and dbg not in got["t_crash"]
    slices = session_slices_for_failing(
        ["t_crash"], owners=got, units_by_test={}, pairs=())
    assert slices["t_crash"] == frozenset({run})
    old = {"t_crash": frozenset({
        FnSite("src/w.rs", "fmt", ""),
        FnSite("src/w.rs", "run", ""),
    })}
    old_exp = expand_locator_sites(old, tree)
    assert disp in old_exp["t_crash"] and dbg in old_exp["t_crash"], (
        "negative control: the empty-container restamp fans out to Display")
    assert recorded_locator_sites(
        {"t": {FnLocator("src/w.rs", "gone")}}, tree) == {}
    assert recorded_locator_sites(
        {"t": {FnLocator("src/w.rs", "fmt")}},
        [FnSite("src/w.rs", "fmt", "")]) == {}, (
        "an empty-container frontier site is not a resolution")


def _eligible_as_source(eligible: dict, site_frontier: dict) -> dict:
    """The 238.5 loop: eligible is the source of units; a locator with no
    frontier site becomes locator-only. Negative control of the join."""
    from pipeline_core.domain.attribution import locator_of as _loc_of
    sites_by_loc: dict[FnLocator, list[FnSite]] = {}
    for site in site_frontier:
        sites_by_loc.setdefault(_loc_of(site), []).append(site)
    out: dict[str, set[FnSite]] = {}
    for loc, tests in eligible.items():
        named = sites_by_loc.get(FnLocator(loc[0], loc[1]), ())
        for t in tests:
            hit = [s for s in named if t in site_frontier.get(s, ())]
            for s in (hit or [FnSite(str(loc[0]), str(loc[1]), "")]):
                out.setdefault(str(t).rsplit("::", 1)[-1], set()).add(s)
    return out


def test_eligible_as_source_explodes_past_the_cap_the_join_does_not() -> None:
    """Coverage named one site per locator (the Debug impl). Eligible holds
    those locators with an empty site frontier — the owner-map admission
    never entered it. The old loop invents locator-only units, expansion
    re-admits Display, and plan_slices drops the group. The join keeps the
    coverage sites and the hop, under the cap, without Display.
    """
    hop = FnSite("src/w.rs", "debug", "impl W")
    tree = [hop]
    owners: dict[str, set[FnSite]] = {"t_pay": set()}
    eligible: dict[FnLocator, set[str]] = {}
    display_sites: set[FnSite] = set()
    first_debug: FnSite | None = None
    for i in range(5):
        path = f"src/m{i}.rs"
        disp = FnSite(path, "fmt", f"impl Display for T{i}")
        dbg = FnSite(path, "fmt", f"impl Debug for T{i}")
        other = FnSite(path, "fmt", f"impl Other for T{i}")
        tree.extend((disp, dbg, other))
        display_sites.add(disp)
        owners["t_pay"].add(dbg)
        eligible[FnLocator(path, "fmt")] = {"t_pay"}
        if first_debug is None:
            first_debug = dbg
    assert first_debug is not None
    pairs = [(first_debug, hop)]
    new_units = attributed_units_for_session(eligible, {})
    new_slices = session_slices_for_failing(
        ["t_pay", "ui"], owners=owners, units_by_test=new_units, pairs=pairs)
    new_exp = expand_locator_sites(new_slices, tree)
    new_plans = plan_slices(new_exp, max_units=8)
    assert len(new_plans) == 1
    assert not (display_sites & set(new_plans[0].units))
    assert hop in new_plans[0].units
    old_units = _eligible_as_source(eligible, {})
    old_slices = session_slices_for_failing(
        ["t_pay", "ui"], owners=owners, units_by_test=old_units, pairs=pairs)
    old_exp = expand_locator_sites(old_slices, tree)
    assert plan_slices(old_exp, max_units=8) == []
    assert display_sites & old_exp["t_pay"]
    gaps = slice_plan_gaps(old_exp, max_units=8)
    assert gaps.over_cap == 1
    assert gaps.over_cap_units > 8
    assert gaps.empty == 1


# ---- cfg twins never raise ------------------------------------------------

def _rec(pred: str, body: str) -> FnRecord:
    return FnRecord(file="src/lib.rs", fn_name="go", container="impl T",
                    body_text=body, cfg_predicates=(pred,))


def test_cfg_twin_keys_the_arm_the_cluster_compiles() -> None:
    on, off = _rec('feature = "x"', " 1 "), _rec('not (feature = "x")', " 2 ")
    cs = CfgSet.of(cfg_true=['feature = "x"'])
    found = records_by_site(
        [off, on],
        compiled=lambda r: not any(cs.excludes_for_cargo(p) for p in r.cfg_predicates))
    assert found.by_site[record_site(on)].body_text == " 1 "
    assert found.collisions == ()


def test_undecidable_twin_keeps_the_first_declaration_and_reports_it() -> None:
    """The pre-fix map raised ValueError on this input and aborted the fill
    phase; the tree's ambiguity is reported, the first declaration keys."""
    a, b = _rec("unix", " a "), _rec("not (unix)", " b ")
    cs = CfgSet.of()
    found = records_by_site(
        [a, b],
        compiled=lambda r: not any(cs.excludes_for_cargo(p) for p in r.cfg_predicates))
    assert found.by_site[record_site(a)].body_text == " a "
    assert found.collisions == ((record_site(a), ("unix", "not (unix)")),)


# ---- events and lifted maps -----------------------------------------------

def test_lift_sites_reads_bare_tuple_keys() -> None:
    lifted = lift_sites({("lib.rs", "crashy"): {"t"}, FnLocator("a.rs", "f"): {"u"}})
    assert lifted == {FnSite("lib.rs", "crashy", ""): {"t"}, FnSite("a.rs", "f", ""): {"u"}}


def test_site_payload_round_trips_and_legacy_strings_read_as_locators() -> None:
    site = FnSite("src/error.rs", "fmt", "impl fmt::Debug for Error")
    payload = site_payload(site)
    assert payload == {"file": "src/error.rs", "fn": "fmt",
                       "container": "impl fmt::Debug for Error"}
    assert site_from_payload(payload) == canon_site(site)
    assert site_from_payload("src/error.rs::fmt") == FnSite("src/error.rs", "fmt", "")


def test_packing_an_over_union_set_does_not_readmit_a_site_the_join_excluded() -> None:
    """The over-union variant of the join: many coverage-Debug tests, each
    fitting the cap, whose union does not; eligible would explode Display.
    Packed sessions still contain no Display site — packing does not
    invent units, it only groups the slices the join already produced."""
    hop = FnSite("src/w.rs", "debug", "impl W")
    tree = [hop]
    owners: dict[str, set[FnSite]] = {}
    eligible: dict[FnLocator, set[str]] = {}
    display_sites: set[FnSite] = set()
    hub = FnSite("src/hub.rs", "construct", "impl Hub")
    tree.append(hub)
    for i in range(6):
        path = f"src/m{i}.rs"
        disp = FnSite(path, "fmt", f"impl Display for T{i}")
        dbg = FnSite(path, "fmt", f"impl Debug for T{i}")
        tree.extend((disp, dbg))
        display_sites.add(disp)
        t = f"t_pay_{i}"
        owners[t] = {dbg, hub}
        eligible[FnLocator(path, "fmt")] = {t}
    pairs = [(next(iter(owners["t_pay_0"] - {hub})), hop)]
    new_units = attributed_units_for_session(eligible, {})
    new_slices = session_slices_for_failing(
        list(owners), owners=owners, units_by_test=new_units, pairs=pairs)
    new_exp = expand_locator_sites(new_slices, tree)
    # Every test fits a cap of 4 (hub + its Debug site, + the hop for one);
    # the union (hub + 6 Debug + hop = 8) does not.
    assert all(len(u) <= 4 for u in new_exp.values())
    assert len(frozenset().union(*new_exp.values())) > 4
    plans = plan_slices(new_exp, max_units=4)
    assert len(plans) >= 2
    assert sorted(t for p in plans for t in p.tests) == sorted(owners)
    for p in plans:
        assert not (display_sites & set(p.units))
        assert hub in p.units
    # Control: the eligible-as-source loop still explodes and is still refused
    # — a single test over the cap is not packable.
    old_units = _eligible_as_source(eligible, {})
    old_slices = session_slices_for_failing(
        list(owners), owners=owners, units_by_test=old_units, pairs=pairs)
    old_exp = expand_locator_sites(old_slices, tree)
    assert any(display_sites & u for u in old_exp.values())
