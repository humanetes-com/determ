"""The write side of the futility guidance suspension (`adapters/retros.py`).

The escalation's futile units are (file, fn) locators; the store is keyed
(file, fn, canonical container), and the record that actually steered the
live fix loop was a per-impl one (`fn == "*impl*"`). `futile_suspension_keys`
bridges the two vocabularies; `suspend_futile_guidance` stamps the records so
`domain/retro_gate.admissible_guidance` withholds their guidance for the
stamping cluster (see `tests/test_retro_gate.py` for the read side).
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.adapters.retros import (
    futile_suspension_keys,
    load_retrospectives,
    retro_key,
    retrospectives_path,
    save_retrospectives,
    suspend_futile_guidance,
)
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.retro_gate import FUTILITY_SUSPENDED

_NEXT_BACK = FnLocator("src/chain.rs", "next_back")
_ERROR = FnLocator("src/error.rs", "error")
_IS = FnLocator("src/error.rs", "is")

# fn_occurrence_index's shape: (file, fn) -> ((container, sig), ...)
_OCCS = {
    # The live case: a single-member impl — its block record IS the
    # accessor's record.
    _NEXT_BACK: (("impl DoubleEndedIterator for Chain<'_>", "fn next_back"),),
    # Two members of one impl; only one is futile.
    _ERROR: (("impl Error", "fn error"),),
    _IS: (("impl Error", "fn is"),),
}


def test_per_fn_keys_always_and_impl_keys_only_when_every_member_is_futile() -> None:
    keys = futile_suspension_keys(_OCCS, [_NEXT_BACK, _ERROR])
    assert str(retro_key("src/chain.rs", "next_back",
                         "impl DoubleEndedIterator for Chain<'_>")) in keys
    assert str(retro_key("src/error.rs", "error", "impl Error")) in keys
    # next_back is `impl DoubleEndedIterator`'s ONLY member, so the block
    # record is implicated; `impl Error` still has a non-futile member (`is`),
    # so its block record — which carries lessons about `is` too — is not.
    assert str(retro_key("src/chain.rs", "*impl*",
                         "impl DoubleEndedIterator for Chain<'_>")) in keys
    assert str(retro_key("src/error.rs", "*impl*", "impl Error")) not in keys


def test_the_whole_block_futile_implicates_the_block_record() -> None:
    keys = futile_suspension_keys(_OCCS, [_ERROR, _IS])
    assert str(retro_key("src/error.rs", "*impl*", "impl Error")) in keys


def test_suspend_stamps_only_records_that_carry_guidance(tmp_path: Path) -> None:
    k_guided = str(retro_key("src/chain.rs", "*impl*",
                             "impl DoubleEndedIterator for Chain<'_>"))
    k_bare = str(retro_key("src/error.rs", "error", "impl Error"))
    save_retrospectives(tmp_path, {
        k_guided: {"guidance": "keep the storage as a Vec",
                   "category": "WRONG_TYPE_CHOICE"},
        k_bare: {"category": "API_HALLUCINATION"},   # no guidance: no reader
    })
    stamped = suspend_futile_guidance(
        tmp_path, [k_guided, k_bare, "src/x.rs::nope::c"],
        cluster="stable", tests=["test_rev"])
    assert stamped == [k_guided]
    store = load_retrospectives(tmp_path)
    assert store[k_guided][FUTILITY_SUSPENDED] == {
        "cluster": "stable", "tests": ["test_rev"]}
    assert FUTILITY_SUSPENDED not in store[k_bare]
    # Idempotent for the same (cluster, tests); a new test widens the list.
    assert suspend_futile_guidance(
        tmp_path, [k_guided], cluster="stable", tests=["test_rev"]) == []
    assert suspend_futile_guidance(
        tmp_path, [k_guided], cluster="stable", tests=["test_iter"]) == [k_guided]
    store = load_retrospectives(tmp_path)
    assert store[k_guided][FUTILITY_SUSPENDED]["tests"] == [
        "test_iter", "test_rev"]
    # The file on disk survives a JSON round-trip (the stamp is plain data).
    raw = json.loads(retrospectives_path(tmp_path).read_text(encoding="utf-8"))
    assert raw[k_guided][FUTILITY_SUSPENDED]["cluster"] == "stable"
