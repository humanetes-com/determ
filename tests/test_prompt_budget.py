"""Pins PromptBudgets.derive() to the exact per-section formulas that used to be
inline `_*_CAP` globals in pipeline.py — byte-for-byte identical caps, so moving
the policy into the VO changes no prompt sizing."""
from __future__ import annotations

import dataclasses

import pytest

from pipeline_core.domain.prompt_budget import PromptBudgets


def _expected(limit: int) -> dict[str, int]:
    return {
        "readme": max(3_000, limit // 20),
        "profile": max(2_000, limit // 30),
        "genesis": max(3_000, limit // 20),
        "repo_spec": max(2_500, limit // 10),
        "repo_spec_wide": max(5_000, limit // 5),
        "strategy": max(4_000, limit // 15),
        "sibling": max(3_000, limit // 30),
        "src_snippet": max(4_000, limit // 10),
        "extra": max(3_000, limit // 20),
        "working": max(800, limit // 100),
        "types_section": max(8_000, limit // 3),
        "types_count": max(40, limit // 6000),
        "failed_body": max(2_000, limit // 12),
    }


@pytest.mark.parametrize("limit", [8_192, 32_000, 128_000, 1_000_000])
def test_derive_matches_legacy_formulas(limit):
    b = PromptBudgets.derive(limit)
    assert dataclasses.asdict(b) == _expected(limit)


def test_floors_bind_on_tiny_window():
    # a small limit must clamp to the per-section floors, not collapse to ~0
    b = PromptBudgets.derive(1)
    assert (b.readme, b.working, b.types_count, b.types_section) == (3_000, 800, 40, 8_000)


def test_is_frozen():
    b = PromptBudgets.derive(32_000)
    with pytest.raises(dataclasses.FrozenInstanceError):
        b.readme = 0  # type: ignore[misc]
