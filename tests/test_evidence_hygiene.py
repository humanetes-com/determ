"""Pins for the evidence-hygiene layer: the one-shot retro migration that
clears verdicts computed under the crash-confounded gate, and the mechanical
retirement of model notes the compiler itself disproved.

Both exist because a false verdict is not inert — it locks the exact block
that must change (`fully_exhausted`) and feeds confabulated guidance back
into later prompts.
"""

from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.adapters.retros import (
    load_retrospectives,
    migrate_confounded_retros,
    retro_key,
    retrospectives_path,
    save_retrospectives,
)
from pipeline_core.domain.model_notes import (
    contradicted_notes,
    rejected_symbols_in_errors,
)

_GATE_PREVIEW = ("This dropped the number of tests that EXECUTE 48→45 (a test "
                 "binary crashed — e.g. segfault from bad unsafe code — or no "
                 "longer compiles).")
_COMPILE_PREVIEW = ("# Errors in target file:\nerror[E0599]: no function or "
                    "associated item named `uninit_array` found")


# Keys are minted exactly as production does (container canonicalised) — a
# raw string key would be silently re-keyed on load and never found again.
_FROM_KEY = retro_key("src/error.rs", "from", "impl<E> From<E> for Error")
_BUF_KEY = retro_key("src/ensure.rs", "fill", "impl Buf")


def _store(tmp_path: Path, records: dict) -> Path:
    save_retrospectives(tmp_path, records)
    return retrospectives_path(tmp_path)


def test_migration_clears_confounded_exhaustion_and_retires_gate_guidance(
        tmp_path: Path) -> None:
    _store(tmp_path, {
        # exhausted purely on a gate verdict → flag cleared AND guidance dropped
        _FROM_KEY: {
            "guidance": "Replace Self::new(error) with Self::new(Box::new(error)).",
            "error_count": 0, "fully_exhausted": True, "reset_attempts": 2,
            "last_error_preview": _GATE_PREVIEW},
        # exhausted on real compile errors → flag cleared, guidance KEPT
        _BUF_KEY: {
            "guidance": "Use array repetition, not uninit_array().",
            "error_count": 1, "fully_exhausted": True,
            "last_error_preview": _COMPILE_PREVIEW},
    })
    cleared, retired = migrate_confounded_retros(tmp_path)
    assert (cleared, retired) == (2, 1)
    after = load_retrospectives(tmp_path)
    assert not any(r.get("fully_exhausted") for r in after.values()
                   if isinstance(r, dict))
    assert after[_FROM_KEY]["guidance"] == ""
    assert after[_FROM_KEY]["reset_attempts"] == 0
    # a lesson learned from real compiler output is evidence, not noise
    assert "uninit_array" in after[_BUF_KEY]["guidance"]


def test_migration_is_one_shot(tmp_path: Path) -> None:
    _store(tmp_path, {_FROM_KEY: {"fully_exhausted": True, "error_count": 0,
                                  "guidance": "x",
                                  "last_error_preview": _GATE_PREVIEW}})
    assert migrate_confounded_retros(tmp_path) == (1, 1)
    # re-exhaustion AFTER the migration is trustworthy — never re-cleared
    recs = load_retrospectives(tmp_path)
    recs[_FROM_KEY]["fully_exhausted"] = True
    save_retrospectives(tmp_path, recs)
    assert migrate_confounded_retros(tmp_path) == (0, 0)
    assert load_retrospectives(tmp_path)[_FROM_KEY]["fully_exhausted"] is True


def test_migration_marker_survives_reload(tmp_path: Path) -> None:
    _store(tmp_path, {_FROM_KEY: {"fully_exhausted": True}})
    migrate_confounded_retros(tmp_path)
    raw = json.loads(retrospectives_path(tmp_path).read_text(encoding="utf-8"))
    assert "__migrations__" in raw
    # the marker row is bookkeeping, never mistaken for a retro record
    assert isinstance(raw["__migrations__"], list)


def test_migration_on_empty_store_is_noop(tmp_path: Path) -> None:
    assert migrate_confounded_retros(tmp_path) == (0, 0)


def test_rejected_symbols_from_compiler_diagnostics() -> None:
    err = ("error[E0599]: no function or associated item named `uninit_array` "
           "found for union `MaybeUninit<T>` in the current scope")
    assert rejected_symbols_in_errors(err) == frozenset({"uninit_array"})
    assert rejected_symbols_in_errors("error[E0308]: mismatched types") == frozenset()


def test_contradicted_notes_retires_only_the_recommending_note() -> None:
    err = ("error[E0599]: no function or associated item named `uninit_array` "
           "found for union `MaybeUninit<T>` in the current scope")
    recommending = ("I tried array repetition, but it requires T: Copy; use "
                    "MaybeUninit::<T>::uninit_array(), not array repetition.")
    warning_against = ("I tried uninit_array() and it does not exist; use "
                       "array repetition, not uninit_array().")
    unrelated = "I tried calling .clone() on a backtrace; use references."
    got = contradicted_notes([recommending, warning_against, unrelated], err)
    assert got == [recommending]


def test_contradicted_notes_needs_compiler_evidence() -> None:
    note = "use MaybeUninit::<T>::uninit_array(), not array repetition."
    # no rejection diagnostic → nothing is disproved → nothing retired
    assert contradicted_notes([note], "error[E0308]: mismatched types") == []
