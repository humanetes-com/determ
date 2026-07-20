"""Pins for crate-scoped name resolution (bare idents → the item they denote)."""
from __future__ import annotations

from pipeline_core.domain.models import ModRecord, UseRecord
from pipeline_core.domain.rust_name_scope import ModuleScopes


def _mod(name, file, declared_in="lib.rs"):
    return ModRecord(name=name, file=file, declared_in=declared_in, vis="private")


def _use(file, alias, path, is_glob=False):
    return UseRecord(file=file, alias=alias, path=tuple(path), is_glob=is_glob)


def _anyhow_scopes():
    """The real shape: the crate root re-binds `StdError` to std's trait while
    an inline `mod ext` in context.rs declares a private trait of that name."""
    mods = [_mod("context", "context.rs"), _mod("error", "error.rs"),
            _mod("ptr", "ptr.rs")]
    uses = [
        _use("lib.rs", "StdError", ["std", "error", "Error"]),
        _use("error.rs", "StdError", ["crate", "StdError"]),
        _use("error.rs", "Own", ["crate", "ptr", "Own"]),
        _use("context.rs", "StdError", ["crate", "StdError"]),
    ]
    return ModuleScopes.from_records(mods, uses)


def _resolver(scopes):
    return scopes.resolver([("StdError", "context.rs"), ("Own", "ptr.rs")])


def test_module_paths_walk_from_the_crate_root():
    scopes = _anyhow_scopes()
    assert scopes.module_of_file["lib.rs"] == ()
    assert scopes.module_of_file["context.rs"] == ("context",)
    assert scopes.module_of_file["ptr.rs"] == ("ptr",)


def test_crate_root_rebinding_is_not_a_private_reference():
    # `use crate::StdError` reaches the ROOT's re-export of std's trait, never
    # `crate::context::ext::StdError` — the collision that welded 43 unrelated
    # items into one component when names were matched bare.
    r = _resolver(_anyhow_scopes())
    assert r.resolve("error.rs", frozenset({"StdError"})) == frozenset()
    assert r.resolve("context.rs", frozenset({"StdError"})) == frozenset()


def test_explicit_use_wins_over_a_same_file_declaration():
    # context.rs both declares the private `StdError` (inside an inline mod)
    # and `use`s the root one; at file scope the `use` governs.
    r = _resolver(_anyhow_scopes())
    assert r.resolve("context.rs", frozenset({"StdError"})) == frozenset()


def test_same_file_declaration_binds_without_a_use():
    scopes = ModuleScopes.from_records([_mod("ptr", "ptr.rs")], [])
    r = scopes.resolver([("Own", "ptr.rs")])
    assert r.resolve("ptr.rs", frozenset({"Own"})) == frozenset({"Own"})
    assert r.resolve("lib.rs", frozenset({"Own"})) == frozenset()


def test_crate_path_import_binds_across_files():
    r = _resolver(_anyhow_scopes())
    assert r.resolve("error.rs", frozenset({"Own"})) == frozenset({"Own"})


def test_alias_resolves_to_the_declared_name():
    scopes = ModuleScopes.from_records(
        [_mod("ptr", "ptr.rs"), _mod("error", "error.rs")],
        [_use("error.rs", "P", ["crate", "ptr", "Own"])])
    r = scopes.resolver([("Own", "ptr.rs")])
    assert r.resolve("error.rs", frozenset({"P"})) == frozenset({"Own"})
    assert r.resolve("error.rs", frozenset({"Own"})) == frozenset()


def test_glob_import_binds_the_globbed_module():
    scopes = ModuleScopes.from_records(
        [_mod("ptr", "ptr.rs"), _mod("error", "error.rs")],
        [_use("error.rs", "*", ["crate", "ptr"], is_glob=True)])
    r = scopes.resolver([("Own", "ptr.rs")])
    assert r.resolve("error.rs", frozenset({"Own"})) == frozenset({"Own"})


def test_self_and_super_paths_resolve_relative_to_the_file():
    scopes = ModuleScopes.from_records(
        [_mod("ptr", "ptr.rs"), _mod("error", "error.rs")],
        [_use("error.rs", "Own", ["super", "ptr", "Own"]),
         _use("ptr.rs", "Own", ["self", "Own"])])
    r = scopes.resolver([("Own", "ptr.rs")])
    assert r.resolve("error.rs", frozenset({"Own"})) == frozenset({"Own"})
    assert r.resolve("ptr.rs", frozenset({"Own"})) == frozenset({"Own"})


def test_external_crate_paths_never_denote_a_private_item():
    scopes = ModuleScopes.from_records(
        [_mod("error", "error.rs")],
        [_use("error.rs", "Own", ["some_crate", "ptr", "Own"])])
    r = scopes.resolver([("Own", "ptr.rs")])
    assert r.resolve("error.rs", frozenset({"Own"})) == frozenset()


def test_unplaceable_file_is_addressable_only_from_itself():
    # A file with no place in the module tree must not absorb `crate::X`
    # references meant for a real module.
    scopes = ModuleScopes.from_records(
        [_mod("error", "error.rs")],
        [_use("error.rs", "Orphan", ["crate", "Orphan"])])
    r = scopes.resolver([("Orphan", "detached.rs")])
    assert r.resolve("error.rs", frozenset({"Orphan"})) == frozenset()
    assert r.resolve("detached.rs", frozenset({"Orphan"})) == frozenset({"Orphan"})


def test_no_module_facts_degrades_to_bare_name_matching():
    r = ModuleScopes().resolver([("Own", "ptr.rs")])
    assert r.resolve("anywhere.rs", frozenset({"Own"})) == frozenset({"Own"})
