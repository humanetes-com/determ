"""FileAugmentStore — TASK 0c augmentation sentinel persistence (P6)."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.adapters.augment_store import FileAugmentStore
from pipeline_core.domain.workspace_layout import CLUSTER_AUGMENTED_DIR, MODULE_AUGMENTED_DIR


def test_cluster_sentinel_roundtrip(tmp_path: Path) -> None:
    s = FileAugmentStore()
    assert s.is_cluster_augmented(tmp_path, "stable", "anyhow") is False
    assert s.cluster_augmented_value(tmp_path, "stable", "anyhow") == 0.0
    s.mark_cluster_augmented(tmp_path, "stable", "anyhow", 98.5)
    assert s.is_cluster_augmented(tmp_path, "stable", "anyhow") is True
    assert s.cluster_augmented_value(tmp_path, "stable", "anyhow") == 98.5
    # R8.4: bare numeric, one decimal, no '%'.
    assert (tmp_path / CLUSTER_AUGMENTED_DIR / "stable__anyhow").read_text() == "98.5\n"


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
