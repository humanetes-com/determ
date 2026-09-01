"""The ledger inside the real driver: it writes at a landing, judges at the
next sweep, and re-seeds at cluster entry.

The unit tests (`test_body_ledger.py`) pin the rules and `test_body_reseed.py`
pins the transaction; this module pins that `run_phase_c_tdd` actually reaches
them — the half that decides whether any of it runs on a live tree at all.

Every assertion here has its control in the same test: the identical run with
the ledger deps unwired, which must reproduce the pre-ledger behaviour (a
cluster entry that starts from stubs, and a landing nobody records).
"""
from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from pipeline_core.adapters.oracle_fingerprint import cluster_oracle_fp
from pipeline_core.domain.body_ledger import (
    BodyState,
    LedgerEntry,
    LedgerKey,
    authored_layout_fp,
    body_sha,
    sig_sha_of,
)
from pipeline_core.domain.models import BuildCluster, FnLocator
from tests.test_tdd_ratchet import _driver_deps

_LAYOUT = """\
pub struct Own { pub ptr: usize }

impl Own {
    pub fn depth(&self) -> usize {
        unimplemented!()
    }
}
"""
# A filled accessor and a filled producer: the shape an escalation needs (an
# accessor that is NOT a stub, and a producer the escalation names).
_LAYOUT_TWO = """\
pub struct Own { pub ptr: usize }

impl Own {
    pub fn depth(&self) -> usize {
        self.ptr
    }

    pub fn width(&self) -> usize {
        self.ptr * 2
    }
}
"""
_BODY = "\n        self.ptr\n    "
_UNIT = FnLocator("src/a.rs", "depth")
_PROD = FnLocator("src/a.rs", "width")
_SWEEP = ("test t1 ... ok\ntest t2 ... FAILED\n"
          "test result: FAILED. 1 passed; 1 failed; 0 ignored")
_FN = re.compile(r"pub fn (\w+)\(&self\) -> usize \{(.*?)\}", re.S)


def _tree(tmp_path: Path, layout: str = _LAYOUT) -> Path:
    """A cluster with its authored layout (`.skeleton/stable/`) and the filled
    tree the loop scores — the pairing rule the whole gate rests on. Both are
    crate roots holding `src/a.rs`, which is how the extractor names the file
    and therefore how the ledger keys it."""
    for rel in (".skeleton/stable/src", "src"):
        d = tmp_path / rel
        d.mkdir(parents=True, exist_ok=True)
        (d / "a.rs").write_text(layout, encoding="utf-8")
    return tmp_path / "src"


def _oracle(tmp_path: Path) -> str:
    """The oracle of the test tree as it stands, through the REAL adapter —
    the same call the driver's dep makes (`_driver_deps` wires it)."""
    return cluster_oracle_fp(tmp_path, tmp_path, "stable")


def _proven_row(tmp_path: Path) -> LedgerEntry:
    skel = tmp_path / ".skeleton" / "stable" / "src" / "a.rs"
    return LedgerEntry(
        # The project is the workspace directory's name — `tmp/anyhow` gives
        # `anyhow`, and here it is whatever pytest named the tmp dir. Keyed
        # per project on purpose: a body proven on `anyhow` is not offered to
        # `hardcrate`.
        key=LedgerKey(tmp_path.name, "stable", "src/a.rs", "depth"),
        body_sha=body_sha((_BODY,)), bodies=(_BODY,),
        sig_sha=sig_sha_of(("    pub fn depth(&self) -> usize",)),
        layout_fp=authored_layout_fp(skel.read_text(encoding="utf-8")),
        state=BodyState.PROVEN, oracle_fp=_oracle(tmp_path))


