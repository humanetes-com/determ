"""Unit tests for domain/coverage_annotation.py — pure coverage annotation functions."""
from __future__ import annotations

from pipeline_core.domain.coverage_annotation import (
    annotate_uncovered,
    extract_branch_guard,
    extract_uncovered_functions,
    filter_cfg_excluded_lines,
    filter_cfg_incompatible_lines,
    focused_annotation,
    partition_uncovered_lines,
)

_SOURCE = """\
pub fn foo(x: i32) -> i32 {
    if x > 0 {
        x + 1
    } else {
        x - 1
    }
}

pub fn bar() -> bool {
    true
}
"""

# ── extract_branch_guard ─────────────────────────────────────────────────────

def test_extract_branch_guard_finds_if() -> None:
    lines = ["fn f() {", "    if x > 0 {", "        x + 1", "    }"]
    # line_no=3 is 1-based: 'x + 1' — should walk back to 'if x > 0'
    result = extract_branch_guard(lines, 3)
    assert result is not None
    assert "if" in result


def test_extract_branch_guard_stops_at_fn() -> None:
    lines = ["fn f() {", "    x + 1"]
    result = extract_branch_guard(lines, 2)
    assert result is None


def test_extract_branch_guard_no_match() -> None:
    lines = ["    let x = 1;", "    x + 1"]
    result = extract_branch_guard(lines, 2)
    assert result is None


# ── annotate_uncovered ───────────────────────────────────────────────────────

def test_annotate_uncovered_marks_lines() -> None:
    content = "fn f() {\n    x\n    y\n}\n"
    result = annotate_uncovered(content, {2})
    lines = result.splitlines()
    assert "UNCOVERED" in lines[1]  # line 2
    assert "UNCOVERED" not in lines[2]  # line 3 is covered


def test_annotate_uncovered_empty_set() -> None:
    content = "fn f() {}\n"
    result = annotate_uncovered(content, set())
    assert "UNCOVERED" not in result


def test_annotate_uncovered_includes_line_numbers() -> None:
    content = "fn f() {\n    x\n}\n"
    result = annotate_uncovered(content, {2})
    assert "2" in result


# ── focused_annotation ───────────────────────────────────────────────────────

def test_focused_annotation_shows_context() -> None:
    lines = ["a", "b", "c", "d", "e", "f", "g"]
    content = "\n".join(lines)
    result = focused_annotation(content, {4}, ctx=1)
    shown = result.splitlines()
    # lines 3,4,5 should appear (context=1 around line 4)
    assert any("c" in ln for ln in shown)
    assert any("d" in ln for ln in shown)
    assert any("e" in ln for ln in shown)


def test_focused_annotation_separator_between_gaps() -> None:
    content = "\n".join(str(i) for i in range(20))
    result = focused_annotation(content, {2, 15}, ctx=0)
    assert "..." in result


# ── extract_uncovered_functions ──────────────────────────────────────────────

_FN_SOURCE = """\
fn foo() {
    let x = 1;
    x + 1
}

fn bar() {
    let y = 2;
    y + 2
}
"""


def test_extract_uncovered_functions_returns_fn_body() -> None:
    # line 2 (let x = 1) is inside fn foo
    result = extract_uncovered_functions(_FN_SOURCE, {2})
    assert "fn foo" in result
    assert "fn bar" not in result


def test_extract_uncovered_functions_fallback_when_no_fn() -> None:
    content = "    let x = 1;\n    x + 1\n"
    result = extract_uncovered_functions(content, {1})
    # Falls back to focused_annotation — still shows the line
    assert "1" in result


def test_extract_uncovered_functions_max_fns() -> None:
    result = extract_uncovered_functions(_FN_SOURCE, {2, 7}, max_fns=1)
    # only first fn should be included
    assert "--- next function ---" not in result


# ── partition_uncovered_lines ────────────────────────────────────────────────

