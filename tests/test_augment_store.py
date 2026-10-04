"""FileAugmentStore — TASK 0c augmentation sentinel persistence."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.adapters.augment_store import FileAugmentStore
from pipeline_core.domain.file_coverage_floor import (
    FILE_ACCOUNTED,
    FILE_MET,
    FileFloor,
    FileFloorReading,
)
from pipeline_core.domain.models import FN_EXECUTION_CRITERION, AugmentVerdict
from pipeline_core.domain.workspace_layout import CLUSTER_AUGMENTED_DIR, MODULE_AUGMENTED_DIR


def test_cluster_sentinel_roundtrip(tmp_path: Path) -> None:
    s = FileAugmentStore()
    assert s.is_cluster_augmented(tmp_path, "stable", "anyhow") is False
    assert s.cluster_augmented_value(tmp_path, "stable", "anyhow") == 0.0
    s.mark_cluster_augmented(tmp_path, "stable", "anyhow", 98.5,
                             reason="done", code_id="abc")
    assert s.is_cluster_augmented(tmp_path, "stable", "anyhow") is True
    assert s.cluster_augmented_value(tmp_path, "stable", "anyhow") == 98.5
    # coverage on the FIRST line (one decimal, no '%'), provenance under it.
    assert (tmp_path / CLUSTER_AUGMENTED_DIR / "stable__anyhow").read_text() == (
        "98.5\nreason=done\ncode_id=abc\n"
        f"criterion={FN_EXECUTION_CRITERION}\nfn_measured=no\nunexecuted=0\nunreached=\n")


def test_module_sentinel_roundtrip_and_clear(tmp_path: Path) -> None:
    s = FileAugmentStore()
    assert s.is_module_augmented(tmp_path, "anyhow") is False
    assert s.module_augmented_value(tmp_path, "anyhow") == 0.0
    s.mark_module_augmented(tmp_path, "anyhow", 100.0)
    assert s.is_module_augmented(tmp_path, "anyhow") is True
    assert s.module_augmented_value(tmp_path, "anyhow") == 100.0
    assert (tmp_path / MODULE_AUGMENTED_DIR / "anyhow").read_text() == "100.0\n"
    s.clear_module_sentinel(tmp_path, "anyhow")
    assert s.is_module_augmented(tmp_path, "anyhow") is False


def test_value_is_zero_on_unparseable(tmp_path: Path) -> None:
    s = FileAugmentStore()
    p = s.module_path(tmp_path, "anyhow")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("not-a-float\n")
    assert s.module_augmented_value(tmp_path, "anyhow") == 0.0


# --- the verdict's provenance -------------------------------------------------
#
# The sentinel used to be a bare number, so "the target was reached" and "this
# code gave up" read identically and a verdict outlived the commit that fixed
# its cause. `LIVE_LEGACY_SENTINEL` is the exact bytes of
# tmp/anyhow/.cluster_augmented/stable__anyhow on 2026-08-27, written by run
# 2881435-34981ab0 after a false ceiling sealed the cluster at 83.9%.
# `LIVE_CODE_ID` is that run's `code=` field (`code_identity`), not the crate
# `Source hash:` line (`b5450483778bbacd` belonged to the later resume
# 1566274-2e1d805c).

LIVE_LEGACY_SENTINEL = "83.9\n"
LIVE_CODE_ID = "3264439950bf"


def test_the_live_legacy_sentinel_parses_but_does_not_bind(tmp_path: Path) -> None:
    s = FileAugmentStore()
    p = s.cluster_path(tmp_path, "stable", "anyhow")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(LIVE_LEGACY_SENTINEL)
    verdict = s.cluster_augmented_verdict(tmp_path, "stable", "anyhow")
    assert verdict is not None
    assert verdict.coverage == 83.9
    # Negative control: the pre-fix predicate — still True, which is exactly why
    # the cluster was skipped for every run after the false ceiling.
    assert s.is_cluster_augmented(tmp_path, "stable", "anyhow") is True
    assert verdict.still_binding(LIVE_CODE_ID) is False


def test_a_give_up_verdict_binds_only_under_its_own_code() -> None:
    ceiling = AugmentVerdict(83.9, "ceiling", LIVE_CODE_ID)
    assert ceiling.still_binding(LIVE_CODE_ID) is True
    assert ceiling.still_binding("4a6d8d3f3e95") is False
    assert AugmentVerdict(0.0, "precheck_failed", "x").still_binding("y") is False
    assert AugmentVerdict(62.0, "hard_block", "x").still_binding("y") is False


def test_a_done_verdict_binds_under_any_code() -> None:
    done = AugmentVerdict(97.8, "done", "some-older-pipeline",
                          criterion=FN_EXECUTION_CRITERION)
    assert done.is_final() is True
    assert done.still_binding("anything-at-all") is True


def test_verdict_text_roundtrips_and_missing_sentinel_is_none(tmp_path: Path) -> None:
    v = AugmentVerdict(83.9, "ceiling", LIVE_CODE_ID)
    assert AugmentVerdict.from_text(v.to_text()) == v
    assert AugmentVerdict.from_text("not-a-float\nreason=ceiling\n").coverage == 0.0
    assert FileAugmentStore().cluster_augmented_verdict(tmp_path, "stable", "anyhow") is None


def test_from_text_reads_a_source_hash_line_as_code_id() -> None:
    # The first draft of this field was named source_hash and stored the crate
    # hash. Parsing still accepts that key so a sentinel written under the old
    # name is not silently unattributable.
    v = AugmentVerdict.from_text("83.9\nreason=ceiling\nsource_hash=3264439950bf\n")
    assert v.code_id == "3264439950bf"
    assert v.still_binding("3264439950bf") is True
    assert v.still_binding("4a6d8d3f3e95") is False


# --- the function-execution criterion on the verdict --------------------------
#
# The live sentinel run `330242-0be2c3e1` wrote for anyhow's `nightly`
# (2026-09-03): a line-coverage `done` at 95.2 % over functions no test had
# entered. Under the old rule a `done` bound under any code, so no later run
# would ever have re-augmented it.
_LIVE_LINE_DONE = "95.2\nreason=done\ncode_id=4b293605efff\n"


def test_a_line_coverage_done_without_the_criterion_no_longer_binds(tmp_path: Path) -> None:
    s = FileAugmentStore()
    p = s.cluster_path(tmp_path, "nightly", "anyhow")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_LIVE_LINE_DONE)
    v = s.cluster_augmented_verdict(tmp_path, "nightly", "anyhow")
    assert v is not None and v.is_final()
    assert v.still_binding("4b293605efff") is False
    assert "function-execution criterion" in v.unbinding_reason("4b293605efff")
    # Negative control: the pre-fix rule — any final verdict binds.
    assert v.is_final() is True


def test_a_done_written_under_the_criterion_roundtrips_its_summary(tmp_path: Path) -> None:
    from pipeline_core.domain.fn_execution import (
        FnCoverageRecord,
        FnTarget,
        PendingFn,
        UnreachedFn,
    )
    from pipeline_core.domain.models import FnSite
    rec = FnCoverageRecord(
        measured=True, candidates=4,
        executed=(FnSite("src/a.rs", "f", ""),),
        pending=(PendingFn(FnTarget(FnSite("src/a.rs", "g", ""), 3, 5, False), (1,)),),
        unreached=(UnreachedFn(FnSite("src/b.rs", "h", "impl T for S"), (1, 2, 3), True),))
    s = FileAugmentStore()
    s.mark_cluster_augmented(tmp_path, "nightly", "m", 91.0, reason="done",
                             code_id="x", functions=rec)
    v = s.cluster_augmented_verdict(tmp_path, "nightly", "m")
    assert v is not None
    assert v.criterion == FN_EXECUTION_CRITERION and v.fn_measured is True
    assert v.unexecuted == 1 and v.unreached == ("src/b.rs::h [impl T for S]",)
    assert v.still_binding("any-later-code") is True
    assert AugmentVerdict.from_text(v.to_text()) == v


def test_the_function_record_persists_beside_the_sentinel(tmp_path: Path) -> None:
    from pipeline_core.domain.fn_execution import FnCoverageRecord, FnTarget, PendingFn
    from pipeline_core.domain.models import FnSite
    s = FileAugmentStore()
    assert s.cluster_fn_record(tmp_path, "stable", "m") is None
    rec = FnCoverageRecord(
        measured=True, candidates=2, executed=(FnSite("src/a.rs", "f", ""),),
        pending=(PendingFn(FnTarget(FnSite("src/a.rs", "g", "impl X"), 7, 9, True), (2,)),))
    s.save_cluster_fn_record(tmp_path, "stable", "m", rec)
    assert s.cluster_fn_record(tmp_path, "stable", "m") == rec
    assert s.all_fn_records(tmp_path) == [rec]
    # The sentinel reader is untouched by the record beside it.
    assert s.is_cluster_augmented(tmp_path, "stable", "m") is False
    # A record of another criterion accounts for nothing.
    s.fn_record_path(tmp_path, "stable", "m").write_text('{"criterion": "old"}')
    assert s.cluster_fn_record(tmp_path, "stable", "m") is None


def test_the_other_clusters_records_for_the_module_exclude_its_own(tmp_path: Path) -> None:
    from pipeline_core.domain.fn_execution import FnCoverageRecord
    s = FileAugmentStore()
    stable = FnCoverageRecord(measured=True, candidates=1)
    nightly = FnCoverageRecord(measured=True, candidates=2)
    other_module = FnCoverageRecord(measured=True, candidates=3)
    s.save_cluster_fn_record(tmp_path, "stable", "m", stable)
    s.save_cluster_fn_record(tmp_path, "nightly", "m", nightly)
    s.save_cluster_fn_record(tmp_path, "stable", "other", other_module)
    assert s.other_cluster_fn_records(tmp_path, "nightly", "m") == [stable]
    assert s.other_cluster_fn_records(tmp_path, "stable", "m") == [nightly]
    assert s.other_cluster_fn_records(tmp_path / "none", "stable", "m") == []


def test_the_file_floor_round_trips_and_stamps_the_verdict(tmp_path: Path) -> None:
    s = FileAugmentStore()
    floor = FileFloor(90.0, (
        FileFloorReading("src/wrapper.rs", 21, 24, 21, 24, FILE_ACCOUNTED,
                         "attempt budget spent on src/wrapper.rs::fmt", (40, 41, 42)),
        FileFloorReading("src/ptr.rs", 79, 79, 79, 88, FILE_MET)))
    assert s.cluster_file_floor(tmp_path, "stable", "anyhow") is None
    s.save_cluster_file_floor(tmp_path, "stable", "anyhow", floor)
    assert s.cluster_file_floor(tmp_path, "stable", "anyhow") == floor
    assert s.module_file_floors(tmp_path, "anyhow") == {"stable": floor}
    assert s.module_file_floors(tmp_path, "other") == {}
    s.mark_cluster_augmented(tmp_path, "stable", "anyhow", 96.4, reason="done",
                             code_id="c", file_floor=floor)
    v = s.cluster_augmented_verdict(tmp_path, "stable", "anyhow")
    assert v is not None and v.file_floor == 90.0
    assert v.files_below == ("src/wrapper.rs 87.5% accounted",)
    assert v.still_binding("c", 90.0) and not v.still_binding("c", 95.0)
