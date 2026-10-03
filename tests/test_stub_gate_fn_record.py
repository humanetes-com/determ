"""The stub gates' reading of augmentation's function-execution record.

Run `301146-2d8a5f06` (2026-09-17) passed every test and exited 1 on six
reachable stubs the gate could only call "never attempted": nothing had asked,
at augmentation, whether any test executes them. With the per-cluster record:

- the end gate (`classify_remaining_stubs` / `report_stub_verdict`, called by
  `lock_or_complete`) excuses a stub every measured record speaking about it
  lists UNREACHED — test blocks written for it compiled and ran and the count
  stayed 0 — and prints the attempts; the decision taken 2026-09-17 is that
  this does not count against the run;
- a stub a record lists EXECUTED stays orphaned (exit 1) and says so;
- a stub no record accounts for stays orphaned exactly as before (control);
- the TDD cluster close reports the same classification the moment it closes.

Invented sites only.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline_core.application.orchestrator import (
    LockDeps,
    classify_remaining_stubs,
    lock_or_complete,
    report_stub_verdict,
)
from pipeline_core.application.reconstruct.close_stub_account import report_close_stubs
from pipeline_core.domain.fn_execution import (
    FnCoverageRecord,
    FnTarget,
    PendingFn,
    ProvenFn,
    UnreachedFn,
)
from pipeline_core.domain.models import FnLocator, FnSite
from pipeline_core.domain.stub_inventory import StubInventory, StubSite

_UNREACHED = FnSite("src/wrap.rs", "provide", "impl Error for Boxed")
_EXECUTED = FnSite("src/probe.rs", "request", "")
_UNKNOWN = FnSite("src/other.rs", "stray", "")

_RECORD = FnCoverageRecord(
    measured=True, candidates=12,
    executed=(_EXECUTED,),
    unreached=(UnreachedFn(_UNREACHED, (2, 3, 4), cluster_gated=True),))


def _inventory(*sites: FnSite) -> StubInventory:
    return StubInventory(tuple(StubSite(s, ()) for s in sites))


def test_an_unreached_stub_is_its_own_bucket_and_not_counted() -> None:
    verdict = classify_remaining_stubs(_inventory(_UNREACHED).site_preds(), [], (), [_RECORD])
    assert verdict.unreached_by_tests == (FnLocator("src/wrap.rs", "provide"),)
    assert verdict.orphaned == ()
    log: list[str] = []
    assert report_stub_verdict(verdict, log.append, 2) is True
    text = "\n".join(log)
    assert "MEASURED them unreached" in text and "PROVED them unreachable" in text
    assert ("src/wrap.rs::provide [impl Error for Boxed] (compiled only under its "
            "cluster's cfg) — targeted attempt(s) 2, 3, 4") in text


def test_negative_control_without_a_record_the_same_stub_fails_the_run() -> None:
    verdict = classify_remaining_stubs(_inventory(_UNREACHED).site_preds(), [], (), [])
    assert verdict.orphaned == (FnLocator("src/wrap.rs", "provide"),)
    log: list[str] = []
    assert report_stub_verdict(verdict, log.append, 2) is False
    assert "no augmentation record accounts for it" in "\n".join(log)


def test_an_executed_or_unaccounted_stub_still_fails_and_says_which() -> None:
    pending = FnCoverageRecord(measured=True, pending=(PendingFn(
        FnTarget(_UNKNOWN, 1, 3, False), (1,)),))
    verdict = classify_remaining_stubs(
        _inventory(_UNREACHED, _EXECUTED, _UNKNOWN).site_preds(), [], (), [_RECORD, pending])
    assert [loc.fn_name for loc in verdict.orphaned] == ["request", "stray"]
    assert verdict.unreached_by_tests == (FnLocator("src/wrap.rs", "provide"),)
    log: list[str] = []
    assert report_stub_verdict(verdict, log.append, 2) is False
    text = "\n".join(log)
    assert ("src/probe.rs::request — the original's suite EXECUTED this body at "
            "augmentation") in text
    assert "src/other.rs::stray — no augmentation record accounts for it" in text


def test_a_hollow_upstream_or_cfg_unreachable_stub_is_decided_before_the_record() -> None:
    verdict = classify_remaining_stubs(
        [(_UNREACHED, ("never_set",))], [], [("src/wrap.rs", "provide")], [_RECORD])
    assert verdict.upstream_hollow == (FnLocator("src/wrap.rs", "provide"),)
    assert verdict.unreached_by_tests == ()


def test_the_single_module_completion_exits_zero_on_unreached_stubs_only() -> None:
    log: list[str] = []

    def _exit(code: int) -> None:
        raise SystemExit(code)

    def _deps(records: list[FnCoverageRecord]) -> LockDeps:
        return LockDeps(
            commit_and_mark=lambda *a, **k: None, set_current=lambda *a: None,
            lock_module=lambda *a: None, git_commit=lambda *a: True,
            log=log.append, sys_exit=_exit,
            scan_unimplemented_functions=lambda *a, **k: _inventory(_UNREACHED),
            augmentation_fn_records=lambda _p: records)

    def _complete(deps: LockDeps) -> int:
        with pytest.raises(SystemExit) as exc:
            lock_or_complete(
                Path("/p"), "m", Path("/p"), Path("/p/.gen"), is_multi_module=False,
                locked_modules=set(), src_hash=None, commit_message="cm", detail="d",
                set_current_label="s", locked_commit_msg="l", success_log="SUCCESS",
                deps=deps)
        return int(exc.value.code or 0)

    assert _complete(_deps([_RECORD])) == 0
    assert _complete(_deps([])) == 1               # control: the pre-record gate


def test_the_cluster_close_reports_each_stub_by_the_record() -> None:
    log: list[str] = []
    events: list[tuple[str, dict[str, object]]] = []
    account = report_close_stubs(
        stubs=_inventory(_UNREACHED, _EXECUTED, _UNKNOWN), record=_RECORD,
        cluster_name="gizmo", close_kind="green", tag="[m]", log=log.append,
        emit=lambda e, **f: events.append((e, f)))
    assert account is not None
    text = "\n".join(log)
    assert "cluster gizmo closes (green) with 3 stub(s) still unimplemented:" in text
    assert ("unreached at augmentation (targeted attempt(s) 2, 3, 4): "
            "src/wrap.rs::provide [impl Error for Boxed]") in text
    assert "EXECUTED at augmentation: src/probe.rs::request" in text
    assert "not accounted by the record: src/other.rs::stray" in text
    (name, fields), = events
    assert name == "phase_c.tdd.close_stubs"
    data = fields["data"]
    assert isinstance(data, dict) and data["record"] == "measured"
    # No record: nothing is accounted, and the line says why.
    log.clear()
    report_close_stubs(stubs=_inventory(_UNREACHED), record=None, cluster_name="gizmo",
                       close_kind="reference floor", tag="[m]", log=log.append,
                       emit=lambda e, **f: None)
    assert "no augmentation function record for this cluster" in log[0]
    assert report_close_stubs(stubs=StubInventory(), record=_RECORD, cluster_name="gizmo",
                              close_kind="green", tag="[m]", log=log.append,
                              emit=lambda e, **f: None) is None


def test_a_stub_proved_unreachable_by_construction_is_excused_and_says_why() -> None:
    proved = FnSite("src/ptr.rs", "clone", "impl Clone for Own<T>")
    record = FnCoverageRecord(measured=True, candidates=3, by_construction=(
        ProvenFn(proved, "`Own` is not nameable and no MIR use reaches it"),))
    verdict = classify_remaining_stubs(_inventory(proved).site_preds(), [], (), [record])
    assert verdict.unreached_by_tests == (FnLocator("src/ptr.rs", "clone"),)
    assert verdict.orphaned == ()
    assert verdict.unreached_evidence == (
        "src/ptr.rs::clone [impl Clone for Own<T>] — unreachable by construction: "
        "`Own` is not nameable and no MIR use reaches it",)
    # Negative control: the same stub with no record is orphaned and fails the run.
    bare = classify_remaining_stubs(_inventory(proved).site_preds(), [], (), [])
    assert bare.orphaned == (FnLocator("src/ptr.rs", "clone"),)
    # A cluster that measured the function EXECUTED outweighs the other's proof.
    other = FnCoverageRecord(measured=True, executed=(proved,))
    both = classify_remaining_stubs(_inventory(proved).site_preds(), [], (), [record, other])
    assert both.orphaned == (FnLocator("src/ptr.rs", "clone"),)