def test_partition_single_group() -> None:
    result = partition_uncovered_lines(_FN_SOURCE, {2, 3}, n_groups=1)
    assert len(result) == 1
    assert result[0] == {2, 3}


def test_partition_empty() -> None:
    result = partition_uncovered_lines(_FN_SOURCE, set(), n_groups=3)
    assert result == []


def test_partition_two_fns_two_groups() -> None:
    # fn foo: lines 1-4, fn bar: lines 6-9
    result = partition_uncovered_lines(_FN_SOURCE, {2, 7}, n_groups=2)
    assert len(result) == 2
    # each group should have lines from only one function
    all_lines = result[0] | result[1]
    assert all_lines == {2, 7}


# ── filter_cfg_incompatible_lines ────────────────────────────────────────────

_CFG_SOURCE = """\
pub fn always() {}

#[cfg(feature = "std")]
pub fn std_only() {
    let x = 1;
}

pub fn also_always() {}
"""


def test_filter_cfg_incompatible_lines_active_feature_not_excluded() -> None:
    # active_cfgs=["std"]: the std_only block is COMPATIBLE, so line 5 is NOT excluded
    result = filter_cfg_incompatible_lines(_CFG_SOURCE, {5}, active_cfgs=["std"])
    assert 5 not in result


def test_filter_cfg_incompatible_lines_inactive_feature_excluded() -> None:
    # active_cfgs=[] (no "std"): std_only block lines should be excluded
    result = filter_cfg_incompatible_lines(_CFG_SOURCE, {5}, active_cfgs=[])
    assert result == set()  # empty active_cfgs → returns empty set (no-op)


# ── filter_cfg_excluded_lines ────────────────────────────────────────────────

def test_filter_cfg_excluded_lines_excluded_feature() -> None:
    # excluded_cfgs=["std"]: lines inside #[cfg(feature = "std")] are excluded
    result = filter_cfg_excluded_lines(_CFG_SOURCE, {5}, excluded_cfgs=["std"])
    assert 5 in result


def test_filter_cfg_excluded_lines_empty_excluded() -> None:
    result = filter_cfg_excluded_lines(_CFG_SOURCE, {5}, excluded_cfgs=[])
    assert result == set()


# --- inject_allow_before_mod ----------------------------------------------
from pipeline_core.domain.coverage_annotation import (  # noqa: E402
    inject_allow_before_mod,
)


def test_inject_allow_lands_above_the_mod_opener() -> None:
    block = "#[cfg(test)]\nmod gen_tests_1 {\n    fn a() {}\n}\n"
    out = inject_allow_before_mod(block, "#[allow(unused)]", gen_only=True)
    assert out.index("#[allow(unused)]") < out.index("mod gen_tests_1")


def test_inject_allow_is_idempotent() -> None:
    """Re-running the pass must not stack duplicate attributes on the mod."""
    block = "mod gen_tests_1 {\n}\n"
    once = inject_allow_before_mod(block, "#[allow(unused)]", gen_only=True)
    assert inject_allow_before_mod(once, "#[allow(unused)]", gen_only=True) == once


def test_inject_allow_leaves_block_untouched_when_no_opener() -> None:
    block = "fn not_a_mod() {}\n"
    assert inject_allow_before_mod(block, "#[allow(unused)]", gen_only=True) == block


def test_partition_uncovered_returns_single_unit_unsplit() -> None:
    """One function body cannot be split across groups — the caller expects the
    whole set back, not an empty partition."""
    src = "fn a() {\n    let x = 1;\n    let y = 2;\n}\n"
    assert partition_uncovered_lines(src, {2, 3}, 3) == [{2, 3}]


def test_extract_uncovered_functions_falls_back_with_nothing_uncovered() -> None:
    src = "fn a() {\n    let x = 1;\n}\n"
    assert (extract_uncovered_functions(src, set())
            == focused_annotation(src, set()))
