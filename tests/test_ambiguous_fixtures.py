"""Fixture-ambiguity gate: unique-name trees fail; a homonym crate passes."""
from __future__ import annotations

from tools.check_ambiguous_fixtures import ambiguity_of, would_fail


def test_unique_names_would_fail_the_gate() -> None:
    """Negative control: a crate of distinct (file, name) pairs has
    ambiguous% == 0, and a fixture set of only those fails the gate."""
    pairs = [("src/a.rs", "foo"), ("src/a.rs", "bar"), ("src/b.rs", "foo")]
    st = ambiguity_of(pairs)
    assert st["ambiguous_pct"] == 0.0
    assert st["fns"] == 3 and st["keys"] == 3
    assert would_fail([st])


def test_homonym_crate_keeps_the_gate_green() -> None:
    """argh-shaped: four set_flag on one key → ambiguous% > 0."""
    pairs = [("src/lib.rs", "set_flag")] * 4 + [("src/lib.rs", "unique")]
    st = ambiguity_of(pairs)
    assert st["ambiguous_keys"] == 1
    assert st["worst"] == 4
    assert st["ambiguous_pct"] > 0
    unique = ambiguity_of([("src/a.rs", "a"), ("src/a.rs", "b")])
    assert not would_fail([unique, st])
    assert would_fail([])
