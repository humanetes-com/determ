"""Unit tests for the cfg domain (`pipeline_core.domain.cfg`) — the typed,
algebraic replacement for the old string-slicing cfg helpers in pipeline.py.

Pure domain: no extractor, no I/O. Exercises the parse → tree → evaluate path
and the cargo-faithful exclusion rule directly on the value objects."""
from __future__ import annotations

from pipeline_core.domain.cfg import (
    All,
    Any,
    CfgAtom,
    CfgSet,
    Leaf,
    Not,
    filter_cfg_inactive_branches,
    normalize_pred,
    parse_cfg_pred,
    split_top_level_commas,
)


def test_normalize_strips_all_spaces() -> None:
    assert normalize_pred('feature = "x"') == 'feature="x"'
    assert normalize_pred("all (a, b)") == "all(a,b)"


def test_split_top_level_commas_respects_nesting() -> None:
    assert split_top_level_commas("a,b,c") == ["a", "b", "c"]
    assert split_top_level_commas("all(a,b),not(c)") == ["all(a,b)", "not(c)"]


def test_atom_parse_bare_and_keyvalue() -> None:
    bare = CfgAtom.parse("nightly")
    assert bare == CfgAtom("nightly", None) and bare.normalized == "nightly"
    feat = CfgAtom.parse('feature="std"')
    assert feat.is_feature and feat.feature_name == "std"
    assert feat.cluster_key == "std"


def test_atom_cluster_key_drops_build_target_facets() -> None:
    # `target_os = "linux"` is a build-target facet → never its own cluster.
    assert CfgAtom.parse('target_os="linux"').cluster_key is None
    assert CfgAtom.parse("nightly").cluster_key == "nightly"


def test_parse_builds_typed_tree() -> None:
    assert parse_cfg_pred("nightly") == Leaf(CfgAtom("nightly", None))
    tree = parse_cfg_pred("all(unix, nightly)")
    assert isinstance(tree, All) and len(tree.parts) == 2
    assert isinstance(parse_cfg_pred("not(unix)"), Not)
    assert isinstance(parse_cfg_pred("any(a, b)"), Any)


def test_evaluate_decidable_and_undecidable() -> None:
    ctx = CfgSet.of(cfg_true=["nightly"], cfg_false=["windows"])
    assert ctx.evaluate("nightly") is True
    assert ctx.evaluate("windows") is False
    assert ctx.evaluate("unix") is None                       # neither set → undecidable
    assert ctx.evaluate("not(windows)") is True
    assert ctx.evaluate("all(nightly, windows)") is False     # one false → false
    assert ctx.evaluate("any(windows, windows)") is False     # all known false → false
    # Truth-dominant: a decided operand wins over undecidable arms.
    assert ctx.evaluate("any(nightly, unix)") is True         # one true → true
    assert ctx.evaluate("all(windows, unix)") is False        # one false → false
    # Only undecided-WITHOUT a dominating operand stays None.
    assert ctx.evaluate("all(nightly, unix)") is None         # true + undecidable → unknown
    assert ctx.evaluate("any(windows, unix)") is None         # false + undecidable → unknown


def test_excluded_uncovered_lines_active_and_excluded_blocks() -> None:
    src = (
        "fn always() {\n"                         # 1-2
        "    body();\n"
        "}\n"                                      # 3
        '#[cfg(error_generic_member_access)]\n'    # 4  excluded under stable
        "fn egma() {\n"                            # 5
        "    nightly_only();\n"                    # 6
        "}\n"                                      # 7
        "#[cfg(not(error_generic_member_access))]\n"  # 8  compiled-out under nightly
        "fn fallback() {\n"                        # 9
        "    stable_only();\n"                     # 10
        "}\n"                                      # 11
    )
    all_lines = set(range(1, 12))
    # Stable: egma excluded → its block lines dropped; fallback (not(egma)) kept.
    stable = CfgSet.of(cfg_false=["error_generic_member_access"])
    assert stable.excluded_uncovered_lines(src, all_lines) == {4, 5, 6, 7}
    # Nightly: egma active → the not(egma) fallback block is compiled out.
    nightly = CfgSet.of(cfg_true=["error_generic_member_access"])
    assert nightly.excluded_uncovered_lines(src, all_lines) == {8, 9, 10, 11}
    # Only intersect with the supplied uncovered set.
    assert stable.excluded_uncovered_lines(src, {6}) == {6}
    assert stable.excluded_uncovered_lines(src, set()) == set()


