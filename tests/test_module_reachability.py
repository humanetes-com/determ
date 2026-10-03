"""Pins for crate module-reachability (layout fix: a `pub` item behind a private
`mod` wall is not part of the crate's external API, however it's marked)."""
from __future__ import annotations

from pipeline_core.domain.models import ModRecord
from pipeline_core.domain.module_reachability import externally_reachable_files


def _mod(name, file, declared_in, vis) -> ModRecord:
    return ModRecord(name=name, file=file, declared_in=declared_in, vis=vis)


def test_public_mod_chain_is_reachable():
    mods = [_mod("error", "error.rs", "lib.rs", "public")]
    all_files = frozenset({"lib.rs", "error.rs"})
    assert externally_reachable_files(mods, all_files) == all_files


def test_private_mod_blocks_reachability():
    # the real anyhow shape: `mod ptr;` (private) hides ptr.rs entirely.
    mods = [_mod("ptr", "ptr.rs", "lib.rs", "private")]
    all_files = frozenset({"lib.rs", "ptr.rs"})
    got = externally_reachable_files(mods, all_files)
    assert got == frozenset({"lib.rs"})
    assert "ptr.rs" not in got


def test_restricted_mod_also_blocks_reachability():
    mods = [_mod("internal", "internal.rs", "lib.rs", "restricted")]
    all_files = frozenset({"lib.rs", "internal.rs"})
    assert externally_reachable_files(mods, all_files) == frozenset({"lib.rs"})


def test_transitive_public_chain_is_reachable():
    mods = [
        _mod("outer", "outer.rs", "lib.rs", "public"),
        _mod("inner", "inner.rs", "outer.rs", "public"),
    ]
    all_files = frozenset({"lib.rs", "outer.rs", "outer/inner.rs"})
    assert externally_reachable_files(mods, all_files) == all_files


def test_public_mod_behind_a_private_ancestor_is_still_unreachable():
    mods = [
        _mod("outer", "outer.rs", "lib.rs", "private"),
        _mod("inner", "inner.rs", "outer.rs", "public"),
    ]
    all_files = frozenset({"lib.rs", "outer.rs", "outer/inner.rs"})
    got = externally_reachable_files(mods, all_files)
    assert got == frozenset({"lib.rs"})


def test_no_recognizable_root_degrades_to_no_restriction():
    mods = [_mod("x", "x.rs", "weird.rs", "private")]
    all_files = frozenset({"weird.rs", "x.rs"})
    assert externally_reachable_files(mods, all_files) == all_files


def test_nested_module_directory_convention():
    # `mod bar;` inside a non-root file `foo.rs` resolves to `foo/bar.rs`,
    # NOT a sibling `bar.rs` — Rust's real module-path convention.
    mods = [
        _mod("foo", "foo.rs", "src/lib.rs", "public"),
        _mod("bar", "bar.rs", "src/foo.rs", "public"),
    ]
    all_files = frozenset({"src/lib.rs", "src/foo.rs", "src/foo/bar.rs"})
    assert externally_reachable_files(mods, all_files) == all_files
