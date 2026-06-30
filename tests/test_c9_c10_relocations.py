"""C9 (genesis rule extraction/store) + C10 (test augmentor) relocations."""
from __future__ import annotations

import re
from pathlib import Path

from pipeline_core.adapters.augment_engine import StaticRustTestAugmentor
from pipeline_core.application.tasks.genesis_audit import extract_and_store_rules
from pipeline_core.domain.text import extract_markdown_rules
from pipeline_core.ports.augment import TestAugmentor

_OLD_RE = re.compile(r'^\s*[-*•]\s+(.+)$|^\s*\d+\.\s+(.+)$', re.MULTILINE)


def _old_extract(text: str, min_len: int = 20) -> list[str]:
    out = []
    for m in _OLD_RE.finditer(text):
        r = (m.group(1) or m.group(2) or "").strip()
        if len(r) > min_len:
            out.append(r)
    return out


def test_extract_markdown_rules_matches_legacy_regex() -> None:
    samples = [
        "- a sufficiently long rule about error handling here\n  * indented long rule\n",
        "1. numbered long enough rule content here\n2.short\n3.  spaced numbered rule long\n",
        "-nospace should not match\n• unicode bullet rule that is long enough yes\n",
        "12. twelve-dot long enough rule\n1.2. nested numbering long enough text here\n",
        "plain prose, no marker, ignored entirely by both\n",
    ]
    for s in samples:
        assert extract_markdown_rules(s, min_len=20) == _old_extract(s)


def test_extract_markdown_rules_min_len_filter() -> None:
    assert extract_markdown_rules("- short\n- this one is plenty long to keep\n", min_len=20) == [
        "this one is plenty long to keep"]


def test_extract_and_store_rules_dedupes_and_tags(tmp_path: Path) -> None:
    calls: list[tuple] = []
    shared = "duplicated rule prefix padded well beyond forty chars"  # >40 chars
    genesis = (
        f"- {shared} ALPHA\n"
        f"- {shared} BETA\n"          # identical first 40 chars → deduped
        "- a genuinely distinct long rule about timeouts here\n"
    )
    extract_and_store_rules(
        tmp_path, genesis, "mymod", "timeout", 7,
        genesis_rules_write=lambda *a: calls.append(a))
    assert len(calls) == 2  # one duplicate prefix dropped
    assert all(c[2] == "timeout" for c in calls)  # failure_type → category
    assert all(c[4] == 7 for c in calls)          # iteration threaded


def test_extract_and_store_rules_unknown_failure_is_general(tmp_path: Path) -> None:
    calls: list[tuple] = []
    extract_and_store_rules(
        tmp_path, "- a long enough rule to be stored as general\n", "m", "weird", 1,
        genesis_rules_write=lambda *a: calls.append(a))
    assert calls and calls[0][2] == "general"


def _augmentor() -> StaticRustTestAugmentor:
    return StaticRustTestAugmentor(
        cargo_env=lambda: {}, build_timeout=5,
        measure_coverage=lambda pd, m: (42.0, [], 3, 7))


def test_static_augmentor_is_a_port() -> None:
    assert isinstance(_augmentor(), TestAugmentor)


def test_static_augmentor_applicability() -> None:
    a = _augmentor()
    assert a.is_applicable("lib")
    assert not a.is_applicable("proc-macro")
    assert not a.is_applicable("PROC-MACRO")  # case-insensitive


def test_static_augmentor_measure_delegates(tmp_path: Path) -> None:
    assert _augmentor().measure_coverage(tmp_path, "m", tmp_path) == (42.0, [], 3, 7)