def test_lines_are_cluster_specific() -> None:
    from pipeline_core.domain.cfg import lines_are_cluster_specific
    src = (
        "fn always() { body(); }\n"                # 1  agnostic
        "#[cfg(error_generic_member_access)]\n"    # 2
        "fn egma() { x(); }\n"                      # 3  nightly-only
    )
    stable = CfgSet.of(cfg_false=["error_generic_member_access"])
    nightly = CfgSet.of(cfg_true=["error_generic_member_access"])
    ctxs = [stable, nightly]
    # Line 1 compiles in both clusters → agnostic → shareable.
    assert lines_are_cluster_specific(src, {1}, ctxs) is False
    # Line 3 is compiled out under stable → cluster-specific → must stay gated.
    assert lines_are_cluster_specific(src, {3}, ctxs) is True
    assert lines_are_cluster_specific(src, set(), ctxs) is False


def test_cluster_specific_symbol_names_flags_gated_fns() -> None:
    # A fn compiled OUT under some cluster is cluster-specific; an
    # ungated fn (and a #[cfg(test)]-only fn, builtin atom) is NOT.
    from pipeline_core.domain.cfg import cluster_specific_symbol_names
    recs = [
        {"fn_name": "always", "cfg_predicates": []},
        {"fn_name": "egma", "cfg_predicates": ["error_generic_member_access"]},
        {"fn_name": "std_only", "cfg_predicates": ['feature = "std"']},
        {"fn_name": "a_test", "cfg_predicates": ["test"]},  # builtin → never excluded
    ]
    stable = CfgSet.of(cfg_false=["error_generic_member_access"])      # default, std on
    no_std = CfgSet.of(cfg_false=["error_generic_member_access", 'feature = "std"'])
    names = cluster_specific_symbol_names(recs, [stable, no_std])
    assert names == frozenset({"egma", "std_only"})


def test_body_uses_cluster_specific_symbol_word_boundary() -> None:
    from pipeline_core.domain.cfg import body_uses_cluster_specific_symbol
    names = frozenset({"egma", "std_only"})
    assert body_uses_cluster_specific_symbol("let x = egma();", names) is True
    # word-boundary: `egma_helper` must NOT match `egma`.
    assert body_uses_cluster_specific_symbol("egma_helper();", names) is False
    assert body_uses_cluster_specific_symbol("let x = always();", names) is False
    assert body_uses_cluster_specific_symbol("", names) is False
    assert body_uses_cluster_specific_symbol("egma()", frozenset()) is False


def test_excludes_for_cargo_custom_off_and_nondefault_feature() -> None:
    empty = CfgSet.of()
    # custom --cfg toggle enabled nowhere → cargo leaves it OFF → excluded.
    assert empty.excludes_for_cargo("extra_cap") is True
    assert empty.excludes_for_cargo('any(extra_cap, feature = "extra_cap")') is True
    # non-default feature → off.
    assert empty.excludes_for_cargo('feature = "async"') is True
    assert CfgSet.of(default_features=["async"]).excludes_for_cargo('feature = "async"') is False


def test_excludes_for_cargo_never_over_excludes_builtins() -> None:
    empty = CfgSet.of()
    # builtin/target cfgs are undecidable to us — cargo may set them → not excluded.
    assert empty.excludes_for_cargo("unix") is False
    assert empty.excludes_for_cargo('target_os = "linux"') is False
    # `not(custom-off)` RUNS in the default build → not excluded.
    assert empty.excludes_for_cargo("not(extra_cap)") is False


