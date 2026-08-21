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

from dataclasses import replace
from pathlib import Path
from typing import Any

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
_BODY = "\n        self.ptr\n    "
_UNIT = FnLocator("src/a.rs", "depth")
_SWEEP = ("test t1 ... ok\ntest t2 ... FAILED\n"
          "test result: FAILED. 1 passed; 1 failed; 0 ignored")


def _tree(tmp_path: Path) -> Path:
    """A cluster with its authored layout (`.skeleton/stable/`) and the filled
    tree the loop scores — the pairing rule the whole gate rests on. Both are
    crate roots holding `src/a.rs`, which is how the extractor names the file
    and therefore how the ledger keys it."""
    for rel in (".skeleton/stable/src", "src"):
        d = tmp_path / rel
        d.mkdir(parents=True, exist_ok=True)
        (d / "a.rs").write_text(_LAYOUT, encoding="utf-8")
    return tmp_path / "src"


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
        state=BodyState.PROVEN)


def _run(monkeypatch, tmp_path: Path, *, ledger: dict | None,
         events: list[tuple[str, dict]], logs: list[str],
         fills: int = 0) -> None:
    from pipeline_core.application.reconstruct.phase_c_tdd import run_phase_c_tdd
    monkeypatch.setenv("TDD_MAX_STALLS", "1")
    monkeypatch.setenv("TDD_FLAT_SCORE_REPEATS", "0")
    monkeypatch.setenv("TDD_NO_VERDICT_SWEEPS", "0")
    src = tmp_path / "src"

    def _cargo(*_a: Any, **_k: Any) -> tuple[int, str]:
        return 101, _SWEEP

    def _enumerate(_src: Any, **_kw: Any) -> list[dict[str, Any]]:
        text = (src / "a.rs").read_text(encoding="utf-8")
        body = text.split("usize {", 1)[1].rsplit("}", 2)[0]
        return [{"file": "src/a.rs", "fn_name": "depth",
                 "sig_text": "    pub fn depth(&self) -> usize",
                 "body_text": body, "container": "impl Own"}]

    def _fill(**_kw: Any):
        from pipeline_core.domain.models import ClusterFillResult
        if fills:
            (src / "a.rs").write_text(
                _LAYOUT.replace("\n        unimplemented!()\n    ", _BODY),
                encoding="utf-8")
        return ClusterFillResult(attempts=1, fns_filled=fills)

    run_phase_c_tdd(
        _driver_deps(
            _cargo, lambda pd, cl, stems, lib=False: (2, frozenset({"t1", "t2"})),
            events, None, frontier={_UNIT: {"t2"}}, logs=logs, fill=_fill,
            stubs=[_UNIT], tree_stubs=[_UNIT],
            reverse_map={"t2": {_UNIT}},
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
    assert judged[0].state is BodyState.UNMEASURED, (
        "t2 RAN but is red in both readings: nothing regressed and nothing "
        "passed, so the body has not been shown to be right — `unmeasured` "
        "is re-seedable and claims nothing, which is what describes it")
    assert any(ev == "phase_c.ledger.judged" for ev, _ in events)

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
    row = replace(_proven_row(tmp_path), state=BodyState.LANDED)
    ledger: dict = {"rows": [row]}
    logs: list[str] = []
    _run(monkeypatch, tmp_path, ledger=ledger, events=[], logs=logs)

    assert any("backfilled body(ies) are the bodies on this tree" in ln
               for ln in logs)
    judged = [r for r in ledger["rows"] if r.state is not BodyState.LANDED]
    assert judged, "the first sweep must judge an adopted row"

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
