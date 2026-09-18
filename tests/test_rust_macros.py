"""Unit tests for the pure `macro_rules!` scanner
(`pipeline_core.domain.rust_macros`) — source-regex→structured.

Pins byte-equivalence to the four regex+brace-counter scans it replaced in
pipeline.py: the body-extracting consumers (`_enumerate_macro_variants`,
`_collect_crate_macros`) which require a balanced body, the head-stripper
(`_compute_cluster_macro_invariants`), and the name-only consumer
(`_find_tests_for_fn`) which counts even unbalanced headers.
"""
from __future__ import annotations

import re

from pipeline_core.domain.rust_macros import macro_definitions

# --- the four originals, used as oracles ---

def _orig_variants(text: str) -> list[tuple[str, int, int]]:
    out = []
    for m in re.finditer(r"macro_rules!\s+(\w+)\s*\{", text):
        start = m.start()
        i = text.find("{", start)
        if i < 0:
            continue
        depth = 1
        j = i + 1
        while j < len(text) and depth > 0:
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
            j += 1
        if depth != 0:
            continue
        out.append((m.group(1), start, j))  # j = one past the closing `}`
    return out


def _orig_collect(text: str) -> list[tuple[str, int, int]]:
    out = []
    for m in re.finditer(r"(?:#\[macro_export\][^\n]*\n)?macro_rules!\s+(\w+)\s*\{", text):
        start = m.start()
        i = text.find("{", start)
        if i < 0:
            continue
        depth = 1
        j = i + 1
        while j < len(text) and depth > 0:
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
            j += 1
        if depth != 0:
            continue
        out.append((m.group(1), start, j))
    return out


def _orig_names(text: str) -> set[str]:
    return {m.group(1) for m in re.finditer(r"macro_rules!\s+(\w+)\s*\{", text)}


def _orig_head_strip(compact: str) -> str:
    hm = re.search(r"macro_rules!\s+\w+\s*\{\s*", compact)
    if hm:
        c = compact[hm.end():].rstrip()
        if c.endswith("}"):
            c = c[:-1].rstrip()
        return c
    return compact


def _new_head_strip(compact: str) -> str:
    defs = macro_definitions(compact)
    if defs:
        hb = defs[0].body_open + 1
        while hb < len(compact) and compact[hb].isspace():
            hb += 1
        c = compact[hb:].rstrip()
        if c.endswith("}"):
            c = c[:-1].rstrip()
        return c
    return compact


_SAMPLES = [
    "",
    "macro_rules! foo { () => {}; }",
    "#[macro_export]\nmacro_rules! bar { ($x:expr) => { { $x } }; }",
    "#[macro_export] // exported\nmacro_rules! baz {\n  () => {};\n}",
    "macro_rules! unbalanced { () => {",   # no closing brace
    "// macro_rules! commented { x }\nmacro_rules! real { () => {}; }",
    "macro_rules! a{}macro_rules! b{x{}}",
    "macro_rules!   spaced   {  arm  }",
    "macro_rules! no_brace_here ;",
    "fn macro_rules() {}",                  # not a real macro_rules!
    "macro_rules! nested { () => { macro_rules! inner { () => {}; } }; }",
]


def test_variants_byte_equivalent():
    for s in _SAMPLES:
        bal = [d for d in macro_definitions(s) if d.balanced]
        got = [(d.name, d.decl_start, d.body_close + 1) for d in bal]
        assert got == _orig_variants(s), s


def test_collect_byte_equivalent():
    for s in _SAMPLES:
        bal = [d for d in macro_definitions(s) if d.balanced]
        got = [(d.name, d.export_start, d.body_close + 1) for d in bal]
        assert got == _orig_collect(s), s


def test_names_count_unbalanced_too():
    for s in _SAMPLES:
        assert {d.name for d in macro_definitions(s)} == _orig_names(s), s


def test_head_strip_byte_equivalent():
    for s in _SAMPLES:
        for d in macro_definitions(s):
            if not d.balanced:
                continue
            body = s[d.decl_start:d.body_close + 1]
            assert _new_head_strip(body) == _orig_head_strip(body), body


def test_export_flag_and_start():
    s = "#[macro_export]\nmacro_rules! e { () => {}; }"
    (d,) = macro_definitions(s)
    assert d.exported and d.export_start == 0 and d.decl_start == s.index("macro_rules!")
    s2 = "macro_rules! ne { () => {}; }"
    (d2,) = macro_definitions(s2)
    assert not d2.exported and d2.export_start == d2.decl_start


def test_unbalanced_dropped_from_body_consumers_kept_for_names():
    s = "macro_rules! oops { () => {"
    (d,) = macro_definitions(s)
    assert not d.balanced
    assert {x.name for x in macro_definitions(s)} == {"oops"}