def _run(monkeypatch, tmp_path: Path, *, ledger: dict | None,
         events: list[tuple[str, dict]], logs: list[str],
         fills: int = 0, escalate=None, max_stalls: str = "1",
         stubbed: bool = True, layout: str = _LAYOUT,
         dispatched_fns: tuple[tuple[str, str], ...] | None = None,
         answer_lost_fns: tuple[tuple[str, str], ...] = (),
         fill_write: Any = None) -> None:
    from pipeline_core.application.reconstruct.phase_c_tdd import run_phase_c_tdd
    monkeypatch.setenv("TDD_MAX_STALLS", max_stalls)
    monkeypatch.setenv("TDD_FLAT_SCORE_REPEATS", "0")
    monkeypatch.setenv("TDD_NO_VERDICT_SWEEPS", "0")
    src = tmp_path / "src"

    def _cargo(*_a: Any, **_k: Any) -> tuple[int, str]:
        return 101, _SWEEP

    def _enumerate(_src: Any, **_kw: Any) -> list[dict[str, Any]]:
        text = (src / "a.rs").read_text(encoding="utf-8")
        return [{"file": "src/a.rs", "fn_name": name,
                 "sig_text": f"    pub fn {name}(&self) -> usize",
                 "body_text": body, "container": "impl Own"}
                for name, body in _FN.findall(text)]

    def _fill(**_kw: Any):
        from pipeline_core.domain.models import ClusterFillResult
        if fills:
            (src / "a.rs").write_text(
                layout.replace("\n        unimplemented!()\n    ", _BODY),
                encoding="utf-8")
        if fill_write is not None:
            fill_write(src)
        return ClusterFillResult(
            attempts=1, fns_filled=fills, dispatched_fns=dispatched_fns,
            answer_lost_fns=answer_lost_fns)

    stubs = [_UNIT] if stubbed else []
    run_phase_c_tdd(
        _driver_deps(
            _cargo, lambda pd, cl, stems, lib=False: (2, frozenset({"t1", "t2"})),
            events, None, frontier={_UNIT: {"t2"}}, logs=logs, fill=_fill,
            stubs=stubs, tree_stubs=stubs,
            reverse_map={"t2": {_UNIT}}, escalate=escalate,
            enumerate_fns=_enumerate, ledger=ledger),
        project_dir=tmp_path, target_src_dir=tmp_path, tests_dir_files=[],
        cluster_tests=[("tests/t.rs", "fn t1() {}")], module="m",
        cluster=BuildCluster(
            name="stable", env_extra={}, tarpaulin_exclude_files=[],
            excluded_cfg=[], active_cfg=[], prompt_nightly_rule=""),
        check_cmd="cargo check", cluster_env={}, leaf_counts=None, fr_path=None,
        test_cmd_builder=lambda stems, include_lib=False: "cargo test",
        tag="[stable]")


def test_cluster_entry_re_seeds_a_proven_body_and_the_control_starts_stubbed(
        monkeypatch, tmp_path: Path) -> None:
    """READ PATH 1, through the real driver. A fresh cluster gets the stub
    skeleton today; with the ledger it gets the stub skeleton plus every body
    the ledger can vouch for under the CURRENT layout. That is the difference
    between a from-scratch restart costing minutes and costing the fortnight
    the record measures."""
    src = _tree(tmp_path)
    ledger: dict = {"rows": [_proven_row(tmp_path)]}
    events: list[tuple[str, dict]] = []
    logs: list[str] = []
    _run(monkeypatch, tmp_path, ledger=ledger, events=events, logs=logs)

    assert "self.ptr" in (src / "a.rs").read_text(encoding="utf-8")
    assert any(ev == "phase_c.ledger.reseed" for ev, _ in events)
    line = next(ln for ln in logs if "[phase-c/ledger]" in ln
                and "cluster entry" in ln)
    assert "re-seeded 1 unit(s) (1 proven" in line
    assert "NOT build-checked" in line, (
        "with no checker wired the line must SAY the tree was not verified — "
        "an unchecked re-seed is the risk, and a silent one is the defect")

    # THE CONTROL: same run, ledger unwired. The body stays a stub, which is
    # what every cluster entry did before this entry existed.
    control = tmp_path / "control"
    (control / "src").mkdir(parents=True)
    _tree(control)
    _run(monkeypatch, control, ledger=None, events=[], logs=[])
    assert "unimplemented!()" in (control / "src" / "a.rs").read_text(
        encoding="utf-8")


