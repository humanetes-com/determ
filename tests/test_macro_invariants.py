"""Characterization for the relocated cluster macro-invariants (P6).
Pins variant enumeration + the cluster-active expansion selection."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct.macro_invariants import (
    compute_cluster_macro_invariants,
    enumerate_macro_variants,
)
from pipeline_core.domain.models import BuildCluster

_SRC = (
    '#[cfg(feature = "std")]\n'
    "macro_rules! pick { () => { STD_PATH }; }\n"
    '#[cfg(not(feature = "std"))]\n'
    "macro_rules! pick { () => { CORE_PATH }; }\n"
    "macro_rules! solo { () => { ONLY }; }\n"
)


def _cluster(active: list[str], excluded: list[str]) -> BuildCluster:
    return BuildCluster(
        name="c", env_extra={}, tarpaulin_exclude_files=[],
        excluded_cfg=excluded, active_cfg=active, prompt_nightly_rule="",
    )


def test_enumerate_picks_up_cfg_and_names(tmp_path: Path) -> None:
    (tmp_path / "m.rs").write_text(_SRC, encoding="utf-8")
    variants = enumerate_macro_variants(tmp_path)
    names = {v[0] for v in variants}
    assert names == {"pick", "solo"}
    # the two `pick` variants carry their cfg preds; solo has none.
    picks = {cfg for name, cfg, _b, _s in variants if name == "pick"}
    assert any("std" in c for c in picks)


def test_none_cluster_is_empty(tmp_path: Path) -> None:
    (tmp_path / "m.rs").write_text(_SRC, encoding="utf-8")
    assert compute_cluster_macro_invariants(tmp_path, None) == ""


def test_active_variant_selected_for_cluster(tmp_path: Path) -> None:
    (tmp_path / "m.rs").write_text(_SRC, encoding="utf-8")
    # std excluded → the not(std) variant (CORE_PATH) is the active one.
    out = compute_cluster_macro_invariants(tmp_path, _cluster([], ["std"]))
    assert "pick!(...)" in out and "CORE_PATH" in out
    assert "STD_PATH" not in out
    assert "solo" not in out  # single-variant macro skipped (no ambiguity)


# --- each variant carries its OWN gate --------------------------------------
#
# Three cfg-gated variants of one macro, the middle one gated on a two-level
# predicate (`all(not(a), feature = "std")`) — the ordinary shape of a crate's
# cfg arms. The enumerator this pins replaced a scan for the LAST `#[cfg(…)]`
# line in an 800-char window with a one-nested-level capture: the two-level
# gate was no match, so the last match in the window was the PREVIOUS item's
# gate, and the third variant (also two-level) inherited the first's.

_THREE_ARMS = (
    "#[cfg(a)]\n"
    "macro_rules! tri { () => { ARM_A }; }\n"
    '#[cfg(all(not(a), feature = "std"))]\n'
    "macro_rules! tri { () => { ARM_B }; }\n"
    '#[cfg(all(not(b), not(feature = "std")))]\n'
    "macro_rules! tri { () => { ARM_C }; }\n"
)

_GATED_THEN_BARE = (
    '#[cfg(feature = "std")]\n'
    "macro_rules! pair { () => { GATED }; }\n"
    "macro_rules! pair { () => { BARE }; }\n"
)


def _pre_fix_enumerate(src_dir: Path) -> list[tuple[str, str]]:
    """The replaced enumerator, reproduced: `(name, cfg_pred)` per balanced
    `macro_rules!`, the predicate taken as the last trailing `#[cfg(P)]` line in
    the 800 chars before the declaration (one nested paren level)."""
    from pipeline_core.domain import cfg as _cfg
    from pipeline_core.domain.rust_macros import macro_definitions

    out: list[tuple[str, str]] = []
    for p in sorted(src_dir.rglob("*.rs")):
        text = p.read_text(encoding="utf-8")
        for md in macro_definitions(text):
            if not md.balanced:
                continue
            head = text[max(0, md.decl_start - 800):md.decl_start]
            out.append((md.name, _cfg.last_trailing_cfg_pred(head).strip()))
    return out


def _preds(variants: list[tuple[str, str, str, str]], name: str) -> list[str]:
    return [cfg for n, cfg, _b, _s in variants if n == name]


def test_each_variant_carries_its_own_predicate(tmp_path: Path) -> None:
    (tmp_path / "m.rs").write_text(_THREE_ARMS, encoding="utf-8")
    assert _preds(enumerate_macro_variants(tmp_path), "tri") == [
        "a",
        'all(not(a), feature = "std")',
        'all(not(b), not(feature = "std"))',
    ]


def test_pre_fix_enumerator_gave_the_third_variant_the_first_items_gate(tmp_path: Path) -> None:
    # Negative control: the replaced scan returns the FIRST item's predicate for
    # the third variant (and the second), because neither two-level gate matches.
    (tmp_path / "m.rs").write_text(_THREE_ARMS, encoding="utf-8")
    old = [cfg for n, cfg in _pre_fix_enumerate(tmp_path) if n == "tri"]
    assert old == ["a", "a", "a"]


def test_ungated_variant_after_a_gated_one_is_ungated(tmp_path: Path) -> None:
    (tmp_path / "m.rs").write_text(_GATED_THEN_BARE, encoding="utf-8")
    assert _preds(enumerate_macro_variants(tmp_path), "pair") == ['feature = "std"', ""]
    # …and the second variant is the always-active fallback, so a cluster with
    # `std` excluded renders the BARE arm rather than omitting the macro.
    out = compute_cluster_macro_invariants(tmp_path, _cluster([], ["std"]))
    assert "BARE" in out and "GATED" not in out


def test_attribute_block_spans_wrapped_attrs_comments_and_blank_lines(tmp_path: Path) -> None:
    # Rust binds an outer attribute to the next item across whitespace and
    # comments; a wrapped attribute is one span; stacked cfgs conjoin.
    src = (
        "const X: [u8; 1] = [1];\n"
        "#[cfg(\n"
        "    all(not(a),\n"
        '        feature = "std")\n'
        ")]\n"
        "/// doc line\n"
        "// plain comment with #[cfg(decoy)] inside\n"
        "#[cfg(unix)]\n"
        "\n"
        "macro_rules! w { () => { W }; }\n"
    )
    (tmp_path / "m.rs").write_text(src, encoding="utf-8")
    assert _preds(enumerate_macro_variants(tmp_path), "w") == [
        'all(all(not(a), feature = "std"), unix)'
    ]


def test_previous_items_gate_never_reaches_a_variant_with_a_bare_neighbour(tmp_path: Path) -> None:
    # A gated non-macro item directly above an ungated macro: the `;` ends the
    # walk, so the `use` item's gate is not the macro's.
    src = (
        "#[cfg(feature = \"std\")]\n"
        "pub(crate) use std::backtrace::Backtrace;\n"
        "macro_rules! near { () => { N }; }\n"
    )
    (tmp_path / "m.rs").write_text(src, encoding="utf-8")
    assert _preds(enumerate_macro_variants(tmp_path), "near") == [""]
