"""Unit tests for the P5 cfg domain (`pipeline_core.domain.cfg`) — the typed,
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
    # Conservative: any undecidable part makes all()/any() undecidable (no True/False
    # short-circuit) — preserved legacy `_eval_cfg_pred` semantics.
    assert ctx.evaluate("any(nightly, unix)") is None
    assert ctx.evaluate("all(nightly, unix)") is None


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