def test_a_landing_is_recorded_and_the_next_sweep_judges_it(
        monkeypatch, tmp_path: Path) -> None:
    """THE WRITE PATH plus the promotion. The bodies were in hand at the
    landed-change site for the whole life of the pipeline and were thrown away
    there — `landed_change` emits the unit NAMES. One row per landed unit, and
    the sweep after it says what the verdict vector thinks."""
    _tree(tmp_path)
    ledger: dict = {"rows": []}
    events: list[tuple[str, dict]] = []
    logs: list[str] = []
    _run(monkeypatch, tmp_path, ledger=ledger, events=events, logs=logs,
         fills=1)

    rows: list[LedgerEntry] = ledger["rows"]
    assert rows, "a landed body must leave a row"
    landed = [r for r in rows if r.state is BodyState.LANDED]
    assert landed and landed[0].bodies == (_BODY,), (
        "the RAW body is stored — the folded form would splice a body whose "
        "`//` comment swallows its own remainder")
    assert landed[0].key.fn_name == "depth"
    judged = [r for r in rows if r.state is not BodyState.LANDED]
    assert judged, "the sweep after the landing must judge it"
    assert judged[0].state is BodyState.FLAT, (
        "t2 RAN and is red in both readings: nothing regressed and nothing "
        "passed, so this is a graded try with an empty delta — `flat`, not "
        "`proven` and not `unmeasured` (`unmeasured` claims the tests did "
        "not run)")
    assert any(ev == "phase_c.ledger.judged" for ev, _ in events)
    # Both the landing and its verdict carry the oracle that graded them —
    # the value the real adapter computes over this tree, never "".
    oracle = _oracle(tmp_path)
    assert oracle and landed[0].oracle_fp == oracle
    assert judged[0].oracle_fp == oracle
    assert any("[phase-c/ledger]" in ln and f"oracle {oracle}" in ln
               for ln in logs), "the cluster-entry line names the oracle"
    assert any("followup-think=off" in ln for ln in logs), (
        "the cluster-entry line names the followup-think off switch "
        "before any GPU is spent")
    assert any("fill-phase-stop=off" in ln for ln in logs), (
        "the cluster-entry line names the fill-phase clock off switch "
        "before any GPU is spent")

    # THE CONTROL: the identical run with the ledger unwired records nothing.
    control = tmp_path / "control"
    (control / "src").mkdir(parents=True)
    _tree(control)
    control_events: list[tuple[str, dict]] = []
    _run(monkeypatch, control, ledger=None, events=control_events, logs=[],
         fills=1)
    assert not any(ev.startswith("phase_c.ledger") for ev, _ in control_events)


def test_a_backfilled_row_that_matches_the_tree_is_adopted_and_judged(
        monkeypatch, tmp_path: Path) -> None:
    """Without adoption the backfill is INERT and silently so. A row indexed
    from an archive or from the live tree enters as `landed` — correctly,
    because promotion needs a verdict measured on THIS code — and the
    promotion pass only judges rows the process itself wrote. So the 465 rows
    the backfill indexed would never have reached a re-seedable state, and the
    ledger would have paid only for bodies the loop re-landed itself, which is
    the case it is least needed for."""
    src = _tree(tmp_path)
    # The body IS on the tree: a filled unit, indexed by the backfill.
    src_file = src / "a.rs"
    src_file.write_text(_LAYOUT.replace("\n        unimplemented!()\n    ",
                                        _BODY), encoding="utf-8")
    # A backfilled row carries NO oracle (nobody recorded one); the sweep
    # that judges it stamps the oracle it was judged under.
    row = replace(_proven_row(tmp_path), state=BodyState.LANDED, oracle_fp="")
    ledger: dict = {"rows": [row]}
    logs: list[str] = []
    _run(monkeypatch, tmp_path, ledger=ledger, events=[], logs=logs)

    assert any("backfilled body(ies) are the bodies on this tree" in ln
               for ln in logs)
    judged = [r for r in ledger["rows"] if r.state is not BodyState.LANDED]
    assert judged, "the first sweep must judge an adopted row"
    assert judged[0].oracle_fp == _oracle(tmp_path) != ""

    # THE CONTROL: a stored body that is NOT the body on the tree is not
    # adopted — it cannot be what the sweep is measuring.
    other = tmp_path / "other"
    (other / "src").mkdir(parents=True)
    _tree(other)
    other_ledger: dict = {"rows": [replace(_proven_row(other),
                                           state=BodyState.LANDED)]}
    other_logs: list[str] = []
    _run(monkeypatch, other, ledger=other_ledger, events=[], logs=other_logs)
    assert not any("backfilled body(ies) are the bodies" in ln
                   for ln in other_logs)


