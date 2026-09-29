"""The pass audit: dispatched is not tried, and the traversal says which.

The cursor (`domain/tdd_floor.py`) marks a unit ATTEMPTED at dispatch, and
every completed-pass reading silently upgrades that to TRIED. The pass that
closed the 2026-08-19 escalation crawl dispatched byte-identical re-authors
(`src/error.rs::source` — a traversal step — plus three `construct_from_*`
producers), peeled fills that left the old body in place (the severed
`src/context.rs::source`), and immutable-by-rule units, for roughly half its
denominator — so a flat sweep after "43/43" refuted the dispatch for barely
half the units it claimed. `domain/landed_change.py` measures the landed
fact per batch from extractor records; the driver accumulates it per pass,
persists it with the cursor, and reports it when the traversal closes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.phase_c_tdd import (
    PhaseCTddDeps,
    run_phase_c_tdd,
)
from pipeline_core.domain.attribution import AttributionResult, FailureKind, key_as_site
from pipeline_core.domain.landed_change import (
    changed_units,
    declined_unchanged,
    locator_has_convicting,
    render_pass_audit,
    split_reverted,
    unattempted_units,
    unit_bodies,
)
from pipeline_core.domain.models import BuildCluster, ClusterFillResult, FnLocator, FnSite
from pipeline_core.domain.stub_inventory import StubInventory

# ---------------------------------------------------------------------------
# Domain
# ---------------------------------------------------------------------------

_K = FnLocator("src/context.rs", "source")
_K2 = FnLocator("src/error.rs", "erase")


def test_unit_bodies_spans_every_same_named_occurrence() -> None:
    """`src/context.rs::source` is TWO impls and the escalation's unit is the
    pair — a change to either occurrence is a change to the unit."""
    recs = [
        {"file": "src/context.rs", "fn_name": "source", "body_text": "None"},
        {"file": "src/context.rs", "fn_name": "source",
         "body_text": "Some(&self.error)"},
        {"file": "src/error.rs", "fn_name": "erase", "body_text": "x"},
        {"file": "src/other.rs", "fn_name": "unwanted", "body_text": "y"},
    ]
    idx = unit_bodies(recs, [_K, _K2])
    assert idx[_K] == ("None", "Some(&self.error)")
    assert idx[_K2] == ("x",)
    assert FnLocator("src/other.rs", "unwanted") not in idx


def test_changed_units_is_whitespace_insensitive_and_total() -> None:
    """A re-author that moved only whitespace re-landed the same body — the
    recorded non-change (`construct_from_std` "changed whitespace only") must
    not read as a landed change. A unit that appeared or vanished did change."""
    recs_a = [{"file": "src/context.rs", "fn_name": "source",
               "body_text": "None"}]
    recs_b = [{"file": "src/context.rs", "fn_name": "source",
               "body_text": "  None\n"}]
    recs_c = [{"file": "src/context.rs", "fn_name": "source",
               "body_text": "Some(&self.error)"}]
    a = unit_bodies(recs_a, [_K])
    assert changed_units(a, unit_bodies(recs_b, [_K])) == frozenset()
    assert changed_units(a, unit_bodies(recs_c, [_K])) == {_K}
    # missing on one side = changed
    assert changed_units(a, unit_bodies([], [_K])) == {_K}


def test_split_reverted_keeps_identical_reply_flat_and_drops_the_peel() -> None:
    """The body comparison cannot see a peel: the 2026-08-19 crawl's
    `src/context.rs::source` sat in `frontier - landed` next to byte-identical
    constructors. FLAT is the identical-reply subset; the fill result's
    reverted set is what splits them."""
    constructors = frozenset(
        FnLocator("src/error.rs", n)
        for n in ("construct", "construct_from_std"))
    peeled = FnLocator("src/context.rs", "source")
    flat, reverted = split_reverted(constructors | {peeled}, [peeled])
    assert flat == constructors
    assert reverted == frozenset({peeled})
    # NEGATIVE CONTROL: no reverted set → the old union, every unchanged
    # escalated unit would have been written FLAT.
    all_flat, none = split_reverted(constructors | {peeled}, ())
    assert all_flat == constructors | {peeled} and not none


def test_unattempted_units_drops_a_landed_passenger() -> None:
    """A whole-file answer can rewrite a frontier unit the route never
    opened. That unit is landed, not unattempted. The pre-fix expression
    (`frontier - attempted`) still names it — that is the control."""
    depth = FnLocator("src/a.rs", "depth")
    width = FnLocator("src/a.rs", "width")
    height = FnLocator("src/a.rs", "height")
    frontier = frozenset({depth, width, height})
    attempted = frozenset({depth, width})
    landed = frozenset({height})
    assert unattempted_units(frontier, attempted, landed) == frozenset()
    # NEGATIVE CONTROL: the expression before landed was subtracted.
    assert frontier - attempted == frozenset({height})
    # An unopened unit that did NOT move stays unattempted — the A3 line.
    assert unattempted_units(frontier, attempted, frozenset()) == frozenset({height})


def test_declined_unchanged_unions_sites_not_locator_get() -> None:
    """Live conviction is FnSite-keyed. ``dict.get(FnLocator)`` on that map
    is None, which is how 59a314c filed every unchanged non-escalated unit
    declined. A site that convicts its locator must NOT be declined; a
    locator with no site must be."""
    loc = FnLocator("src/error.rs", "from")
    site = FnSite("src/error.rs", "from", "impl From<E>for Error")
    other = FnLocator("src/error.rs", "drop")
    sites = {site: {"test_from"}}
    unchanged = frozenset({loc, other})
    # NEGATIVE CONTROL: the miss this helper exists to close.
    assert sites.get(loc) is None  # type: ignore[arg-type]
    assert not locator_has_convicting(sites, other)
    assert locator_has_convicting(sites, loc)
    declined = declined_unchanged(
        unchanged, convicting=sites, escalated=frozenset())
    assert declined == frozenset({other})
    # An escalated unconvicted unit is a failed rewrite, not a decline.
    assert declined_unchanged(
        unchanged, convicting=sites, escalated=frozenset({other})) == frozenset()


def test_render_pass_audit_names_the_unchanged_units() -> None:
    """The unchanged units are the finding — the reader deciding "genuine
    plateau" needs their names, because the flat verdict does not cover them."""
    line = render_pass_audit(
        cohort_size=43, attempted=40, landed=frozenset({_K2}),
        unchanged=frozenset({_K}))
    assert "cohort 43" in line and "40 dispatched" in line
    assert "1 landed a semantic change" in line
    assert "src/context.rs::source" in line
    assert "landed subset" in line
    # nothing unchanged → no name list, and the verdict clause stays
    clean = render_pass_audit(cohort_size=2, attempted=2,
                              landed=frozenset({_K, _K2}),
                              unchanged=frozenset())
    assert "NO landed change" not in clean