# filter_cfg_inactive_branches tests
_STABLE_CTX = CfgSet.of(cfg_true=["not(nightly)"], cfg_false=["nightly"])


def test_filter_strips_inactive_use_statement() -> None:
    src = (
        '#[cfg(nightly)]\nuse nightly_only::Thing;\n'
        'use std::error::Error as StdError;\n'
    )
    out = filter_cfg_inactive_branches(src, _STABLE_CTX)
    assert "nightly_only" not in out
    assert "StdError" in out


def test_filter_strips_inactive_brace_block() -> None:
    src = (
        '#[cfg(nightly)]\ntrait StdError: Debug {\n    fn source(&self);\n}\n'
        'use std::error::Error as StdError;\n'
    )
    out = filter_cfg_inactive_branches(src, _STABLE_CTX)
    assert "fn source" not in out
    assert "StdError" in out


def test_filter_keeps_active_branch() -> None:
    src = (
        '#[cfg(not(nightly))]\nuse std::error::Error as StdError;\n'
        'fn foo() {}\n'
    )
    out = filter_cfg_inactive_branches(src, _STABLE_CTX)
    assert "StdError" in out
    assert "fn foo" in out


def test_filter_leaves_undecidable_branch_intact() -> None:
    # unix is a builtin — CfgSet has no opinion → undecidable → kept
    src = '#[cfg(unix)]\nuse unix_thing::X;\nfn bar() {}\n'
    out = filter_cfg_inactive_branches(src, _STABLE_CTX)
    assert "unix_thing" in out
    assert "fn bar" in out


def test_filter_handles_stacked_attributes() -> None:
    src = (
        '#[cfg(nightly)]\n'
        '#[allow(dead_code)]\n'
        'fn nightly_fn() { let x = 1; }\n'
        'fn stable_fn() {}\n'
    )
    out = filter_cfg_inactive_branches(src, _STABLE_CTX)
    assert "nightly_fn" not in out
    assert "stable_fn" in out


def test_filter_empty_text_returns_empty() -> None:
    assert filter_cfg_inactive_branches("", _STABLE_CTX) == ""


def test_filter_no_cfg_returns_text_unchanged() -> None:
    src = 'fn foo() {}\nfn bar() {}\n'
    assert filter_cfg_inactive_branches(src, _STABLE_CTX) == src


def test_unreachable_under_every_cfg_set() -> None:
    from pipeline_core.domain.cfg import unreachable_under_every_cfg_set

    # `anyhow`'s two baked clusters.
    stable = CfgSet.of(cfg_true=[], cfg_false=["error_generic_member_access"])
    nightly = CfgSet.of(cfg_true=["error_generic_member_access"], cfg_false=[])
    both = [stable, nightly]

    # The live case: nightly.rs 12/17/22, the Debug/Display/provide impls for a
    # local MyError inside #[cfg(anyhow_build_probe)] — a probe cfg no
    # `cargo test` sets. Stubbed IDENTICALLY in both cluster trees, so t2b's
    # pre-merge backfill has no donor either, and the final cfg-blind scan
    # would sys.exit(1) on them after a fully green suite — converting a
    # genuine success into a failure once the run's whole cost is paid.
    assert unreachable_under_every_cfg_set(["anyhow_build_probe"], both) is True

    # Reachable under ONE cluster is a real, fillable stub — the case the hard
    # failure must keep.
    assert unreachable_under_every_cfg_set(
        ["error_generic_member_access"], both) is False
    # Ungated: always reachable.
    assert unreachable_under_every_cfg_set([], both) is False
    assert unreachable_under_every_cfg_set([""], both) is False

    # With NO cfg-sets on hand nothing is provably unreachable. Absence of
    # evidence must not become the exemption, or a dropped cluster's real stubs
    # would be excused by the rule that exists to excuse the probe trio.
    assert unreachable_under_every_cfg_set(["anyhow_build_probe"], []) is False