def test_a_moved_layout_leaves_the_cluster_entry_stubbed(
        monkeypatch, tmp_path: Path) -> None:
    """The retrigger's intent, preserved. t1.5 re-authors the layout on every
    run, which is why bodies from one run do not compile against another's
    skeleton (`ErrorImpl` lost `pub(crate) backtrace` between `1f4099b` and the
    tree that replaced it). A moved layout must refuse."""
    src = _tree(tmp_path)
    row = _proven_row(tmp_path)
    skel = tmp_path / ".skeleton" / "stable" / "src" / "a.rs"
    skel.write_text(_LAYOUT.replace("pub ptr: usize", "pub raw: usize"),
                    encoding="utf-8")
    logs: list[str] = []
    _run(monkeypatch, tmp_path, ledger={"rows": [row]}, events=[], logs=logs)
    assert "unimplemented!()" in (src / "a.rs").read_text(encoding="utf-8")
    assert any("skipped 1 (layout moved)" in ln for ln in logs)


_GEN_TEST = ("#[test]\nfn t2() { assert_eq!(Own { ptr: 1 }.depth(), 2); }\n")


def test_a_moved_oracle_leaves_the_cluster_entry_stubbed(
        monkeypatch, tmp_path: Path) -> None:
    """The plan's sentence through the real driver and the real adapter. The
    layout is byte-identical and the signature matches; what moved is a
    `gen_tests` sibling in the tree's `src/` — the cold run's regeneration in
    miniature — and the body proven under the old tests stays a stub. The
    control is the same tree with the row stamped under the NEW oracle, which
    re-seeds."""
    src = _tree(tmp_path)
    row = _proven_row(tmp_path)          # graded under the tree WITHOUT the sibling
    (src / "a.gen_tests_1.rs").write_text(_GEN_TEST, encoding="utf-8")
    moved = _oracle(tmp_path)
    assert moved != row.oracle_fp, "a new sibling is a new oracle"
    logs: list[str] = []
    _run(monkeypatch, tmp_path, ledger={"rows": [row]}, events=[], logs=logs)
    assert "unimplemented!()" in (src / "a.rs").read_text(encoding="utf-8")
    assert any("skipped 1 (oracle moved)" in ln for ln in logs)

    # A row from before the field (no oracle at all) is refused the same way.
    blank = tmp_path / "blank"
    (blank / "src").mkdir(parents=True)
    bsrc = _tree(blank)
    blogs: list[str] = []
    _run(monkeypatch, blank, ledger={"rows": [replace(_proven_row(blank),
                                                       oracle_fp="")]},
         events=[], logs=blogs)
    assert "unimplemented!()" in (bsrc / "a.rs").read_text(encoding="utf-8")
    assert any("skipped 1 (oracle moved)" in ln for ln in blogs)

    # THE CONTROL: same sibling on the tree, row graded under THAT oracle.
    control = tmp_path / "control"
    (control / "src").mkdir(parents=True)
    csrc = _tree(control)
    (csrc / "a.gen_tests_1.rs").write_text(_GEN_TEST, encoding="utf-8")
    crow = _proven_row(control)          # `_oracle` now sees the sibling
    assert crow.oracle_fp == _oracle(control)
    clogs: list[str] = []
    _run(monkeypatch, control, ledger={"rows": [crow]}, events=[], logs=clogs)
    assert "self.ptr" in (csrc / "a.rs").read_text(encoding="utf-8")
    assert any("re-seeded 1 unit(s) (1 proven" in ln for ln in clogs)


def _escalate_to_width(_src: Any, tname: str, _units: Any,
                       producers_out: Any = None) -> dict[FnLocator, set[str]]:
    if producers_out is not None:
        producers_out.add(_PROD)
    return {_PROD: {tname}}