# ---------------------------------------------------------------------------
# Driver — the audit reaches the log, the events, and the persisted floor
# ---------------------------------------------------------------------------

_FRONTIER = [FnLocator("src/a.rs", f"fn{i}") for i in range(30)]

_FLAT = ("    Running tests/t.rs (t/t-1)\n"
         "running 4 tests\n"
         "test t1 ... ok\n"
         "test t2 ... FAILED\ntest t3 ... FAILED\ntest t4 ... FAILED\n"
         "test result: FAILED. 1 passed; 3 failed")


def _cluster() -> BuildCluster:
    return BuildCluster(
        name="stable", env_extra={}, tarpaulin_exclude_files=[],
        excluded_cfg=[], active_cfg=[], prompt_nightly_rule="")


def _run_audited(tmp_path: Path, monkeypatch, *, cap: str,
                 ) -> tuple[list[str], list[tuple[str, dict]], list[dict]]:
    """A flat capped run whose fills change exactly ONE unit's body (`fn0`)
    per fill pass — the other 29 dispatched units land nothing, which is the
    dispatched-but-unchanged population the audit exists to name."""
    logs: list[str] = []
    emits: list[tuple[str, dict]] = []
    floors: list[dict] = []
    fills: list[int] = []

    def _fill(**kw: Any) -> ClusterFillResult:
        fills.append(1)
        return ClusterFillResult(attempts=1)

    def _enumerate(_src: Path) -> list[dict[str, Any]]:
        # fn0's body moves with every fill; every other body never does.
        return ([{"file": "src/a.rs", "fn_name": "fn0",
                  "body_text": f"v{len(fills)}"}]
                + [{"file": "src/a.rs", "fn_name": f"fn{i}",
                    "body_text": "same"} for i in range(1, 30)])

    deps = PhaseCTddDeps(
        attribute_failures=lambda *a, **k: AttributionResult(
            {f: {"t2"} for f in _FRONTIER}, FailureKind.RUNTIME,
            {key_as_site(f): {"t2"} for f in _FRONTIER}),
        build_test_reverse_map=lambda *a, **k: {},
        filter_test_targets=lambda frontier, src, **_k: frontier,
        check_graceful_stop=lambda *a, **k: None,
        check_auto_operator=lambda *a, **k: None,
        converge_in_cluster=lambda *a, **k: None,
        count_relevant_notes=lambda *a, **k: 0,
        effective_stalls=lambda budget, module: budget,
        emit=lambda event, **k: emits.append((event, k.get("data") or {})),
        log=logs.append,
        order_frontier=lambda eligible, counts: list(eligible.items()),
        record_tdd_regression=lambda *a, **k: "",
        reset_exhaustion_flags=lambda *a, **k: [],
        run_cargo=lambda *a, **k: (101, _FLAT),
        scan_unimplemented_for_cluster=lambda *a, **k: StubInventory(),
        scan_unimplemented_functions=lambda *a, **k: StubInventory(),
        task2_fill_fns_one_by_one=_fill,
        enumerate_fns=_enumerate,
        load_tdd_floor=lambda *a, **k: None,
        save_tdd_floor=lambda p, c, row: floors.append(row),
    )
    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "0")
    monkeypatch.setenv("TDD_MAX_STALLS", "3")
    monkeypatch.setenv("TDD_FLAT_SCORE_REPEATS", "0")
    monkeypatch.setenv("TDD_REFILL_MAX_ATTEMPTS", "99")
    monkeypatch.setenv("PHASE_C_TDD_MAX_DISPATCH", cap)
    run_phase_c_tdd(
        deps, project_dir=tmp_path, target_src_dir=tmp_path,
        tests_dir_files=[], cluster_tests=[("tests/t.rs", "fn t1() {}")],
        module="m", cluster=_cluster(), check_cmd="cargo check",
        cluster_env={}, leaf_counts=None, fr_path=None,
        test_cmd_builder=lambda stems, include_lib=False: "cargo test",
        tag="[stable]")
    return logs, emits, floors


