"""C1/C2 ranking on the recorded post-producer product of run 2220347-7fa9b90f.

The plan sentence: after promote_by_service, src/lib.rs::format_err is in the
first 12 and src/error.rs::construct_from_adhoc is not. The recorded input is
the ranking AFTER promote_producers (format_err at slot 21, construct_from_adhoc
at slot 8), not a hand-made list of constructors. Negative control: take_batch
on that ranking, which is what the live loop dispatched.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.domain.dispatch_batch import (
    promote_by_service,
    take_batch,
)
from pipeline_core.domain.models import FnLocator

_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "dispatch"
    / "2220347_product_service.json"
)


def _loc(s: str) -> FnLocator:
    file, fn = s.rsplit("::", 1)
    return FnLocator(file, fn)


def _load() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _ordered(data: dict) -> list[tuple[FnLocator, frozenset[str]]]:
    return [(_loc(s), frozenset()) for s in data["after_producers"]]


def test_recorded_post_producer_ranking_is_the_negative_control() -> None:
    """Without promote_by_service the first 12 is the constructor head the
    live loop already re-bought: construct_from_adhoc in, format_err out."""
    data = _load()
    keys = [k for k, _ in _ordered(data)]
    assert keys.index(_loc("src/lib.rs::format_err")) + 1 == data[
        "format_err_rank_before"]
    assert keys.index(_loc("src/error.rs::construct_from_adhoc")) + 1 == data[
        "construct_from_adhoc_rank_before"]
    batch = take_batch(keys, set(), 12)
    names = {f"{k.file}::{k.fn_name}" for k in batch.keys}
    assert "src/error.rs::construct_from_adhoc" in names
    assert "src/lib.rs::format_err" not in names


def test_promote_by_service_puts_format_err_in_the_first_12() -> None:
    """The plan's claim, on the recorded post-producer ranking: format_err
    enters the cap and construct_from_adhoc leaves it."""
    data = _load()
    ranked = promote_by_service(
        _ordered(data),
        {_loc(s) for s in data["served"]},
        {_loc(s) for s in data["flat"]},
        {_loc(s) for s in data["dead"]},
    )
    batch = take_batch([k for k, _ in ranked], set(), 12)
    names = [f"{k.file}::{k.fn_name}" for k in batch.keys]
    assert "src/lib.rs::format_err" in names
    assert "src/error.rs::construct_from_adhoc" not in names


def test_dead_bodies_do_not_occupy_the_cap() -> None:
    """C2 on the recorded product: never-served dead units occupy the cap
    without the dead bucket; with it they do not. format_err still in.

    The fixture's dead list is computed (every body under the name unexecuted).
    src/ensure.rs::write_str and src/fmt.rs::write_str have a live sibling
    and stay never-served — they must remain in the first 12. src/error.rs::
    backtrace is not in the dead list: it is live at a different line and is
    an earned frontier unit.
    """
    data = _load()
    assert "src/error.rs::backtrace" not in data["dead"]
    assert "src/ensure.rs::write_str" not in data["dead"]
    assert "src/fmt.rs::write_str" not in data["dead"]
    occupy_without = [
        "src/context.rs::write_str",
        "src/ensure.rs::write_str",
        "src/fmt.rs::write_str",
        "src/error.rs::erase",
    ]
    truly_dead = [
        "src/context.rs::write_str",
        "src/error.rs::erase",
    ]
    stay_never_served = [
        "src/ensure.rs::write_str",
        "src/fmt.rs::write_str",
    ]
    served = {_loc(s) for s in data["served"]}
    flat = {_loc(s) for s in data["flat"]}
    without = promote_by_service(_ordered(data), served, flat, ())
    without_batch = {
        f"{k.file}::{k.fn_name}"
        for k in take_batch([k for k, _ in without], set(), 12).keys
    }
    assert set(occupy_without) <= without_batch
    with_dead = promote_by_service(
        _ordered(data), served, flat, {_loc(s) for s in data["dead"]})
    with_batch = {
        f"{k.file}::{k.fn_name}"
        for k in take_batch([k for k, _ in with_dead], set(), 12).keys
    }
    assert not set(truly_dead) & with_batch
    assert set(stay_never_served) <= with_batch
    assert "src/lib.rs::format_err" in with_batch
    assert "src/error.rs::construct_from_adhoc" not in with_batch


def test_empty_service_inputs_are_a_noop() -> None:
    """Walks and a virgin cluster pass empty served/flat/dead; the ranking
    must stay the post-producer order."""
    data = _load()
    ordered = _ordered(data)
    assert promote_by_service(ordered, set(), set(), ()) == ordered