def test_an_escalated_unit_that_came_back_unchanged_leaves_a_flat_row(
        monkeypatch, tmp_path: Path) -> None:
    """THE FLAT ARM, through the real escalation. The accessor `depth` is
    filled and its test keeps failing, so the futility escalation names the
    producer `width`; the rewrite returns it byte-identical. Before this arm
    the audit logged `ESCALATED, unchanged` and dropped it — the nine
    `src/error.rs` constructors of run `82157-476ddc16` iter 1 — so no
    process after could tell the graded try from a unit never dispatched.
    Now: one `flat` row with the CURRENT body, stamped like a landing, and
    the event names the set under its own key. The accessor, unchanged under
    the REFILL grade, still writes no row: the grade is what the arm keys on.

    The negative control injects the pre-change writer (no FLAT arm) into
    the same replay: zero rows for `width`, while the event still names it."""
    _tree(tmp_path, layout=_LAYOUT_TWO)
    ledger: dict = {"rows": []}
    events: list[tuple[str, dict]] = []
    logs: list[str] = []
    _run(monkeypatch, tmp_path, ledger=ledger, events=events, logs=logs,
         escalate=_escalate_to_width, max_stalls="3", stubbed=False,
         layout=_LAYOUT_TWO)
    assert any(ev == "phase_c.attribution.escalated" for ev, _ in events), (
        "the replay must reach the escalation for the arm to be exercised")
    rows: list[LedgerEntry] = ledger["rows"]
    flat = [r for r in rows if r.state is BodyState.FLAT]
    named = [d for ev, d in events if ev == "phase_c.tdd.landed_change"
             and d.get("esc_unchanged")]
    assert named and named[0]["esc_unchanged"] == ["src/a.rs::width"]
    assert named[0]["iter"] == 2, "the escalation fires at iter 2"
    assert "src/a.rs::width" in named[0]["unchanged"], (
        "a subset of `unchanged`, now labelled")
    # ONE row per escalated-unchanged dispatch: the product stays in the
    # frontier while the evidence stands, so the recorded replay dispatches
    # it again at iter 3 and that second graded try is a second row —
    # append-only, exactly as a second landing would be.
    assert {r.key.fn_name for r in flat} == {"width"}
    assert [r.iteration for r in flat] == [d["iter"] for d in named]
    assert flat[0].iteration == 2
    assert flat[0].bodies == ("\n        self.ptr * 2\n    ",), (
        "the CURRENT body, raw — the one the rewrite returned unchanged")
    assert all(r.verdict_delta == () and r.layout_fp
               and r.oracle_fp == _oracle(tmp_path) != "" for r in flat)
    assert not [r for r in rows if r.key.fn_name == "depth"], (
        "unchanged under the REFILL grade is not a graded try: no row")
    assert any("recorded as `flat`" in ln for ln in logs)

    # NEGATIVE CONTROL: the pre-change writer — the shared constructor with
    # its FLAT arm removed — on the identical replay.
    import pipeline_core.application.reconstruct.phase_c_tdd as driver
    real = driver.rows_for_units

    def _no_flat_arm(units: Any, recs: Any, **kw: Any) -> list[LedgerEntry]:
        return [] if kw["state"] is BodyState.FLAT else real(units, recs, **kw)

    monkeypatch.setattr(driver, "rows_for_units", _no_flat_arm)
    control = tmp_path / "control"
    (control / "src").mkdir(parents=True)
    _tree(control, layout=_LAYOUT_TWO)
    c_ledger: dict = {"rows": []}
    c_events: list[tuple[str, dict]] = []
    _run(monkeypatch, control, ledger=c_ledger, events=c_events, logs=[],
         escalate=_escalate_to_width, max_stalls="3", stubbed=False,
         layout=_LAYOUT_TWO)
    assert not [r for r in c_ledger["rows"] if r.key.fn_name == "width"], (
        "the old writer leaves the graded try with no row")
    assert any(d.get("esc_unchanged") == ["src/a.rs::width"]
               for ev, d in c_events if ev == "phase_c.tdd.landed_change"), (
        "the event key is independent of the writer")


_LAYOUT_THREE = """\
pub struct Own { pub ptr: usize }

impl Own {
    pub fn depth(&self) -> usize {
        self.ptr
    }

    pub fn width(&self) -> usize {
        self.ptr * 2
    }

    pub fn height(&self) -> usize {
        self.ptr * 3
    }
}
"""
_HEIGHT = FnLocator("src/a.rs", "height")


def _escalate_to_width_and_height(_src: Any, tname: str, _units: Any,
                                 producers_out: Any = None) -> dict[FnLocator, set[str]]:
    if producers_out is not None:
        producers_out.add(_PROD)
        producers_out.add(_HEIGHT)
    return {_PROD: {tname}, _HEIGHT: {tname}}