def test_pass_audit_reports_effective_coverage_not_dispatch(
        tmp_path, monkeypatch) -> None:
    """A completed pass must state its LANDED coverage, per unit — the
    denominator the post-pass flat verdict actually applies to. Pre-audit,
    the only per-pass statement was the cursor's dispatch count, which is the
    aggregate misread one level up from the sweep-level one."""
    logs, emits, floors = _run_audited(tmp_path, monkeypatch, cap="10")
    audits = [d for e, d in emits if e == "phase_c.tdd.pass_audit"]
    assert audits, "a completed traversal must emit its audit"
    first = audits[0]
    assert first["cohort_size"] == 30
    assert first["landed"] == ["src/a.rs::fn0"]
    assert len(first["unchanged"]) == 29
    assert any("PASS AUDIT" in ln and "1 landed a semantic change" in ln
               for ln in logs)
    # The per-batch reading exists too, and names the unchanged units.
    batch = [d for e, d in emits if e == "phase_c.tdd.landed_change"]
    assert batch and batch[0]["dispatched"] == 10
    # The floor carries the landed record WITH the cursor: some mid-pass save
    # holds a non-empty cursor and the landed unit beside it.
    midpass = [f for f in floors if f.get("attempted")]
    assert midpass, "a capped flat run must persist mid-pass cursors"
    assert any(f.get("landed") == ["src/a.rs::fn0"] for f in midpass), (
        "the landed record must persist with the cursor, or the crawl's "
        "pass closes knowing only its last batch's landing")


