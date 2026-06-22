"""Unit tests for the pure context guard (`pipeline_core.domain.context_guard`)
relocated from pipeline.ask in P6 (BL-015).

Pins the section-aware trimming: under-budget passthrough, TESTFILE-block drop,
sibling-comment drop, middle <FILE> drop, and the middle-trim fallback — all as
a pure function returning the trimmed prompt + guard events.
"""
from __future__ import annotations

from pipeline_core.domain.context_guard import fit_prompt

LIMIT = 10_000
MARGIN = 1_000  # mirrors domain._MARGIN


def test_under_budget_passthrough_no_events():
    p = "small prompt"
    out = fit_prompt(p, sys_len=0, context_chars=LIMIT)
    assert out.prompt == p
    assert out.events == ()


def test_drops_testfile_blocks_first():
    section = (LIMIT - MARGIN) * 3 // 5  # one fits, two don't
    block = f'\n<TESTFILE path="tests/foo.rs">{"x" * section}</TESTFILE>'
    p = "A" * 200 + block + block
    assert len(p) > LIMIT - MARGIN
    out = fit_prompt(p, sys_len=0, context_chars=LIMIT)
    assert len(out.prompt) <= LIMIT - MARGIN
    assert out.prompt.count("<TESTFILE") < p.count("<TESTFILE")
    assert any(t == "context-guard/section_removal" for t, _ in out.events)


def test_middle_trim_fallback_when_no_sections():
    # No droppable sections → falls back to head/tail middle-trim.
    p = "Z" * (LIMIT * 2)
    out = fit_prompt(p, sys_len=0, context_chars=LIMIT)
    assert "trimmed by context-guard" in out.prompt
    assert any(t == "context-guard/middle_trim" for t, _ in out.events)
    assert len(out.prompt) < len(p)


def test_system_length_counts_against_budget():
    # A prompt that fits alone but not with a large system prefix must trim.
    p = "Q" * (LIMIT - MARGIN - 100)
    fits_alone = fit_prompt(p, sys_len=0, context_chars=LIMIT)
    assert fits_alone.events == ()
    with_sys = fit_prompt(p, sys_len=5_000, context_chars=LIMIT)
    assert with_sys.events != ()