def test_under_a3_only_an_attempted_escalated_unit_leaves_a_flat_row(
        monkeypatch, tmp_path: Path) -> None:
    """The plan's sentence through the real driver. Run `9933-efe68f37`
    iter 1 asked for 12, the file-first route attempted `{context, msg}`,
    `msg` landed, and the FLAT arm wrote 8 rows — 7 of them for units no
    route opened. Under A3 the analog is: three filled units, escalation
    names `width` and `height`, the engine reports `depth` and `width`
    attempted (`depth` so the attributed unit is served and escalation
    is not parked), bodies unchanged. Exactly one `flat` row, `width`.
    `height` is in the batch and escalated but no route opened it — the
    seven false FLAT rows of the live iter 1. The `attempted` field on
    `landed_change` is the write list so the next session does not
    re-derive it from `logs/prompts/`.

    NEGATIVE CONTROL: `dispatched_fns=None` (legacy/stub engine) still
    writes a `flat` row for both escalated units — the pre-fix set
    exactly, which is also what
    `test_legacy_engine_without_report_reproduces_the_old_burn` pins on
    the exhaustion path."""
    _tree(tmp_path, layout=_LAYOUT_THREE)
    ledger: dict = {"rows": []}
    events: list[tuple[str, dict]] = []
    logs: list[str] = []
    _run(monkeypatch, tmp_path, ledger=ledger, events=events, logs=logs,
         escalate=_escalate_to_width_and_height, max_stalls="3",
         stubbed=False, layout=_LAYOUT_THREE,
         dispatched_fns=(("src/a.rs", "depth"), ("src/a.rs", "width")))
    named = [d for ev, d in events if ev == "phase_c.tdd.landed_change"
             and d.get("esc_unchanged")]
    assert named, "escalation must fire and the FLAT arm must name a set"
    row = named[0]
    assert row["esc_unchanged"] == ["src/a.rs::width"]
    assert "src/a.rs::depth" in row["attempted"]
    assert "src/a.rs::width" in row["attempted"]
    assert "src/a.rs::height" not in row["attempted"]
    assert "src/a.rs::height" not in row["esc_unchanged"]
    assert "src/a.rs::height" not in row.get("unchanged", [])
    assert "src/a.rs::height" in row["unattempted"]
    assert {r.key.fn_name for r in ledger["rows"]
            if r.state is BodyState.FLAT} == {"width"}
    assert any("no route attempted" in ln and "height" in ln for ln in logs)

    control = tmp_path / "control"
    (control / "src").mkdir(parents=True)
    _tree(control, layout=_LAYOUT_THREE)
    c_ledger: dict = {"rows": []}
    c_events: list[tuple[str, dict]] = []
    _run(monkeypatch, control, ledger=c_ledger, events=c_events, logs=[],
         escalate=_escalate_to_width_and_height, max_stalls="3",
         stubbed=False, layout=_LAYOUT_THREE)
    c_named = [d for ev, d in c_events if ev == "phase_c.tdd.landed_change"
               and d.get("esc_unchanged")]
    assert c_named
    assert set(c_named[0]["esc_unchanged"]) == {
        "src/a.rs::width", "src/a.rs::height"}
    assert {r.key.fn_name for r in c_ledger["rows"]
            if r.state is BodyState.FLAT} == {"width", "height"}


def test_a_lost_answer_is_not_an_attempt_and_writes_no_flat_row(
        monkeypatch, tmp_path: Path) -> None:
    """The dispatch stamp is written before the model call, so a file-first
    reply that came back with nothing to route still reports its units
    dispatched — and grading them fabricates evidence. Run `347397-01e2463b`
    iter 1 (2026-08-31): src/ensure.rs replied with 0 FN tags, the audit
    graded `__dispatch_ensure` and `render` `ESCALATED, unchanged (a FAILED
    try)`, and `render` — an owner of the still-failing test_trailer — sat
    in the next two iterations' flat-sink derank on that fabricated `flat`
    row. Here the engine reports the loss (`answer_lost_fns`): `height` is
    dispatched AND lost, so it leaves `attempted`, is listed under `no route
    attempted`, and writes NO ledger row — while `width`, a unit the call
    genuinely opened and returned unchanged, still writes its `flat` row (a
    reply that merely omits a requested unit is a real failed try, which is
    what keeps the attempted-only audit cut intact).

    NEGATIVE CONTROL: the identical replay with the loss unreported writes
    `flat` rows for BOTH escalated units — the fabricated row exactly."""
    _tree(tmp_path, layout=_LAYOUT_THREE)
    ledger: dict = {"rows": []}
    events: list[tuple[str, dict]] = []
    logs: list[str] = []
    _run(monkeypatch, tmp_path, ledger=ledger, events=events, logs=logs,
         escalate=_escalate_to_width_and_height, max_stalls="3",
         stubbed=False, layout=_LAYOUT_THREE,
         dispatched_fns=(("src/a.rs", "depth"), ("src/a.rs", "width"),
                         ("src/a.rs", "height")),
         answer_lost_fns=(("src/a.rs", "height"),))
    named = [d for ev, d in events if ev == "phase_c.tdd.landed_change"
             and d.get("esc_unchanged")]
    assert named, "escalation must fire and the FLAT arm must name a set"
    row = named[0]
    assert row["esc_unchanged"] == ["src/a.rs::width"]
    assert "src/a.rs::width" in row["attempted"]
    assert "src/a.rs::height" not in row["attempted"], (
        "a lost answer is not an attempt")
    assert "src/a.rs::height" in row["unattempted"]
    assert {r.key.fn_name for r in ledger["rows"]
            if r.state is BodyState.FLAT} == {"width"}, (
        "the lost unit writes no row; the omitted-but-served unit still does")

    control = tmp_path / "control"
    (control / "src").mkdir(parents=True)
    _tree(control, layout=_LAYOUT_THREE)
    c_ledger: dict = {"rows": []}
    c_events: list[tuple[str, dict]] = []
    _run(monkeypatch, control, ledger=c_ledger, events=c_events, logs=[],
         escalate=_escalate_to_width_and_height, max_stalls="3",
         stubbed=False, layout=_LAYOUT_THREE,
         dispatched_fns=(("src/a.rs", "depth"), ("src/a.rs", "width"),
                         ("src/a.rs", "height")))
    assert {r.key.fn_name for r in c_ledger["rows"]
            if r.state is BodyState.FLAT} == {"width", "height"}, (
        "unreported, the lost unit is graded — the fabricated row")