def test_audit_is_skipped_never_guessed_without_the_extractor(
        tmp_path, monkeypatch) -> None:
    """`enumerate_fns=None` (legacy/stub engines) must not fabricate an
    all-unchanged audit — no audit at all is the honest reading."""
    logs: list[str] = []
    emits: list[tuple[str, dict]] = []

    deps = PhaseCTddDeps(
        attribute_failures=lambda *a, **k: AttributionResult(
            {f: {"t2"} for f in _FRONTIER}, FailureKind.RUNTIME,
            {key_as_site(f): {"t2"} for f in _FRONTIER}),
        build_test_reverse_map=lambda *a, **k: {},
        filter_test_targets=lambda frontier, src, **_k: frontier,
        check_graceful_stop=lambda *a, **k: None,
        check_auto_operator=lambda *a, **k: None,
        converge_in_cluster=lambda *a, **k: None,
        count_relevant_notes=lambda *a, **k: 0,
        effective_stalls=lambda budget, module: budget,
        emit=lambda event, **k: emits.append((event, k.get("data") or {})),
        log=logs.append,
        order_frontier=lambda eligible, counts: list(eligible.items()),
        record_tdd_regression=lambda *a, **k: "",
        reset_exhaustion_flags=lambda *a, **k: [],
        run_cargo=lambda *a, **k: (101, _FLAT),
        scan_unimplemented_for_cluster=lambda *a, **k: StubInventory(),
        scan_unimplemented_functions=lambda *a, **k: StubInventory(),
        task2_fill_fns_one_by_one=lambda **kw: ClusterFillResult(attempts=1),
    )
    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "0")
    monkeypatch.setenv("TDD_MAX_STALLS", "3")
    monkeypatch.setenv("TDD_FLAT_SCORE_REPEATS", "0")
    monkeypatch.setenv("PHASE_C_TDD_MAX_DISPATCH", "10")
    run_phase_c_tdd(
        deps, project_dir=tmp_path, target_src_dir=tmp_path,
        tests_dir_files=[], cluster_tests=[("tests/t.rs", "fn t1() {}")],
        module="m", cluster=_cluster(), check_cmd="cargo check",
        cluster_env={}, leaf_counts=None, fr_path=None,
        test_cmd_builder=lambda stems, include_lib=False: "cargo test",
        tag="[stable]")
    assert not any(e == "phase_c.tdd.pass_audit" for e, _ in emits)
    assert not any("PASS AUDIT" in ln for ln in logs)


def test_render_pass_audit_keeps_inferred_unchanged_out_of_the_failed_tries() -> None:
    """A unit no test convicts was told to return a right body unchanged
    (`[REFILL?]`), so its "unchanged" is the instructed outcome — reported
    apart from the byte-identical/peeled/immutable failed tries, and not
    counted among them."""
    line = render_pass_audit(
        cohort_size=43, attempted=40, landed=frozenset({_K2}),
        unchanged=frozenset(), declined=frozenset({_K}))
    assert "NO landed change" not in line
    assert "1 INFERRED re-fill(s) unchanged" in line
    assert "not a failed try" in line and "src/context.rs::source" in line
    both = render_pass_audit(
        cohort_size=3, attempted=3, landed=frozenset(),
        unchanged=frozenset({_K2}), declined=frozenset({_K}))
    assert "1 dispatched with NO landed change" in both
    assert "src/error.rs::erase" in both.split("INFERRED", 1)[0]
    assert "src/context.rs::source" in both.split("INFERRED", 1)[1]


def test_render_pass_audit_names_unopened_units_apart_from_failed_tries() -> None:
    """A unit no route opened is not a try: the line names it under its own
    clause, and the failed-try clause does not carry it."""
    line = render_pass_audit(
        cohort_size=3, attempted=3, landed=frozenset(),
        unchanged=frozenset({_K2}), unopened=frozenset({_K}))
    assert "1 dispatched but NO route opened them" in line
    _, _, rest = line.partition("NO route opened them")
    assert "src/context.rs::source" in rest.split("; 1 dispatched with NO")[0]
    assert "1 dispatched with NO landed change" in line
    assert render_pass_audit(cohort_size=1, attempted=1, landed=frozenset(),
                             unchanged=frozenset()).count("NO route") == 0


def test_pass_unopened_never_claims_a_resumed_cursors_units() -> None:
    from pipeline_core.domain.landed_change import pass_unopened
    got = pass_unopened({_K, _K2}, resumed={_K2}, opened=set(), landed=set())
    assert got == frozenset({_K})
    assert pass_unopened({_K, _K2}, resumed=set(), opened={_K},
                         landed={_K2}) == frozenset()
