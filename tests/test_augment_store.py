"""FileAugmentStore — TASK 0c augmentation sentinel persistence (P6)."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.adapters.augment_store import FileAugmentStore
from pipeline_core.domain.models import AugmentVerdict
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
        "98.5\nreason=done\ncode_id=abc\n")


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
    done = AugmentVerdict(97.8, "done", "some-older-pipeline")
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