def test_a_landed_passenger_is_not_reported_as_unattempted(
        monkeypatch, tmp_path: Path) -> None:
    """A whole-file answer can rewrite a frontier unit the route never
    targeted — a passenger of the write list. Its body moved, so the audit
    event records it as landed; a "no route attempted" row naming the same
    unit one field over would contradict that list inside one event. Here
    the engine reports `depth` and `width` attempted while the fill also
    rewrites `height`'s body: `height` must be in the landed list, not in
    `unattempted`, and the FLAT set is still exactly `width` (attempted,
    escalated, unchanged).

    NEGATIVE CONTROL: the pre-fix expression (`frontier - attempted`, no
    landed subtraction) is injected at the same call site. Height is then
    in both `landed` and `unattempted` of one event."""
    _tree(tmp_path, layout=_LAYOUT_THREE)
    ledger: dict = {"rows": []}
    events: list[tuple[str, dict]] = []
    logs: list[str] = []

    def _rewrite_height(src: Path) -> None:
        # Alternates so EVERY fill call moves the body — the escalated
        # audit fires on a later iteration than the first fill, and an
        # idempotent rewrite would land nothing by then.
        a = src / "a.rs"
        text = a.read_text(encoding="utf-8")
        text = (text.replace("self.ptr * 3", "self.ptr * 4")
                if "self.ptr * 3" in text
                else text.replace("self.ptr * 4", "self.ptr * 3"))
        a.write_text(text, encoding="utf-8")

    _run(monkeypatch, tmp_path, ledger=ledger, events=events, logs=logs,
         escalate=_escalate_to_width_and_height, max_stalls="3",
         stubbed=False, layout=_LAYOUT_THREE,
         dispatched_fns=(("src/a.rs", "depth"), ("src/a.rs", "width")),
         fill_write=_rewrite_height)
    named = [d for ev, d in events if ev == "phase_c.tdd.landed_change"
             and d.get("esc_unchanged")]
    assert named, "escalation must fire and the FLAT arm must name a set"
    row = named[0]
    assert "src/a.rs::height" in row["landed"]
    assert "src/a.rs::height" not in row["attempted"]
    assert "src/a.rs::height" not in row["unattempted"]
    assert row["esc_unchanged"] == ["src/a.rs::width"]
    assert not any("no route attempted" in ln and "height" in ln
                   for ln in logs)
    assert {r.key.fn_name for r in ledger["rows"]
            if r.state is BodyState.FLAT} == {"width"}

    # NEGATIVE CONTROL: the pre-fix expression, injected at the same
    # call site. Height is in unattempted and the log names it, while
    # landed still lists it — the contradiction this field exists to stop.
    import pipeline_core.application.reconstruct.phase_c_tdd as driver

    def _old(frontier, attempted, landed):
        return frozenset(frontier) - frozenset(attempted)

    monkeypatch.setattr(driver, "unattempted_units", _old)
    control = tmp_path / "control"
    (control / "src").mkdir(parents=True)
    _tree(control, layout=_LAYOUT_THREE)
    c_ledger: dict = {"rows": []}
    c_events: list[tuple[str, dict]] = []
    c_logs: list[str] = []
    _run(monkeypatch, control, ledger=c_ledger, events=c_events, logs=c_logs,
         escalate=_escalate_to_width_and_height, max_stalls="3",
         stubbed=False, layout=_LAYOUT_THREE,
         dispatched_fns=(("src/a.rs", "depth"), ("src/a.rs", "width")),
         fill_write=_rewrite_height)
    c_named = [d for ev, d in c_events if ev == "phase_c.tdd.landed_change"
               and d.get("esc_unchanged")]
    assert c_named
    c_row = c_named[0]
    assert "src/a.rs::height" in c_row["landed"]
    assert "src/a.rs::height" in c_row["unattempted"]
    assert any("no route attempted" in ln and "height" in ln
               for ln in c_logs)


