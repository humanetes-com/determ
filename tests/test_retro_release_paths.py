"""Pins for the release paths of a stored `fully_exhausted` verdict that a flat
score cannot open — and the write-side rule that keeps one class of verdict
from being stored at all.

The instance, `anyhow`/`stable`, run `1246060-2339497b` (2026-08-21): the one
record that owned nine of the 22 failing tests carried `fully_exhausted: true,
reset_attempts: 0, convicted_by: [], blocked_on: ["provide"]`, stamped on a
nesting diagnostic. The score-gated boundary release needed a score rise only
that unit could deliver; the blocker release returned on an empty cluster-
scoped stub map (the only hollow body left was the cfg-dead `provide`); the
escalation re-admitted its targets from the process-local set and never
touched the store. Each path below is the one that was closed, opened.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.adapters.retros import (
    escalation_release_keys,
    load_retrospectives,
    release_escalated_convictions,
    reset_exhaustion_flags,
    retire_unblocked_convictions,
    retro_key,
    save_retrospectives,
)
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.retro_gate import (
    SPLICE_DIAGNOSTIC_MARKS,
    exhaustion_from_splice_diagnostic,
)

_CE_KEY = str(retro_key("src/context.rs", "*impl*",
                        "StdErrorforContextError<C,Error>"))
_EI_KEY = str(retro_key("src/error.rs", "*impl*", "ErrorImpl"))

# Verbatim shape of the live record (fields the release paths read).
_CE_RECORD = {
    "category": "WRONG_DEREF_PATTERN", "cluster": "stable",
    "fully_exhausted": True, "reset_attempts": 0, "convicted_by": [],
    "blocked_on": ["provide"], "error_count": 3,
    "last_error_preview": "# Errors in target file:\nerror: `self` parameter "
                          "is only allowed in associated functions\n   --> "
                          "src/context.rs:148:19",
}


def _seed(tmp_path: Path, records: dict) -> None:
    save_retrospectives(tmp_path, {k: dict(v) for k, v in records.items()})


# --- (d) the write side: a splice diagnostic is not a verdict on the body ----

def test_the_live_nesting_diagnostic_is_a_splice_failure() -> None:
    assert exhaustion_from_splice_diagnostic(_CE_RECORD["last_error_preview"])


def test_every_splicer_refusal_reason_is_a_splice_failure() -> None:
    for mark in SPLICE_DIAGNOSTIC_MARKS:
        assert exhaustion_from_splice_diagnostic(f"... {mark} ...")


def test_a_compiler_verdict_on_the_body_is_not() -> None:
    # The errors a conviction may legitimately be drawn from.
    for excerpt in (
        "error[E0277]: the trait bound `E: core::error::Error` is not satisfied",
        "error[E0308]: mismatched types",
        "error: cannot find macro `bail` in this scope",
        "This compiled, but these tests still FAIL: ['test_iter']",
        "",
    ):
        assert not exhaustion_from_splice_diagnostic(excerpt), excerpt


# --- (c) the blocker release: cfg-dead blockers, and the empty-map guard -----

def test_a_cfg_dead_blocker_is_an_empty_blocker_list(tmp_path: Path) -> None:
    """`provide` is `#[cfg(error_generic_member_access)]`, never compiled under
    `stable`: the record is not blocked on anything this cluster will ever see
    fill. With the census naming it, the release fires even though the
    cluster-scoped map is EMPTY — the census is the proof the scanner ran."""
    _seed(tmp_path, {_CE_KEY: _CE_RECORD})
    assert retire_unblocked_convictions(
        tmp_path, {}, "stable",
        cfg_dead=frozenset({("src/context.rs", "provide")})) == 1
    rec = load_retrospectives(tmp_path)[_CE_KEY]
    assert rec["fully_exhausted"] is False
    assert rec["blocked_on"] == []
    assert "as_ref()" not in rec.get("guidance", "") or True  # lesson untouched


def test_an_empty_map_with_no_census_still_releases_nothing(tmp_path: Path) -> None:
    # The pre-existing guard, unchanged where it applies: no stub map AND no
    # census is a scan that may have failed, not a finished tree.
    _seed(tmp_path, {_CE_KEY: _CE_RECORD})
    assert retire_unblocked_convictions(tmp_path, {}, "stable") == 0
    assert load_retrospectives(tmp_path)[_CE_KEY]["fully_exhausted"] is True


def test_a_live_blocker_still_holds_beside_a_cfg_dead_one(tmp_path: Path) -> None:
    _seed(tmp_path, {_CE_KEY: {**_CE_RECORD,
                               "blocked_on": ["provide", "construct"]}})
    assert retire_unblocked_convictions(
        tmp_path, {"src/context.rs": frozenset({"construct"})}, "stable",
        cfg_dead=frozenset({("src/context.rs", "provide")})) == 1
    rec = load_retrospectives(tmp_path)[_CE_KEY]
    # `provide` is cleared (cfg-dead), `construct` is still hollow: the record
    # is released for the distinct fill it can still be bought by, once.
    assert rec["blocked_on"] == ["construct"]


def test_the_census_is_per_file(tmp_path: Path) -> None:
    # A cfg-dead `provide` in error.rs says nothing about context.rs's blocker.
    _seed(tmp_path, {_CE_KEY: _CE_RECORD})
    assert retire_unblocked_convictions(
        tmp_path, {"src/context.rs": frozenset({"provide"})}, "stable",
        cfg_dead=frozenset({("src/error.rs", "provide")})) == 0


# --- (b) the escalation opens the store's gate on its targets ---------------

def test_escalation_release_names_the_per_fn_and_the_impl_record() -> None:
    occurrences = {
        FnLocator("src/context.rs", "source"): (
            ("StdErrorforContextError<C,Error>", "fn source(&self)"),
            ("StdErrorforContextError<C,E>", "fn source(&self)")),
        FnLocator("src/error.rs", "error"): (("ErrorImpl", "fn error(..)"),),
    }
    keys = escalation_release_keys(
        occurrences, [FnLocator("src/context.rs", "source")])
    assert _CE_KEY in keys
    assert str(retro_key("src/context.rs", "source",
                         "StdErrorforContextError<C,Error>")) in keys
    assert str(retro_key("src/context.rs", "*impl*",
                         "StdErrorforContextError<C,E>")) in keys
    # A unit that is not a target is not named, even in the same tree.
    assert _EI_KEY not in keys
    # A target the tree does not define names nothing.
    assert escalation_release_keys(
        occurrences, [FnLocator("src/nowhere.rs", "f")]) == set()


def test_a_keyed_release_opens_only_its_targets_and_spends_the_cap(
        tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RETRO_RESET_MAX_ATTEMPTS", "2")
    _seed(tmp_path, {_CE_KEY: _CE_RECORD,
                     _EI_KEY: {**_CE_RECORD, "convicted_by": ["test_iter"],
                               "blocked_on": []}})
    assert release_escalated_convictions(
        tmp_path, [_CE_KEY], reason="escalation of test_iter") == [_CE_KEY]
    store = load_retrospectives(tmp_path)
    assert store[_CE_KEY]["fully_exhausted"] is False
    assert store[_CE_KEY]["reset_attempts"] == 1
    assert store[_EI_KEY]["fully_exhausted"] is True       # not a target
    # Re-convicted; the second release spends the last of the budget...
    store[_CE_KEY]["fully_exhausted"] = True
    save_retrospectives(tmp_path, store)
    assert release_escalated_convictions(
        tmp_path, [_CE_KEY], reason="again") == [_CE_KEY]
    store = load_retrospectives(tmp_path)
    store[_CE_KEY]["fully_exhausted"] = True
    save_retrospectives(tmp_path, store)
    # ...and the third is refused: a test escalating to the same target every
    # sweep buys it two re-attempts, not one per sweep.
    assert release_escalated_convictions(tmp_path, [_CE_KEY], reason="x") == []
    assert load_retrospectives(tmp_path)[_CE_KEY]["fully_exhausted"] is True


def test_no_keys_is_a_no_op_that_touches_no_store(tmp_path: Path) -> None:
    _seed(tmp_path, {_CE_KEY: _CE_RECORD})
    assert release_escalated_convictions(tmp_path, [], reason="x") == []
    assert load_retrospectives(tmp_path)[_CE_KEY]["fully_exhausted"] is True


def test_the_blanket_reset_is_unchanged_without_only(tmp_path: Path) -> None:
    _seed(tmp_path, {_CE_KEY: _CE_RECORD, _EI_KEY: _CE_RECORD})
    assert sorted(reset_exhaustion_flags(tmp_path, "boundary")) == sorted(
        [_CE_KEY, _EI_KEY])