def test_a3_on_the_live_iter1_lists_writes_one_flat_row() -> None:
    """The plan's fixture, computed on run `9933-efe68f37` iter 1, not on
    the function's output. Under A3, attempted minus landed is exactly
    `src/error.rs::context`. Negative control 1: the live write is 8
    `esc_unchanged` rows, 7 of them absent from the wanted set."""
    import json
    from pathlib import Path
    data = json.loads(
        (Path(__file__).parent / "fixtures" / "dispatch"
         / "9933_attempted_vs_batch.json").read_text())
    batch = set(data["batch"])
    attempted = set(data["attempted"])
    landed = set(data["landed"])
    live_flat = set(data["live_esc_unchanged"])
    assert len(batch) == 12
    assert attempted == {"src/error.rs::context", "src/error.rs::msg"}
    assert landed == {"src/error.rs::msg"}
    assert attempted - landed == {"src/error.rs::context"}
    assert landed <= attempted, (
        "this run had no passenger; landed is a subset of attempted, so "
        "subtracting landed from unattempted is a no-op here")
    assert batch - attempted - landed == batch - attempted
    assert len(batch - attempted - landed) == 10
    assert len(live_flat) == 8
    assert len(live_flat - attempted) == 7
    assert live_flat & attempted == {"src/error.rs::context"}


def test_an_escalated_unit_whose_every_reply_was_capped_is_not_graded_flat(
        monkeypatch, tmp_path: Path) -> None:
    """FIX to the FLAT arm's denominator: a truncated call that re-emits the
    old body is not a measured refusal. Iter 3 of run `358833-aa15d93c`
    (2026-09-01) wrote 8 flat rows in a batch whose own audit said `3 of 4
    fill replies reasoning-capped`, and the re-buy detector then stood on
    truncated measurements. Same replay as the FLAT-arm test above, plus the
    fill recording a capped stream row joined to `width` — the unit moves
    out of the flat set (no row, re-seedable) and the audit names it.

    The negative control is the FLAT-arm test above itself: an unchanged
    escalated unit with NO capped reply (that replay records no stream rows,
    so the join names nothing) still writes its flat row."""
    from pipeline_core.domain.reasoning_cap import (
        record_stream_meta,
        serving_units,
    )
    _tree(tmp_path, layout=_LAYOUT_TWO)
    ledger: dict = {"rows": []}
    events: list[tuple[str, dict]] = []
    logs: list[str] = []

    def _capped_meta(_src: Any) -> None:
        with serving_units(("src/a.rs::width",)):
            record_stream_meta({"finish_reason": "length", "stop": "",
                                "followup": True})

    _run(monkeypatch, tmp_path, ledger=ledger, events=events, logs=logs,
         escalate=_escalate_to_width, max_stalls="3", stubbed=False,
         layout=_LAYOUT_TWO, fill_write=_capped_meta)
    assert any(ev == "phase_c.attribution.escalated" for ev, _ in events)
    flat = [r for r in ledger["rows"] if r.state is BodyState.FLAT]
    assert not [r for r in flat if r.key.fn_name == "width"], flat
    capped_named = [d for ev, d in events
                    if ev == "phase_c.tdd.landed_change"
                    and d.get("esc_capped")]
    assert capped_named and capped_named[0]["esc_capped"] == [
        "src/a.rs::width"]
    assert capped_named[0]["esc_unchanged"] == [], (
        "withheld from the graded set, not merely relabelled")
    assert any("every reply capped" in ln for ln in logs)
